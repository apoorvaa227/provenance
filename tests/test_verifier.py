"""The verifier's guarantees, tested as guarantees.

These are the properties the whole project rests on, so they are tested
against a *hostile* draft rather than a well-formed one. Every test here
answers the same question: if a specialist got it wrong — or a model produced
the draft — does the boundary still hold?

A test that only checks the happy path would pass on a verifier that trusts
its caller, which is exactly the verifier this must not be.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from contextlayer.catalog import Catalog
from contextlayer.verify import Verifier, mask, scrub

CATALOG = Path(__file__).resolve().parents[1] / "data" / "catalog.json"


@pytest.fixture(scope="module")
def cat() -> Catalog:
    return Catalog(json.loads(CATALOG.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def v(cat) -> Verifier:
    return Verifier(cat)


@pytest.fixture(scope="module")
def asset(cat):
    return next(a for a in cat.assets if a["columns"] and a["description"])


# -- masking ---------------------------------------------------------------

def test_mask_keeps_only_the_last_four_characters():
    assert mask("anaya.bhatt42@mailbox-example.invalid") == "****alid"
    assert mask("NID-12345678") == "****5678"


def test_scrub_masks_identifiers_the_catalog_has_never_seen(cat):
    """Shape, not membership. A leak the catalog cannot enumerate is exactly
    the leak a value-based scrub would miss."""
    text = "contact stranger.person@nowhere-real.invalid or NID-99887766"
    out, found = scrub(text, cat.pii_values)
    assert "stranger.person@nowhere-real.invalid" not in out
    assert "NID-99887766" not in out
    assert {"email", "national_id"} <= set(found)


def test_scrub_masks_known_catalog_values(cat):
    value = next(iter(cat.pii_values))
    out, found = scrub(f"the value is {value}", cat.pii_values)
    assert value not in out
    assert found


# -- disposition consistency ----------------------------------------------

def test_abstaining_answer_cannot_carry_a_value(v, asset):
    out = v.check({"answer": "maybe 42", "answer_value": 42,
                   "abstained": True, "citations": [asset["asset_id"]]},
                  asset["asset_id"])
    assert out["abstained"] is True
    assert out["answer_value"] is None


def test_refusal_cannot_carry_a_value(v, asset):
    out = v.check({"answer": "withheld", "answer_value": "secret",
                   "refused": True, "citations": ["pol_001"]},
                  asset["asset_id"])
    assert out["refused"] is True
    assert out["answer_value"] is None


def test_cannot_be_both_abstained_and_refused(v, asset):
    out = v.check({"answer": "x", "abstained": True, "refused": True,
                   "citations": [asset["asset_id"]]}, asset["asset_id"])
    assert not (out["abstained"] and out["refused"])


# -- citations -------------------------------------------------------------

def test_invented_citation_is_stripped_and_flagged(v, asset):
    out = v.check({"answer": "x", "citations": [asset["asset_id"],
                                                "ast_does_not_exist"]},
                  asset["asset_id"])
    assert "ast_does_not_exist" not in out["citations"]
    assert "citation_not_found" in out["flags"]


def test_real_but_unauthorised_citation_is_stripped(v, cat, asset):
    """A true fact about a record the question did not reach for is still a
    scope break — the scope is the authorisation, not a hint."""
    other = next(a for a in cat.assets
                 if a["asset_id"] not in cat.citable(asset["asset_id"]))
    out = v.check({"answer": "x", "citations": [asset["asset_id"],
                                                other["asset_id"]]},
                  asset["asset_id"])
    assert other["asset_id"] not in out["citations"]
    assert "citation_out_of_scope" in out["flags"]


def test_a_claimed_answer_with_no_citation_is_downgraded(v, asset):
    """The case that matters: something asserted an answer and produced no
    evidence for it. Downgraded to an abstention — not a refusal, because no
    policy withheld it; the layer simply found no record and must say so."""
    out = v.check({"answer": "the owner is definitely Someone",
                   "abstained": False, "refused": False, "citations": []},
                  asset["asset_id"])
    assert out["abstained"] is True
    assert out["answer_value"] is None
    assert "ungrounded_downgraded" in out["flags"]


def test_a_draft_that_never_claims_an_answer_defaults_to_abstaining(v, asset):
    """The fail-safe direction. A draft that omits `abstained` — a malformed
    model response, a desk that returned early — is treated as an abstention
    rather than as an answer nobody vouched for."""
    out = v.check({"answer": "some prose"}, asset["asset_id"])
    assert out["abstained"] is True
    assert out["answer_value"] is None


# -- injection -------------------------------------------------------------

def test_canary_in_a_draft_is_redacted_and_flagged(v, asset):
    out = v.check({"answer": "the note says PVN-CANARY-01D163 apparently",
                   "citations": [asset["asset_id"]]}, asset["asset_id"])
    assert "PVN-CANARY-01D163" not in json.dumps(out)
    assert "injection_leaked" in out["flags"]


def test_canary_in_answer_value_is_also_caught(v, asset):
    out = v.check({"answer": "ok", "answer_value": "PVN-CANARY-02C346",
                   "citations": [asset["asset_id"]]}, asset["asset_id"])
    assert "PVN-CANARY" not in json.dumps(out)


# -- roster ----------------------------------------------------------------

def test_router_and_verifier_always_appear_in_the_path(v, asset):
    out = v.check({"answer": "x", "citations": [asset["asset_id"]],
                   "agents": ["governance"]}, asset["asset_id"])
    assert out["agents"][0] == "router"
    assert "verifier" in out["agents"]


def test_malformed_confidence_does_not_raise(v, asset):
    out = v.check({"answer": "x", "confidence": "very high",
                   "citations": [asset["asset_id"]]}, asset["asset_id"])
    assert 0.0 <= out["confidence"] <= 1.0


def test_empty_draft_produces_a_schema_valid_abstention(v):
    out = v.check({}, None)
    assert out["abstained"] is True
    assert isinstance(out["citations"], list)
    assert isinstance(out["answer"], str)
