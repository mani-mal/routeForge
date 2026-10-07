"""Offline router-version check (P2 evaluation metrics, spec §7).

One bench run scores two router arms over the SAME sweep matrix: the
deployment's routing config (`router_online`) and that config with a
candidate overlay (`router_online:candidate`, `app/eval/candidate.py`). The
two arms see the same cases, the same eligible sets, the same seed and the
same per-endpoint outcomes, so every difference between them is the
candidate's.

**It applies nothing.** The result is two files and an exit code. There is
no admin call, no config write and no hook into serving: the handoff's
"promotion gate" is, here, a check an operator runs before THEY edit
`config/routing.yaml`. RouteForge has no in-process promotion path
(CLAUDE.md rule 4) and this module is not one.

**The decision rule** (`decide`, spec §7.4). `w` is the candidate's
worsening of a metric (positive = worse), `[lo, hi]` its paired-bootstrap
range, `tol` the operator's tolerance for that metric:

1. `w` undefined                -> `undefined`
2. `w > tol`                    -> `regressed`  (whatever the range says)
3. no range, or `hi > tol`      -> `inconclusive`
4. otherwise                    -> `within_tolerance`

A range that includes 0 decides nothing on its own: it is reported as
`change: "not_detectable"` beside whichever state the rule gave.

**Verdict and exit code** (`verdict_of`): `hard_fail` 2 (the candidate made
an ineligible choice or served a case with an empty feasible set, or the run
itself failed an integrity check -- no tolerance applies); `regressed` 3;
`inconclusive` 4; `pass` 0; `no_hard_failure` 0 when no tolerance was named.

Pure: no pipeline, registry, settings or clock. Never imported by serving
code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.eval.groups import DIMENSIONS, INSUFFICIENT, bench_caveats, partition
from app.eval.oracle import _SCORE_FIELD, CaseScore, _mean, _round_value
from app.eval.uncertainty import MAX_REPLICATES, draw_indices, interval_of

SCHEMA = "routeforge.router-version-check/v1"
METHOD = "paired_case_bootstrap"

#: The metrics a tolerance may be named for, in report order.
CHECK_METRICS = tuple(_SCORE_FIELD)
#: `pass_rate` is the one metric where a LOWER value is the worse one.
LOWER_IS_WORSE = ("pass_rate",)

EXIT_PASS, EXIT_HARD_FAIL, EXIT_REGRESSED, EXIT_INCONCLUSIVE = 0, 2, 3, 4
_EXIT = {"pass": EXIT_PASS, "no_hard_failure": EXIT_PASS, "hard_fail": EXIT_HARD_FAIL,
         "regressed": EXIT_REGRESSED, "inconclusive": EXIT_INCONCLUSIVE}

# Tolerance comparisons round to 12 dp first, like `oracle.headline`'s parity
# check: `0.92 - 0.90` is `0.020000000000000018` and must not exceed 0.02.
_DP = 12

# --- fixed text (spec §8; pinned by tests/eval/test_version_check.py) ----------

CHECK_NO_EFFECT = (
    "This check applies nothing and promotes nothing. It reads a candidate routing "
    "config, scores it offline beside the current one, and writes this report. A "
    "routing change is still made by an operator editing config/routing.yaml.")
CHECK_CONDITIONAL = (
    "The verdict is conditional on this suite, this endpoint file, this simulation "
    "seed and this case order. It is not a statement about live traffic.")
CHECK_RANGE = (
    "A range is the spread of the paired difference when the suite's cases are "
    "resampled. It says how much the difference depends on which cases are in the "
    "suite, and nothing about whether the suite resembles real traffic.")
CHECK_NO_TOLERANCE = (
    "No tolerance was named, so only the hard checks were applied: the differences "
    "below are shown, not judged.")


def check_caveats(mode: str) -> list[str]:
    return [CHECK_NO_EFFECT, *bench_caveats(mode), CHECK_CONDITIONAL, CHECK_RANGE]


@dataclass(frozen=True)
class CheckPlan:
    """What `--candidate` asked for. The defaults are the CLI's."""

    tolerances: dict[str, float] = field(default_factory=dict)
    replicates: int = 1000
    interval: float = 0.90
    seed: int = 0
    min_group_size: int = 5

    def __post_init__(self) -> None:
        for metric, tol in self.tolerances.items():
            if metric not in CHECK_METRICS:
                raise ValueError(f"tolerance for unknown metric {metric!r} "
                                 f"(metrics: {list(CHECK_METRICS)})")
            if (isinstance(tol, bool) or not isinstance(tol, (int, float))
                    or not 0.0 <= tol < float("inf")):
                raise ValueError(f"tolerance for {metric} must be a finite number "
                                 f">= 0; got {tol!r}")
        if (isinstance(self.replicates, bool) or not isinstance(self.replicates, int)
                or not 1 <= self.replicates <= MAX_REPLICATES):
            raise ValueError(f"replicates must be an int from 1 to {MAX_REPLICATES}; "
                             f"got {self.replicates!r}")
        if (isinstance(self.interval, bool) or not isinstance(self.interval, (int, float))
                or not 0.0 < self.interval < 1.0):
            raise ValueError(f"interval must be a number strictly between 0 and 1; "
                             f"got {self.interval!r}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError(f"seed must be an int >= 0; got {self.seed!r}")
        if (isinstance(self.min_group_size, bool)
                or not isinstance(self.min_group_size, int) or self.min_group_size < 1):
            raise ValueError(f"min_group_size must be an int >= 1; "
                             f"got {self.min_group_size!r}")


def parse_tolerances(items: list[str]) -> dict[str, float]:
    """`["pass_rate=0.02", ...]` -> `{"pass_rate": 0.02}`. Raises `ValueError`
    on a malformed item, a repeated metric, or a value that is not a number."""
    out: dict[str, float] = {}
    for item in items:
        metric, sep, raw = item.partition("=")
        metric = metric.strip()
        if not sep or not metric:
            raise ValueError(f"tolerance {item!r} is not METRIC=VALUE")
        if metric in out:
            raise ValueError(f"tolerance for {metric} given twice")
        try:
            out[metric] = float(raw)
        except ValueError:
            raise ValueError(f"tolerance {item!r}: {raw!r} is not a number") from None
    return out


# --- one figure ----------------------------------------------------------------

def _column(scores: list[CaseScore], metric: str) -> list[float | None]:
    field_name = _SCORE_FIELD[metric]
    return [getattr(s, field_name) for s in scores]


def _mean_at(column: list[float | None], idx: list[int]) -> float | None:
    return _mean([v for v in (column[i] for i in idx) if v is not None])


def worsening(metric: str, baseline: float | None, candidate: float | None
              ) -> float | None:
    """The candidate's change in the direction that is WORSE for `metric`:
    `candidate - baseline`, or `baseline - candidate` for `pass_rate`. `None`
    when either side is undefined."""
    if baseline is None or candidate is None:
        return None
    return baseline - candidate if metric in LOWER_IS_WORSE else candidate - baseline


def decide(w: float | None, hi: float | None, tol: float) -> str:
    """The decision rule (module docstring, spec §7.4)."""
    if w is None:
        return "undefined"
    if round(w, _DP) > tol:
        return "regressed"
    if hi is None or round(hi, _DP) > tol:
        return "inconclusive"
    return "within_tolerance"


def change_of(identical: bool, lo: float | None, hi: float | None) -> str:
    """What the range says, independent of any tolerance: `none` (every
    case has the same value in both arms), `undefined` (no range),
    `not_detectable` (the range includes 0), `worse` or `better`."""
    if identical:
        return "none"
    if lo is None or hi is None:
        return "undefined"
    if lo > 0:
        return "worse"
    if hi < 0:
        return "better"
    return "not_detectable"


def compare(baseline: list[CaseScore], candidate: list[CaseScore], scope: str,
            plan: CheckPlan) -> dict[str, dict]:
    """Every `CHECK_METRICS` figure for one scope (`"overall"` or
    `"<dimension>=<group>"`), keyed by metric.

    `baseline` and `candidate` are the two arms' scores for the SAME cases.
    Both are sorted by case id here, so the result does not depend on the
    order they are given in. Replicate `b` draws case positions with
    `uncertainty.draw_indices(plan.seed, b, scope, n)` -- one draw, used for
    both arms and every metric: that is the pairing.

    A figure whose denominator is below `plan.min_group_size` in either arm
    is `{"state": "insufficient_cases", ...}`: it keeps its `support` and
    carries no value, no worsening and no range."""
    base = sorted(baseline, key=lambda s: s.case_id)
    cand = sorted(candidate, key=lambda s: s.case_id)
    if [s.case_id for s in base] != [s.case_id for s in cand]:
        raise ValueError(f"{scope}: the two arms do not cover the same cases")
    n = len(base)
    columns = {m: (_column(base, m), _column(cand, m)) for m in CHECK_METRICS}
    samples: dict[str, list[float | None]] = {m: [] for m in CHECK_METRICS}
    everything = list(range(n))
    if n:
        for b in range(plan.replicates):
            idx = draw_indices(plan.seed, b, scope, n)
            for metric, (bcol, ccol) in columns.items():
                samples[metric].append(
                    worsening(metric, _mean_at(bcol, idx), _mean_at(ccol, idx)))
    out: dict[str, dict] = {}
    for metric, (bcol, ccol) in columns.items():
        b_value, c_value = _mean_at(bcol, everything), _mean_at(ccol, everything)
        support = {"baseline": sum(1 for v in bcol if v is not None),
                   "candidate": sum(1 for v in ccol if v is not None)}
        tol = plan.tolerances.get(metric)
        figure: dict = {"baseline": b_value, "candidate": c_value, "support": support,
                        "tolerance": tol}
        if min(support.values()) < plan.min_group_size:
            # Too few cases: the counts, and no figure at all (spec §4.3).
            figure.update(baseline=None, candidate=None, worsening=None, lo=None,
                          hi=None, change="undefined", state=INSUFFICIENT)
        else:
            w = worsening(metric, b_value, c_value)
            values = samples[metric]
            lo = hi = None
            if values and all(v is not None for v in values):
                lo, hi = interval_of(values, plan.interval)
            figure.update(
                worsening=w, lo=lo, hi=hi,
                change=change_of(bcol == ccol, lo, hi),
                state="not_checked" if tol is None else decide(w, hi, tol))
        out[metric] = figure
    return out


# --- the whole check -----------------------------------------------------------

def verdict_of(hard_failed: bool, states: list[str], tolerances_named: bool) -> str:
    """The verdict from the hard checks and every JUDGED figure's state
    (the overall figures, and each sufficiently large group's)."""
    if hard_failed:
        return "hard_fail"
    if "regressed" in states:
        return "regressed"
    if any(s in ("inconclusive", "undefined", INSUFFICIENT) for s in states):
        return "inconclusive"
    return "pass" if tolerances_named else "no_hard_failure"


def run_check(baseline: list[CaseScore], candidate: list[CaseScore],
              keys: dict[str, dict[str, str]],
              chains: tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]],
              plan: CheckPlan, *, arms: tuple[str, str], candidate_doc: dict,
              run_exit_code: int, mode: str) -> dict:
    """The `check.json` document.

    `chains` is `(baseline, candidate)`: each arm's attempt chain per case
    id. `run_exit_code` is the bench run's own code (`runner._exit_code`): 2
    there is a hard failure here whichever arm caused it."""
    ids = sorted(s.case_id for s in baseline)
    by_base = {s.case_id: s for s in baseline}
    by_cand = {s.case_id: s for s in candidate}
    violations = sum(1 for s in candidate if s.violation)
    served_on_empty = sum(1 for s in candidate if s.empty and s.violation)
    hard_failed = violations > 0 or run_exit_code != 0
    changed = sum(1 for i in ids if tuple(chains[0].get(i, ())) != tuple(chains[1].get(i, ())))

    overall = compare(baseline, candidate, "overall", plan)
    # Overall: every named metric is judged; one that cannot be is inconclusive.
    judged: list[tuple[str, str, str]] = [
        ("overall", m, f["state"]) for m, f in overall.items() if f["tolerance"] is not None]
    groups: dict[str, dict[str, dict]] = {}
    for dim in DIMENSIONS:
        groups[dim] = {}
        for label, members in partition(ids, keys, dim).items():
            block: dict = {"n_cases": len(members)}
            if len(members) < plan.min_group_size:
                block["state"] = INSUFFICIENT
            else:
                scope = f"{dim}={label}"
                block["metrics"] = compare([by_base[i] for i in members],
                                           [by_cand[i] for i in members], scope, plan)
                # A group: a figure with too few cases behind it is listed,
                # not judged ("any sufficiently large group").
                judged += [(scope, m, f["state"]) for m, f in block["metrics"].items()
                           if f["tolerance"] is not None and f["state"] != INSUFFICIENT]
            groups[dim][label] = block

    verdict = verdict_of(hard_failed, [state for _, _, state in judged],
                         bool(plan.tolerances))

    def named(*states: str) -> list[dict]:
        return [{"scope": scope, "metric": metric, "state": state}
                for scope, metric, state in judged if state in states]

    caveats = check_caveats(mode)
    if not plan.tolerances:
        caveats.append(CHECK_NO_TOLERANCE)
    return _round_value({
        "schema": SCHEMA,
        "verdict": verdict,
        "exit_code": _EXIT[verdict],
        "effect": "none",
        "arms": {"baseline": arms[0], "candidate": arms[1]},
        "candidate": candidate_doc,
        "plan": {"method": METHOD, "tolerances": dict(sorted(plan.tolerances.items())),
                 "replicates": plan.replicates, "interval": plan.interval,
                 "seed": plan.seed, "min_group_size": plan.min_group_size},
        "hard": {"failed": hard_failed, "candidate_violations": violations,
                 "candidate_served_on_empty": served_on_empty,
                 "run_exit_code": run_exit_code},
        "changed": {"n_cases": len(ids), "choice_changed": changed,
                    "share": changed / len(ids) if ids else None},
        "overall": overall,
        "groups": groups,
        "regressed": named("regressed"),
        "inconclusive": named("inconclusive", "undefined", INSUFFICIENT),
        "caveats": caveats,
    })


# --- check.md ------------------------------------------------------------------

_VERDICT_LINE = {
    "hard_fail": "**HARD FAIL** (exit 2). The candidate made an ineligible choice, "
                 "served a case whose feasible set is empty, or the run failed an "
                 "integrity check. No tolerance applies to this.",
    "regressed": "**REGRESSED** (exit 3). At least one named metric is worse than its "
                 "tolerance allows, overall or in a group with enough cases.",
    "inconclusive": "**INCONCLUSIVE** (exit 4). No named metric is worse than its "
                    "tolerance at the point estimate, but at least one could not be "
                    "shown to be within it.",
    "pass": "**PASS** (exit 0). No hard failure, and every named metric is within its "
            "tolerance, range included, overall and in every group with enough cases.",
    "no_hard_failure": "**NO HARD FAILURE** (exit 0). No tolerance was named, so no "
                       "metric was judged.",
}


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def _figure_row(scope: str, metric: str, f: dict) -> str:
    if f["state"] == INSUFFICIENT:
        n = min(f["support"].values())
        return (f"| {scope} | {metric} | {_num(f['baseline'])} | {_num(f['candidate'])} | "
                f"insufficient cases (n = {n}) | n/a | n/a | {_num(f['tolerance'])} | "
                f"{INSUFFICIENT} |")
    rng = ("n/a" if f["lo"] is None
           else f"[{_num(f['lo'])}, {_num(f['hi'])}]")
    return (f"| {scope} | {metric} | {_num(f['baseline'])} | {_num(f['candidate'])} | "
            f"{_num(f['worsening'])} | {rng} | {f['change']} | {_num(f['tolerance'])} | "
            f"{f['state']} |")


def render_check(doc: dict) -> str:
    """`check.md`: the fixed text, the verdict, the hard checks, every
    overall figure, then each group's figures for the metrics a tolerance was
    named for. Rows follow `DIMENSIONS`, label and `CHECK_METRICS` order;
    nothing is sorted by size."""
    plan = doc["plan"]
    level = plan["interval"]
    cand = doc["candidate"]
    lines = ["# Router-version check (offline)", ""]
    lines += [f"> {text}" for text in doc["caveats"]]
    lines += ["", "## Verdict", "", _VERDICT_LINE[doc["verdict"]], ""]
    lines += [f"- **baseline arm**: `{doc['arms']['baseline']}` (the routing config as "
              "loaded)",
              f"- **candidate arm**: `{doc['arms']['candidate']}` (id `{cand['id']}`"
              + (f", name `{cand['name']}`" if cand.get("name") else "") + ")",
              f"- **candidate overlay**: `{cand['routing']}`",
              f"- **cases whose attempt chain changed**: "
              f"{doc['changed']['choice_changed']} of {doc['changed']['n_cases']}",
              f"- **tolerances**: "
              + (", ".join(f"{m} {_num(t)}" for m, t in plan["tolerances"].items())
                 or "none named"),
              f"- **range**: {level:.0%} percentile interval of the paired difference, "
              f"{plan['replicates']} resamples of the cases, seed {plan['seed']}",
              f"- **minimum cases behind a judged figure**: {plan['min_group_size']}", ""]
    hard = doc["hard"]
    lines += ["## Hard checks (no tolerance)", "",
              f"- **candidate attempts outside the eligible set (cases)**: "
              f"{hard['candidate_violations']}",
              f"- **of those, on a case whose feasible set is empty**: "
              f"{hard['candidate_served_on_empty']}",
              f"- **bench run exit code**: {hard['run_exit_code']}", ""]
    header = ["| scope | metric | baseline | candidate | worsening | range | change | "
              "tolerance | state |", "|---|---|---|---|---|---|---|---|---|"]
    lines += ["## Overall", "",
              "`worsening` is candidate minus baseline in the direction that is worse "
              "for that metric (for `pass_rate`, baseline minus candidate). Positive "
              "is worse.", "", *header]
    lines += [_figure_row("overall", m, doc["overall"][m]) for m in CHECK_METRICS]
    named = [m for m in CHECK_METRICS if m in plan["tolerances"]]
    lines += ["", "## Groups", "",
              "Only the metrics a tolerance was named for; `check.json` holds every "
              "metric for every group.", "", *header]
    for dim in DIMENSIONS:
        for label, block in doc["groups"][dim].items():
            scope = f"{dim}={label}"
            if "metrics" not in block:
                lines.append(f"| {scope} | all | n/a | n/a | insufficient cases "
                             f"(n = {block['n_cases']}) | n/a | n/a | n/a | "
                             f"{INSUFFICIENT} |")
                continue
            lines += [_figure_row(scope, m, block["metrics"][m]) for m in named]
    lines.append("")
    return "\n".join(lines)
