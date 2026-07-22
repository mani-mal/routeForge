"""Phase 7 Task 4: `/api/traffic` control endpoint (demo live-fire).

Uses `live_internal_server` (a real socket) because the endpoint launches an
in-process task that SELF-CALLS the running server over real HTTP (like the
agent runtime's internal endpoint) — a bare TestClient doesn't accept real
socket connections, so this can't be exercised with the plain `client`
fixture.

409 note (see plan Task 4 step 1): a live double-POST race is inherently
timing-sensitive. We fire the second POST immediately after the first 200
response comes back — `traffic_running` is set to True synchronously before
the first request handler returns, so as long as the burst (10 requests at
rps=50, i.e. ~180ms of pacing) hasn't finished in that gap, the second POST
reliably observes `running=True` and gets 409. This is the "best-effort
immediate second POST" option the plan calls out, not a direct app.state
reach-in, since it also proves the guard actually rejects over-the-wire.
"""

from __future__ import annotations

import time

import httpx


def test_traffic_endpoint_starts_runs_and_fills_stats(live_internal_server):
    base = f"http://127.0.0.1:{live_internal_server.port}"
    with httpx.Client(base_url=base, timeout=10.0) as client:
        resp = client.post("/api/traffic", json={"requests": 10, "rps": 50})
        assert resp.status_code == 200
        assert resp.json() == {"status": "started", "requests": 10}

        # Best-effort 409: fired immediately, while the burst is still running.
        second = client.post("/api/traffic", json={"requests": 10, "rps": 50})
        assert second.status_code == 409

        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            status = client.get("/api/traffic").json()
            if not status["running"]:
                break
            time.sleep(0.25)

        assert status is not None
        assert status["running"] is False
        assert status["last_summary"] is not None
        assert status["last_summary"]["count"] == 10

        stats = client.get("/api/stats").json()
        assert stats["count"] >= 10


def test_traffic_status_before_any_run(live_internal_server):
    base = f"http://127.0.0.1:{live_internal_server.port}"
    with httpx.Client(base_url=base, timeout=10.0) as client:
        status = client.get("/api/traffic").json()
        assert status == {"running": False, "last_summary": None}
