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

**Failover-aware scoring (B48 spec §5).** A strategy yields an *attempt
chain*, not one choice. `attempt_outcome` walks the chain: an error row
(`MatrixRow.error` set) is a failed attempt that costs 0 and adds its
`failure_latency_ms` (0.0 when unknown); the first non-error row serves
and ends the chain -- failover is on errors only, never on quality. A
chain where every attempt fails is **exhausted** (production's 502):
quality 0, nothing served. The oracle is **unchanged** -- still the best
eligible endpoint's successful single call -- and regret is computed from
the chain's outcome, so a strategy whose first pick is dead pays that
attempt's latency (and wasted attempt), not quality 0, unless every
attempt fails. Every recorded attempt must be in `E(c)`.

Every float in every dict this module returns is rounded to 12 decimal
places before it is handed back, so two runs over identical inputs produce
byte-identical output (determinism ruling, task-4 brief).
"""
from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class MatrixRow:
    """One `(case, endpoint)` sweep outcome (spec §7 `matrix.jsonl`).

    `counts_unhealth`/`failure_latency_ms`/`error_class` are spec §4
    additions (failure injection, B48): all `None` on success. On a failed
    sweep call the runner sets them from the real backend exception when one
    is reachable (only one hop below the gateway's own exhaustion error --
    see `app/eval/runner.py::_backend_exception`), else `counts_unhealth`
    is `False` (not an endpoint verdict) and the other two stay `None`.
    This module stays pure -- it only carries the fields; it never computes
    them."""

    case_id: str
    endpoint: str
    quality: float
    passed: bool
    cost_usd: float
    latency_ms: float | None      # None when error
    error: str | None = None
    counts_unhealth: bool | None = None    # None on success (spec §4)
    failure_latency_ms: float | None = None  # None on success or when unknown
    error_class: str | None = None  # the classified backend exception's class name

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
    """One `(case, strategy)` decision (spec §7 `choices.jsonl`).

    `attempts` is the attempt chain in order (B48 spec §5); `endpoint` is
    the **served** endpoint -- the chain's first successful attempt -- or
    `None` (no choice, or an exhausted chain). An empty `attempts` means
    a legacy single choice: scored as the chain `(endpoint,)` (or `()`
    when `endpoint` is `None`)."""

    case_id: str
    strategy: str
    endpoint: str | None          # served endpoint; None when nothing was served
    attempts: tuple[str, ...] = ()


def _attempt_chain(choice: Choice) -> tuple[str, ...]:
    """The chain to score for `choice`: its `attempts`, or the legacy
    single-choice chain `(endpoint,)` when `attempts` is empty."""
    if choice.attempts:
        return tuple(choice.attempts)
    return (choice.endpoint,) if choice.endpoint is not None else ()

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

def attempt_outcome(
    rows_by_endpoint: dict[str, MatrixRow], attempts: tuple[str, ...],
) -> dict:
    """The outcome of one attempt chain on one case (B48 spec §5), rounded
    like every other dict this module hands back. See
    `_attempt_outcome_raw` for the full definition; `strategy_metrics`
    uses the unrounded form and rounds once at its own output (the same
    unrounded-internal pattern as `_oracle_raw` / `case_oracle`)."""
    return _round_value(_attempt_outcome_raw(rows_by_endpoint, attempts))

def _attempt_outcome_raw(
    rows_by_endpoint: dict[str, MatrixRow], attempts: tuple[str, ...],
) -> dict:
    """The outcome of one attempt chain on one case (B48 spec §5),
    **unrounded**. Internal.

    `rows_by_endpoint` maps endpoint slug to that case's sweep row; every
    slug in `attempts` up to and including the first success must be
    present (`KeyError` otherwise -- `strategy_metrics` checks this, and
    eligibility, before calling). `attempts` is scored **exactly as given**:
    no attempt cap is applied here (the cap, `1 +
    max_failover_attempts`, is the strategy's job). Walking the chain in
    order:

    - an error row (`error` set) is a failed attempt: cost 0, latency
      `failure_latency_ms` (0.0 when unknown);
    - the first non-error row serves and ends the chain -- any later
      entries are not attempts and are not scored (failover is on error
      only; a below-floor answer is served, not retried).

    Returns `{"served", "quality", "cost_usd", "latency_ms", "attempts",
    "wasted_attempts", "exhausted"}`:
    - `served`: the serving endpoint, or `None`;
    - `quality`: the served row's quality, 0.0 if nothing was served;
    - `cost_usd`: sum of attempt costs (failed attempts cost 0);
    - `latency_ms`: sum of failed attempts' latencies plus the served
      row's latency; for an exhausted chain the failed latencies alone;
      `None` for an empty chain or a served row with no latency;
    - `attempts`: attempts made (through the first success);
    - `wasted_attempts`: failed attempts;
    - `exhausted`: `True` iff at least one attempt was made and none
      succeeded. An empty chain is "no choice", not exhausted.
    """
    served: MatrixRow | None = None
    made = 0
    wasted = 0
    cost = 0.0
    failed_latency = 0.0
    for endpoint in attempts:
        row = rows_by_endpoint[endpoint]
        made += 1
        if row.error is not None:
            wasted += 1          # a failed attempt costs 0 (spec §5)
            failed_latency += (row.failure_latency_ms
                               if row.failure_latency_ms is not None else 0.0)
            continue
        served = row
        cost += row.cost_usd
        break

    if served is not None:
        latency = (failed_latency + served.latency_ms
                   if served.latency_ms is not None else None)
    else:
        latency = failed_latency if made else None

    return {
        "served": served.endpoint if served is not None else None,
        "quality": served.quality if served is not None else 0.0,
        "cost_usd": cost,
        "latency_ms": latency,
        "attempts": made,
        "wasted_attempts": wasted,
        "exhausted": made > 0 and served is None,
    }

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
    - `violation_rate`: share of `cases` whose attempt chain names **any**
      endpoint not in that case's `eligible` set (including any attempt on
      an empty case), plus a chain naming an eligible endpoint with no
      matching sweep row, plus a non-empty case with no recorded `Choice`
      at all for `strategy` -- all "should not happen" (brief ruling /
      fix-round-1 finding 4), but counted as violations rather than
      crashing. For an explicit `attempts` chain it is also a violation
      when the chain repeats an endpoint, records entries after the served
      attempt, or `Choice.endpoint` is not the chain's served endpoint
      (`None` when exhausted). At most one violation per case: this is a
      per-case rate, unlike `violations()`'s per-attempt count.
    - `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`,
      `floor_miss_rate`: as spec §6 points 2-4, computed over non-empty
      cases only (an empty case has no quality/cost/latency notion), from
      the chain's `attempt_outcome` (B48 spec §5): a case passes iff the
      *served* row passed; quality is the served row's (0 if exhausted);
      cost regret uses the chain's summed cost and latency regret its
      summed latency (failed attempts' latency plus the served call's).
      An empty chain, an exhausted chain, or a violating chain scores
      quality 0 and never contributes to cost/latency regret (those are
      computed only when the served outcome passed).
    - `mean_chosen_cost`: mean, over non-empty cases, of the chain's
      failover-aware cost (sum of attempt costs; failed attempts, an
      empty chain, and a violating chain cost 0.0) -- not itself one of
      spec §6's five metrics, but needed by `headline`'s saving-percentage
      calculation, which only receives this function's output.
    - `wasted_attempts`: mean, over non-empty cases, of the chain's failed
      attempts (B48 spec §5).
    - `exhausted_rate`: share of non-empty cases whose chain was exhausted
      (at least one attempt, none succeeded).
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
    wasted_counts: list[float] = []
    exhausted_flags: list[float] = []

    for case in cases:
        actual_empty = len(case.eligible) == 0
        mismatch = (case.expect_empty != actual_empty) or (
            actual_empty and not case.rejected_ok)
        if mismatch:
            empty_mismatches += 1

        has_choice_record = case.case_id in choice_by_case
        choice = choice_by_case.get(case.case_id)
        chain = _attempt_chain(choice) if choice is not None else ()

        if actual_empty:
            # A correct chain on an empty case is empty; any attempt names
            # an endpoint outside E(c) = {} by construction. A missing
            # Choice record is also correct here (nothing to record) --
            # the harness-bug rule below applies only to non-empty cases.
            if chain:
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

        # Failover-aware outcome (B48 spec §5). A violating chain -- any
        # attempt outside E(c), or an eligible attempt with no sweep row --
        # is scored exactly like "no choice" (quality 0, cost 0, no
        # attempts, not exhausted) so one bad case cannot silently distort
        # the aggregate; the violation itself is what fails the run.
        outcome = _attempt_outcome_raw({}, ())
        if not has_choice_record:
            # A non-empty case with no Choice record at all for this
            # strategy is a harness bug -- the runner is expected to
            # record one Choice per (case, strategy) it ran. Distinct from
            # an explicit Choice(endpoint=None) with no attempts, which is
            # a legitimate "no choice" and stays violation-free (below).
            violations_count += 1
        elif chain:
            # Every recorded attempt must be in E(c) and have a sweep row
            # -- including any recorded past the served one (spec §5:
            # "any attempt outside E(c) is a violation").
            case_rows_by_endpoint = {
                endpoint: rows_by_case_endpoint[(case.case_id, endpoint)]
                for endpoint in chain
                if endpoint in case.eligible
                and (case.case_id, endpoint) in rows_by_case_endpoint
            }
            if (len(case_rows_by_endpoint) != len(set(chain))
                    or len(set(chain)) != len(chain)):
                # an attempt outside E(c) / with no sweep row, or the same
                # endpoint attempted twice (the gateway excludes a failed one)
                violations_count += 1
            else:
                candidate = _attempt_outcome_raw(case_rows_by_endpoint, chain)
                if choice.attempts and (candidate["attempts"] != len(chain)
                                        or choice.endpoint != candidate["served"]):
                    # entries recorded after the served attempt, or a
                    # `Choice.endpoint` that is not the chain's served one
                    # (ruling D) -- the record contradicts itself
                    violations_count += 1
                else:
                    outcome = candidate

        served_row = (rows_by_case_endpoint[(case.case_id, outcome["served"])]
                      if outcome["served"] is not None else None)
        chosen_passed = served_row.passed if served_row is not None else False
        chosen_cost = outcome["cost_usd"]
        chosen_latency = outcome["latency_ms"]

        q_star = oracle["q_star"] if oracle is not None else 0.0
        quality_regrets.append(q_star - outcome["quality"])
        pass_flags.append(1.0 if chosen_passed else 0.0)
        chosen_costs.append(chosen_cost)
        wasted_counts.append(float(outcome["wasted_attempts"]))
        exhausted_flags.append(1.0 if outcome["exhausted"] else 0.0)

        if oracle is not None and oracle["passing"]:
            floor_miss_flags.append(0.0 if chosen_passed else 1.0)
            if chosen_passed:
                # Only a served, passing outcome reaches here, so an
                # exhausted chain's (failure-only) latency never enters
                # latency regret.
                c_star = oracle["c_star"]
                if c_star is not None:
                    cost_regrets.append(chosen_cost - c_star)
                l_star = oracle["l_star"]
                if chosen_latency is not None and l_star is not None:
                    latency_regrets.append(chosen_latency - l_star)

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
        "wasted_attempts": _mean(wasted_counts),
        "exhausted_rate": _mean(exhausted_flags),
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
    - every **attempt** in every `choice`'s chain (B48 spec §5; a legacy
      choice with empty `attempts` is the chain `(endpoint,)`) naming an
      endpoint outside that case's `eligible` set -- one per offending
      attempt (any attempt on an empty case always counts, since `E(c)` is
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
        for endpoint in _attempt_chain(choice):
            if endpoint not in eligible:
                count += 1
    for case_id, endpoint in sweep_served:
        eligible = eligible_by_case.get(case_id, set())
        if endpoint not in eligible:
            count += 1
    return count
