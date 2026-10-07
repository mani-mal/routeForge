"""P3c-4: the proposal keys a capacity question adds -- `capacity` and
`workload.volume_scale` (spec §3.2, §4; invariants C1, C15).

Both are assumptions read only by `app/eval/capacity.py`. Neither may reach
the applied snapshot document: that is what keeps every default output
byte-identical.
"""
from __future__ import annotations

import pytest

from app.eval.proposal import (CAPACITY_LIMITS, MAX_VOLUME_SCALE, ProposalError, _validate_keys,
                               apply_proposal, assumptions_block, normalized_edits,
                               validate_capacity_limits, volume_scale_block)
from app.eval.scenarios import SCHEMA as SCENARIOS_SCHEMA, ScenarioError, validate_scenarios
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_snapshot

P = "routeforge.policy-proposal/v1"


def _apply(body: dict, snapshot: dict | None = None) -> dict:
    return apply_proposal(snapshot or make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")]),
                          {"schema": P, **body}, base=BASE_SETTINGS)


def _error(body: dict) -> ProposalError:
    with pytest.raises(ProposalError) as err:
        _apply(body)
    return err.value


def test_the_limits_are_a_closed_ordered_set():
    assert CAPACITY_LIMITS == ("max_concurrency", "requests_per_minute", "tokens_per_minute",
                               "output_tokens_per_minute")
    assert MAX_VOLUME_SCALE == 1_000_000


def test_c1_capacity_and_volume_never_reach_the_applied_document():                    # C1
    """The applied snapshot is what both arms are built from. If a capacity
    or a volume scale reached it, the simulator's default output could move."""
    snapshot = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")])
    body = {"capacity": {"local-mid": {"max_concurrency": 8, "tokens_per_minute": 400000}},
            "workload": {"volume_scale": 3, "apps": {"app-b": {"volume_scale": 0.5}}}}
    assert _apply(body, snapshot) == snapshot
    assert assumptions_block({"schema": P, **body}) is None
    # ... and beside real assumptions the block is exactly what it was.
    mixed = {"schema": P, **body}
    mixed["workload"] = {**body["workload"], "token_scale": 0.5}
    assert assumptions_block(mixed) == {"token_scale": {"*": 0.5}, "cached_share": {},
                                        "latency_ms": {}}
    assert _apply({k: v for k, v in mixed.items() if k != "schema"}, snapshot)["assumptions"] == \
        assumptions_block(mixed)
    # The record of what was asked for does carry them.
    assert normalized_edits({"schema": P, **body}) == body


def test_volume_scale_block_resolves_global_and_per_app():
    assert volume_scale_block(None) == {} and volume_scale_block({"token_scale": 2}) == {}
    assert volume_scale_block({"volume_scale": 3, "apps": {"b": {"volume_scale": 0.5},
                                                           "a": {"token_scale": 2}}}) == {
        "*": 3.0, "b": 0.5}


@pytest.mark.parametrize("value", [0, -1, True, "2", float("nan"), float("inf"), 1_000_001, None])
def test_c15_a_bad_volume_scale_is_refused(value):                                     # C15
    assert _error({"workload": {"volume_scale": value}}).where == "workload.volume_scale"
    err = _error({"workload": {"apps": {"app-a": {"volume_scale": value}}}})
    assert err.where == "workload.apps.app-a.volume_scale"


def test_volume_scale_bounds_are_accepted():
    for value in (0.001, 1, 1_000_000):
        _apply({"workload": {"volume_scale": value}})


def test_c15_volume_scale_for_an_unknown_app_is_refused():                             # C15
    assert _error({"workload": {"apps": {"nobody": {"volume_scale": 2}}}}).where == \
        "workload.apps.nobody"


@pytest.mark.parametrize("limits,where", [
    ({"max_concurrency": 0}, "capacity.local-mid.max_concurrency"),
    ({"max_concurrency": -2}, "capacity.local-mid.max_concurrency"),
    ({"tokens_per_minute": "400000"}, "capacity.local-mid.tokens_per_minute"),
    ({"requests_per_minute": True}, "capacity.local-mid.requests_per_minute"),
    ({"output_tokens_per_minute": None}, "capacity.local-mid.output_tokens_per_minute"),
    ({"max_concurrency": float("inf")}, "capacity.local-mid.max_concurrency"),
    ({"tokens_per_second": 5}, "capacity.local-mid.tokens_per_second"),
    (8, "capacity.local-mid"),
])
def test_c15_a_bad_limit_is_refused_naming_it(limits, where):                          # C15
    """A limit of 0 is never read as "no capacity": the key is left out for
    "not stated"."""
    err = _error({"capacity": {"local-mid": limits}})
    assert err.where == where
    if limits == {"max_concurrency": 0}:
        assert "not stated" in err.message


def test_c15_a_capacity_for_an_endpoint_that_is_not_there_is_refused():                # C15
    err = _error({"capacity": {"local-typo": {"max_concurrency": 8}}})
    assert err.where == "capacity.local-typo" and "not an onboarded endpoint" in err.message
    removed = _error({"endpoints": {"remove": ["local-mid"]},
                      "capacity": {"local-mid": {"max_concurrency": 8}}})
    assert removed.where == "capacity.local-mid" and "removes it" in removed.message
    assert _error({"capacity": []}).where == "capacity"


def test_a_capacity_may_name_an_added_a_disabled_or_a_pool_endpoint():
    snapshot = make_snapshot()
    pool_model = sorted(snapshot["settings"]["pricing"]["models"])[0]
    _apply({"endpoints": {"add": [{"slug": "local-new", "provider": "onprem", "tier": "mid",
                                   "price": {"input": 0.1, "output": 0.2, "cached_input": None}}],
                          "disable": ["local-big"]},
            "capacity": {"local-new": {"max_concurrency": 4},
                         "local-big": {"requests_per_minute": 60},
                         pool_model: {"tokens_per_minute": 90000},
                         "local-cheap": {}}}, snapshot)


def test_validate_capacity_limits_is_the_one_rule_for_a_file_and_a_proposal():
    validate_capacity_limits("x", {key: 1 for key in CAPACITY_LIMITS})
    validate_capacity_limits("x", {"max_concurrency": 0.5})          # a fraction is legal
    with pytest.raises(ProposalError):
        validate_capacity_limits("x", {"max_concurrency": 0})


def test_unknown_keys_are_refused_before_any_snapshot_is_read():
    with pytest.raises(ProposalError) as err:
        _validate_keys({"schema": P, "capacity": {"local-mid": {"max_concurency": 8}}})
    assert err.value.where == "capacity.local-mid.max_concurency"
    with pytest.raises(ProposalError) as err:
        _validate_keys({"schema": P, "workload": {"volume": 2}})
    assert err.value.where == "workload.volume"


def test_a_scenario_body_takes_the_same_keys():
    doc = {"schema": SCENARIOS_SCHEMA, "scenarios": [
        {"name": "twice", "proposal": {"workload": {"volume_scale": 2},
                                       "capacity": {"local-mid": {"max_concurrency": 8}}}}]}
    validate_scenarios(doc)
    doc["scenarios"][0]["proposal"]["capacity"]["local-mid"] = {"max_concurency": 8}
    with pytest.raises(ScenarioError) as err:
        validate_scenarios(doc)
    assert err.value.where == "scenarios[0](twice).proposal.capacity.local-mid.max_concurency"
