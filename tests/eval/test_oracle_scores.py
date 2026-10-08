"""Per-case scores in `app.eval.oracle` (P2 evaluation metrics, Task 2).

`strategy_metrics` is now `aggregate_scores(case_scores(...))`, rounded. The
existing `tests/eval/test_oracle.py` and the golden pin
(`tests/eval/test_bench_golden.py`) show the aggregate did not move; these
tests pin the per-case form that the group report and the router-version
check are built on, against a matrix computed by hand.
"""
from __future__ import annotations

from app.eval.oracle import (
    CaseInfo,
    CaseScore,
    Choice,
    MatrixRow,
    aggregate_scores,
    case_scores,
    metric_support,
    strategy_metrics,
)

S = "s"


def _row(case_id, endpoint, quality, cost, latency, *, error=None) -> MatrixRow:
    return MatrixRow(case_id=case_id, endpoint=endpoint, quality=quality,
                     passed=error is None and quality >= 1.0, cost_usd=cost,
                     latency_ms=latency, error=error,
                     counts_unhealth=True if error else None,
                     failure_latency_ms=40.0 if error else None)


def _case(case_id, eligible, *, expect_empty=False, rejected_ok=False) -> CaseInfo:
    return CaseInfo(case_id=case_id, eligible=tuple(eligible), expect_empty=expect_empty,
                    rejected_ok=rejected_ok, tags={})


# Five cases, two endpoints `a` (cheap, slow) and `b` (dear, fast).
#
#   c1  a passes (cost 1, 300 ms), b passes (cost 3, 100 ms); chosen b
#         -> pass, quality regret 0, cost regret 3-1 = 2, latency regret 0
#   c2  a fails the floor (q 0), b passes (cost 3, 100 ms); chosen a
#         -> floor miss, quality regret 1, no cost or latency regret
#   c3  a errors, b passes (cost 3, 100 ms); chain (a, b)
#         -> pass, cost regret 0, latency 40 + 100 - 100 = 40, one wasted attempt
#   c4  a and b both fail the floor; chosen a
#         -> infeasible at floor: quality regret 0, no floor-miss entry
#   c5  empty feasible set, rejected correctly, no chain
CASES = [
    _case("c1", ["a", "b"]), _case("c2", ["a", "b"]), _case("c3", ["a", "b"]),
    _case("c4", ["a", "b"]), _case("c5", [], expect_empty=True, rejected_ok=True),
]
ROWS = [
    _row("c1", "a", 1.0, 1.0, 300.0), _row("c1", "b", 1.0, 3.0, 100.0),
    _row("c2", "a", 0.0, 1.0, 300.0), _row("c2", "b", 1.0, 3.0, 100.0),
    _row("c3", "a", 0.0, 0.0, None, error="ConnectionError: down"),
    _row("c3", "b", 1.0, 3.0, 100.0),
    _row("c4", "a", 0.0, 1.0, 300.0), _row("c4", "b", 0.0, 3.0, 100.0),
]
CHOICES = [
    Choice("c1", S, "b", ("b",)), Choice("c2", S, "a", ("a",)),
    Choice("c3", S, "b", ("a", "b")), Choice("c4", S, "a", ("a",)),
    Choice("c5", S, None, ()),
]


def _scores() -> dict[str, CaseScore]:
    return {s.case_id: s for s in case_scores(CASES, ROWS, CHOICES, S)}


def test_one_score_per_case_in_the_order_given():
    assert [s.case_id for s in case_scores(CASES, ROWS, CHOICES, S)] == [
        "c1", "c2", "c3", "c4", "c5"]
    assert [s.case_id for s in case_scores(CASES[::-1], ROWS, CHOICES, S)] == [
        "c5", "c4", "c3", "c2", "c1"]


def test_each_case_matches_the_hand_computed_matrix():
    s = _scores()
    assert s["c1"] == CaseScore("c1", False, False, False, False, 1.0, 0.0, 0.0, 2.0, 0.0,
                                3.0, 0.0, 0.0)
    assert s["c2"] == CaseScore("c2", False, False, False, False, 0.0, 1.0, 1.0, None, None,
                                1.0, 0.0, 0.0)
    assert s["c3"] == CaseScore("c3", False, False, False, False, 1.0, 0.0, 0.0, 0.0, 40.0,
                                3.0, 1.0, 0.0)
    # Infeasible at floor: q* is 0, so no quality regret and no floor-miss entry.
    assert s["c4"] == CaseScore("c4", False, False, False, True, 0.0, 0.0, None, None, None,
                                1.0, 0.0, 0.0)
    # An empty case is outside every quality, cost and latency denominator.
    assert s["c5"] == CaseScore("c5", True, False, False, False, None, None, None, None,
                                None, None, None, None)


def test_the_aggregate_is_the_hand_computed_one():
    m = aggregate_scores(case_scores(CASES, ROWS, CHOICES, S))
    assert m["n_cases"] == 5
    assert m["violation_rate"] == 0.0
    assert m["pass_rate"] == 2 / 4                 # c1, c3 of the four non-empty cases
    assert m["quality_regret"] == 1 / 4            # c2 only
    assert m["floor_miss_rate"] == 1 / 3           # c2 of c1, c2, c3 (c4 has P = {})
    assert m["cost_regret"] == (2.0 + 0.0) / 2     # c1, c3: the passing choices only
    assert m["latency_regret"] == (0.0 + 40.0) / 2
    assert m["mean_chosen_cost"] == (3.0 + 1.0 + 3.0 + 1.0) / 4
    assert m["wasted_attempts"] == 1 / 4
    assert m["exhausted_rate"] == 0.0
    assert m["infeasible_at_floor"] == 1
    assert m["empty_set_mismatches"] == 0


def test_strategy_metrics_is_the_rounded_aggregate():
    scores = case_scores(CASES, ROWS, CHOICES, S)
    assert strategy_metrics(CASES, ROWS, CHOICES, S) == {
        k: (round(v, 12) if isinstance(v, float) else v)
        for k, v in aggregate_scores(scores).items()}


def test_each_metric_reports_its_own_denominator():
    assert metric_support(case_scores(CASES, ROWS, CHOICES, S)) == {
        "violation_rate": 5, "pass_rate": 4, "quality_regret": 4, "cost_regret": 2,
        "latency_regret": 2, "floor_miss_rate": 3, "mean_chosen_cost": 4,
        "wasted_attempts": 4, "exhausted_rate": 4}


def test_a_subset_of_scores_aggregates_like_a_subset_of_cases():
    """What the group report relies on: scoring every case once and
    aggregating a subset gives exactly what scoring that subset gives."""
    scores = _scores()
    for subset in (["c1", "c2"], ["c3", "c5"], ["c4"], ["c5"], []):
        cases = [c for c in CASES if c.case_id in subset]
        assert (aggregate_scores([scores[i] for i in subset])
                == aggregate_scores(case_scores(cases, ROWS, CHOICES, S)))


def test_an_empty_list_has_no_rate_and_no_mean():
    m = aggregate_scores([])
    assert m["n_cases"] == 0 and m["violation_rate"] is None and m["pass_rate"] is None
    assert metric_support([])["violation_rate"] == 0


def test_a_violating_chain_scores_like_no_choice_and_is_flagged():
    choices = [Choice("c1", S, "zzz", ("zzz",))]
    (score,) = case_scores(CASES[:1], ROWS, choices, S)
    assert score.violation is True
    assert (score.passed, score.quality_regret, score.chosen_cost) == (0.0, 1.0, 0.0)
    assert score.cost_regret is None and score.latency_regret is None


def test_any_attempt_on_an_empty_case_is_a_violation():
    choices = [Choice("c5", S, "a", ("a",))]
    (score,) = case_scores(CASES[4:], ROWS, choices, S)
    assert score.empty is True and score.violation is True
    assert score.passed is None


def test_an_empty_case_that_was_not_rejected_is_a_mismatch_not_a_violation():
    case = _case("c5", [], expect_empty=True, rejected_ok=False)
    (score,) = case_scores([case], ROWS, [Choice("c5", S, None, ())], S)
    assert score.empty_mismatch is True and score.violation is False
