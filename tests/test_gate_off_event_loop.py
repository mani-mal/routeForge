"""B118: no detector call runs on the asyncio event loop.

Presidio is CPU-bound; on a 56k-token prompt `Gate.evaluate` blocked the loop
for ~16 s, so `/health` could not answer and the k8s liveness probe killed the
pod. Every request-path detector call is now awaited through
`app.policy.detector_pool.run_detector` -- a dedicated single-worker executor,
not the default one the loop also resolves DNS on. These tests pin:

* the loop keeps turning while a slow detector runs (a concurrent ticker
  coroutine makes progress), on `run()` and on `run_stream()`;
* every detector pass -- gate, tool-conversation view, step-9 verify, egress
  tool-call redaction, cache-hit re-verify, the incremental streaming verifier,
  `capture_io: redacted` telemetry/console copies -- runs on the detector
  executor's thread;
* under contention (the executor busy with another request's pass) a
  cache-hit and an incremental_verify stream WAIT for it without blocking the
  loop;
* fail-closed (CLAUDE.md rule 5) is unchanged -- a detector exception in the
  executor still surfaces as `PolicyGateError` and nothing downstream runs.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.cache.store import DeterministicSemanticCache
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.errors import PolicyGateError
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Entity, Message
from app.pipeline import Pipeline
from app.policy import detector_pool, detectors
from app.policy.detectors import RegexDetector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

_SLOW_S = 0.5
_TICK_S = 0.01
_SSN = "123-45-6789"


def _on_detector_thread() -> bool:
    return threading.current_thread().name.startswith(detector_pool.THREAD_NAME_PREFIX)


class _RecordingDetector(RegexDetector):
    """Regex detection that records the thread of every `analyze` and runs the
    production off-loop check (strict in tests, see conftest), optionally
    sleeping -- a stand-in for a CPU-bound Presidio pass. `time.sleep` blocks
    whatever thread calls it, so on the loop it would stop the loop."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.thread_names: list[str] = []

    def analyze(self, text: str) -> list[Entity]:
        self.thread_names.append(threading.current_thread().name)
        detectors.check_off_loop("_RecordingDetector.analyze")
        if self.delay:
            time.sleep(self.delay)
        return super().analyze(text)

    def assert_all_on_detector_thread(self) -> None:
        assert self.thread_names, "the detector was never called"
        bad = [n for n in self.thread_names
               if not n.startswith(detector_pool.THREAD_NAME_PREFIX)]
        assert not bad, f"detector ran off the detector executor: {bad}"


class _ExplodingDetector(RegexDetector):
    def analyze(self, text: str) -> list[Entity]:
        raise RuntimeError("detector crashed")


class _SpyGateway(Gateway):
    calls = 0

    async def run(self, *a, **kw):  # type: ignore[override]
        type(self).calls += 1
        return await super().run(*a, **kw)


class _ToolCallBackend(_DeterministicLiteLLM):
    """Answers with a tool call whose arguments carry an SSN, so the egress
    tool-call redaction has something to redact."""

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        result = await super().generate(model, messages, price, tier, hint, tools)
        return result.model_copy(update={"tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "lookup",
                         "arguments": f'{{"ssn": "{_SSN}"}}'}}]})


def _settings(*, cache: bool = False):
    s = load_settings()
    serving = s.serving.model_copy(update={"pii_detection": "regex"})
    if cache:
        sc = serving.semantic_cache.model_copy(update={"enabled": True})
        serving = serving.model_copy(update={"semantic_cache": sc})
    return s.model_copy(update={"serving": serving})


def _pipeline(det, *, backend_cls=_DeterministicLiteLLM, cache: bool = False) -> Pipeline:
    settings = _settings(cache=cache)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = _SpyGateway(settings, backend_cls(settings),
                          Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600), registry=registry,
                    cache=DeterministicSemanticCache(ttl_seconds=3600) if cache else None)


def _request() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="What is 2+2?")])


def _tool_request() -> ChatRequest:
    """A client-driven tool conversation: a replayed assistant tool call and
    its result, both carrying an SSN."""
    return ChatRequest(
        messages=[
            Message(role="user", content="look up the record"),
            Message(role="assistant", content=None, tool_calls=[{
                "id": "call_0", "type": "function",
                "function": {"name": "lookup",
                             "arguments": f'{{"ssn": "{_SSN}"}}'}}]),
            Message(role="tool", tool_call_id="call_0",
                    content=f"record for {_SSN}: ok"),
        ],
        tools=[{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object"}}}])


async def _with_ticker(coro) -> tuple[object, int]:
    """Run `coro` while a ticker coroutine counts how often the loop lets it
    run. A blocked loop yields ~0 ticks for the whole slow detector pass."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(_TICK_S)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, ticks


def _occupy_executor(seconds: float) -> threading.Thread:
    """Another request's long detector pass: a background thread puts a
    `seconds`-long job on the detector executor (and holds it)."""
    started = threading.Event()

    def job() -> None:
        started.set()
        time.sleep(seconds)

    th = threading.Thread(
        target=lambda: detector_pool._EXECUTOR.submit(job).result())
    th.start()
    assert started.wait(2), "the executor never picked the blocking job up"
    return th


# --- the loop keeps turning --------------------------------------------------


def test_run_gate_does_not_block_event_loop():
    det = _RecordingDetector(delay=_SLOW_S)
    pipeline = _pipeline(det)

    result, ticks = asyncio.run(_with_ticker(
        pipeline.run("req-b118", "acme", _request())))
    assert result is not None
    det.assert_all_on_detector_thread()
    # At least the gate's 0.5 s pass elapsed; a free loop ticks ~50 times in
    # it. Blocked, it would tick at most once or twice around the awaits.
    assert ticks >= 10, f"event loop starved during the gate: {ticks} ticks"


def test_run_stream_gate_does_not_block_event_loop():
    det = _RecordingDetector(delay=_SLOW_S)
    pipeline = _pipeline(det)

    async def main():
        (deltas, _meta), ticks = await _with_ticker(
            pipeline.run_stream("req-b118s", "demo", _request()))
        await deltas.aclose()
        return ticks

    ticks = asyncio.run(main())
    det.assert_all_on_detector_thread()
    assert ticks >= 10, f"event loop starved during the gate: {ticks} ticks"


# --- every detector pass runs on the detector executor -----------------------


def test_tool_conversation_view_and_egress_tool_call_redaction_off_loop(monkeypatch):
    """The lazy per-message redacted view (`_tool_turn_messages`) and the
    egress tool-call redaction both run detector passes on the executor --
    and still redact (the SSN never reaches the model or the client)."""
    import app.pipeline as pipeline_mod

    det = _RecordingDetector()
    pipeline = _pipeline(det, backend_cls=_ToolCallBackend)
    seen: dict[str, bool] = {}

    real_view = Pipeline._tool_turn_messages

    def view(reps, eff):
        seen["tool_turn_messages"] = _on_detector_thread()
        return real_view(reps, eff)

    real_egress = pipeline_mod.redact_tool_calls_with_entities

    def egress(*a, **kw):
        seen["egress_tool_calls"] = _on_detector_thread()
        return real_egress(*a, **kw)

    monkeypatch.setattr(Pipeline, "_tool_turn_messages", staticmethod(view))
    monkeypatch.setattr(pipeline_mod, "redact_tool_calls_with_entities", egress)

    resp = asyncio.run(pipeline.run("req-b118t", "acme", _tool_request()))

    assert seen == {"tool_turn_messages": True, "egress_tool_calls": True}
    det.assert_all_on_detector_thread()
    tool_calls = resp["choices"][0]["message"]["tool_calls"]
    assert _SSN not in tool_calls[0]["function"]["arguments"]


def test_cache_hit_reverify_runs_on_detector_executor():
    det = _RecordingDetector()
    pipeline = _pipeline(det, cache=True)

    asyncio.run(pipeline.run("r1", "acme", _request()))      # miss: fills the cache
    det.thread_names.clear()
    hit = asyncio.run(pipeline.run("r2", "acme", _request()))

    assert next(r for r in hit["trace"] if r.get("step") == "cache")["hit"] is True
    det.assert_all_on_detector_thread()


def test_capture_io_redacted_copies_run_on_detector_executor(monkeypatch):
    """`capture_io: redacted` builds telemetry and console redacted copies --
    detector passes -- on the executor, and `full`/`off` never reach it."""
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import create_app
    from app.obs import export
    from app.obs.redact import redacted_copy as real_copy

    monkeypatch.setenv("SP_CAPTURE_IO", "redacted")
    threads: list[bool] = []

    def copy(*a, **kw):
        threads.append(_on_detector_thread())
        return real_copy(*a, **kw)

    monkeypatch.setattr(routes, "redacted_copy", copy)
    monkeypatch.setattr(export, "redacted_copy", copy)

    client = TestClient(create_app())
    from tests.test_stream_unverified import _make_app
    token = _make_app(client, "b118-cio", stream_mode="off")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "messages": [
                        {"role": "user", "content": f"my ssn is {_SSN}"}]})
    assert r.status_code == 200, r.text
    assert threads, "no redacted copy was built under capture_io: redacted"
    assert all(threads), f"redacted_copy ran off the detector executor: {threads}"


# --- contention: the executor is busy with another request's pass ------------


def test_contention_cache_hit_and_incremental_verify_wait_without_blocking_loop(
        monkeypatch):
    """Another request's 0.5 s detector pass holds the (single-worker)
    executor. A cache-hit re-verify and an incremental_verify stream both
    need it: they must queue behind it -- and the loop must keep turning."""
    from app.api import routes
    from app.apps.policy import AppPolicyView
    from app.apps.privacy import AppPrivacyProfile

    det = _RecordingDetector()
    pipeline = _pipeline(det, cache=True)
    asyncio.run(pipeline.run("r1", "acme", _request()))      # fills the cache

    feed_threads: list[str] = []

    class _RecordingVerifier:
        def __init__(self, *_a):
            pass

        def feed(self, d):
            feed_threads.append(threading.current_thread().name)
            return d

        def flush(self):
            feed_threads.append(threading.current_thread().name)
            return ""

    async def no_finalize(*_a, **_kw):
        return None

    monkeypatch.setattr(routes, "StreamingVerifier", _RecordingVerifier)
    monkeypatch.setattr(routes, "_finalize", no_finalize)

    async def stream_once() -> bytes:
        async def deltas():
            yield "one "
            yield "two"

        def get_meta():
            return {"raw_completion": "one two", "trace": [], "model": "local-8b"}
        get_meta.detector = None
        get_meta.ctx = None

        resp = routes._incremental_verify_response(
            object(), request_id="req-b118-iv", deltas=deltas(),
            get_meta=get_meta,
            body=ChatRequest(model="auto", messages=[Message(role="user", content="hi")]),
            tenant="acme", app_policy=AppPolicyView(), app_langfuse=None,
            profile=AppPrivacyProfile(recognizers=(), rules=()),
            session_id=None, s=load_settings(), started=0.0)
        body = bytearray()

        async def send(message):
            body.extend(message.get("body", b""))

        async def receive():
            await asyncio.sleep(10)
            return {"type": "http.disconnect"}

        await resp({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        return bytes(body)

    async def main():
        holder = _occupy_executor(_SLOW_S)
        t0 = time.monotonic()
        try:
            (hit, body), ticks = await _with_ticker(asyncio.gather(
                pipeline.run("r2", "acme", _request()), stream_once()))
        finally:
            holder.join()
        return hit, body, ticks, time.monotonic() - t0

    det.thread_names.clear()
    hit, body, ticks, elapsed = asyncio.run(main())

    assert next(r for r in hit["trace"] if r.get("step") == "cache")["hit"] is True
    assert b"one " in body and b"two" in body and b"[DONE]" in body
    det.assert_all_on_detector_thread()
    assert feed_threads and all(
        n.startswith(detector_pool.THREAD_NAME_PREFIX) for n in feed_threads)
    # Both waited for the busy executor (so they really used it) ...
    assert elapsed >= _SLOW_S * 0.8
    # ... and the loop kept turning the whole time.
    assert ticks >= 10, f"event loop starved under contention: {ticks} ticks"


# --- fail closed --------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_gate_exception_in_detector_executor_fails_closed(stream: bool):
    pipeline = _pipeline(_ExplodingDetector())
    _SpyGateway.calls = 0

    async def main():
        if stream:
            await pipeline.run_stream("req-b118x", "demo", _request())
        else:
            await pipeline.run("req-b118x", "acme", _request())

    with pytest.raises(PolicyGateError) as exc:
        asyncio.run(main())
    # The original error is chained, not swallowed, and nothing past the gate
    # ran: the gateway was never called.
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert _SpyGateway.calls == 0


def test_off_loop_guard_fails_an_on_loop_detector_call():
    """The suite-wide guard (conftest) is live: a checked detector called on
    the loop thread raises instead of silently blocking it."""
    det = _RecordingDetector()

    async def on_loop():
        det.analyze("hello")

    with pytest.raises(RuntimeError, match="B118"):
        asyncio.run(on_loop())
