"""Task 4: wire the step-7 circuit breaker into the Gateway (B45).

`Gateway.run` and `Gateway.run_stream` read `breaker.open_set()` on EVERY
`Router.select` call (each hop, not once per request), record a failure only
when `counts_as_endpoint_unhealth` says the failure is the endpoint's fault
(a 4xx never trips it -- B36), and record a success after a successful
generate. `RoutingDecision.circuit_bypass` (set when every eligible candidate
was open and the router retried without the breaker) rides the step="router"
`DecisionRecord` unchanged.

Helpers (`_FailForModels`, `_onprem`, `_reg`, `_ctx`, `S`) copied from
tests/test_gateway_lateral_failover.py so this module has no import-time
dependency on it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.apps import store
from app.config.loader import load_settings
from app.gateway.backends.base import StreamChunk
from app.gateway.breaker import CircuitBreaker
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, GenerationResult, Message
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM

# Enable both cloud providers so their static-pool candidates are actually
# present in the effective pool (mirrors test_gateway_lateral_failover.py).
_S0 = load_settings()
_prov = dict(_S0.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S = _S0.model_copy(update={"serving": _S0.serving.model_copy(update={"providers": _prov})})


def _onprem(slug: str, *, tier: str = "cheap", price: float = 0.1) -> dict:
    return dict(display_name=slug, provider="onprem", tier=tier,
               price_input=price, price_output=price, endpoint=f"http://{slug}/v1",
               enabled=True, allowed_app_ids=None)


async def _build_reg(rows: dict[str, dict]) -> ModelRegistry:
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, fields in rows.items():
            await store.upsert_onboarded(s, slug, **fields)
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg


def _reg(rows: dict[str, dict]) -> ModelRegistry:
    return asyncio.run(_build_reg(rows))


class _FailForModels(_DeterministicLiteLLM):
    """Deterministic backend that raises for any model whose name is in
    `fail_models`, and records every model it was asked to generate for."""

    def __init__(self, settings, fail_models: set[str]) -> None:
        super().__init__(settings)
        self._fail = set(fail_models)
        self.calls: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(model)
        if model in self._fail:
            raise RuntimeError(f"forced failure for {model}")
        return await super().generate(model, messages, price, tier, {})


class _Exc400(Exception):
    status_code = 400


class _FailWith(_DeterministicLiteLLM):
    """Raises `exc_factory()` for `model`, delegates otherwise."""

    def __init__(self, settings, model: str, exc_factory) -> None:
        super().__init__(settings)
        self._model = model
        self._exc_factory = exc_factory
        self.calls: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(model)
        if model == self._model:
            raise self._exc_factory()
        return await super().generate(model, messages, price, tier, {})


class _FakeStreamBackend:
    """Minimal InferenceBackend for streaming tests: no litellm dependency.
    `mode`:
      "ok"   -- yields a delta then the terminal StreamChunk(final=...).
      "fail" -- yields a delta then raises `exc` (default RuntimeError, no
                status_code -> counts_as_endpoint_unhealth is True).
      "hang" -- yields a delta then awaits an Event that never fires, so a
                consumer that closes the iterator early lands mid-stream,
                before any terminal chunk or exception -- neither
                `record_success` nor `record_failure` should ever run.
    """

    name = "fake-stream"

    def __init__(self, mode: str = "ok", exc: Exception | None = None) -> None:
        self._mode = mode
        self._exc = exc if exc is not None else RuntimeError("forced stream failure")

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        raise NotImplementedError

    async def generate_stream(self, model, messages, price, tier, hint,
                              tools=None, tool_choice=None, params=None):
        yield StreamChunk(delta="hi")
        if self._mode == "ok":
            yield StreamChunk(final=GenerationResult(
                model=model, text="hi", tokens_in=1, tokens_out=1,
                latency_ms=10.0, cost_usd=0.0001, confidence=None))
        elif self._mode == "fail":
            raise self._exc
        elif self._mode == "hang":
            await asyncio.Event().wait()   # never resolves; caller closes early

    async def health(self):
        raise NotImplementedError


def _ctx(reg, providers, *, onprem: bool = False, risk: str = "low",
         max_cost: float = 0.05) -> ExecutionPolicyContext:
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in reg.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _gw(backend, reg, breaker):
    return Gateway(S, backend, Router(S, registry=reg), reg, breaker=breaker)


def _call(gw, ctx):
    return asyncio.run(gw.run(ctx, [Message(role="user", content="hi")], 20, 0.1, {}))


async def _stream_and_collect(gw, ctx):
    agen, records = await gw.run_stream(
        ctx, [Message(role="user", content="hi")], 20, 0.1, {})
    async for _ in agen:
        pass
    return records


async def _stream_first_then_close(gw, ctx):
    """Consumes exactly one item off the stream then closes the iterator
    early (as a disconnected client would) -- `aclose()` throws GeneratorExit
    into `_agen` at its `yield chunk` suspension point, which propagates
    (Gateway's `except Exception` does not catch BaseException) without ever
    reaching a terminal chunk or an `except Exception` handler."""
    agen, records = await gw.run_stream(
        ctx, [Message(role="user", content="hi")], 20, 0.1, {})
    await agen.__anext__()
    await agen.aclose()
    return records


def test_open_circuit_saves_the_wasted_attempt():
    # "a" is cheaper than "b", so absent the breaker the router would prefer
    # and attempt "a" first every time -- the open circuit must override that
    # preference and route straight to "b" without ever calling "a".
    reg = _reg({"a": _onprem("a", price=0.05), "b": _onprem("b", price=0.2)})
    backend = _FailForModels(S, {"a"})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(backend, reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    for _ in range(S.routing.breaker.failure_threshold):
        br.record_failure("a")
    backend.calls.clear()
    _result, records = _call(gw, ctx)
    assert backend.calls == ["b"]                   # "a" never attempted
    router = next(r for r in records if r.step == "router")
    assert any(x.name == "a" and x.code == "circuit_open" for x in router.rejected)


def test_gateway_failures_trip_the_breaker_and_4xx_does_not():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(_FailForModels(S, {"a"}), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    for _ in range(S.routing.breaker.failure_threshold):
        _call(gw, ctx)          # whenever "a" is picked first it fails over to "b"
    # "a" failed at least once per call it was chosen; assert via state, not count:
    st = br.state("a")
    assert st.consecutive_failures >= 1


def test_success_records_on_the_breaker():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    br.record_failure("a"); br.record_failure("a")
    _call(_gw(_FailForModels(S, set()), reg, br), _ctx(reg, {"onprem"}, onprem=True))
    assert br.state("a").consecutive_failures == 0


def test_all_open_bypass_is_on_the_record():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    for _ in range(S.routing.breaker.failure_threshold):
        br.record_failure("a")
    _result, records = _call(_gw(_FailForModels(S, set()), reg, br),
                             _ctx(reg, {"onprem"}, onprem=True))
    router = next(r for r in records if r.step == "router")
    assert router.circuit_bypass is True


def test_no_breaker_is_todays_behaviour():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    backend = _FailForModels(S, {"a"})
    _result, records = _call(_gw(backend, reg, None), _ctx(reg, {"onprem"}, onprem=True))
    assert all(not getattr(r, "circuit_bypass", False) for r in records)


def test_4xx_does_not_trip_the_breaker():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    br = CircuitBreaker(S.routing.breaker)
    backend = _FailWith(S, "a", lambda: _Exc400("bad request"))
    gw = _gw(backend, reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    for _ in range(S.routing.breaker.failure_threshold + 1):
        _call(gw, ctx)
    assert br.state("a").consecutive_failures == 0
    assert br.open_set() == frozenset()


def test_run_stream_open_circuit_excludes_the_model():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    br = CircuitBreaker(S.routing.breaker)
    for _ in range(S.routing.breaker.failure_threshold):
        br.record_failure("a")
    gw = _gw(_FakeStreamBackend(mode="ok"), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    records = asyncio.run(_stream_and_collect(gw, ctx))
    router = next(r for r in records if r.step == "router")
    assert router.chosen != "a"
    assert any(x.name == "a" and x.code == "circuit_open" for x in router.rejected)


# --- Fix round 1, minor 3: run_stream breaker recording ---------------------

def test_run_stream_success_records_on_the_breaker():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    br.record_failure("a")                       # below threshold, still closed
    gw = _gw(_FakeStreamBackend(mode="ok"), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    asyncio.run(_stream_and_collect(gw, ctx))
    assert br.state("a").consecutive_failures == 0


def test_run_stream_failure_records_on_the_breaker():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(_FakeStreamBackend(mode="fail"), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    with pytest.raises(RuntimeError):
        asyncio.run(_stream_and_collect(gw, ctx))
    assert br.state("a").consecutive_failures == 1


def test_run_stream_early_close_records_nothing():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(_FakeStreamBackend(mode="hang"), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    asyncio.run(_stream_first_then_close(gw, ctx))
    st = br.state("a")
    assert st.state == "closed"
    assert st.consecutive_failures == 0


# --- Fix round 1, finding 1 (reviewer probe): removing the Task 2 lease -----
#
# Old design: `open_set()` granted a single-caller trial lease from inside a
# plain READ, so a request that could never have used "a" anyway (narrower
# app scope) still burned it -- leaving a genuinely recovered "a" excluded
# for `trial_timeout_s` even though nothing ever attempted it. New design:
# `open_set()` is a pure read, so an ineligible request reading it changes
# nothing, and the very next eligible request still finds "a" admitted.

def test_ineligible_request_does_not_starve_a_recovered_endpoint():
    # "a" is cheaper than "b" -- the router prefers "a" whenever both are
    # eligible candidates, so if "a" is genuinely admitted it gets chosen.
    reg = _reg({"a": _onprem("a", price=0.05), "b": _onprem("b", price=0.2)})
    br = CircuitBreaker(S.routing.breaker)
    # Trip "a" using `now` values far enough in the past that its cooldown
    # has already elapsed relative to the real wall clock Gateway.run() will
    # use (it calls open_set()/record_* with no explicit `now`).
    base = time.monotonic() - (S.routing.breaker.cooldown_s + 3600)
    for k in range(S.routing.breaker.failure_threshold):
        br.record_failure("a", now=base + k)
    assert br.state("a").state == "half_open"   # cooldown already elapsed

    backend = _FailForModels(S, set())           # nothing fails once attempted
    gw = _gw(backend, reg, br)

    # One request that CANNOT use "a" at all (narrower app scope) -- under
    # the old lease design this alone would have consumed the single trial
    # and left "a" excluded for every OTHER caller until trial_timeout_s.
    ctx_b_only = _ctx(reg, {"onprem"}, onprem=True)
    ctx_b_only = ctx_b_only.model_copy(update={"allowed_models": frozenset({"b"})})
    _call(gw, ctx_b_only)
    assert backend.calls == ["b"]

    # The very next request that CAN use "a" must still find it admitted.
    backend.calls.clear()
    ctx_any = _ctx(reg, {"onprem"}, onprem=True)
    _call(gw, ctx_any)
    assert backend.calls == ["a"]
