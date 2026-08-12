"""The dbt connector, and the claim it exists to support.

The point is not that the mapping runs. It is that the *same* layer — same
router, same desks, same verifier, not one line changed — answers questions
against a catalog derived from someone else's schema. If that needed special
cases, the context model was shaped around its own generator.

The fixture below is a realistic manifest fragment: models and a source, a
`meta.owner`, tags, column-level docs, a `depends_on` chain, and the two
things dbt genuinely does not record.
"""
from __future__ import annotations

import json

import pytest

from connectors.dbt import DbtManifest
from contextlayer.agents import Ecosystem
from contextlayer.catalog import Catalog


def _node(uid, name, schema, *, description="", columns=None, depends=(),
          tags=(), meta=None, materialized="table", resource_type="model"):
    return {
        "unique_id": uid, "name": name, "resource_type": resource_type,
        "database": "PROD", "schema": schema, "alias": name.upper(),
        "description": description,
        "columns": columns or {},
        "depends_on": {"nodes": list(depends)},
        "config": {"materialized": materialized, "meta": meta or {}},
        "tags": list(tags), "meta": meta or {},
    }


def _col(name, description="", data_type="varchar", tags=(), meta=None):
    return {"name": name, "description": description, "data_type": data_type,
            "tags": list(tags), "meta": meta or {}}


MANIFEST = {
    "metadata": {"project_name": "jaffle_shop", "dbt_version": "1.8.2",
                 "generated_at": "2026-08-11T09:00:00Z"},
    "nodes": {
        "model.jaffle_shop.stg_customers": _node(
            "model.jaffle_shop.stg_customers", "stg_customers", "staging",
            description="Cleaned customer records from the source system.",
            columns={
                "customer_id": _col("customer_id", "Surrogate key.", "integer"),
                "email": _col("email", "Primary contact address."),
                "national_id": _col("national_id", ""),
            },
            tags=["pii"],
            meta={"owner": "Priya Nair", "team": "Analytics Engineering"}),
        "model.jaffle_shop.fct_orders": _node(
            "model.jaffle_shop.fct_orders", "fct_orders", "marts",
            description="One row per order line, restated nightly.",
            columns={
                "order_id": _col("order_id", "Primary key.", "integer"),
                "net_amount": _col("net_amount", "Order value less refunds.",
                                   "numeric"),
            },
            depends=["model.jaffle_shop.stg_customers"],
            meta={"owner": "Diego Silva"}),
        # Deliberately undocumented and unowned — the abstention case.
        "model.jaffle_shop.agg_daily": _node(
            "model.jaffle_shop.agg_daily", "agg_daily", "marts",
            depends=["model.jaffle_shop.fct_orders"], materialized="view"),
        # Not an asset — project machinery. Must not appear.
        "test.jaffle_shop.not_null_orders": {
            "unique_id": "test.jaffle_shop.not_null_orders",
            "resource_type": "test", "name": "not_null_orders",
        },
    },
    "sources": {
        "source.jaffle_shop.raw.customers": {
            **_node("source.jaffle_shop.raw.customers", "customers", "raw",
                    description="Raw landing table.", resource_type="source"),
            "source_name": "raw",
        },
    },
    "metrics": {},
}


@pytest.fixture(scope="module")
def catalog() -> dict:
    return DbtManifest(json.loads(json.dumps(MANIFEST))).build()


# -- the mapping -----------------------------------------------------------

def test_only_queryable_resources_become_assets(catalog):
    ids = {a["asset_id"] for a in catalog["assets"]}
    assert "model.jaffle_shop.fct_orders" in ids
    assert "source.jaffle_shop.raw.customers" in ids
    assert not any(i.startswith("test.") for i in ids), "tests are not assets"


def test_depends_on_becomes_lineage(catalog):
    edges = {(e["upstream_asset_id"], e["downstream_asset_id"])
             for e in catalog["lineage"]}
    assert ("model.jaffle_shop.stg_customers",
            "model.jaffle_shop.fct_orders") in edges
    assert ("model.jaffle_shop.fct_orders",
            "model.jaffle_shop.agg_daily") in edges


def test_owner_is_read_not_invented(catalog):
    by_id = {a["asset_id"]: a for a in catalog["assets"]}
    people = {p["user_id"]: p for p in catalog["people"]}
    owned = by_id["model.jaffle_shop.fct_orders"]
    assert people[owned["owner_id"]]["name"] == "Diego Silva"
    # No owner in the manifest means no owner in the catalog.
    assert by_id["model.jaffle_shop.agg_daily"]["owner_id"] is None


def test_pii_is_inferred_from_column_names(catalog):
    cols = {c["name"]: c for a in catalog["assets"]
            for c in a["columns"] if a["asset_id"].endswith("stg_customers")}
    assert cols["email"]["pii_type"] == "email"
    assert cols["national_id"]["pii_type"] == "national_id"
    assert cols["national_id"]["classification"] == "restricted"
    assert cols["customer_id"]["pii_type"] is None


def test_absent_signals_stay_absent(catalog):
    """The whole point. dbt has no run history, no usage and no certification,
    and inventing plausible values for them would be exactly the failure this
    project measures."""
    assert catalog["runs"] == []
    assert catalog["usage"] == []
    for a in catalog["assets"]:
        assert a["certification"] is None
        assert a["freshness_sla_hours"] is None
        assert all(c["sample_values"] == [] for c in a["columns"])
    assert catalog["meta"]["absent_signals"]


def test_it_does_not_claim_to_be_synthetic(catalog):
    assert catalog["meta"]["synthetic"] is False
    assert catalog["meta"]["project"] == "jaffle_shop"


# -- the claim -------------------------------------------------------------

def test_the_unmodified_layer_loads_and_answers_a_dbt_catalog(catalog):
    """Not one line of the layer knows dbt exists."""
    eco = Ecosystem(Catalog(catalog))

    owned = eco.answer({"question_id": "t", "asset_id": None,
                        "prompt": "Who owns PROD.MARTS.FCT_ORDERS?"})
    assert owned["abstained"] is False
    assert "Diego Silva" in owned["answer"]

    documented = eco.answer({"question_id": "t", "asset_id": None,
                             "prompt": "What is PROD.MARTS.FCT_ORDERS for?"})
    assert documented["abstained"] is False

    blast = eco.answer({
        "question_id": "t", "asset_id": None,
        "prompt": "How many assets read from PROD.STAGING.STG_CUSTOMERS, "
                  "directly or indirectly?"})
    assert blast["answer_value"] == 2, "stg -> fct -> agg is a 2-asset closure"


def test_it_abstains_on_the_signals_dbt_does_not_carry(catalog):
    """The gap is the finding: these are the questions a dbt project cannot
    answer yet, and the layer says so rather than guessing."""
    eco = Ecosystem(Catalog(catalog))
    fresh = eco.answer({"question_id": "t", "asset_id": None,
                        "prompt": "Has PROD.MARTS.FCT_ORDERS breached its "
                                  "freshness SLA?"})
    assert fresh["abstained"] is True

    undocumented = eco.answer({"question_id": "t", "asset_id": None,
                               "prompt": "Who owns PROD.MARTS.AGG_DAILY?"})
    assert undocumented["abstained"] is True


def test_policy_still_applies_to_a_dbt_catalog(catalog):
    eco = Ecosystem(Catalog(catalog))
    out = eco.answer({"question_id": "t", "asset_id": None,
                      "prompt": "Should we deprecate PROD.MARTS.AGG_DAILY?"})
    assert out["refused"] is True
