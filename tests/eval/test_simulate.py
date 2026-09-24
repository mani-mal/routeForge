"""P3a Task 4: replay invariants (spec §9.1-§9.6)."""
from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.eval.simulate import observed_latencies, select_rows, simulate
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"


def _run(records, snap, prop):
    return asyncio.run(simulate(records, snap, prop, base=BASE_SETTINGS))


def _rows(n=6, app="app-a", **over):
    return [make_row(f"r{i}", app, complexity=[0.05, 0.6, 1.5][i % 3],
                     ts=f"2026-09-20T12:00:{i:02d}+00:00", **over) for i in range(n)]


def test_empty_proposal_zero_change():                                    # §9.1
    results, _ = _run(_rows(), make_snapshot(), {"schema": P})
    assert results
    for r in results:
        assert r.current == r.proposed


def test_onprem_only_never_cloud_and_rejects_without_onprem():            # §9.3
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    results, _ = _run(_rows(), snap, {"schema": P,
        "apps": {"app-a": {"residency": "onprem_only"}}})
    for r in results:
        assert r.proposed.provider in (None, "onprem")
    snap2 = make_snapshot(onboarded=[], providers={"azure": {"enabled": True}})
    results2, _ = _run(_rows(), snap2, {"schema": P,
        "apps": {"app-a": {"residency": "onprem_only"}}})
    assert all(r.proposed.outcome == "rejected" for r in results2)


def test_sole_model_removal_rejects_exactly_those_rows():                  # §9.4
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"]),
                               app_entry("app-b")])
    rows = _rows(3, "app-a") + [make_row(f"b{i}", "app-b", ts=f"2026-09-20T13:00:0{i}+00:00")
                                for i in range(3)]
    results, _ = _run(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})
    for r in results:
        if r.row["application_id"] == "app-a":
            assert (r.current.outcome, r.proposed.outcome) == ("routed", "rejected")
        else:
            assert r.proposed.outcome == "routed"


def test_price_change_moves_cost_exactly():                                # §9.5
    rows = _rows()
    results, _ = _run(rows, make_snapshot(), {"schema": P,
        "prices": {"local-cheap": {"input": 1.0, "output": 2.0, "cached_input": None}}})
    for r in results:
        assert r.current.model == r.proposed.model
        if r.current.model == "local-cheap":
            p, c = r.row["prompt_tokens"], r.row["completion_tokens"]
            expected = (p * 1.0 + c * 2.0) / 1_000_000
            assert abs(r.proposed.cost_usd - expected) < 1e-12
        else:
            assert r.proposed.cost_usd == r.current.cost_usd


def test_sensitivity_rule_replays_recorded_entity_types():                 # §9.6
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    rows = [make_row("s1", "app-a", detected_entity_types=["US_SSN"]),
            make_row("s2", "app-a", detected_entity_types=[])]
    rule = {"entities": ["US_SSN"], "sensitivity": "restricted", "risk_class": "high",
            "residency": "onprem_only", "require_verification": True}
    results, _ = _run(rows, snap, {"schema": P,
        "apps": {"app-a": {"sensitivity_rules": [rule]}}})
    by = {r.row["request_id"]: r for r in results}
    assert by["s1"].proposed.provider in (None, "onprem")
    assert by["s2"].proposed == by["s2"].current


def test_select_rows_exclusions():
    recs = [make_row("a", "app-a"), make_row("b", "app-a", cache_hit=True),
            make_row("c", "app-a", status="weird"), make_row("d", "ghost"),
            make_row("e", "app-a", complexity=None)]
    rows, excluded = select_rows(recs, {"app-a"})
    assert [r["request_id"] for r in rows] == ["a"]
    assert excluded == {"cache_hit": 1, "unknown_status": 1, "unknown_app": 1,
                        "missing_features": 1}


def test_observed_latency_median_single_attempt_ok_only():
    recs = [make_row("1", "app-a", latency_ms=100.0), make_row("2", "app-a", latency_ms=300.0),
            make_row("3", "app-a", latency_ms=9999.0, attempts=["x", "local-cheap"]),
            make_row("4", "app-a", latency_ms=9999.0, status="backend_failed")]
    assert observed_latencies(recs) == {"local-cheap": 200.0}


_TIGHTEN = st.fixed_dictionaries({}, optional={
    "residency": st.just("onprem_only"),
    "allowed_tiers": st.lists(st.sampled_from(["cheap", "mid", "frontier"]),
                              min_size=1, unique=True),
    "allowed_regions": st.just(["onprem"]),
    "require_no_retention": st.just(True),
})


@hsettings(max_examples=30, deadline=None,
           suppress_health_check=[HealthCheck.too_slow])
@given(tighten=_TIGHTEN,
       disable=st.lists(st.sampled_from(["local-cheap", "local-mid", "local-big"]),
                        unique=True, max_size=2),
       etypes=st.lists(st.sampled_from(["US_SSN", "EMAIL_ADDRESS", "PERSON"]),
                       unique=True, max_size=2),
       complexity=st.floats(0.0, 3.0))
def test_stricter_never_widens(tighten, disable, etypes, complexity):      # §9.2
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    prop = {"schema": P, "apps": {"app-a": tighten}}
    if disable:
        prop["endpoints"] = {"disable": disable}
    rows = [make_row("h", "app-a", detected_entity_types=etypes, complexity=complexity)]
    results, _ = _run(rows, snap, prop)
    (r,) = results
    assert set(r.proposed.eligible) <= set(r.current.eligible)


def test_proposed_rule_on_custom_entity_moves_cloud_row_onprem():         # §9.6, non-vacuous
    # The stock policy.yaml already escalates US_SSN, so the test above cannot
    # show a CHANGE. A type no global rule matches does: cloud -> on-prem.
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    rule = {"entities": ["PROJECT_CODE"], "sensitivity": "restricted", "risk_class": "high",
            "residency": "onprem_only", "require_verification": True}
    rows = [make_row("p1", "app-a", complexity=0.6, detected_entity_types=["PROJECT_CODE"])]
    (r,), _ = _run(rows, snap, {"schema": P,
        "apps": {"app-a": {"sensitivity_rules": [rule]}}})
    assert r.current.provider == "azure"
    assert r.proposed.provider == "onprem"
    assert "azure-gpt-mid" not in r.proposed.eligible


def test_rejection_records_codes_and_no_cost():
    snap = make_snapshot(onboarded=[], providers={"azure": {"enabled": True}})
    (r,), _ = _run([make_row("x", "app-a")], snap, {"schema": P,
        "apps": {"app-a": {"residency": "onprem_only"}}})
    assert r.current.outcome == "routed"
    assert r.proposed.outcome == "rejected"
    assert r.proposed.reject_codes and "residency" in r.proposed.reject_codes
    assert r.proposed.eligible == ()
    assert (r.proposed.model, r.proposed.cost_usd, r.proposed.latency_ms) == (None, None, None)
