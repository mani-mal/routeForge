"""The group report (`app/eval/groups.py`; spec §4; invariants E3-E6, E13,
E14). Pure: every test builds `CaseScore`s by hand.

The hand-computed example (eight cases, two workloads):

    case  workload  placement    passed  q-regret  floor-miss  cost-regret  lat-regret
    c1    faq       any          1       0         0           0.2          10
    c2    faq       any          1       0         0           0.0          0
    c3    faq       any          0       1         1           -            -
    c4    faq       any          1       0         0           0.4          30
    c5    faq       onprem_only  1       0         0           0.0          0
    c6    claim     onprem_only  1       0         0           0.6          60
    c7    claim     onprem_only  0       1         1           -            -
    c8    claim     onprem_only  0       1         -           -            -     (P = {})

    pass_rate    faq 4/5 = 0.8     claim 1/3            overall 5/8
    cost_regret  faq 0.6/4 = 0.15  claim 0.6/1 = 0.6    overall 1.2/5 = 0.24

`5 x 0.8 + 3 x (1/3) = 5`, over 8: the overall pass rate is the combination
of the groups weighted by each figure's OWN denominator. Cost regret shows
why the weight cannot be the case count: `(4 x 0.15 + 1 x 0.6) / 5 = 0.24`
is right, `(5 x 0.15 + 3 x 0.6) / 8 = 0.31875` is not.
"""
from __future__ import annotations

import json

import pytest

from app.eval import groups
from app.eval.groups import (
    CAVEAT_GROUPS,
    CAVEAT_LABELS,
    CAVEAT_LIVE,
    CAVEAT_SIMULATED,
    CAVEAT_SYNTHETIC,
    CAVEAT_TIER_RULE,
    CAVEAT_TO_MEASURE,
    CAVEAT_WORST,
    DIMENSIONS,
    GROUP_METRICS,
    INSUFFICIENT,
    UNTAGGED,
    application_key,
    bench_caveats,
    case_keys,
    cell,
    group_report,
    metric_value,
    partition,
    render_group_report,
    strategy_groups,
)
from app.eval.oracle import CaseScore


def _score(case_id, passed, *, floor_miss="same", cost=None, latency=None,
           violation=False, empty=False, mismatch=False) -> CaseScore:
    """A non-empty case by default: quality regret and floor miss are `1 -
    passed` unless `floor_miss` says otherwise (None = P(c) is empty)."""
    if empty:
        return CaseScore(case_id, True, violation, mismatch, False, None, None, None,
                         None, None, None, None, None)
    miss = (1.0 - passed) if floor_miss == "same" else floor_miss
    return CaseScore(case_id, False, violation, mismatch, miss is None, float(passed),
                     1.0 - passed, miss, cost, latency, 1.0, 0.0, 0.0)


SCORES = [
    _score("c1", 1, cost=0.2, latency=10.0),
    _score("c2", 1, cost=0.0, latency=0.0),
    _score("c3", 0),
    _score("c4", 1, cost=0.4, latency=30.0),
    _score("c5", 1, cost=0.0, latency=0.0),
    _score("c6", 1, cost=0.6, latency=60.0),
    _score("c7", 0),
    _score("c8", 0, floor_miss=None),
]
KEYS = {
    s.case_id: {
        "workload": "faq" if s.case_id <= "c5" else "claim",
        "placement": "any" if s.case_id <= "c4" else "onprem_only",
        "protection": "enforced",
        "application": "app-a" if s.case_id in ("c1", "c2", "c6") else "app-b",
    } for s in SCORES}


def _doc(min_size=3, *, scores=None, keys=None, mode="simulated") -> dict:
    return group_report({"s": scores or SCORES}, keys or KEYS, min_size=min_size,
                        mode=mode)


# --- which group a case is in (E3) ---------------------------------------------

def test_application_is_the_app_tag_or_the_policy_fingerprint():
    assert application_key({"app": "billing"}, {"residency": "any"}) == "billing"
    a = application_key({}, {"residency": "any", "policy_mode": "enforced"})
    b = application_key({}, {"policy_mode": "enforced", "residency": "any"})
    c = application_key({}, {"residency": "onprem_only", "policy_mode": "enforced"})
    assert a == b and a != c                      # key order does not matter; values do
    assert a.startswith("policy-") and len(a) == len("policy-") + 8
    # An empty or non-string tag is not an application name.
    assert application_key({"app": ""}, {}) == application_key({"app": True}, {})


def test_a_missing_tag_is_the_untagged_group():
    keys = case_keys({"workload": "faq"}, {})
    assert keys["workload"] == "faq"
    assert keys["placement"] == UNTAGGED and keys["protection"] == UNTAGGED
    assert set(keys) == set(DIMENSIONS)
    # A boolean tag value is a label like any other.
    assert case_keys({"protection": True}, {})["protection"] == "True"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_e3_every_case_is_in_exactly_one_group_per_dimension(dimension):
    ids = [s.case_id for s in SCORES]
    parts = partition(ids, KEYS, dimension)
    members = [i for group in parts.values() for i in group]
    assert sorted(members) == sorted(ids) and len(members) == len(set(members))
    assert list(parts) == sorted(parts)


def test_e3_group_sizes_sum_to_the_total():
    doc = _doc()
    for dim in DIMENSIONS:
        assert sum(doc["group_sizes"][dim].values()) == doc["n_cases"] == 8
        blocks = doc["strategies"]["s"]["dimensions"][dim]
        assert sum(b["n_cases"] for b in blocks.values()) == 8
        assert {k: b["n_cases"] for k, b in blocks.items()} == doc["group_sizes"][dim]


def test_e3_an_untagged_case_is_counted_not_dropped():
    keys = {**KEYS, "c8": {**KEYS["c8"], "workload": UNTAGGED}}
    doc = _doc(keys=keys)
    assert doc["group_sizes"]["workload"] == {UNTAGGED: 1, "claim": 2, "faq": 5}


# --- the hand-computed example (E6) --------------------------------------------

def test_e6_group_figures_match_the_hand_computed_example():
    workload = _doc()["strategies"]["s"]["dimensions"]["workload"]
    faq, claim = workload["faq"]["metrics"], workload["claim"]["metrics"]
    assert faq["pass_rate"] == {"value": 0.8, "n": 5}
    assert faq["quality_regret"] == {"value": 0.2, "n": 5}
    assert faq["floor_miss_rate"] == {"value": 0.2, "n": 5}
    assert faq["cost_regret"] == {"value": 0.15, "n": 4}
    assert faq["latency_regret"] == {"value": 10.0, "n": 4}
    assert faq["violation_rate"] == {"value": 0.0, "n": 5}
    assert claim["pass_rate"] == {"value": round(1 / 3, 12), "n": 3}
    assert claim["quality_regret"] == {"value": round(2 / 3, 12), "n": 3}
    overall = _doc()["strategies"]["s"]["overall"]["metrics"]
    assert overall["pass_rate"] == {"value": 0.625, "n": 8}
    assert overall["cost_regret"] == {"value": 0.24, "n": 5}


def test_e6_the_worst_group_is_named_with_its_size():
    worst = _doc()["strategies"]["s"]["worst"]
    # Lowest pass rate: claim (1/3), not the onprem_only placement (2/4).
    assert worst["pass_rate"] == {
        "dimension": "workload", "group": "claim", "value": round(1 / 3, 12), "n": 3,
        "n_cases": 3, "groups_total": 7, "groups_printed": 7, "groups_at_value": 1,
        "tied": ["workload=claim"]}
    # Highest cost regret among the PRINTED figures: claim's 0.6 is over one
    # case and is not printed; application app-a is (0.2 + 0 + 0.6) / 3.
    assert worst["cost_regret"]["group"] == "app-a"
    assert worst["cost_regret"]["dimension"] == "application"
    assert worst["cost_regret"]["value"] == round(0.8 / 3, 12)


def test_e6_no_group_is_named_when_every_printed_group_is_equal():
    doc = _doc()
    worst = doc["strategies"]["s"]["worst"]["violation_rate"]
    assert worst["value"] == 0.0
    assert worst["groups_at_value"] == worst["groups_printed"] == 7
    line = next(row for row in render_group_report(doc, detail="s").splitlines()
                if row.startswith("| s | violation_rate |"))
    assert line == ("| s | violation_rate | all | all | 0 in every printed group | n/a | "
                    "7 of 7 printed; 7 at this value |")


def test_e5_every_strategy_prints_its_counts_whatever_the_group_sizes():
    """A violation in a group too small to print a rate is still in
    `groups.md`, for every strategy, not only the detailed one."""
    doc = _doc()
    doc["strategies"]["other"] = json.loads(json.dumps(doc["strategies"]["s"]))
    doc["strategies"]["other"]["overall"]["violations"] = 1
    text = render_group_report(doc, detail="s")
    assert "## Counts per strategy" in text
    assert "| other | 8 | 1 | 0 |" in text
    assert "| s | 8 | 0 | 0 |" in text


def test_e6_a_tie_between_some_groups_is_listed_not_broken():
    doc = _doc()
    worst = doc["strategies"]["s"]["worst"]["pass_rate"]
    worst.update(groups_at_value=2, tied=["placement=onprem_only", "application=app-a"])
    line = next(row for row in render_group_report(doc, detail="s").splitlines()
                if row.startswith("| s | pass_rate |"))
    assert "| tied | placement=onprem_only, application=app-a |" in line
    assert line.endswith("| 7 of 7 printed; 2 at this value |")


def test_an_unknown_mode_is_refused_not_read_as_live():
    with pytest.raises(ValueError, match="mode"):
        bench_caveats("replay")


def test_e6_a_metric_no_group_prints_has_no_worst_group():
    doc = _doc(min_size=9)
    assert all(v is None for v in doc["strategies"]["s"]["worst"].values())
    assert "no group has enough cases" in render_group_report(doc, detail="s")


def test_e6_a_tie_keeps_the_first_dimension_then_label():
    scores = [_score("a1", 0), _score("a2", 1), _score("b1", 0), _score("b2", 1)]
    keys = {s.case_id: {"placement": s.case_id[0], "protection": "p",
                        "application": s.case_id[0], "workload": "w"} for s in scores}
    worst = strategy_groups(scores, keys, 1)["worst"]["pass_rate"]
    assert (worst["dimension"], worst["group"]) == ("placement", "a")


# --- the weighted combination (E4) ---------------------------------------------

@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("metric", GROUP_METRICS)
def test_e4_overall_is_the_denominator_weighted_combination(dimension, metric):
    """With every group printed (minimum 1): overall = sum(n_g x value_g) /
    sum(n_g), where n_g is the FIGURE's denominator in group g."""
    block = strategy_groups(SCORES, KEYS, 1)
    figures = [g["metrics"][metric] for g in block["dimensions"][dimension].values()]
    printed = [f for f in figures if "value" in f]
    overall = block["overall"]["metrics"][metric]
    assert sum(f["n"] for f in figures) == overall["n"]
    assert sum(f["n"] * f["value"] for f in printed) / overall["n"] == pytest.approx(
        overall["value"], abs=1e-12)


def test_e4_weighting_by_case_count_is_wrong_for_a_conditional_metric():
    block = strategy_groups(SCORES, KEYS, 1)
    workload = block["dimensions"]["workload"]
    by_cases = sum(g["n_cases"] * g["metrics"]["cost_regret"]["value"]
                   for g in workload.values()) / 8
    assert by_cases == pytest.approx(0.31875)
    assert block["overall"]["metrics"]["cost_regret"]["value"] == pytest.approx(0.24)


# --- small groups (E5) ---------------------------------------------------------

def test_e5_a_figure_below_the_minimum_prints_a_count_and_no_rate():
    claim = _doc(min_size=4)["strategies"]["s"]["dimensions"]["workload"]["claim"]
    assert claim["n_cases"] == 3
    for metric in GROUP_METRICS:
        assert "value" not in claim["metrics"][metric]
        assert claim["metrics"][metric]["state"] == INSUFFICIENT
    assert claim["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 3}
    text = render_group_report(_doc(min_size=4), detail="s")
    assert "insufficient cases (n = 3)" in text


def test_e5_the_rule_is_on_the_figures_own_denominator():
    """claim has 3 cases, enough for a pass rate at minimum 3 -- but its cost
    regret is over ONE passing case and its floor-miss rate over two."""
    claim = _doc(min_size=3)["strategies"]["s"]["dimensions"]["workload"]["claim"]
    assert "value" in claim["metrics"]["pass_rate"]
    assert claim["metrics"]["cost_regret"] == {"state": INSUFFICIENT, "n": 1}
    assert claim["metrics"]["floor_miss_rate"] == {"state": INSUFFICIENT, "n": 2}


def test_e5_the_minimum_is_inclusive():
    scores = SCORES[:5]
    assert "value" in cell(scores, "pass_rate", 5)
    assert cell(scores, "pass_rate", 6) == {"state": INSUFFICIENT, "n": 5}


def test_e5_a_violation_in_a_one_case_group_is_still_counted():
    scores = [*SCORES, _score("c9", 0, violation=True)]
    keys = {**KEYS, "c9": {"workload": "rare", "placement": "any",
                           "protection": "enforced", "application": "app-b"}}
    rare = _doc(min_size=5, scores=scores, keys=keys)[
        "strategies"]["s"]["dimensions"]["workload"]["rare"]
    assert rare["n_cases"] == 1 and rare["violations"] == 1
    assert rare["metrics"]["violation_rate"] == {"state": INSUFFICIENT, "n": 1}
    text = render_group_report(_doc(min_size=5, scores=scores, keys=keys), detail="s")
    # cases 1, violations 1, empty-set mismatches 0 -- then no rate at all.
    assert ("| workload | rare | 1 | 1 | 0 | insufficient cases (n = 1) |"
            in text)


def test_e5_an_empty_case_counts_in_the_group_and_in_no_quality_figure():
    scores = [*SCORES, _score("c9", 0, empty=True, mismatch=True)]
    keys = {**KEYS, "c9": KEYS["c1"]}
    faq = strategy_groups(scores, keys, 1)["dimensions"]["workload"]["faq"]
    assert faq["n_cases"] == 6 and faq["empty_cases"] == 1
    assert faq["empty_set_mismatches"] == 1
    assert faq["metrics"]["pass_rate"]["n"] == 5
    assert faq["metrics"]["empty_set_mismatch_rate"] == {"value": 1 / 6, "n": 6}
    assert metric_value([scores[-1]], "pass_rate") == (None, 0)


def test_a_bad_minimum_is_refused():
    for bad in (0, -1, 2.5, True, "5"):
        with pytest.raises(ValueError, match="min_size"):
            _doc(min_size=bad)


# --- the fixed text (E13, E14) -------------------------------------------------

def test_e14_the_caveats_are_the_spec_text():
    assert CAVEAT_SIMULATED == (
        "On the simulated backend, quality is an input: each pass or fail is a seeded "
        "draw against a probability written in the endpoint file. Pass rate and quality "
        "regret measure the harness, not the models.")
    assert CAVEAT_TIER_RULE == (
        "The router comparison is mostly the tier rule: the target tier is chosen from "
        "the request's length, so on short cases the router's regret against the oracle "
        "is set by which tier it may use, not by the four-term scorer.")
    assert CAVEAT_SYNTHETIC == "Cases are synthetic and single-turn."
    assert CAVEAT_GROUPS == (
        "A group is the cases of this suite that carry one label. A figure over fewer "
        "cases than the minimum prints its case count and no rate. A group above the "
        "minimum is still small: read its case count with every figure.")
    assert CAVEAT_TO_MEASURE == "Live-backend results: TO MEASURE."
    assert CAVEAT_LIVE == (
        "This run called the endpoints in the endpoint file, once per case and "
        "endpoint, and graded each answer with a deterministic grader. One call is one "
        "sample.")


def test_e14_a_simulated_report_opens_with_the_caveats_in_order():
    doc = _doc()
    assert doc["caveats"] == [CAVEAT_SIMULATED, CAVEAT_TIER_RULE, CAVEAT_SYNTHETIC,
                              CAVEAT_TO_MEASURE, CAVEAT_GROUPS, CAVEAT_WORST,
                              CAVEAT_LABELS]
    lines = render_group_report(doc, detail="s").splitlines()
    assert lines[0] == "# FeasibleRouteBench group report"
    assert lines[2:9] == [f"> {text}" for text in doc["caveats"]]


def test_e14_a_live_run_does_not_claim_quality_is_an_input():
    assert bench_caveats("live") == [CAVEAT_LIVE, CAVEAT_TIER_RULE, CAVEAT_SYNTHETIC]
    assert CAVEAT_SIMULATED not in _doc(mode="live")["caveats"]


_FORBIDDEN = ("rank", "recommend", "best", "winner", "prefer", "should", "relax",
              "promot", "vendor")


def _keys_of(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys_of(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys_of(v)}
    return set()


def test_e13_no_key_or_sentence_ranks_or_recommends():
    doc = _doc()
    text = render_group_report(doc, detail="s").lower()
    names = " ".join(sorted(_keys_of(doc))).lower()
    constants = " ".join(v for k, v in vars(groups).items()
                         if k.startswith("CAVEAT_") and isinstance(v, str)).lower()
    for word in _FORBIDDEN:
        assert word not in names, word
        assert word not in text, word
        assert word not in constants, word


# --- shape ---------------------------------------------------------------------

def test_the_document_is_deterministic_and_json_clean():
    one = json.dumps(_doc(), sort_keys=True)
    two = json.dumps(group_report({"s": list(SCORES)}, dict(reversed(list(KEYS.items()))),
                                  min_size=3, mode="simulated"), sort_keys=True)
    assert one == two
    assert json.loads(one)["schema"] == "routeforge.bench-group-report/v1"


def test_the_detail_table_is_only_for_the_named_strategy():
    doc = group_report({"s": SCORES, "t": SCORES}, KEYS, min_size=3, mode="simulated")
    text = render_group_report(doc, detail="t")
    assert "## Every group: `t`" in text and "## Every group: `s`" not in text
    assert "## Every group" not in render_group_report(doc, detail="absent")
    assert "None" not in text
