"""Models-page metrics endpoints (admin only, content-free)."""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.apps import store
from app.apps.admin_auth import require_admin
from app.apps.models import AppPolicy
from app.metrics import external as ext
from app.metrics.compute import (apply_placement, filter_workload, make_placement_of,
                                 app_view, model_rows, page_alerts, summarize)
from app.metrics.load import load_observations, load_usage, load_window

metrics_router = APIRouter(prefix="/admin/metrics", tags=["admin"])

Placement = Literal["onprem", "cloud"]


def _window(hours: int) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    until = dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(hours=hours)
    return since - dt.timedelta(hours=hours), since, until


def _window_json(hours: int, since: dt.datetime, until: dt.datetime) -> dict:
    return {"hours": hours, "since": since.isoformat(), "until": until.isoformat()}


async def _load_raw(request: Request, hours: int, application_id: str | None):
    prev_since, since, until = _window(hours)
    async with request.app.state.db.session() as session:
        cur = await load_window(session, since=since, until=until,
                                application_id=application_id)
        prev = await load_window(session, since=prev_since, until=since,
                                 application_id=application_id)
        obs = await load_observations(session, since=since, until=until)
    return cur, prev, obs, since, until


async def _load_shared(request: Request, hours: int, application_id: str | None):
    """The page fires /summary and /models together: concurrent identical loads
    share one scan. Nothing is kept once the scan finishes, so data never goes stale."""
    inflight = getattr(request.app.state, "metrics_inflight", None)
    if inflight is None:
        inflight = request.app.state.metrics_inflight = {}
    key = (hours, application_id)
    task = inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_load_raw(request, hours, application_id))
        inflight[key] = task
        task.add_done_callback(lambda _t: inflight.pop(key, None))
    return await asyncio.shield(task)


async def _load_all(request: Request, hours, application_id, workload, placement):
    cur, prev, obs, since, until = await _load_shared(request, hours, application_id)
    pool = getattr(request.app.state, "registry", None)
    pool_providers = {c.name: c.provider for c in pool.effective_pool()} if pool else {}
    placement_of = make_placement_of(pool_providers, [*cur[0], *prev[0]])
    if placement:
        cur = apply_placement(*cur, placement, placement_of)
        prev = apply_placement(*prev, placement, placement_of)
    cur_f, prev_f = cur, prev
    if workload is not None:
        cur_f = filter_workload(*cur, workload)
        prev_f = filter_workload(*prev, workload)
    # AA data is cheap and read per request, outside the shared window scan.
    async with request.app.state.db.session() as session:
        snapshot = await ext.latest_snapshot(session)
        published = await ext.snapshot_models(session, snapshot.id) if snapshot else {}
        model_map = await ext.get_map(session)
    prices = request.app.state.settings.pricing.models
    rows = model_rows(*cur_f, prev_f[0], prev_f[1], obs, placement_of=placement_of,
                      published=published, model_map=model_map, prices=prices,
                      snapshot=snapshot)
    return cur, prev, rows, since, until, snapshot


@metrics_router.get("/summary")
async def metrics_summary(
        request: Request, hours: int = Query(720, ge=1, le=8760),
        application_id: str | None = None, workload: str | None = None,
        placement: Placement | None = None, _: str = Depends(require_admin)) -> dict:
    cur, prev, rows, since, until, snapshot = await _load_all(
        request, hours, application_id, workload, placement)
    kw = {"workload": workload, "placement_filtered": placement is not None}
    current, previous = summarize(*cur, **kw), summarize(*prev, **kw)
    pricing = request.app.state.settings.pricing
    now = dt.datetime.now(dt.timezone.utc)
    alerts = page_alerts(pricing, today=now.date(),
                         excluded_rows=current["savings"]["excluded_rows"], models=rows,
                         snapshot=snapshot)
    return {"window": _window_json(hours, since, until), "current": current,
            "previous": previous, "alerts": alerts,
            "published": None if snapshot is None else {
                "snapshot_id": snapshot.id,
                "retrieved_at": ext.aware(snapshot.retrieved_at).isoformat(),
                "dated_at_upload": bool(snapshot.dated_at_upload),
                "index_version": snapshot.index_version, "source": snapshot.source,
                "origin": snapshot.origin},
            "baseline": {"model": pricing.baseline_model,
                         "as_of": pricing.baseline_as_of.isoformat()
                         if pricing.baseline_as_of else None},
            "generated_at": now.isoformat()}


@metrics_router.get("/models")
async def metrics_models(
        request: Request, hours: int = Query(720, ge=1, le=8760),
        application_id: str | None = None, workload: str | None = None,
        placement: Placement | None = None, _: str = Depends(require_admin)) -> dict:
    _cur, _prev, rows, since, until, _snap = await _load_all(
        request, hours, application_id, workload, placement)
    return {"window": _window_json(hours, since, until), "models": rows,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


@metrics_router.get("/app/{app_id}")
async def metrics_app(
        request: Request, app_id: str, hours: int = Query(720, ge=1, le=8760),
        _: str = Depends(require_admin)) -> dict:
    """The team-lead view of one application: budget and forecast, task and cost
    metrics, spend by workload, models used and why."""
    async with request.app.state.db.session() as session:
        application = await store.get_application(session, app_id)
        if application is None:
            raise HTTPException(404, f"unknown application {app_id!r}")
        policy = await session.get(AppPolicy, app_id)
        budget = policy.monthly_budget_usd if policy else None
    now = dt.datetime.now(dt.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cur, prev, _obs, since, until = await _load_shared(request, hours, app_id)
    async with request.app.state.db.session() as session:
        month_usage = await load_usage(session, since=month_start, until=now,
                                       application_id=app_id)
    kw = {"month_usage": month_usage, "monthly_budget_usd": budget, "now": now}
    return {"application_id": app_id, "window": _window_json(hours, since, until),
            "current": app_view(*cur, **kw), "previous": app_view(*prev, **kw),
            "generated_at": now.isoformat()}
