"""Offline loader and outcome report for the `routing_outcomes` NDJSON export
(P2b-2 Task 7).

**File-based only.** No DB, no `app.api`, no serving import -- this module
reads a file an operator downloaded from `GET /admin/outcomes/export` and
nothing else. `app/eval/` is operator-run tooling, never part of the serving
process (see `tests/eval/test_isolation.py`).

Consumes the export line format from `app/api/admin_routes.py::_export_lines`
(Task 6): one JSON object per line,
`{"schema": "routeforge.outcome/v1", "outcome": {...columns...}, "feedback":
[{"score_name", "value", "source", "ts"}, ...]}`. `value` is numeric for a
numeric score (e.g. `quality`, `route_correct`) or a string for a
categorical one.

**Never pools across `endpoint_registry_version` values** -- a registry
change can change which endpoints exist and what "correct" means, so mixing
versions in one aggregate would silently average across incomparable
regimes. `null` is its own group, labelled `"(unknown)"`.

**Semantic-cache hits** land in their own `"(cache)"` chosen bucket, never
in an endpoint's bucket or in `"(none)"` (unserved requests).

**No oracle regret.** Production observes only the served endpoint per
request -- there is no counterfactual to compare against -- so this module
never computes or reports regret. `render_report` prints `CAVEAT` verbatim
as the first line of every report.

Every float in `summarize`'s output is rounded to 6 decimal places, and
every dict is written with `sort_keys=True`, so re-running the CLI over the
same export produces a byte-identical `summary.json`.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

_SCHEMA_PREFIX = "routeforge.outcome/"
_SCHEMA_MAJOR = "v1"

UNTAGGED_WORKLOAD = "(untagged)"
UNCHOSEN = "(none)"
CACHE_HIT = "(cache)"
UNKNOWN_REGISTRY_VERSION = "(unknown)"

CAVEAT = (
    "Production observes only the served endpoint for each request, so "
    "oracle regret is not computable from these records and is not "
    "reported. Endpoint comparisons are confounded by the router's own "
    "choices; treat them as descriptive, not causal."
)


def load_export(path: str) -> tuple[list[dict], list[str]]:
    """Read an NDJSON export file, return `(records, warnings)`.

    Each record is the parsed `outcome` dict with the line's `feedback`
    list attached verbatim under `record["feedback"]`. Blank lines are
    skipped. A line that fails to parse as JSON, or that has no `outcome`
    key, is skipped and noted in `warnings` -- the rest of the file still
    loads. A line whose `schema` field's major version is not `v1` (e.g.
    `routeforge.outcome/v2`), OR that has no (or a non-string) `schema`
    field at all, raises `ValueError` immediately: unlike a malformed line
    or a missing `outcome`, an unrecognized or absent schema means this
    module may not understand the *rest* of the file either, so it does
    not guess and does not silently skip.
    """
    records: list[dict] = []
    warnings: list[str] = []
    with open(path, encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"line {lineno}: invalid JSON ({exc}), skipped")
                continue
            if not isinstance(obj, dict):
                warnings.append(f"line {lineno}: not a JSON object, skipped")
                continue
            schema = obj.get("schema")
            if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
                raise ValueError(f"line {lineno}: unrecognized schema {schema!r}")
            major = schema[len(_SCHEMA_PREFIX):].split(".", 1)[0]
            if major != _SCHEMA_MAJOR:
                raise ValueError(
                    f"line {lineno}: unsupported schema major {schema!r} "
                    f"(this loader understands {_SCHEMA_PREFIX}{_SCHEMA_MAJOR} only)")
            outcome = obj.get("outcome")
            if not isinstance(outcome, dict):
                warnings.append(f"line {lineno}: missing outcome, skipped")
                continue
            record = dict(outcome)
            record["feedback"] = obj.get("feedback") or []
            records.append(record)
    return records, warnings


def _round6(value):
    """Round every float leaf in `value` to 6 dp; recurse through dict and
    list containers, pass everything else through unchanged. `bool` is
    excluded explicitly since `isinstance(True, int)` is True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _round6(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round6(v) for v in value]
    return value


def _label(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _chosen_label(record: dict) -> str:
    """The `chosen` bucket a record lands in. A semantic-cache hit has no
    `chosen` (no router ran) and gets its own `CACHE_HIT` bucket: pooling it
    into an endpoint would credit that endpoint with a free, instant answer
    it never gave, and pooling it into `UNCHOSEN` would read it as unserved."""
    if record.get("cache_hit") is True:
        return CACHE_HIT
    return _label(record.get("chosen"), UNCHOSEN)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _nearest_rank(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile: the `ceil(pct/100 * n)`-th smallest value
    (1-indexed), clamped to `[1, n]`. `None` on an empty input."""
    n = len(sorted_values)
    if n == 0:
        return None
    rank = max(1, min(n, math.ceil(pct / 100 * n)))
    return sorted_values[rank - 1]


def _numeric_feedback(records: list[dict], score_name: str) -> list[float]:
    values: list[float] = []
    for r in records:
        for fb in r.get("feedback") or []:
            if fb.get("score_name") != score_name:
                continue
            v = fb.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            values.append(float(v))
    return values


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _summarize_bucket(records: list[dict]) -> dict:
    n = len(records)
    served_ok = sum(1 for r in records if r.get("status") == "ok")
    verify_rows = [r for r in records if r.get("verify_performed") is True]
    verify_passed = sum(1 for r in verify_rows if r.get("verify_passed") is True)
    quality_vals = _numeric_feedback(records, "quality")
    route_vals = _numeric_feedback(records, "route_correct")
    ok_latencies = sorted(
        float(r["latency_ms"]) for r in records
        if r.get("status") == "ok" and r.get("latency_ms") is not None)
    # cost_mean is over ok rows only, like latency -- a failed attempt costs
    # 0 by construction (RoutingOutcome default), so pooling it in would
    # dilute the served cost with rows that were never served.
    ok_costs = [
        float(r["cost_usd"]) for r in records
        if r.get("status") == "ok" and _is_number(r.get("cost_usd"))]
    # More than one attempt = a failover OR a confidence escalation; the
    # record does not distinguish them, so neither does the name.
    multi_attempt = sum(1 for r in records if len(r.get("attempts") or []) > 1)

    return {
        "n": n,
        "served_ok_rate": served_ok / n if n else None,
        "verify_pass_rate": (verify_passed / len(verify_rows)) if verify_rows else None,
        "quality_mean": _mean(quality_vals),
        "quality_n": len(quality_vals),
        "route_correct_rate": _mean(route_vals),
        "route_correct_n": len(route_vals),
        "cost_mean": _mean(ok_costs),
        "latency_p50": _nearest_rank(ok_latencies, 50),
        "latency_p95": _nearest_rank(ok_latencies, 95),
        "multi_attempt_rate": multi_attempt / n if n else None,
    }


def _summarize_workload(records: list[dict]) -> dict:
    n = len(records)
    no_eligible = sum(1 for r in records if r.get("status") == "no_eligible")
    backend_failed = sum(1 for r in records if r.get("status") == "backend_failed")
    labeled = sum(1 for r in records if r.get("feedback"))
    return {
        "n": n,
        "no_eligible_rate": no_eligible / n if n else None,
        "backend_failed_rate": backend_failed / n if n else None,
        "label_coverage": labeled / n if n else None,
    }


def _summarize_version_group(records: list[dict]) -> dict:
    by_wc: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    by_w: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        w = _label(r.get("workload"), UNTAGGED_WORKLOAD)
        c = _chosen_label(r)
        by_wc[w][c].append(r)
        by_w[w].append(r)

    return {
        "n": len(records),
        "by_workload_chosen": {
            w: {c: _summarize_bucket(recs) for c, recs in chosen.items()}
            for w, chosen in by_wc.items()
        },
        "by_workload": {w: _summarize_workload(recs) for w, recs in by_w.items()},
    }


def summarize(records: list[dict]) -> dict:
    """Deterministic summary (spec in Task 7's brief). Grouped first by
    `endpoint_registry_version` -- never pooled across versions -- then, per
    version group, by `(workload, chosen)` and by `workload` alone."""
    by_version: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_version[_label(r.get("endpoint_registry_version"), UNKNOWN_REGISTRY_VERSION)
                    ].append(r)

    summary = {
        "n_records": len(records),
        "registry_versions": {
            version: _summarize_version_group(recs)
            for version, recs in by_version.items()
        },
    }
    return _round6(summary)


def _fmt(value) -> str:
    """Render a summary value for the markdown table: `None` is "n/a"."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


_BUCKET_COLUMNS = (
    "n", "served_ok_rate", "verify_pass_rate", "quality_mean", "quality_n",
    "route_correct_rate", "route_correct_n", "cost_mean", "latency_p50",
    "latency_p95", "multi_attempt_rate",
)
_WORKLOAD_COLUMNS = ("n", "no_eligible_rate", "backend_failed_rate", "label_coverage")


def render_report(summary: dict) -> str:
    """Markdown report. Starts with `CAVEAT` verbatim, then one section per
    `endpoint_registry_version` group (sorted), each with a
    `(workload, chosen)` table and a `workload` table."""
    lines: list[str] = [CAVEAT, "", "# Routing outcomes report", "",
                        f"n records: {summary['n_records']}", ""]

    registry_versions = summary["registry_versions"]
    for version in sorted(registry_versions):
        group = registry_versions[version]
        lines += [f"## endpoint_registry_version: {version}", "",
                  f"n: {group['n']}", "",
                  "### By workload / chosen", "",
                  "| workload | chosen | " + " | ".join(_BUCKET_COLUMNS) + " |",
                  "|" + "---|" * (2 + len(_BUCKET_COLUMNS))]
        by_wc = group["by_workload_chosen"]
        for w in sorted(by_wc):
            for c in sorted(by_wc[w]):
                bucket = by_wc[w][c]
                row = [w, c] + [_fmt(bucket[col]) for col in _BUCKET_COLUMNS]
                lines.append("| " + " | ".join(row) + " |")
        lines += ["", "### By workload", "",
                  "| workload | " + " | ".join(_WORKLOAD_COLUMNS) + " |",
                  "|" + "---|" * (1 + len(_WORKLOAD_COLUMNS))]
        by_w = group["by_workload"]
        for w in sorted(by_w):
            wl = by_w[w]
            row = [w] + [_fmt(wl[col]) for col in _WORKLOAD_COLUMNS]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    return "\n".join(lines) + "\n"
