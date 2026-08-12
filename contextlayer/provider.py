"""Whichever model is available, behind one interface.

The context layer needs a model for exactly one thing — deciding what a
question was asking — and that job does not care whose model does it. Binding
the project to a single vendor would be a choice made for no engineering
reason, so this is the seam: one `complete()` call, four providers behind it,
selected from the environment.

    Anthropic       ANTHROPIC_API_KEY        native SDK
    Google Gemini   GEMINI_API_KEY           OpenAI-compatible endpoint
    Groq            GROQ_API_KEY             OpenAI-compatible endpoint
    Ollama          nothing — localhost      OpenAI-compatible endpoint

Three of those speak the same protocol, so they share one implementation.
Anthropic gets its own path rather than being routed through a compatibility
shim, because a shim would quietly drop the features that path actually uses.

Selection is automatic and reported. A run's usage file always names the
provider and model that produced it, so a comparison can never silently be
between two different models — the single most common way an eval lies about
what it measured.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> list[str]:
    """Read `.env` into the environment, if it exists.

    Twelve lines instead of a dependency, and it keeps the credential in one
    gitignored file rather than in shell history, terminal scrollback and
    every `env` dump. Existing environment variables win — an explicitly
    exported key should not be silently overridden by a stale file.
    """
    loaded = []
    p = Path(path)
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


load_dotenv()

# Cloud endpoints that implement the OpenAI chat-completions protocol. Ollama
# is here too, which is why "run this with no account and no network" costs
# nothing extra to support.
COMPATIBLE = {
    "gemini": {
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        # The `-latest` alias rather than a pinned version: model ids move, and
        # a project someone clones in three months should run. `served_model`
        # below records what the alias actually resolved to, so a published
        # number still says which model produced it.
        "model": "gemini-flash-latest",
        "json_schema": True,
    },
    "groq": {
        "env": ("GROQ_API_KEY",),
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "json_schema": False,
    },
    "ollama": {
        "env": (),
        "base_url": os.environ.get("OLLAMA_HOST",
                                   "http://localhost:11434") + "/v1",
        "model": "qwen2.5:3b",
        "json_schema": False,
    },
}


class ProviderUnavailable(RuntimeError):
    pass


class Provider:
    """One completion call, normalised usage. Nothing model-specific leaks
    past this boundary — callers see text and a token count."""

    def __init__(self, name: str, model: str, client, kind: str,
                 supports_schema: bool):
        self.name, self.model, self._client = name, model, client
        self._kind, self._schema = kind, supports_schema
        # What the provider says it actually served. When `model` is an alias
        # this is the only honest record of which weights produced a number.
        self.served_model: str | None = None

    # -- construction -----------------------------------------------------

    @staticmethod
    def _ollama_alive(base_url: str, timeout: float = 1.0) -> bool:
        import urllib.error
        import urllib.request
        try:
            urllib.request.urlopen(base_url.rstrip("/") + "/models",
                                   timeout=timeout).read()
            return True
        except Exception:                                  # noqa: BLE001
            return False

    @staticmethod
    def _key(names) -> str | None:
        for n in names:
            v = os.environ.get(n)
            if v:
                return v
        return None

    @classmethod
    def detect(cls, prefer: str | None = None) -> "Provider":
        """Explicit choice wins; otherwise the first provider with credentials.
        Ollama is last because it is the only one that can appear available
        (localhost resolves) while nothing is actually listening."""
        want = prefer or os.environ.get("PROVENANCE_PROVIDER")
        order = [want] if want else ["anthropic", "gemini", "groq", "ollama"]

        errors = []
        for name in order:
            try:
                if name == "anthropic":
                    if not cls._key(("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")) \
                            and not want:
                        continue
                    import anthropic
                    model = os.environ.get("PROVENANCE_MODEL", "claude-opus-5")
                    return cls(name, model, anthropic.Anthropic(),
                               "anthropic", True)

                spec = COMPATIBLE.get(name)
                if spec is None:
                    errors.append(f"{name}: unknown provider")
                    continue
                key = cls._key(spec["env"])
                if not key and spec["env"] and not want:
                    continue
                if name == "ollama" and not cls._ollama_alive(spec["base_url"]):
                    # Constructing a client does not open a socket, so an
                    # unreachable localhost would otherwise be "detected"
                    # successfully and then fail on every question. Probe it.
                    errors.append("ollama: nothing listening")
                    continue
                from openai import OpenAI
                model = os.environ.get("PROVENANCE_MODEL", spec["model"])
                client = OpenAI(api_key=key or "ollama",
                                base_url=spec["base_url"], timeout=60.0)
                return cls(name, model, client, "openai", spec["json_schema"])
            except Exception as e:                         # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__}: {e}")

        raise ProviderUnavailable(
            "no model provider available. Set one of GEMINI_API_KEY, "
            "GROQ_API_KEY or ANTHROPIC_API_KEY, or run ollama locally. "
            + ("Tried — " + "; ".join(errors) if errors else ""))

    # -- the one call -----------------------------------------------------

    def complete(self, system: str, user: str, *, max_tokens: int = 512,
                 schema: dict | None = None) -> tuple[str, dict]:
        """Returns (text, usage). Raises on failure — retry and fallback
        policy belongs to the caller, which knows what a failure costs."""
        if self._kind == "anthropic":
            kw: dict = {"output_config": {"effort": "low"}}
            if schema:
                kw["output_config"]["format"] = {"type": "json_schema",
                                                 "schema": schema}
            resp = self._client.messages.create(
                model=self.model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}], **kw)
            if getattr(resp, "stop_reason", None) == "refusal":
                raise RuntimeError("model declined the request")
            text = next((b.text for b in resp.content
                         if getattr(b, "type", None) == "text"), "")
            self.served_model = getattr(resp, "model", None) or self.model
            u = resp.usage
            return text, {
                "in": getattr(u, "input_tokens", 0) or 0,
                "out": getattr(u, "output_tokens", 0) or 0,
                "cached": getattr(u, "cache_read_input_tokens", 0) or 0,
            }

        kw = {}
        if schema:
            # Providers disagree on how much of the schema they honour, so the
            # instruction is also in the prompt and the parse is defensive.
            # Constrained decoding is an optimisation here, never the contract.
            kw["response_format"] = (
                {"type": "json_schema",
                 "json_schema": {"name": "intent", "schema": schema,
                                 "strict": True}}
                if self._schema else {"type": "json_object"})
        resp = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}], **kw)
        text = resp.choices[0].message.content or ""
        self.served_model = getattr(resp, "model", None) or self.model
        u = getattr(resp, "usage", None)
        return text, {
            "in": getattr(u, "prompt_tokens", 0) or 0,
            "out": getattr(u, "completion_tokens", 0) or 0,
            "cached": 0,
        }

    # -- helper -----------------------------------------------------------

    @staticmethod
    def parse_json(text: str) -> dict:
        """Models wrap JSON in prose and fences whatever the request said.
        Take the outermost braces and parse those."""
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"no JSON object in response: {text[:120]!r}")
        return json.loads(text[start:end + 1])

    def describe(self) -> dict:
        return {"provider": self.name,
                "model_requested": self.model,
                "model_served": self.served_model}

    def models(self) -> list[str]:
        """What this provider will actually accept. Model ids move; a run that
        fails on an id that was renamed should be able to say what the current
        ones are rather than leaving you to guess."""
        try:
            if self._kind == "anthropic":
                return [m.id for m in self._client.models.list()]
            return sorted(m.id for m in self._client.models.list().data)
        except Exception as e:                             # noqa: BLE001
            return [f"(could not list models: {type(e).__name__}: {e})"[:160]]


def main() -> None:
    """`python -m contextlayer.provider` — check credentials before a run
    rather than discovering them 40 questions in."""
    try:
        p = Provider.detect()
    except ProviderUnavailable as e:
        print(f"\n  no provider\n    {e}\n")
        raise SystemExit(1)

    loaded = load_dotenv()
    if loaded:
        print(f"\n  .env       loaded {', '.join(loaded)}")
    print(f"  provider   {p.name}\n  requested  {p.model}")
    try:
        # Generous ceiling on purpose. A reasoning model can spend tokens
        # before the first brace, and a truncated reply fails the JSON parse
        # in a way that reads exactly like a bad key — which is a rubbish
        # thing for a credentials check to tell you.
        text, usage = p.complete(
            "Reply with a JSON object and nothing else.",
            'Return {"ok": true}.', max_tokens=2048)
        got = Provider.parse_json(text)
        print(f"  served     {p.served_model}")
        print(f"  live call  ok — {usage['in']} in / {usage['out']} out tokens")
        print(f"  parsed     {got}\n")
    except Exception as e:                                 # noqa: BLE001
        print(f"  live call  FAILED — {type(e).__name__}: {e}\n")
        print("  models this provider accepts:")
        for m in p.models()[:40]:
            print(f"    {m}")
        print("\n  set one with:  PROVENANCE_MODEL=<id>\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
