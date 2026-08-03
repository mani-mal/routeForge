"""Phase 6a Task 2: DecisionBus fan-out + `/api/stream` SSE endpoint.

The suite has no pytest-asyncio/anyio plugin installed (see pyproject.toml
dev deps) — async behavior elsewhere (tests/test_gateway.py,
tests/test_agent_runtime.py) is driven with `asyncio.run(...)` inside an
ordinary sync `def test_...`, not `@pytest.mark.asyncio`. Mirror that here.
"""

from __future__ import annotations

import asyncio
import http.client

from app.api.bus import DecisionBus
from tests.conftest import admin_cookie
import pytest


def test_bus_fanout_and_drop_oldest():
    async def _run():
        bus = DecisionBus()
        q1, q2 = bus.subscribe(), bus.subscribe()
        await bus.publish({"type": "request", "summary": {"id": "a"}})
        assert (await asyncio.wait_for(q1.get(), 1))["summary"]["id"] == "a"
        assert (await asyncio.wait_for(q2.get(), 1))["summary"]["id"] == "a"

        bus.unsubscribe(q2)
        await bus.publish({"type": "step", "request_id": "a"})
        assert q1.qsize() == 1  # q2 unsubscribed, no error, still fans out to q1

    asyncio.run(_run())


def test_bus_drop_oldest_on_full_queue():
    async def _run():
        bus = DecisionBus(maxsize=2)
        q = bus.subscribe()
        await bus.publish({"n": 1})
        await bus.publish({"n": 2})
        # queue is now full (maxsize=2); this publish must drop the OLDEST
        # entry (n=1), not block and not raise, so a slow subscriber can
        # never stall serving.
        await bus.publish({"n": 3})
        assert q.qsize() == 2
        assert q.get_nowait()["n"] == 2
        assert q.get_nowait()["n"] == 3

    asyncio.run(_run())


def test_stream_endpoint_returns_event_stream(live_internal_server):
    """`GET /api/stream` returns a real SSE stream.

    NOTE on why this doesn't use `TestClient.stream(...)` (as the plan
    suggested): httpx's `ASGITransport` — which backs both
    `TestClient.stream()` and an in-process `httpx.AsyncClient` — fully
    drains the ASGI app callable (`await self.app(scope, receive, send)`)
    before it constructs *any* `Response`, including headers/status. Our
    `/api/stream` generator is intentionally infinite (heartbeats every
    ~15s until the client disconnects), so that call never returns and
    even `r.status_code` would hang — confirmed by direct experiment with
    both `TestClient` and a raw `httpx.AsyncClient(transport=ASGITransport(...))`.
    Real incremental streaming needs a real socket, which is exactly what
    the `live_internal_server` fixture (tests/conftest.py) already
    provides for the agent runtime's live HTTP self-calls. Reuse it here:
    connect over a real TCP socket, read the status + one preamble line,
    then close — the close triggers the server's disconnect path
    (`CancelledError` -> `finally: bus.unsubscribe(queue)`).
    """
    cookie = admin_cookie(live_internal_server.port)  # /api/* is admin-gated
    conn = http.client.HTTPConnection("127.0.0.1", live_internal_server.port, timeout=5)
    try:
        conn.request("GET", "/api/stream", headers={"Cookie": cookie})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("content-type", "").startswith("text/event-stream")
        line = resp.readline()  # dechunks (the endpoint is chunked-transfer)
        assert b"connected" in line
    finally:
        conn.close()


def test_chat_completion_publishes_to_stream(live_internal_server):
    """End-to-end: a POST to /v1/chat/completions must fan a `request` event
    (and per-step events) out to a connected /api/stream subscriber. This is
    the whole point of the stream; the publish block's broad except would
    otherwise let a regression go dark with every unit test still green.
    """
    import json

    port = live_internal_server.port
    cookie = admin_cookie(port)  # /api/* is admin-gated
    stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        stream.request("GET", "/api/stream", headers={"Cookie": cookie})
        sresp = stream.getresponse()
        assert sresp.status == 200
        assert b"connected" in sresp.readline()  # preamble; subscriber now live

        # Fire a request on a separate connection; publish happens before it returns.
        post = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            body = json.dumps({"messages": [{"role": "user", "content": "capital of France?"}]})
            post.request("POST", "/v1/chat/completions", body=body,
                         headers={"Authorization": "Bearer demo-key",
                                  "Content-Type": "application/json"})
            assert post.getresponse().status == 200
        finally:
            post.close()

        # Drain the SSE stream until we see the request event (bounded).
        saw_request = False
        for _ in range(50):
            line = sresp.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                evt = json.loads(line[len(b"data:"):].strip())
                if evt.get("type") == "request":
                    assert evt["summary"]["model"]  # redaction-safe summary present
                    saw_request = True
                    break
        assert saw_request, "no 'request' event reached the /api/stream subscriber"
    finally:
        stream.close()
