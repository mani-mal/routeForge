"""Pure computation of the Models-page metrics (no DB, no FastAPI).

Inputs are plain row dataclasses; outputs are the JSON-ready response shapes.
Every sample-size gate and status is decided here, server-side.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from app.config.schema import PricingCfg

MIN_N_REQUESTS = 30
MIN_N_TASKS = 20
MIN_N_SPEED = 30
MIN_N_TTFT = 10

_DENOMINATOR = ("ok", "backend_failed", "verify_failed", "context_overflow")
_FAILURES = ("backend_failed", "verify_failed", "context_overflow")


@dataclass(frozen=True)
class UsageRow:
    request_id: str
    application_id: str
    ts: datetime
    model: str
    provider: str
    status: str
    cost_usd: float
    prompt_tokens: int
    cached_prompt_tokens: int | None
    compression_saved_usd: float | None
    routing_saved_usd: float | None
    prompt_cache_saved_usd: float | None
    semantic_cache_saved_usd: float | None


@dataclass(frozen=True)
class OutcomeRow:
    request_id: str
    application_id: str
    ts: datetime
    status: str
    chosen: str | None
    workload: str | None
    trajectory_key: str | None
    latency_ms: float | None
    route_reason: str | None
    attempts: list[str]


@dataclass(frozen=True)
class FeedbackRow:
    request_id: str
    ts: datetime
    score_name: str
    value_num: float | None


def _pctile(sorted_vals: list[float], pct: float) -> float:
    # Nearest rank; same behaviour as app/apps/store.py::_pctile.
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _completed(outcomes: list[OutcomeRow]) -> dict:
    n = sum(1 for o in outcomes if o.status in _DENOMINATOR)
    ok = sum(1 for o in outcomes if o.status == "ok")
    insufficient = n < MIN_N_REQUESTS
    return {
        "value": None if insufficient else ok / n,
        "n": n,
        "insufficient": insufficient,
        "failures": sum(1 for o in outcomes if o.status in _FAILURES),
        "policy_rejections": sum(1 for o in outcomes if o.status == "no_eligible"),
        "excluded": sum(1 for o in outcomes
                        if o.status not in _DENOMINATOR and o.status != "no_eligible"),
    }


@dataclass
class _Task:
    key: str
    apps: set
    cost: float
    cost_by_model: dict
    label: tuple | None = None  # (ts, success)

    @property
    def primary_model(self) -> str | None:
        if not self.cost_by_model:
            return None
        return max(sorted(self.cost_by_model), key=lambda m: self.cost_by_model[m])


def _build_tasks(usage: list[UsageRow], outcomes: list[OutcomeRow],
                 feedback: list[FeedbackRow]) -> tuple[list[_Task], bool]:
    """Group counted outcomes into tasks. A request without a trajectory key is
    its own task. Returns (tasks, any_trajectory_key)."""
    cost_by_req: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for u in usage:
        cost_by_req[u.request_id][u.model] += u.cost_usd
    tasks: dict[str, _Task] = {}
    req_task: dict[str, str] = {}
    any_key = False
    for o in outcomes:
        if o.trajectory_key:
            any_key = True
        if o.status not in _DENOMINATOR:
            continue
        key = f"t:{o.trajectory_key}" if o.trajectory_key else f"r:{o.request_id}"
        t = tasks.setdefault(key, _Task(key, set(), 0.0, defaultdict(float)))
        t.apps.add(o.application_id)
        req_task[o.request_id] = key
        for m, c in cost_by_req.get(o.request_id, {}).items():
            t.cost += c
            t.cost_by_model[m] += c
    for f in feedback:
        if f.score_name != "task_success" or f.value_num is None:
            continue
        key = req_task.get(f.request_id)
        if key is None:
            continue
        t = tasks[key]
        if t.label is None or f.ts >= t.label[0]:
            t.label = (f.ts, f.value_num >= 0.5)
    return list(tasks.values()), any_key


def _cost_per_task(tasks: list[_Task], task_unit: str) -> dict:
    labelled = [t for t in tasks if t.label is not None]
    successes = [t for t in labelled if t.label[1]]
    total = sum(t.cost for t in tasks)
    return {
        "attempted": total / len(tasks) if tasks else None,
        "successful": (sum(t.cost for t in labelled) / len(successes)) if successes else None,
        "n_tasks": len(tasks),
        "n_labelled": len(labelled),
        "task_unit": task_unit,
    }


def summarize(usage: list[UsageRow], outcomes: list[OutcomeRow],
              feedback: list[FeedbackRow], *, workload: str | None = None) -> dict:
    workloads = sorted({o.workload for o in outcomes if o.workload})
    if workload is not None:
        keep = {o.request_id for o in outcomes if o.workload == workload}
        usage = [u for u in usage if u.request_id in keep]
        outcomes = [o for o in outcomes if o.request_id in keep]
        feedback = [f for f in feedback if f.request_id in keep]

    tasks, any_key = _build_tasks(usage, outcomes, feedback)
    unit = "task" if any_key else "request"
    labelled = [t for t in tasks if t.label is not None]
    reported = None
    if labelled:
        insufficient = len(labelled) < MIN_N_TASKS
        reported = {
            "value": None if insufficient else sum(1 for t in labelled if t.label[1]) / len(labelled),
            "n": len(labelled),
            "insufficient": insufficient,
            "apps_reporting": len({a for t in labelled for a in t.apps}),
        }

    counted = [u for u in usage if u.routing_saved_usd is not None]

    def _sum(attr: str) -> float:
        return float(sum(getattr(u, attr) or 0.0 for u in counted))

    sv = {
        "routing": _sum("routing_saved_usd"),
        "prompt_cache": _sum("prompt_cache_saved_usd"),
        "compression": _sum("compression_saved_usd"),
        "semantic_cache": _sum("semantic_cache_saved_usd"),
    }
    sv["total"] = sum(sv.values())
    sv["excluded_rows"] = len(usage) - len(counted)

    reasons: dict[str, int] = defaultdict(int)
    for o in outcomes:
        if o.route_reason:
            reasons[o.route_reason] += 1

    return {
        "spend": {"value": float(sum(u.cost_usd for u in usage)), "n": len(usage)},
        "completed": _completed(outcomes),
        "successful_reported": reported,
        "cost_per_task": _cost_per_task(tasks, unit),
        "savings": sv,
        "route_reasons": dict(reasons),
        "workloads": workloads,
    }


# --------------------------------------------------------------------------
# Per-model rows, statuses and page alerts
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservationRow:
    ts: datetime
    model: str
    outcome: str
    latency_ms: float | None
    streamed: bool
    ttft_ms: float | None
    completion_tokens: int | None


STATUS_ORDER = ("cost_anomaly", "performance_degradation", "cache_regression",
                "insufficient_data", "healthy")

_COST_FACTOR = 2.0
_LATENCY_FACTOR = 1.5
_CACHE_DROP = 0.15


def _cache_rate(usage: list[UsageRow]) -> float | None:
    rows = [u for u in usage if u.status == "ok" and u.cached_prompt_tokens is not None]
    prompt = sum(u.prompt_tokens for u in rows)
    if prompt <= 0:
        return None
    return sum(u.cached_prompt_tokens or 0 for u in rows) / prompt


def _p95_latency(outcomes: list[OutcomeRow]) -> float | None:
    vals = sorted(o.latency_ms for o in outcomes
                  if o.status == "ok" and o.latency_ms is not None)
    return _pctile(vals, 0.95) if vals else None


def _speed(obs: list[ObservationRow]) -> dict:
    vals = sorted(o.completion_tokens / (o.latency_ms / 1000.0) for o in obs
                  if o.outcome == "ok" and not o.streamed and o.completion_tokens
                  and o.latency_ms and o.latency_ms > 0)
    insufficient = len(vals) < MIN_N_SPEED
    return {"value": None if insufficient else _pctile(vals, 0.5), "n": len(vals),
            "insufficient": insufficient, "scope": "all_applications"}


def _ttft(obs: list[ObservationRow]) -> dict:
    vals = sorted(o.ttft_ms for o in obs
                  if o.outcome == "ok" and o.streamed and o.ttft_ms is not None)
    insufficient = len(vals) < MIN_N_TTFT
    return {"p50": None if insufficient else _pctile(vals, 0.5),
            "p95": None if insufficient else _pctile(vals, 0.95), "n": len(vals),
            "insufficient": insufficient, "scope": "all_applications"}


def _status(requests: int, spend: float, p95: float | None, cache: float | None,
            prev_usage: list[UsageRow], prev_outcomes: list[OutcomeRow]) -> tuple[str, list[str]]:
    reasons: dict[str, str] = {}
    n_prev = len(prev_usage)
    if requests >= MIN_N_REQUESTS and n_prev >= MIN_N_REQUESTS:
        prev_spend = sum(u.cost_usd for u in prev_usage)
        cur_pr, prev_pr = spend / requests, prev_spend / n_prev
        if prev_pr > 0 and cur_pr > _COST_FACTOR * prev_pr:
            reasons["cost_anomaly"] = (
                f"spend per request ${cur_pr:.2f} vs ${prev_pr:.2f} previous")
        prev_p95 = _p95_latency(prev_outcomes)
        if p95 is not None and prev_p95 and p95 > _LATENCY_FACTOR * prev_p95:
            reasons["performance_degradation"] = (
                f"p95 latency {p95:.0f} ms vs {prev_p95:.0f} ms previous")
        prev_cache = _cache_rate(prev_usage)
        if cache is not None and prev_cache is not None and prev_cache - cache > _CACHE_DROP:
            reasons["cache_regression"] = (
                f"cache-hit rate {cache:.0%} vs {prev_cache:.0%} previous")
    if requests < MIN_N_REQUESTS:
        reasons["insufficient_data"] = f"only {requests} requests (minimum {MIN_N_REQUESTS})"
    status = next((s for s in STATUS_ORDER if s in reasons), "healthy")
    # insufficient_data is a status of its own, not a reason beside real findings.
    listed = [reasons[s] for s in STATUS_ORDER if s in reasons and s != "insufficient_data"]
    if status == "insufficient_data":
        listed = [reasons["insufficient_data"]]
    return status, listed


def model_rows(cur_usage: list[UsageRow], cur_outcomes: list[OutcomeRow],
               cur_feedback: list[FeedbackRow], prev_usage: list[UsageRow],
               prev_outcomes: list[OutcomeRow], observations: list[ObservationRow],
               *, placement_of: Callable[[str], str]) -> list[dict]:
    models = {u.model for u in cur_usage} | {o.chosen for o in cur_outcomes if o.chosen}
    tasks, any_key = _build_tasks(cur_usage, cur_outcomes, cur_feedback)
    unit = "task" if any_key else "request"
    rows: list[dict] = []
    for m in models:
        usage = [u for u in cur_usage if u.model == m]
        outs = [o for o in cur_outcomes if o.chosen == m]
        mtasks = [t for t in tasks if t.primary_model == m]
        labelled = [t for t in mtasks if t.label is not None]
        successes = [t for t in labelled if t.label[1]]
        insufficient = len(labelled) < MIN_N_TASKS
        spend = float(sum(u.cost_usd for u in usage))
        p95 = _p95_latency(outs)
        cache = _cache_rate(usage)
        status, reasons = _status(
            len(usage), spend, p95, cache,
            [u for u in prev_usage if u.model == m],
            [o for o in prev_outcomes if o.chosen == m])
        mobs = [o for o in observations if o.model == m]
        rows.append({
            "model": m,
            "placement": placement_of(m),
            "requests": len(usage),
            "spend": spend,
            "completed": _completed(outs),
            "cost_per_successful_task": {
                "value": None if insufficient or not successes
                else sum(t.cost for t in labelled) / len(successes),
                "n": len(labelled), "insufficient": insufficient},
            "cost_per_task": _cost_per_task(mtasks, unit),
            "p95_latency_ms": p95,
            "cache_hit_rate": cache,
            "errors": sum(1 for o in outs if o.status in _FAILURES),
            "failovers": sum(1 for o in outs if o.route_reason == "failover"),
            "speed_tps": _speed(mobs),
            "ttft_ms": _ttft(mobs),
            "status": status,
            "status_reasons": reasons,
        })
    rows.sort(key=lambda r: (-r["spend"], r["model"]))
    return rows


def page_alerts(pricing: PricingCfg, *, today: date, excluded_rows: int,
                models: list[dict]) -> list[dict]:
    alerts: list[dict] = []
    dated: list[tuple[str, date | None]] = [(n, p.as_of) for n, p in pricing.models.items()]
    dated.append((f"baseline ({pricing.baseline_model})", pricing.baseline_as_of))
    undated = [n for n, d in dated if d is None]
    if undated:
        alerts.append({"kind": "prices_undated", "severity": "warning", "model": None,
                       "message": "Prices without a check date: " + ", ".join(sorted(undated))})
    cutoff = today - timedelta(days=pricing.pricing_stale_days)
    stale = [n for n, d in dated if d is not None and d < cutoff]
    if stale:
        alerts.append({"kind": "prices_stale", "severity": "warning", "model": None,
                       "message": (f"Prices last checked more than {pricing.pricing_stale_days} "
                                   "days ago: " + ", ".join(sorted(stale)))})
    if excluded_rows > 0:
        alerts.append({"kind": "savings_partial", "severity": "info", "model": None,
                       "message": (f"{excluded_rows} requests predate savings tracking and "
                                   "are left out of the savings figures")})
    flagged = [m for m in models if m["status"] not in ("healthy", "insufficient_data")]
    flagged.sort(key=lambda m: STATUS_ORDER.index(m["status"]))
    for m in flagged:
        alerts.append({"kind": m["status"], "severity": "warning", "model": m["model"],
                       "message": "; ".join(m["status_reasons"])})
    return alerts
