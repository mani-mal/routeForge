"""P3c-3 final-review fixes F2, F3 and F6: what a leaf says when a bare
range would mislead.

- A figure over an arm's ROUTED rows (latency, quality, on-prem share) gets
  no range when that arm routed fewer than `min_rows` rows (F2).
- A count of 0 or a share of 1 has a one-value range at any sample size; the
  leaf carries a one-sided bound and the reports say so (F3).
- A point outside its own range is marked wherever the range is printed (F3).
- Too few replicates for the level make the range a min/max, and the output
  says so (F3).
- The rows whose size is RouteForge's own estimate are counted beside the
  caveat that names them (F6).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app.eval.uncertainty as uncertainty_mod
from app.eval.scenarios import comparison_doc, render_comparison
from app.eval.shadow import render_shadow_section, shadow_prices
from app.eval.simulate import simulate
from app.eval.uncertainty import (ZERO_COUNT_NOTE, UncertaintyPlan, bucket_lines, interval_of,
                                  leaf, plan_block, render_uncertainty_section,
                                  results_block, with_range, zero_bound)
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot
from tests.eval.test_scenarios_compare import _INPUTS, _TRIAD, _compare, _doc
from tests.eval.test_scenarios_compare import _snap as _compare_snap
from tests.eval.test_uncertainty import REJECTED, _arm, _leaves, _result
from tests.eval.test_uncertainty_golden import run_mode
from tests.eval.test_uncertainty_scenarios import _rows as _compare_rows
from tests.eval.test_uncertainty_shadow import _find, _records, _snap as _shadow_snap

P = "routeforge.policy-proposal/v1"


def _partly_routed(routed: int, n: int = 100) -> list:
    """One app, `n` rows, every one routed now; the proposal routes the
    first `routed` (to a faster, cheaper, lower-quality model) and rejects
    the rest. Costs differ row by row."""
    out = []
    for i in range(n):
        current = _arm("local-mid", 0.004 + 0.0001 * i, latency=[700.0, 900.0][i % 2])
        proposed = (_arm("local-cheap", 0.001 + 0.00002 * i, latency=[250.0, 350.0][i % 2],
                         quality=0.5) if i < routed else REJECTED)
        out.append(_result(i, "app-a", current, proposed))
    return out


_CONDITIONAL = (("latency_ms", "p50"), ("latency_ms", "p95"), ("quality_mean",),
                ("onprem_share",))


def _at(bucket: dict, path: tuple[str, ...], key: str) -> dict:
    node = bucket
    for part in (*path, key):
        node = node[part]
    return node


# --- F2: figures over an arm's routed rows ---------------------------------------------

def test_f2_ten_routed_rows_of_a_hundred_get_no_range_for_conditional_figures():
    """A 100-row bucket passes `min_rows`, but the proposed arm's latency,
    quality and on-prem share are figures of its 10 ROUTED rows. A 29-row
    app gets no range; neither may a mean of 10."""
    block = results_block(_partly_routed(10), UncertaintyPlan(replicates=200))
    for bucket in (block["totals"], block["by_app"]["app-a"]):
        assert bucket["n"] == 100 and bucket["routed"] == {"current": 100, "proposed": 10}
        for path in _CONDITIONAL:
            node = _at(bucket, path, "proposed")
            assert node["point"] is not None
            assert {k: v for k, v in node.items() if k != "point"} == {
                "lo": None, "hi": None, "insufficient_data": True, "n": 10}, path
            current = _at(bucket, path, "current")
            assert current["lo"] is not None and "insufficient_data" not in current, path
            if path != ("onprem_share",):
                delta = _at(bucket, path, "delta")
                assert (delta["lo"], delta["hi"], delta["insufficient_data"], delta["n"]) == (
                    None, None, True, 10), path
        # Sums and counts are over every row of the bucket: they keep a range.
        assert bucket["cost_usd"]["proposed"]["lo"] < bucket["cost_usd"]["proposed"]["hi"]
        assert bucket["cost_usd"]["delta"]["lo"] is not None
        rejected = bucket["newly_rejected"]
        assert rejected["point"] == 90 and rejected["lo"] < 90 < rejected["hi"]
    assert block["points_outside_interval"] == 0


def test_f2_forty_routed_rows_of_a_hundred_get_a_range():
    block = results_block(_partly_routed(40), UncertaintyPlan(replicates=200))
    bucket = block["totals"]
    assert bucket["routed"] == {"current": 100, "proposed": 40}
    for path in _CONDITIONAL:
        for key in ("current", "proposed") + (() if path == ("onprem_share",) else ("delta",)):
            node = _at(bucket, path, key)
            assert node["lo"] is not None and node["hi"] is not None, (path, key)
            assert "insufficient_data" not in node and "n" not in node, (path, key)


def test_f2_the_threshold_is_the_plans_and_is_inclusive():
    rows = _partly_routed(30)
    at = results_block(rows, UncertaintyPlan(replicates=50, min_rows=30))["totals"]
    assert "insufficient_data" not in at["quality_mean"]["proposed"]
    above = results_block(rows, UncertaintyPlan(replicates=50, min_rows=31))["totals"]
    assert above["quality_mean"]["proposed"]["n"] == 30
    assert above["quality_mean"]["delta"]["insufficient_data"] is True
    assert "insufficient_data" not in above["quality_mean"]["current"]


def test_f2_an_arm_that_routes_nothing_has_no_range_and_no_point():
    block = results_block(_partly_routed(0), UncertaintyPlan(replicates=50, min_rows=1))
    node = block["totals"]["latency_ms"]["p50"]["proposed"]
    assert node == {"point": None, "lo": None, "hi": None, "insufficient_data": True, "n": 0}


def test_f2_the_report_prints_how_many_routed_rows_there_were():
    plan = UncertaintyPlan(replicates=100)
    block = {**plan_block(plan), **results_block(_partly_routed(10), plan)}
    lines = bucket_lines(block["totals"], 0.90)
    for label in ("latency_ms p50 proposed", "latency_ms p95 delta", "quality_mean proposed",
                  "quality_mean delta", "onprem_share proposed"):
        line = next(l for l in lines if l.startswith(f"| {label} |"))
        assert line.endswith("| insufficient data (n = 10 routed) |"), line
    current = next(l for l in lines if l.startswith("| quality_mean current |"))
    assert "insufficient" not in current
    assert "insufficient data (n = 10 routed)" in render_uncertainty_section(block)


# --- F6: the rows the admission-estimate caveat is about -------------------------------

def _estimated_replay():
    """Two apps. A third of `capped`'s rows were rejected live (no backend
    token count); the proposal lifts the cap and serves them at RouteForge's
    own estimate."""
    snap = make_snapshot(apps=[app_entry("capped", max_cost_usd_per_request=0.0004),
                               app_entry("steady")])
    rows = []
    for i in range(45):
        size = [200, 1200, 6000][i % 3] + 5 * i
        over = dict(complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=size,
                    routing_tokens=size, ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
        if i % 3 == 2:
            over.update(status="no_eligible", prompt_tokens=0, completion_tokens=None,
                        attempts=[], chosen=None, latency_ms=None)
        rows.append(make_row(f"capped-{i:03d}", "capped", **over))
        rows.append(make_row(f"steady-{i:03d}", "steady", prompt_tokens=300 + 3 * i,
                             routing_tokens=300 + 3 * i,
                             ts=f"2026-09-{20 + i % 3:02d}T13:{i % 60:02d}:00+00:00"))
    results, _excluded = asyncio.run(simulate(
        rows, snap, {"schema": P, "apps": {"capped": {"max_cost_usd_per_request": None}}},
        base=BASE_SETTINGS))
    return results


def test_f6_each_bucket_counts_its_rows_with_no_backend_token_count():
    plan = UncertaintyPlan(replicates=60)
    block = {**plan_block(plan), **results_block(_estimated_replay(), plan)}
    assert block["totals"]["estimated_token_rows"] == 15
    assert block["by_app"]["capped"]["estimated_token_rows"] == 15
    assert block["by_app"]["steady"]["estimated_token_rows"] == 0
    assert block["totals"]["newly_served"]["point"] == 15
    text = render_uncertainty_section(block)
    line = ("15 of 90 simulated rows have no backend token count (`estimated_token_rows`): "
            "replay sizes and prices them from RouteForge's own estimate, the same in every "
            "resample.")
    assert line in text
    assert text.index("narrow range on a biased replay") < text.index(line) \
        < text.index("### Totals")


def test_f6_hand_built_rows_with_no_sizes_count_as_not_estimated():
    block = results_block(_partly_routed(40), UncertaintyPlan(replicates=20))
    assert block["totals"]["estimated_token_rows"] == 0


# --- F3: a count of 0, a share of 1 ------------------------------------------------------

def _shadow(plan: UncertaintyPlan) -> dict:
    return asyncio.run(shadow_prices(_records(), _shadow_snap(), base=BASE_SETTINGS,
                                     uncertainty=plan))


def test_f3_a_relaxation_with_no_new_rejection_carries_the_zero_bound():
    """`newly rejected: 0 [0, 0]` in the shadow Ranges table reads as a
    safety claim. The leaf carries the one-sided bound for the rows in
    scope, and the table is followed by one fixed sentence."""
    block = _shadow(UncertaintyPlan(replicates=100))
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")
    assert (big["newly_rejected"]["point"], big["newly_rejected"]["lo"],
            big["newly_rejected"]["hi"]) == (0, 0, 0)
    assert big["newly_rejected"]["zero_bound"] == round(zero_bound(60, 0.90), 6) == 0.037649
    # Only the count a reader would take as a safety claim, and only at 0.
    assert "zero_bound" not in big["route_changed"]
    assert "zero_bound" not in big["rejections_avoided"]
    report = render_shadow_section(block)
    ranges = report[report.index("### Ranges"):]
    assert ranges.count(uncertainty_mod.ZERO_RANGE_NOTE) == 1
    assert uncertainty_mod.ZERO_RANGE_NOTE == (
        "A count of 0 shows `[0, 0]` at any sample size; that is not zero risk.")


def test_f3_the_comparison_table_is_followed_by_the_zero_sentence():
    doc = comparison_doc(_compare(_compare_rows(), _compare_snap(), _doc(*_TRIAD),
                                  uncertainty=UncertaintyPlan(replicates=60)), inputs=_INPUTS)
    report = render_comparison(doc)
    table, section = report.index("## Comparison"), report.index("## Uncertainty ranges")
    assert table < report.index(uncertainty_mod.ZERO_RANGE_NOTE) < section
    plain = render_comparison(comparison_doc(
        _compare(_compare_rows(), _compare_snap(), _doc(*_TRIAD)), inputs=_INPUTS))
    assert uncertainty_mod.ZERO_RANGE_NOTE not in plain
    # Too few rows for any range: no brackets, so no sentence about brackets.
    few = render_comparison(comparison_doc(
        _compare(_compare_rows(12), _compare_snap(), _doc(*_TRIAD),
                 uncertainty=UncertaintyPlan(replicates=60)), inputs=_INPUTS))
    assert uncertainty_mod.ZERO_RANGE_NOTE not in few


def test_f3_the_zero_count_note_says_one_sided_and_replayed():
    note = ZERO_COUNT_NOTE.format(n=80, level="90%", bound="2.84%", one_in="35")
    assert "one-sided" in note
    assert "replayed newly-rejected rate" in note and "not of the live rate" in note
    assert "That is not zero risk" in note and "independent draws" in note


def test_f3_a_share_of_one_carries_the_lower_bound():
    """The mirror case. Every row newly rejected: the share is 1 and its
    range is `1 to 1` at any sample size. `one_bound` is the smallest rate
    at which n independent rows would all show the event with probability
    1 - P."""
    block = results_block(_partly_routed(0), UncertaintyPlan(replicates=50))
    share = block["totals"]["newly_rejected_share"]
    assert (share["point"], share["lo"], share["hi"]) == (1, 1, 1)
    assert share["one_bound"] == round(0.1 ** (1 / 100), 6) == 0.977237
    assert uncertainty_mod.one_bound(100, 0.90) ** 100 == pytest.approx(0.10)
    assert "one_bound" not in block["totals"]["route_changed_share"]         # its point is 0
    assert block["totals"]["route_changed_share"]["zero_bound"] == round(zero_bound(100, 0.9), 6)
    # An arm's on-prem share is over its ROUTED rows: n is the routed count.
    onprem = results_block(_partly_routed(40), UncertaintyPlan(replicates=50))["totals"]
    assert onprem["onprem_share"]["current"]["one_bound"] == round(0.1 ** (1 / 100), 6)
    assert onprem["onprem_share"]["proposed"]["one_bound"] == round(0.1 ** (1 / 40), 6)
    lines = bucket_lines(block["totals"], 0.90)
    assert lines.count(uncertainty_mod.SATURATED_SHARE_NOTE) == 1
    assert "that is not certainty" in uncertainty_mod.SATURATED_SHARE_NOTE


def test_f3_an_on_prem_share_of_zero_carries_the_zero_bound():
    rows = [_result(i, "app-a", _arm("azure-big", 0.01 + 0.0001 * i, provider="azure"),
                    _arm("local-mid", 0.004)) for i in range(50)]
    totals = results_block(rows, UncertaintyPlan(replicates=30))["totals"]
    assert totals["onprem_share"]["current"]["zero_bound"] == round(zero_bound(50, 0.90), 6)
    assert "one_bound" not in totals["onprem_share"]["current"]
    assert totals["onprem_share"]["proposed"]["one_bound"] == round(0.1 ** (1 / 50), 6)


def test_f3_a_share_strictly_between_has_neither_bound():
    totals = results_block(_partly_routed(40), UncertaintyPlan(replicates=30))["totals"]
    assert set(totals["newly_rejected_share"]) == {"point", "lo", "hi"}
    assert uncertainty_mod.SATURATED_SHARE_NOTE not in bucket_lines(
        {**totals, "onprem_share": {"current": {"point": 0.5, "lo": 0.4, "hi": 0.6},
                                    "proposed": {"point": 0.5, "lo": 0.4, "hi": 0.6}}}, 0.90)


# --- F3: a point outside its range is marked wherever the range is printed --------------

def test_f3_with_range_marks_a_point_outside_and_an_insufficient_leaf():
    assert with_range("9", {"point": 9.0, "lo": 1.0, "hi": 2.0, "point_outside": True}) == \
        "9 [1, 2]*"
    assert with_range("9", {"point": 9.0, "lo": 1.0, "hi": 20.0}) == "9 [1, 20]"
    assert with_range("300", {"point": 300.0, "lo": None, "hi": None,
                              "insufficient_data": True, "n": 10}) == \
        "300 [insufficient data, n = 10 routed]"


def test_f3_the_comparison_table_marks_points_outside_and_explains_the_mark():
    """One replicate: `lo == hi ==` that replicate's value, which is not the
    point. The comparison table used to print such a cell like any other."""
    doc = comparison_doc(_compare(_compare_rows(), _compare_snap(), _doc(*_TRIAD),
                                  uncertainty=UncertaintyPlan(replicates=1)), inputs=_INPUTS)
    outside = sum(1 for entry in doc["uncertainty"]["scenarios"]
                  for _path, node in _leaves(entry["table"]) if node.get("point_outside"))
    assert outside > 0
    report = render_comparison(doc)
    table = report[report.index("## Comparison"):report.index("## Uncertainty ranges")]
    assert table.count("]*") == outside - sum(
        1 for entry in doc["uncertainty"]["scenarios"]
        for key in ("onprem_share_current", "onprem_share_proposed")
        if entry["table"][key].get("point_outside"))        # that cell shows no range
    assert table.count(uncertainty_mod.OUTSIDE_FOOTNOTE) == 1
    calm = render_comparison(comparison_doc(
        _compare(_compare_rows(), _compare_snap(), _doc(*_TRIAD),
                 uncertainty=UncertaintyPlan(replicates=200)), inputs=_INPUTS))
    assert "]*" not in calm and uncertainty_mod.OUTSIDE_FOOTNOTE not in calm


def test_f3_shadow_ranges_count_and_mark_points_outside():
    block = _shadow(UncertaintyPlan(replicates=1))
    ranges = block["uncertainty"]
    expected = sum(1 for _path, node in _leaves(ranges["constraints"])
                   if node.get("point_outside"))
    assert ranges["points_outside_interval"] == expected > 0
    section = render_shadow_section(block)
    section = section[section.index("### Ranges"):]
    assert "]*" in section and section.count(uncertainty_mod.OUTSIDE_FOOTNOTE) == 1
    calm = _shadow(UncertaintyPlan(replicates=200))
    assert calm["uncertainty"]["points_outside_interval"] == 0
    assert uncertainty_mod.OUTSIDE_FOOTNOTE not in render_shadow_section(calm)


# --- F3: the interval at its edges ------------------------------------------------------

def test_f3_interval_of_never_returns_an_inverted_pair():
    """A level so small that both tails pass the middle: `k` is clamped, so
    the pair is the two middle values, in order."""
    values = [float(v) for v in range(1000)]
    assert interval_of(values, 1e-13) == (499.0, 500.0)
    assert interval_of([float(v) for v in range(7)], 1e-9) == (3.0, 3.0)
    for n in (1, 2, 3, 10, 999, 1000):
        for level in (1e-13, 1e-6, 0.02, 0.5, 0.9, 0.999, 1 - 1e-12):
            lo, hi = interval_of([float(v) for v in range(n)], level)
            assert lo <= hi, (n, level)


@pytest.mark.parametrize("replicates,level,expected", [
    (19, 0.90, True), (20, 0.90, False), (1000, 0.90, False), (1, 0.5, True),
    (39, 0.95, True), (40, 0.95, False), (1000, 0.999, True), (2000, 0.999, False)])
def test_f3_too_few_replicates_for_the_level_is_said_in_the_head(replicates, level, expected):
    """With fewer than 2 / (1 - P) replicates no value is left out on either
    side: the "interval" is the replicates' minimum and maximum."""
    plan = UncertaintyPlan(replicates=replicates, interval=level)
    assert plan_block(plan)["interval_is_min_max"] is expected
    values = [float(v) for v in range(replicates)]
    assert (interval_of(values, level) == (0.0, float(replicates - 1))) is expected


def test_f3_the_report_and_stderr_say_a_range_is_a_min_max(tmp_path, capsys):
    plan = UncertaintyPlan(replicates=19)
    block = {**plan_block(plan), **results_block(_partly_routed(40), plan)}
    text = render_uncertainty_section(block)
    line = ("With 19 resamples each range is the smallest and the largest resample value, not "
            "a 90% interval: a 90% interval needs at least 20 resamples.")
    assert line in text
    full = UncertaintyPlan(replicates=20)
    assert "smallest and the largest" not in render_uncertainty_section(
        {**plan_block(full), **results_block(_partly_routed(40), full)})
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--bootstrap", "19") == 0
    err = capsys.readouterr().err
    assert ("policy_simulate: warning: --bootstrap 19 is too few for a 90% interval (it needs "
            "at least 20): every range is the smallest and the largest resample value") in err
    assert json.loads((out / "simulation.json").read_text())["uncertainty"][
        "interval_is_min_max"] is True
    assert run_mode("proposal", tmp_path / "ok", "--uncertainty", "--bootstrap", "20") == 0
    assert "too few" not in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0.9", None, [0.9], b"0.9"])
def test_f3_a_non_numeric_interval_is_a_value_error(value):
    with pytest.raises(ValueError, match="interval"):
        UncertaintyPlan(interval=value)


def test_f3_leaf_bounds():
    plan = UncertaintyPlan(replicates=4, interval=0.5)
    assert leaf(1.0, [1.0] * 4, plan, one_n=10) == {
        "point": 1.0, "lo": 1.0, "hi": 1.0, "one_bound": round(0.5 ** 0.1, 6)}
    assert leaf(0, [0] * 4, plan, zero_n=10) == {
        "point": 0, "lo": 0, "hi": 0, "zero_bound": round(1 - 0.5 ** 0.1, 6)}
