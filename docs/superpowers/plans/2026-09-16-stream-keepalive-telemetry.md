# Stream Keepalive, Disconnect Cancel and Trace Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A long, non-token-streamed turn stays alive for an agent client. A client that gives up stops its backend work, instead of that work running to completion and being metered as `ok`. Langfuse shows each request's real latency, a generation with model, tokens and cost, and one session per agent conversation.

**Architecture:**
- The route runs the pipeline in a task.
- **Stream requests:** if the task finishes within a short grace period, the response is exactly as today, so fast policy rejections keep their HTTP status. Otherwise the route opens the SSE response immediately and sends keepalive comments until the answer is ready. A late failure is sent as an SSE error frame.
- **Disconnects:** a client disconnect cancels the pipeline task and records a `cancelled` usage row.
- **Traces:** the exporter gets real timestamps and marks the serving router hop as a generation.
- **Sessions:** when the client sends no `x-routeforge-session` header, a session id is derived from a hash of the conversation's opening turns.

**Tech Stack:** Python 3.12, FastAPI/Starlette `StreamingResponse`, asyncio, OpenTelemetry SDK → Langfuse OTLP, SQLAlchemy async, pytest.

**Spec:** This plan is the spec. It comes from the 2026-09-16 data audit of a live agent-client app (owner approved "ok" to the recommended fixes). The audit found:
1. **Retry loop.** 88 of 433 requests were retries starting exactly 120–122 s apart. The client's transport timeout (default 120 s) covers connect plus first response bytes. RouteForge sends no bytes until a tool turn finishes, which takes 5–10 min at ~19 tok/s.
2. **Wasted work on abandoned requests.** Abandoned requests kept generating on the backend and were recorded as `status=ok` usage rows, snapshots and Langfuse traces.
3. **Langfuse spans and sessions.** Every Langfuse span lasted 0 s. No observation carried model or token usage. `sessionId` was always empty because the client sends no session header.
4. **Unexplained 502.** One 502 left no cause in any log.

## Global Constraints

- **Contract tests.** Follow `.claude/skills/consumer-contract-tests/SKILL.md`. Every serving-path change gets a test that drives `/v1/chat/completions` over HTTP with a token.
  - Never name a specific client application in a symbol, test id, fixture, comment or config.
  - Use generic shapes: "agent tool turn", "slow backend".
- **Running tests.** `LITELLM_LOCAL_MODEL_COST_MAP=True` is required or pytest hangs. Command: `rtk proxy bash -c 'cd <worktree> && LITELLM_LOCAL_MODEL_COST_MAP=True timeout 580 /home/manimal/projects/routeForge/.venv/bin/python -m pytest -q -p no:cacheprovider <target> -rxXf > <file> 2>&1; echo exit=$?'`
  - Run in the FOREGROUND only, with Bash timeout 600000, and read the file afterwards.
  - The pytest summary line may be missing under rtk, so rely on the exit code and FAILED/ERROR lines.
- **Non-negotiable rules (CLAUDE.md).**
  - Fail closed.
  - Nothing external before the gate.
  - Telemetry is redacted on egress. Only `capture_io=full` I/O keys may skip redaction, and only to the app's own project.
  - `onprem_only` is absolute.
  - Step 9 verification cannot be waived for `onprem_only` / `require_verification`.
- **Keepalive scope.** Keepalive changes only *when bytes are sent*, never *what* is verified. The keepalive path serves exactly the same `pipeline.run` result that `verified_then_stream` serves today.
- **Real token streaming is unchanged.** The `stream_unverified` / `incremental_verify` real token-streaming paths keep their drain-on-disconnect accounting. Cancellation applies only to paths that await a complete `pipeline.run`.
- **Config.** One owner per concern: new server knobs live only in `config/serving.yaml` under `server:`.
  - Load each with `.get(key, default)` in `app/config/loader.py`, so an older mounted serving.yaml still boots.
  - Give each a schema default in `app/config/schema.py`.
- **Message prefixes.** Existing HTTP error prefixes are preserved: `no model is available for this request's policy`, `model request rejected`, `response failed verification`, `no backend could serve this request`, `policy evaluation unavailable`, `streaming is not supported`.
- **Commits.** End every commit message with the line `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Never commit `.superpowers/`, `*.db`, or `node_modules`.

---

### Task 1: Keepalive for streamed turns that wait for a complete answer

**Files:**
- Modify: `config/serving.yaml` (the `server:` block)
- Modify: `app/config/schema.py` (`ServingCfg`)
- Modify: `app/config/loader.py:145` area
- Modify: `app/api/streaming.py` (add `SSE_KEEPALIVE`, `sse_error_frame`)
- Modify: `app/api/routes.py` (`chat_completions`; new helpers `_error_status_detail`, `_keepalive_stream`)
- Test: `tests/test_stream_keepalive.py` (new)

**Interfaces:**
- Produces:
  - `ServingCfg.stream_keepalive_s: float = 5.0`
  - `app.api.streaming.SSE_KEEPALIVE: str == ": keepalive\n\n"`
  - `app.api.streaming.sse_error_frame(status: int, detail: str) -> str`
  - `app.api.routes._error_status_detail(exc: BaseException) -> tuple[int, str] | None`, which maps a pipeline exception to exactly the (status, detail) the HTTP path returns today, or None when the exception is not a known serving error.
  - `app.api.routes._keepalive_stream(...)`, the async generator used by the route.

- [ ] **Step 1: Write the failing tests** (`tests/test_stream_keepalive.py`)

```python
"""Contract: a streamed agent turn that has to wait for a complete, verified
answer keeps the connection alive. A client transport timeout that only
covers "first response bytes" must not fire while the model is still working,
and a failure after the stream opened still reaches the client as an error."""
import asyncio
import json

import pytest

from tests.onboarding.harness import conformance_client, onboard

_TURN = {"stream": True, "messages": [{"role": "user", "content": "summarise the repo"}],
         "tools": [{"type": "function", "function": {
             "name": "read_file", "description": "read a file",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]}


def _fast_keepalive(c, seconds=0.05):
    s = c.app.state.settings
    c.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"stream_keepalive_s": seconds})})


def _slow_run(monkeypatch, c, *, delay=0.3, result=None, exc=None):
    real = c.app.state.pipeline.run

    async def slow(*a, **k):
        await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return await real(*a, **k)

    monkeypatch.setattr(c.app.state.pipeline, "run", slow)


def _events(text):
    return [block for block in text.split("\n\n") if block]


def test_slow_streamed_turn_sends_keepalives_before_the_answer(monkeypatch):
    with conformance_client() as c:
        app = onboard(c, "ka-slow", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.3)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = "".join(r.iter_text())
    events = _events(body)
    assert events[0] == ": keepalive"
    data = [e for e in events if e.startswith("data: ")]
    assert data[-1] == "data: [DONE]"
    chunks = [json.loads(e[6:]) for e in data[:-1]]
    openai = pytest.importorskip("openai.types.chat")
    for ch in chunks:
        openai.ChatCompletionChunk.model_validate(ch)


def test_fast_streamed_turn_has_no_keepalive(monkeypatch):
    with conformance_client() as c:
        app = onboard(c, "ka-fast", stream_mode="verified_then_stream")
        _fast_keepalive(c, seconds=5.0)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            body = "".join(r.iter_text())
    assert ": keepalive" not in body
    assert body.rstrip().endswith("data: [DONE]")


def test_fast_policy_rejection_keeps_its_http_status():
    with conformance_client() as c:
        app = onboard(c, "ka-reject", stream_mode="verified_then_stream",
                      allowed_models=["no-such-model"])
        r = c.post("/v1/chat/completions", headers=app.headers, json=_TURN)
    assert r.status_code == 422
    assert "allowed_models" in r.json()["detail"]


def test_late_failure_after_stream_opened_is_an_sse_error_frame(monkeypatch):
    from app.errors import BackendUnavailable
    with conformance_client() as c:
        app = onboard(c, "ka-late-error", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.3,
                  exc=BackendUnavailable("backend failed on all reachable candidates"))
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
    data = [e for e in _events(body) if e.startswith("data: ")]
    err = json.loads(data[-2][6:])
    assert data[-1] == "data: [DONE]"
    assert err["error"]["code"] == 502
    assert err["error"]["message"].startswith("no backend could serve this request")


def test_openai_sdk_raises_on_the_late_error_frame(monkeypatch):
    openai = pytest.importorskip("openai")
    from app.errors import BackendUnavailable
    with conformance_client() as c:
        app = onboard(c, "ka-sdk-error", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.2, exc=BackendUnavailable("boom"))
        client = openai.OpenAI(base_url="http://testserver/v1", api_key="x",
                               http_client=c, max_retries=0)
        client._client = c  # route SDK traffic through the TestClient transport
        with pytest.raises(openai.APIError):
            for _ in client.chat.completions.create(
                    model="auto", stream=True, messages=_TURN["messages"],
                    extra_headers=app.headers):
                pass


def test_config_default_and_older_serving_yaml():
    from app.config.loader import load_settings
    assert load_settings().serving.stream_keepalive_s == 5.0
```

Two notes on these tests:
- **The SDK test (`test_openai_sdk_raises_on_the_late_error_frame`).** It depends on how the installed `openai` package accepts a custom transport. If it can't be wired through the TestClient in about 15 lines, replace it with an assertion on the frame shape the SDK reads: the SDK raises `APIError` when a streamed `data:` JSON object has an `"error"` key. Record the substitution in your report.
- **The older-serving.yaml test.** It must also prove that a serving.yaml with no `stream_keepalive_s` key still loads. Follow the existing pattern for other `.get()`-defaulted keys: `grep -n "semantic_cache\|default_policy_mode" tests/test_config_loader.py`.

- [ ] **Step 2: Run the tests and confirm they fail** (the keepalive frame is absent and there is no `stream_keepalive_s` attribute).

- [ ] **Step 3: Config.** In `config/serving.yaml`, under `server:` after `reject_stream_requests`, add:

```yaml
  # Streamed requests that must wait for a complete, verified answer (tool
  # turns, verified_then_stream, runtime-floored requests) open the SSE response
  # after this many seconds and send a keepalive comment at the same interval, so
  # a client whose transport timeout covers only the first response bytes does
  # not abort a long generation. Fast responses and fast rejections are
  # unaffected (they finish inside the first interval and keep their HTTP status).
  stream_keepalive_s: 5
```

In `app/config/schema.py`, add `stream_keepalive_s: float = 5.0` to `ServingCfg` directly after `reject_stream_requests: bool`.

In `app/config/loader.py`, directly after the `reject_stream_requests=...` line, add:

```python
        stream_keepalive_s=float(serving_raw["server"].get("stream_keepalive_s", 5.0)),
```

- [ ] **Step 4: Streaming helpers.** In `app/api/streaming.py`, add after `SSE_DONE`:

```python
# SSE comment line: ignored by every SSE/OpenAI client parser, but it is bytes
# on the wire, which keeps idle and first-byte transport timeouts from firing.
SSE_KEEPALIVE = ": keepalive\n\n"


def sse_error_frame(status: int, detail: str) -> str:
    """A failure after the SSE response already started (status 200 is on the
    wire). OpenAI SDK stream parsers raise APIError on a data frame carrying an
    `error` object; `code` mirrors the HTTP status the non-stream path returns."""
    return sse_frame({"error": {"message": detail, "type": "routeforge_error",
                                "code": status}})
```

- [ ] **Step 5: Route.** In `app/api/routes.py`:

(a) Extract today's exception → HTTP mapping into a helper used by both paths. Add it above `chat_completions`:

```python
def _error_status_detail(exc: BaseException) -> tuple[int, str] | None:
    """The (status, detail) the HTTP path returns for a serving exception. One
    mapping, used by the plain HTTP path and by the keepalive stream's error
    frame, so the two can never drift."""
    if isinstance(exc, PipelineRejected):
        message = str(exc)
        prefix = ("model request rejected: " if message.startswith("requested model")
                  else "response failed verification: ")
        return 422, f"{prefix}{message}"
    if isinstance(exc, NoEligibleCandidate):
        return 422, f"no model is available for this request's policy: {exc}"
    if isinstance(exc, AgentCapExceeded):
        return 500, "agent did not converge within its step limit"
    if isinstance(exc, BackendUnavailable):
        return 502, f"no backend could serve this request: {exc}"
    if isinstance(exc, PolicyGateError):
        return 503, "policy evaluation unavailable"
    return None
```

Rewrite the `except PipelineRejected / NoEligibleCandidate / AgentCapExceeded / BackendUnavailable / PolicyGateError` clauses in `chat_completions` to use it, keeping the `metrics.observe_request("-", "rejected")` calls:

```python
    except HTTPException:
        raise
    except Exception as e:
        mapped = _error_status_detail(e)
        if mapped is None:
            metrics.observe_request("-", "error")
            raise HTTPException(500, "internal error")
        metrics.observe_request("-", "rejected")
        raise HTTPException(mapped[0], mapped[1])
```

(b) Keepalive. Today the route does `resp = await request.app.state.pipeline.run(...)`, then `_finalize`, then either returns JSON or `sse_response(chunk_text(...))`. Change it for `stream_mode is not None` (verified_then_stream, or a StreamFallback from the real-streaming modes):

```python
        if stream_mode is not None:
            task = asyncio.create_task(request.app.state.pipeline.run(
                request_id, tenant, body, app_policy=app_policy, profile=profile,
                session_id=session_id))
            keepalive = s.serving.stream_keepalive_s
            done, _ = await asyncio.wait({task}, timeout=keepalive)
            if not done:
                return sse_response(_keepalive_stream(
                    request, task=task, keepalive_s=keepalive, request_id=request_id,
                    body=body, tenant=tenant, app_policy=app_policy,
                    app_langfuse=app_langfuse, profile=profile,
                    session_id=session_id, s=s))
            resp = task.result()   # raises -> the except clauses above map it
        else:
            resp = await request.app.state.pipeline.run(
                request_id, tenant, body, app_policy=app_policy, profile=profile,
                session_id=session_id)
```

Place this where the single `pipeline.run` call is today, inside the existing `try`. Add `import asyncio` to the imports.

Add the generator next to `_stream_unverified_response`:

```python
async def _keepalive_stream(request: Request, *, task: "asyncio.Task[dict]",
                            keepalive_s: float, request_id: str, body: ChatRequest,
                            tenant: str, app_policy: AppPolicyView,
                            app_langfuse: object | None, profile: AppPrivacyProfile,
                            session_id: str | None, s) -> AsyncIterator[str]:
    """SSE for a streamed request whose complete, verified answer is not ready
    within one keepalive interval. Sends a keepalive comment each interval until
    the pipeline task finishes, then the same frames `verified_then_stream`
    sends. A failure after the headers are out cannot change the HTTP status,
    so it goes out as one error frame (same status and detail as the HTTP path)
    followed by [DONE]."""
    while True:
        done, _ = await asyncio.wait({task}, timeout=keepalive_s)
        if done:
            break
        yield SSE_KEEPALIVE
    try:
        resp = task.result()
    except Exception as e:
        mapped = _error_status_detail(e)
        if mapped is None:
            logging.getLogger("serving.api").exception(
                "unhandled error for %s after stream opened", request_id)
            mapped = (500, "internal error")
        metrics.observe_request("-", "rejected" if mapped[0] != 500 else "error")
        yield sse_error_frame(*mapped)
        yield SSE_DONE
        return
    await _finalize(request, resp, body=body, tenant=tenant, app_policy=app_policy,
                    app_langfuse=app_langfuse, profile=profile,
                    session_id=session_id, s=s, request_id=request_id)
    message = resp["choices"][0]["message"]
    async for frame in chunk_text(request_id, resp["model"], message["content"] or "",
                                  tool_calls=message.get("tool_calls")):
        yield frame
```

Add `SSE_DONE`, `SSE_KEEPALIVE` and `sse_error_frame` to the `from app.api.streaming import (...)` block if they aren't there already.

- [ ] **Step 6: Run** `tests/test_stream_keepalive.py`, then `tests/test_streaming.py tests/test_stream_tool_calls.py tests/test_stream_unverified.py tests/test_incremental_verify_route.py tests/test_error_taxonomy.py tests/onboarding`, then the full suite. Expected: exit 0.

If an existing streaming test fails because it monkeypatches `pipeline.run` with a sync function, or expects `pipeline.run` to be awaited directly, adapt the test double. Do not adapt product behaviour. List every such change in your report.

- [ ] **Step 7: Commit** `feat(api): keep slow streamed turns alive; late failures become SSE error frames`

---

### Task 2: Cancel backend work when the client disconnects

**Files:**
- Modify: `app/api/routes.py` (`chat_completions`, `_keepalive_stream`; new helpers `_await_unless_disconnected`, `_record_cancelled`)
- Modify: `app/obs/metrics.py`, only if `observe_request` restricts status labels (check it first)
- Test: `tests/test_client_disconnect_cancel.py` (new)

**Interfaces:**
- Consumes: `_keepalive_stream`, `_error_status_detail`, `ServingCfg.stream_keepalive_s` (Task 1).
- Produces:
  - `app.api.routes.ClientDisconnected(Exception)`
  - `async def _await_unless_disconnected(task, is_disconnected, poll_s) -> dict`. It returns the task's result, re-raises the task's exception, or cancels the task and raises `ClientDisconnected` once `await is_disconnected()` is True.
  - `async def _record_cancelled(request, *, tenant: str, request_id: str, started: float) -> None`. It writes a usage row with `status="cancelled"`.

- [ ] **Step 1: Write the failing tests** (`tests/test_client_disconnect_cancel.py`)

```python
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
    from tests.onboarding.harness import conformance_client, onboard
    from app.apps import store

    rows = []
    real_record = store.record_usage

    async def spy(session, **kw):
        rows.append(kw["status"])
        return await real_record(session, **kw)

    monkeypatch.setattr(store, "record_usage", spy)
    with conformance_client() as c:
        app = onboard(c, "dc-stream", stream_mode="verified_then_stream")
        s = c.app.state.settings
        c.app.state.settings = s.model_copy(update={
            "serving": s.serving.model_copy(update={"stream_keepalive_s": 0.05})})
        started = {}

        async def never_finishes(*a, **k):
            started["yes"] = True
            await asyncio.sleep(30)

        monkeypatch.setattr(c.app.state.pipeline, "run", never_finishes)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={"stream": True, "messages": [{"role": "user", "content": "hi"}]}) as r:
            it = r.iter_text()
            assert next(it).startswith(": keepalive")
        # leaving the context closes the response = client disconnect
    assert started.get("yes")
    assert rows == ["cancelled"]
```

If the TestClient does not propagate a closed response as generator cancellation on this Starlette version, keep the three unit tests. Replace the fourth with a direct test of `_keepalive_stream`: drive it with `anext()`, call `aclose()`, then assert that the task was cancelled and that `_record_cancelled` was awaited (monkeypatch it with a recorder). Record the substitution in your report.

- [ ] **Step 2: Run the tests and confirm they fail** (`ClientDisconnected` is not defined).

- [ ] **Step 3: Implement.** In `app/api/routes.py`, add near `_error_status_detail`:

```python
class ClientDisconnected(Exception):
    """The client went away before a complete answer was ready."""


async def _await_unless_disconnected(task: "asyncio.Task", is_disconnected,
                                     poll_s: float):
    """Await `task`, polling the client connection every `poll_s`. A client that
    disconnected gets its backend work cancelled; cancelling the task closes the
    in-flight model call, which OpenAI-compatible servers treat as an abort.
    Only for paths that wait for a complete answer; real token streaming keeps
    its drain-on-disconnect accounting."""
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll_s)
        if done:
            return task.result()
        if await is_disconnected():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise ClientDisconnected()


async def _record_cancelled(request: Request, *, tenant: str, request_id: str,
                            started: float) -> None:
    """Meter a request the client abandoned. Distinct from `ok` so dashboards
    and budgets don't count work nobody received as served."""
    metrics.observe_request("-", "cancelled")
    logging.getLogger("serving.api").info(
        "client disconnected; cancelled %s after %.1fs", request_id,
        time.monotonic() - started)
    try:
        async with request.app.state.db.session() as session:
            await store.record_usage(
                session, application_id=tenant, request_id=request_id,
                model="-", provider="-", tier="-", prompt_tokens=0,
                completion_tokens=0, total_tokens=0, cost_usd=0.0,
                baseline_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                sensitivity="-", onprem_only=False, status="cancelled")
            await session.commit()
    except Exception:
        logging.getLogger("serving.obs").exception(
            "cancelled-usage record failed (swallowed)")
```

Record `started = time.monotonic()` at the top of `chat_completions`, next to `request_id`, and pass it through to `_keepalive_stream` as a new keyword argument `started`.

Wiring:
- **Non-stream path** (`stream_mode is None`). Replace `resp = await request.app.state.pipeline.run(...)` with a task awaited through `_await_unless_disconnected(task, request.is_disconnected, s.serving.stream_keepalive_s)`. Catch `ClientDisconnected` in the route before the generic `except Exception`: call `await _record_cancelled(...)`, then `return Response(status_code=499)`. The client is gone, so the status is never read. 499 is the conventional "client closed request" code, and it keeps access logs honest.
- **Keepalive stream path.** In the `if not done:` branch of Task 1 nothing changes, because the generator owns the task. In `_keepalive_stream`, wrap the whole body in `try/finally`. In the `finally`, if `not task.done()`, call `task.cancel()`, suppress the result with `await asyncio.gather(task, return_exceptions=True)`, and `await _record_cancelled(...)`. Starlette cancels a streaming response's generator when the client disconnects, so this runs then.
- **Fast stream path.** In the `done` branch within the grace period there is nothing to cancel.

- [ ] **Step 4: Run** `tests/test_client_disconnect_cancel.py tests/test_stream_keepalive.py tests/test_error_taxonomy.py tests/onboarding`, then the full suite. Expected: exit 0.

- [ ] **Step 5: Commit** `feat(api): cancel backend work when the client disconnects; meter it as cancelled`

---

### Task 3: Langfuse spans carry real timings and a generation observation

**Files:**
- Modify: `app/obs/export.py` (`export_trace`)
- Modify: `app/obs/otel.py` (`_NoOpSpan.end`, `_NoOpTracer.start_as_current_span` accepting kwargs)
- Modify: `app/api/routes.py` (`_finalize`, and every caller, pass `started_ns`)
- Test: `tests/test_trace_timing_generation.py` (new)

**Interfaces:**
- Consumes: `started` bookkeeping from Task 2. Record `started_ns = time.time_ns()` beside it at the top of `chat_completions`.
- Produces: `export_trace(..., started_ns: int | None = None, usage: dict | None = None)`. The root span starts at `started_ns`, and the serving router hop is a Langfuse generation.

- [ ] **Step 1: Write the failing tests.** First find the in-memory span-exporter helper that existing tests use: `grep -n "def _wire\|InMemorySpanExporter" tests/test_capture_io.py tests/test_obs_export.py`. Reuse it. The test below assumes `_wire(monkeypatch, redacting=True)` returns `(memory_exporter, provider)` as in `tests/test_capture_io.py`; adapt the names to what you find.

```python
"""Contract: a request's Langfuse trace shows how long it took and which
model generated the answer with how many tokens, not zero-length spans."""
import json
import time

import pytest

pytest.importorskip("opentelemetry.sdk")

from app.obs.export import export_trace
from tests.test_capture_io import _wire

TRACE = [
    {"step": "compression", "latency_ms": 5.0},
    {"step": "router", "chosen": "local-8b", "chosen_tier": "cheap",
     "chosen_provider": "onprem", "reason": "tier cheap", "rejected": [],
     "est_cost_usd": 0.001, "actual_cost_usd": 0.0009, "latency_ms": 1200.0, "hop_index": 0},
    {"step": "verify", "latency_ms": 2.0, "reason": "passed"},
]


def _spans(mem):
    return {s.name: s for s in mem.get_finished_spans()}


def test_spans_have_real_durations(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    started = time.time_ns() - 2_000_000_000   # request began 2 s ago
    export_trace(request_id="req-t", tenant_id="acme", model="local-8b",
                 cost={"total_usd": 0.0009, "baseline_usd": 0.01},
                 policy={"sensitivity": "public"}, trace=TRACE,
                 started_ns=started,
                 usage={"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020})
    provider.force_flush()
    spans = _spans(mem)
    root = spans["serving.request"]
    assert root.start_time == started
    assert (root.end_time - root.start_time) >= 1_900_000_000
    router = spans["serving.router"]
    assert (router.end_time - router.start_time) == 1_200_000_000
    assert root.start_time <= router.start_time and router.end_time <= root.end_time


def test_router_hop_is_a_generation_with_model_tokens_and_cost(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    export_trace(request_id="req-g", tenant_id="acme", model="local-8b",
                 cost={"total_usd": 0.0009, "baseline_usd": 0.01},
                 policy={"sensitivity": "public"}, trace=TRACE,
                 started_ns=time.time_ns() - 1_500_000_000,
                 usage={"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020})
    provider.force_flush()
    a = dict(_spans(mem)["serving.router"].attributes)
    assert a["langfuse.observation.type"] == "generation"
    assert a["langfuse.observation.model.name"] == "local-8b"
    assert a["gen_ai.request.model"] == "local-8b"
    assert a["gen_ai.usage.input_tokens"] == 900
    assert a["gen_ai.usage.output_tokens"] == 120
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 900, "output": 120}
    assert json.loads(a["langfuse.observation.cost_details"]) == {"total": 0.0009}
    other = dict(_spans(mem)["serving.verify"].attributes)
    assert "langfuse.observation.type" not in other


def test_no_started_ns_keeps_old_behaviour(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    export_trace(request_id="req-o", tenant_id="acme", model="local-8b",
                 cost={}, policy={}, trace=TRACE)
    provider.force_flush()
    assert "serving.request" in _spans(mem)


def test_http_request_trace_is_timed(monkeypatch, client):
    mem, provider = _wire(monkeypatch, redacting=True)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello there"}]})
    assert r.status_code == 200
    provider.force_flush()
    spans = _spans(mem)
    root = spans["serving.request"]
    assert root.end_time > root.start_time
    assert spans["serving.router"].attributes.get("langfuse.observation.type") == "generation"
```

- [ ] **Step 2: Run the tests and confirm they fail** (`export_trace()` got an unexpected keyword argument `started_ns`).

- [ ] **Step 3: Exporter.** In `app/obs/export.py`:
- Add `started_ns: int | None = None, usage: dict | None = None` to `export_trace`'s signature, after `raw_completion`.
- Add `import time`.
- Replace `with tracer.start_as_current_span("serving.request") as root:` with:

```python
        root_kwargs = {"start_time": started_ns} if started_ns else {}
        with tracer.start_as_current_span("serving.request", **root_kwargs) as root:
```

- Replace the child-span loop with:

```python
            # Lay each step out on the real clock: back to back from the request
            # start, each as long as its recorded latency, never past "now".
            # Without explicit times every span is created after the fact and
            # shows as 0 s in Langfuse.
            now_ns = time.time_ns()
            cursor = started_ns or now_ns
            served_idx = max((i for i, r in enumerate(trace)
                              if r.get("step") == "router" and r.get("chosen")),
                             default=None)
            for i, rec in enumerate(trace):
                step = str(rec.get("step", f"step-{i}"))
                dur_ns = int(float(rec.get("latency_ms") or 0.0) * 1_000_000)
                span_start = min(cursor, now_ns)
                span_end = min(span_start + dur_ns, now_ns)
                cursor = span_end
                timed = {"start_time": span_start, "end_on_exit": False} if started_ns else {}
                with tracer.start_as_current_span(f"serving.{step}", **timed) as sp:
                    sp.set_attribute("sp.step", step)
                    if rec.get("chosen") is not None:
                        sp.set_attribute("sp.chosen", redact_value(str(rec["chosen"])))
                    if "reason" in rec:
                        sp.set_attribute("sp.reason", redact_value(str(rec["reason"])))
                    cands = _candidates_json(rec)
                    if cands is not None:
                        sp.set_attribute("sp.candidates", redact_value(cands))
                    for k in ("hop_index", "actual_cost_usd", "est_cost_usd", "latency_ms"):
                        if rec.get(k) is not None:
                            sp.set_attribute(f"sp.{k}", float(rec[k]))
                    if i == served_idx:
                        _mark_generation(sp, rec, usage)
                    if started_ns:
                        sp.end(end_time=span_end)
```

- Add the helper above `export_trace`:

```python
def _mark_generation(span, rec: dict, usage: dict | None) -> None:
    """Make the hop that produced the answer a Langfuse generation: model, token
    usage and cost, which Langfuse reads from these OTel attributes. Values are
    a model id and numbers (no free text), so no redaction applies."""
    model = str(rec.get("chosen"))
    span.set_attribute("langfuse.observation.type", "generation")
    span.set_attribute("langfuse.observation.model.name", model)
    span.set_attribute("gen_ai.request.model", model)
    if usage:
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        span.set_attribute("gen_ai.usage.input_tokens", tin)
        span.set_attribute("gen_ai.usage.output_tokens", tout)
        span.set_attribute("langfuse.observation.usage_details",
                           json.dumps({"input": tin, "output": tout}))
    if rec.get("actual_cost_usd") is not None:
        span.set_attribute("langfuse.observation.cost_details",
                           json.dumps({"total": float(rec["actual_cost_usd"])}))
```

In `app/obs/otel.py`, add `def end(self, *a, **k): pass` to `_NoOpSpan`. `_NoOpTracer.start_as_current_span(self, *a, **k)` already accepts kwargs; keep it that way.

- [ ] **Step 4: Route.** In `app/api/routes.py`:
- Record `started_ns = time.time_ns()` at the top of `chat_completions`.
- Add `started_ns: int | None = None` to `_finalize`'s signature, and pass `started_ns=started_ns, usage=resp.get("usage")` into its `export_trace(...)` call.
- Thread `started_ns` through every `_finalize` caller: the non-stream path, `_keepalive_stream` (add a kwarg), `_stream_unverified_response`, `_incremental_verify_response` and `_finalize_stream` (add a kwarg to each). Use `grep -n "_finalize(\|_finalize_stream(\|_keepalive_stream(\|_stream_unverified_response(\|_incremental_verify_response(" app/api/routes.py` to find them all.

- [ ] **Step 5: Run** `tests/test_trace_timing_generation.py tests/test_capture_io.py tests/test_obs_export.py tests/test_obs_otel.py tests/test_agent_telemetry_redaction.py`, then the full suite. Expected: exit 0.

- [ ] **Step 6: Commit** `feat(obs): real span timings and a generation observation with model, tokens and cost`

---

### Task 4: Derive a session id when the client sends none

**Files:**
- Modify: `config/serving.yaml` (`server:` block), `app/config/schema.py`, `app/config/loader.py`
- Modify: `app/api/routes.py` (`_read_session_id` call site; new `_derive_session_id`)
- Test: `tests/test_derived_session_id.py` (new)

**Interfaces:**
- Produces:
  - `ServingCfg.derive_session_id: bool = True`
  - `app.api.routes._derive_session_id(body: ChatRequest, tenant: str) -> str | None`, which returns `"auto-" + 24 hex` or None.

- [ ] **Step 1: Write the failing tests**

```python
"""Contract: an agent client that sends no session header still gets one
session per conversation, derived from the conversation's opening turns
(stable as the history grows), and an explicit header always wins."""
from app.api.routes import _derive_session_id
from app.models import ChatRequest


def _req(*msgs):
    return ChatRequest.model_validate({"messages": [dict(m) for m in msgs]})


SYS = {"role": "system", "content": "You are a coding agent."}
U1 = {"role": "user", "content": "Refactor the billing module."}


def test_same_opening_turns_share_a_session_as_history_grows():
    first = _derive_session_id(_req(SYS, U1), "app-a")
    later = _derive_session_id(_req(SYS, U1,
                                    {"role": "assistant", "content": "ok"},
                                    {"role": "user", "content": "now add tests"}), "app-a")
    assert first == later
    assert first.startswith("auto-") and len(first) == 5 + 24


def test_different_conversation_or_app_gets_a_different_session():
    base = _derive_session_id(_req(SYS, U1), "app-a")
    assert _derive_session_id(_req(SYS, {"role": "user", "content": "Fix the login bug."}),
                              "app-a") != base
    assert _derive_session_id(_req(SYS, U1), "app-b") != base


def test_no_user_turn_means_no_session():
    assert _derive_session_id(_req(SYS), "app-a") is None


def test_content_parts_are_handled():
    parts = {"role": "user", "content": [{"type": "text", "text": "Refactor the billing module."}]}
    assert _derive_session_id(_req(SYS, parts), "app-a") == _derive_session_id(_req(SYS, U1), "app-a")


def test_http_response_carries_derived_session_and_header_wins(client):
    h = {"Authorization": "Bearer demo-key"}
    body = {"messages": [U1]}
    derived = client.post("/v1/chat/completions", headers=h, json=body).json()["session_id"]
    assert derived.startswith("auto-")
    explicit = client.post("/v1/chat/completions",
                           headers={**h, "x-routeforge-session": "wf-123"}, json=body).json()
    assert explicit["session_id"] == "wf-123"


def test_switch_off_keeps_old_behaviour(client):
    s = client.app.state.settings
    client.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"derive_session_id": False})})
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [U1]})
    assert not r.json().get("session_id")
```

Before writing the HTTP tests, confirm the response key: `grep -n "session_id" app/api/routes.py | head`. If the response carries the session id under a different key, or only when it is set, adapt the assertions and record the change in your report.

- [ ] **Step 2: Run the tests and confirm they fail** (import error).

- [ ] **Step 3: Implement.**

In `config/serving.yaml`, under `server:` after `stream_keepalive_s`, add:

```yaml
  # When a request has no x-routeforge-session header, derive a session id from
  # a hash of the app id, the first system message and the first user message, so
  # an agent conversation groups into one session in Langfuse and the console as
  # its history grows. The hash is one-way; no content is stored in the id.
  derive_session_id: true
```

- Schema: add `derive_session_id: bool = True` after `stream_keepalive_s` in `ServingCfg`.
- Loader: add `derive_session_id=bool(serving_raw["server"].get("derive_session_id", True)),`.

In `app/api/routes.py`, add `import hashlib` and add below `_read_session_id`:

```python
def _derive_session_id(body: ChatRequest, tenant: str) -> str | None:
    """A stable session id for a conversation whose client sent none: a one-way
    hash of the app id plus the conversation's opening turns (first system
    message, first user message). Later turns append history but keep those
    opening turns, so every request in one conversation shares the id."""
    first_user = next((m.content for m in body.messages
                       if m.role == "user" and m.content), None)
    if not first_user:
        return None
    system = next((m.content or "" for m in body.messages if m.role == "system"), "")
    digest = hashlib.sha256(
        f"{tenant}\x00{system}\x00{first_user}".encode()).hexdigest()[:24]
    return f"auto-{digest}"
```

Where the route does `session_id = _read_session_id(request)` (after authentication, so `tenant` is known), change it to:

```python
    session_id = _read_session_id(request)
    if session_id is None and s.serving.derive_session_id:
        session_id = _derive_session_id(body, tenant)
```

- [ ] **Step 4: Run** `tests/test_derived_session_id.py`, plus the existing session tests (`grep -rln "x-routeforge-session\|session_id" tests | head`), then the full suite. Expected: exit 0.

An existing test that asserts `session_id` is absent when no header is sent now encodes the old default. Update it to disable `derive_session_id`, or to expect the `auto-` prefix, whichever matches that test's intent. List every such change in your report.

- [ ] **Step 5: Commit** `feat(api): derive a per-conversation session id when the client sends none`

---

### Task 5: Log upstream failures and unhandled errors

**Files:**
- Modify: `app/gateway/gateway.py` (`run`: the `except Exception as exc` block; `run_stream`)
- Modify: `app/api/routes.py` (the 5xx branches of the route's error mapping)
- Test: `tests/test_serving_error_logs.py` (new)

**Interfaces:**
- Consumes: `_scrub_endpoints` (existing, in `gateway.py`) and the Task 1 error mapping.

- [ ] **Step 1: Write the failing tests**

```python
"""Contract: an operator can find why a request failed upstream from the
RouteForge log alone. Endpoints are scrubbed; no prompt content is logged."""
import logging


def test_backend_failure_is_logged_with_request_model_and_scrubbed_error(client, monkeypatch, caplog):
    from app.gateway.backends import litellm_backend

    async def refuse(**k):
        raise RuntimeError("connection refused by 10.0.0.5:8000 secret-prompt-text")

    monkeypatch.setattr(litellm_backend, "acompletion", refuse)
    caplog.set_level(logging.WARNING)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 502
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "serving.gateway"]
    assert lines, "no gateway failure log line"
    assert any("req-" in l and "RuntimeError" in l and "<endpoint>" in l for l in lines)
    assert not any("10.0.0.5" in l for l in lines)
    assert any(rec.name == "serving.api" and "502" in rec.getMessage() for rec in caplog.records)


def test_unhandled_error_is_logged_with_traceback(client, monkeypatch, caplog):
    async def explode(*a, **k):
        raise KeyError("unexpected")

    monkeypatch.setattr(client.app.state.pipeline, "run", explode)
    caplog.set_level(logging.ERROR)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 500
    errs = [rec for rec in caplog.records if rec.name == "serving.api" and rec.exc_info]
    assert errs and "req-" in errs[0].getMessage()
```

Check first how `client` wires the pipeline. If `client.app.state.pipeline` is not the object the route calls, monkeypatch `app.pipeline.Pipeline.run` instead. The test's scrubbed text includes a fake "secret-prompt-text" token only to make sure the log line doesn't dump the whole exception unbounded. Do not assert on it.

- [ ] **Step 2: Run the tests and confirm they fail** (no `serving.gateway` log record).

- [ ] **Step 3: Implement.**

In `app/gateway/gateway.py`, add `import logging` and `log = logging.getLogger("serving.gateway")`. In `run`'s `except Exception as exc:`, right after `last_error = ...`, add:

```python
                log.warning("backend call failed request=%s model=%s attempt=%d: %s",
                            ctx.request_id, cand.name, failover_attempts + 1, last_error)
```

In `run_stream`, wrap the `self._backend.generate_stream(...)` iteration so an exception is logged the same way (message `"backend stream failed request=%s model=%s: %s"`) and then re-raised unchanged.

In `app/api/routes.py`, in the route's mapped-error branch, log 5xx outcomes:

```python
        if mapped[0] >= 500:
            logging.getLogger("serving.api").warning(
                "request %s failed with %d: %s", request_id, mapped[0], mapped[1])
```

In the unmapped branch, change it to `logging.getLogger("serving.api").exception("unhandled error for %s", request_id)` before raising the 500. Do the same in `_keepalive_stream`'s error branch, which Task 1 already logs for the unmapped case. Add the 5xx warning for mapped errors there too.

- [ ] **Step 4: Run** `tests/test_serving_error_logs.py tests/test_error_taxonomy.py tests/test_rejection_messages.py tests/test_gateway_lateral_failover.py`, then the full suite. Expected: exit 0, and the test output must stay free of new warnings noise. If pytest shows captured-log noise that makes other tests fail, don't lower the log level. Report it instead.

- [ ] **Step 5: Commit** `fix(obs): log upstream backend failures and unhandled serving errors`

---

### Task 6: Docs and backlog

**Files:**
- Modify: `docs/reference/api-reference.md`
- Modify: `docs/reference/per-app-config.md`, only if it documents stream modes. Otherwise skip it and say so.
- Modify: `docs/backlog/bugs.md` (new B35 entry above the newest entry)

- [ ] **Step 1: API reference.** Before editing, read the code for the exact text: `grep -n "SSE_KEEPALIVE\|sse_error_frame\|stream_keepalive_s\|derive_session_id\|_derive_session_id\|499" app/api/*.py config/serving.yaml`. Add these to the streaming section of `docs/reference/api-reference.md`:
- **Keepalive comments.** Streamed requests that wait for a complete, verified answer (tool turns, `verified_then_stream`, runtime-floored requests) send `: keepalive` SSE comment lines every `server.stream_keepalive_s` seconds (default 5) once the answer takes longer than one interval. OpenAI-compatible clients ignore comment lines.
- **Late failures.** A failure after the stream opened arrives as one `data: {"error": {"message", "type": "routeforge_error", "code": <http status>}}` frame, then `data: [DONE]`. The message uses the same text as the HTTP error.
- **Fast responses and rejections** still return their normal HTTP status.
- **Client disconnects.** A client that disconnects while waiting for a complete answer has its backend work cancelled. The usage row is recorded with `status: cancelled`.
- **Sessions.** Without an `x-routeforge-session` header, the session id is derived: `auto-` plus a hash of the app, the first system message and the first user message. It can be disabled with `server.derive_session_id: false`.
- **Client guidance.** Some OpenAI-SDK-based agent clients apply a separate request timeout (often 120 s) to connect and first response bytes. The keepalive covers this. For older RouteForge versions, raise that client timeout.

- [ ] **Step 2: Backlog.** Add at the top of the entries in `docs/backlog/bugs.md`:

```markdown
## B35 — Agent tool turns retried every 120 s; abandoned work metered as served; traces unusable  ✅ RESOLVED (2026-09-16)

**Found by** a data audit of a live agent-client app. 88 of 433 requests were
retries whose starts were exactly 120–122 s apart. The client's transport
timeout covers connect plus first response bytes, and RouteForge sent nothing
until a 5–10 minute tool turn finished. Each abandoned attempt kept generating
on the backend, overlapped the retry, and was written as a `status=ok` usage
row, snapshot and Langfuse trace. In Langfuse every span was 0 s long, there was
no generation or token usage, and `sessionId` was empty. One 502 had no logged
cause.

**Fixed.**
- Streamed turns that wait for a complete answer get SSE keepalives after
  `server.stream_keepalive_s`. Late failures become SSE error frames.
- A client disconnect cancels the backend work and meters it as `cancelled`.
- Trace spans carry real timings, and the served router hop is a Langfuse
  generation with model, tokens and cost.
- A session id is derived when the client sends none.
- Upstream backend failures and unhandled errors are logged.

**Unchanged by design.** Real token streaming (`stream_unverified`,
`incremental_verify`) still drains on disconnect to keep accounting.
```

- [ ] **Step 3: Run** `tests/test_docs_links.py`, then the full suite. Expected: exit 0.

- [ ] **Step 4: Commit** `docs: stream keepalive, disconnect cancel, derived sessions (B35)`

---

## After all tasks (controller)

1. Run the final whole-branch review and one PR, then merge once the full suite passes.
2. Deploy to the DGX Spark: pull and rebuild api. There are no schema changes; the serving.yaml keys are baked into the image. Then verify:
   - `/health`;
   - a slow streamed tool turn from the Spark shows `: keepalive` frames;
   - a Langfuse trace in the app's own project shows a GENERATION observation with model and token usage, non-zero latency, and a `sessionId`;
   - for the next hour of traffic, usage rows show no back-to-back repeats at ~120 s gaps.
