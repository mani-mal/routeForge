"""B60: replay sizes a row from the token count the router was given.

A rejected outcome row records `prompt_tokens: 0` (no backend billed it).
Before `routing_tokens` existed the simulator replayed such a row as an
EMPTY prompt, which fits any per-request budget and any context window, so
its `current` arm ROUTED a request the record says was rejected -- and a
proposal that raised the budget showed nothing to rescue.

Invariants pinned here (spec §9): R2, R3, R4, R6, R9, R11, R12.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

import app.eval.simulate as simulate_mod
from app.eval.outcomes import load_export
from app.eval.simulate import (EXCLUSION_REASONS, RECORDED_OUTCOME, SIMULATED_STATUSES,
                               agreement_ok, baseline_check, gate_agreement, replay_tokens,
                               select_rows, simulate, summarize)
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"
_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": None, "routing_policy_version": "r", "registry_version": "v",
           "snapshot_generated_at": "2026-09-24T00:00:00Z"}

# The default fixture pool, USD per 1M tokens (input / output):
#   local-cheap 0.1 / 0.2 · local-mid 0.5 / 1.0 · local-big 2.0 / 6.0
# A 3000-token prompt with a 16-token output cap is estimated at
#   cheap 0.0003032 · mid 0.001516 · frontier 0.006096
# so a 0.0002 cap rejects it on every tier. The SAME request with a zero-token
# prompt is estimated at 0.000096 on the frontier tier -- under the cap. That
# is B60: without its size, the row routes.
CAP = 0.0002
FRONTIER_ESTIMATE = (3000 * 2.0 + 16 * 6.0) / 1_000_000


def _snap(cap=CAP, **kw):
    return make_snapshot(apps=[app_entry("app-a", max_cost_usd_per_request=cap)], **kw)


def _rejected_row(request_id="rej", **over):
    """A row as serving writes it for a request rejected at the per-request
    budget: no backend usage, no attempts, the router's token count."""
    row = dict(status="no_eligible", complexity=1.0, prompt_tokens=0, completion_tokens=0,
               routing_tokens=3000, max_tokens=16, attempts=[], chosen=None, latency_ms=3.0)
    row.update(over)
    return make_row(request_id, "app-a", **row)


def _without_size(row):
    """The same row as an export from before the column existed carries it."""
    return {k: v for k, v in row.items() if k != "routing_tokens"}


def _run(records, snap, prop=None, **kw):
    return asyncio.run(simulate(records, snap, prop or {"schema": P}, base=BASE_SETTINGS, **kw))


def _raise_cap(cap=1.0):
    return {"schema": P, "apps": {"app-a": {"max_cost_usd_per_request": cap}}}


# --- R3 / R4: the rejection is reproduced, and a higher cap rescues it --------

def test_r3_budget_rejected_row_is_rejected_in_the_current_arm():          # R3
    (r,), excluded = _run([_rejected_row()], _snap())
    assert r.current.outcome == "rejected"
    assert r.current.reject_codes == ("over_budget",)
    assert r.current.core == ("max_cost_usd_per_request",)
    assert r.current.cost_usd is None
    assert excluded["missing_routing_tokens"] == 0


def test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate():   # R4
    results, excluded = _run([_rejected_row()], _snap(), _raise_cap())
    (r,) = results
    assert (r.current.outcome, r.proposed.outcome) == ("rejected", "routed")
    assert r.proposed.model == "local-big"                    # complexity 1.0 -> frontier
    # Priced at the SAME pair the live budget check priced it at: the routing
    # tokens and the client's own output cap. Never at the prompt alone.
    assert r.proposed.cost_usd == pytest.approx(FRONTIER_ESTIMATE)
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_served"] == 1
    assert sim["totals"]["newly_rejected"] == 0
    assert sim["affected_apps"] == ["app-a"]
    assert sim["rows"]["estimated_tokens"] == 1


def test_a_cap_still_below_the_estimate_serves_nothing():
    """The cap that matters is the cheapest tier's estimate (the budget
    fallback steps down to it): just under it still rejects, just over
    serves -- on the cheap tier."""
    cheap = (3000 * 0.1 + 16 * 0.2) / 1_000_000
    (under,), _ = _run([_rejected_row()], _snap(), _raise_cap(cheap - 1e-9))
    (over,), _ = _run([_rejected_row()], _snap(), _raise_cap(cheap + 1e-9))
    assert under.proposed.outcome == "rejected"
    assert (over.proposed.outcome, over.proposed.model) == ("routed", "local-cheap")
    assert over.proposed.cost_usd == pytest.approx(cheap)


def test_no_recorded_cap_prices_the_rescued_row_at_the_default_output_estimate():
    default_out = int(BASE_SETTINGS.policy.budgets["default_estimated_output_tokens"])
    (r,), _ = _run([_rejected_row(max_tokens=None)], _snap(cap=1e-9), _raise_cap(100.0))
    assert r.current.outcome == "rejected"
    assert r.proposed.cost_usd == pytest.approx((3000 * 2.0 + default_out * 6.0) / 1_000_000)


def test_token_scale_reaches_the_budget_check_of_a_rejected_row():
    """Halving the prompt (a stricter redaction, Headroom on) brings the
    cheap tier's estimate under the cap: 1500 tokens -> 0.0001532."""
    (r,), _ = _run([_rejected_row()], _snap(),
                   {"schema": P, "workload": {"token_scale": 0.5}})
    assert (r.current.outcome, r.proposed.outcome) == ("rejected", "routed")
    assert r.proposed.model == "local-cheap"
    assert r.proposed.cost_usd == pytest.approx((1500 * 0.1 + 16 * 0.2) / 1_000_000)


def test_context_overflow_row_is_reproduced_when_the_window_is_declared():
    """A recorded `context_overflow` row replays at its routing tokens, so a
    DECLARED window it exceeds rejects it; an undeclared one cannot."""
    row = _rejected_row("ovf", status="context_overflow", attempts=["m"], complexity=1.0)
    declared = make_snapshot(onboarded=[onboarded_row("m", "frontier", context_window=2048)])
    undeclared = make_snapshot(onboarded=[onboarded_row("m", "frontier")])
    (a,), _ = _run([row], declared)
    (b,), _ = _run([row], undeclared)
    assert (a.current.outcome, a.current.reject_codes) == ("rejected", ("context_window",))
    assert b.current.outcome == "routed"


# --- the two counts: routed on one, priced and sized on the other -------------

def test_replay_tokens():
    served = make_row("s", "app-a", prompt_tokens=10, routing_tokens=3000)
    assert replay_tokens(served) == (3000, 10, False)          # route / bill / not estimated
    assert replay_tokens(_without_size(served)) == (10, 10, False)        # as before B60
    assert replay_tokens(_rejected_row()) == (3000, 3000, True)
    assert replay_tokens(make_row("z", "app-a", prompt_tokens=0)) == (0, 0, False)
    assert replay_tokens(make_row("n", "app-a", routing_tokens=None)) == (200, 200, False)


def test_served_row_is_routed_on_routing_tokens_and_priced_on_the_backend_count():
    """The backend billed 10 tokens; the router was given 3000. Under a cap
    of 0.002 the frontier estimate (0.006096) is over budget and the mid one
    (0.001516) fits, so the router falls back to mid -- as it did live. The
    10-token count alone would have stayed on frontier. The cost is the bill:
    10 prompt + 100 completion tokens at mid prices."""
    row = make_row("s", "app-a", complexity=1.0, prompt_tokens=10, completion_tokens=100,
                   routing_tokens=3000, max_tokens=16,
                   attempts=["local-mid"], chosen="local-mid")
    (new,), _ = _run([row], _snap(cap=0.002))
    (old,), _ = _run([_without_size(row)], _snap(cap=0.002))
    assert new.current.model == "local-mid"
    assert new.current.cost_usd == pytest.approx((10 * 0.5 + 100 * 1.0) / 1_000_000)
    assert old.current.model == "local-big"                    # what replay did before


def test_window_check_uses_the_backend_count_for_a_served_row():
    """Live, overflow is the backend's refusal on its own tokenisation, so a
    served row's window check stays on `prompt_tokens`: 900 + 16 fits a
    1000-token window although the routing count (3000) would not."""
    snap = make_snapshot(onboarded=[onboarded_row("m", "frontier", context_window=1000)])
    row = make_row("s", "app-a", complexity=1.0, prompt_tokens=900, routing_tokens=3000,
                   max_tokens=16, attempts=["m"], chosen="m")
    (r,), _ = _run([row], snap)
    assert (r.current.outcome, r.current.overflow_hops) == ("routed", 0)


# --- R2: a served row is unchanged when the two counts coincide ---------------

@hsettings(max_examples=60, deadline=None,
           suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(tokens=st.integers(min_value=1, max_value=20_000),
       complexity=st.sampled_from([0.0, 0.05, 0.3, 0.6, 0.99, 1.0]),
       max_tokens=st.sampled_from([None, 16, 4096]),
       scale=st.sampled_from([0.1, 0.5, 1.0, 2.0]),
       cap=st.sampled_from([0.0002, 0.002, 100.0]),
       status=st.sampled_from(["ok", "verify_failed"]))
def test_r2_served_row_replay_is_unchanged_when_the_counts_coincide(      # R2
        tokens, complexity, max_tokens, scale, cap, status):
    """`routing_tokens == prompt_tokens` must reproduce, field for field,
    the `ArmResult` the row produced before the column existed -- in both
    arms, under any `token_scale`."""
    legacy = make_row("r", "app-a", status=status, complexity=complexity,
                      prompt_tokens=tokens, max_tokens=max_tokens)
    recorded = {**legacy, "routing_tokens": tokens}
    prop = {"schema": P, "workload": {"token_scale": scale}}
    (a,), ex_a = _run([legacy], _snap(cap=cap), prop)
    (b,), ex_b = _run([recorded], _snap(cap=cap), prop)
    assert a.current == b.current and a.proposed == b.proposed
    assert ex_a == ex_b


# --- R6: a row with no size is excluded and counted, never routed -------------

@pytest.mark.parametrize("status", ["no_eligible", "context_overflow", "backend_failed",
                                    "verify_failed"])
def test_r6_unsized_rows_are_excluded_and_counted_never_routed(status, monkeypatch):  # R6
    simulated: list[str] = []
    real = simulate_mod.simulate_row

    def spy(scn, row, observed):
        simulated.append(row["request_id"])
        return real(scn, row, observed)

    monkeypatch.setattr(simulate_mod, "simulate_row", spy)
    unsized = _without_size(_rejected_row("old", status=status))
    results, excluded = _run([unsized, make_row("ok1", "app-a")], _snap(), _raise_cap())
    assert [r.row["request_id"] for r in results] == ["ok1"]
    assert excluded["missing_routing_tokens"] == 1
    assert "old" not in simulated                              # it reaches no arm at all
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_served"] == 0
    assert sim["rows"]["excluded"]["missing_routing_tokens"] == 1


def test_missing_routing_tokens_is_the_last_exclusion_reason():
    """First match wins, in `EXCLUSION_REASONS` order: an unsized row that is
    ALSO before `--since`, of an unknown app or malformed is counted there."""
    assert EXCLUSION_REASONS[-1] == "missing_routing_tokens"
    unsized = _without_size(_rejected_row("u"))
    rows, excluded = select_rows(
        [unsized, {**unsized, "request_id": "ghost", "application_id": "ghost"},
         {**unsized, "request_id": "bad", "complexity": None}], {"app-a"})
    assert rows == []
    assert (excluded["missing_routing_tokens"], excluded["unknown_app"],
            excluded["missing_features"]) == (1, 1, 1)


def test_rows_that_still_have_a_size_are_kept():
    """Not every row without `routing_tokens` is unsized: a served row, and a
    `verify_failed` row carrying its served attempt's backend count, replay
    exactly as before. A null value is the same as an absent key."""
    kept = [make_row("ok-old", "app-a"),
            make_row("ok-zero", "app-a", prompt_tokens=0),
            make_row("vf-old", "app-a", status="verify_failed", prompt_tokens=1500),
            make_row("ok-null", "app-a", routing_tokens=None),
            _rejected_row("rej-new"),
            _rejected_row("rej-empty", routing_tokens=0)]      # recorded, and really 0
    rows, excluded = select_rows(kept, {"app-a"})
    assert {r["request_id"] for r in rows} == {r["request_id"] for r in kept}
    assert excluded["missing_routing_tokens"] == 0
    null_rejected = _rejected_row("rej-null", routing_tokens=None)
    assert select_rows([null_rejected], {"app-a"})[1]["missing_routing_tokens"] == 1


@pytest.mark.parametrize("bad", ["3000", 3000.0, True, -1, [3000], {"n": 1}])
def test_mistyped_routing_tokens_is_missing_features(bad):
    """A present-but-mistyped count is a malformed row like any other --
    counted, never a crash, and never guessed at."""
    for row in (_rejected_row(routing_tokens=bad), make_row("s", "app-a", routing_tokens=bad)):
        rows, excluded = select_rows([row], {"app-a"})
        assert rows == []
        assert (excluded["missing_features"], excluded["missing_routing_tokens"]) == (1, 0)


# --- R9: an export from before the field existed still loads and replays ------

def test_r9_export_without_the_field_still_loads_and_replays_served_rows(tmp_path):  # R9
    served = make_row("s1", "app-a", prompt_tokens=200)
    old_rejected = _without_size(_rejected_row("r1"))
    new_rejected = _rejected_row("r2")
    path = tmp_path / "old.ndjson"
    with path.open("w") as f:
        for row in (served, old_rejected, new_rejected):
            outcome = {k: v for k, v in row.items() if k != "feedback"}
            f.write(json.dumps({"schema": "routeforge.outcome/v1", "outcome": outcome,
                                "feedback": []}) + "\n")
    records, warnings = load_export(str(path))
    assert warnings == [] and len(records) == 3
    assert "routing_tokens" not in records[0] and records[2]["routing_tokens"] == 3000

    results, excluded = _run(records, _snap())
    by = {r.row["request_id"]: r for r in results}
    assert set(by) == {"s1", "r2"} and excluded["missing_routing_tokens"] == 1
    # The served row replays exactly as a row that says its routing count is
    # its backend count -- i.e. exactly as it did before the field existed.
    (same,), _ = _run([{**served, "routing_tokens": 200}], _snap())
    assert by["s1"].current == same.current
    assert by["r2"].current.outcome == "rejected"


# --- shadow prices gain the fix through the same row path ---------------------

def test_shadow_price_of_the_budget_now_shows_the_rejections_it_would_avoid():
    from app.eval.shadow import shadow_prices
    from tests.eval.test_shadow import _entry

    rows = [_rejected_row(f"rej{i}", ts=f"2026-09-20T12:00:0{i}+00:00") for i in range(3)]
    rows.append(make_row("ok1", "app-a"))
    block = asyncio.run(shadow_prices(rows, _snap(), base=BASE_SETTINGS))
    entry = _entry(block, "max_cost_usd_per_request", app="app-a")
    assert entry is not None and entry["rejections_avoided"] == 3
    legacy = [_without_size(r) for r in rows[:3]] + rows[3:]
    assert _entry(asyncio.run(shadow_prices(legacy, _snap(), base=BASE_SETTINGS)),
                  "max_cost_usd_per_request", app="app-a") is None


# --- outcome-level agreement (spec §7) ----------------------------------------

def _baseline_of(records, snap, **kw):
    results, excluded = _run(records, snap, **kw)
    return summarize(results, excluded, inputs=_INPUTS, edits={})["baseline"]


def test_r11_every_status_has_a_recorded_outcome_class():                  # R11
    """The class table covers exactly the statuses serving can write. A
    sixth status fails here until someone decides what its first routing
    decision was."""
    from app.apps.outcomes import STATUSES
    assert set(RECORDED_OUTCOME) == set(STATUSES) == set(SIMULATED_STATUSES)
    assert set(RECORDED_OUTCOME.values()) == {"routed", "rejected"}
    assert {s for s, c in RECORDED_OUTCOME.items() if c == "rejected"} == \
        {"no_eligible", "context_overflow"}


def test_outcome_agreement_is_perfect_when_rejections_are_reproduced():
    rows = [make_row(f"ok{i}", "app-a") for i in range(3)] + \
           [_rejected_row(f"rej{i}") for i in range(2)]
    b = _baseline_of(rows, _snap())
    assert (b["agreement"], b["rows_compared"]) == (1.0, 3)        # model: `ok` rows only
    assert (b["outcome_agreement"], b["outcome_rows_compared"]) == (1.0, 5)
    assert b["outcome_warning"] is False
    assert (b["recorded_rejected"], b["recorded_rejected_but_routed"],
            b["recorded_routed_but_rejected"]) == (2, 0, 0)
    assert b["outcome_by_app"] == {"app-a": {
        "rows": 5, "agreement": 1.0, "recorded_rejected": 2,
        "recorded_rejected_but_routed": 0, "recorded_routed_but_rejected": 0}}


def test_a_recorded_rejection_that_replay_routes_is_counted():
    """The B60 signature, made visible: the snapshot's cap is ABOVE the
    estimate (it was raised after the traffic was recorded), so replay
    routes rows the record says were rejected. Model agreement cannot see
    it; outcome agreement and its count can."""
    rows = [make_row(f"ok{i}", "app-a") for i in range(3)] + \
           [_rejected_row(f"rej{i}") for i in range(2)]
    b = _baseline_of(rows, _snap(cap=100.0))
    assert b["agreement"] == 1.0 and b["warning"] is False         # blind to it
    assert b["outcome_agreement"] == pytest.approx(3 / 5)
    assert b["outcome_warning"] is True
    assert (b["recorded_rejected"], b["recorded_rejected_but_routed"],
            b["recorded_routed_but_rejected"]) == (2, 2, 0)


def test_a_recorded_route_that_replay_rejects_is_counted_per_app():
    """The reverse direction, and the per-app split: app-b's served rows are
    over ITS cap in replay; app-a is untouched."""
    snap = make_snapshot(apps=[app_entry("app-a"),
                               app_entry("app-b", max_cost_usd_per_request=1e-9)])
    rows = [make_row("a1", "app-a"), make_row("b1", "app-b"),
            make_row("b2", "app-b", status="backend_failed", routing_tokens=200,
                     prompt_tokens=0)]
    b = _baseline_of(rows, snap)
    assert b["recorded_routed_but_rejected"] == 2 and b["recorded_rejected_but_routed"] == 0
    assert b["outcome_agreement"] == pytest.approx(1 / 3)
    assert b["outcome_by_app"]["app-a"]["agreement"] == 1.0
    assert b["outcome_by_app"]["app-b"] == {
        "rows": 2, "agreement": 0.0, "recorded_rejected": 0,
        "recorded_rejected_but_routed": 0, "recorded_routed_but_rejected": 2}


def test_backend_failed_and_verify_failed_rows_are_recorded_as_routed():
    """Their first decision was a route: replay routing them is agreement."""
    rows = [make_row("ok1", "app-a"),
            make_row("bf", "app-a", status="backend_failed", prompt_tokens=0,
                     routing_tokens=200, chosen=None),
            make_row("vf", "app-a", status="verify_failed", routing_tokens=200)]
    b = _baseline_of(rows, make_snapshot())
    assert (b["outcome_agreement"], b["outcome_rows_compared"]) == (1.0, 3)
    assert b["recorded_rejected"] == 0


def test_outcome_agreement_is_null_with_no_simulated_row():
    b = _baseline_of([], make_snapshot())
    assert b["outcome_agreement"] is None and b["outcome_rows_compared"] == 0
    assert b["outcome_warning"] is False and b["outcome_by_app"] == {}


def test_diluted_outcome_agreement_still_reports_every_rejection_wrong():
    """Why the count is reported with its denominator: 2 of 2 rejections
    replay wrong while outcome agreement is still above the 90% threshold."""
    rows = [make_row(f"ok{i}", "app-a") for i in range(28)] + \
           [_rejected_row(f"rej{i}") for i in range(2)]
    b = _baseline_of(rows, _snap(cap=100.0))
    assert b["outcome_agreement"] == pytest.approx(28 / 30) and b["outcome_warning"] is False
    assert (b["recorded_rejected_but_routed"], b["recorded_rejected"]) == (2, 2)
    assert agreement_ok(b, 0.9) is True and agreement_ok(b, 1.0) is False


@pytest.mark.parametrize("agreement,outcome,gate", [
    (1.0, 0.72, 0.72), (0.5, 1.0, 0.5), (1.0, 1.0, 1.0),
    (None, 1.0, None), (1.0, None, None), (None, None, None),
])
def test_r12_gate_takes_the_lower_of_the_two(agreement, outcome, gate):    # R12
    baseline = {"agreement": agreement, "outcome_agreement": outcome}
    assert gate_agreement(baseline) == gate
    assert agreement_ok(baseline, 0.9) is (gate is not None and gate >= 0.9)
    assert agreement_ok(baseline, 0.0) is (gate is not None)   # n/a never passes


def test_baseline_check_carries_the_outcome_fields():
    """`--shadow-prices` without `--proposal` gates through `baseline_check`:
    it must return the same block, outcome fields included."""
    rows = [make_row("ok1", "app-a"), _rejected_row("rej1")]
    snap = _snap(cap=100.0)
    assert asyncio.run(baseline_check(rows, snap, base=BASE_SETTINGS)) == \
        _baseline_of(rows, snap)
