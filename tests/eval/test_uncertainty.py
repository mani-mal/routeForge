"""P3c-3: uncertainty ranges (spec §4-§9, invariants U2-U7, U11).

The engine is tested on hand-built `RowResult`s (fast, every figure
controllable) and against a real replay (`simulate`), so its figure
definitions cannot drift from `simulate._bucket_stats`.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections import Counter

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.eval.outcomes import _nearest_rank
from app.eval.simulate import ArmResult, RowResult, _bucket_stats, simulate
from app.eval.uncertainty import (MAX_REPLICATES, METHOD, NOT_COVERED, UncertaintyPlan,
                                  _figures, _flatten, _strata, _weighted_rank,
                                  draw_indices, interval_of, leaf, plan_block, plan_inputs,
                                  results_block, run_bootstrap, window_days, zero_bound)
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot

P = "routeforge.policy-proposal/v1"
REJECTED = ArmResult("rejected", None, None, None, (), ("over_budget",),
                     None, None, None, None, None)


def _arm(model, cost, latency=800.0, quality=0.75, provider="onprem") -> ArmResult:
    return ArmResult("routed", model, provider, "mid", (model,), (), cost, latency,
                     "observed", quality, "tier_default")


def _result(i: int, app: str, current: ArmResult, proposed: ArmResult) -> RowResult:
    return RowResult({"request_id": f"r{i}", "application_id": app, "workload": None},
                     current, proposed)


def _synthetic(n: int = 240, seed: int = 7) -> list[RowResult]:
    """Three apps. `app-0` is untouched by the proposal; in the other two
    about 30% of rows move to a cheaper, faster, lower-quality model and
    about 4% are newly rejected. Costs differ row by row."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        app = f"app-{i % 3}"
        cost = rng.uniform(0.001, 0.02)
        current = _arm("local-mid", cost)
        roll = rng.random()
        if app == "app-0" or roll >= 0.34:
            proposed = current
        elif roll < 0.30:
            proposed = _arm("local-cheap", cost * 0.2, latency=300.0, quality=0.5)
        else:
            proposed = REJECTED
        out.append(_result(i, app, current, proposed))
    return out


def _leaves(node, path=""):
    """Every `{"point", "lo", "hi"}` leaf under `node`, with its dotted path."""
    if isinstance(node, dict):
        if "point" in node and "lo" in node:
            yield path, node
        else:
            for key, inner in node.items():
                yield from _leaves(inner, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, inner in enumerate(node):
            yield from _leaves(inner, f"{path}[{i}]")


def _width(leaf_: dict) -> float:
    return leaf_["hi"] - leaf_["lo"]


def _simulated(rows, snap, proposal):
    results, _excluded = asyncio.run(simulate(rows, snap, proposal, base=BASE_SETTINGS))
    return results


def _two_app_replay():
    """A real replay: two apps, three sizes, and a proposal that removes the
    mid endpoint (routes move, costs change)."""
    snap = make_snapshot(apps=[app_entry("claims"), app_entry("chat")])
    rows = [make_row(f"r{i:03d}", "claims" if i % 2 else "chat",
                     complexity=[0.05, 0.6, 1.0][i % 3],
                     prompt_tokens=[200, 1200, 3000][i % 3] + i,
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            for i in range(90)]
    return _simulated(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})


def _identity_figures(strata, classes, days):
    """`_figures` over the rows as recorded -- every row drawn exactly once:
    what the engine itself says the point estimate is."""
    counts: Counter = Counter()
    costs = [0.0, 0.0, 0.0, 0.0]
    for s in strata:
        counts.update(s.cls)
        for j, i in enumerate(s.col_of):
            costs[j] += sum(s.cols[i])
    return _figures(sum(s.n for s in strata), counts, costs, classes, days)


# --- the engine agrees with the figures the documents already carry -------------

@pytest.mark.parametrize("build", [_synthetic, _two_app_replay])
def test_engine_figures_equal_bucket_stats(build):
    """The engine recomputes every figure from class counts and cost sums.
    Drawing each row exactly once must give `_bucket_stats`' own numbers,
    for the totals and for each app."""
    results = build()
    strata, classes = _strata(results, {})
    expected = _flatten(_bucket_stats(results), len(results), 2.0)
    got = _identity_figures(strata, classes, 2.0)
    # This one figure divides a delta ROUNDED to 6 dp (it mirrors the
    # comparison table's cell), so a last-bit difference in a float sum can
    # move it by one rounding step.
    rounded = "per_day.cost_usd_both_routed.delta"
    assert got.pop(rounded) == pytest.approx(expected.pop(rounded), abs=1e-6)
    assert got == pytest.approx(expected, abs=1e-9)
    for s in strata:
        rows = [r for r in results if r.row["application_id"] == s.app]
        assert _identity_figures([s], classes, None) == pytest.approx(
            _flatten(_bucket_stats(rows), s.n, None), abs=1e-9)


def test_equal_cost_columns_are_stored_once():
    """An app the proposal does not touch has one distinct cost column."""
    strata, _classes = _strata(_synthetic(), {})
    untouched = next(s for s in strata if s.app == "app-0")
    assert len(untouched.cols) == 1 and untouched.col_of == (0, 0, 0, 0)
    touched = next(s for s in strata if s.app == "app-1")
    assert len(touched.cols) > 1


@given(st.lists(st.tuples(st.floats(0, 5000, allow_nan=False), st.integers(1, 6)),
                min_size=0, max_size=12),
       st.sampled_from([50, 95, 1, 100]))
@hsettings(max_examples=60, deadline=None)
def test_weighted_rank_matches_nearest_rank(pairs, pct):
    weights: dict[float, int] = {}
    for value, count in pairs:
        weights[value] = weights.get(value, 0) + count
    expanded = sorted(v for v, c in weights.items() for _ in range(c))
    assert _weighted_rank(weights, pct) == _nearest_rank(expanded, pct)


# --- U2: deterministic ----------------------------------------------------------

def test_u2_same_plan_same_block_and_the_seed_changes_it():                # U2
    results = _synthetic()
    plan = UncertaintyPlan(replicates=200)
    first = json.dumps(results_block(results, plan, days=3.0), sort_keys=True)
    assert json.dumps(results_block(results, plan, days=3.0), sort_keys=True) == first
    other = json.dumps(results_block(results, UncertaintyPlan(replicates=200, seed=1),
                                     days=3.0), sort_keys=True)
    assert other != first


def test_u2_the_global_random_state_is_not_read_or_moved():                # U2
    results = _synthetic(60)
    random.seed(123)
    before = random.getstate()
    a = results_block(results, UncertaintyPlan(replicates=20))
    assert random.getstate() == before
    random.seed(999)
    assert results_block(results, UncertaintyPlan(replicates=20)) == a


# --- U3: the point and its own range --------------------------------------------

def test_u3_every_point_is_inside_its_range_or_is_flagged():                # U3
    block = results_block(_synthetic(), UncertaintyPlan(replicates=400), days=3.0)
    leaves = list(_leaves(block))
    assert len(leaves) > 40
    for path, node in leaves:
        if node["lo"] is None:
            assert "undefined_replicates" in node, path
        elif not node["lo"] <= node["point"] <= node["hi"]:
            assert node.get("point_outside") is True, path
        else:
            assert "point_outside" not in node, path
    # On this data, at 400 replicates and a 90% interval, no point is outside.
    assert block["points_outside_interval"] == 0


def test_u3_one_replicate_can_leave_the_point_outside_and_says_so():        # U3
    """A percentile interval does not promise to contain the point. With one
    replicate `lo == hi ==` that replicate's value, which is not the point;
    the leaf says so and the block counts it. Nothing is widened."""
    block = results_block(_synthetic(), UncertaintyPlan(replicates=1))
    cost = block["totals"]["cost_usd"]["delta"]
    assert cost["lo"] == cost["hi"] != cost["point"]
    assert cost["point_outside"] is True
    assert block["points_outside_interval"] == sum(
        1 for _path, node in _leaves(block) if node.get("point_outside"))
    assert block["points_outside_interval"] > 0


def test_leaf_point_equals_the_figure_the_document_carries():
    """`point` is `_bucket_stats`' own value (rounded as the document is)."""
    results = _two_app_replay()
    block = results_block(results, UncertaintyPlan(replicates=50))
    stats = _bucket_stats(results)
    totals = block["totals"]
    assert totals["n"] == stats["requests"] == 90
    for key in ("current", "proposed", "delta"):
        assert totals["cost_usd"][key]["point"] == round(stats["cost_usd"][key], 6)
        assert totals["latency_ms"]["p95"][key]["point"] == stats["latency_ms"]["p95"][key]
    assert totals["route_changed"]["point"] == stats["route_changed"]
    assert totals["route_changed_share"]["point"] == round(stats["route_changed"] / 90, 6)
    assert set(block["by_app"]) == {"chat", "claims"}


# --- U4: paired -----------------------------------------------------------------

def _delta_leaves(block):
    return [(path, node) for path, node in _leaves(block)
            if path.endswith(".delta") or path.split(".")[-1] in (
                "route_changed", "newly_rejected", "newly_served", "route_changed_share",
                "newly_rejected_share", "newly_served_share")]


def test_u4_noop_proposal_has_every_delta_range_exactly_zero():             # U4
    """Both arms are the same rows in every replicate, so a proposal that
    changes nothing has a delta of exactly 0 in every replicate -- not a
    small range around 0, which two independent draws would give."""
    snap = make_snapshot(apps=[app_entry("claims"), app_entry("chat")])
    rows = [make_row(f"r{i:03d}", "claims" if i % 2 else "chat",
                     complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=200 + 37 * i,
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            for i in range(80)]
    block = results_block(_simulated(rows, snap, {"schema": P}),
                          UncertaintyPlan(replicates=200), days=2.0)
    deltas = _delta_leaves(block)
    # 11 per bucket (5 deltas, 3 counts, 3 shares) x (totals + 2 apps), + 2 per-day.
    assert len(deltas) == 35
    for path, node in deltas:
        assert (node["point"], node["lo"], node["hi"]) == (0, 0, 0), path
    # ...while the two arms themselves do vary from replicate to replicate.
    assert _width(block["totals"]["cost_usd"]["current"]) > 0
    assert block["totals"]["cost_usd"]["current"] == block["totals"]["cost_usd"]["proposed"]


def test_u4_an_app_is_drawn_the_same_way_alone_and_inside_the_whole_export():  # U4
    """The draw depends on (seed, replicate, app, row count) only. That is
    what pairs a shadow-price relaxation scoped to one app with the main
    run, and one scenario with the next."""
    results = _synthetic()
    plan = UncertaintyPlan(replicates=100)
    whole = results_block(results, plan)
    alone = results_block([r for r in results if r.row["application_id"] == "app-1"], plan)
    assert alone["by_app"]["app-1"] == whole["by_app"]["app-1"]
    assert alone["totals"] == whole["by_app"]["app-1"]


# --- U5: ranges narrow with more rows ---------------------------------------------

def test_u5_ranges_narrow_as_the_rows_are_replicated():                    # U5
    """The same rows 1x, 4x and 16x: a sum's relative width, and a share's
    or a mean's width, shrink at each step (about 1/sqrt(k))."""
    base = _synthetic(240)
    plan = UncertaintyPlan(replicates=300)

    def measures(k: int) -> tuple[float, float, float, float]:
        rows = [_result(j * 1000 + i, r.row["application_id"], r.current, r.proposed)
                for j in range(k) for i, r in enumerate(base)]
        totals = results_block(rows, plan)["totals"]
        cost = totals["cost_usd"]["delta"]
        return (_width(cost) / abs(cost["point"]),
                _width(totals["newly_rejected_share"]),
                _width(totals["route_changed_share"]),
                _width(totals["quality_mean"]["delta"]))

    one, four, sixteen = measures(1), measures(4), measures(16)
    for a, b, c in zip(one, four, sixteen):
        assert a > b > c > 0
    # Roughly halves per 4x; a loose bound that still fails for a range that
    # does not depend on the row count at all.
    assert sixteen[0] < 0.5 * one[0]


# --- U6: stratification -----------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 7, 500])
def test_u6_every_replicate_draws_exactly_the_apps_row_count(n):           # U6
    for replicate in range(20):
        idx = draw_indices(3, replicate, "app-x", n)
        assert len(idx) == n and all(0 <= i < n for i in idx)


def test_u6_a_one_row_app_is_present_in_every_replicate():                 # U6
    """One row among 300: an unstratified resample would leave the app out
    of about a third of the replicates. Stratified, it is drawn every time,
    so none of its figures is ever undefined."""
    results = _synthetic(300) + [_result(9999, "tiny", _arm("local-mid", 0.01),
                                         _arm("local-cheap", 0.002, latency=300.0))]
    block = results_block(results, UncertaintyPlan(replicates=100, min_rows=1))
    tiny = block["by_app"]["tiny"]
    assert tiny["n"] == 1
    nodes = list(_leaves(tiny))
    assert nodes
    for path, node in nodes:
        assert "undefined_replicates" not in node, path
        assert node["lo"] == node["hi"] == node["point"], path


# --- U7: insufficient data ----------------------------------------------------------

def test_u7_a_small_app_gets_no_range_and_says_how_many_rows():            # U7
    results = _synthetic(240) + [
        _result(5000 + i, "small", _arm("local-mid", 0.01), REJECTED) for i in range(5)]
    block = results_block(results, UncertaintyPlan(replicates=50, min_rows=30))
    assert block["by_app"]["small"] == {"insufficient_data": True, "n": 5}
    assert "insufficient_data" not in block["by_app"]["app-1"]
    # Its rows still count in the totals.
    assert block["totals"]["n"] == 245
    assert block["totals"]["newly_rejected"]["point"] >= 5


def test_u7_too_few_rows_in_all_draws_nothing():                           # U7
    results = _synthetic(12)
    boot = run_bootstrap(results, UncertaintyPlan(replicates=50, min_rows=30), days=3.0)
    assert boot.totals is None and all(samples is None for _n, samples in boot.by_app.values())
    block = results_block(results, UncertaintyPlan(replicates=50, min_rows=30), days=3.0)
    assert block["totals"] == {"insufficient_data": True, "n": 12}
    assert block["per_day"] is None and block["points_outside_interval"] == 0
    assert all(b == {"insufficient_data": True, "n": 4} for b in block["by_app"].values())


def test_u7_no_rows_at_all():                                              # U7
    block = results_block([], UncertaintyPlan())
    empty = {"routed": 0, "calibrated": 0, "posterior": 0}
    assert block == {"totals": {"insufficient_data": True, "n": 0}, "by_app": {},
                     "per_day": None, "points_outside_interval": 0,
                     "quality_posterior": {"current": empty, "proposed": empty}}


def test_min_rows_is_inclusive():
    results = _synthetic(30)
    assert "insufficient_data" not in results_block(
        results, UncertaintyPlan(replicates=10, min_rows=30))["totals"]
    assert results_block(results, UncertaintyPlan(replicates=10, min_rows=31))["totals"] == {
        "insufficient_data": True, "n": 30}


# --- zero counts, undefined figures, per-day ----------------------------------------

def test_a_zero_count_is_zero_in_every_replicate_and_carries_a_bound():
    """`app-0` has no newly rejected row, so no resample of it can have one:
    the range is [0, 0]. That is not "no risk", so the share carries the
    largest rate that would still show nothing in n rows."""
    block = results_block(_synthetic(240), UncertaintyPlan(replicates=50))
    app0 = block["by_app"]["app-0"]
    assert app0["newly_rejected"] == {"point": 0, "lo": 0, "hi": 0}
    share = app0["newly_rejected_share"]
    assert (share["point"], share["lo"], share["hi"]) == (0, 0, 0)
    assert share["zero_bound"] == pytest.approx(zero_bound(80, 0.90), abs=1e-6)
    assert "zero_bound" not in block["by_app"]["app-1"]["newly_rejected_share"]


def test_zero_bound_values():
    assert zero_bound(1, 0.90) == pytest.approx(0.9)
    assert zero_bound(24, 0.90) == pytest.approx(0.0915, abs=1e-4)      # the DGX lab's 24 rows
    assert zero_bound(2_000_000, 0.90) == pytest.approx(1.1513e-6, rel=1e-3)
    assert zero_bound(100, 0.95) == pytest.approx(0.0295, abs=1e-4)     # the "rule of three"
    # (1 - bound) ** n is exactly the chance of seeing nothing at that rate.
    assert (1 - zero_bound(50, 0.90)) ** 50 == pytest.approx(0.10)


def test_a_figure_undefined_in_some_replicate_has_no_range():
    """One routed row among forty in the proposed arm: a replicate that does
    not draw it has no proposed latency at all. An interval over the other
    replicates would be conditional on the figure existing, so there is none."""
    results = [_result(i, "a", _arm("local-mid", 0.01), REJECTED) for i in range(39)]
    results.append(_result(39, "a", _arm("local-mid", 0.01), _arm("local-mid", 0.01)))
    plan = UncertaintyPlan(replicates=200)
    block = results_block(results, plan)
    node = block["totals"]["latency_ms"]["p50"]["proposed"]
    assert node["point"] == 800.0 and node["lo"] is None and node["hi"] is None
    assert 0 < node["undefined_replicates"] < 200
    assert "point_outside" not in node
    # The current arm routes every row, so its latency is always defined.
    assert block["totals"]["latency_ms"]["p50"]["current"] == {
        "point": 800.0, "lo": 800.0, "hi": 800.0}


def test_per_day_divides_by_the_fixed_window():
    results = _synthetic(240)
    plan = UncertaintyPlan(replicates=100)
    block = results_block(results, plan, days=4.0)
    for key in ("current", "proposed", "delta"):
        day, whole = block["per_day"]["cost_usd"][key], block["totals"]["cost_usd"][key]
        for part in ("point", "lo", "hi"):
            assert day[part] == pytest.approx(whole[part] / 4.0, abs=1e-6)
    both = block["per_day"]["cost_usd_both_routed"]["delta"]
    assert both["point"] == pytest.approx(
        block["totals"]["cost_usd_both_routed"]["delta"]["point"] / 4.0, abs=1e-6)
    assert "per_day" not in block["totals"]
    assert results_block(results, plan)["per_day"] is None


def test_window_days():
    assert window_days(None) is None
    assert window_days({"start": "2026-09-20T00:00:00+00:00",
                        "end": "2026-09-20T23:59:59+00:00"}) is None        # under a day
    assert window_days({"start": "2026-09-20T00:00:00+00:00",
                        "end": "2026-09-22T12:00:00Z"}) == pytest.approx(2.5)


# --- small pieces ---------------------------------------------------------------------

def test_interval_of():
    values = [float(v) for v in range(1000)]
    random.Random(0).shuffle(values)
    assert interval_of(values, 0.90) == (50.0, 949.0)
    assert interval_of(values, 0.50) == (250.0, 749.0)
    assert interval_of([3.0], 0.90) == (3.0, 3.0)
    assert interval_of([1.0, 2.0], 0.90) == (1.0, 2.0)
    assert interval_of([1.0, 2.0, 3.0], 0.02) == (2.0, 2.0)


def test_leaf_rounds_and_flags():
    plan = UncertaintyPlan(replicates=4, interval=0.5)
    assert leaf(0.1234567, [0.1, 0.12, 0.13, 0.2], plan) == {
        "point": 0.123457, "lo": 0.12, "hi": 0.13}
    assert leaf(0.5, [0.1, 0.12, 0.13, 0.2], plan) == {
        "point": 0.5, "lo": 0.12, "hi": 0.13, "point_outside": True}
    assert leaf(None, [None, None, None, None], plan) == {
        "point": None, "lo": None, "hi": None, "undefined_replicates": 4}
    assert leaf(-0.0, [-0.0] * 4, plan) == {"point": 0.0, "lo": 0.0, "hi": 0.0}


@pytest.mark.parametrize("kwargs", [
    {"replicates": 0}, {"replicates": MAX_REPLICATES + 1}, {"replicates": 10.0},
    {"replicates": True}, {"interval": 0.0}, {"interval": 1.0}, {"interval": float("nan")},
    {"interval": -0.1}, {"seed": -1}, {"seed": 1.5}, {"min_rows": 0}, {"min_rows": 2.5}])
def test_plan_refuses_values_out_of_range(kwargs):
    with pytest.raises(ValueError):
        UncertaintyPlan(**kwargs)


def test_plan_block_and_inputs():
    plan = UncertaintyPlan(replicates=250, interval=0.8, seed=4, min_rows=10)
    assert plan_inputs(plan) == {"replicates": 250, "interval": 0.8, "seed": 4, "min_rows": 10,
                                 "quality_posteriors_sha256": []}
    assert plan_block(plan) == {
        "method": METHOD, "strata": "application_id", "replicates": 250, "interval": 0.8,
        "seed": 4, "min_rows": 10, "quality_posterior_sets": [],
        "not_covered": list(NOT_COVERED)}
    assert UncertaintyPlan() == UncertaintyPlan(replicates=1000, interval=0.90, seed=0,
                                                min_rows=30)


# --- U11: nothing ranks ---------------------------------------------------------------

def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


BANNED = ("rank", "recommend", "best", "winner", "prefer", "score")


def test_u11_no_key_ranks_or_recommends():                                 # U11
    block = {**plan_block(UncertaintyPlan()),
             **results_block(_synthetic(), UncertaintyPlan(replicates=20), days=3.0)}
    assert not [k for k in _keys(block) if any(word in k.lower() for word in BANNED)]
    assert not [v for v in block["not_covered"] if any(word in v for word in BANNED)]
