"""Feasible oracle and regret metrics for FeasibleRouteBench (spec §6).

**Pure module.** No imports of the pipeline, registry, DB, or settings --
only dataclasses and arithmetic, so every definition here can be checked
against a hand-computed matrix in a unit test. Nothing under `app/` outside
`app/eval/` may import `app.eval` (global constraints).

For a case `c` with eligible set `E(c)` and sweep outcomes:

- `q*(c) = max_{e in E} q(c,e)` -- the best quality any eligible endpoint
  reached, whether or not it passed the floor.
- `P(c) = {e in E : q(c,e) >= floor(c)}` -- the endpoints that passed. A
  `MatrixRow.passed` is already this per-row test (computed by the runner
  against that case's floor); this module trusts it rather than
  recomputing it, since it has no access to `floor(c)`.
- `c*(c) = min_{e in P} cost(c,e)`, `l*(c) = min_{e in P} latency(c,e)`;
  undefined (`None`) when `P = {}` (**infeasible-at-floor**).

Cost and latency regret are **never averaged over floor misses** -- a cheap
wrong answer must not look like a saving (global constraint). A choice of
`None` on a non-empty case is a violation-free floor miss with quality
treated as 0 (ruling, task-4 brief); a choice naming an endpoint outside
`E(c)`, or one with no matching sweep row, is a policy violation, also
scored as quality 0 for regret purposes so one bad case cannot silently
distort the aggregate.

Every float in every dict this module returns is rounded to 12 decimal
places before it is handed back, so two runs over identical inputs produce
byte-identical output (determinism ruling, task-4 brief).
"""
from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class MatrixRow:
    """One `(case, endpoint)` sweep outcome (spec §7 `matrix.jsonl`)."""

    case_id: str
    endpoint: str
    quality: float
    passed: bool
    cost_usd: float
    latency_ms: float | None      # None when error
    error: str | None = None

@dataclass(frozen=True)
class CaseInfo:
    """A case's gate/feasible-set outcome, independent of any strategy."""

    case_id: str
    eligible: tuple[str, ...]
    expect_empty: bool
    rejected_ok: bool             # True if an empty case was rejected with >=1 reason and no calls
    tags: dict

@dataclass(frozen=True)
class Choice:
    """One `(case, strategy)` decision (spec §7 `choices.jsonl`)."""

    case_id: str
    strategy: str
    endpoint: str | None          # None when the strategy made no choice (empty set)

def _round_value(value):
    """Round every float leaf in `value` to 12 dp; recurse through dict,
    list, and tuple containers; pass everything else through unchanged.
    `bool` is excluded explicitly since `isinstance(True, int)` is True but
    `isinstance(True, float)` is False -- kept for clarity, not correctness.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 12)
    if isinstance(value, dict):
        return {k: _round_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_round_value(v) for v in value)
    return value

def _mean(xs: list[float]) -> float | None:
    """`None` (undefined), never 0.0, when `xs` is empty -- an unpopulated
    aggregate must not read as "zero regret"."""
    return sum(xs) / len(xs) if xs else None

def _oracle_raw(rows: list[MatrixRow]) -> dict:
    """The feasible-oracle arithmetic (spec §6), **unrounded**. Internal.

    `case_oracle` (the public function) wraps this with `_round_value` for
    external callers. `strategy_metrics` calls this directly and diffs its
    result against each row's own (also unrounded) fields -- never against
    an oracle already rounded to 12 dp. Rounding `q_star`/`c_star`/`l_star`
    *before* subtracting a chosen row's exact value can manufacture a
    spurious nonzero -- even a `-0.0` -- regret out of what is, in exact
    arithmetic, an identical value (fix-round-1 finding 2).
    """
    if not rows:
        raise ValueError("case_oracle requires at least one row")
    case_id = rows[0].case_id
    for row in rows:
        if row.case_id != case_id:
            raise ValueError(
                "case_oracle requires every row to share one case_id "
                f"(got {case_id!r} and {row.case_id!r})")

    q_star = max(row.quality for row in rows)
    passing_rows = [row for row in rows if row.passed]
    passing = sorted(row.endpoint for row in passing_rows)
    c_star = min((row.cost_usd for row in passing_rows), default=None)
    latencies = [row.latency_ms for row in passing_rows if row.latency_ms is not None]
    l_star = min(latencies) if latencies else None

    return {
        "q_star": q_star,
        "c_star": c_star,
        "l_star": l_star,
        "passing": passing,
        "infeasible_at_floor": len(passing) == 0,
    }

def case_oracle(rows: list[MatrixRow]) -> dict:
    """The feasible oracle for one case (spec §6), from that case's own
    sweep rows (every row must share one `case_id`; `rows` must be
    non-empty -- an empty case is never swept, so it never reaches here).
    **Every row passed in is treated as eligible** -- this function has no
    notion of `E(c)` (it isn't given a `CaseInfo`), so a caller that wants
    the oracle restricted to `E(c)` (spec §6: `q*`, `P`, `c*`, `l*` are all
    defined over `E(c)`) must filter `rows` to eligible endpoints first.
    `strategy_metrics` does exactly this.

    Returns `{"q_star", "c_star", "l_star", "passing", "infeasible_at_floor"}`:
    - `q_star`: `max` quality across *all* rows (passing or not).
    - `passing`: sorted slugs of rows with `passed=True` -- `P(c)`.
    - `c_star` / `l_star`: `min` cost / latency over `passing` rows, or
      `None` if `passing` is empty. A passing row's `latency_ms` is not
      expected to be `None` (only error rows have `latency_ms=None`, and
      error rows are never `passed=True`), but `l_star` still degrades to
      `None` rather than raising if it happens.
    - `infeasible_at_floor`: `True` iff `passing` is empty (`P(c) = {}`).
    """
    return _round_value(_oracle_raw(rows))

def strategy_metrics(
    cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice], strategy: str,
) -> dict:
    """Aggregate metrics 1-5 (spec §6) for one `strategy`, over `cases`.

    Returns:
    - `n_cases`: `len(cases)`.
    - `violation_rate`: share of `cases` whose choice names an endpoint not
      in that case's `eligible` set (including a non-`None` choice on an
      empty case), plus a choice naming an eligible endpoint with no
      matching sweep row, plus a non-empty case with no recorded `Choice`
      at all for `strategy` -- all "should not happen" (brief ruling /
      fix-round-1 finding 4), but counted as violations rather than
      crashing.
    - `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`,
      `floor_miss_rate`: as spec §6 points 2-4, computed over non-empty
      cases only (an empty case has no quality/cost/latency notion). A
      `None` choice, or a violating choice, scores quality 0 for the
      regret computation and never contributes to cost/latency regret
      (those are computed only when the choice itself passed).
    - `mean_chosen_cost`: mean, over non-empty cases, of the chosen
      endpoint's `cost_usd` (0.0 for an error row, a `None` choice, or a
      violating choice) -- not itself one of spec §6's five metrics, but
      needed by `headline`'s saving-percentage calculation, which only
      receives this function's output.
    - `infeasible_at_floor`: count of non-empty cases where `P(c) = {}`.
      Strategy-independent (an oracle property of the case), exposed here
      per-strategy for convenient reporting.
    - `empty_set_mismatches`: count of cases (strategy-independent) where
      `expect_empty` disagrees with actual emptiness (`eligible == ()`), or
      an actually-empty case was not `rejected_ok`. A case tripping both
      conditions is counted once.

    Any of the five regret-shaped metrics is `None`, not 0.0, when its
    denominator is empty (e.g. no non-empty cases, or no case where the
    choice passed) -- an aggregate with no data is undefined, not zero.
    """
    rows_by_case: dict[str, list[MatrixRow]] = {}
    rows_by_case_endpoint: dict[tuple[str, str], MatrixRow] = {}
    for row in rows:
        rows_by_case.setdefault(row.case_id, []).append(row)
        rows_by_case_endpoint[(row.case_id, row.endpoint)] = row

    choice_by_case = {c.case_id: c for c in choices if c.strategy == strategy}

    n_cases = len(cases)
    violations_count = 0
    empty_mismatches = 0
    infeasible_count = 0

    quality_regrets: list[float] = []
    pass_flags: list[float] = []
    floor_miss_flags: list[float] = []
    cost_regrets: list[float] = []
    latency_regrets: list[float] = []
    chosen_costs: list[float] = []

    for case in cases:
        actual_empty = len(case.eligible) == 0
        mismatch = (case.expect_empty != actual_empty) or (
            actual_empty and not case.rejected_ok)
        if mismatch:
            empty_mismatches += 1

        has_choice_record = case.case_id in choice_by_case
        choice = choice_by_case.get(case.case_id)
        chosen_endpoint = choice.endpoint if choice is not None else None

        if actual_empty:
            # A correct choice on an empty case is None; anything else
            # names an endpoint outside E(c) = {} by construction. A
            # missing Choice record is also correct here (nothing to
            # record) -- the harness-bug rule below applies only to
            # non-empty cases.
            if chosen_endpoint is not None:
                violations_count += 1
            continue

        # Spec §6: q*, P, c*, l* are all defined over E(c). A row for an
        # endpoint outside case.eligible must never contaminate the oracle
        # (fix-round-1 finding 3) -- it is still flagged, but only via
        # violations() / the missing-row check below, never by skewing q*.
        case_rows = [
            row for row in rows_by_case.get(case.case_id, [])
            if row.endpoint in case.eligible
        ]
        oracle = _oracle_raw(case_rows) if case_rows else None
        if oracle is not None and oracle["infeasible_at_floor"]:
            infeasible_count += 1

        row: MatrixRow | None = None
        if not has_choice_record:
            # A non-empty case with no Choice record at all for this
            # strategy is a harness bug -- the runner is expected to
            # record one Choice per (case, strategy) it ran. Distinct from
            # an explicit Choice(endpoint=None), which is a legitimate "no
            # choice" and stays violation-free (below).
            violations_count += 1
        elif chosen_endpoint is not None:
            if chosen_endpoint not in case.eligible:
                violations_count += 1
            else:
                row = rows_by_case_endpoint.get((case.case_id, chosen_endpoint))
                if row is None:
                    violations_count += 1

        chosen_quality = row.quality if row is not None else 0.0
        chosen_passed = row.passed if row is not None else False
        chosen_cost = row.cost_usd if row is not None else 0.0

        q_star = oracle["q_star"] if oracle is not None else 0.0
        quality_regrets.append(q_star - chosen_quality)
        pass_flags.append(1.0 if chosen_passed else 0.0)
        chosen_costs.append(chosen_cost)

        if oracle is not None and oracle["passing"]:
            floor_miss_flags.append(0.0 if chosen_passed else 1.0)
            if chosen_passed:
                c_star = oracle["c_star"]
                if c_star is not None:
                    cost_regrets.append(chosen_cost - c_star)
                l_star = oracle["l_star"]
                if (row is not None and row.latency_ms is not None
                        and l_star is not None):
                    latency_regrets.append(row.latency_ms - l_star)

    violation_rate = violations_count / n_cases if n_cases else None

    return _round_value({
        "n_cases": n_cases,
        "violation_rate": violation_rate,
        "pass_rate": _mean(pass_flags),
        "quality_regret": _mean(quality_regrets),
        "cost_regret": _mean(cost_regrets),
        "latency_regret": _mean(latency_regrets),
        "floor_miss_rate": _mean(floor_miss_flags),
        "infeasible_at_floor": infeasible_count,
        "empty_set_mismatches": empty_mismatches,
        "mean_chosen_cost": _mean(chosen_costs),
    })

def grouped(
    cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice],
    strategy: str, tag: str,
) -> dict[str, dict]:
    """`strategy_metrics` for `strategy`, split by each distinct value of
    `tags[tag]` among `cases`. A case missing `tag` entirely is excluded
    from every group (it belongs to none of them). Keys are `str(value)`,
    sorted, for deterministic iteration order (used by `worst_groups` for
    deterministic tie-breaking).
    """
    values: dict[str, list[CaseInfo]] = {}
    for case in cases:
        if tag not in case.tags:
            continue
        key = str(case.tags[tag])
        values.setdefault(key, []).append(case)

    return {
        key: strategy_metrics(group_cases, rows, choices, strategy)
        for key, group_cases in sorted(values.items())
    }

# Only these three tags are considered for grouping (fix-round-1 finding
# 5): they're the ones spec §9's starter suite uses to characterise a case
# (workload / placement / protection posture). Other tags -- e.g.
# `synthetic`, a loader-only marker -- must never become a reported "worst
# group" dimension.
_WORST_GROUP_TAGS = ("workload", "placement", "protection")

# Spec §6 points 2-4: quality regret (point 2, plus pass rate), cost
# regret (point 3, plus floor-miss rate), latency regret (point 4). Higher
# is worse for every metric except pass_rate, where lower is worse.
_HIGHER_IS_WORSE = ("quality_regret", "cost_regret", "latency_regret", "floor_miss_rate")
_LOWER_IS_WORSE = ("pass_rate",)
_WORST_GROUP_METRICS = _HIGHER_IS_WORSE + _LOWER_IS_WORSE

def worst_groups(
    cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice], strategy: str,
) -> dict:
    """The worst group per spec §6 points 2-4's metrics --
    `quality_regret`, `cost_regret`, `latency_regret`, `floor_miss_rate`
    (higher is worse for all four), and `pass_rate` (lower is worse) --
    across every value of `workload` / `placement` / `protection` among
    `cases`. No other tag is considered, even if `cases` carry one (e.g.
    `synthetic`).

    Returns `{"quality_regret": (tag, value, metric_value) | None, ...}`
    for each of the five metrics above. A metric with no group reporting a
    defined (non-`None`) value maps to `None`. Ties keep the first found
    under sorted (tag, value) iteration -- deterministic.
    """
    worst: dict[str, tuple[str, str, float] | None] = {
        metric: None for metric in _WORST_GROUP_METRICS
    }

    for tag in _WORST_GROUP_TAGS:
        groups = grouped(cases, rows, choices, strategy, tag)
        for value, metrics in groups.items():
            for metric in _WORST_GROUP_METRICS:
                metric_value = metrics.get(metric)
                if metric_value is None:
                    continue
                current = worst[metric]
                if current is None:
                    worst[metric] = (tag, value, metric_value)
                elif metric in _LOWER_IS_WORSE:
                    if metric_value < current[2]:
                        worst[metric] = (tag, value, metric_value)
                elif metric_value > current[2]:
                    worst[metric] = (tag, value, metric_value)

    return _round_value(worst)

def headline(
    metrics_by_strategy: dict, *, router: str, reference: str, parity_tol: float,
) -> dict:
    """The headline comparison (spec §6, "Headline (§20)"): `router`'s mean
    chosen cost vs `reference`'s, as a savings percentage -- printed only
    when the two strategies' pass rates are within `parity_tol` of each
    other (a cost win at a materially lower pass rate is not a saving,
    it's a worse product).

    `metrics_by_strategy` maps strategy name to a `strategy_metrics` dict
    (each must include `"pass_rate"` and `"mean_chosen_cost"`).

    Returns `{"comparable", "router_pass_rate", "reference_pass_rate",
    "saving_pct"}`. `comparable` requires both pass rates to be defined,
    `abs(router_pass_rate - reference_pass_rate) <= parity_tol`, and a
    strictly positive `reference` mean cost (a zero or undefined reference
    cost makes a percentage saving meaningless). `saving_pct` is `None`
    whenever `comparable` is `False`.

    The parity check rounds the absolute difference to 12 dp before
    comparing to `parity_tol` (fix-round-1 finding 1): e.g. 45/50 vs 46/50
    at `parity_tol=0.02` is exactly at the boundary mathematically, but
    `abs(0.9 - 0.92)` is `0.020000000000000018` in floating point --
    spuriously failing "<=" for a case spec §6 says must pass.
    """
    router_metrics = metrics_by_strategy[router]
    reference_metrics = metrics_by_strategy[reference]

    router_pass_rate = router_metrics.get("pass_rate")
    reference_pass_rate = reference_metrics.get("pass_rate")
    router_cost = router_metrics.get("mean_chosen_cost")
    reference_cost = reference_metrics.get("mean_chosen_cost")

    comparable = (
        router_pass_rate is not None
        and reference_pass_rate is not None
        and round(abs(router_pass_rate - reference_pass_rate), 12) <= parity_tol
        and reference_cost is not None
        and reference_cost > 0
    )

    saving_pct = None
    if comparable:
        saving_pct = (reference_cost - router_cost) / reference_cost * 100

    return _round_value({
        "comparable": comparable,
        "router_pass_rate": router_pass_rate,
        "reference_pass_rate": reference_pass_rate,
        "saving_pct": saving_pct,
    })

def violations(
    cases: list[CaseInfo], choices: list[Choice], sweep_served: list[tuple[str, str]],
) -> int:
    """The raw policy-violation count (spec §6 point 1) across *all*
    strategies' `choices` and the sweep's own served calls -- the CLI's
    exit-2 gate (global constraint: "Policy-violation rate must be 0;
    otherwise CLI exit code 2").

    Counts:
    - every `choice` naming an endpoint outside that case's `eligible` set
      (a non-`None` choice on an empty case always counts, since `E(c)` is
      empty);
    - every `(case_id, endpoint)` in `sweep_served` -- the calls the sweep
      actually made -- naming an endpoint outside that case's `eligible`
      set. This is the "no eligibility bypass" invariant (global
      constraints): every swept call must have come from that case's own
      `feasible_set(...).eligible`.
    """
    eligible_by_case = {case.case_id: set(case.eligible) for case in cases}

    count = 0
    for choice in choices:
        eligible = eligible_by_case.get(choice.case_id, set())
        if choice.endpoint is not None and choice.endpoint not in eligible:
            count += 1
    for case_id, endpoint in sweep_served:
        eligible = eligible_by_case.get(case_id, set())
        if endpoint not in eligible:
            count += 1
    return count
