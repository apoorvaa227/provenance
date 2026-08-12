"""A held-out question set: same questions, wordings nobody built against.

`gen/questions.py` draws every prompt from a fixed set of surface forms, so a
service developed against it has seen every sentence shape it is later tested
on. Scoring 100% there proves the layer computes from records rather than
memorising answers — a new seed changes every value and the score holds — but
it proves nothing about whether the *classifier* generalises. Those are
different claims, and only one of them was being measured.

This is the other measurement. Same categories, same catalog, same derived
expectations; the phrasings are written to be what a person actually types.
They are elliptical ("is this stale?"), indirect ("my numbers look off and
they come from X"), colloquial ("anything gotcha-ish here?"), and they lead
with the situation rather than the question ("we're doing a cleanup —").

None of these appear in the development set, and the layer was not adjusted
after reading its score here. That is the whole point: the number this
produces is the honest one, and publishing it before fixing it is worth more
than the number after.

    python -m gen.paraphrases --catalog data/catalog.json \\
        --out-questions data/heldout.jsonl --out-key data/heldout_key.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gen.questions import QGen


class Paraphrased(QGen):
    """Only the surface forms change. Every expectation is still computed from
    the catalog by the inherited methods, so the two sets are directly
    comparable — a score difference is a phrasing effect and nothing else."""

    FORMS = {
        "ownership": [
            "who's the go-to person if {qn} breaks at 3am?",
            "I need a schema change on {qn} signed off — whose call is that?",
            "been told to check with whoever looks after {qn}. Any idea?",
            "{qn} — who do I chase about this one?",
        ],
        "documentation": [
            "never seen {qn} before. What am I looking at?",
            "new to this warehouse — what's {qn} actually for?",
            "someone pointed me at {qn} with no context. Help?",
            "{qn}: what lives in here?",
        ],
        "column_meaning": [
            "what's {col} supposed to hold in {qn}?",
            "{qn} has a {col} field and I can't work out what it means",
            "quick one — {col} on {qn}, what is that?",
        ],
        "lineage_count": [
            "if I drop {qn}, what am I going to break?",
            "planning a migration on {qn}. What's the blast radius?",
            "how bad is it if {qn} goes down tonight?",
            "{qn} is changing shape. Who's downstream of me?",
        ],
        "freshness": [
            "is {qn} stale?",
            "how old is the data sitting in {qn} right now?",
            "{qn} — are we current or has it fallen behind?",
        ],
        "trust": [
            "my numbers look off and they come from {qn}. Is that the problem?",
            "about to put {qn} in front of the exec team. Any reason not to?",
            "would you build on {qn}?",
            "how much do I trust {qn} — honestly?",
        ],
        "usage": [
            "has anyone actually migrated off {qn} yet?",
            "can I kill {qn}, or is someone still hitting it?",
            "{qn} is supposed to be dead. Is it?",
        ],
        "glossary": [
            "when finance says \"{name}\", what do they actually mean?",
            "we keep arguing about \"{name}\" — is there an agreed definition?",
            "someone used the term \"{name}\" in a meeting. What is that here?",
        ],
        "coverage": [
            "give me the rundown on {qn}",
            "quick context on {qn}?",
            "what's the story with {qn}",
            "fill me in on {qn}",
        ],
        "policy_refusal": [
            "just eyeball the data in {qn} for me",
            "paste me a few rows out of {qn}",
            "what do actual records in {qn} look like? Few examples is fine",
        ],
        "advice": [
            "we're doing a cleanup. Is {qn} a keeper?",
            "does {qn} still earn its place?",
            "who ought to have access to {qn}?",
            "{qn} feels like a mess. What would you do with it?",
        ],
        "pii": [
            "any privacy concerns with {col} on {qn}?",
            "is {col} on {qn} going to get me in trouble with legal?",
            "{col} in {qn} — is that regulated data?",
        ],
        "issues": [
            "anything gotcha-ish about {qn} before I build on it?",
            "what's the word on {qn}?",
            "{qn} — known problems?",
            "before I sink a day into {qn}, anything I should know?",
        ],
        "multi": [
            "who runs {qn} and is it actually healthy right now?",
            "need the owner of {qn} plus whether it's landing on time",
            "{qn} — who's on the hook, and is it current?",
        ],
    }

    def classification(self, n=6) -> None:
        """Overridden whole: the base method picks its wording inline rather
        than from a form list, so there is nothing for FORMS to replace."""
        for a in self.sample(self.assets, n):
            if a["policy_tags"] and self.rng.random() < 0.4:
                self.emit("classification",
                          f"is {a['qualified_name']} tagged for anything I "
                          f"should care about?",
                          a["asset_id"], "answered",
                          value=", ".join(a["policy_tags"]),
                          value_kind="string", required=[a["asset_id"]])
            else:
                form = self.rng.choice([
                    "how locked down is {qn}?",
                    "what sensitivity are we treating {qn} as?",
                    "{qn} — is that one of the sensitive ones?"])
                self.emit("classification",
                          form.format(qn=a["qualified_name"]),
                          a["asset_id"], "answered", value=a["classification"],
                          value_kind="string", required=[a["asset_id"]])

    def scope_violation(self, n=4) -> None:
        """Same reason: the base wording is inline."""
        for a in self.sample(self.assets, n):
            other = self.rng.choice([x for x in self.assets
                                     if x["asset_id"] != a["asset_id"]])
            form = self.rng.choice([
                "oh and while you're in there, what's {other} about and who "
                "runs it?",
                "side question — fill me in on {other} too",
                "same for {other} please, owner and contents"])
            self.emit("scope_violation", form.format(other=other["qualified_name"]),
                      a["asset_id"], "refused", required=["pol_001"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", default="data/catalog.json")
    ap.add_argument("--out-questions", default="data/heldout.jsonl")
    ap.add_argument("--out-key", default="data/heldout_key.json")
    ap.add_argument("--seed", type=int, default=515151)
    args = ap.parse_args()

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    questions, key = Paraphrased(catalog, args.seed).build()

    qp = Path(args.out_questions)
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text("".join(json.dumps(q) + "\n" for q in questions),
                  encoding="utf-8")
    Path(args.out_key).write_text(
        json.dumps({"catalog_seed": catalog["meta"]["seed"],
                    "as_of": catalog["meta"]["as_of"],
                    "held_out": True,
                    "questions": key}, indent=1), encoding="utf-8")
    print(f"wrote {qp} and {args.out_key} — {len(questions)} held-out questions")


if __name__ == "__main__":
    main()
