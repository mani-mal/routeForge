"""P3a Task 3: proposal validation and application (spec §5)."""
from __future__ import annotations

import copy
import json

import pytest

from app.eval.proposal import ProposalError, apply_proposal, load_proposal
from tests.eval.sim_fixtures import app_entry, make_snapshot

P = "routeforge.policy-proposal/v1"


def test_empty_proposal_is_identity():
    snap = make_snapshot()
    assert apply_proposal(snap, {"schema": P}) == snap


def test_inputs_never_mutated():
    snap, prop = make_snapshot(), {"schema": P, "apps": {"app-a": {"residency": "onprem_only"}}}
    s0, p0 = copy.deepcopy(snap), copy.deepcopy(prop)
    apply_proposal(snap, prop)
    assert (snap, prop) == (s0, p0)


def test_app_edit_replaces_only_named_fields():
    snap = make_snapshot(apps=[app_entry("app-a", allowed_tiers=["cheap"])])
    out = apply_proposal(snap, {"schema": P, "apps": {"app-a": {
        "residency": "onprem_only", "allowed_regions": ["eu", "onprem"]}}})
    pol = out["apps"][0]["policy"]
    assert pol["residency"] == "onprem_only"
    assert pol["allowed_regions"] == ["eu", "onprem"]
    assert pol["allowed_tiers"] == ["cheap"]


def test_endpoint_ops_and_prices():
    out = apply_proposal(make_snapshot(), {"schema": P,
        "endpoints": {"add": [{"slug": "new-mid", "provider": "onprem", "tier": "mid",
                               "price": {"input": 0.3, "output": 0.6, "cached_input": None}}],
                      "remove": ["local-big"], "disable": ["local-cheap"]},
        "prices": {"local-mid": {"input": 9.0, "output": 9.0, "cached_input": None}}})
    by = {o["slug"]: o for o in out["onboarded"]}
    assert "local-big" not in by and by["new-mid"]["enabled"] is True
    assert by["local-cheap"]["enabled"] is False
    assert (by["local-mid"]["price_input"], by["local-mid"]["price_output"]) == (9.0, 9.0)
    assert [o["slug"] for o in out["onboarded"]] == sorted(by)


def test_remove_drops_quality_entries():
    snap = make_snapshot(quality={"customer": [], "calibrated": [
        {"model": "local-big", "workload": None, "score": 0.9, "set_id": "s"}]})
    out = apply_proposal(snap, {"schema": P, "endpoints": {"remove": ["local-big"]}})
    assert out["quality"]["calibrated"] == []


@pytest.mark.parametrize("prop,where", [
    ({"schema": "routeforge.policy-proposal/v2"}, "schema"),
    ({"schema": P, "bogus": 1}, "bogus"),
    ({"schema": P, "apps": {"nope": {"residency": "any"}}}, "apps.nope"),
    ({"schema": P, "apps": {"app-a": {"residencyy": "any"}}}, "apps.app-a.residencyy"),
    ({"schema": P, "apps": {"app-a": {"residency": "moon"}}}, "apps.app-a.residency"),
    ({"schema": P, "apps": {"app-a": {"allowed_tiers": ["strong"]}}}, "apps.app-a.allowed_tiers"),
    ({"schema": P, "providers": {"nosuch": {"enabled": True}}}, "providers.nosuch"),
    ({"schema": P, "endpoints": {"remove": ["ghost"]}}, "endpoints.remove.ghost"),
    ({"schema": P, "endpoints": {"remove": ["local-mid"], "disable": ["local-mid"]}},
     "endpoints.local-mid"),
    ({"schema": P, "endpoints": {"add": [{"slug": "local-mid", "provider": "onprem",
      "tier": "mid", "price": {"input": 1, "output": 1, "cached_input": None}}]}},
     "endpoints.add.local-mid"),
    ({"schema": P, "prices": {"local-mid": {"input": -1, "output": 1, "cached_input": None}}},
     "prices.local-mid"),
])
def test_errors_name_the_location(prop, where):
    with pytest.raises(ProposalError) as exc:
        apply_proposal(make_snapshot(), prop)
    assert str(exc.value).startswith(where)


def test_load_proposal_rejects_unknown_key(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"schema": P, "apss": {}}))
    with pytest.raises(ProposalError) as exc:
        load_proposal(str(p))
    assert str(exc.value).startswith("apss")
