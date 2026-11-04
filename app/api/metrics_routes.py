"""Models-page metrics endpoints (admin only, content-free)."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.apps.admin_auth import require_admin
from app.metrics.compute import (filter_workload, model_rows, page_alerts, summarize)
from app.metrics.load import load_observations, load_window, placement_for_provider

metrics_router = APIRouter(prefix="/admin/metrics", tags=["admin"])

Placement = Literal["onprem", "cloud"]


def _window(hours: int) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    until = dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(hours=hours)
    return since - dt.timedelta(hours=hours), since, until


def _window_json(hours: int, since: dt.datetime, until: dt.datetime) -> dict:
    return {"hours": hours, "since": since.isoformat(), "until": until.isoformat()}


async def _load_all(request: Request, hours, application_id, workload, placement):
    prev_since, since, until = _window(hours)
    async with request.app.state.db.session() as session:
        cur = await load_window(session, since=since, until=until,
                                application_id=application_id, placement=placement)
        prev = await load_window(session, since=prev_since, until=since,
                                 application_id=application_id, placement=placement)
        obs = await load_observations(session, since=since, until=until)
    if workload is not None:
        cur = filter_workload(*cur, workload)
        prev = filter_workload(*prev, workload)
    provider_of = {u.model: u.provider for u in [*cur[0], *prev[0]]}
    rows = model_rows(*cur, prev[0], prev[1], obs,
                      placement_of=lambda m: placement_for_provider(provider_of.get(m)))
    return cur, prev, rows, since, until


@metrics_router.get("/summary")
async def metrics_summary(
        request: Request, hours: int = Query(720, ge=1, le=8760),
        application_id: str | None = None, workload: str | None = None,
        placement: Placement | None = None, _: str = Depends(require_admin)) -> dict:
    cur, prev, rows, since, until = await _load_all(
        request, hours, application_id, workload, placement)
    current, previous = summarize(*cur), summarize(*prev)
    pricing = request.app.state.settings.pricing
    now = dt.datetime.now(dt.timezone.utc)
    alerts = page_alerts(pricing, today=now.date(),
                         excluded_rows=current["savings"]["excluded_rows"], models=rows)
    return {"window": _window_json(hours, since, until), "current": current,
            "previous": previous, "alerts": alerts,
            "baseline": {"model": pricing.baseline_model,
                         "as_of": pricing.baseline_as_of.isoformat()
                         if pricing.baseline_as_of else None},
            "generated_at": now.isoformat()}


@metrics_router.get("/models")
async def metrics_models(
        request: Request, hours: int = Query(720, ge=1, le=8760),
        application_id: str | None = None, workload: str | None = None,
        placement: Placement | None = None, _: str = Depends(require_admin)) -> dict:
    _cur, _prev, rows, since, until = await _load_all(
        request, hours, application_id, workload, placement)
    return {"window": _window_json(hours, since, until), "models": rows,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
