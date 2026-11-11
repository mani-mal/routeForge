"""route_reason: why the served model sits in the tier it does (content-free)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.apps.outcomes import ROUTE_REASONS, build_ok_outcome, derive_route_reason
from app.policy.feasible import RESIDENCY


def _d(**kw):
    base = dict(cache_hit=False, attempts=["m"], chosen_tier="mid",
                tier_target="mid", complexity_tier="cheap", rejected=[], n_eligible=3)
    base.update(kw)
    return derive_route_reason(**base)


def test_vocabulary():
    assert ROUTE_REASONS == ("failover", "escalation", "quality_floor", "placement",
                             "risk_floor", "length_tier", "only_eligible", "other")


def test_cache_hit_is_null():
    assert _d(cache_hit=True) is None


def test_no_attempts_is_null():
    assert _d(attempts=[]) is None


def test_failover():
    assert _d(attempts=["a", "b"]) == "failover"


def test_placement():
    rej = [{"name": "x", "tier": "cheap", "code": RESIDENCY}]
    assert _d(rejected=rej) == "placement"


def test_placement_needs_strictly_lower_tier():
    rej = [{"name": "x", "tier": "mid", "code": RESIDENCY}]
    assert _d(rejected=rej, tier_target="mid", complexity_tier="mid") == "length_tier"


def test_non_placement_code_ignored():
    rej = [{"name": "x", "tier": "cheap", "code": "circuit_open"}]
    assert _d(rejected=rej, tier_target="mid", complexity_tier=None) == "other"


def test_risk_floor():
    assert _d(tier_target="mid", complexity_tier="cheap") == "risk_floor"


def test_length_tier():
    assert _d(chosen_tier="cheap", tier_target="cheap", complexity_tier="cheap") == "length_tier"


def test_only_eligible():
    assert _d(n_eligible=1, tier_target="mid", complexity_tier="frontier") == "only_eligible"


def test_other():
    assert _d(tier_target=None, complexity_tier=None) == "other"


def test_precedence_failover_beats_placement():
    rej = [{"name": "x", "tier": "cheap", "code": RESIDENCY}]
    assert _d(attempts=["a", "b"], rejected=rej) == "failover"


def test_failed_first_hop_without_chosen_tier_is_failover():
    assert _d(attempts=["a", "b"], chosen_tier=None) == "failover"


def test_unknown_tier_strings_never_raise():
    rej = [{"name": "x", "tier": "weird", "code": RESIDENCY}]
    assert _d(rejected=rej, chosen_tier="odd", tier_target="odd2",
              complexity_tier="odd3", n_eligible=2) == "other"


def test_build_ok_outcome_sets_route_reason():
    trace = [{"step": "router", "hop_index": 0, "chosen": "m", "chosen_tier": "cheap",
              "tier_target": "cheap", "rejected": [],
              "scoring": {"candidates": [{"name": "m"}, {"name": "n"}]},
              "latency_ms": 1.0}]
    resp = {"trace": trace, "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            "cost": {"total_usd": 0.0}, "policy": {}}
    row = build_ok_outcome(resp, request_id="r", application_id="a",
                           outcome_ctx={"complexity": 0.2, "complexity_tier": "cheap"},
                           routing_policy_version="v", workload=None,
                           workload_source="none")
    assert row["route_reason"] == "length_tier"
    assert row["rejected"] == []


def test_migration_lists_route_reason():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_app_policies.py"
    spec = importlib.util.spec_from_file_location("migrate_app_policies", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.OUTCOMES_TABLE == "routing_outcomes"
    assert "route_reason" in {c[0] for c in mod.OUTCOMES_COLUMNS}


def test_onprem_long_prompt_is_length_tier_not_placement():
    rej = [{"name": "azure-gpt-cheap", "tier": "cheap", "code": RESIDENCY}]
    assert _d(rejected=rej, chosen_tier="frontier", tier_target="frontier",
              complexity_tier="frontier") == "length_tier"

def test_placement_when_length_tier_candidate_rejected():
    rej = [{"name": "x", "tier": "cheap", "code": RESIDENCY}]
    assert _d(rejected=rej, chosen_tier="mid", tier_target="cheap",
              complexity_tier="cheap") == "placement"

def test_escalation_distinct_from_failover():
    assert _d(attempts=["a", "b"], served_hops=2) == "escalation"

def test_failover_beats_escalation():
    assert _d(attempts=["a", "b", "c"], served_hops=2) == "failover"

def test_missing_scoring_is_not_only_eligible():
    assert _d(n_eligible=None, tier_target="mid", complexity_tier="frontier") == "other"


# --- B80: quality floor ----------------------------------------------------

def test_route_reason_quality_floor():
    # An enforced floor that was met picked the served model: that, not the
    # length/risk/placement tier logic, is why it sits where it does.
    assert _d(floor_status="met") == "quality_floor"
    rej = [{"name": "x", "tier": "cheap", "code": RESIDENCY}]
    assert _d(floor_status="met", rejected=rej) == "quality_floor"
    assert len("quality_floor") <= 16   # routing_outcomes.route_reason is String(16)


@pytest.mark.parametrize("status", [None, "none", "unmet_no_qualifier", "unmet_context",
                                    "unmet_budget", "shadow_met",
                                    "shadow_unmet_no_qualifier"])
def test_route_reason_quality_floor_only_when_met(status):
    assert _d(floor_status=status) == "risk_floor"


def test_route_reason_failover_and_escalation_beat_quality_floor():
    assert _d(floor_status="met", attempts=["a", "b"]) == "failover"
    assert _d(floor_status="met", attempts=["a", "b"], served_hops=2) == "escalation"
    assert _d(floor_status="met", cache_hit=True) is None


def _floor_hop(status):
    return {"step": "router", "hop_index": 0, "chosen": "m", "chosen_tier": "mid",
            "tier_target": "mid", "rejected": [],
            "scoring": {"candidates": [{"name": "n"}, {"name": "m"}],
                        "quality_floor": {"floor": 0.8, "mode": "enforce",
                                          "workload": "claims", "status": status}},
            "latency_ms": 1.0}


def test_build_ok_outcome_route_reason_reads_first_served_hop_floor_status():
    resp = {"trace": [_floor_hop("met")],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            "cost": {"total_usd": 0.0}, "policy": {}}
    row = build_ok_outcome(resp, request_id="r", application_id="a",
                           outcome_ctx={"complexity": 0.2, "complexity_tier": "cheap"},
                           routing_policy_version="v", workload="claims",
                           workload_source="client")
    assert row["route_reason"] == "quality_floor"
    resp["trace"] = [_floor_hop("shadow_met")]
    row = build_ok_outcome(resp, request_id="r", application_id="a",
                           outcome_ctx={"complexity": 0.2, "complexity_tier": "cheap"},
                           routing_policy_version="v", workload="claims",
                           workload_source="client")
    assert row["route_reason"] == "risk_floor"
