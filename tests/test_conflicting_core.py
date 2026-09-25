"""P3b Task 2: minimum hitting set + ConflictingCore (spec §2.4, §3, §5)."""
from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.policy.core import (REQUIREMENT_ORDER, SECURITY_IDS, build_core,
                             minimum_hitting_set)

IDS = st.sampled_from(REQUIREMENT_ORDER)
VSETS = st.lists(st.frozensets(IDS, min_size=1, max_size=4), min_size=1, max_size=8)

def _covers(core, vsets):
    return all(set(core) & v for v in vsets)

def _brute_min(vsets):
    u = [i for i in REQUIREMENT_ORDER if any(i in v for v in vsets)]
    for k in range(1, len(u) + 1):
        for combo in itertools.combinations(u, k):
            if _covers(combo, vsets):
                return combo

@hsettings(max_examples=300, deadline=None)
@given(VSETS)
def test_covering_minimum_irreducible(vsets):                      # §5.1-5.3
    core = minimum_hitting_set(vsets)
    assert _covers(core, vsets)
    assert len(core) == len(_brute_min(vsets))
    for drop in core:
        rest = [i for i in core if i != drop]
        assert not _covers(rest, vsets)

@hsettings(max_examples=100, deadline=None)
@given(VSETS, st.randoms())
def test_order_independent(vsets, rnd):                             # §5.5
    shuffled = list(vsets)
    rnd.shuffle(shuffled)
    assert minimum_hitting_set(vsets) == minimum_hitting_set(shuffled)

def test_empty_violation_set_raises():
    with pytest.raises(ValueError):
        minimum_hitting_set([frozenset({"residency"}), frozenset()])

def test_worked_example_onprem_pin():                                # §5.8
    vsets = [frozenset({"requested_model"}), frozenset({"residency"})]
    core = build_core({"local-8b": vsets[0], "azure-gpt-mid": vsets[1]},
                      state={}, details={"requested_model": {"value": "azure-gpt-mid"},
                                         "residency": {"value": "onprem_only"}},
                      sources={})
    assert core.ids() == ("residency", "requested_model")
    assert [r.security for r in core.requirements] == [True, False]
    assert "azure-gpt-mid" in core.summary()

def test_state_only_core_is_empty():                                 # §5.6
    core = build_core({}, state={"disabled": ["b", "a"], "circuit_open": []},
                      details={}, sources={})
    assert core.ids() == () and core.state == (("disabled", ("a", "b")),)

def test_sensitivity_rule_source_in_label():
    core = build_core({"x": frozenset({"residency"})}, state={},
                      details={"residency": {"value": "onprem_only", "entities": ["US_SSN"]}},
                      sources={"residency": "sensitivity_rule"})
    assert "US_SSN" in core.requirements[0].label

def test_security_hints_never_relax():                               # §5.7
    deny = ("remove", "disable", "relax", "lower", "loosen", "drop", "allow cloud",
            "turn off", "waive", "bypass")
    for rid in SECURITY_IDS:
        core = build_core({"x": frozenset({rid})}, state={}, details={}, sources={})
        for hint in core.requirements[0].hints:
            assert not any(d in hint.lower() for d in deny), (rid, hint)

def test_to_record_drops_label_and_hints():
    core = build_core({"x": frozenset({"residency"})}, state={}, details={}, sources={})
    rec = core.to_record()
    assert "label" not in rec["requirements"][0] and "hints" not in rec["requirements"][0]

def test_label_joins_list_and_set_details_review_finding_1():
    core = build_core({"x": frozenset({"residency"})}, state={},
                      details={"residency": {"value": "onprem_only",
                                              "entities": ["US_SSN", "US_DRIVER_LICENSE"]}},
                      sources={"residency": "sensitivity_rule"})
    assert core.requirements[0].label == (
        "must run on-premises (raised by sensitivity rule on US_SSN, US_DRIVER_LICENSE)")

    core = build_core({"x": frozenset({"allowed_providers"})}, state={},
                      details={"allowed_providers": {"providers": ["onprem", "azure"]}},
                      sources={})
    assert core.requirements[0].label == "provider must be one of onprem, azure"

    core = build_core({"x": frozenset({"allowed_regions"})}, state={},
                      details={"allowed_regions": {"regions": ["eu", "us"]}},
                      sources={})
    assert core.requirements[0].label == "endpoint region must be one of eu, us"

    core = build_core({"x": frozenset({"allowed_tiers"})}, state={},
                      details={"allowed_tiers": {"tiers": ["mid", "frontier"]}},
                      sources={})
    assert core.requirements[0].label == "tier must be one of mid, frontier"

    core = build_core({"x": frozenset({"allowed_models"})}, state={},
                      details={"allowed_models": {"models": ["m1", "m2"]}},
                      sources={})
    assert core.requirements[0].label == "model must be one of m1, m2"

def test_summary_shows_primary_detail_per_id_review_finding_2():
    core = build_core(
        {"a": frozenset({"allowed_regions"}), "b": frozenset({"max_cost_usd_per_request"}),
         "c": frozenset({"risk_floor"}), "d": frozenset({"require_no_retention"})},
        state={},
        details={"allowed_regions": {"regions": ["eu", "us"]},
                 "max_cost_usd_per_request": {"cap": 0.05},
                 "risk_floor": {"floor": "mid"}},
        sources={})
    text = core.summary()
    assert "allowed_regions (eu, us)" in text
    assert "max_cost_usd_per_request (cap $0.0500)" in text
    assert "risk_floor (>= mid)" in text
    # no detail supplied for require_no_retention -> bare id, no parenthetical
    assert "require_no_retention" in text
    assert "require_no_retention (" not in text
