"""Customer-facing `/api/*` read surface for the Phase-6 UI.

These routes are meant to be reachable by the browser dev server and are
included in OpenAPI. Task 1 added read-only list/detail/stats, originally over the
in-memory `HistoryStore`; B3 moved that backing store to the durable, shared
`request_snapshots` table (`app.state.db`, see `app/apps/store.py`) so every
API replica's console reads see every replica's writes. Task 2 adds
`/api/stream`, a Server-Sent-Events feed of the same redaction-safe events
(backed by the `DecisionBus` on `app.state.bus`, fanned out cross-replica by
`app.state.bridge`, see `app/api/pg_bridge.py`) so the live flow visualizer
can animate requests as they complete instead of polling.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.history import RequestSummary, stats_over
from app.apps import store
from app.apps.admin_auth import require_admin
from app.apps.langfuse_provision import load_app_langfuse
from app.apps.models import RequestSnapshot
from app.apps.outcomes import feedback_values
from app.obs.scores import SCORE_NAMES, ensure_score_config, post_score

# The whole /api/* console surface is admin-gated: it exposes cross-tenant
# metadata (per-app spend, PII/entity-type counts, request traces). The browser
# console calls these with credentials:"same-origin" so the admin session cookie
# rides along — including the SSE /api/stream (EventSource sends cookies too).
# The serving path (/v1/*) uses per-app bearer tokens and is unaffected.
router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])

# Controls deferred for this phase (Task 3) — documented in the GET response
# so the UI can render them as disabled/coming-soon rather than silently
# missing. cost_weight: the router is complexity/risk-tiered, not
# cost-tiered, so a cost slider wouldn't honestly shift tiers.
# provider_toggles: per-request `rejected[]` already greys out ineligible
# providers in the visualizer; a separate runtime toggle is redundant for
# the demo.
_DEFERRED_CONTROLS = [
    "cost_weight (router is complexity/risk-tiered, not cost-tiered)",
    "provider_toggles (per-request rejected[] already greys exclusions)",
]


class ControlsUpdate(BaseModel):
    headroom_enabled: bool


def _controls_state(request: Request) -> dict:
    co_cfg = request.app.state.co_cfg
    return {
        "headroom_enabled": co_cfg.enabled,
        "min_tokens": co_cfg.min_tokens,
        "deferred_controls": _DEFERRED_CONTROLS,
    }

# How long to wait for the next bus event before emitting an SSE comment
# heartbeat. Keeps intermediary proxies / browsers from treating the
# connection as idle and closing it during quiet periods.
_HEARTBEAT_SECONDS = 15


@router.get("/requests")
async def list_requests(request: Request, limit: int = 100, app: str | None = None) -> list:
    async with request.app.state.db.session() as s:
        return await store.list_snapshot_summaries(s, application_id=app, limit=limit)


@router.get("/requests/{request_id}")
async def get_request(request_id: str, request: Request) -> dict:
    async with request.app.state.db.session() as s:
        resp = await store.get_snapshot_full(s, request_id)
    if resp is None:
        raise HTTPException(404, f"request {request_id!r} not found")
    return resp


class AdminFeedbackIn(BaseModel):
    """Body for POST /api/requests/{request_id}/feedback. Unlike the
    app-token-authed `/v1/feedback` (`app/api/routes.py`), no `trace_id` is
    accepted here — it comes from the stored request the admin is looking
    at, never from client-supplied input."""
    score_name: str
    value: float | str
    comment: str | None = None
    corrected_output: str | None = None


async def _find_outcome_safe(request: Request, *, tenant: str, request_id: str):
    """`store.find_outcome`, best-effort like every other record on the
    serving path: a DB error here must never break feedback intake or block
    the Langfuse post below -- log and return None, same as "no matching
    outcome" (P2b-2 Task 5 fix round 1)."""
    try:
        async with request.app.state.db.session() as session:
            return await store.find_outcome(
                session, application_id=tenant, request_id=request_id, trace_id=None)
    except Exception:
        logging.getLogger("serving.obs").warning(
            "outcome lookup failed for admin feedback request=%s", request_id, exc_info=True)
        return None


async def _write_feedback_safe(request: Request, *, request_id: str, application_id: str,
                                score_name: str, value_num: float | None,
                                value_cat: str | None, source: str) -> bool:
    """`store.write_feedback` + commit, best-effort: a DB error here must
    never change the HTTP status, `stored`, or block a Langfuse post that
    hasn't happened yet -- log and return False (P2b-2 Task 5 fix round 1)."""
    try:
        async with request.app.state.db.session() as session:
            await store.write_feedback(
                session, request_id=request_id, application_id=application_id,
                score_name=score_name, value_num=value_num, value_cat=value_cat,
                source=source)
            await session.commit()
        return True
    except Exception:
        logging.getLogger("serving.obs").warning(
            "local feedback write failed request=%s score=%s", request_id, score_name,
            exc_info=True)
        return False


@router.post("/requests/{request_id}/feedback")
async def admin_feedback(request_id: str, body: AdminFeedbackIn, request: Request) -> dict:
    """Admin-console feedback intake (F1 Phase D task 4; local storage added
    P2b-2 Task 5): score a request the admin is already looking at in the
    console, by its stored trace_id — admin-gating is inherited from this
    router's `Depends(require_admin)`. Resolves the app's own Langfuse
    project via the request's tenant (the snapshot row's `application_id`,
    B3) and posts through the same `post_score` helper `/v1/feedback` uses,
    so fail-closed behaviour (unknown tenant / app has no project ->
    `app_langfuse is None` -> dropped, `stored: false`, HTTP 200) is
    identical.

    Local (`recorded`): looks the request up in `routing_outcomes` by
    `request_id` scoped to the snapshot's own tenant and, when found, writes
    an `outcome_feedback` row with `source="admin"` — including when there
    is no trace_id to post to Langfuse at all (`comment`/`corrected_output`
    TEXT is still never stored locally; see `/v1/feedback`)."""
    # Python's json accepts the non-standard NaN/Infinity literals; a
    # non-finite score must never reach Langfuse or outcome_feedback.
    if isinstance(body.value, float) and not math.isfinite(body.value):
        raise HTTPException(422, "value must be a finite number")
    async with request.app.state.db.session() as session:
        snap = await session.get(RequestSnapshot, request_id)
    if snap is None:
        raise HTTPException(404, f"request {request_id!r} not found")
    if body.score_name not in SCORE_NAMES:
        raise HTTPException(422, f"unknown score_name {body.score_name!r}")
    resp = snap.full_json
    trace_id = resp.get("trace_id")
    tenant = snap.application_id

    outcome = None
    if tenant is not None and request.app.state.settings.serving.outcomes.enabled:
        outcome = await _find_outcome_safe(request, tenant=tenant, request_id=request_id)

    recorded = False
    if outcome is not None:
        parsed = feedback_values(body.score_name, body.value)
        if parsed is not None:
            value_num, value_cat = parsed
            recorded = await _write_feedback_safe(
                request, request_id=outcome.request_id, application_id=tenant,
                score_name=body.score_name, value_num=value_num, value_cat=value_cat,
                source="admin")
        if body.corrected_output:
            await _write_feedback_safe(
                request, request_id=outcome.request_id, application_id=tenant,
                score_name="corrected_output", value_num=1.0, value_cat=None,
                source="admin")

    if not trace_id:
        # Telemetry was off (or export failed) for this request — nothing to
        # attach a score to in Langfuse. Not an error: the admin just can't
        # score this one there (the local row above still recorded).
        return {"stored": False, "reason": "no trace", "recorded": recorded}

    tenant_app = None
    if tenant is not None:
        async with request.app.state.db.session() as session:
            tenant_app = await store.get_application(session, tenant)
    app_langfuse = load_app_langfuse(tenant_app) if tenant_app is not None else None
    await ensure_score_config(app_langfuse, body.score_name)  # best-effort, never raises
    stored = await post_score(app_langfuse, trace_id, body.score_name,
                              body.value, body.comment)
    if body.corrected_output:
        await ensure_score_config(app_langfuse, "corrected_output")
        await post_score(app_langfuse, trace_id, "corrected_output", 1,
                         body.corrected_output)
    return {"stored": stored, "recorded": recorded}


@router.get("/stats")
async def get_stats(request: Request) -> dict:
    async with request.app.state.db.session() as s:
        rows = await store.all_snapshot_summaries(s, limit=store.SNAPSHOT_CAPACITY)
    return stats_over([RequestSummary(**r) for r in rows])


@router.get("/fleet")
async def fleet(request: Request, hours: int | None = Query(None, ge=1)) -> list:
    """Overview rows from the durable usage table (B76), over the last `hours`
    (omitted = all time). Not the snapshot ring, which is capped at 500."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
             if hours is not None else None)
    async with request.app.state.db.session() as s:
        return await store.fleet_from_usage(s, since=since)


@router.get("/controls")
def get_controls(request: Request) -> dict:
    """Current runtime-mutable controls state. Only `headroom_enabled` is
    mutable this phase (see `_DEFERRED_CONTROLS`). Reflects
    `app.state.co_cfg` — the single mutable source `Pipeline` reads via its
    `co_cfg_provider` callback, so a toggle here takes effect on the next
    request without a restart."""
    return _controls_state(request)


@router.post("/controls")
def set_controls(update: ControlsUpdate, request: Request) -> dict:
    """Toggle Headroom on/off at runtime. Mutates `app.state.co_cfg` in
    place (via `model_copy`) so the NEXT request on either path picks up
    the new value — no restart required."""
    request.app.state.co_cfg = request.app.state.co_cfg.model_copy(
        update={"enabled": update.headroom_enabled})
    return _controls_state(request)


class TrafficStart(BaseModel):
    requests: int = 100
    rps: int = 20


# Fixed seed for the live "Run burst" demo control: the same button always
# fires the same reproducible persona stream (Global Constraints: seeded +
# reproducible), matching the CLI generator's own default behavior.
_TRAFFIC_SEED = 42


@router.post("/traffic")
async def start_traffic(body: TrafficStart, request: Request) -> dict:
    """Launch the traffic-generator burst in-process, self-calling this same
    server over real HTTP so the live dashboard fills exactly as it would
    from an operator running the CLI. Refuses (409) if a run is already in
    progress."""
    state = request.app.state
    if getattr(state, "traffic_running", False):
        raise HTTPException(409, "a traffic run is already in progress")

    state.traffic_running = True
    base_url = f"http://127.0.0.1:{state.self_port}"
    n = body.requests
    rps = body.rps

    async def _run() -> None:
        # The traffic generator lives under tests/ (traffic-and-tests skill)
        # — it's test infrastructure reused here to drive the live demo
        # burst. Import lazily, inside the task, so app/ stays import-time
        # clean of tests/ (a layering oddity we accept rather than moving
        # the generator out of tests/).
        try:
            # Import INSIDE the try: if tests/ isn't on the path (e.g. a wheel
            # that ships only app/ — see Phase 8 packaging note), the import
            # itself raises, and the finally must still clear the flag or the
            # UI is wedged at "running" forever with every retry → 409.
            from tests.traffic.generator import run_traffic
            registry = getattr(state, "registry", None)
            if registry is not None:
                await registry.ensure_loaded(state.db)
            summary = await run_traffic(
                base_url, n=n, rps=rps, seed=_TRAFFIC_SEED, registry=registry)
            state.traffic_last_summary = dataclasses.asdict(summary)
        except Exception:
            logging.getLogger("serving.traffic").exception("traffic burst failed")
        finally:
            # Always clears, even if the import or run_traffic raised, so the
            # UI never gets stuck showing "running" forever.
            state.traffic_running = False

    state.traffic_task = asyncio.create_task(_run())
    return {"status": "started", "requests": n}


@router.get("/traffic")
def traffic_status(request: Request) -> dict:
    state = request.app.state
    return {
        "running": getattr(state, "traffic_running", False),
        "last_summary": getattr(state, "traffic_last_summary", None),
    }


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE feed of decision events for the live flow visualizer.

    No replay-on-connect (documented deferral, see plan Task 2): a fresh
    connection only sees events published after it subscribes. The
    `/api/requests` list endpoint covers backfill for anything missed.

    Events carry only what `routes.py`/`app/apps/store.py`'s snapshot write
    already redacted before publishing — this generator never touches raw
    request/response bodies itself. B3: `bridge.ensure_listening()` makes
    sure this pod is also LISTENing for other replicas' NOTIFYs (inert on
    sqlite / a fresh `active` check on the first call per pod) so a request
    served by another pod still reaches a subscriber connected here.
    """
    bus = request.app.state.bus
    try:
        await request.app.state.bridge.ensure_listening()
    except Exception:
        logging.getLogger("serving.obs").exception("stream bridge listen failed (local-only)")
    queue = bus.subscribe()

    async def event_source():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(evt)}\n\n"
        except asyncio.CancelledError:
            # Client disconnected — let the finally below unsubscribe and
            # exit quietly instead of propagating.
            pass
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(event_source(), media_type="text/event-stream")
