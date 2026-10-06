"""P3a Task 4: replay invariants (spec §9.1-§9.6)."""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.eval.simulate import (EXCLUSION_REASONS, REPORT_CAVEAT, agreement_ok,
                               baseline_check, observed_latencies, parse_since,
                               render_report, select_rows, simulate, simulate_row,
                               summarize)
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
            make_row("e", "app-a", complexity=None),
            # B60: a rejected row from before `routing_tokens` was recorded
            make_row("f", "app-a", status="no_eligible", prompt_tokens=0)]
    rows, excluded = select_rows(recs, {"app-a"})
    assert [r["request_id"] for r in rows] == ["a"]
    assert excluded == {"before_since": 0, "cache_hit": 1, "unknown_status": 1,
                        "unknown_app": 1, "missing_features": 1,
                        "missing_routing_tokens": 1}


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


def test_cost_usd_both_routed_excludes_newly_rejected_rows():             # M1
    """A newly rejected row drops out of the proposed `cost_usd` and looks
    like a saving; `cost_usd_both_routed` compares only rows routed in BOTH
    arms, so removing app-a's only model shows zero like-for-like change."""
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"]),
                               app_entry("app-b")])
    rows = _rows(3, "app-a") + [make_row(f"b{i}", "app-b", ts=f"2026-09-20T13:00:0{i}+00:00")
                                for i in range(3)]
    results, excluded = _run(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    t = sim["totals"]
    assert t["newly_rejected"] == 3
    assert t["cost_usd"]["delta"] < 0                       # the misleading "saving"
    both = t["cost_usd_both_routed"]
    assert set(both) == {"current", "proposed", "delta"}
    assert both["delta"] == 0 and both["current"] == both["proposed"] > 0
    assert sim["by_app"]["app-a"]["cost_usd_both_routed"]["current"] == 0
    assert "cost_usd_both_routed" in next(iter(sim["by_workload"].values()))
    report = render_report(sim)
    headline = report.split("## Headline")[1].split("## New rejections")[0]
    assert "cost_usd (both routed)" in headline


# --- B55: recorded pin and output cap ----------------------------------------

def test_pinned_row_replays_to_the_pinned_model():
    """Unpinned, a 0.1-complexity row routes to local-cheap; pinned to
    local-big it must replay to local-big (as the gate would), and the
    baseline agrees with the recorded first attempt."""
    unpinned = make_row("u", "app-a", complexity=0.1)
    pinned = make_row("p", "app-a", complexity=0.1, requested_model="local-big",
                      attempts=["local-big"], chosen="local-big")
    results, excluded = _run([unpinned, pinned], make_snapshot(), {"schema": P})
    by = {r.row["request_id"]: r for r in results}
    assert by["u"].current.model == "local-cheap"
    assert by["p"].current.model == "local-big"
    assert by["p"].current.eligible == ("local-big",)
    sim = summarize(*_run([pinned], make_snapshot(), {"schema": P}),
                    inputs=_INPUTS, edits={})
    assert sim["baseline"]["agreement"] == 1.0


def test_null_pin_replays_unpinned():
    row = make_row("n", "app-a", complexity=0.1, requested_model=None, max_tokens=None)
    (r,), _ = _run([row], make_snapshot(), {"schema": P})
    assert r.current.model == "local-cheap"


def test_removing_the_pinned_model_newly_rejects_the_row():
    pinned = make_row("p", "app-a", requested_model="local-big",
                      attempts=["local-big"], chosen="local-big")
    (r,), _ = _run([pinned], make_snapshot(),
                   {"schema": P, "endpoints": {"remove": ["local-big"]}})
    assert (r.current.outcome, r.proposed.outcome) == ("routed", "rejected")
    assert r.proposed.reject_codes == ("not_requested",)
    sim = summarize(*_run([pinned], make_snapshot(),
                          {"schema": P, "endpoints": {"remove": ["local-big"]}}),
                    inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_rejected"] == 1


def test_old_export_row_without_the_keys_still_simulates_unpinned():
    row = make_row("old", "app-a", complexity=0.1)
    assert "requested_model" not in row and "max_tokens" not in row
    rows, excluded = select_rows([row], {"app-a"})
    assert len(rows) == 1 and excluded["missing_features"] == 0
    (r,), _ = _run([row], make_snapshot(), {"schema": P})
    assert r.current.model == "local-cheap"


@pytest.mark.parametrize("over", [
    {"requested_model": 5}, {"requested_model": ["local-big"]},
    {"max_tokens": "64"}, {"max_tokens": 0}, {"max_tokens": -3},
    {"max_tokens": True}, {"max_tokens": 1.5},
])
def test_select_rows_malformed_pin_or_cap_is_missing_features(over):
    rows, excluded = select_rows([make_row("r1", "app-a", **over)], {"app-a"})
    assert rows == [] and excluded["missing_features"] == 1


# --- Task 5 (P3b): simulator groups new rejections by conflicting core ------

def test_summarize_counts_new_rejections_by_core():
    """Removing app-a's only allowed model (local-mid) leaves local-cheap and
    local-big as the available candidates; both violate
    allowed_models=["local-mid"] and nothing else, so the minimal conflicting
    core is exactly {allowed_models}. Verified by calling the router directly
    (scn.router.select) on this exact scenario and inspecting
    NoEligibleCandidate.core.ids(), which returned ('allowed_models',)."""
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    results, excluded = _run(_rows(3), snap, {"schema": P,
                                              "endpoints": {"remove": ["local-mid"]}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["new_rejections"]["by_core"] == {"allowed_models": 3}


def test_summarize_disabled_pinned_model_groups_by_requested_model_core():
    """Disabling the pinned model (local-big) makes it state `disabled` --
    excluded from the core computation's available pool (spec §2.2). The
    remaining candidates (local-cheap, local-mid) all violate the pin
    (`requested_model`), so the core is {requested_model} and the state is
    {disabled: [local-big]}. Verified by calling the router directly on this
    exact scenario: NoEligibleCandidate.core.ids() == ('requested_model',),
    .state_ids() == ('disabled',). Because the core is non-empty, the by_core
    key uses "+".join(core) ("requested_model"), never the "(state:...)"
    fallback."""
    pinned = make_row("p", "app-a", requested_model="local-big",
                      attempts=["local-big"], chosen="local-big")
    results, excluded = _run([pinned], make_snapshot(),
                             {"schema": P, "endpoints": {"disable": ["local-big"]}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["new_rejections"]["by_core"] == {"requested_model": 1}


def test_summarize_empty_core_falls_back_to_state_key():
    """The default snapshot routes fine (three onprem models); a proposal
    that removes every one of them leaves the proposed effective pool
    literally empty -- there is no *available* candidate for the core
    computation to reason about (spec §2.2), so the core is empty and the
    rejection is explained purely by state. Verified directly by calling the
    router on this exact scenario: NoEligibleCandidate.core.ids() == (),
    .state_ids() == ('no_endpoints',). Per spec the by_core key then falls
    back to "(state:" + ",".join(core_state) + ")"."""
    results, excluded = _run([make_row("x", "app-a")], make_snapshot(), {"schema": P,
        "endpoints": {"remove": ["local-cheap", "local-mid", "local-big"]}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["new_rejections"]["by_core"] == {"(state:no_endpoints)": 1}


def test_arm_result_core_empty_for_routed_row():
    results, _ = _run(_rows(1), make_snapshot(), {"schema": P})
    (r,) = results
    assert r.current.core == () and r.current.core_state == ()
    assert r.proposed.core == () and r.proposed.core_state == ()


def test_report_by_conflicting_core_table_present():
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    results, excluded = _run(_rows(3), snap, {"schema": P,
                                              "endpoints": {"remove": ["local-mid"]}})
    md = render_report(summarize(results, excluded, inputs=_INPUTS, edits={}))
    assert "By conflicting core" in md
    assert "| allowed_models | 3 |" in md


def test_report_by_conflicting_core_table_absent_when_no_new_rejections():
    results, excluded = _run(_rows(), make_snapshot(), {"schema": P})
    md = render_report(summarize(results, excluded, inputs=_INPUTS, edits={}))
    assert "By conflicting core" not in md


def test_recorded_max_tokens_reaches_the_budget_check():
    """Only local-big exists: 200 prompt tokens at the default 512-token output
    estimate costs ~0.0035 USD (under the 0.01 cap); a recorded 100k-token
    client cap prices at ~0.6 USD and is rejected over budget."""
    snap = make_snapshot(onboarded=[onboarded_row("local-big", "frontier")],
                         apps=[app_entry("app-a", max_cost_usd_per_request=0.01)])
    no_cap = make_row("nc", "app-a", attempts=["local-big"], chosen="local-big")
    big_cap = make_row("bc", "app-a", attempts=["local-big"], chosen="local-big",
                       max_tokens=100_000)
    results, _ = _run([no_cap, big_cap], snap, {"schema": P})
    by = {r.row["request_id"]: r for r in results}
    assert by["nc"].current.outcome == "routed"
    assert by["bc"].current.outcome == "rejected"
    assert "over_budget" in by["bc"].current.reject_codes


# --- P3c-2 Task 1: --since row cut (spec §6, S10) -----------------------------

def _since_rows():
    return [make_row("old1", "app-a", ts="2026-09-20T11:59:59+00:00"),
            make_row("old2", "app-a", ts="2026-09-19T00:00:00+00:00", cache_hit=True),
            make_row("edge", "app-a", ts="2026-09-20T12:00:00+00:00"),
            make_row("new1", "app-a", ts="2026-09-21T00:00:00+00:00")]


def test_s10_since_excludes_and_counts_old_rows():                         # S10
    since = parse_since("2026-09-20T12:00:00Z")
    results, excluded = asyncio.run(simulate(
        _since_rows(), make_snapshot(), {"schema": P}, base=BASE_SETTINGS, since=since))
    # none before the cut reaches replay; the boundary row (ts == since) does
    assert [r.row["request_id"] for r in results] == ["edge", "new1"]
    # before_since is checked FIRST: old2 is also a cache hit, counted once, here
    assert excluded["before_since"] == 2 and excluded["cache_hit"] == 0
    assert EXCLUSION_REASONS[0] == "before_since"


def test_since_equals_a_precut_export():
    """`--since` is a cut of the export: every number equals a run over an
    export that never held the old rows -- including the observed-latency
    medians, which an old row must not feed."""
    old = make_row("old", "app-a", ts="2026-09-01T00:00:00+00:00", latency_ms=99999.0)
    new = [make_row(f"n{i}", "app-a", ts=f"2026-09-20T12:00:0{i}+00:00") for i in range(3)]
    since = parse_since("2026-09-10T00:00:00+00:00")
    cut, cut_excluded = asyncio.run(simulate(
        [old] + new, make_snapshot(), {"schema": P}, base=BASE_SETTINGS, since=since))
    pre, _ = _run(new, make_snapshot(), {"schema": P})
    assert [(r.row["request_id"], r.current, r.proposed) for r in cut] == \
           [(r.row["request_id"], r.current, r.proposed) for r in pre]
    assert all(r.current.latency_ms == 120.0 for r in cut)      # not the old row's 99999
    assert cut_excluded["before_since"] == 1


@pytest.mark.parametrize("flag", ["2026-09-20T12:00:00Z", "2026-09-20T12:00:00+00:00",
                                  "2026-09-20T12:00:00", "2026-09-20T14:00:00+02:00"])
@pytest.mark.parametrize("row_ts,kept", [
    ("2026-09-20T12:00:00Z", True),             # Z suffix, on the boundary
    ("2026-09-20T12:00:00", True),              # naive is read as UTC
    ("2026-09-20T13:59:59+02:00", False),       # 11:59:59 UTC
    ("2026-09-20T07:00:00-05:00", True),        # 12:00:00 UTC
    ("2026-09-20T11:59:59.999999+00:00", False),
])
def test_since_compares_instants_not_strings(flag, row_ts, kept):          # Review Focus 1
    rows, excluded = select_rows([make_row("r", "app-a", ts=row_ts)], {"app-a"},
                                 since=parse_since(flag))
    assert (len(rows) == 1) is kept
    assert excluded["before_since"] == (0 if kept else 1)


def test_since_row_without_ts_is_missing_features_not_simulated():         # Review Focus 1
    """A row with no `ts` cannot be placed in the window. Without a cut it
    is simulated as before; with one it is counted, never guessed at."""
    row = make_row("r", "app-a", ts=None)
    assert len(select_rows([row], {"app-a"})[0]) == 1
    rows, excluded = select_rows([row], {"app-a"}, since=parse_since("2026-01-01"))
    assert rows == [] and excluded["missing_features"] == 1 and excluded["before_since"] == 0


@pytest.mark.parametrize("bad", ["yesterday", "", "2026-13-01", "12:00"])
def test_parse_since_rejects_non_timestamps(bad):
    with pytest.raises(ValueError):
        parse_since(bad)


# --- P3c-2 Task 2: the agreement gate's inputs (spec §6) ----------------------

def test_baseline_check_equals_the_summarized_baseline():
    """`baseline_check` (no proposal) and `summarize()["baseline"]` (empty
    proposal) are the same block: both replay the snapshot as exported."""
    rows = _rows(4) + [make_row("d", "app-a", attempts=["local-big"], chosen="local-big")]
    sim = summarize(*_run(rows, make_snapshot(), {"schema": P}), inputs=_INPUTS, edits={})
    assert asyncio.run(baseline_check(rows, make_snapshot(), base=BASE_SETTINGS)) \
        == sim["baseline"]


@pytest.mark.parametrize("agreement,minimum,ok", [
    (1.0, 1.0, True), (0.9, 0.9, True), (0.899999, 0.9, False),
    (0.0, 0.0, True), (None, 0.0, False),
])
def test_agreement_ok(agreement, minimum, ok):
    # Outcome agreement is perfect here, so model agreement alone decides
    # (B60: the gate takes the lower of the two --
    # tests/eval/test_replay_rejections.py::test_r12_gate_takes_the_lower_of_the_two).
    assert agreement_ok({"agreement": agreement, "rows_compared": 0,
                         "outcome_agreement": 1.0}, minimum) is ok
