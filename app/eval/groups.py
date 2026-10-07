"""Group report for FeasibleRouteBench (P2 evaluation metrics, spec §4).

Every bench metric, per strategy, broken down by four dimensions of a case:

- `placement`, `protection`, `workload` -- the case author's own tags (the
  same three `oracle.grouped` reads);
- `application` -- `tags.app` when the author set one, else a fingerprint
  of the case's `app_policy`: cases with the same policy are one
  application. The bench runs every case under one tenant id, so there is
  no tenant to group by.

A case with no value for a dimension is in that dimension's `(untagged)`
group, so every case is in exactly one group per dimension.

**Small groups print no rate.** A figure over fewer than `min_size` cases
is `{"state": "insufficient_cases", "n": k}`. The rule is applied to each
figure's own denominator, not to the group's case count: cost regret is over
the cases whose served answer passed, which can be far fewer. Counts
(`violations`, `empty_set_mismatches`) are always printed -- a violation in
a one-case group must stay visible.

**Worst group.** Per metric, the group with the worst figure among the
figures that are printed. It names a set of cases. It orders no model,
endpoint or vendor.

Pure: no pipeline, registry, settings or clock. Never imported by serving
code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json

from app.eval.oracle import CaseScore, _mean, _round_value, _values

SCHEMA = "routeforge.bench-group-report/v1"

DIMENSIONS = ("placement", "protection", "application", "workload")
#: dimension -> the case tag it is read from (`application` falls back to
#: the policy fingerprint, see `application_key`).
_TAG = {"placement": "placement", "protection": "protection",
        "application": "app", "workload": "workload"}
UNTAGGED = "(untagged)"
DEFAULT_MIN_GROUP_SIZE = 5
INSUFFICIENT = "insufficient_cases"

#: metric -> the `CaseScore` field it is the mean of (see `cell`).
_FIELD = {
    "violation_rate": "violation",
    "pass_rate": "passed",
    "quality_regret": "quality_regret",
    "cost_regret": "cost_regret",
    "latency_regret": "latency_regret",
    "floor_miss_rate": "floor_miss",
    "empty_set_mismatch_rate": "empty_mismatch",
}
GROUP_METRICS = tuple(_FIELD)
#: `pass_rate` is the one metric where a LOWER value is the worse one.
LOWER_IS_WORSE = ("pass_rate",)

# --- fixed caveat text (spec §8; pinned by tests/eval/test_groups.py) ----------

CAVEAT_SIMULATED = (
    "On the simulated backend, quality is an input: each pass or fail is a seeded "
    "draw against a probability written in the endpoint file. Pass rate and quality "
    "regret measure the harness, not the models.")
CAVEAT_LIVE = (
    "This run called the endpoints in the endpoint file, once per case and "
    "endpoint, and graded each answer with a deterministic grader. One call is one "
    "sample.")
CAVEAT_TIER_RULE = (
    "The router comparison is mostly the tier rule: the target tier is chosen from "
    "the request's length, so on short cases the router's regret against the oracle "
    "is set by which tier it may use, not by the four-term scorer.")
CAVEAT_SYNTHETIC = "Cases are synthetic and single-turn."
CAVEAT_GROUPS = (
    "A group is the cases of this suite that carry one label. A figure over fewer "
    "cases than the minimum prints its case count and no rate. A group above the "
    "minimum is still small: read its case count with every figure.")
CAVEAT_TO_MEASURE = "Live-backend results: TO MEASURE."


def bench_caveats(mode: str) -> list[str]:
    """The caveats every new report opens with. `mode` is `run.json`'s
    `"simulated"` or `"live"`."""
    first = CAVEAT_SIMULATED if mode == "simulated" else CAVEAT_LIVE
    tail = [CAVEAT_TO_MEASURE] if mode == "simulated" else []
    return [first, CAVEAT_TIER_RULE, CAVEAT_SYNTHETIC, *tail]


# --- which group a case is in --------------------------------------------------

def application_key(tags: dict, app_policy: dict) -> str:
    """`tags["app"]` when it is a non-empty string, else `policy-` plus the
    first 8 hex of sha256 over the case's `app_policy` (sorted keys): cases
    with the same policy are the same application."""
    app = tags.get("app")
    if isinstance(app, str) and app:
        return app
    blob = json.dumps(app_policy, sort_keys=True, default=str)
    return "policy-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def case_keys(tags: dict, app_policy: dict) -> dict[str, str]:
    """One group label per dimension for one case. Never missing a key."""
    keys: dict[str, str] = {}
    for dim in DIMENSIONS:
        if dim == "application":
            keys[dim] = application_key(tags, app_policy)
        else:
            value = tags.get(_TAG[dim])
            keys[dim] = UNTAGGED if value is None else str(value)
    return keys


def partition(case_ids: list[str], keys: dict[str, dict[str, str]],
              dimension: str) -> dict[str, list[str]]:
    """`{group: [case ids]}` for one dimension, groups sorted by label, each
    group's ids in the order given. Every id lands in exactly one group."""
    groups: dict[str, list[str]] = {}
    for case_id in case_ids:
        groups.setdefault(keys[case_id][dimension], []).append(case_id)
    return dict(sorted(groups.items()))


# --- figures -------------------------------------------------------------------

def metric_value(scores: list[CaseScore], metric: str) -> tuple[float | None, int]:
    """`(value, n)`: one metric over `scores`, unrounded, and the number of
    cases it is over. The two boolean fields are rates over every case; the
    rest are means over the cases whose field is not `None` -- the same
    denominators as `oracle.aggregate_scores`."""
    field = _FIELD[metric]
    if metric in ("violation_rate", "empty_set_mismatch_rate"):
        n = len(scores)
        return (sum(1 for s in scores if getattr(s, field)) / n if n else None), n
    values = _values(scores, field)
    return _mean(values), len(values)


def cell(scores: list[CaseScore], metric: str, min_size: int) -> dict:
    """One printed figure: `{"value", "n"}`, or `{"state":
    "insufficient_cases", "n"}` when it is over fewer than `min_size` cases."""
    value, n = metric_value(scores, metric)
    if n < min_size or value is None:
        return {"state": INSUFFICIENT, "n": n}
    return {"value": value, "n": n}


def group_block(scores: list[CaseScore], min_size: int) -> dict:
    """One group's block: its counts (always) and one `cell` per metric."""
    return {
        "n_cases": len(scores),
        "empty_cases": sum(1 for s in scores if s.empty),
        "violations": sum(1 for s in scores if s.violation),
        "empty_set_mismatches": sum(1 for s in scores if s.empty_mismatch),
        "metrics": {m: cell(scores, m, min_size) for m in GROUP_METRICS},
    }


def worst_groups(dimensions: dict[str, dict[str, dict]]) -> dict[str, dict | None]:
    """Per metric, the printed figure that is worst across every group of
    every dimension: highest, or lowest for `pass_rate`. `None` when no group
    prints that metric. A tie keeps the first in `DIMENSIONS` order, then
    label order.

    `groups_printed` is how many groups print the metric and `groups_at_value`
    how many of them share the worst value: when the two are equal no group
    is worse than another, and the report says so instead of naming one."""
    worst: dict[str, dict | None] = {m: None for m in GROUP_METRICS}
    for metric in GROUP_METRICS:
        printed = [(dim, label, block) for dim in DIMENSIONS
                   for label, block in dimensions.get(dim, {}).items()
                   if "value" in block["metrics"][metric]]
        if not printed:
            continue
        values = [block["metrics"][metric]["value"] for _, _, block in printed]
        target = min(values) if metric in LOWER_IS_WORSE else max(values)
        dim, label, block = next(p for p, v in zip(printed, values) if v == target)
        worst[metric] = {"dimension": dim, "group": label, "value": target,
                         "n": block["metrics"][metric]["n"],
                         "n_cases": block["n_cases"],
                         "groups_printed": len(printed),
                         "groups_at_value": sum(1 for v in values if v == target)}
    return worst


def strategy_groups(scores: list[CaseScore], keys: dict[str, dict[str, str]],
                    min_size: int) -> dict:
    """One strategy's `{"overall", "dimensions", "worst"}`. `scores` is that
    strategy's `oracle.case_scores`, in suite order."""
    by_id = {s.case_id: s for s in scores}
    ids = [s.case_id for s in scores]
    dimensions = {
        dim: {label: group_block([by_id[i] for i in members], min_size)
              for label, members in partition(ids, keys, dim).items()}
        for dim in DIMENSIONS}
    return {"overall": group_block(scores, min_size), "dimensions": dimensions,
            "worst": worst_groups(dimensions)}


def group_report(scores_by_strategy: dict[str, list[CaseScore]],
                 keys: dict[str, dict[str, str]], *, min_size: int, mode: str) -> dict:
    """The `groups.json` document. Floats are rounded to 12 dp, like every
    other bench output."""
    if isinstance(min_size, bool) or not isinstance(min_size, int) or min_size < 1:
        raise ValueError(f"min_size must be an int >= 1; got {min_size!r}")
    ids = sorted(keys)
    return _round_value({
        "schema": SCHEMA,
        "min_group_size": min_size,
        "dimensions": list(DIMENSIONS),
        "metrics": list(GROUP_METRICS),
        "n_cases": len(ids),
        "group_sizes": {dim: {label: len(members)
                              for label, members in partition(ids, keys, dim).items()}
                        for dim in DIMENSIONS},
        "strategies": {name: strategy_groups(scores, keys, min_size)
                       for name, scores in scores_by_strategy.items()},
        "caveats": [*bench_caveats(mode), CAVEAT_GROUPS],
    })


# --- groups.md -----------------------------------------------------------------

def figure_text(figure: dict) -> str:
    """A printed figure, or `insufficient cases (n = k)`."""
    if "value" not in figure:
        return f"insufficient cases (n = {figure['n']})"
    return f"{figure['value']:.6g} (n = {figure['n']})"


def render_group_report(doc: dict, *, detail: str) -> str:
    """`groups.md`: the caveats, the group sizes, every strategy's worst
    groups, and the full breakdown of the one strategy named `detail`."""
    lines = ["# FeasibleRouteBench group report", ""]
    lines += [f"> {text}" for text in doc["caveats"]]
    lines += ["", f"Minimum cases behind a printed rate: **{doc['min_group_size']}** "
              f"(`--min-group-size`). Cases in this run: {doc['n_cases']}.", ""]
    lines += ["## Group sizes", "", "| dimension | group | cases |", "|---|---|---|"]
    for dim in doc["dimensions"]:
        for label, n in doc["group_sizes"][dim].items():
            lines.append(f"| {dim} | {label} | {n} |")
    lines += ["", "## Worst group per metric", "",
              "The worst printed figure across every group of every dimension. It "
              "names a set of cases, not a model or an endpoint.", "",
              "| strategy | metric | dimension | group | figure | group cases |",
              "|---|---|---|---|---|---|"]
    for name, block in doc["strategies"].items():
        for metric in doc["metrics"]:
            worst = block["worst"][metric]
            if worst is None:
                lines.append(f"| {name} | {metric} | n/a | n/a | "
                             "no group has enough cases | n/a |")
            elif (worst["groups_printed"] > 1
                  and worst["groups_at_value"] == worst["groups_printed"]):
                lines.append(f"| {name} | {metric} | all | all | {worst['value']:.6g} in "
                             f"every printed group ({worst['groups_printed']}) | n/a |")
            else:
                lines.append(f"| {name} | {metric} | {worst['dimension']} | "
                             f"{worst['group']} | {figure_text(worst)} | "
                             f"{worst['n_cases']} |")
    block = doc["strategies"].get(detail)
    if block is not None:
        lines += ["", f"## Every group: `{detail}`", "",
                  "`groups.json` holds the same table for every strategy.", ""]
        header = ("| dimension | group | cases | violations | empty-set mismatches | "
                  + " | ".join(doc["metrics"]) + " |")
        lines += [header, "|---|---|---|---|---|" + "---|" * len(doc["metrics"])]
        rows = [("overall", "all cases", block["overall"])]
        rows += [(dim, label, group) for dim in doc["dimensions"]
                 for label, group in block["dimensions"][dim].items()]
        for dim, label, group in rows:
            lines.append(
                f"| {dim} | {label} | {group['n_cases']} | {group['violations']} | "
                f"{group['empty_set_mismatches']} | "
                + " | ".join(figure_text(group["metrics"][m]) for m in doc["metrics"])
                + " |")
    lines.append("")
    return "\n".join(lines)
