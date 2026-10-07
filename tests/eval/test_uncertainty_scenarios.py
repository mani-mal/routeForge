"""P3c-3: ranges in the scenario comparison (spec §9.2, §10.2; U1, U4, U11)."""
from __future__ import annotations

import json

from app.eval.scenarios import (NO_RANKING_NOTE, RANGE_NOT_RANKING_NOTE, comparison_doc,
                                render_comparison)
from app.eval.uncertainty import METHOD, UNCERTAINTY_CAVEAT, UncertaintyPlan
from tests.eval.sim_fixtures import make_row
from tests.eval.test_scenarios_compare import (_INPUTS, _TABLE_KEYS, _TRIAD, _compare, _doc,
                                               _keys, _snap)
from tests.eval.test_uncertainty import BANNED

PLAN = UncertaintyPlan(replicates=120)


def _rows(n: int = 90) -> list[dict]:
    """Two apps, three sizes, three days; every row a different prompt size
    so costs differ row by row."""
    return [make_row(f"r{i:03d}", "claims" if i % 2 else "chat",
                     complexity=[0.05, 0.6, 1.0][i % 3],
                     prompt_tokens=[200, 1200, 3000][i % 3] + 7 * i,
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:{i % 50:02d}+00:00")
            for i in range(n)]


def _document(doc, *, rows=None, plan=PLAN):
    kw = {} if plan is None else {"uncertainty": plan}
    return comparison_doc(_compare(_rows() if rows is None else rows, _snap(), doc, **kw),
                          inputs=_INPUTS)


def test_u1_a_plan_adds_one_key_and_changes_no_other():                    # U1
    """With a plan the document is the plan-less document plus `uncertainty`.
    Every other key -- the table, each scenario's summary, the baseline, the
    id -- is the same object, value for value."""
    plain = _document(_doc(*_TRIAD), plan=None)
    ranged = _document(_doc(*_TRIAD))
    assert "uncertainty" not in plain
    assert set(ranged) == set(plain) | {"uncertainty"}
    without = {k: v for k, v in ranged.items() if k != "uncertainty"}
    assert json.dumps(without, sort_keys=True) == json.dumps(plain, sort_keys=True)
    for row in ranged["table"]:
        assert set(row) == _TABLE_KEYS


def test_block_head_and_scenarios_in_file_order():
    block = _document(_doc(*_TRIAD))["uncertainty"]
    assert (block["method"], block["replicates"], block["interval"], block["seed"],
            block["min_rows"]) == (METHOD, 120, 0.9, 0, 30)
    assert [entry["name"] for entry in block["scenarios"]] == [n for n, _ in _TRIAD]
    for entry in block["scenarios"]:
        assert set(entry) == {"name", "totals", "by_app", "per_day", "quality_posterior",
                              "points_outside_interval", "totals_strata", "table"}
        assert set(entry["by_app"]) == {"chat", "claims"} and entry["totals"]["n"] == 90


def test_every_table_range_is_about_the_figure_in_that_column():
    """`uncertainty.scenarios[i].table[key].point` is `table[i][key]`."""
    doc = _document(_doc(*_TRIAD))
    for row, entry in zip(doc["table"], doc["uncertainty"]["scenarios"]):
        assert entry["name"] == row["name"]
        ranges = entry["table"]
        assert set(ranges) == _TABLE_KEYS - {"name", "overflow_hops", "affected_apps"}
        for key, node in ranges.items():
            assert node["point"] == row[key], (row["name"], key)


def test_u4_an_empty_scenario_has_every_range_at_zero():                   # U4
    doc = _document(_doc(("empty", {}), _TRIAD[0]))
    empty, changed = doc["uncertainty"]["scenarios"]
    for key in ("cost_per_day_delta_usd", "cost_delta_usd", "route_changed", "newly_rejected",
                "newly_served", "latency_p50_delta_ms", "latency_p95_delta_ms",
                "quality_mean_delta"):
        node = empty["table"][key]
        assert (node["point"], node["lo"], node["hi"]) == (0, 0, 0), key
    cost = changed["table"]["cost_delta_usd"]
    assert cost["lo"] < cost["hi"]


def test_u4_a_scenario_gets_the_same_ranges_alone_and_among_others():      # U4
    """Every scenario is resampled with the same draws, so a scenario's
    block does not depend on which other scenarios are in the file, or on
    their order."""
    name, body = _TRIAD[1]
    alone = _document(_doc((name, body)))["uncertainty"]["scenarios"][0]
    forward = _document(_doc(*_TRIAD))["uncertainty"]["scenarios"]
    backward = _document(_doc(*reversed(_TRIAD)))["uncertainty"]["scenarios"]
    assert alone == forward[1] == backward[1]
    assert {e["name"]: e for e in forward} == {e["name"]: e for e in backward}
    # The shared current arm has one range, whatever the scenario.
    currents = [e["totals"]["cost_usd_both_routed"]["current"] for e in forward]
    assert currents[0] == currents[1] == currents[2]


def test_u11_ranges_add_no_ranking_key_and_keep_file_order():              # U11
    doc = _document(_doc(*_TRIAD))
    assert not [k for k in _keys(doc) if any(word in k.lower() for word in BANNED)]
    names = [entry["name"] for entry in doc["uncertainty"]["scenarios"]]
    assert names == [n for n, _ in _TRIAD] == [row["name"] for row in doc["table"]]
    lows = [e["table"]["cost_delta_usd"]["lo"] for e in doc["uncertainty"]["scenarios"]]
    assert lows != sorted(lows) and lows != sorted(lows, reverse=True)    # not in cost order


def test_report_puts_ranges_in_the_table_and_explains_them():
    doc = _document(_doc(*_TRIAD))
    report = render_comparison(doc)
    for row, entry in zip(doc["table"], doc["uncertainty"]["scenarios"]):
        line = next(l for l in report.split("\n") if l.startswith(f"| {row['name']} |"))
        node = entry["table"]["cost_delta_usd"]
        assert f"[{node['lo']:.6g}, {node['hi']:.6g}]" in line
        assert line.count("[") == 8                     # the eight figures with a range
    table, section = report.index("## Comparison"), report.index("## Uncertainty ranges")
    first_scenario = report.index("## Scenario: ")
    assert table < report.index(NO_RANKING_NOTE) < section < first_scenario
    for line in UNCERTAINTY_CAVEAT.split("\n"):
        assert "> " + line in report
    assert RANGE_NOT_RANKING_NOTE in report
    assert report.count("### Ranges") == 3
    assert report.count("Quality uncertainty covers") == 3
    assert "Per-app ranges are in `comparison.json`." in report
    positions = [report.index(f"- **{n}** -- ") for n, _ in _TRIAD]
    assert positions == sorted(positions)               # file order


def test_report_without_a_plan_is_unchanged():
    report = render_comparison(_document(_doc(*_TRIAD), plan=None))
    assert "Uncertainty" not in report and "### Ranges" not in report
    assert "[" not in report.split("## Comparison")[1].split("## Scenario:")[0]


def test_u7_too_few_rows_prints_insufficient_data_and_no_brackets():       # U7
    doc = _document(_doc(*_TRIAD), rows=_rows(12))
    for entry in doc["uncertainty"]["scenarios"]:
        assert entry["totals"] == {"insufficient_data": True, "n": 12}
        assert entry["table"] == {"insufficient_data": True, "n": 12}
    report = render_comparison(doc)
    assert "[" not in report.split("## Comparison")[1].split("## Uncertainty ranges")[0]
    assert report.count("-- insufficient data (n = 12)") == 3
    assert report.count("### Ranges\n\ninsufficient data (n = 12)") == 3


def test_a_small_app_is_named_under_each_scenario():
    plan = UncertaintyPlan(replicates=40, min_rows=46)       # 45 rows per app
    report = render_comparison(_document(_doc(_TRIAD[0]), plan=plan))
    assert ("Per-app ranges are in `comparison.json`. Insufficient data: 2 app(s): "
            "chat (n = 45), claims (n = 45).") in report
