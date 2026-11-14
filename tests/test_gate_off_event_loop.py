"""B118: the policy gate runs off the asyncio event loop.

Presidio is CPU-bound; on a 56k-token prompt `Gate.evaluate` blocked the loop
for ~16 s, so `/health` could not answer and the k8s liveness probe killed the
pod. The pipeline now awaits the gate (and the other whole-text detector
passes) via `asyncio.to_thread`. These tests pin both halves of that change:

* the loop keeps turning while a slow detector runs (a concurrent ticker
  coroutine makes progress), on `run()` and on `run_stream()`;
* fail-closed (CLAUDE.md rule 5) is unchanged -- an exception raised by the
  detector inside the worker thread still surfaces as `PolicyGateError`, and
  nothing downstream (the gateway) is reached.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.errors import PolicyGateError
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Entity, Message
from app.pipeline import Pipeline
from app.policy.detectors import RegexDetector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

_SLOW_S = 0.5
_TICK_S = 0.01


class _SlowDetector(RegexDetector):
    """Regex detection, but every `analyze` sleeps -- a stand-in for a
    CPU-bound Presidio pass. `time.sleep` blocks whatever thread calls it, so
    if the pipeline calls this on the event loop, the loop stops."""

    def __init__(self) -> None:
        self.threads: list[int] = []

    def analyze(self, text: str) -> list[Entity]:
        self.threads.append(threading.get_ident())
        time.sleep(_SLOW_S)
        return super().analyze(text)


class _ExplodingDetector(RegexDetector):
    def analyze(self, text: str) -> list[Entity]:
        raise RuntimeError("detector crashed")


class _SpyGateway(Gateway):
    calls = 0

    async def run(self, *a, **kw):  # type: ignore[override]
        type(self).calls += 1
        return await super().run(*a, **kw)


def _pipeline(det) -> Pipeline:
    s = load_settings()
    settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"pii_detection": "regex"})})
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = _SpyGateway(settings, _DeterministicLiteLLM(settings),
                          Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600), registry=registry)


def _request() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="What is 2+2?")])


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


def test_run_gate_does_not_block_event_loop():
    det = _SlowDetector()
    pipeline = _pipeline(det)

    async def main():
        loop_thread = threading.get_ident()
        result, ticks = await _with_ticker(
            pipeline.run("req-b118", "acme", _request()))
        return loop_thread, result, ticks

    loop_thread, result, ticks = asyncio.run(main())
    assert result is not None
    assert det.threads, "the slow detector was never called"
    # Every detector pass ran on a worker thread, never on the loop's thread.
    assert loop_thread not in det.threads
    # At least the gate's 0.5 s pass elapsed; a free loop ticks ~50 times in
    # it. Blocked, it would tick at most once or twice around the awaits.
    assert ticks >= 10, f"event loop starved during the gate: {ticks} ticks"


def test_run_stream_gate_does_not_block_event_loop():
    det = _SlowDetector()
    pipeline = _pipeline(det)

    async def main():
        loop_thread = threading.get_ident()
        (deltas, _meta), ticks = await _with_ticker(
            pipeline.run_stream("req-b118s", "demo", _request()))
        await deltas.aclose()
        return loop_thread, ticks

    loop_thread, ticks = asyncio.run(main())
    assert det.threads, "the slow detector was never called"
    assert loop_thread not in det.threads
    assert ticks >= 10, f"event loop starved during the gate: {ticks} ticks"


@pytest.mark.parametrize("stream", [False, True])
def test_gate_exception_in_worker_thread_fails_closed(stream: bool):
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
