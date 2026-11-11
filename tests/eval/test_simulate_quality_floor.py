"""B80 Task 8: the simulator replays quality floors and calibrated lower bounds."""
from __future__ import annotations

import asyncio
import copy

import pytest

from app.eval.proposal import ProposalError, apply_proposal
from app.eval.scenario import SnapshotError, build_scenario, close_scenario, validate_snapshot
from app.eval.simulate import baseline_check, simulate
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot

P = "routeforge.policy-proposal/v1"
BOUNDS = [{"model": "local-cheap", "workload": "claims", "lower": 0.9, "set_id": "cal-1"},
          {"model": "local-mid", "workload": "claims", "lower": 0.95, "set_id": "cal-1"}]


def _snap(bounds=None, **kw):
    snap = make_snapshot(**kw)
    if bounds is not None:
        snap["quality"]["bounds"] = copy.deepcopy(bounds)
    return snap


def _rows(n=6):
    return [make_row(f"r{i}", "app-a", workload="claims", complexity=1.5,
                     ts=f"2026-09-20T12:00:{i:02d}+00:00",
                     attempts=["local-big"], chosen="local-big") for i in range(n)]


def _run(records, snap, prop):
    return asyncio.run(simulate(records, snap, prop, base=BASE_SETTINGS))


def test_scenario_loads_bounds_into_the_resolver():
    async def go():
        scn = await build_scenario(_snap(BOUNDS), base=BASE_SETTINGS)
        try:
            return scn.quality.bounds_map()
        finally:
            await close_scenario(scn)
    assert asyncio.run(go()) == {("local-cheap", "claims"): (0.9, "cal-1"),
                                 ("local-mid", "claims"): (0.95, "cal-1")}


@pytest.mark.parametrize("bad", [
    [{"model": "m", "workload": None, "lower": 1.5, "set_id": "s"}],
    [{"model": "m", "workload": None, "lower": "x", "set_id": "s"}],
    [{"model": "m", "workload": 3, "lower": 0.5, "set_id": "s"}],
    [{"model": "m", "workload": None, "lower": 0.5}],
    "nope"])
def test_bad_bounds_entries_are_snapshot_errors(bad):
    with pytest.raises(SnapshotError):
        validate_snapshot(_snap(bad))


def test_old_snapshot_without_bounds_replays_unchanged():
    rows = _rows()
    snap = _snap()
    assert "bounds" not in snap["quality"]
    for a in snap["apps"]:
        for f in ("quality_floors", "quality_floor_mode"):
            a["policy"].pop(f, None)
    validate_snapshot(snap)
    out = asyncio.run(baseline_check(rows, snap, base=BASE_SETTINGS))
    with_empty = _snap([]); with_empty["quality"]["bounds"] = []
    again = asyncio.run(baseline_check(rows, with_empty, base=BASE_SETTINGS))
    assert out == again and out["rows_compared"] == len(rows)


def test_proposal_enabling_floor_moves_routes():
    snap = _snap(BOUNDS)
    prop = {"schema": P, "routing": {"quality_floors": {
        "mode": "enforce", "workloads": {"claims": 0.8}}}}
    results, _ = _run(_rows(), snap, prop)
    moved = [r for r in results if r.current.model != r.proposed.model]
    assert moved
    assert all(r.proposed.model == "local-cheap" for r in moved)
    assert (sum(r.proposed.cost_usd for r in results)
            < sum(r.current.cost_usd for r in results))


def test_proposal_floor_validation():
    snap = _snap(BOUNDS)
    for bad in ({"quality_floors": {"mode": "bogus"}},
                {"quality_floors": {"workloads": {"claims": 2.0}}},
                {"quality_floors": {"extra": 1}},
                {"other": {}}):
        with pytest.raises(ProposalError):
            apply_proposal(snap, {"schema": P, "routing": bad}, base=BASE_SETTINGS)


def test_app_floor_in_snapshot_policy_applies():
    snap = _snap(BOUNDS, apps=[app_entry("app-a", quality_floors={"claims": 0.8},
                                         quality_floor_mode="enforce")])
    results, _ = _run(_rows(), snap, {"schema": P})
    assert all(r.current.model == "local-cheap" for r in results)
