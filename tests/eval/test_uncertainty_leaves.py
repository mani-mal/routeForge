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

import pytest

from app.eval.simulate import simulate
from app.eval.uncertainty import (UncertaintyPlan, bucket_lines, plan_block,
                                  render_uncertainty_section, results_block)
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot
from tests.eval.test_uncertainty import REJECTED, _arm, _result

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
