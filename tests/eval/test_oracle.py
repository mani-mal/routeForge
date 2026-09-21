"""Tests for `app.eval.oracle` (Task 4, FeasibleRouteBench).

Every scenario is a hand-computed matrix -- no dependency on the pipeline,
registry, or any other app/eval module. See
docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md §6 for the
binding definitions this module implements, and §14 for the worked example
`TestSpec14Fixture` reproduces.
"""
from __future__ import annotations

import pytest

from app.eval.oracle import (
    CaseInfo,
    Choice,
    MatrixRow,
    case_oracle,
    grouped,
    headline,
    strategy_metrics,
    violations,
    worst_groups,
)


def _row(case_id, endpoint, quality, passed, cost_usd, latency_ms, error=None) -> MatrixRow:
    return MatrixRow(
        case_id=case_id, endpoint=endpoint, quality=quality, passed=passed,
        cost_usd=cost_usd, latency_ms=latency_ms, error=error)


def _case(case_id, eligible=(), expect_empty=False, rejected_ok=False, tags=None) -> CaseInfo:
    return CaseInfo(
        case_id=case_id, eligible=tuple(eligible), expect_empty=expect_empty,
        rejected_ok=rejected_ok, tags=tags or {})


def _choice(case_id, strategy, endpoint) -> Choice:
    return Choice(case_id=case_id, strategy=strategy, endpoint=endpoint)


# ---------------------------------------------------------------------------
# §14 fixture: Local Llama / Azure US / European / General US (retains)
# ---------------------------------------------------------------------------

class TestSpec14Fixture:
    """local-llama (q .92, $0.08, 900ms) and azure-us ($0.03, 600ms) are
    eligible for a US on-prem-preferred claim; european ($0.02, 500ms) and
    general-us (retains) are not -- they are never swept, so they never
    appear in `rows`. Floor is 0.9: both eligible endpoints pass, so
    `c* = azure-us` at $0.03."""

    ROWS = [
        _row("claim-001", "local-llama", 0.92, True, 0.08, 900.0),
        _row("claim-001", "azure-us", 0.95, True, 0.03, 600.0),
    ]
    CASE = _case("claim-001", eligible=("local-llama", "azure-us"))

    def test_oracle_picks_azure_us_as_cost_and_latency_star(self):
        oracle = case_oracle(self.ROWS)
        assert oracle["q_star"] == 0.95
        assert oracle["c_star"] == 0.03
        assert oracle["l_star"] == 600.0
        assert oracle["passing"] == ["azure-us", "local-llama"]
        assert oracle["infeasible_at_floor"] is False

    def test_choosing_local_llama_has_cost_regret_005(self):
        choices = [_choice("claim-001", "s", "local-llama")]
        metrics = strategy_metrics([self.CASE], self.ROWS, choices, "s")
        assert metrics["cost_regret"] == pytest.approx(0.05)
        assert metrics["violation_rate"] == 0.0
        assert metrics["floor_miss_rate"] == 0.0
        assert metrics["quality_regret"] == pytest.approx(0.95 - 0.92)

    def test_choosing_european_is_a_violation(self):
        # european was never swept (not eligible), so it has no matrix row
        # either -- the choice fails on eligibility alone.
        choices = [_choice("claim-001", "s", "european")]
        metrics = strategy_metrics([self.CASE], self.ROWS, choices, "s")
        assert metrics["violation_rate"] == 1.0
        # A violating choice scores quality 0 and contributes no cost/latency
        # regret (it never passed).
        assert metrics["quality_regret"] == pytest.approx(0.95)
        assert metrics["cost_regret"] is None
        assert metrics["latency_regret"] is None

    def test_violations_helper_flags_ineligible_choice_and_sweep_call(self):
        choices = [_choice("claim-001", "s", "european")]
        assert violations([self.CASE], choices, sweep_served=[]) == 1
        # A sweep call to a non-eligible slug (a harness bug, per global
        # constraints "no eligibility bypass") is also flagged, even with a
        # clean choice.
        clean_choices = [_choice("claim-001", "s", "azure-us")]
        served = [("claim-001", "azure-us"), ("claim-001", "european")]
        assert violations([self.CASE], clean_choices, served) == 1


# ---------------------------------------------------------------------------
# Floor misses
# ---------------------------------------------------------------------------

class TestFloorMiss:
    """cheap-fail is cheaper than mid-pass but fails the floor; mid-pass
    passes. A strategy that picks the cheap failer must show a floor miss,
    and its cost regret must never go negative (never averaged over a
    floor miss at all)."""

    ROWS = [
        _row("c1", "cheap-fail", 0.5, False, 0.01, 200.0),
        _row("c1", "mid-pass", 0.95, True, 0.05, 400.0),
    ]
    CASE = _case("c1", eligible=("cheap-fail", "mid-pass"))

    def test_oracle_ignores_the_failing_endpoint_for_c_star(self):
        oracle = case_oracle(self.ROWS)
        assert oracle["q_star"] == 0.95
        assert oracle["c_star"] == 0.05
        assert oracle["passing"] == ["mid-pass"]
        assert oracle["infeasible_at_floor"] is False

    def test_picking_the_cheap_failer_is_a_pure_floor_miss(self):
        choices = [_choice("c1", "s", "cheap-fail")]
        metrics = strategy_metrics([self.CASE], self.ROWS, choices, "s")
        assert metrics["floor_miss_rate"] == 1.0
        assert metrics["pass_rate"] == 0.0
        assert metrics["violation_rate"] == 0.0  # cheap-fail IS eligible
        # Not computed for this case -- cost_regret has no data at all
        # (its only case is a floor miss), so it must be None, never a
        # (wrongly) negative number.
        assert metrics["cost_regret"] is None
        assert metrics["latency_regret"] is None

    def test_floor_miss_case_is_excluded_from_a_mixed_average(self):
        # A second, cleanly-passing case in the same suite must carry the
        # whole cost_regret average by itself.
        rows = self.ROWS + [
            _row("c2", "only-endpoint", 0.99, True, 0.10, 300.0),
        ]
        cases = [self.CASE, _case("c2", eligible=("only-endpoint",))]
        choices = [
            _choice("c1", "s", "cheap-fail"),
            _choice("c2", "s", "only-endpoint"),
        ]
        metrics = strategy_metrics(cases, rows, choices, "s")
        # only-endpoint IS c*(c2) (its only passing candidate), so its own
        # regret is 0 -- the average is exactly 0, not "no data".
        assert metrics["cost_regret"] == pytest.approx(0.0)
        assert metrics["floor_miss_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# All endpoints fail -> infeasible-at-floor
# ---------------------------------------------------------------------------

class TestInfeasibleAtFloor:
    ROWS = [
        _row("c1", "a", 0.3, False, 0.01, 100.0),
        _row("c1", "b", 0.4, False, 0.02, 150.0),
    ]
    CASE = _case("c1", eligible=("a", "b"))

    def test_oracle_reports_infeasible_with_no_passing_endpoint(self):
        oracle = case_oracle(self.ROWS)
        assert oracle["passing"] == []
        assert oracle["c_star"] is None
        assert oracle["l_star"] is None
        assert oracle["infeasible_at_floor"] is True
        assert oracle["q_star"] == 0.4  # still defined: max over ALL rows

    def test_only_quality_regret_counts(self):
        choices = [_choice("c1", "s", "b")]  # the best of a bad set
        metrics = strategy_metrics([self.CASE], self.ROWS, choices, "s")
        assert metrics["infeasible_at_floor"] == 1
        assert metrics["quality_regret"] == pytest.approx(0.4 - 0.4)
        # No passing set to miss -> floor_miss_rate has no denominator.
        assert metrics["floor_miss_rate"] is None
        assert metrics["cost_regret"] is None
        assert metrics["latency_regret"] is None
        assert metrics["pass_rate"] == 0.0


# ---------------------------------------------------------------------------
# Error rows
# ---------------------------------------------------------------------------

class TestErrorRows:
    """An error row (spec §8): quality 0, passed False, cost 0, latency
    None. It must never poison an oracle's l_star / c_star (computed only
    over passing rows) and must never crash latency aggregation."""

    ROWS = [
        _row("c1", "broken", 0.0, False, 0.0, None, error="TimeoutError: boom"),
        _row("c1", "good", 0.9, True, 0.04, 500.0),
    ]
    CASE = _case("c1", eligible=("broken", "good"))

    def test_error_row_excluded_from_c_star_and_l_star(self):
        oracle = case_oracle(self.ROWS)
        assert oracle["passing"] == ["good"]
        assert oracle["c_star"] == 0.04
        assert oracle["l_star"] == 500.0

    def test_choosing_the_broken_endpoint_is_a_floor_miss_not_a_crash(self):
        choices = [_choice("c1", "s", "broken")]
        metrics = strategy_metrics([self.CASE], self.ROWS, choices, "s")
        assert metrics["floor_miss_rate"] == 1.0
        assert metrics["quality_regret"] == pytest.approx(0.9)
        assert metrics["cost_regret"] is None
        assert metrics["latency_regret"] is None
        assert metrics["mean_chosen_cost"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# None choice on a non-empty case
# ---------------------------------------------------------------------------

class TestNoneChoiceOnNonEmptyCase:
    ROWS = [
        _row("c1", "only", 0.8, True, 0.02, 100.0),
    ]
    CASE = _case("c1", eligible=("only",))

    def test_none_choice_is_violation_free_and_scores_quality_zero(self):
        choices = [_choice("c1", "s", None)]
        metrics = strategy_metrics([self.CASE], self.ROWS, choices, "s")
        assert metrics["violation_rate"] == 0.0
        assert metrics["quality_regret"] == pytest.approx(0.8)
        assert metrics["floor_miss_rate"] == 1.0
        assert metrics["pass_rate"] == 0.0

    def test_missing_choice_entirely_behaves_like_none(self):
        metrics = strategy_metrics([self.CASE], self.ROWS, choices=[], strategy="s")
        assert metrics["violation_rate"] == 0.0
        assert metrics["quality_regret"] == pytest.approx(0.8)


class TestNoneChoiceOnEmptyCase:
    CASE = _case("c1", eligible=(), expect_empty=True, rejected_ok=True)

    def test_none_choice_is_correct_and_free(self):
        choices = [_choice("c1", "s", None)]
        metrics = strategy_metrics([self.CASE], rows=[], choices=choices, strategy="s")
        assert metrics["violation_rate"] == 0.0
        assert metrics["empty_set_mismatches"] == 0
        # No non-empty cases at all -> every regret-shaped aggregate is
        # undefined, not zero.
        assert metrics["pass_rate"] is None
        assert metrics["quality_regret"] is None

    def test_non_none_choice_on_empty_case_is_a_violation(self):
        choices = [_choice("c1", "s", "anything")]
        metrics = strategy_metrics([self.CASE], rows=[], choices=choices, strategy="s")
        assert metrics["violation_rate"] == 1.0


# ---------------------------------------------------------------------------
# Empty-set mismatches, both directions, plus the rejected_ok clause
# ---------------------------------------------------------------------------

class TestEmptySetMismatches:
    def test_expect_empty_but_actually_eligible_is_a_mismatch(self):
        case = _case("c1", eligible=("x",), expect_empty=True, rejected_ok=False)
        rows = [_row("c1", "x", 0.9, True, 0.01, 100.0)]
        metrics = strategy_metrics([case], rows, [], "s")
        assert metrics["empty_set_mismatches"] == 1

    def test_expect_nonempty_but_actually_empty_is_a_mismatch(self):
        case = _case("c1", eligible=(), expect_empty=False, rejected_ok=True)
        metrics = strategy_metrics([case], [], [], "s")
        assert metrics["empty_set_mismatches"] == 1

    def test_correctly_empty_and_rejected_ok_is_not_a_mismatch(self):
        case = _case("c1", eligible=(), expect_empty=True, rejected_ok=True)
        metrics = strategy_metrics([case], [], [], "s")
        assert metrics["empty_set_mismatches"] == 0

    def test_empty_and_expected_but_not_rejected_ok_is_still_a_mismatch(self):
        # expect_empty agrees with actual emptiness, but the case was not
        # cleanly rejected (e.g. a model call slipped through) -- still a
        # mismatch, per the "plus empty cases not rejected_ok" clause.
        case = _case("c1", eligible=(), expect_empty=True, rejected_ok=False)
        metrics = strategy_metrics([case], [], [], "s")
        assert metrics["empty_set_mismatches"] == 1

    def test_double_failure_counts_once_not_twice(self):
        case = _case("c1", eligible=(), expect_empty=False, rejected_ok=False)
        metrics = strategy_metrics([case], [], [], "s")
        assert metrics["empty_set_mismatches"] == 1


# ---------------------------------------------------------------------------
# grouped / worst_groups over two workloads
# ---------------------------------------------------------------------------

class TestGroupedAndWorstGroups:
    """Two workloads: 'claim' cases route well (small regret); 'faq' cases
    route badly (large regret) -- the worst group per metric must name
    'workload' / 'faq'."""

    CASES = [
        _case("claim-1", eligible=("a",), tags={"workload": "claim"}),
        _case("claim-2", eligible=("a",), tags={"workload": "claim"}),
        _case("faq-1", eligible=("a", "b"), tags={"workload": "faq"}),
    ]
    ROWS = [
        _row("claim-1", "a", 0.95, True, 0.05, 200.0),
        _row("claim-2", "a", 0.95, True, 0.05, 200.0),
        _row("faq-1", "a", 0.95, True, 0.05, 200.0),
        _row("faq-1", "b", 0.10, False, 0.01, 50.0),
    ]

    def test_grouped_reports_one_entry_per_workload(self):
        choices = [
            _choice("claim-1", "s", "a"),
            _choice("claim-2", "s", "a"),
            _choice("faq-1", "s", "b"),  # picks the bad, cheap, failing one
        ]
        groups = grouped(self.CASES, self.ROWS, choices, "s", "workload")
        assert set(groups) == {"claim", "faq"}
        assert groups["claim"]["quality_regret"] == pytest.approx(0.0)
        assert groups["claim"]["n_cases"] == 2
        assert groups["faq"]["quality_regret"] == pytest.approx(0.95 - 0.10)
        assert groups["faq"]["n_cases"] == 1

    def test_worst_group_names_faq_for_quality_regret(self):
        choices = [
            _choice("claim-1", "s", "a"),
            _choice("claim-2", "s", "a"),
            _choice("faq-1", "s", "b"),
        ]
        worst = worst_groups(self.CASES, self.ROWS, choices, "s")
        tag, value, metric_value = worst["quality_regret"]
        assert (tag, value) == ("workload", "faq")
        assert metric_value == pytest.approx(0.85)
        # faq-1's choice never passed, so it contributes no cost/latency
        # regret data at all -- 'claim' (whose choice always equals its own
        # c*/l*, i.e. regret 0) is the only group with a defined value, and
        # so is trivially "worst" for these two metrics.
        assert worst["cost_regret"] == ("workload", "claim", 0.0)
        assert worst["latency_regret"] == ("workload", "claim", 0.0)

    def test_all_groups_clean_yields_no_worst_regret(self):
        choices = [
            _choice("claim-1", "s", "a"),
            _choice("claim-2", "s", "a"),
            _choice("faq-1", "s", "a"),
        ]
        worst = worst_groups(self.CASES, self.ROWS, choices, "s")
        assert worst["quality_regret"] == ("workload", "claim", 0.0) or \
            worst["quality_regret"] == ("workload", "faq", 0.0)
        # Either way the magnitude is 0 -- ties are deterministic but which
        # tag/value wins a true 0-0 tie isn't the property under test.
        assert worst["quality_regret"][2] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# headline: comparable vs not
# ---------------------------------------------------------------------------

class TestHeadline:
    def test_comparable_when_pass_rates_are_close(self):
        metrics_by_strategy = {
            "router_warm": {"pass_rate": 0.90, "mean_chosen_cost": 0.03},
            "best_published_quality": {"pass_rate": 0.91, "mean_chosen_cost": 0.10},
        }
        result = headline(
            metrics_by_strategy, router="router_warm",
            reference="best_published_quality", parity_tol=0.02)
        assert result["comparable"] is True
        assert result["saving_pct"] == pytest.approx((0.10 - 0.03) / 0.10 * 100)

    def test_not_comparable_when_pass_rates_diverge(self):
        metrics_by_strategy = {
            "router_warm": {"pass_rate": 0.60, "mean_chosen_cost": 0.03},
            "best_published_quality": {"pass_rate": 0.95, "mean_chosen_cost": 0.10},
        }
        result = headline(
            metrics_by_strategy, router="router_warm",
            reference="best_published_quality", parity_tol=0.02)
        assert result["comparable"] is False
        assert result["saving_pct"] is None
        assert result["router_pass_rate"] == 0.60
        assert result["reference_pass_rate"] == 0.95

    def test_not_comparable_when_reference_cost_is_zero(self):
        metrics_by_strategy = {
            "router_warm": {"pass_rate": 0.90, "mean_chosen_cost": 0.03},
            "reference": {"pass_rate": 0.90, "mean_chosen_cost": 0.0},
        }
        result = headline(
            metrics_by_strategy, router="router_warm",
            reference="reference", parity_tol=0.02)
        assert result["comparable"] is False
        assert result["saving_pct"] is None

    def test_not_comparable_when_a_pass_rate_is_undefined(self):
        metrics_by_strategy = {
            "router_warm": {"pass_rate": None, "mean_chosen_cost": 0.03},
            "reference": {"pass_rate": 0.90, "mean_chosen_cost": 0.10},
        }
        result = headline(
            metrics_by_strategy, router="router_warm",
            reference="reference", parity_tol=0.02)
        assert result["comparable"] is False
        assert result["saving_pct"] is None


# ---------------------------------------------------------------------------
# Rounding and case_oracle input validation
# ---------------------------------------------------------------------------

class TestRoundingAndValidation:
    def test_floats_are_rounded_to_12dp(self):
        rows = [
            _row("c1", "a", 1 / 3, True, 1 / 7, 100 / 3),
        ]
        oracle = case_oracle(rows)
        assert oracle["q_star"] == round(1 / 3, 12)
        assert oracle["c_star"] == round(1 / 7, 12)
        assert oracle["l_star"] == round(100 / 3, 12)

    def test_case_oracle_rejects_empty_rows(self):
        with pytest.raises(ValueError):
            case_oracle([])

    def test_case_oracle_rejects_mixed_case_ids(self):
        rows = [
            _row("c1", "a", 0.9, True, 0.01, 100.0),
            _row("c2", "b", 0.9, True, 0.01, 100.0),
        ]
        with pytest.raises(ValueError):
            case_oracle(rows)

    def test_missing_matrix_row_counts_as_a_violation(self):
        # Endpoint is eligible but the sweep somehow produced no row for
        # it -- a harness bug, per the interface docstring, but must not
        # crash and must be counted.
        case = _case("c1", eligible=("ghost",))
        choices = [_choice("c1", "s", "ghost")]
        metrics = strategy_metrics([case], rows=[], choices=choices, strategy="s")
        assert metrics["violation_rate"] == 1.0
