"""Record lookups over the metadata graph.

This is the whole tools layer. Every question the desks can answer resolves to
some composition of the methods here, and none of them consult a model — they
walk a graph and return what is in it, or return nothing.

That "or return nothing" is the load-bearing part. `resolve()` returns `None`
for a name the catalog does not hold, and `None` is not a degraded answer to be
patched over downstream; it is the answer. An agent that cannot distinguish
"this asset has no owner recorded" from "I could not find an owner" will
eventually paper over the first with a guess, and the guess will read exactly
like a lookup.

So the accessor is deliberately literal. It has no fallbacks, no fuzzy
matching that might land on a neighbour, and no default values.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime


def _d(s: str) -> date:
    """Dates in the graph are ISO, sometimes with a time component."""
    return datetime.fromisoformat(s).date() if "T" in s else date.fromisoformat(s)


class Catalog:
    def __init__(self, raw: dict):
        self.raw = raw
        self.as_of = date.fromisoformat(raw["meta"]["as_of"])

        self.assets = raw["assets"]
        self.by_id = {a["asset_id"]: a for a in self.assets}
        self.by_qn = {a["qualified_name"]: a for a in self.assets}
        self.by_qn_ci = {a["qualified_name"].upper(): a for a in self.assets}

        self.people = {p["user_id"]: p for p in raw["people"]}
        self.policies = {p["policy_id"]: p for p in raw["policies"]}
        self.sources = {s["source_id"]: s for s in raw["sources"]}

        self.terms = {t["term_id"]: t for t in raw["glossary"]}
        self.terms_by_name = {t["name"].lower(): t for t in raw["glossary"]}

        self.columns = {}
        self.column_owner = {}
        for a in self.assets:
            for c in a["columns"]:
                self.columns[c["column_id"]] = c
                self.column_owner[c["column_id"]] = a["asset_id"]

        self._down = defaultdict(set)
        self._up = defaultdict(set)
        for e in raw["lineage"]:
            self._down[e["upstream_asset_id"]].add(e["downstream_asset_id"])
            self._up[e["downstream_asset_id"]].add(e["upstream_asset_id"])

        self._runs = defaultdict(list)
        for r in raw["runs"]:
            self._runs[r["asset_id"]].append(r)
        for rows in self._runs.values():
            rows.sort(key=lambda r: r["started_at"], reverse=True)

        self._usage = {u["asset_id"]: u for u in raw["usage"]}

        self._issues = defaultdict(list)
        for i in raw["issues"]:
            self._issues[i["asset_id"]].append(i)

        # Every personal-data sample in the graph, kept as one set so the
        # verifier can scrub by value as well as by shape. Shape catches the
        # ones this set does not know about; value catches the ones whose
        # shape is unremarkable.
        self.pii_values: set[str] = set()
        for a in self.assets:
            for c in a["columns"]:
                if c["pii_type"]:
                    self.pii_values.update(c["sample_values"])

    # -- resolution -------------------------------------------------------

    def resolve(self, name: str | None):
        """An asset id or a qualified name, or None. No fuzzy matching: a
        near-miss that silently lands on a neighbouring table is worse than
        not finding it, because the answer that follows will be confident."""
        if not name:
            return None
        return (self.by_id.get(name)
                or self.by_qn.get(name)
                or self.by_qn_ci.get(name.upper()))

    def mentioned_assets(self, text: str) -> list[dict]:
        """Every catalogued asset whose qualified name appears in the text.
        Used for scope enforcement, not for routing."""
        up = text.upper()
        return [a for qn, a in self.by_qn_ci.items() if qn in up]

    def looks_like_qualified_name(self, text: str) -> list[str]:
        """DB.SCHEMA.OBJECT tokens in free text, whether or not the catalog
        holds them. The ones it does not hold are the interesting ones."""
        out = []
        for token in text.replace(",", " ").replace("?", " ").split():
            t = token.strip(".;:'\"()").upper()
            if t.count(".") == 2 and all(part for part in t.split(".")):
                out.append(t)
        return out

    def term(self, name: str):
        return self.terms_by_name.get(name.strip().strip('"').lower())

    def person(self, uid: str | None):
        return self.people.get(uid) if uid else None

    # -- graph walks ------------------------------------------------------

    def _reach(self, aid: str, edges) -> set[str]:
        seen, stack = set(), [aid]
        while stack:
            for nxt in edges[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def downstream(self, aid: str, transitive: bool = True) -> set[str]:
        return self._reach(aid, self._down) if transitive else set(self._down[aid])

    def upstream(self, aid: str, transitive: bool = True) -> set[str]:
        return self._reach(aid, self._up) if transitive else set(self._up[aid])

    def feeds_into(self, upstream_id: str, downstream_id: str) -> bool:
        return upstream_id in self.upstream(downstream_id)

    # -- operational ------------------------------------------------------

    def runs(self, aid: str) -> list[dict]:
        """Most recent first."""
        return self._runs[aid]

    def last_run(self, aid: str):
        rows = self._runs[aid]
        return rows[0] if rows else None

    def usage(self, aid: str):
        return self._usage.get(aid)

    def issues(self, aid: str) -> list[dict]:
        return self._issues[aid]

    def staleness_hours(self, asset: dict) -> int:
        return (self.as_of - _d(asset["last_updated_at"])).days * 24

    def sla_breached(self, asset: dict):
        """None when there is no SLA to breach — which is not the same as
        False, and the desks are required to say so."""
        sla = asset.get("freshness_sla_hours")
        if not sla:
            return None
        return self.staleness_hours(asset) > sla

    # -- policy -----------------------------------------------------------

    def restricted(self, asset: dict) -> bool:
        return asset["classification"] == "restricted"

    def policy_for(self, asset: dict):
        """The deny policy covering this asset, if any."""
        if self.restricted(asset):
            return self.policies["pol_001"]
        if "sensitive_personal" in asset.get("policy_tags", []):
            return self.policies["pol_003"]
        return None

    # -- citation scope ---------------------------------------------------

    def citable(self, aid: str | None) -> set[str]:
        """Everything an answer scoped to this asset is entitled to cite: the
        asset, its columns, its runs, its issues, its lineage neighbours and
        the terms its columns are linked to.

        The verifier checks citations against this set. A citation outside it
        means the answer reached for a record the question did not authorise,
        which is worth catching even when the cited record happens to say
        something true."""
        if not aid or aid not in self.by_id:
            return set()
        asset = self.by_id[aid]
        ids = {aid}
        for c in asset["columns"]:
            ids.add(c["column_id"])
            if c["term_id"]:
                ids.add(c["term_id"])
        ids |= {r["run_id"] for r in self._runs[aid]}
        ids |= {i["issue_id"] for i in self._issues[aid]}
        ids |= self.downstream(aid) | self.upstream(aid)
        ids |= set(self.policies)
        return ids

    def known_id(self, cid: str) -> bool:
        return (cid in self.by_id or cid in self.columns or cid in self.terms
                or cid in self.policies
                or any(cid == r["run_id"] for rows in self._runs.values()
                       for r in rows)
                or any(cid == i["issue_id"] for rows in self._issues.values()
                       for i in rows))
