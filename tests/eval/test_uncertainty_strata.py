"""P3c-3 final-review fixes F1 and F4: which strata the totals are resampled
in, and what the engine holds in memory.

- An application with at least `min_rows` rows is its own stratum. All rows
  of the smaller applications are ONE pooled stratum, which feeds the totals
  only (F1).
- `fixtures/uncertainty_engine/large_apps_block.json` is the block the engine
  produced at commit 4344546, BEFORE strata and replicates swapped loops
  (F4a). An export with no pooled application must still give those leaves.
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import pytest

from app.eval.scenarios import comparison_doc, render_comparison
from app.eval.shadow import RANGE_STRATA_NOTE, render_shadow_section, shadow_prices
from app.eval.simulate import ArmResult, RowResult, _bucket_stats, simulate
from app.eval.uncertainty import (Posterior, UncertaintyPlan, _flatten, method_line,
                                  plan_block, render_uncertainty_section, results_block,
                                  run_bootstrap, strata_line)
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)
from tests.eval.test_scenarios_compare import _INPUTS, _compare, _doc
from tests.eval.test_uncertainty import (REJECTED, _arm, _delta_leaves, _leaves, _result,
                                         _width)

P = "routeforge.policy-proposal/v1"

FIXTURE = Path(__file__).parent / "fixtures" / "uncertainty_engine" / "large_apps_block.json"
SET_ID = "set-0123456789ab"
POSTERIORS = {(SET_ID, "local-cheap", None): Posterior(41.0, 9.0, 0.82)}
#: The leaf keys that existed when the fixture was written.
_OLD_LEAF_KEYS = ("point", "lo", "hi", "point_outside", "undefined_replicates", "zero_bound")


def _calibrated(cost: float) -> ArmResult:
    return ArmResult("routed", "local-cheap", "onprem", "cheap", ("local-cheap",), (), cost,
                     300.0, "observed", 0.82, "calibrated", quality_set_id=SET_ID)


def _mixed(sizes: dict[str, int], seed: int = 11) -> list[RowResult]:
    """Rows with every kind of arm pair: unchanged, moved to a calibrated
    cheaper model, moved to the cloud, newly rejected, newly served and
    rejected in both arms. Latencies, qualities and costs differ; rows are
    interleaved across applications the way an export is."""
    rng = random.Random(seed)
    out: list[RowResult] = []
    order = [app for app, n in sorted(sizes.items()) for _ in range(n)]
    rng.shuffle(order)
    for i, app in enumerate(order):
        cost = rng.uniform(0.001, 0.02)
        roll = rng.random()
        current: ArmResult = _arm("local-mid", cost, latency=rng.choice([700.0, 800.0, 950.0]))
        if roll < 0.45:
            proposed = current
        elif roll < 0.70:
            proposed = _calibrated(cost * 0.2)
        elif roll < 0.80:
            proposed = _arm("azure-big", cost * 3, latency=1500.0, quality=0.9,
                            provider="azure")
        elif roll < 0.88:
            proposed = REJECTED
        elif roll < 0.96:
            current, proposed = REJECTED, _calibrated(cost * 0.2)
        else:
            current = proposed = REJECTED
        out.append(_result(i, app, current, proposed))
    return out


LARGE = {"app-a": 50, "app-b": 65, "app-c": 80, "app-d": 95}


def _large_block() -> dict:
    plan = UncertaintyPlan(replicates=300, seed=3, posteriors=POSTERIORS)
    return results_block(_mixed(LARGE), plan, days=3.0)


def _old_leaves(block: dict) -> dict[str, dict]:
    return {path: {k: node[k] for k in _OLD_LEAF_KEYS if k in node}
            for path, node in _leaves({k: block[k] for k in ("totals", "by_app", "per_day")})}


def write_fixture() -> None:
    """Run ONLY against the engine as it was at 4344546 (see module docstring)."""
    FIXTURE.write_text(json.dumps(_old_leaves(_large_block()), sort_keys=True, indent=1) + "\n",
                       encoding="utf-8")


def test_f4_an_export_with_only_large_apps_gives_the_leaves_it_gave_before():
    """Strata outer, replicates inner, per-app samples reduced as each app
    finishes: for an export with no pooled application every leaf is the one
    the replicate-outer engine wrote, to the last digit."""
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(expected) == 119          # 5 buckets x 23 leaves + 4 per-day
    assert _old_leaves(_large_block()) == expected


# --- F1: the engine draws what the two seed formulas say ------------------------------

SIZES = {"big-a": 45, "big-b": 30, "s1": 3, "s2": 1, "s3": 12, "s4": 29}


def _resampled(results: list[RowResult], seed: int, b: int, min_rows: int
               ) -> tuple[dict[str, list[RowResult]], list[RowResult]]:
    """Replicate `b` rebuilt row by row from the two seed formulas (spec
    §4.2, §4.3), with no engine code: `(each own app's drawn rows, the
    totals' drawn rows)`. An app with at least `min_rows` rows is drawn from
    its own rows; all the other apps' rows, concatenated in app order, are
    drawn as one pool."""
    by_app: dict[str, list[RowResult]] = {}
    for r in results:
        by_app.setdefault(r.row["application_id"], []).append(r)
    own: dict[str, list[RowResult]] = {}
    totals: list[RowResult] = []
    pool: list[RowResult] = []
    for app in sorted(by_app):
        rows = by_app[app]
        if len(rows) < min_rows:
            pool += rows
            continue
        idx = random.Random(f"{seed}:{b}:app:{app}").choices(range(len(rows)), k=len(rows))
        own[app] = [rows[i] for i in idx]
        totals += own[app]
    if pool:
        idx = random.Random(f"{seed}:{b}:pool").choices(range(len(pool)), k=len(pool))
        totals += [pool[i] for i in idx]
    return own, totals


def _assert_figures(got: dict, expected: dict, where: str) -> None:
    assert set(got) == set(expected), where
    for name, value in expected.items():
        if name in ("route_changed", "newly_rejected", "newly_served"):
            assert got[name] == value and isinstance(got[name], int), (where, name)
        elif value is None:
            assert got[name] is None, (where, name)
        else:
            # This one leaf divides a delta ROUNDED to 6 dp (the comparison
            # table's own arithmetic), so one rounding step is its tolerance.
            tolerance = 1e-6 if name == "per_day.cost_usd_both_routed.delta" else 1e-9
            assert got[name] == pytest.approx(value, abs=tolerance), (where, name)


@pytest.mark.parametrize("min_rows", [30, 1, 46])
def test_f1_every_replicate_is_the_rows_the_seed_formulas_draw(min_rows):   # U4, U6, U13
    """Brute force. For every replicate the resampled rows are rebuilt from
    the seed formulas and summarised by `_bucket_stats`, the code that
    writes the document's own figures; the engine's replicate values must be
    those -- counts exactly, floats to 1e-9 -- for the totals and for every
    application that has a range. Rejected arms in both directions, three
    latencies and per-day figures are all in the rows."""
    results = _mixed(SIZES, seed=5)
    plan = UncertaintyPlan(replicates=25, seed=9, min_rows=min_rows)
    days = 2.5
    boot = run_bootstrap(results, plan, days=days)
    ranged = {app for app, n in SIZES.items() if n >= min_rows}
    assert {app for app, (_n, samples) in boot.by_app.items() if samples is not None} == ranged
    for b in range(plan.replicates):
        own, totals = _resampled(results, plan.seed, b, min_rows)
        assert set(own) == ranged and len(totals) == len(results)
        _assert_figures({name: values[b] for name, values in boot.totals.items()},
                        _flatten(_bucket_stats(totals), len(totals), days), f"totals[{b}]")
        for app, rows in own.items():
            _n, samples = boot.by_app[app]
            _assert_figures({name: values[b] for name, values in samples.items()},
                            _flatten(_bucket_stats(rows), len(rows), None), f"{app}[{b}]")


# --- F1: small applications are pooled, and the block says so --------------------------

def _homogeneous(n: int = 240, seed: int = 21) -> list[tuple[ArmResult, ArmResult]]:
    """`n` arm pairs from one distribution: no application differs from another."""
    rng = random.Random(seed)
    pairs = []
    for _ in range(n):
        cost = rng.uniform(0.001, 0.02)
        current = _arm("local-mid", cost)
        roll = rng.random()
        pairs.append((current, current if roll >= 0.34 else
                      _arm("local-cheap", cost * 0.2, latency=300.0, quality=0.5)
                      if roll < 0.30 else REJECTED))
    return pairs


def _shaped(apps: int, per_app: int) -> list[RowResult]:
    """The SAME 240 rows, labelled as `apps` applications of `per_app` rows."""
    assert apps * per_app == 240
    return [_result(i, f"app-{i // per_app:03d}", current, proposed)
            for i, (current, proposed) in enumerate(_homogeneous())]


@pytest.mark.parametrize("apps,per_app", [(80, 3), (120, 2), (240, 1)])
def test_f1_many_small_apps_give_the_totals_the_width_of_one_app(apps, per_app):
    """Each app drawn alone would give the totals sqrt((n - S)/n) of the
    width -- 0.82, 0.71 and 0 for these three shapes. Pooled, the totals
    range is that of the same 240 rows as one application."""
    plan = UncertaintyPlan(replicates=1000, min_rows=30)
    one = results_block(_shaped(1, 240), plan)
    many = results_block(_shaped(apps, per_app), plan)
    for arm in ("current", "proposed", "delta"):
        ratio = _width(many["totals"]["cost_usd"][arm]) / _width(one["totals"]["cost_usd"][arm])
        assert 0.9 <= ratio <= 1.1, (arm, ratio)
    assert many["totals_strata"] == {"own": 0, "pooled_apps": apps, "pooled_rows": 240,
                                     "smallest": 240, "variance_factor": round(239 / 240, 6)}
    assert one["totals_strata"] == {"own": 1, "pooled_apps": 0, "pooled_rows": 0,
                                    "smallest": 240, "variance_factor": round(239 / 240, 6)}
    # A pooled app has no range of its own: insufficient data, as before.
    assert all(bucket == {"insufficient_data": True, "n": per_app}
               for bucket in many["by_app"].values())


def test_f1_min_rows_1_pools_nothing_and_the_block_says_the_width_is_zero():
    """`--min-rows 1`: every one-row app is its own stratum, a constant in
    every replicate. The totals have no width at all -- and the disclosure
    says exactly that, rather than leaving a zero-width range to be read as
    precision."""
    block = results_block(_shaped(240, 1), UncertaintyPlan(replicates=200, min_rows=1))
    assert block["totals_strata"] == {"own": 240, "pooled_apps": 0, "pooled_rows": 0,
                                      "smallest": 1, "variance_factor": 0.0}
    for arm in ("current", "proposed", "delta"):
        assert _width(block["totals"]["cost_usd"][arm]) == 0
    assert _width(block["totals"]["newly_rejected"]) == 0


def test_f1_totals_strata_counts_a_mixed_export():
    block = results_block(_mixed(SIZES, seed=5), UncertaintyPlan(replicates=20))
    # big-a (45) and big-b (30) on their own; s1..s4 (3 + 1 + 12 + 29 rows) pooled.
    assert block["totals_strata"] == {"own": 2, "pooled_apps": 4, "pooled_rows": 45,
                                      "smallest": 30, "variance_factor": round(117 / 120, 6)}
    assert {app for app, bucket in block["by_app"].items()
            if bucket.get("insufficient_data")} == {"s1", "s2", "s3", "s4"}


def test_f1_nothing_drawn_has_no_strata():
    block = results_block(_mixed({"a": 5, "b": 7}), UncertaintyPlan(replicates=20))
    assert block["totals"] == {"insufficient_data": True, "n": 12}
    assert block["totals_strata"] is None


def test_f1_the_report_states_the_strata_and_that_ranges_are_not_corrected():
    plan = UncertaintyPlan(replicates=20)
    block = {**plan_block(plan), **results_block(_mixed(SIZES, seed=5), plan)}
    line = strata_line(block["totals_strata"], plan.min_rows)
    assert line == (
        "Totals strata: 2 application(s) resampled on their own and 4 with fewer than 30 "
        "rows resampled together (45 rows); the smallest stratum has 30 rows. Resampling "
        "within strata leaves the totals about 0.975 of their variance (analytic, equal "
        "variance assumed, not measured); ranges are not corrected for it.")
    text = render_uncertainty_section(block)
    assert line in text and text.index(method_line(block)) < text.index(line)
    assert ("drawn with replacement within each application (applications with fewer than "
            "30 rows are resampled together for the totals) (seed 0).") in method_line(block)


# --- F1: pairing survives the pool -------------------------------------------------------

def _small_app_rows(apps: dict[str, int]) -> list[dict]:
    rows = []
    for app, n in apps.items():
        rows += [make_row(f"{app}-{i:03d}", app, complexity=[0.05, 0.6, 1.0][i % 3],
                          prompt_tokens=[200, 1200, 3000][i % 3] + 7 * i + len(app),
                          ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
                 for i in range(n)]
    return rows


_APPS = {"claims": 40, "chat": 34, "tiny-a": 5, "tiny-b": 9, "tiny-c": 1}


def _pool_snap(**mid) -> dict:
    return make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid", **mid),
                   onboarded_row("local-big", "frontier")],
        apps=[app_entry(app) for app in _APPS])


def test_f1_noop_is_exactly_zero_with_pooled_apps():                       # U4
    """The pool draw is one draw of row positions shared by both arms, so a
    proposal that changes nothing is still exactly [0, 0] in the totals."""
    results, _excluded = asyncio.run(simulate(_small_app_rows(_APPS), _pool_snap(),
                                              {"schema": P}, base=BASE_SETTINGS))
    block = results_block(results, UncertaintyPlan(replicates=200), days=2.0)
    assert block["totals_strata"]["pooled_apps"] == 3
    deltas = _delta_leaves({"totals": block["totals"], "per_day": block["per_day"]})
    assert len(deltas) == 13
    for path, node in deltas:
        assert (node["point"], node["lo"], node["hi"]) == (0, 0, 0), path
    assert _width(block["totals"]["cost_usd"]["current"]) > 0


def test_f1_a_scenario_gets_the_same_ranges_alone_and_among_others_with_small_apps():   # U4
    plan = UncertaintyPlan(replicates=80)
    drop = ("drop-mid", {"endpoints": {"remove": ["local-mid"]}})
    half = ("half-tokens", {"workload": {"token_scale": 0.5}})
    rows = _small_app_rows(_APPS)

    def document(*items):
        return comparison_doc(_compare(rows, _pool_snap(), _doc(*items), uncertainty=plan),
                              inputs=_INPUTS)

    def scenarios(*items):
        return document(*items)["uncertainty"]["scenarios"]

    (alone,) = scenarios(drop)
    forward, backward = scenarios(half, drop), scenarios(drop, half)
    assert alone == forward[1] == backward[0]
    assert alone["totals_strata"] == {"own": 2, "pooled_apps": 3, "pooled_rows": 15,
                                      "smallest": 15, "variance_factor": round(86 / 89, 6)}
    assert _width(alone["totals"]["cost_usd"]["delta"]) > 0
    # The comparison report states the strata once: every scenario has the same.
    report = render_comparison(document(half, drop))
    assert report.count(strata_line(alone["totals_strata"], 30)) == 1


def test_f1_an_endpoint_relaxation_equals_the_equivalent_proposals_totals():   # U4
    """`endpoint_app_allowlist` is the one relaxation that re-simulates every
    row, so it is resampled in the export's own strata, pool included. Its
    ranges must be the totals of the proposal that makes the same edit."""
    plan = UncertaintyPlan(replicates=120)
    rows = _small_app_rows(_APPS)
    snap = _pool_snap(allowed_app_ids=["chat"])
    block = asyncio.run(shadow_prices(rows, snap, base=BASE_SETTINGS, uncertainty=plan))
    entry = next(e for e in block["uncertainty"]["constraints"]
                 if e["scope"] == {"endpoint": "local-mid"})
    results, _excluded = asyncio.run(simulate(
        rows, snap, {"schema": P, "endpoints": {"update": {"local-mid": {
            "allowed_app_ids": None}}}}, base=BASE_SETTINGS))
    expected = results_block(results, plan)
    totals = expected["totals"]
    assert entry["n"] == totals["n"] == sum(_APPS.values())
    assert entry["totals_strata"] == expected["totals_strata"]
    assert entry["totals_strata"]["own"] == 2 and entry["totals_strata"]["pooled_apps"] == 3
    assert entry["route_changed"] == totals["route_changed"]
    assert entry["route_changed"]["lo"] < entry["route_changed"]["hi"]
    assert entry["cost_usd_both_routed_delta"] == totals["cost_usd_both_routed"]["delta"]
    assert entry["rejections_avoided"] == totals["newly_served"]
    for key in ("point", "lo", "hi"):
        assert entry["newly_rejected"][key] == totals["newly_rejected"][key]
    report = render_shadow_section(block)
    assert RANGE_STRATA_NOTE in report[report.index("### Ranges"):]
    assert "ranges are not corrected for it" in RANGE_STRATA_NOTE


if __name__ == "__main__":
    write_fixture()
