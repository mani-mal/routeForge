"""P3c-4: capacity in the scenario comparison (spec §7.2, §8.2; invariants
C1, C5, C8-C10, C17)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.eval.capacity import (NO_RANGE_NOTE, NO_REMEDY_NOTE, PEAK_COLUMN_NOTE, CapacityPlan,
                               capacity_block, caveat_lines)
from app.eval.outcomes import _round6
from app.eval.scenarios import (NO_RANKING_NOTE, SCHEMA, ScenarioError, compare,
                                comparison_doc, render_comparison)
from app.eval.uncertainty import UncertaintyPlan
from tests.eval.sim_fixtures import BASE_SETTINGS
from tests.eval.test_capacity import (CAPACITY_BANNED, LIMITS, QUIET_THEN_BUSY, keys_of,
                                      mixed_traffic, one_endpoint, replay, three_endpoints,
                                      traffic)

_TRIO = (("no-change", {}),
         ("twice-the-traffic", {"workload": {"volume_scale": 2}}),
         ("second-box", {"capacity": {"local-cheap": {"max_concurrency": 2}}}))
_PLAN = CapacityPlan(limits=LIMITS)


def _file(*scenarios) -> dict:
    return {"schema": SCHEMA, "title": "Can the box carry it",
            "scenarios": [{"name": name, "proposal": body} for name, body in scenarios]}


def _compare(rows, snapshot, doc, **kw):
    return asyncio.run(compare(rows, snapshot, doc, base=BASE_SETTINGS, **kw))


def _doc(rows=None, snapshot=None, scenarios=_TRIO, **kw) -> dict:
    kw.setdefault("capacity", _PLAN)
    cmp = _compare(rows or traffic(QUIET_THEN_BUSY), snapshot or one_endpoint(),
                   _file(*scenarios), **kw)
    return comparison_doc(cmp, inputs={"title": "Can the box carry it"})


def test_c1_without_a_plan_nothing_is_added():                                         # C1
    doc = _doc(capacity=None)
    assert "capacity" not in doc
    assert not [key for row in doc["table"] for key in row if key.startswith("peak_")]
    text = render_comparison(doc)
    assert "apacity" not in text and "peak load" not in text


def test_c1_a_plan_adds_one_key_and_three_table_keys_and_changes_nothing_else():       # C1
    plain, loaded = _doc(capacity=None), _doc()
    assert set(loaded) - set(plain) == {"capacity"}
    for before, after in zip(plain["table"], loaded["table"]):
        assert set(after) - set(before) == {"peak_utilisation", "peak_endpoint", "peak_limit"}
        assert {k: after[k] for k in before} == before
    stripped = {k: v for k, v in loaded.items() if k not in ("capacity", "table")}
    assert stripped == {k: v for k, v in plain.items() if k != "table"}


def test_c17_each_scenario_is_the_single_proposal_block_in_file_order():               # C17
    rows, snapshot = traffic(QUIET_THEN_BUSY), one_endpoint()
    doc = _doc(rows, snapshot)
    assert [entry["name"] for entry in doc["capacity"]["scenarios"]] == [n for n, _ in _TRIO]
    for entry, (name, body) in zip(doc["capacity"]["scenarios"], _TRIO):
        results, excluded = replay(rows, snapshot, body)
        alone = _round6(capacity_block(results, _PLAN, body, excluded=excluded))
        assert {k: v for k, v in entry.items() if k != "name"} == alone, name


def test_c17_the_current_arm_is_the_same_in_every_scenario():                          # C17
    scenarios = _doc()["capacity"]["scenarios"]
    first = scenarios[0]
    for entry in scenarios[1:]:
        assert entry["peak"]["current"] == first["peak"]["current"]
        assert entry["window"] == first["window"] and entry["rows"] == first["rows"]
        assert (entry["endpoints"]["local-cheap"]["current"]
                == first["endpoints"]["local-cheap"]["current"])


def test_c5_the_empty_scenario_has_the_same_load_in_both_arms():                       # C5
    entry = _doc()["capacity"]["scenarios"][0]
    assert entry["name"] == "no-change"
    assert entry["peak"]["proposed"] == entry["peak"]["current"]
    endpoint = entry["endpoints"]["local-cheap"]
    assert endpoint["proposed"] == endpoint["current"]


def test_the_table_carries_the_proposed_arms_most_loaded_limit():
    doc = _doc()
    rows = {row["name"]: row for row in doc["table"]}
    assert (rows["no-change"]["peak_utilisation"], rows["no-change"]["peak_endpoint"],
            rows["no-change"]["peak_limit"]) == (2.4, "local-cheap", "output_tokens_per_minute")
    assert rows["twice-the-traffic"]["peak_utilisation"] == 4.8
    assert rows["second-box"]["peak_utilisation"] == 2.4       # output tokens still bind
    text = render_comparison(doc)
    header = next(line for line in text.split("\n") if line.startswith("| scenario |"))
    assert header.endswith("| affected apps | peak load (proposed) |")
    assert "| none | 240.0% (local-cheap, output_tokens_per_minute) |" in text
    assert "| none | 480.0% (local-cheap, output_tokens_per_minute) |" in text


def test_c10_the_table_stays_in_file_order_and_nothing_ranks():                        # C10
    """The most loaded scenario is first in the file and stays first; no key
    orders the scenarios by load, and no sentence prescribes anything."""
    reordered = (_TRIO[1], _TRIO[2], _TRIO[0])
    doc = _doc(scenarios=reordered)
    assert [row["name"] for row in doc["table"]] == [n for n, _ in reordered]
    assert [e["name"] for e in doc["capacity"]["scenarios"]] == [n for n, _ in reordered]
    words = keys_of(doc["capacity"]) + [k for row in doc["table"] for k in row]
    assert not [w for w in words
                if any(b in w.lower() for b in CAPACITY_BANNED)]
    text = render_comparison(doc)
    order = [text.index(f"## Scenario: {name}") for name, _ in reordered]
    assert order == sorted(order)
    assert NO_RANKING_NOTE in text and PEAK_COLUMN_NOTE in text
    section = text[text.index("## Capacity"):text.index("## Scenario: ")]
    assert not [word for word in CAPACITY_BANNED if word in section.lower()]


def test_the_report_has_one_capacity_section_and_one_per_scenario():
    text = render_comparison(_doc())
    assert text.count("\n## Capacity\n") == 1 and text.count("\n### Capacity\n") == 3
    top = text[text.index("\n## Capacity\n"):]
    lines = top.split("\n")
    assert lines[3:13] == caveat_lines() and lines[14] == NO_REMEDY_NOTE
    assert ("Current arm peak load (the same for every scenario): 240.0% (local-cheap, "
            "output_tokens_per_minute).") in top
    second = text[text.index("## Scenario: second-box"):]
    assert "#### local-cheap" in second
    assert "| max_concurrency | proposed | 2 | 60.0% |" in second
    assert "| max_concurrency | current | 1 | 120.0% |" in second
    twice = text[text.index("## Scenario: twice-the-traffic"):text.index("## Scenario: second-box")]
    assert "Volume scale assumed by the proposal" in twice and "*=2." in twice
    assert NO_RANGE_NOTE not in text


def test_c8_a_short_window_fills_the_cell_with_the_state():                            # C8
    doc = _doc(rows=traffic([3] * 4))
    for row in doc["table"]:
        assert (row["peak_utilisation"], row["peak_endpoint"], row["peak_limit"]) == (
            None, None, None)
    assert all(entry["window_too_short"] for entry in doc["capacity"]["scenarios"])
    text = render_comparison(doc)
    assert text.count("| window too short (4 of the 10 buckets needed) |") == 3
    assert "Current arm peak load (the same for every scenario): window too short" in text
    capacity_text = text[text.index("\n## Capacity\n"):]
    assert "%" not in capacity_text.split("## Scenario:")[0]


def test_a_scenario_with_no_limit_anywhere_says_not_stated():
    doc = _doc(scenarios=(("no-change", {}),), capacity=CapacityPlan())
    assert doc["table"][0]["peak_utilisation"] is None
    assert "| none | not stated |" in render_comparison(doc)


def test_c9_ranges_and_capacity_together_leave_capacity_as_points():                   # C9
    """Capacity is identical with and without `--uncertainty`; the ranges
    are identical with and without a capacity plan; the capacity cell has
    no bracket; and the section says why."""
    ranged = UncertaintyPlan(replicates=40, min_rows=30)
    both, capacity_only = _doc(uncertainty=ranged), _doc()
    ranges_only = _doc(uncertainty=ranged, capacity=None)
    assert both["capacity"] == capacity_only["capacity"]
    assert both["uncertainty"] == ranges_only["uncertainty"]
    assert "capacity" not in json.dumps(both["uncertainty"])
    text = render_comparison(both)
    assert text.count(NO_RANGE_NOTE) == 1
    cells = [line.rsplit("|", 2)[1] for line in text.split("\n")
             if line.startswith("| twice-the-traffic |")]
    assert cells == [" 480.0% (local-cheap, output_tokens_per_minute) "]


def test_c9_volume_scale_does_not_move_a_range_even_with_small_apps_pooled():          # C9
    """`volume_scale` thickens demand for the capacity section only. The
    bootstrap still resamples the recorded rows, with small applications
    pooled as before -- a scenario that only scales volume has every delta
    range at exactly zero."""
    rows = mixed_traffic()                    # app-b has 30 rows; min_rows 40 pools it
    ranged = UncertaintyPlan(replicates=40, min_rows=40)
    doc = _doc(rows, three_endpoints(), scenarios=(
        ("no-change", {}),
        ("ten-times", {"workload": {"volume_scale": 10, "apps": {"app-b": {"volume_scale": 50}}}}),
    ), uncertainty=ranged, capacity=CapacityPlan(limits={"local-mid": {"max_concurrency": 1.0}}))
    plain, scaled = doc["uncertainty"]["scenarios"]
    assert plain["totals_strata"]["pooled_apps"] == 1
    assert {k: v for k, v in scaled.items() if k != "name"} == {
        k: v for k, v in plain.items() if k != "name"}
    delta = scaled["totals"]["cost_usd"]["delta"]
    assert (delta["point"], delta["lo"], delta["hi"]) == (0.0, 0.0, 0.0)
    loads = {entry["name"]: entry for entry in doc["capacity"]["scenarios"]}
    assert (loads["ten-times"]["peak"]["proposed"]["utilisation"]
            > 10 * loads["no-change"]["peak"]["proposed"]["utilisation"] > 0)
    assert doc["table"][1]["route_changed"] == 0 and doc["table"][1]["cost_delta_usd"] == 0.0


@pytest.mark.parametrize("body", [{"capacity": {"local-typo": {"max_concurrency": 2}}},
                                  {"capacity": {"local-cheap": {"max_concurrency": 0}}}])
def test_a_bad_capacity_in_a_scenario_names_the_scenario(body):
    with pytest.raises(ScenarioError) as err:
        _doc(scenarios=(("no-change", {}), ("bad", body)))
    assert err.value.where.startswith("scenarios[1](bad).proposal.capacity.local-")
