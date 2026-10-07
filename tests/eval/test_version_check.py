"""The offline router-version check (`app/eval/version_check.py`; spec §7;
invariants E7-E10, E13, E14, E17). Pure: every test builds `CaseScore`s by
hand; the end-to-end tests are in `test_version_check_run.py`.

Two examples are computed by hand.

**A constant shift.** Ten cases; the candidate costs exactly 0.25 more on
every case. Whatever cases a resample draws, the paired difference of the two
means is 0.25, so the range is `[0.25, 0.25]`: worsening 0.25, `change`
"worse". Against a tolerance of 0.25 that is within tolerance (the boundary
is inclusive); against 0.2 it is regressed.

**Six cases, one of them much dearer.** Baseline cost 1 on every case; the
candidate costs 10 on `k1` and 0 on the other five. Worsening of the mean
cost: `(10 + 0x5)/6 - 1 = 4/6`. A resample of six cases misses `k1` with
probability `(5/6)^6 = 0.335`, and then the difference is -1: the 90% range
starts at -1 and includes 0. So the figure is `regressed` against a
tolerance of 0.5 (the point estimate decides) AND `change` is
"not_detectable" (the range includes 0). Neither overrides the other.
"""
from __future__ import annotations

import json
import random

import pytest

from app.eval import version_check
from app.eval.groups import (CAVEAT_SIMULATED, CAVEAT_SYNTHETIC, CAVEAT_TIER_RULE,
                             CAVEAT_TO_MEASURE, INSUFFICIENT)
from app.eval.oracle import CaseScore
from app.eval.version_check import (
    CHECK_CONDITIONAL,
    CHECK_METRICS,
    CHECK_NO_EFFECT,
    CHECK_NO_TOLERANCE,
    CHECK_RANGE,
    CheckPlan,
    change_of,
    compare,
    decide,
    parse_tolerances,
    render_check,
    run_check,
    verdict_of,
    worsening,
)

ARMS = ("router_online", "router_online:candidate")
CAND = {"id": "abc123abc123", "name": "t", "routing": {"weights": {"latency": 0.6}}}


def _score(case_id, *, passed=1.0, cost=1.0, violation=False, empty=False) -> CaseScore:
    if empty:
        return CaseScore(case_id, True, violation, False, False, None, None, None, None,
                         None, None, None, None)
    return CaseScore(case_id, False, violation, False, False, passed, 1.0 - passed,
                     1.0 - passed, 0.0 if passed else None, 0.0 if passed else None,
                     cost, 0.0, 0.0)


def _ids(n, prefix="k") -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


def _keys(ids, workload=lambda i: "w") -> dict:
    return {i: {"placement": "any", "protection": "enforced", "application": "app",
                "workload": workload(i)} for i in ids}


def _check(base, cand, *, tolerances=None, keys=None, run_exit_code=0, chains=None,
           **plan) -> dict:
    ids = [s.case_id for s in base]
    return run_check(base, cand, keys or _keys(ids), chains or ({}, {}),
                     CheckPlan(tolerances=tolerances or {}, **plan), arms=ARMS,
                     candidate_doc=CAND, run_exit_code=run_exit_code, mode="simulated")


# --- the decision rule (E10, E17) ----------------------------------------------

def test_e10_worsening_is_signed_so_that_positive_is_worse():
    assert worsening("mean_chosen_cost", 1.0, 1.5) == 0.5      # dearer
    assert worsening("latency_regret", 30.0, 10.0) == -20.0    # faster
    assert worsening("pass_rate", 0.9, 0.8) == pytest.approx(0.1)   # fewer passes
    assert worsening("pass_rate", 0.8, 0.9) == pytest.approx(-0.1)
    assert worsening("cost_regret", None, 1.0) is None
    assert worsening("cost_regret", 1.0, None) is None


@pytest.mark.parametrize("w, hi, tol, state", [
    (None, None, 0.1, "undefined"),
    (0.2, 0.3, 0.1, "regressed"),
    (0.2, None, 0.1, "regressed"),          # the point decides, range or no range
    (0.05, 0.2, 0.1, "inconclusive"),       # the point is inside, the range is not
    (0.05, None, 0.1, "inconclusive"),      # no range: nothing shows it is inside
    (0.05, 0.1, 0.1, "within_tolerance"),   # the boundary is inclusive
    (0.1, 0.1, 0.1, "within_tolerance"),
    (-0.3, -0.1, 0.0, "within_tolerance"),  # an improvement
    (0.0, 0.0, 0.0, "within_tolerance"),
])
def test_e10_the_decision_rule_branch_by_branch(w, hi, tol, state):
    assert decide(w, hi, tol) == state


def test_e10_the_tolerance_boundary_is_not_decided_by_float_noise():
    w = 0.92 - 0.90
    assert w > 0.02                                   # 0.020000000000000018
    assert decide(w, w, 0.02) == "within_tolerance"


def test_e17_the_lower_end_of_a_range_never_enters_the_decision():
    """`decide` does not take `lo` at all; `change_of` does not take a
    tolerance. A range that includes 0 therefore decides nothing by itself."""
    assert change_of(False, -0.1, 0.3) == "not_detectable"
    assert change_of(False, 0.0, 0.3) == "not_detectable"
    assert change_of(False, 0.1, 0.3) == "worse"
    assert change_of(False, -0.3, -0.1) == "better"
    assert change_of(False, None, None) == "undefined"
    assert change_of(True, 0.0, 0.0) == "none"


def test_e10_a_constant_shift_has_a_degenerate_range_by_hand():
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, cost=1.25) for i in _ids(10)]
    plan = CheckPlan(tolerances={"mean_chosen_cost": 0.25})
    f = compare(base, cand, "overall", plan)["mean_chosen_cost"]
    assert (f["baseline"], f["candidate"]) == (1.0, 1.25)
    assert (f["worsening"], f["lo"], f["hi"]) == (0.25, 0.25, 0.25)
    assert f["change"] == "worse" and f["state"] == "within_tolerance"
    assert f["support"] == {"baseline": 10, "candidate": 10}
    tighter = CheckPlan(tolerances={"mean_chosen_cost": 0.2})
    assert compare(base, cand, "overall", tighter)["mean_chosen_cost"]["state"] == "regressed"


def test_e17_a_regression_whose_range_includes_zero_is_both_things():
    base = [_score(i) for i in _ids(6)]
    cand = [_score(i, cost=10.0 if i == "k01" else 0.0) for i in _ids(6)]
    plan = CheckPlan(tolerances={"mean_chosen_cost": 0.5})
    f = compare(base, cand, "overall", plan)["mean_chosen_cost"]
    assert f["worsening"] == pytest.approx(4 / 6)
    assert f["lo"] == -1.0 and f["hi"] > 0.5
    assert f["change"] == "not_detectable" and f["state"] == "regressed"
    assert _check(base, cand, tolerances={"mean_chosen_cost": 0.5})["verdict"] == "regressed"


def test_every_range_value_is_a_difference_some_resample_can_have():
    """Two cases, per-case cost difference 0 and 1: a resample's difference
    is 0, 0.5 or 1 and nothing else."""
    base = [_score("a"), _score("b")]
    cand = [_score("a"), _score("b", cost=2.0)]
    f = compare(base, cand, "overall", CheckPlan(min_group_size=1))["mean_chosen_cost"]
    assert f["worsening"] == 0.5
    assert {f["lo"], f["hi"]} <= {0.0, 0.5, 1.0} and f["lo"] <= 0.5 <= f["hi"]


def test_the_range_widens_with_the_level():
    base = [_score(i) for i in _ids(12)]
    cand = [_score(i, cost=1.0 + (n % 4)) for n, i in enumerate(_ids(12))]
    narrow = compare(base, cand, "overall", CheckPlan(interval=0.5))["mean_chosen_cost"]
    wide = compare(base, cand, "overall", CheckPlan(interval=0.95))["mean_chosen_cost"]
    assert wide["lo"] <= narrow["lo"] <= narrow["worsening"] <= narrow["hi"] <= wide["hi"]
    assert wide["lo"] < wide["hi"]


def test_the_draw_is_paired_and_seeded():
    base = [_score(i) for i in _ids(12)]
    cand = [_score(i, cost=1.0 + (n % 4)) for n, i in enumerate(_ids(12))]
    one = compare(base, cand, "overall", CheckPlan(seed=1))
    assert one == compare(base, cand, "overall", CheckPlan(seed=1))
    two = compare(base, cand, "overall", CheckPlan(seed=2))
    assert (one["mean_chosen_cost"]["lo"], one["mean_chosen_cost"]["hi"]) != (
        two["mean_chosen_cost"]["lo"], two["mean_chosen_cost"]["hi"])
    # A metric both arms agree on case by case has no spread at all: the two
    # arms were drawn together, not each on its own.
    assert (one["pass_rate"]["lo"], one["pass_rate"]["hi"]) == (0.0, 0.0)
    assert one["pass_rate"]["change"] == "none"


def test_a_figure_undefined_in_some_resample_has_no_range_and_cannot_pass():
    """Cost regret is over the passing cases. With one passing case in five,
    a third of the resamples draw none: the figure is undefined there, so it
    has no range, and a figure with no range is never within tolerance."""
    ids = _ids(5)
    base = [_score(i, passed=float(i == "k01")) for i in ids]
    plan = CheckPlan(tolerances={"cost_regret": 1.0}, min_group_size=1)
    f = compare(base, list(base), "overall", plan)["cost_regret"]
    assert f["support"] == {"baseline": 1, "candidate": 1}
    assert (f["worsening"], f["lo"], f["hi"]) == (0.0, None, None)
    assert f["state"] == "inconclusive"
    # Every case agrees between the arms, and `change` still says so.
    assert f["change"] == "none"


# --- identical arms (E8) -------------------------------------------------------

def test_e8_identical_arms_are_no_change_everywhere():
    scores = [_score(i, passed=float(n % 3 != 0), cost=1.0 + n)
              for n, i in enumerate(_ids(12))]
    tolerances = {m: 0.0 for m in CHECK_METRICS}
    doc = _check(scores, list(scores), tolerances=tolerances)
    assert doc["verdict"] == "pass" and doc["exit_code"] == 0
    assert doc["regressed"] == [] and doc["inconclusive"] == []
    for metric, f in doc["overall"].items():
        assert f["baseline"] == f["candidate"], metric
        assert (f["worsening"], f["lo"], f["hi"]) == (0.0, 0.0, 0.0), metric
        assert f["change"] == "none" and f["state"] == "within_tolerance", metric


# --- case order (E9) -----------------------------------------------------------

def test_e9_the_document_does_not_depend_on_case_order():
    ids = _ids(14)
    base = [_score(i, passed=float(n % 4 != 0), cost=1.0 + n % 3)
            for n, i in enumerate(ids)]
    cand = [_score(i, passed=float(n % 5 != 0), cost=1.0 + n % 2)
            for n, i in enumerate(ids)]
    keys = _keys(ids, workload=lambda i: "a" if int(i[1:]) <= 7 else "b")
    tolerances = {"pass_rate": 0.05, "mean_chosen_cost": 0.1}
    first = _check(base, cand, tolerances=tolerances, keys=keys)
    rng = random.Random(5)
    for _ in range(3):
        b2, c2 = list(base), list(cand)
        rng.shuffle(b2)
        rng.shuffle(c2)                    # the two arms in DIFFERENT orders
        k2 = dict(sorted(keys.items(), key=lambda kv: rng.random()))
        assert _check(b2, c2, tolerances=tolerances, keys=k2) == first


def test_arms_that_do_not_cover_the_same_cases_are_refused():
    with pytest.raises(ValueError, match="same cases"):
        compare([_score("a"), _score("b")], [_score("a"), _score("c")], "overall",
                CheckPlan())


# --- hard checks (E7) ----------------------------------------------------------

@pytest.mark.parametrize("tolerances", [
    {}, {m: 1e9 for m in CHECK_METRICS}, {"pass_rate": 0.0}])
def test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances(tolerances):
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, violation=(i == "k03")) for i in _ids(10)]
    doc = _check(base, cand, tolerances=tolerances)
    assert doc["verdict"] == "hard_fail" and doc["exit_code"] == 2
    assert doc["hard"] == {"failed": True, "candidate_violations": 1,
                           "candidate_served_on_empty": 0, "run_exit_code": 0}


def test_e7_an_attempt_on_an_empty_feasible_set_is_counted_as_such():
    base = [*(_score(i) for i in _ids(9)), _score("z", empty=True)]
    cand = [*(_score(i) for i in _ids(9)), _score("z", empty=True, violation=True)]
    doc = _check(base, cand, tolerances={"pass_rate": 1.0})
    assert doc["verdict"] == "hard_fail"
    assert doc["hard"]["candidate_violations"] == 1
    assert doc["hard"]["candidate_served_on_empty"] == 1


def test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate():
    scores = [_score(i) for i in _ids(10)]
    doc = _check(scores, list(scores), tolerances={"pass_rate": 1.0}, run_exit_code=2)
    assert doc["verdict"] == "hard_fail" and doc["exit_code"] == 2
    assert doc["hard"]["candidate_violations"] == 0 and doc["hard"]["run_exit_code"] == 2


def test_a_baseline_violation_alone_does_not_fail_the_candidate():
    """The baseline arm's own violation reaches the check through the run's
    exit code (the bench exits 2 for any strategy), not through this count."""
    base = [_score(i, violation=(i == "k01")) for i in _ids(10)]
    cand = [_score(i) for i in _ids(10)]
    assert _check(base, cand)["hard"]["failed"] is False


# --- verdicts -------------------------------------------------------------------

@pytest.mark.parametrize("hard, states, named, verdict", [
    (True, ["regressed"], True, "hard_fail"),
    (False, ["within_tolerance", "regressed", "inconclusive"], True, "regressed"),
    (False, ["within_tolerance", "inconclusive"], True, "inconclusive"),
    (False, ["within_tolerance", "undefined"], True, "inconclusive"),
    (False, ["within_tolerance", INSUFFICIENT], True, "inconclusive"),
    (False, ["within_tolerance"], True, "pass"),
    (False, [], False, "no_hard_failure"),
])
def test_the_verdict_is_the_worst_state(hard, states, named, verdict):
    assert verdict_of(hard, states, named) == verdict


def test_the_exit_codes():
    assert (version_check.EXIT_PASS, version_check.EXIT_HARD_FAIL,
            version_check.EXIT_REGRESSED, version_check.EXIT_INCONCLUSIVE) == (0, 2, 3, 4)
    assert version_check._EXIT == {"pass": 0, "no_hard_failure": 0, "hard_fail": 2,
                                   "regressed": 3, "inconclusive": 4}


def test_no_tolerance_named_judges_nothing_and_says_so():
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, cost=99.0) for i in _ids(10)]
    doc = _check(base, cand)
    assert doc["verdict"] == "no_hard_failure" and doc["exit_code"] == 0
    assert all(f["state"] == "not_checked" for f in doc["overall"].values())
    assert doc["caveats"][-1] == CHECK_NO_TOLERANCE
    assert doc["overall"]["mean_chosen_cost"]["worsening"] == 98.0     # shown, not judged
    assert "NO HARD FAILURE" in render_check(doc)


def test_an_inconclusive_figure_is_not_a_pass():
    """k01 costs 10 more; the mean worsens by 1.0 <= 1.5, but a resample
    that draws k01 twice is at 2.0 or more: the range reaches past it."""
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, cost=11.0 if i == "k01" else 1.0) for i in _ids(10)]
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 1.5})
    f = doc["overall"]["mean_chosen_cost"]
    assert f["worsening"] == 1.0 and f["hi"] > 1.5 and f["state"] == "inconclusive"
    assert doc["verdict"] == "inconclusive" and doc["exit_code"] == 4
    assert {"scope": "overall", "metric": "mean_chosen_cost",
            "state": "inconclusive"} in doc["inconclusive"]


# --- groups ---------------------------------------------------------------------

def _two_groups():
    ids = _ids(10)
    in_a = lambda i: int(i[1:]) <= 5                               # noqa: E731
    base = [_score(i) for i in ids]
    cand = [_score(i, cost=2.0 if in_a(i) else 0.0) for i in ids]
    return base, cand, _keys(ids, workload=lambda i: "a" if in_a(i) else "b")


def test_a_regression_in_one_group_fails_when_overall_is_unchanged():
    """Group a costs 1 more on every case, group b 1 less: the overall mean
    cost is unchanged; group a's is worse by exactly 1."""
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys)
    assert doc["overall"]["mean_chosen_cost"]["worsening"] == 0.0
    assert doc["overall"]["mean_chosen_cost"]["state"] == "inconclusive"
    a = doc["groups"]["workload"]["a"]["metrics"]["mean_chosen_cost"]
    assert (a["worsening"], a["lo"], a["hi"], a["state"]) == (1.0, 1.0, 1.0, "regressed")
    b = doc["groups"]["workload"]["b"]["metrics"]["mean_chosen_cost"]
    assert (b["worsening"], b["state"]) == (-1.0, "within_tolerance")
    assert doc["verdict"] == "regressed" and doc["exit_code"] == 3
    assert doc["regressed"] == [{"scope": "workload=a", "metric": "mean_chosen_cost",
                                 "state": "regressed"}]


def test_e5_a_group_below_the_minimum_is_listed_and_not_judged():
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys,
                 min_group_size=6)
    assert doc["groups"]["workload"]["a"] == {"n_cases": 5, "state": INSUFFICIENT}
    assert doc["regressed"] == []                       # group a was not judged
    assert doc["verdict"] == "inconclusive"             # the overall figure still is
    assert "| workload=a | all | n/a | n/a | insufficient cases (n = 5) |" in (
        render_check(doc))


def test_e5_a_thin_figure_is_skipped_in_a_group_and_inconclusive_overall():
    """Cost regret is over the passing cases only. Ten cases, four passing:
    below a minimum of five the figure has no rate. In a group that is
    "not judged"; overall, a named metric that cannot be judged cannot pass."""
    ids = _ids(10)
    base = [_score(i, passed=float(n < 4)) for n, i in enumerate(ids)]
    doc = _check(base, list(base), tolerances={"cost_regret": 1.0})
    f = doc["overall"]["cost_regret"]
    assert f["state"] == INSUFFICIENT and f["support"] == {"baseline": 4, "candidate": 4}
    assert f["worsening"] is None and f["lo"] is None
    assert f["baseline"] is None and f["candidate"] is None      # no figure at all
    assert doc["verdict"] == "inconclusive"
    assert doc["inconclusive"] == [{"scope": "overall", "metric": "cost_regret",
                                    "state": INSUFFICIENT}]
    # The same thin figure inside each (sufficiently large) group is skipped.
    assert doc["groups"]["workload"]["w"]["metrics"]["cost_regret"]["state"] == INSUFFICIENT


def test_changed_counts_the_cases_whose_attempt_chain_differs():
    scores = [_score(i) for i in _ids(5)]
    chains = ({"k01": ("a",), "k02": ("a", "b"), "k03": ("a",), "k04": (), "k05": ("b",)},
              {"k01": ("a",), "k02": ("a",), "k03": ("b",), "k04": (), "k05": ("b",)})
    doc = _check(scores, list(scores), chains=chains)
    assert doc["changed"] == {"n_cases": 5, "choice_changed": 2, "share": 0.4}


# --- inputs ---------------------------------------------------------------------

def test_tolerances_parse_from_metric_equals_value():
    assert parse_tolerances(["pass_rate=0.02", " cost_regret = 1e-4"]) == {
        "pass_rate": 0.02, "cost_regret": 1e-4}
    assert parse_tolerances([]) == {}


@pytest.mark.parametrize("items, message", [
    (["pass_rate"], "METRIC=VALUE"),
    (["=0.1"], "METRIC=VALUE"),
    (["pass_rate=lots"], "not a number"),
    (["pass_rate=0.1", "pass_rate=0.2"], "given twice"),
])
def test_a_malformed_tolerance_is_refused(items, message):
    with pytest.raises(ValueError, match=message):
        parse_tolerances(items)


@pytest.mark.parametrize("kwargs, message", [
    ({"tolerances": {"accuracy": 0.1}}, "unknown metric"),
    ({"tolerances": {"pass_rate": -0.1}}, "finite number >= 0"),
    ({"tolerances": {"pass_rate": float("nan")}}, "finite number >= 0"),
    ({"tolerances": {"pass_rate": float("inf")}}, "finite number >= 0"),
    ({"tolerances": {"pass_rate": True}}, "finite number >= 0"),
    ({"replicates": 0}, "replicates"),
    ({"replicates": 20001}, "replicates"),
    ({"interval": 1.0}, "interval"),
    ({"interval": 0}, "interval"),
    ({"seed": -1}, "seed"),
    ({"min_group_size": 0}, "min_group_size"),
])
def test_a_bad_plan_is_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        CheckPlan(**kwargs)


# --- the fixed text (E13, E14) -------------------------------------------------

def test_e14_the_check_text_is_the_spec_text():
    assert CHECK_NO_EFFECT == (
        "This check applies nothing and promotes nothing. It reads a candidate routing "
        "config, scores it offline beside the current one, and writes this report. A "
        "routing change is still made by an operator editing config/routing.yaml.")
    assert CHECK_CONDITIONAL == (
        "The verdict is conditional on this suite, this endpoint file, this simulation "
        "seed and this case order. It is not a statement about live traffic.")
    assert CHECK_RANGE == (
        "A range is the spread of the paired difference when the suite's cases are "
        "resampled. It says how much the difference depends on which cases are in the "
        "suite, and nothing about whether the suite resembles real traffic.")
    assert CHECK_NO_TOLERANCE == (
        "No tolerance was named, so only the hard checks were applied: the differences "
        "below are shown, not judged.")


def test_e14_every_report_opens_with_the_caveats_in_order():
    scores = [_score(i) for i in _ids(10)]
    doc = _check(scores, list(scores), tolerances={"pass_rate": 0.0})
    assert doc["caveats"] == [CHECK_NO_EFFECT, CAVEAT_SIMULATED, CAVEAT_TIER_RULE,
                              CAVEAT_SYNTHETIC, CAVEAT_TO_MEASURE, CHECK_CONDITIONAL,
                              CHECK_RANGE]
    lines = render_check(doc).splitlines()
    assert lines[0] == "# Router-version check (offline)"
    assert lines[2:9] == [f"> {text}" for text in doc["caveats"]]
    assert doc["effect"] == "none"
    assert "**PASS** (exit 0)" in render_check(doc)


_FORBIDDEN = ("rank", "recommend", "best", "winner", "prefer", "should", "relax",
              "vendor", "deploy", "activate", "rollout")


def _keys_of(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys_of(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys_of(v)}
    return set()


def test_e13_no_key_or_sentence_ranks_recommends_or_promotes():
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys)
    names = " ".join(sorted(_keys_of(doc))).lower()
    text = render_check(doc).lower()
    for word in _FORBIDDEN:
        assert word not in names, word
        assert word not in text, word
    # "promote" appears exactly once in a report: in the sentence that says
    # this check does not do it. No key says it at all.
    for word in ("promot", "apply", "applied"):
        assert word not in names, word
    assert text.count("promot") == 1 and "promotes nothing" in text


def test_the_document_is_json_clean_and_rounded():
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys)
    assert json.loads(json.dumps(doc, sort_keys=True)) == doc
    assert doc["schema"] == "routeforge.router-version-check/v1"
    assert doc["plan"] == {"method": "paired_case_bootstrap",
                           "tolerances": {"mean_chosen_cost": 0.5}, "replicates": 1000,
                           "interval": 0.9, "seed": 0, "min_group_size": 5}
    assert doc["arms"] == {"baseline": ARMS[0], "candidate": ARMS[1]}
    assert doc["candidate"] == CAND
    assert "None" not in render_check(doc)
