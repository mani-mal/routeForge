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
