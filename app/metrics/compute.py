"""Pure computation of the Models-page metrics (no DB, no FastAPI).

Inputs are plain row dataclasses; outputs are the JSON-ready response shapes.
Every sample-size gate and status is decided here, server-side.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cached_property
from math import floor, log10
from typing import Callable

from app.config.schema import PriceCfg, PricingCfg
from app.metrics.external import AAModel, aware as ext_aware, comparability

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
    id: int = 0  # tie-break for equal timestamps: the higher id is the later label


_NO_MODEL = ("", "-", "?")  # usage-row placeholders: cancelled / budget 429 / unknown name


def is_real_model(model: str | None) -> bool:
    return bool(model) and model not in _NO_MODEL


def attributed_model(o: OutcomeRow) -> str | None:
    """The model an outcome belongs to. The writer stores chosen=None for
    backend_failed / context_overflow / no_eligible / cancelled; for the first
    two the last attempted candidate is the model that failed."""
    return o.chosen or (o.attempts[-1] if o.attempts else None)


def placement_for_provider(provider: str | None) -> str:
    if provider == "onprem":
        return "onprem"
    if provider in (None, "", "-", "?"):
        return "unknown"
    return "cloud"


def make_placement_of(pool_providers: dict[str, str],
                      usage: list[UsageRow]) -> Callable[[str], str]:
    """Placement of a model: the registry's provider first, else the providers
    seen on its usage rows (ignoring "-" / "?"), else "unknown"."""
    seen: dict[str, set[str]] = defaultdict(set)
    for u in usage:
        if u.provider not in ("-", "?", ""):
            seen[u.model].add(u.provider)

    def placement_of(model: str) -> str:
        if model in pool_providers:
            return placement_for_provider(pool_providers[model])
        provs = seen.get(model)
        if not provs:
            return "unknown"
        return "onprem" if "onprem" in provs else "cloud"
    return placement_of


def apply_placement(usage: list[UsageRow], outcomes: list[OutcomeRow],
                    feedback: list[FeedbackRow], placement: str,
                    placement_of: Callable[[str], str]):
    """Keep rows of one placement. Outcomes are attributed to a model first;
    unattributable ones (no_eligible, cancelled, overflow with no attempt) are
    dropped from a placement view."""
    def u_place(u: UsageRow) -> str:
        return placement_of(u.model) if is_real_model(u.model) \
            else placement_for_provider(u.provider)
    usage = [u for u in usage if u_place(u) == placement]
    outcomes = [o for o in outcomes
                if (m := attributed_model(o)) is not None and placement_of(m) == placement]
    keep = {o.request_id for o in outcomes}
    return usage, outcomes, [f for f in feedback if f.request_id in keep]


def money_text(v: float) -> str:
    """Dollars for status reasons: 2 dp from a cent up, else 4 significant digits."""
    if v == 0:
        return "$0.00"
    if abs(v) >= 0.01:
        return f"${v:.2f}"
    return f"${v:.{3 - floor(log10(abs(v)))}f}".rstrip("0")


def _pctile(sorted_vals: list[float], pct: float) -> float:
    # Nearest rank; same behaviour as app/apps/store.py::_pctile.
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _completed(outcomes: list[OutcomeRow], *, attributable: bool = True) -> dict:
    n = sum(1 for o in outcomes if o.status in _DENOMINATOR)
    ok = sum(1 for o in outcomes if o.status == "ok")
    insufficient = n < MIN_N_REQUESTS
    return {
        "value": None if insufficient else ok / n,
        "n": n,
        "insufficient": insufficient,
        "failures": sum(1 for o in outcomes if o.status in _FAILURES),
        # B70 item 1: of those failures, the requests whose only attempted
        # endpoint failed with nothing left to fail over to.
        "no_failover": sum(1 for o in outcomes
                           if o.status in _FAILURES and o.route_reason == "no_failover"),
        # Rejections and cancels carry no model, so a placement-filtered view
        # cannot attribute them: null, not a misleading 0.
        "policy_rejections": (sum(1 for o in outcomes if o.status == "no_eligible")
                              if attributable else None),
        "excluded": (sum(1 for o in outcomes
                         if o.status not in _DENOMINATOR and o.status != "no_eligible")
                     if attributable else None),
    }


@dataclass
class _Task:
    key: str
    apps: set
    cost: float
    cost_by_model: dict
    ok_by_model: dict
    label: tuple | None = None  # (ts, success, feedback id)
    rows: list = field(default_factory=list)  # the task's counted OutcomeRows

    @cached_property
    def primary_model(self) -> str | None:
        """Largest spend; ties go to a model that actually served, then name."""
        if not self.cost_by_model:
            return None
        return max(sorted(self.cost_by_model),
                   key=lambda m: (self.cost_by_model[m], self.ok_by_model.get(m, 0)))


def _build_tasks(usage: list[UsageRow], outcomes: list[OutcomeRow],
                 feedback: list[FeedbackRow]) -> tuple[list[_Task], bool]:
    """Group counted outcomes into tasks. A request without a trajectory key is
    its own task. Returns (tasks, any_trajectory_key)."""
    cost_by_req: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    ok_by_req: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for u in usage:
        cost_by_req[u.request_id][u.model] += u.cost_usd
        if u.status == "ok":
            ok_by_req[u.request_id][u.model] += 1
    tasks: dict[str, _Task] = {}
    req_task: dict[str, str] = {}
    any_key = False
    for o in outcomes:
        if o.trajectory_key:
            any_key = True
        if o.status not in _DENOMINATOR:
            continue
        key = f"t:{o.trajectory_key}" if o.trajectory_key else f"r:{o.request_id}"
        t = tasks.setdefault(key, _Task(key, set(), 0.0, defaultdict(float), defaultdict(int)))
        t.apps.add(o.application_id)
        t.rows.append(o)
        req_task[o.request_id] = key
        for m, c in cost_by_req.get(o.request_id, {}).items():
            t.cost += c
            t.cost_by_model[m] += c
        for m, n in ok_by_req.get(o.request_id, {}).items():
            t.ok_by_model[m] += n
    for f in feedback:
        # 1 = success, 0 = failure; any other value is not a label.
        if f.score_name != "task_success" or f.value_num not in (0, 1):
            continue
        key = req_task.get(f.request_id)
        if key is None:
            continue
        t = tasks[key]
        if t.label is None or (f.ts, f.id) >= (t.label[0], t.label[2]):
            t.label = (f.ts, f.value_num == 1, f.id)
    return list(tasks.values()), any_key


def _reported(tasks: list[_Task]) -> dict | None:
    """Reported success over the labelled tasks; None when nothing is labelled."""
    labelled = [t for t in tasks if t.label is not None]
    if not labelled:
        return None
    insufficient = len(labelled) < MIN_N_TASKS
    return {
        "value": None if insufficient
        else sum(1 for t in labelled if t.label[1]) / len(labelled),
        "n": len(labelled), "insufficient": insufficient,
        "apps_reporting": len({a for t in labelled for a in t.apps}),
    }


def _cost_per_task(tasks: list[_Task], task_unit: str) -> dict:
    labelled = [t for t in tasks if t.label is not None]
    successes = [t for t in labelled if t.label[1]]
    total = sum(t.cost for t in tasks)
    insufficient = len(labelled) < MIN_N_TASKS
    return {
        "attempted": total / len(tasks) if tasks else None,
        "successful": (None if insufficient or not successes
                       else sum(t.cost for t in labelled) / len(successes)),
        "successful_insufficient": insufficient,
        "n_tasks": len(tasks),
        "n_labelled": len(labelled),
        "task_unit": task_unit,
    }


def filter_workload(usage: list[UsageRow], outcomes: list[OutcomeRow],
                    feedback: list[FeedbackRow], workload: str):
    """Keep only requests whose outcome row carries `workload`. A usage row with
    no outcome row (e.g. a daily-budget 429) is dropped."""
    keep = {o.request_id for o in outcomes if o.workload == workload}
    return ([u for u in usage if u.request_id in keep],
            [o for o in outcomes if o.request_id in keep],
            [f for f in feedback if f.request_id in keep])


def summarize(usage: list[UsageRow], outcomes: list[OutcomeRow],
              feedback: list[FeedbackRow], *, workload: str | None = None,
              placement_filtered: bool = False) -> dict:
    workloads = sorted({o.workload for o in outcomes if o.workload})
    if workload is not None:
        usage, outcomes, feedback = filter_workload(usage, outcomes, feedback, workload)

    tasks, any_key = _build_tasks(usage, outcomes, feedback)
    unit = "task" if any_key else "request"
    reported = _reported(tasks)

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

    # "Why this tier" shares: `no_failover` marks a FAILED request (nothing was
    # served), so it is counted under `completed.no_failover`, not here.
    reasons: dict[str, int] = defaultdict(int)
    for o in outcomes:
        if o.route_reason and o.route_reason != "no_failover":
            reasons[o.route_reason] += 1

    return {
        "spend": {"value": float(sum(u.cost_usd for u in usage)), "n": len(usage)},
        "completed": _completed(outcomes, attributable=not placement_filtered),
        "empty": not usage and not outcomes,
        "successful_reported": reported,
        "cost_per_task": _cost_per_task(tasks, unit),
        "savings": sv,
        "route_reasons": dict(reasons),
        "workloads": workloads,
    }


# --------------------------------------------------------------------------
# Team-lead view of one application
# --------------------------------------------------------------------------

_RETRY_REASONS = ("failover", "escalation")
_RISK_WATCH = 0.9
_RISK_HIGH = 1.1


def _month_budget(month_usage: list[UsageRow], monthly_budget_usd: float | None,
                  now: datetime) -> dict:
    """Month-to-date spend, the month-end forecast and the risk band.

    The month is the calendar month of `now` in UTC. The forecast is spend per
    elapsed day (fractional, at least one day, so the first hours of a month
    cannot project an absurd figure) times the days in the month. Risk compares
    the forecast to the budget: below 90% low, 90-110% watch, above 110% high;
    null without a budget."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_start = (start + timedelta(days=32)).replace(day=1)
    days_in_month = (next_start - start).days
    mtd = float(sum(u.cost_usd for u in month_usage))
    elapsed_days = max(1.0, (now - start).total_seconds() / 86400.0)
    forecast = mtd / elapsed_days * days_in_month
    risk = None
    if monthly_budget_usd:
        ratio = forecast / monthly_budget_usd
        risk = "low" if ratio < _RISK_WATCH else "watch" if ratio <= _RISK_HIGH else "high"
    return {"monthly_budget_usd": monthly_budget_usd, "month_to_date_usd": mtd,
            "forecast_usd": forecast, "risk": risk, "month": start.strftime("%Y-%m")}


def _p95_task_time(tasks: list[_Task], outcomes: list[OutcomeRow], unit: str) -> dict:
    """p95 task duration over tasks with at least one ok request. `ts` is when a
    request FINISHED, so a task runs from (first.ts - first.latency) to last.ts:
    last.ts - first.ts + the first request's latency. With request units, p95
    request latency of ok requests."""
    if unit == "request":
        vals = sorted(o.latency_ms for o in outcomes
                      if o.status == "ok" and o.latency_ms is not None)
        gate = MIN_N_REQUESTS
    else:
        vals = []
        for t in tasks:
            if not any(o.status == "ok" for o in t.rows):
                continue
            rows = sorted(t.rows, key=lambda o: o.ts)
            span_ms = (rows[-1].ts - rows[0].ts).total_seconds() * 1000.0
            vals.append(span_ms + (rows[0].latency_ms or 0.0))
        vals.sort()
        gate = MIN_N_TASKS
    insufficient = len(vals) < gate
    return {"value": None if insufficient else _pctile(vals, 0.95), "n": len(vals),
            "insufficient": insufficient, "task_unit": unit}


def app_view(usage: list[UsageRow], outcomes: list[OutcomeRow], feedback: list[FeedbackRow],
             *, month_usage: list[UsageRow], monthly_budget_usd: float | None,
             now: datetime) -> dict:
    """One application over one window, for its team lead. `usage`/`outcomes`/
    `feedback` are the window's rows; `month_usage` is the calendar month's
    usage, independent of the window."""
    tasks, any_key = _build_tasks(usage, outcomes, feedback)
    unit = "task" if any_key else "request"
    reported = _reported(tasks)

    by_req = {o.request_id: o for o in outcomes}
    spend: dict[str | None, float] = defaultdict(float)
    retry_usd = 0.0
    for u in usage:
        o = by_req.get(u.request_id)
        spend[o.workload if o else None] += u.cost_usd
        if o is not None and o.route_reason in _RETRY_REASONS:
            retry_usd += u.cost_usd

    routed = [o for o in outcomes if attributed_model(o) is not None]
    models = Counter(attributed_model(o) for o in routed)
    # `no_failover` is a failed request, not a reason for a served tier.
    reasons = Counter(o.route_reason for o in routed
                      if o.route_reason and o.route_reason != "no_failover")
    n_reasons = sum(reasons.values())
    return {
        "budget": _month_budget(month_usage, monthly_budget_usd, now),
        "cost_per_task": _cost_per_task(tasks, unit),
        "successful_reported": reported,
        "completed": _completed(outcomes),
        "p95_task_time_ms": _p95_task_time(tasks, outcomes, unit),
        "spend_by_workload": [
            {"workload": w, "usd": float(v)}
            for w, v in sorted(spend.items(), key=lambda kv: (-kv[1], kv[0] or ""))],
        "retries_and_failovers_usd": float(retry_usd),
        "models_used": [
            {"model": m, "requests": n, "share": n / len(routed)}
            for m, n in sorted(models.items(), key=lambda kv: (-kv[1], kv[0]))],
        "route_reasons": [
            {"reason": r, "count": n, "share": n / n_reasons}
            for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))],
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
                "price_drift", "insufficient_data", "healthy")
_PRICE_DRIFT = 0.10
_UNSET: object = object()  # page_alerts(snapshot=...) not supplied: skip AA alerts
AA_STALE_DAYS = 30

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
            prev_usage: list[UsageRow], prev_outcomes: list[OutcomeRow],
            drift: str | None = None) -> tuple[str, list[str]]:
    reasons: dict[str, str] = {}
    if drift:
        reasons["price_drift"] = drift
    n_prev = len(prev_usage)
    if requests >= MIN_N_REQUESTS and n_prev >= MIN_N_REQUESTS:
        prev_spend = sum(u.cost_usd for u in prev_usage)
        cur_pr, prev_pr = spend / requests, prev_spend / n_prev
        if prev_pr > 0 and cur_pr > _COST_FACTOR * prev_pr:
            reasons["cost_anomaly"] = (
                f"spend per request {money_text(cur_pr)} vs {money_text(prev_pr)} previous")
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


def _group(rows, key) -> dict:
    out: dict = defaultdict(list)
    for r in rows:
        k = key(r)
        if k is not None:
            out[k].append(r)
    return out


def _chart_point(cost_succ: dict, cost_task: dict, reported: dict, completed: dict) -> dict | None:
    """The one point this model contributes to the cost-vs-success chart, chosen
    here so the UI plots it as given. Null when either axis lacks data."""
    if cost_succ["value"] is not None:
        x, x_kind = cost_succ["value"], "per_successful_task"
    elif cost_task["attempted"] is not None:
        x, x_kind = cost_task["attempted"], "per_task"
    else:
        return None
    if reported["value"] is not None:
        y, y_kind = reported["value"], "reported"
    elif completed["value"] is not None:
        y, y_kind = completed["value"], "completed"
    else:
        return None
    return {"x": x, "x_kind": x_kind, "y": y, "y_kind": y_kind}


def _price_drift(model: str, aa: AAModel, price: PriceCfg) -> str | None:
    """Cloud list price in pricing.yaml vs AA's, either side off by more than 10%."""
    parts = []
    for label, ours, theirs in (("input", price.input, aa.price_in),
                                ("output", price.output, aa.price_out)):
        if theirs is None or theirs < 0:
            continue
        if theirs == 0:  # AA lists it as free: flag any nonzero price here (no division)
            if ours > 0:
                parts.append(f"{label} ${ours:g}/1M in pricing.yaml vs free at AA")
            continue
        if abs(ours - theirs) / theirs > _PRICE_DRIFT:
            parts.append(f"{label} ${ours:g}/1M in pricing.yaml vs ${theirs:g}/1M at AA")
    return "; ".join(parts) if parts else None

def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    mid = len(v) // 2
    return float(v[mid]) if len(v) % 2 else (v[mid - 1] + v[mid]) / 2

def _published_json(aa: AAModel, snapshot) -> dict:
    d = {f: getattr(aa, f) for f in aa.__dataclass_fields__}
    d["snapshot_id"] = snapshot.id if snapshot is not None else None
    d["retrieved_at"] = (
        ext_aware(snapshot.retrieved_at).isoformat() if snapshot is not None else None)
    d["dated_at_upload"] = bool(snapshot.dated_at_upload) if snapshot is not None else False
    d["index_version"] = snapshot.index_version if snapshot is not None else None
    return d

def model_rows(cur_usage: list[UsageRow], cur_outcomes: list[OutcomeRow],
               cur_feedback: list[FeedbackRow], prev_usage: list[UsageRow],
               prev_outcomes: list[OutcomeRow], observations: list[ObservationRow],
               *, placement_of: Callable[[str], str],
               published: dict[str, AAModel] | None = None,
               model_map: dict[str, str] | None = None,
               prices: dict[str, PriceCfg] | None = None,
               snapshot=None) -> list[dict]:
    published, model_map, prices = published or {}, model_map or {}, prices or {}
    real = [u for u in cur_usage if is_real_model(u.model)]
    usage_by = _group(real, lambda u: u.model)
    outs_by = _group(cur_outcomes, attributed_model)
    prev_usage_by = _group(prev_usage, lambda u: u.model)
    prev_outs_by = _group(prev_outcomes, attributed_model)
    obs_by = _group(observations, lambda o: o.model)
    models = {m for m in {*usage_by, *outs_by} if is_real_model(m)}
    tasks, any_key = _build_tasks(cur_usage, cur_outcomes, cur_feedback)
    unit = "task" if any_key else "request"
    tasks_by = _group(tasks, lambda t: t.primary_model)
    rows: list[dict] = []
    for m in models:
        usage = usage_by.get(m, [])
        outs = outs_by.get(m, [])
        mtasks = tasks_by.get(m, [])
        labelled = [t for t in mtasks if t.label is not None]
        successes = [t for t in labelled if t.label[1]]
        insufficient = len(labelled) < MIN_N_TASKS
        spend = float(sum(u.cost_usd for u in usage))
        p95 = _p95_latency(outs)
        cache = _cache_rate(usage)
        placement = placement_of(m)
        slug = model_map.get(m)
        aa = published.get(slug) if slug else None
        drift = (_price_drift(m, aa, prices[m])
                 if aa is not None and placement == "cloud" and m in prices else None)
        status, reasons = _status(
            len(usage), spend, p95, cache,
            prev_usage_by.get(m, []), prev_outs_by.get(m, []), drift)
        mobs = obs_by.get(m, [])
        provs = Counter(u.provider for u in usage if u.provider not in _NO_MODEL)
        comp = comparability(
            mapped=slug is not None, in_snapshot=aa is not None, placement=placement,
            provider=provs.most_common(1)[0][0] if provs else None,
            speed_n=_speed(mobs)["n"], ttft_n=_ttft(mobs)["n"],
            median_prompt_tokens=_median([u.prompt_tokens for u in usage if u.status == "ok"]))
        completed = _completed(outs)
        cost_succ = {
            "value": None if insufficient or not successes
            else sum(t.cost for t in labelled) / len(successes),
            "n": len(labelled), "insufficient": insufficient}
        reported = {
            "value": None if insufficient else len(successes) / len(labelled),
            "n": len(labelled), "insufficient": insufficient}
        cost_task = _cost_per_task(mtasks, unit)
        rows.append({
            "model": m,
            "placement": placement,
            "requests": len(usage),
            "spend": spend,
            "completed": completed,
            "successful_reported": reported,
            "cost_per_successful_task": cost_succ,
            "cost_per_task": cost_task,
            "chart": _chart_point(cost_succ, cost_task, reported, completed),
            "p95_latency_ms": p95,
            "cache_hit_rate": cache,
            "errors": sum(1 for o in outs if o.status in _FAILURES),
            "failovers": sum(1 for o in outs if o.route_reason == "failover"),
            "speed_tps": _speed(mobs),
            "ttft_ms": _ttft(mobs),
            "status": status,
            "status_rank": STATUS_ORDER.index(status),
            "status_reasons": reasons,
            "published": _published_json(aa, snapshot) if aa is not None else None,
            "comparability": comp,
        })
    rows.sort(key=lambda r: (-r["spend"], r["model"]))
    return rows


def page_alerts(pricing: PricingCfg, *, today: date, excluded_rows: int,
                models: list[dict], snapshot=_UNSET) -> list[dict]:
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
    if snapshot is not _UNSET:
        if snapshot is None:
            alerts.append({"kind": "aa_snapshot_missing", "severity": "info", "model": None,
                           "message": "No Artificial Analysis snapshot imported yet"})
        else:
            got = ext_aware(snapshot.retrieved_at)
            age = (today - got.date()).days
            if age > AA_STALE_DAYS:
                alerts.append({"kind": "aa_snapshot_stale", "severity": "warning",
                               "model": None,
                               "message": (f"Artificial Analysis snapshot is {age} days old "
                                           f"(retrieved {got.date().isoformat()})")})
    flagged = [m for m in models if m["status"] not in ("healthy", "insufficient_data")]
    flagged.sort(key=lambda m: STATUS_ORDER.index(m["status"]))
    for m in flagged:
        alerts.append({"kind": m["status"], "severity": "warning", "model": m["model"],
                       "message": "; ".join(m["status_reasons"])})
    return alerts
