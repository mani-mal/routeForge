"""P3a Task 4: replay invariants (spec §9.1-§9.6)."""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.eval.simulate import (REPORT_CAVEAT, observed_latencies, render_report, select_rows,
                               simulate, simulate_row, summarize)
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"

_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": "t", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}


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


# --- Task 5: summarize, render_report, REPORT_CAVEAT ------------------------

def test_summarize_empty_proposal_zero_deltas():
    results, excluded = _run(_rows(), make_snapshot(), {"schema": P})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    t = sim["totals"]
    assert t["route_changed"] == t["newly_rejected"] == t["newly_served"] == 0
    assert t["cost_usd"]["delta"] == 0
    assert sim["moves"] == [] and sim["affected_apps"] == []
    assert sim["schema"] == "routeforge.simulation/v1"


def test_summarize_counts_new_rejections_and_moves():
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    results, excluded = _run(_rows(3), snap, {"schema": P,
                                              "endpoints": {"remove": ["local-mid"]}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_rejected"] == 3
    assert sim["moves"] == [{"from": "local-mid", "to": "(rejected)", "count": 3}]
    assert sim["affected_apps"] == ["app-a"]
    assert sim["new_rejections"]["by_app"] == {"app-a": 3}


def test_baseline_agreement_and_warning():
    rows = _rows(4)                              # all recorded attempts[0] == local-cheap
    results, excluded = _run(rows, make_snapshot(), {"schema": P})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["baseline"]["rows_compared"] == 4
    assert 0.0 <= sim["baseline"]["agreement"] <= 1.0
    assert sim["baseline"]["warning"] == (sim["baseline"]["agreement"] < 0.90)


def test_report_opens_with_caveat():
    results, excluded = _run(_rows(), make_snapshot(), {"schema": P})
    md = render_report(summarize(results, excluded, inputs=_INPUTS, edits={}))
    assert md.startswith("> " + REPORT_CAVEAT.split("\n")[0])
    assert "## New rejections" in md and "## Excluded rows" in md


def test_summarize_deterministic():                                        # §9.8
    a = summarize(*_run(_rows(), make_snapshot(), {"schema": P}), inputs=_INPUTS, edits={})
    b = summarize(*_run(_rows(), make_snapshot(), {"schema": P}), inputs=_INPUTS, edits={})
    import json
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_entity_placeholder_only_type_is_read():
    """Poisoned start/end/value on the placeholder Entity must not affect the
    replayed decision -- only `.type` is read on this path (spec §7)."""
    snap = make_snapshot()
    row = make_row("poison-check", "app-a", detected_entity_types=["US_SSN"])
    import asyncio as _asyncio
    from app.eval.scenario import build_scenario, close_scenario

    async def _both():
        scn = await build_scenario(snap, base=BASE_SETTINGS)
        try:
            observed = observed_latencies([row])
            normal = simulate_row(scn, row, observed)

            import app.eval.simulate as sim_mod
            real_entity = sim_mod.Entity
            poisoned = []

            def _poison_entity(*, type, start, end, value):  # noqa: A002
                poisoned.append((start, end, value))
                return real_entity(type=type, start=999, end=-1, value="POISON")

            sim_mod.Entity = _poison_entity
            try:
                tampered = simulate_row(scn, row, observed)
            finally:
                sim_mod.Entity = real_entity
            return normal, tampered, poisoned
        finally:
            await close_scenario(scn)

    normal, tampered, poisoned = _asyncio.run(_both())
    assert poisoned, "the monkeypatched Entity constructor was never called"
    assert tampered == normal


def test_signal_latency_rung_used_when_no_observed_rows():
    """A model with enough snapshot signal samples and no observed rows in
    the export routes with `latency_source == 'signal'` and the snapshot's
    latency value (spec §7's latency ladder, rung 2)."""
    min_samples = BASE_SETTINGS.routing.signals.min_samples
    snap = make_snapshot(signals={
        "local-cheap": {"latency_ms": 4242.0, "latency_n": min_samples,
                        "availability": 1.0, "availability_n": min_samples}})
    # status != "ok" so this row's own latency is never folded into
    # `observed_latencies` -- otherwise the "observed" rung would shadow
    # the "signal" rung this test targets.
    row = make_row("sig1", "app-a", complexity=0.05, status="backend_failed")
    results, _ = _run([row], snap, {"schema": P})
    (r,) = results
    assert r.current.model == "local-cheap"
    assert r.current.latency_source == "signal"
    assert r.current.latency_ms == 4242.0


@pytest.mark.parametrize("over", [                                          # Important 3
    {"request_id": None}, {"request_id": 7},
    {"complexity": "high"}, {"complexity": True}, {"prompt_tokens": "abc"},
    {"prompt_tokens": None}, {"completion_tokens": "x"},
    {"detected_entity_types": "US_SSN"}, {"detected_entity_types": [["US_SSN"]]},
    {"workload": ["a"]}, {"ts": 5}, {"ts": "not-a-time"},
    {"attempts": "local-cheap"}, {"attempts": [["local-cheap"]]},
])
def test_select_rows_malformed_features_are_counted_not_crashed(over):
    row = make_row("r1", "app-a")
    row.update(over)
    rows, excluded = select_rows([row], {"app-a"})
    assert rows == [] and excluded["missing_features"] == 1


def test_select_rows_missing_keys_and_unhashables():
    no_types = make_row("r1", "app-a")
    del no_types["detected_entity_types"]
    no_id = make_row("r2", "app-a")
    del no_id["request_id"]
    recs = [no_types, no_id,
            make_row("r3", "app-a", status=["ok"]),
            make_row("r4", ["app-a"])]
    rows, excluded = select_rows(recs, {"app-a"})
    assert rows == []
    assert (excluded["missing_features"], excluded["unknown_status"],
            excluded["unknown_app"]) == (2, 1, 1)
    # null detected_entity_types is legal (no entities recorded)
    ok, _ = select_rows([make_row("r5", "app-a", detected_entity_types=None)], {"app-a"})
    assert len(ok) == 1


def test_observed_latencies_ignores_malformed_attempts():
    assert observed_latencies([make_row("r1", "a", attempts=5),
                               make_row("r2", "a", attempts=[["x"]], chosen=["x"])]) == {}
