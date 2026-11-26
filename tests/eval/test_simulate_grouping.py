"""B65 items 5 and 7: single-pass per-application grouping in `simulate`, and
the insufficient-data count for latency and quality."""
from __future__ import annotations

import asyncio
from collections import Counter

import app.eval.simulate as sim_mod
from app.eval.outcomes import UNTAGGED_WORKLOAD, _round6
from app.eval.simulate import _agrees, _bucket_stats, _outcome_stats, simulate, summarize
from app.eval.uncertainty import UncertaintyPlan, results_block
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot
from tests.eval.test_simulate import _INPUTS, P
from tests.eval.test_uncertainty import REJECTED, _arm, _result

_APPS = ("app-c", "app-a", "app-d", "app-b")


def _fixture():
    """Four apps, rows interleaved (so a per-app group is not contiguous),
    mixed workloads, recorded rejections, and a proposal that moves rows."""
    rows = []
    for i in range(24):
        over = {"complexity": [0.05, 0.6, 1.5][i % 3],
                "ts": f"2026-09-20T12:00:{i:02d}+00:00",
                "workload": [None, "chat", "rag"][i % 3]}
        if i % 7 == 0:
            over.update(status="no_eligible", attempts=[], chosen=None)
        rows.append(make_row(f"r{i}", _APPS[i % 4], **over))
    snap = make_snapshot(apps=[app_entry(a) for a in _APPS])
    prop = {"schema": P, "endpoints": {"remove": ["local-cheap"]}}
    results, excluded = asyncio.run(simulate(rows, snap, prop, base=BASE_SETTINGS))
    return results, excluded


def _old_grouped(results):
    """The pre-B65 re-filtering, kept as the oracle."""
    ids = sorted({r.row["application_id"] for r in results})
    labels = sorted({r.row.get("workload") or UNTAGGED_WORKLOAD for r in results})
    by_app = {a: _bucket_stats([r for r in results if r.row["application_id"] == a])
              for a in ids}
    by_wl = {w: _bucket_stats([r for r in results
                               if (r.row.get("workload") or UNTAGGED_WORKLOAD) == w])
             for w in labels}
    compared = [r for r in results
                if r.row.get("status") == "ok" and (r.row.get("attempts") or [])]
    base_by_app = {}
    for a in sorted({r.row["application_id"] for r in compared}):
        sub = [r for r in compared if r.row["application_id"] == a]
        base_by_app[a] = sum(1 for r in sub if _agrees(r)) / len(sub)
    outcome_by_app = {a: _outcome_stats([r for r in results if r.row["application_id"] == a])
                      for a in ids}
    return by_app, by_wl, base_by_app, outcome_by_app


def test_summarize_grouping_matches_refiltering_oracle():
    results, excluded = _fixture()
    assert len({r.row["application_id"] for r in results}) == 4
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    by_app, by_wl, base_by_app, outcome_by_app = _old_grouped(results)
    assert sim["by_app"] == _round6(by_app)
    assert list(sim["by_app"]) == sorted(by_app)
    assert sim["by_workload"] == _round6(by_wl)
    assert list(sim["by_workload"]) == sorted(by_wl)
    assert sim["baseline"]["by_app"] == _round6(base_by_app)
    assert sim["baseline"]["outcome_by_app"] == _round6(outcome_by_app)
    assert list(sim["baseline"]["outcome_by_app"]) == sorted(outcome_by_app)


def test_summarize_walks_the_rows_once_per_grouping(monkeypatch):
    """No per-application re-filtering: `_group_by_app` is called with the
    full result list at most a fixed number of times, never once per app."""
    results, excluded = _fixture()
    calls = []
    real = sim_mod._group_by_app
    monkeypatch.setattr(sim_mod, "_group_by_app",
                        lambda rs: calls.append(len(rs)) or real(rs))
    summarize(results, excluded, inputs=_INPUTS, edits={})
    assert len(calls) == 3                      # summarize, and _baseline twice
    assert Counter(calls)[len(results)] >= 2 and len(calls) < len(_APPS)


def test_insufficient_count_is_routed_rows_carrying_the_figure():
    """B65 item 5: replay always gives a routed arm a latency and a quality,
    but an `ArmResult` may be built without one; the count behind latency and
    quality is the routed rows that carry it."""
    rows = [_result(i, "app-a", _arm("m", 0.01),
                    _arm("m2", 0.01, latency=None if i < 25 else 300.0,
                         quality=None if i < 10 else 0.6))
            for i in range(40)]
    b = results_block(rows, UncertaintyPlan(replicates=50, min_rows=30))["totals"]
    assert b["routed"] == {"current": 40, "proposed": 40}
    lat = b["latency_ms"]["p50"]["proposed"]
    assert lat["insufficient_data"] is True and lat["n"] == 15
    assert b["latency_ms"]["p50"]["delta"]["n"] == 15
    q = b["quality_mean"]["proposed"]
    assert "insufficient_data" not in q                    # 30 rows carry a quality
    assert "insufficient_data" not in b["onprem_share"]["proposed"]
