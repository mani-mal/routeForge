"""B70 item 5 / B71 item 3: replay honours each row's RECORDED endpoint
state -- the circuits its first decision recorded open, and the endpoints its
attempt chain records as failed -- in both arms, and reports the recorded
cost beside the replayed one.

Fixture: mid-tier endpoints `m1..mN`, cheapest first, so with nothing open or
failed the router picks `m1`."""
from __future__ import annotations

import asyncio

import pytest

from app.eval.simulate import (BACKEND_FAILED, REPORT_CAVEAT, recorded_failures,
                               recorded_open_circuits, render_report, simulate, summarize)
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot, onboarded_row

P = "routeforge.policy-proposal/v1"
_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": "t", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}


def _run(records, snap, prop=None):
    return asyncio.run(simulate(records, snap, prop or {"schema": P}, base=BASE_SETTINGS))


def _summary(records, snap, prop=None):
    results, excluded = _run(records, snap, prop)
    return results, summarize(results, excluded, inputs=_INPUTS, edits={})


def _mids(n=2, *, max_failover=None, apps=("app-a",)):
    rows = [onboarded_row(f"m{i + 1}", "mid", price_input=0.1 * (i + 1)) for i in range(n)]
    snap = make_snapshot(onboarded=rows,
                         apps=[app_entry(a, max_cost_usd_per_request=100.0) for a in apps])
    if max_failover is not None:
        snap["settings"]["routing"]["max_failover_attempts"] = max_failover
    return snap


def _row(request_id="r", app_id="app-a", **over):
    return make_row(request_id, app_id, complexity=0.6, prompt_tokens=900, **over)


def _open(*names):
    """A recorded first decision's `rejected` list with `names` open."""
    return [{"name": n, "code": "circuit_open"} for n in names]


def _err(model, kind="http_5xx"):
    return {"model": model, "type": "APIError", "http_status": 503, "kind": kind}


# --- what a row records -------------------------------------------------------

def test_recorded_open_circuits_reads_only_circuit_open_rejections():
    row = _row(rejected=[{"name": "m1", "code": "circuit_open"},
                         {"name": "m2", "code": None},
                         {"name": "m3", "code": "residency"},
                         "garbage", {"code": "circuit_open"}])
    assert recorded_open_circuits(row) == frozenset({"m1"})
    assert recorded_open_circuits(_row()) == frozenset()          # no `rejected` key
    assert recorded_open_circuits(_row(rejected="nope")) == frozenset()


def test_recorded_failures_from_backend_errors_skips_size_refusals():
    row = _row(backend_errors=[_err("m1"), _err("m2", "context_overflow"), _err("m3", "timeout")])
    assert recorded_failures(row) == frozenset({"m1", "m3"})
    assert recorded_failures(_row(backend_errors=None)) == frozenset()


def test_recorded_failures_infers_only_where_the_record_is_unambiguous():
    """Before B117 a `backend_failed` row served nothing, so every attempt
    failed; an `ok` row's extra attempts may be escalations, so none is
    inferred. A row WITH the key (even null) is never inferred from."""
    assert recorded_failures(_row(status="backend_failed", chosen=None,
                                  attempts=["m1", "m2"])) == frozenset({"m1", "m2"})
    assert recorded_failures(_row(attempts=["m1", "m2"], chosen="m2")) == frozenset()
    assert recorded_failures(_row(status="backend_failed", chosen=None, attempts=["m1"],
                                  backend_errors=None)) == frozenset()


# --- B70 item 5: breaker state ------------------------------------------------

def test_without_the_record_replay_sends_a_diverted_row_to_the_open_endpoint():
    """The pre-fix behaviour, kept as the control: a row live served on m2
    (m1's circuit open) but with no `circuit_open` recorded replays to m1."""
    (r,), _ = _run([_row(attempts=["m2"], chosen="m2")], _mids())
    assert r.current.model == "m1"


def test_recorded_open_circuit_is_handed_to_the_router_in_both_arms():
    (r,), _ = _run([_row(attempts=["m2"], chosen="m2", rejected=_open("m1"))], _mids())
    assert (r.current.model, r.proposed.model) == ("m2", "m2")
    assert r.current.attempted == ("m2",) and r.current.failed == ()


def test_breaker_diverted_rows_are_their_own_class_and_left_out_of_agreement():
    """Two diverted rows (one per app) and two ordinary ones, one of which
    disagrees. Model agreement is over the two ordinary rows only."""
    snap = _mids(3, apps=("app-a", "app-b"))
    rows = [_row("d1", "app-a", attempts=["m2"], chosen="m2", rejected=_open("m1")),
            _row("d2", "app-b", attempts=["m2"], chosen="m2", rejected=_open("m1")),
            _row("ok", "app-a", attempts=["m1"], chosen="m1"),
            _row("bad", "app-b", attempts=["m3"], chosen="m3")]
    _results, sim = _summary(rows, snap)
    b = sim["baseline"]
    assert b["breaker_diverted"] == 2
    assert b["breaker_diverted_by_app"] == {"app-a": 1, "app-b": 1}
    assert b["breaker_diverted_reproduced"] == 2
    assert b["rows_compared"] == 2 and b["agreement"] == 0.5
    assert b["by_app"] == {"app-a": 1.0, "app-b": 0.0}
    assert b["top_disagreements"] == [{"recorded": "m3", "simulated": "m1", "count": 1}]
    # Outcome agreement still covers every simulated row.
    assert b["outcome_rows_compared"] == 4 and b["outcome_agreement"] == 1.0
    report = render_report(sim)
    assert ("Model agreement EXCLUDES 2 breaker-diverted row(s) (app-a 1 · app-b 1)"
            in report)
    assert "matches the record on 2 of them" in report


def test_an_app_whose_rows_are_all_diverted_has_no_model_agreement():
    rows = [_row("d1", attempts=["m2"], chosen="m2", rejected=_open("m1"))]
    _results, sim = _summary(rows, _mids())
    assert (sim["baseline"]["rows_compared"], sim["baseline"]["agreement"]) == (0, None)
    assert sim["baseline"]["by_app"] == {}


def test_no_diverted_rows_prints_no_breaker_sentence():
    _results, sim = _summary([_row(attempts=["m1"], chosen="m1")], _mids())
    assert sim["baseline"]["breaker_diverted"] == 0
    assert "breaker-diverted" not in render_report(sim)


def test_a_proposal_still_routes_around_a_recorded_open_circuit():
    """m1 open on this row. The proposal adds a cheaper mid endpoint the row
    says nothing about: it is assumed closed and wins."""
    add = {"slug": "m0", "provider": "onprem", "tier": "mid",
           "price": {"input": 0.01, "output": 0.02, "cached_input": None},
           "region": "onprem", "prompt_retention": "none", "accepts_raw_sensitive": True,
           "context_window": None, "quality": None, "allowed_app_ids": None}
    (r,), _ = _run([_row(attempts=["m2"], chosen="m2", rejected=_open("m1"))], _mids(),
                   {"schema": P, "endpoints": {"add": [add]}})
    assert (r.current.model, r.proposed.model) == ("m2", "m0")


def test_a_no_op_proposal_changes_no_route_on_recorded_state():
    rows = [_row("d", attempts=["m2"], chosen="m2", rejected=_open("m1")),
            _row("f", attempts=["m1", "m2"], chosen="m2", backend_errors=[_err("m1")])]
    _results, sim = _summary(rows, _mids())
    assert sim["totals"]["route_changed"] == 0
    assert sim["totals"]["cost_usd"]["delta"] == 0.0


# --- B71 item 3: recorded failed attempts ---------------------------------------

def test_without_the_record_replay_assumes_the_failed_endpoint_answers():
    (r,), _ = _run([_row(attempts=["m1", "m2"], chosen="m2")], _mids())
    assert r.current.model == "m1"        # pre-B117 `ok` row: nothing inferred


def test_a_recorded_failure_fails_over_as_live_and_costs_the_server():
    row = _row(attempts=["m1", "m2"], chosen="m2", backend_errors=[_err("m1")])
    (r,), _ = _run([row], _mids())
    assert r.current.model == "m2"
    assert r.current.attempted == ("m1", "m2") and r.current.failed == ("m1",)
    assert r.current.overflow_hops == 0
    # Priced at m2 (0.2/M in, 1.0/M out); the failed call bills nothing.
    assert r.current.cost_usd == pytest.approx((900 * 0.2 + 100 * 1.0) / 1_000_000)


def test_a_reproduced_failover_still_agrees_on_the_first_decision():
    row = _row(attempts=["m1", "m2"], chosen="m2", backend_errors=[_err("m1")])
    _results, sim = _summary([row], _mids())
    assert sim["baseline"]["agreement"] == 1.0
    assert sim["baseline"]["recorded_failure_rows"] == 1
    assert "1 simulated row(s) record a failed attempt" in render_report(sim)


def test_every_pick_failing_is_the_exhausted_502_and_still_a_routed_first_decision():
    """A `backend_failed` row: both endpoints failed live. Replay reproduces
    that -- rejected `backend_failed`, nothing costed -- and outcome
    agreement counts it as the routed first decision it was."""
    row = _row(status="backend_failed", chosen=None, attempts=["m1", "m2"],
               completion_tokens=0, backend_errors=[_err("m1"), _err("m2")])
    results, sim = _summary([row], _mids())
    a = results[0].current
    assert (a.outcome, a.reject_codes) == ("rejected", (BACKEND_FAILED,))
    assert a.attempted == ("m1", "m2") and a.failed == ("m1", "m2")
    assert a.cost_usd is None and a.overflow_hops == 0 and a.first_decision_routed
    assert sim["baseline"]["outcome_agreement"] == 1.0
    assert sim["baseline"]["recorded_routed_but_rejected"] == 0


def test_a_pre_b117_backend_failed_row_replays_its_attempts_as_failed():
    row = _row(status="backend_failed", chosen=None, attempts=["m1"], completion_tokens=0)
    assert "backend_errors" not in row
    (r,), _ = _run([row], _mids())
    assert (r.current.model, r.current.failed) == ("m2", ("m1",))


def test_a_recorded_size_refusal_is_not_replayed_as_a_failure():
    """`context_overflow` stays the declared-window prediction (§6)."""
    row = _row(attempts=["m1", "m2"], chosen="m2",
               backend_errors=[_err("m1", "context_overflow")])
    (r,), _ = _run([row], _mids())
    assert r.current.model == "m1" and r.current.failed == ()


def test_failures_share_the_live_failover_bound():
    row = _row(attempts=["m1", "m2"], chosen="m2", backend_errors=[_err("m1")])
    (r,), _ = _run([row], _mids(max_failover=0))
    assert r.current.reject_codes == (BACKEND_FAILED,) and r.current.attempted == ("m1",)


def test_only_the_rows_own_failures_are_honoured():
    """m1 failed on row `f`; row `g` says nothing about m1, so m1 serves it."""
    rows = [_row("f", attempts=["m1", "m2"], chosen="m2", backend_errors=[_err("m1")]),
            _row("g", attempts=["m1"], chosen="m1")]
    results, _ = _run(rows, _mids())
    assert {r.row["request_id"]: r.current.model for r in results} == {"f": "m2", "g": "m1"}


def test_a_proposal_that_removes_the_failed_endpoint_routes_straight_to_a_peer():
    row = _row(attempts=["m1", "m2"], chosen="m2", backend_errors=[_err("m1")])
    (r,), _ = _run([row], _mids(), {"schema": P, "endpoints": {"remove": ["m1"]}})
    assert r.proposed.attempted == ("m2",) and r.proposed.failed == ()
    assert r.current.model == r.proposed.model == "m2"


def test_a_proposal_that_leaves_only_a_failed_endpoint_newly_rejects_backend_failed():
    """The row records m2 failing. Live m1 served; the proposal removes m1, so
    the proposed arm's only pick fails as recorded."""
    row = _row(attempts=["m1"], chosen="m1", backend_errors=[_err("m2")])
    results, sim = _summary([row], _mids(), {"schema": P, "endpoints": {"remove": ["m1"]}})
    assert results[0].proposed.reject_codes == (BACKEND_FAILED,)
    assert sim["totals"]["newly_rejected"] == 1
    assert sim["new_rejections"]["by_code"] == {BACKEND_FAILED: 1}
    assert sim["new_rejections"]["by_core"] == {"(backend_failed)": 1}


# --- B71 item 3: recorded cost beside replayed cost ------------------------------

def test_recorded_cost_is_reported_beside_the_replayed_cost_overall_and_per_app():
    snap = _mids(apps=("app-a", "app-b"))
    rows = [_row("a1", "app-a", attempts=["m1"], chosen="m1", cost_usd=0.5),
            _row("b1", "app-b", attempts=["m1"], chosen="m1", cost_usd=0.25),
            _row("b2", "app-b", status="backend_failed", chosen=None, attempts=["m1", "m2"],
                 completion_tokens=0, cost_usd=0.0,
                 backend_errors=[_err("m1"), _err("m2")])]
    results, sim = _summary(rows, snap)
    per_row = (900 * 0.1 + 100 * 1.0) / 1_000_000      # m1: 0.1/M in, 1.0/M out
    b = sim["baseline"]
    assert b["cost_usd"]["recorded"] == pytest.approx(0.75)
    assert b["cost_usd"]["replayed"] == pytest.approx(2 * per_row)
    assert b["cost_usd"]["replayed"] == pytest.approx(sim["totals"]["cost_usd"]["current"])
    assert b["cost_by_app"]["app-a"] == {"recorded": 0.5, "replayed": pytest.approx(per_row)}
    assert b["cost_by_app"]["app-b"]["recorded"] == 0.25
    report = render_report(sim)
    assert "Cost of the simulated rows: recorded $0.750000, replayed (current arm) $" in report
    assert "app-a $0.500000 / $" in report and "app-b $0.250000 / $" in report


def test_schema_is_unchanged_and_the_caveat_names_the_recorded_state():
    _results, sim = _summary([_row()], _mids())
    assert sim["schema"] == "routeforge.simulation/v1"
    assert "recorded open" in REPORT_CAVEAT and "recorded failed attempts" in REPORT_CAVEAT
