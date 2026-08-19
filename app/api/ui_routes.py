"""Customer-facing `/api/*` read surface for the Phase-6 UI.

Unlike `/internal/*` (loopback-only agent-hop endpoint), these routes are
meant to be reachable by the browser dev server and are included in
OpenAPI. Task 1 added read-only list/detail/stats over the in-memory
`HistoryStore` on `app.state.history`. Task 2 adds `/api/stream`, a
Server-Sent-Events feed of the same redaction-safe events (backed by the
`DecisionBus` on `app.state.bus`) so the live flow visualizer can animate
requests as they complete instead of polling.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.apps import store
from app.apps.admin_auth import require_admin
from app.apps.langfuse_provision import load_app_langfuse
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
def list_requests(request: Request, limit: int = 100, app: str | None = None) -> list:
    history = request.app.state.history
    return history.list(limit=limit, app=app)


@router.get("/requests/{request_id}")
def get_request(request_id: str, request: Request) -> dict:
    history = request.app.state.history
    resp = history.get(request_id)
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


@router.post("/requests/{request_id}/feedback")
async def admin_feedback(request_id: str, body: AdminFeedbackIn, request: Request) -> dict:
    """Admin-console feedback intake (F1 Phase D task 4): score a request the
    admin is already looking at in the console, by its stored trace_id —
    admin-gating is inherited from this router's `Depends(require_admin)`.
    Resolves the app's own Langfuse project via the request's tenant
    (`HistoryStore.get_tenant`, added alongside `_full`) and posts through the
    same `post_score` helper `/v1/feedback` uses, so fail-closed behaviour
    (unknown tenant / app has no project -> `app_langfuse is None` -> dropped,
    `{"stored": false}`, HTTP 200) is identical."""
    history = request.app.state.history
    resp = history.get(request_id)
    if resp is None:
        raise HTTPException(404, f"request {request_id!r} not found")
    trace_id = resp.get("trace_id")
    if not trace_id:
        # Telemetry was off (or export failed) for this request — nothing to
        # attach a score to. Not an error: the admin just can't score this one.
        return {"stored": False, "reason": "no trace"}
    if body.score_name not in SCORE_NAMES:
        raise HTTPException(422, f"unknown score_name {body.score_name!r}")
    tenant = history.get_tenant(request_id)
    app_langfuse = None
    if tenant is not None:
        async with request.app.state.db.session() as session:
            app = await store.get_application(session, tenant)
        app_langfuse = load_app_langfuse(app) if app is not None else None
    await ensure_score_config(app_langfuse, body.score_name)  # best-effort, never raises
    stored = await post_score(app_langfuse, trace_id, body.score_name,
                              body.value, body.comment)
    if body.corrected_output:
        await ensure_score_config(app_langfuse, "corrected_output")
        await post_score(app_langfuse, trace_id, "corrected_output", 1,
                         body.corrected_output)
    return {"stored": stored}


@router.get("/stats")
def get_stats(request: Request) -> dict:
    history = request.app.state.history
    return history.stats()


@router.get("/fleet")
def fleet(request: Request) -> list:
    return request.app.state.history.fleet()


@router.get("/controls")
def get_controls(request: Request) -> dict:
    """Current runtime-mutable controls state. Only `headroom_enabled` is
    mutable this phase (see `_DEFERRED_CONTROLS`). Reflects
    `app.state.co_cfg` — the single mutable source shared by both the
    direct pipeline path (via `Pipeline`'s `co_cfg_provider` callback) and
    the internal per-hop endpoint (which reads `app.state.co_cfg` directly)."""
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
    server over real HTTP (like the agent runtime's internal hops) so the
    live dashboard fills exactly as it would from an operator running the
    CLI. Refuses (409) if a run is already in progress."""
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

    Events carry only what `HistoryStore`/`routes.py` already redacted
    before publishing — this generator never touches raw request/response
    bodies itself.
    """
    bus = request.app.state.bus
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
