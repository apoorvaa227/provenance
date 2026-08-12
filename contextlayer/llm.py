"""The model, in the one place it belongs.

The held-out paraphrase run says exactly where the deterministic layer breaks.
Quality falls from 100% to 45.3% on wordings it has never seen, and every
single failure is an abstention — 55 questions it could have answered, 13
policy questions it declined to classify. Zero invented answers, zero
disclosures. Comprehension degrades; safety does not.

That failure profile says precisely what to fix and what to leave alone. The
broken part is *understanding what was asked*. The parts that held — computing
figures from records, checking citations, masking identifiers, refusing on
policy — held because they never depended on recognising a sentence.

So the model gets one job: decide what the question was. It never sees a
figure, never produces a number, never touches a record. A classification is
the one thing here that is genuinely a language task, and it is also the one
place where being wrong is cheap — a misclassification routes to the wrong
desk, and the wrong desk abstains.

Three properties follow, and each is a deliberate choice:

**The model is an enhancement, not a dependency.** Any failure — no API key,
rate limit, timeout, malformed output, provider outage — falls back to the
regex classifier rather than failing the question. The blackout behaviour of
this service is "quality returns to 45%", not "the service is down".

**The model cannot widen what the layer will disclose.** It returns one label
from a closed set. It cannot invent an intent, cannot request a record, and
cannot reach the response — everything downstream of the label runs exactly as
it did before, including the verifier. An adversarial classification routes to
a desk that answers from the wrong records and is then caught by the citation
check, which is why this placement is safe in a way "let the model write the
answer" would not be.

**It is cheap and it is measured.** One short call per question against a
cached system prompt, at low effort. Token counts land in the run's usage
file, so the comparison reports what the recovery cost as well as what it
bought.
"""
from __future__ import annotations

import os
import time

from contextlayer.agents import RULES, RegexClassifier
from contextlayer.provider import Provider

MAX_RETRIES = int(os.environ.get("PROVENANCE_MAX_RETRIES", "2"))

INTENTS = [name for name, _ in RULES] + ["unknown"]

# One line per intent, phrased as the decision rather than the implementation.
# The model is choosing between questions, not between desks.
GUIDE = """\
advice             asking what SHOULD be done — deprecate, consolidate, grant
                   access, reclassify, rewrite a definition. A recommendation,
                   not a lookup.
restricted_detail  asking to see the actual data — sample values, rows, "what
                   does it look like", "paste me a few records".
multi              asking two things at once, where one is ownership or
                   certification and the other is freshness or pipeline health.
lineage_path       asking whether one specific asset feeds another.
lineage_count      asking how many assets are downstream, the blast radius,
                   what breaks if this changes.
trust              asking whether the asset can be relied on — safe to build
                   on, trustworthy, "would you use this".
usage              asking whether anyone is still querying it, whether people
                   have migrated off.
freshness          asking when it last landed, whether it is stale, whether it
                   is inside its SLA.
issues             asking about known problems, complaints, open issues, or
                   "anything I should know before using this".
glossary           asking what a business TERM means — a phrase in quotes or a
                   named concept, not an asset.
pii                asking whether a COLUMN holds personal data, or what kind of
                   data it holds, or whether there are privacy concerns.
classification     asking about the asset's sensitivity level or policy tags.
column_meaning     asking what a specific named COLUMN means or contains.
ownership          asking who owns it, who is accountable, who to talk to.
documentation      asking what the ASSET is for or what it contains.
unknown            none of the above, or genuinely ambiguous."""

SYSTEM = f"""You classify questions about a data catalog into exactly one \
intent. You are not answering the question and you never will — a separate \
deterministic layer computes every answer from records. Your only output is \
the label.

{GUIDE}

Two distinctions carry most of the weight, because they decide whether the \
question gets answered at all:

- What the records SAY versus what someone SHOULD DO about it. "Has this \
breached its SLA" is freshness; "should we deprecate this" is advice. They \
often appear in the same sentence — classify on what is being asked for.
- Whether the subject is an ASSET (a table or view, usually DB.SCHEMA.NAME), a \
COLUMN within one, or a business TERM. The same question shape means different \
things for each.

Colloquial and indirect phrasings are expected: "is this stale?", "who's the \
go-to person if this breaks?", "my numbers look off and they come from here". \
Classify on what the person needs to know, not on the words they used.

When genuinely torn between two intents, prefer the narrower one. When nothing \
fits, return unknown — a wrong label produces a confident answer to a question \
nobody asked, and an honest unknown produces an abstention."""

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "subject": {"type": "string",
                    "enum": ["asset", "column", "term", "none"]},
    },
    "required": ["intent", "subject"],
    "additionalProperties": False,
}


class ModelClassifier:
    """Intent only. Drop-in for `RegexClassifier` — same call signature, so
    swapping them is a constructor argument rather than a code path."""

    name = "model"

    def __init__(self, provider: Provider | None = None):
        self.fallback = RegexClassifier()
        self._in = self._out = self._calls = self._failed = self._cached = 0
        self._latency = 0.0
        self._client = None
        self._reason: str | None = None
        # A missing key does not announce itself at construction — the SDK
        # resolves credentials lazily, so the first failure is the first call.
        # Without this, a keyless run pays three timeouts on every one of 110
        # questions to learn the same fact 110 times.
        self._consecutive_failures = 0
        self._breaker_at = int(os.environ.get("PROVENANCE_BREAKER", "3"))
        try:
            self.provider = provider or Provider.detect()
        except Exception as e:                            # noqa: BLE001
            # No credentials, no package, no network — all the same thing from
            # here: the deterministic path handles every question instead.
            self.provider = None
            self._reason = f"{type(e).__name__}: {e}"[:200]

    # -- accounting -------------------------------------------------------

    def usage(self) -> dict:
        return {
            "classifier": ("regex (no provider available)" if self.provider is None
                           else "regex (circuit open after repeated failures)"
                           if self._consecutive_failures >= self._breaker_at
                           else self.name),
            **(self.provider.describe() if self.provider else
               {"provider": None, "model": None}),
            "model_calls": self._calls,
            "model_calls_failed": self._failed,
            "tokens_in": self._in,
            "tokens_out": self._out,
            "cached_tokens_in": self._cached,
            "mean_classify_latency_s": round(
                self._latency / self._calls, 4) if self._calls else 0.0,
            "unavailable_reason": self._reason,
        }

    # -- classification ---------------------------------------------------

    def __call__(self, prompt: str) -> str:
        if self.provider is None:
            return self.fallback(prompt)
        if self._consecutive_failures >= self._breaker_at:
            # Open circuit. The run continues on the deterministic path and
            # says so in its usage file, rather than reporting a model arm
            # that silently never ran.
            return self.fallback(prompt)

        for attempt in range(MAX_RETRIES + 1):
            try:
                started = time.perf_counter()
                text, usage = self.provider.complete(
                    SYSTEM, prompt, max_tokens=200, schema=SCHEMA)
                self._latency += time.perf_counter() - started
                self._calls += 1
                self._in += usage["in"]
                self._out += usage["out"]
                self._cached += usage["cached"]
                self._consecutive_failures = 0

                intent = Provider.parse_json(text).get("intent", "unknown")
                # The schema constrains this, but the enum is the authority on
                # what the router can dispatch — an unrecognised label would
                # fall through to an abstention silently.
                return intent if intent in INTENTS else "unknown"

            except Exception as e:                       # noqa: BLE001
                if attempt < MAX_RETRIES:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                self._failed += 1
                self._consecutive_failures += 1
                if self._reason is None:
                    self._reason = f"{type(e).__name__}: {e}"[:200]
                # Degrade, never fail. A classification the model could not
                # produce is one the regex still can, and the answer that
                # follows is computed from records either way.
                return self.fallback(prompt)
        return self.fallback(prompt)
