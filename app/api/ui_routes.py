"""Customer-facing `/api/*` read surface for the Phase-6 UI.

Unlike `/internal/*` (loopback-only agent-hop endpoint), these routes are
meant to be reachable by the browser dev server and are included in
OpenAPI. They are read-only in this task (Task 1): list/detail/stats over
the in-memory `HistoryStore` on `app.state.history`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api")


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
