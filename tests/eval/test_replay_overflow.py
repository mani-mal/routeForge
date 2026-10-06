"""P3c-2 Task 6: context-window overflow re-selection in replay (spec §5, S7)."""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.eval.simulate import REPORT_CAVEAT, render_report, simulate, summarize
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"
DEFAULT_OUTPUT = int(BASE_SETTINGS.policy.budgets["default_estimated_output_tokens"])   # 512

_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": "t", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}


def _run(records, snap, prop=None):
    return asyncio.run(simulate(records, snap, prop or {"schema": P}, base=BASE_SETTINGS))


def _mids(*windows, max_failover=None, extra=()):
    """Mid-tier endpoints `m1..mN`, cheapest first, so the router prefers them
    in that order; `windows[i]` is `m{i+1}`'s declared context window."""
    rows = [onboarded_row(f"m{i + 1}", "mid", price_input=0.1 * (i + 1), context_window=w)
            for i, w in enumerate(windows)]
    snap = make_snapshot(onboarded=rows + list(extra),
                         apps=[app_entry("app-a", max_cost_usd_per_request=100.0)])
    if max_failover is not None:
        snap["settings"]["routing"]["max_failover_attempts"] = max_failover
    return snap


def _row(request_id="r", prompt_tokens=900, **over):
    return make_row(request_id, "app-a", complexity=0.6, prompt_tokens=prompt_tokens, **over)


def test_fixture_order_is_the_routers_preference():
    (r,), _ = _run([_row(prompt_tokens=10)], _mids(None, None, None))
    assert r.current.model == "m1" and r.current.attempted == ("m1",)
    assert r.current.overflow_hops == 0


def test_overflowing_pick_is_excluded_and_the_next_one_serves():
    """900 prompt + 512 output > m1's 1000-token window: m1 is excluded, m2
    serves, and m2 is the endpoint costed."""
    (r,), _ = _run([_row()], _mids(1000, None))
    assert r.current.model == "m2"
    assert r.current.attempted == ("m1", "m2") and r.current.overflow_hops == 1
    assert r.current.cost_usd == pytest.approx((900 * 0.2 + 100 * 1.0) / 1_000_000)


@pytest.mark.parametrize("prompt_tokens,hops", [(1000 - DEFAULT_OUTPUT, 0),
                                                (1000 - DEFAULT_OUTPUT + 1, 1)])
def test_boundary_prompt_plus_output_equal_to_window_fits(prompt_tokens, hops):  # Review Focus 4
    (r,), _ = _run([_row(prompt_tokens=prompt_tokens)], _mids(1000, None))
    assert r.current.overflow_hops == hops
    assert r.current.model == ("m1" if hops == 0 else "m2")


def test_the_clients_recorded_max_tokens_is_the_output_counted():
    snap = _mids(1000, None)
    (small,), _ = _run([_row(max_tokens=64)], snap)            # 900 + 64 fits
    (large,), _ = _run([_row(max_tokens=101)], snap)           # 900 + 101 does not
    assert (small.current.model, large.current.model) == ("m1", "m2")


def test_s7_undeclared_window_never_overflows():                           # S7
    (r,), _ = _run([_row(prompt_tokens=5_000_000)], _mids(None, None))
    assert r.current.outcome == "routed" and r.current.model == "m1"
    assert r.current.overflow_hops == 0


def test_s7_all_overflow_rejects_context_window():                         # S7
    """Both reachable endpoints overflow: the third select finds nothing left
    and the row is rejected `context_window` (live: 400), never `no_eligible`."""
    (r,), _ = _run([_row()], _mids(1000, 1200))
    assert (r.current.outcome, r.current.reject_codes) == ("rejected", ("context_window",))
    assert r.current.attempted == ("m1", "m2") and r.current.overflow_hops == 2
    assert (r.current.model, r.current.cost_usd, r.current.core) == (None, None, ())
    assert r.current.eligible == ("m1", "m2")       # policy still admits both


def test_bound_is_max_failover_attempts_exactly_as_live():                # Review Focus 4
    """Three overflowing endpoints ahead of one that fits. With the shipped
    bound (2 extra selects) the row is rejected although `m4` would fit --
    which is what the live gateway does. Raising the bound to 3 reaches it;
    a bound of 0 makes the first overflow terminal."""
    windows = (1000, 1000, 1000, None)
    (default,), _ = _run([_row()], _mids(*windows))
    assert BASE_SETTINGS.routing.max_failover_attempts == 2
    assert default.current.reject_codes == ("context_window",)
    assert default.current.attempted == ("m1", "m2", "m3")
    (raised,), _ = _run([_row()], _mids(*windows, max_failover=3))
    assert raised.current.model == "m4" and raised.current.overflow_hops == 3
    (zero,), _ = _run([_row()], _mids(1000, None, max_failover=0))
    assert zero.current.reject_codes == ("context_window",)
    assert zero.current.attempted == ("m1",)


def test_overflow_can_leave_the_tier_when_it_empties():
    """The only mid endpoint overflows; excluding it leaves the frontier tier."""
    snap = _mids(1000, extra=[onboarded_row("big", "frontier", context_window=None)])
    (r,), _ = _run([_row()], snap)
    assert (r.current.model, r.current.tier, r.current.overflow_hops) == ("big", "frontier", 1)


def test_a_proposed_context_window_change_now_moves_the_route():
    """Reference doc §6 used to say this had no visible effect."""
    results, excluded = _run([_row("a"), _row("b")], _mids(1000, None), {"schema": P,
        "endpoints": {"update": {"m1": {"context_window": 8000}}}})
    for r in results:
        assert (r.current.model, r.proposed.model) == ("m2", "m1")
        assert (r.current.overflow_hops, r.proposed.overflow_hops) == (1, 0)
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["route_changed"] == 2
    assert sim["totals"]["overflow_hops"] == {"current": 2, "proposed": 0}
    assert "| overflow_hops | 2 | 0 | -2 |" in render_report(sim)


def test_token_scale_is_applied_before_the_window_check():
    """At 0.5x the 900-token prompt is 450: 450 + 512 fits m1's window."""
    (r,), _ = _run([_row()], _mids(1000, None),
                   {"schema": P, "workload": {"token_scale": 0.5}})
    assert (r.current.model, r.proposed.model) == ("m2", "m1")


def test_shrinking_a_window_newly_rejects_and_is_keyed_context_window():
    results, excluded = _run([_row()], _mids(None), {"schema": P,
        "endpoints": {"update": {"m1": {"context_window": 1000}}}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_rejected"] == 1
    assert sim["new_rejections"]["by_code"] == {"context_window": 1}
    assert sim["new_rejections"]["by_core"] == {"(context_window)": 1}
    assert sim["totals"]["overflow_hops"] == {"current": 0, "proposed": 1}


def test_baseline_compares_the_first_decision_not_the_served_model():     # Review Focus 4
    """The record says m1 was tried first and m2 served. Replay reproduces
    exactly that; the fidelity check must call it agreement, and a rejected
    arm must still count as a disagreement. Neither row's record proves its
    OWN first pick served it alone (the failover row names two attempts; the
    all-overflow row's one attempt was never actually overflowed live, but
    it IS excluded by replay -- see the dedicated overflow_contradicted
    tests below for that distinction)."""
    failover = _row("f", attempts=["m1", "m2"], chosen="m2")
    results, excluded = _run([failover], _mids(1000, None))
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert results[0].current.model == "m2"
    assert sim["baseline"]["agreement"] == 1.0 and sim["baseline"]["top_disagreements"] == []
    assert sim["baseline"]["overflow_contradicted"] == 0
    served_live = _row("s", attempts=["m1"], chosen="m1")
    results, excluded = _run([served_live], _mids(1000, 1200))     # replay: all overflow
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["baseline"]["agreement"] == 0.0
    assert sim["baseline"]["top_disagreements"] == [
        {"recorded": "m1", "simulated": "(rejected)", "count": 1}]
    # The record's one attempt IS "m1" chosen, so this row also qualifies as
    # overflow_contradicted -- it is already a disagreement either way.
    assert sim["baseline"]["overflow_contradicted"] == 1


def test_overflow_contradicted_counts_a_served_pick_replay_excludes():     # F1 final review
    """4 `ok` rows the record says m1 ALONE served (one recorded attempt,
    chosen == attempts[0]). m1's declared window is 1000 and the row is 900
    prompt tokens, so replay's `current` arm excludes m1 as an overflow and
    serves m2 instead. The first pick still matches the record ("m1"), so
    before this fix `_baseline` read perfect agreement with no warning --
    even though replay's own overflow prediction contradicts what the
    record proves happened."""
    rows = [_row(f"r{i}", attempts=["m1"], chosen="m1") for i in range(4)]
    results, excluded = _run(rows, _mids(1000, None))
    for r in results:
        assert r.current.model == "m2" and r.current.attempted == ("m1", "m2")
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["baseline"]["agreement"] < 1.0
    assert sim["baseline"]["overflow_contradicted"] == 4


def test_reproduced_failover_is_not_overflow_contradicted():              # F1 final review
    """The already-covered reproduced-failover case (record: m1 tried, m2
    chosen) must keep agreeing, and the new field must read 0 for it --
    only a row whose record proves a SINGLE pick served it alone can be
    contradicted."""
    failover = _row("f", attempts=["m1", "m2"], chosen="m2")
    results, excluded = _run([failover], _mids(1000, None))
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["baseline"]["agreement"] == 1.0
    assert sim["baseline"]["overflow_contradicted"] == 0


def test_caveat_names_the_overflow_re_selection():
    assert "plus deterministic context-window overflow" in REPORT_CAVEAT
    assert "first routing" in REPORT_CAVEAT


_WINDOW = st.sampled_from([None, 600, 1000, 4000])


@hsettings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(windows=st.lists(_WINDOW, min_size=1, max_size=4),
       prompt_tokens=st.integers(1, 6000),
       max_tokens=st.sampled_from([None, 64, 2000]),
       max_failover=st.integers(0, 3))
def test_s7_overflowing_pick_is_never_served(windows, prompt_tokens, max_tokens,  # S7
                                             max_failover):
    snap = _mids(*windows, max_failover=max_failover)
    declared = {f"m{i + 1}": w for i, w in enumerate(windows)}
    needed = prompt_tokens + (max_tokens or DEFAULT_OUTPUT)
    (r,), _ = _run([_row(prompt_tokens=prompt_tokens, max_tokens=max_tokens)], snap)
    arm = r.current

    def overflows(name):
        return declared[name] is not None and needed > declared[name]

    assert arm.overflow_hops <= max_failover + 1
    if arm.outcome == "routed":
        assert not overflows(arm.model)                      # the served pick always fits
        assert arm.attempted[-1] == arm.model
        assert all(overflows(name) for name in arm.attempted[:-1])
        assert arm.overflow_hops == len(arm.attempted) - 1 <= max_failover
    else:
        assert arm.reject_codes == ("context_window",)
        assert arm.attempted and all(overflows(name) for name in arm.attempted)
    assert len(set(arm.attempted)) == len(arm.attempted)     # never re-selects an excluded pick
    assert r.proposed == arm                                 # empty proposal: both arms agree
