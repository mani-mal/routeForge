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
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api")

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
