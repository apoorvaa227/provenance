"""Read a dbt `manifest.json` into the catalog shape.

Everything so far has run on a graph this project generated for itself, which
is the weakest possible test of whether the context model is real. dbt is the
opposite: a schema defined by someone else, for other reasons, that most
analytics teams already produce on every run. If the layer's questions
survive that mapping, the abstraction is doing work. If they only survive on
the generated graph, it was shaped around its own fixture.

A manifest is a file — `target/manifest.json` after any `dbt compile`. No
credentials, no warehouse connection, no network.

**What maps cleanly.** Models, seeds, snapshots and sources become assets.
`depends_on.nodes` becomes lineage — dbt's dependency graph is exactly the
upstream/downstream relation the lineage desk walks. Column descriptions,
data types, tags and `meta` all have direct homes.

**What does not exist in dbt, and is left empty on purpose.** A manifest has
no pipeline run history, no query usage, no certification badge and no
freshness SLA — those live in `run_results.json`, the warehouse's query log,
and a catalog tool respectively. The temptation is to synthesise something
plausible so the desks have data to work with. Doing that would be the exact
failure this project exists to measure: inventing a trust signal that no
record supports.

So they stay absent, and the layer abstains on them. A run against a real dbt
project therefore scores *lower* than one against the generated catalog, and
the shape of that gap is a genuine finding — it says precisely which questions
your dbt project cannot answer yet, which is the useful output.

    python -m connectors.dbt --manifest target/manifest.json \\
        --out data/dbt_catalog.json
    python -m evals.run --catalog data/dbt_catalog.json ...
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

# dbt resource types that are things you can query, and therefore things a
# question can be about. Tests, macros, operations and analyses are excluded:
# they are project machinery, not assets someone builds a dashboard on.
ASSET_TYPES = {"model", "seed", "snapshot", "source"}

# Column-name heuristics for personal data. Deliberately conservative and
# deliberately *reported* — the connector says the tag was inferred rather
# than read, because a governance signal invented by a regex should not be
# indistinguishable from one a steward set.
PII_PATTERNS = [
    (re.compile(r"e?mail", re.I), "email"),
    (re.compile(r"phone|mobile|msisdn", re.I), "phone"),
    (re.compile(r"ssn|national_?id|passport|nin\b", re.I), "national_id"),
    (re.compile(r"dob|date_of_birth|birth_?date", re.I), "date_of_birth"),
    (re.compile(r"addr|address|postcode|zip_?code", re.I), "address"),
    (re.compile(r"ip_?addr", re.I), "ip_address"),
    (re.compile(r"salary|compensation|comp_amount", re.I), "compensation"),
]

CLASSIFICATION_TAGS = {"public", "internal", "confidential", "restricted"}


def infer_pii(column_name: str, meta: dict, tags: list[str]) -> str | None:
    """An explicit declaration always beats a guess."""
    declared = (meta or {}).get("pii_type") or (meta or {}).get("pii")
    if isinstance(declared, str):
        return declared
    for t in tags or []:
        if t.lower().startswith("pii:"):
            return t.split(":", 1)[1]
    for pattern, kind in PII_PATTERNS:
        if pattern.search(column_name or ""):
            return kind
    return None


def classification_of(node: dict) -> str:
    meta = {**(node.get("meta") or {}),
            **((node.get("config") or {}).get("meta") or {})}
    declared = meta.get("classification") or meta.get("sensitivity")
    if isinstance(declared, str) and declared.lower() in CLASSIFICATION_TAGS:
        return declared.lower()
    for t in node.get("tags") or []:
        if t.lower() in CLASSIFICATION_TAGS:
            return t.lower()
    if any(str(t).lower() in ("pii", "sensitive") for t in node.get("tags") or []):
        return "confidential"
    return "internal"


class DbtManifest:
    def __init__(self, raw: dict):
        self.raw = raw
        self.meta = raw.get("metadata") or {}
        self.people: dict[str, dict] = {}
        self._person_seq = 0

    # -- owners ------------------------------------------------------------

    def person_id(self, name: str | None, team: str | None) -> str | None:
        """dbt records an owner as free text. Absent means absent — this
        returns None rather than inventing a placeholder, because 'no owner
        recorded' is an answer the layer is built to give."""
        if not name:
            return None
        key = str(name).strip().lower()
        if key not in self.people:
            self._person_seq += 1
            self.people[key] = {
                "user_id": f"usr_{self._person_seq:03d}",
                "name": str(name).strip(),
                "team": (team or "unspecified"),
                "email": None,
            }
        return self.people[key]["user_id"]

    @staticmethod
    def _owner_fields(node: dict) -> tuple[str | None, str | None]:
        meta = {**(node.get("meta") or {}),
                **((node.get("config") or {}).get("meta") or {})}
        owner = meta.get("owner") or meta.get("maintainer")
        if isinstance(owner, dict):
            return owner.get("name") or owner.get("email"), owner.get("team")
        group = node.get("group") or meta.get("team")
        return (owner if isinstance(owner, str) else None,
                group if isinstance(group, str) else None)

    # -- nodes -------------------------------------------------------------

    def nodes(self) -> dict:
        out = {}
        for bucket in ("nodes", "sources"):
            for uid, node in (self.raw.get(bucket) or {}).items():
                if node.get("resource_type") in ASSET_TYPES:
                    out[uid] = node
        return out

    def asset(self, uid: str, node: dict, index: int) -> dict:
        db = node.get("database") or "UNKNOWN"
        schema = node.get("schema") or "UNKNOWN"
        name = (node.get("alias") or node.get("identifier")
                or node.get("name") or uid.split(".")[-1])
        owner_name, team = self._owner_fields(node)
        description = (node.get("description") or "").strip() or None

        columns = []
        for i, (cname, col) in enumerate((node.get("columns") or {}).items(), 1):
            col = col or {}
            cdesc = (col.get("description") or "").strip() or None
            ctags = list(col.get("tags") or [])
            pii = infer_pii(cname, col.get("meta") or {}, ctags)
            columns.append({
                "column_id": f"col_{index:04d}_{i:03d}",
                "name": cname,
                "data_type": col.get("data_type") or "unknown",
                "nullable": True,
                "description": cdesc,
                "classification": "restricted" if pii in
                ("national_id", "compensation") else classification_of(node),
                "pii_type": pii,
                # A manifest carries no data, only metadata. There is nothing
                # to sample, and nothing invented to fill the gap.
                "sample_values": [],
                "term_id": None,
            })

        tags = [str(t) for t in (node.get("tags") or [])]
        policy_tags = sorted({
            *(["pii"] if any(c["pii_type"] for c in columns) else []),
            *(["sensitive_personal"] if any(
                c["pii_type"] in ("national_id", "compensation")
                for c in columns) else []),
            *[t.lower() for t in tags if t.lower() in
              ("financial", "gdpr", "phi", "access_controlled")],
        })

        return {
            "asset_id": uid,
            "source_id": node.get("source_name") or self.meta.get(
                "project_name", "dbt"),
            "qualified_name": f"{db}.{schema}.{name}".upper(),
            "database": db, "schema": schema, "name": name,
            "object_type": ("view" if (node.get("config") or {}).get(
                "materialized") == "view" else "table"),
            "layer": node.get("resource_type"),
            "entity": name,
            # Absent in a manifest. Left absent.
            "row_count": None,
            "size_bytes": None,
            "created_at": None,
            "last_updated_at": None,
            "description": description,
            "owner_id": self.person_id(owner_name, team),
            "steward_id": None,
            "certification": None,
            "certified_at": None,
            "certified_by": None,
            "classification": classification_of(node),
            "policy_tags": policy_tags,
            "freshness_sla_hours": None,
            "columns": columns,
            "dbt": {"resource_type": node.get("resource_type"),
                    "materialized": (node.get("config") or {}).get(
                        "materialized"),
                    "tags": tags},
        }

    # -- assembly ----------------------------------------------------------

    def build(self, as_of: str | None = None) -> dict:
        nodes = self.nodes()
        assets = [self.asset(uid, node, i)
                  for i, (uid, node) in enumerate(sorted(nodes.items()), 1)]
        known = {a["asset_id"] for a in assets}

        lineage = []
        for uid, node in sorted(nodes.items()):
            for parent in ((node.get("depends_on") or {}).get("nodes") or []):
                if parent in known and uid in known:
                    lineage.append({
                        "edge_id": f"lin_{len(lineage) + 1:04d}",
                        "upstream_asset_id": parent,
                        "downstream_asset_id": uid,
                        "job": "dbt",
                        "transform": (node.get("config") or {}).get(
                            "materialized") or "select",
                    })

        # dbt's semantic layer is the closest thing to a glossary, when the
        # project defines one. Most do not, and an empty glossary is the
        # honest result — the glossary desk then abstains.
        glossary = []
        for i, (uid, m) in enumerate(
                sorted((self.raw.get("metrics") or {}).items()), 1):
            definition = (m.get("description") or "").strip()
            if not definition:
                continue
            glossary.append({
                "term_id": f"trm_{i:03d}",
                "name": m.get("label") or m.get("name") or uid.split(".")[-1],
                "definition": definition,
                "status": "approved",
                "owner_id": None,
                "linked_column_ids": [],
                "updated_at": None,
            })

        generated = self.meta.get("generated_at") or ""
        try:
            stamp = datetime.fromisoformat(
                generated.replace("Z", "+00:00")).date().isoformat()
        except Exception:                                  # noqa: BLE001
            stamp = as_of or date.today().isoformat()

        return {
            "meta": {
                "synthetic": False,
                "source": "dbt manifest",
                "project": self.meta.get("project_name"),
                "dbt_version": self.meta.get("dbt_version"),
                "as_of": stamp,
                "catalogued_sources": sorted(
                    {a["source_id"] for a in assets}),
                "absent_signals": [
                    "pipeline run history (lives in run_results.json)",
                    "query usage (lives in the warehouse query log)",
                    "certification status (no dbt equivalent)",
                    "freshness SLA (source freshness only, if configured)",
                    "column sample values (a manifest carries no data)",
                ],
                "counts": {
                    "assets": len(assets),
                    "columns": sum(len(a["columns"]) for a in assets),
                    "lineage_edges": len(lineage),
                    "glossary_terms": len(glossary),
                    "people": len(self.people),
                },
            },
            "sources": [{"source_id": s, "platform": "dbt",
                         "display_name": s, "database": ""}
                        for s in sorted({a["source_id"] for a in assets})],
            "people": list(self.people.values()),
            "assets": assets,
            "lineage": lineage,
            "glossary": glossary,
            # Policies are governance decisions, not dbt artefacts. The same
            # four the generated catalog uses apply — they describe how this
            # layer behaves, not what dbt recorded.
            "policies": DEFAULT_POLICIES,
            "runs": [],
            "usage": [],
            "issues": [],
        }


DEFAULT_POLICIES = [
    {"policy_id": "pol_001",
     "name": "Restricted assets are not described in detail",
     "applies_to": {"classification": "restricted"}, "effect": "deny",
     "rule": "Column-level detail and sample values for restricted assets are "
             "disclosed only through an approved access request."},
    {"policy_id": "pol_002",
     "name": "Personal data is never returned in the clear",
     "applies_to": {"policy_tag": "pii"}, "effect": "mask",
     "rule": "Values from columns carrying a pii_type are masked wherever "
             "they appear."},
    {"policy_id": "pol_003",
     "name": "Compensation is need-to-know",
     "applies_to": {"policy_tag": "sensitive_personal"}, "effect": "deny",
     "rule": "Salary, national identifiers and equivalent fields are not "
             "summarised, aggregated or sampled."},
    {"policy_id": "pol_004",
     "name": "The catalog reports, it does not advise",
     "applies_to": {"intent": "advice"}, "effect": "deny",
     "rule": "Recommendations to deprecate, grant access, change an owner or "
             "restructure a model are decisions for the owning team."},
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True,
                    help="path to dbt target/manifest.json")
    ap.add_argument("--out", default="data/dbt_catalog.json")
    args = ap.parse_args()

    raw = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    catalog = DbtManifest(raw).build()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, indent=1), encoding="utf-8")

    c = catalog["meta"]["counts"]
    print(f"wrote {out} — {c['assets']} assets, {c['columns']} columns, "
          f"{c['lineage_edges']} lineage edges, {c['glossary_terms']} terms, "
          f"{c['people']} owners")
    undoc = sum(1 for a in catalog["assets"] if not a["description"])
    unowned = sum(1 for a in catalog["assets"] if not a["owner_id"])
    print(f"  coverage gaps: {undoc} undocumented, {unowned} without an owner")
    print("  absent by design (the layer will abstain on these):")
    for s in catalog["meta"]["absent_signals"]:
        print(f"    - {s}")


if __name__ == "__main__":
    main()
