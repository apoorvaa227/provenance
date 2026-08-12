"""The context layer as an MCP server.

The eval in `evals/` proves the layer works. This makes it *usable*: any MCP
client — Claude Desktop, Claude Code, an agent you wrote — can ask the catalog
questions and get answers that carry their provenance.

The design point is what an external agent **cannot** get out of these tools.
Every response is assembled by the same desks and passed through the same
verifier as the scored path, so:

  * a figure that no record supports comes back as an abstention, not a guess;
  * a citation outside what the question authorised is stripped and flagged;
  * personal data is masked by value and by shape, whatever produced it;
  * text from an issue thread never reaches the caller, so an injection sitting
    in the catalog cannot reach the client's model through this door.

That last one is the interesting one. An MCP server is an *untrusted input
channel* to whatever agent connects to it — a hostile string in a table
description is a prompt injection with a delivery mechanism. The tools here
report that such text exists, and refuse to repeat it.

Tools are deliberately narrow and named for the question they answer rather
than the table they read. An agent choosing between `trust_signals` and
`lineage` is choosing between two questions; one choosing between `get_rows`
and `get_meta` is choosing between two implementations.

    pip install -r requirements.txt
    python -m mcp_server.server            # stdio, for an MCP client to spawn

Register with Claude Desktop by adding to `claude_desktop_config.json`:

    {
      "mcpServers": {
        "provenance": {
          "command": "python",
          "args": ["-m", "mcp_server.server"],
          "cwd": "/absolute/path/to/provenance",
          "env": {"CATALOG_PATH": "data/catalog.json"}
        }
      }
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from contextlayer.agents import Ecosystem
from contextlayer.catalog import Catalog
from contextlayer.verify import Verifier, blank

CATALOG_PATH = os.environ.get("CATALOG_PATH", "data/catalog.json")

_catalog = Catalog(json.loads(Path(CATALOG_PATH).read_text(encoding="utf-8")))
_eco = Ecosystem(_catalog)
_verifier = Verifier(_catalog)

mcp = FastMCP("provenance")


def _verified(draft: dict, scope: str | None) -> dict:
    """Every tool returns through here. A tool that assembles its own response
    still cannot ship an unmasked identifier or an unsupported citation."""
    out = _verifier.check(draft, scope)
    out.pop("question_id", None)
    return out


def _not_in_catalog(name: str) -> dict:
    return {
        "found": False,
        "answer": f"'{name}' is not in the catalog. It has no owner, "
                  f"description, certification, lineage or row count here.",
        "note": "This is a real answer, not a lookup failure. Do not substitute "
                "knowledge about a similarly-named table from elsewhere.",
    }


@mcp.tool()
def ask(question: str, asset: str | None = None) -> str:
    """Ask the catalog a question in plain language and get an answer with its
    citations.

    Routes through the full context layer: the question is classified, policy
    and scope are checked, a specialist desk answers from records, and the
    verifier checks the result before it is returned.

    The response always carries a disposition. `answered` means the catalog
    holds it. `abstained` means it does not — treat that as the answer, not as
    a failed lookup to work around. `refused` means policy withholds it, and
    names the policy.

    Args:
        question: e.g. "Is PROD.SALES.FCT_ORDERS safe to build a dashboard on?"
        asset: optional qualified name or asset id to scope the question to.
               Supplying it enables the scope check; questions about a
               different asset will then be refused.
    """
    scope = None
    if asset:
        found = _catalog.resolve(asset)
        if found is None:
            return json.dumps(_not_in_catalog(asset), indent=1)
        scope = found["asset_id"]
    out = _eco.answer({"question_id": "mcp", "prompt": question,
                       "asset_id": scope})
    out.pop("question_id", None)
    return json.dumps(out, indent=1)


@mcp.tool()
def resolve_asset(name: str) -> str:
    """Look up one asset and return what the catalog actually records about it.

    Returns `found: false` for a name the catalog does not hold — including
    names that look entirely plausible. That is the intended answer for an
    uncatalogued asset, and it is the case worth handling carefully: a
    confident description of a table that does not exist is the failure this
    whole layer is built to prevent.

    Args:
        name: qualified name (DB.SCHEMA.OBJECT) or asset id.
    """
    asset = _catalog.resolve(name)
    if asset is None:
        return json.dumps(_not_in_catalog(name), indent=1)

    owner = _catalog.person(asset["owner_id"])
    steward = _catalog.person(asset["steward_id"])
    out = {
        "found": True,
        "asset_id": asset["asset_id"],
        "qualified_name": asset["qualified_name"],
        "object_type": asset["object_type"],
        "description": asset["description"],
        "documented": asset["description"] is not None,
        "owner": f"{owner['name']} ({owner['team']})" if owner else None,
        "steward": f"{steward['name']} ({steward['team']})" if steward else None,
        "certification": asset["certification"],
        "classification": asset["classification"],
        "policy_tags": asset["policy_tags"],
        "row_count": asset["row_count"],
        "last_updated_at": asset["last_updated_at"],
        "column_count": len(asset["columns"]),
        # Column *names* and types are structure. Sample values are data, and
        # they do not leave through this tool at any classification.
        "columns": [{"name": c["name"], "data_type": c["data_type"],
                     "documented": c["description"] is not None,
                     "pii_type": c["pii_type"]}
                    for c in asset["columns"]],
    }
    if _catalog.restricted(asset):
        policy = _catalog.policy_for(asset)
        out["restricted"] = True
        out["policy"] = policy["policy_id"] if policy else None
        out["note"] = ("Classified restricted. Existence and ownership are "
                       "confirmed; column-level detail is released through an "
                       "approved access request.")
        out.pop("columns", None)
        out.pop("row_count", None)
    return json.dumps(out, indent=1)


@mcp.tool()
def trust_signals(name: str) -> str:
    """Should you rely on this asset? Returns the certification badge *and* the
    operational record, and says explicitly when they disagree.

    This is the tool to call before building on an asset. A `verified` badge on
    something whose last two pipeline runs failed is the common case worth
    catching, and the disagreement is reported rather than resolved — deciding
    between the two records is the owning team's call, not this layer's.

    Args:
        name: qualified name or asset id.
    """
    asset = _catalog.resolve(name)
    if asset is None:
        return json.dumps(_not_in_catalog(name), indent=1)

    aid = asset["asset_id"]
    runs = _catalog.runs(aid)[:5]
    usage = _catalog.usage(aid)
    breached = _catalog.sla_breached(asset)
    last = runs[0] if runs else None

    conflicts = []
    if asset["certification"] == "verified" and (
            breached or (last and last["status"] == "failed")):
        conflicts.append(
            "Certified 'verified', but the operational record disagrees: "
            + ("the most recent run failed. " if last and
               last["status"] == "failed" else "")
            + ("freshness SLA is breached." if breached else ""))
    if asset["certification"] == "deprecated" and usage and \
            usage["queries_30d"] > 500:
        conflicts.append(
            f"Certified 'deprecated', but still queried "
            f"{usage['queries_30d']} times in the last 30 days by "
            f"{usage['distinct_users_30d']} users. The badge says stop; the "
            f"usage says nobody has.")

    return json.dumps({
        "found": True,
        "qualified_name": asset["qualified_name"],
        "certification": asset["certification"],
        "certified_at": asset["certified_at"],
        "last_updated_at": asset["last_updated_at"],
        "freshness_sla_hours": asset["freshness_sla_hours"],
        "sla_breached": breached,
        "hours_since_update": _catalog.staleness_hours(asset),
        "recent_runs": [{"run_id": r["run_id"], "started_at": r["started_at"],
                         "status": r["status"], "message": r["message"]}
                        for r in runs],
        "usage_30d": usage and {
            "queries": usage["queries_30d"],
            "distinct_users": usage["distinct_users_30d"],
            "last_queried_at": usage["last_queried_at"]},
        "conflicts": conflicts,
        "verdict": "conflicting signals — read both" if conflicts
                   else "signals agree",
    }, indent=1)


@mcp.tool()
def lineage(name: str, direction: str = "downstream") -> str:
    """What breaks if this changes, or what feeds it.

    Returns the transitive closure, not just immediate neighbours — the
    question "how many assets does this affect" is almost never about one hop.

    Args:
        name: qualified name or asset id.
        direction: "downstream" (what reads from it) or "upstream" (what feeds
                   it). "both" returns each separately.
    """
    asset = _catalog.resolve(name)
    if asset is None:
        return json.dumps(_not_in_catalog(name), indent=1)
    aid = asset["asset_id"]

    def side(fn):
        transitive = fn(aid)
        direct = fn(aid, transitive=False)
        return {
            "transitive_count": len(transitive),
            "direct_count": len(direct),
            "direct": sorted(_catalog.by_id[i]["qualified_name"] for i in direct),
            "transitive": sorted(_catalog.by_id[i]["qualified_name"]
                                 for i in transitive),
        }

    out = {"found": True, "qualified_name": asset["qualified_name"]}
    if direction in ("downstream", "both"):
        out["downstream"] = side(_catalog.downstream)
    if direction in ("upstream", "both"):
        out["upstream"] = side(_catalog.upstream)
    if direction not in ("downstream", "upstream", "both"):
        return json.dumps({"error": "direction must be downstream, upstream "
                                    "or both"}, indent=1)
    return json.dumps(out, indent=1)


@mcp.tool()
def glossary(term: str) -> str:
    """The agreed definition of a business term — and whether the columns it is
    attached to actually agree with it.

    A term whose definition contradicts the description on its own linked
    column is the most expensive kind of metadata bug, because both sides look
    authoritative in isolation. Both are returned.

    Args:
        term: e.g. "Net Revenue", "Active Customer", "MRR".
    """
    found = _catalog.term(term)
    if found is None:
        available = sorted(t["name"] for t in _catalog.terms.values())
        return json.dumps({
            "found": False,
            "answer": f"No business term named '{term}' is defined in this "
                      f"catalog.",
            "defined_terms": available,
        }, indent=1)

    draft = _eco.glossary_desk(f'What does the business mean by "{found["name"]}"?')
    verified = _verified(draft, None)
    return json.dumps({
        "found": True,
        "term": found["name"],
        "definition": found["definition"],
        "status": found["status"],
        "owner": (_catalog.person(found["owner_id"]) or {}).get("name"),
        "linked_columns": len(found["linked_column_ids"]),
        "conflict": "conflict" in verified["flags"],
        "answer": verified["answer"],
        "citations": verified["citations"],
    }, indent=1)


@mcp.tool()
def search_assets(query: str, limit: int = 20) -> str:
    """Find catalogued assets by name fragment, schema, or entity.

    Returns only what the catalog holds — there is no fuzzy expansion onto
    names it does not have.

    Args:
        query: a substring, e.g. "ORDERS", "FINANCE", "FCT_".
        limit: maximum results (default 20).
    """
    q = query.strip().upper()
    hits = [a for a in _catalog.assets if q in a["qualified_name"].upper()]
    return json.dumps({
        "query": query,
        "match_count": len(hits),
        "returned": min(len(hits), limit),
        "assets": [{
            "qualified_name": a["qualified_name"],
            "certification": a["certification"],
            "classification": a["classification"],
            "documented": a["description"] is not None,
            "owned": a["owner_id"] is not None,
        } for a in hits[:limit]],
    }, indent=1)


@mcp.tool()
def catalog_summary() -> str:
    """What this catalog covers, and where it is thin.

    Reporting the gaps is the point: an agent that knows 10 of 95 assets are
    undocumented can caveat accordingly, where one that only sees the 85 will
    not know to.
    """
    assets = _catalog.assets
    undocumented = [a for a in assets if not a["description"]]
    unowned = [a for a in assets if not a["owner_id"]]
    restricted = [a for a in assets if _catalog.restricted(a)]
    stale = [a for a in assets if _catalog.sla_breached(a)]
    return json.dumps({
        "sources": [s["display_name"] for s in _catalog.raw["sources"]],
        "assets": len(assets),
        "columns": sum(len(a["columns"]) for a in assets),
        "lineage_edges": len(_catalog.raw["lineage"]),
        "glossary_terms": len(_catalog.terms),
        "as_of": _catalog.as_of.isoformat(),
        "coverage_gaps": {
            "undocumented_assets": len(undocumented),
            "assets_without_an_owner": len(unowned),
            "assets_past_their_freshness_sla": len(stale),
            "restricted_assets": len(restricted),
        },
        "policies": [{"policy_id": p["policy_id"], "name": p["name"],
                      "effect": p["effect"]}
                     for p in _catalog.policies.values()],
        "note": "All records are synthetic, generated by gen/catalog.py.",
    }, indent=1)


if __name__ == "__main__":
    mcp.run()
