"""Admin endpoints for published (Artificial Analysis) metrics.

AA is called ONLY from `POST /refresh` (admin-triggered) and scripts/aa_fetch.py,
never on the request path. The outbound request carries the API key header only:
no tenant, model-usage or request data.
"""
from __future__ import annotations

import datetime as dt
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.apps.admin_auth import require_admin
from app.metrics import external as ext
from app.metrics.compute import placement_for_provider

AA_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
AA_TIMEOUT_S = 20.0
REFRESH_MIN_INTERVAL_S = 600
MAX_BODY_BYTES = 10 * 1024 * 1024
_ADVISORY_LOCK_KEY = 7_302_411

external_metrics_router = APIRouter(
    prefix="/admin/external-metrics", tags=["admin"], dependencies=[Depends(require_admin)])


async def fetch_aa(api_key: str, *,
                   transport: httpx.AsyncBaseTransport | None = None) -> object:
    """The one outbound call. Only the key header is sent."""
    async with httpx.AsyncClient(timeout=AA_TIMEOUT_S, transport=transport) as c:
        r = await c.get(AA_URL, headers={"x-api-key": api_key})
    if r.status_code != 200:
        raise HTTPException(502, f"Artificial Analysis returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise HTTPException(502, "Artificial Analysis returned a non-JSON response")


def _fetch_disabled_reason() -> str | None:
    if os.environ.get("SP_EXTERNAL_FETCH", "").strip().lower() == "off":
        return "fetching is disabled (SP_EXTERNAL_FETCH=off)"
    if not os.environ.get("AA_API_KEY", "").strip():
        return "AA_API_KEY is not set"
    return None


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


async def _read_capped(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, "import file exceeds 10 MB")
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES:
            raise HTTPException(413, "import file exceeds 10 MB")
    return bytes(buf)


def _parse_or_422(payload: object) -> list[ext.AAModel]:
    try:
        return ext.parse_aa_payload(payload)
    except ext.AAPayloadError as e:
        raise HTTPException(422, str(e))


@external_metrics_router.post("/import")
async def import_snapshot(request: Request,
                          index_version: str | None = Query(None, max_length=16)) -> dict:
    import json
    raw = await _read_capped(request)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "body is not valid JSON")
    models = _parse_or_422(payload)
    async with request.app.state.db.session() as s:
        sid = await ext.store_snapshot(
            s, models, origin="import", retrieved_at=dt.datetime.now(dt.timezone.utc),
            index_version=index_version, api_tier=None)
    return {"snapshot_id": sid, "row_count": len(models)}


@external_metrics_router.post("/refresh")
async def refresh_snapshot(request: Request,
                           index_version: str | None = Query(None, max_length=16)) -> dict:
    reason = _fetch_disabled_reason()
    if reason:
        raise HTTPException(409, reason)
    api_key = os.environ["AA_API_KEY"].strip()
    transport = getattr(request.app.state, "aa_transport", None)
    # The rate-limit check, the fetch and the insert share one session (and, on
    # Postgres, one advisory transaction lock; SQLite serialises sessions), so a
    # concurrent second refresh waits, then sees the new snapshot and gets 429.
    async with request.app.state.db.session() as s:
        if s.get_bind().dialect.name == "postgresql":
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        newest = await ext.latest_snapshot(s)
        now = dt.datetime.now(dt.timezone.utc)
        if newest is not None:
            wait = REFRESH_MIN_INTERVAL_S - (now - _aware(newest.retrieved_at)).total_seconds()
            if wait > 0:
                raise HTTPException(429, detail={
                    "message": f"refresh allowed once per {REFRESH_MIN_INTERVAL_S // 60} "
                               f"minutes; retry in {int(wait) + 1}s",
                    "retry_after_s": int(wait) + 1})
        try:
            payload = await fetch_aa(api_key, transport=transport)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Artificial Analysis unreachable: {type(e).__name__}")
        try:
            models = ext.parse_aa_payload(payload)
        except ext.AAPayloadError as e:
            raise HTTPException(502, f"Artificial Analysis returned an unexpected payload: {e}")
        sid = await ext.store_snapshot(s, models, origin="fetch", retrieved_at=now,
                                       index_version=index_version, api_tier=None)
    return {"snapshot_id": sid, "row_count": len(models)}


@external_metrics_router.get("/snapshots")
async def list_snapshots(request: Request) -> dict:
    async with request.app.state.db.session() as s:
        snaps = await ext.list_snapshots(s)
    reason = _fetch_disabled_reason()
    return {
        "snapshots": [{"id": x.id, "source": x.source, "origin": x.origin,
                       "retrieved_at": _aware(x.retrieved_at).isoformat(),
                       "index_version": x.index_version, "row_count": x.row_count}
                      for x in snaps],
        "fetch_enabled": reason is None, "fetch_disabled_reason": reason}


def _pool(request: Request) -> dict[str, str]:
    reg = getattr(request.app.state, "registry", None)
    return {c.name: c.provider for c in reg.effective_pool()} if reg else {}


@external_metrics_router.get("/map")
async def get_model_map(request: Request) -> dict:
    async with request.app.state.db.session() as s:
        snap = await ext.latest_snapshot(s)
        slugs = list((await ext.snapshot_models(s, snap.id)).keys()) if snap else []
        mapping = await ext.get_map(s)
    normed = [(sl, _norm(sl)) for sl in slugs]
    out = []
    for name, provider in sorted(_pool(request).items()):
        n = _norm(name)
        sugg = [sl for sl, ns in normed if n and ns and (n in ns or ns in n)][:3]
        out.append({"model": name, "placement": placement_for_provider(provider),
                    "ext_model_slug": mapping.get(name), "suggestions": sugg})
    return {"models": out, "snapshot_id": snap.id if snap else None}


class MapBody(BaseModel):
    model: str
    ext_model_slug: str | None = None


@external_metrics_router.put("/map")
async def put_model_map(body: MapBody, request: Request,
                        admin_id: str = Depends(require_admin)) -> dict:
    if body.model not in _pool(request):
        raise HTTPException(404, f"model {body.model!r} is not in the model pool")
    async with request.app.state.db.session() as s:
        if body.ext_model_slug is not None:
            snap = await ext.latest_snapshot(s)
            known = await ext.snapshot_models(s, snap.id) if snap else {}
            if body.ext_model_slug not in known:
                raise HTTPException(
                    422, f"{body.ext_model_slug!r} is not in the latest snapshot")
        await ext.set_map(s, body.model, body.ext_model_slug, by=str(admin_id)[:64])
    return {"model": body.model, "ext_model_slug": body.ext_model_slug}
