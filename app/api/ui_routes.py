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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api")

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
def list_requests(request: Request, limit: int = 100) -> list:
    history = request.app.state.history
    return history.list(limit=limit)


@router.get("/requests/{request_id}")
def get_request(request_id: str, request: Request) -> dict:
    history = request.app.state.history
    resp = history.get(request_id)
    if resp is None:
        raise HTTPException(404, f"request {request_id!r} not found")
    return resp


@router.get("/stats")
def get_stats(request: Request) -> dict:
    history = request.app.state.history
    return history.stats()


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
        from tests.traffic.generator import run_traffic
        try:
            summary = await run_traffic(base_url, n=n, rps=rps, seed=_TRAFFIC_SEED)
            state.traffic_last_summary = dataclasses.asdict(summary)
        finally:
            # Always clears, even if run_traffic raised, so the UI never
            # gets stuck showing "running" forever.
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
