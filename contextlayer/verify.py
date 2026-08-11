"""The last gate before an answer ships.

Every answer passes through here, whatever produced it — a deterministic desk,
a model, or a model that ignored its instructions. That placement is the whole
argument. A masking rule enforced inside the specialist that happened to run is
a rule that holds only as long as every specialist remembers it, and a new one
written next month will not. A masking rule enforced on the assembled response
holds for code nobody has written yet.

The verifier checks five things and it does not trust the caller on any of them:

  shape        the response is well formed, and the disposition is internally
               consistent. Abstaining or refusing while still carrying a value
               is the specific bug that lets a hedged sentence ship a number.

  citations    every cited id is real, and every cited id is inside the set the
               scoped question authorised. Citing a true fact about a record
               the question did not reach for is still a scope break.

  grounding    an answered response with no citation is downgraded. Not to a
               refusal — to an abstention, because that is what it is: a claim
               with nothing behind it.

  masking      personal data is masked by *value* and by *shape*. Value catches
               what the catalog knows; shape catches what it does not. Any
               plausible identifier is rewritten regardless of provenance.

  canaries     if injected record text reached the surface, it is removed and
               flagged. This is defence in depth — the scorer checks for the
               same tokens independently, so a verifier bug shows up as a
               failed run rather than a clean one.

Nothing here asks whether the answer is *good*. It asks whether the answer is
allowed to leave.
"""
from __future__ import annotations

import re

# Shapes, not values. Keyed on what an identifier looks like rather than on a
# list of known ones, so an identifier the catalog never held is still caught.
SHAPES = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("national_id", re.compile(r"\bNID-\d{6,10}\b")),
    ("phone", re.compile(r"\+\d{1,3}-\d{1,3}-\d{6,10}\b")),
    ("ip_address", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
]
CANARY = re.compile(r"\bPVN-CANARY-[A-Z0-9]{2,10}\b", re.I)

DISPOSITIONS = ("answered", "abstained", "refused")


def mask(value: str) -> str:
    """Last four characters survive. Enough to reconcile a record against one
    you already hold, not enough to learn one you do not."""
    tail = str(value)[-4:]
    return f"****{tail}"


def scrub(text: str, known_values: set[str]) -> tuple[str, list[str]]:
    """Rewrite anything that looks like personal data. Returns the cleaned
    text and the kinds of thing that were found, so a leak that the desks
    should have prevented is visible rather than silently repaired."""
    found: list[str] = []
    if not text:
        return text, found

    # Known values first — they are exact, and doing them first stops a shape
    # rule from half-masking a value the catalog could have masked properly.
    for value in sorted(known_values, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, mask(value))
            found.append("known_value")

    for kind, pattern in SHAPES:
        if pattern.search(text):
            text = pattern.sub(lambda m: mask(m.group(0)), text)
            found.append(kind)

    return text, sorted(set(found))


def blank() -> dict:
    return {"answer": "", "answer_value": None, "abstained": True,
            "refused": False, "reason": None, "citations": [],
            "confidence": 0.0, "flags": [], "agents": ["router"]}


class Verifier:
    role = "verifier"

    def __init__(self, catalog):
        self.cat = catalog

    def check(self, draft: dict, scope_asset_id: str | None) -> dict:
        out = {**blank(), **{k: v for k, v in (draft or {}).items()
                             if v is not None or k in ("answer_value", "reason")}}
        flags = set(out.get("flags") or [])

        # -- shape ---------------------------------------------------------
        out["abstained"] = bool(out.get("abstained"))
        out["refused"] = bool(out.get("refused"))
        if out["abstained"] and out["refused"]:
            # Cannot be both. Refusal is the stronger claim — it says the
            # answer exists and policy withholds it — so it wins.
            out["abstained"] = False
        if out["abstained"] or out["refused"]:
            out["answer_value"] = None

        out["answer"] = str(out.get("answer") or "")
        try:
            out["confidence"] = min(1.0, max(0.0, float(out.get("confidence", 0.0))))
        except (TypeError, ValueError):
            out["confidence"] = 0.0

        # -- citations -----------------------------------------------------
        allowed = self.cat.citable(scope_asset_id)
        kept, unknown, out_of_scope = [], 0, 0
        for cid in out.get("citations") or []:
            if not isinstance(cid, str) or not self.cat.known_id(cid):
                unknown += 1
                continue
            if allowed and cid not in allowed:
                out_of_scope += 1
                continue
            if cid not in kept:
                kept.append(cid)
        out["citations"] = kept
        if unknown:
            flags.add("citation_not_found")
        if out_of_scope:
            flags.add("citation_out_of_scope")

        # -- grounding -----------------------------------------------------
        # An answer with nothing behind it is not an answer. It becomes an
        # abstention rather than a refusal, because no policy withheld it —
        # the layer simply failed to find a record, and should say so.
        if not out["abstained"] and not out["refused"] and not kept:
            out["abstained"] = True
            out["answer_value"] = None
            out["reason"] = out.get("reason") or \
                "no catalog record supports this answer"
            flags.add("ungrounded_downgraded")

        # -- masking and canaries ------------------------------------------
        for field in ("answer", "reason"):
            text = out.get(field)
            if not text:
                continue
            if CANARY.search(text):
                text = CANARY.sub("[redacted]", text)
                flags.add("injection_leaked")
            text, found = scrub(text, self.cat.pii_values)
            if found:
                flags.add("masked")
            out[field] = text

        if isinstance(out.get("answer_value"), str):
            value, found = scrub(out["answer_value"], self.cat.pii_values)
            if CANARY.search(value):
                value = CANARY.sub("[redacted]", value)
                flags.add("injection_leaked")
            if found:
                flags.add("masked")
            out["answer_value"] = value

        # -- roster --------------------------------------------------------
        agents = [a for a in (out.get("agents") or []) if isinstance(a, str)]
        if "router" not in agents:
            agents.insert(0, "router")
        if self.role not in agents:
            agents.append(self.role)
        out["agents"] = agents

        out["flags"] = sorted(flags)
        return out
