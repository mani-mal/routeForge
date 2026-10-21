"""Contract: when a client gives up on a request that waits for a complete
answer, the backend work stops. It is not left running to completion and
metered as a served request."""
import asyncio

import pytest

from app.api import routes


def test_await_returns_result_when_client_stays():
    async def go():
        task = asyncio.create_task(asyncio.sleep(0.05, result={"ok": 1}))

        async def connected():
            return False

        return await routes._await_unless_disconnected(task, connected, 0.01)

    assert asyncio.run(go()) == {"ok": 1}


def test_await_cancels_the_work_when_client_disconnects():
    cancelled = {}

    async def go():
        async def work():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled["yes"] = True
                raise

        task = asyncio.create_task(work())
        calls = {"n": 0}

        async def disconnected():
            calls["n"] += 1
            return calls["n"] >= 2

        with pytest.raises(routes.ClientDisconnected):
            await routes._await_unless_disconnected(task, disconnected, 0.01)
        await asyncio.sleep(0)
        return task.cancelled()

    assert asyncio.run(go()) is True
    assert cancelled.get("yes") is True


def test_wait_or_cancel_on_shutdown_cancels_task_when_handler_itself_is_cancelled():
    """M1 (final-review fix wave): if the request handler coroutine itself is
    cancelled while waiting on the pipeline task (e.g. server shutdown, not a
    client disconnect), the pipeline task must be cancelled too -- otherwise
    it keeps running, orphaned, with no caller left to await or record it.
    Shared by both `_await_unless_disconnected` and the keepalive-vs-direct
    wait in `chat_completions`."""
    async def go():
        async def work():
            await asyncio.sleep(10)

        task = asyncio.create_task(work())
        await asyncio.sleep(0)  # let it actually start

        outer = asyncio.create_task(routes._wait_or_cancel_on_shutdown(task, 5.0))
        await asyncio.sleep(0)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)  # let the cancellation land in `task`
        return task.cancelled()

    assert asyncio.run(go()) is True


def test_await_reraises_pipeline_errors():
    async def go():
        async def boom():
            raise ValueError("x")

        async def connected():
            return False

        return await routes._await_unless_disconnected(asyncio.create_task(boom()),
                                                        connected, 0.01)

    with pytest.raises(ValueError):
        asyncio.run(go())


def test_keepalive_stream_closed_by_client_cancels_work_and_records_cancelled(monkeypatch):
    """SUBSTITUTION (see task-2-report.md): this Starlette/httpx TestClient
    combination runs the whole ASGI call to completion inside
    `portal.call(...)` before `.stream()` even returns a response object --
    there is no real backpressure, so closing the `with c.stream(...)` block
    early never interrupts a still-running generator; it only reads back an
    already-fully-produced body. Confirmed experimentally: a `never_finishes`
    pipeline.run only completes after its real 30s sleep elapses (blocking
    the whole test for that long), never via early client disconnect.

    So this drives `_keepalive_stream` directly: read one keepalive frame,
    then `aclose()` the generator -- the same GeneratorExit Starlette
    delivers into a StreamingResponse's body_iterator on a real client
    disconnect -- and assert the still-running backend task is cancelled and
    the work is metered `cancelled` (via `_record_cancelled`, monkeypatched
    with a recorder here so this test doesn't need a full DB-backed app)."""
    from app.api import routes
    from app.apps.policy import AppPolicyView
    from app.apps.privacy import AppPrivacyProfile
    from app.config.loader import load_settings
    from app.models import ChatRequest, Message

    recorded = []

    async def fake_record_cancelled(request, *, tenant, request_id, started, **_kw):
        recorded.append((tenant, request_id))

    monkeypatch.setattr(routes, "_record_cancelled", fake_record_cancelled)

    async def go():
        work_cancelled = {}

        async def work():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                work_cancelled["yes"] = True
                raise

        task = asyncio.create_task(work())
        await asyncio.sleep(0)  # let `work` actually start (reach its `await`)
                                 # before anything cancels it -- matches a real
                                 # backend call that is genuinely in flight
        body = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
        gen = routes._keepalive_stream(
            None, task=task, keepalive_s=0.01, request_id="req-test",
            body=body, tenant="acme", app_policy=AppPolicyView(),
            app_langfuse=None, profile=AppPrivacyProfile(recognizers=(), rules=()),
            session_id=None, s=load_settings(), started=0.0)
        first = await gen.__anext__()
        assert first.startswith(": keepalive")
        await gen.aclose()
        await asyncio.sleep(0)  # let the cancellation land in `work`
        return task.cancelled(), work_cancelled.get("yes")

    cancelled, work_was_cancelled = asyncio.run(go())
    assert cancelled is True
    assert work_was_cancelled is True
    assert recorded == [("acme", "req-test")]


def test_keepalive_stream_polls_is_disconnected_and_records_cancelled_once(monkeypatch):
    """Primary disconnect detection (fix round 1, Important finding):
    `_keepalive_stream` cannot rely solely on Starlette cancelling the
    generator. Starlette only does that via a concurrent disconnect-listener
    task on ASGI spec_version < 2.4, or by letting `send()` raise OSError on
    spec_version >= 2.4. uvicorn 0.51.0 advertises 2.3 today (the listener
    path), but uvicorn's h11 `send()` silently returns instead of raising
    once the connection is closed -- so a future uvicorn advertising >= 2.4
    would leave this generator with no cancellation signal at all, streaming
    into the void until the task finished and got recorded `ok`.

    This drives the generator with a stub `request` whose `is_disconnected()`
    flips from False to True, and asserts the poll path alone catches it:
    the backend task is cancelled, `_record_cancelled` runs exactly once, no
    further frames (keepalive, answer, or error) are yielded once the
    disconnect is detected, and a subsequent `aclose()` on the now-exhausted
    generator does not record a second time."""
    from app.api import routes
    from app.apps.policy import AppPolicyView
    from app.apps.privacy import AppPrivacyProfile
    from app.config.loader import load_settings
    from app.models import ChatRequest, Message

    recorded = []

    async def fake_record_cancelled(request, *, tenant, request_id, started, **_kw):
        recorded.append((tenant, request_id))

    monkeypatch.setattr(routes, "_record_cancelled", fake_record_cancelled)

    class StubRequest:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls >= 2  # connected on the first poll, gone after

    async def go():
        work_cancelled = {}

        async def work():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                work_cancelled["yes"] = True
                raise

        task = asyncio.create_task(work())
        await asyncio.sleep(0)  # let `work` actually start before anything
                                 # cancels it -- matches a real in-flight call
        body = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
        stub = StubRequest()
        gen = routes._keepalive_stream(
            stub, task=task, keepalive_s=0.01, request_id="req-poll",
            body=body, tenant="acme", app_policy=AppPolicyView(),
            app_langfuse=None, profile=AppPrivacyProfile(recognizers=(), rules=()),
            session_id=None, s=load_settings(), started=0.0)

        frames = []
        async for frame in gen:
            frames.append(frame)
        # The generator already returned (StopAsyncIteration) -- closing an
        # already-exhausted generator must not re-run the finally block.
        await gen.aclose()
        return frames, task.cancelled(), work_cancelled.get("yes")

    frames, cancelled, work_was_cancelled = asyncio.run(go())
    assert cancelled is True
    assert work_was_cancelled is True
    # Only keepalives before the disconnect is caught -- no answer, no error
    # frame follows it.
    assert frames and all(f.startswith(": keepalive") for f in frames)
    assert recorded == [("acme", "req-poll")]


def test_keepalive_stream_real_asgi_disconnect_still_records_cancelled(monkeypatch):
    """I1 (final-review fix wave): in production (uvicorn ASGI spec 2.3,
    Starlette 1.3.1) `StreamingResponse` runs `listen_for_disconnect` in an
    anyio task group. On a real ASGI disconnect that task group's cancel
    scope cancels the body generator AND KEEPS RE-CANCELLING it, so the first
    `await` inside `_keepalive_stream`'s `finally` (the `asyncio.gather(task,
    ...)` that retrieves the already-cancelled backend task) raises
    CancelledError again and `_record_cancelled` never runs -- the disconnect
    is silently unmetered.

    Unlike the two tests above (which drive the generator directly via
    `aclose()`/manual iteration -- a GeneratorExit, not the anyio cancel-scope
    re-cancellation Starlette's real disconnect listener produces), this one
    drives the REAL `StreamingResponse.__call__(scope, receive, send)` with a
    `receive` that reports `http.disconnect` shortly after the first
    keepalive frame, so it actually exercises Starlette's own cancellation
    path. Confirmed to fail before the `anyio.CancelScope(shield=True)` fix:
    the task never gets cancelled and `_record_cancelled` never runs."""
    from app.api import routes
    from app.api.streaming import sse_response
    from app.apps.policy import AppPolicyView
    from app.apps.privacy import AppPrivacyProfile
    from app.config.loader import load_settings
    from app.models import ChatRequest, Message

    recorded = []

    async def fake_record_cancelled(request, *, tenant, request_id, started, **_kw):
        recorded.append((tenant, request_id))

    monkeypatch.setattr(routes, "_record_cancelled", fake_record_cancelled)

    class StubRequest:
        """Never trips the route's own is_disconnected() poll -- this test
        isolates the REAL ASGI/Starlette disconnect path, not the polling
        path the two tests above already cover."""

        async def is_disconnected(self):
            return False

    async def go():
        work_cancelled = {}

        async def work():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                work_cancelled["yes"] = True
                raise

        task = asyncio.create_task(work())
        await asyncio.sleep(0)  # let `work` actually start before anything
                                 # cancels it -- matches a real in-flight call
        body = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
        gen = routes._keepalive_stream(
            StubRequest(), task=task, keepalive_s=0.02, request_id="req-real-disc",
            body=body, tenant="acme", app_policy=AppPolicyView(),
            app_langfuse=None, profile=AppPrivacyProfile(recognizers=(), rules=()),
            session_id=None, s=load_settings(), started=0.0)
        resp = sse_response(gen)

        async def receive():
            await asyncio.sleep(0.1)  # after the first keepalive is on the wire
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        await resp(scope, receive, send)
        await asyncio.sleep(0.05)  # let any shielded cleanup settle
        return task.cancelled(), work_cancelled.get("yes")

    cancelled, work_was_cancelled = asyncio.run(go())
    assert cancelled is True
    assert work_was_cancelled is True
    assert recorded == [("acme", "req-real-disc")]
