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


def _onprem(slug: str, *, tier: str = "cheap") -> dict:
    return dict(display_name=slug, provider="onprem", tier=tier,
               price_input=0.1, price_output=0.1, endpoint=f"http://{slug}/v1",
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
    """Minimal InferenceBackend for the streaming test: never fails, no
    litellm dependency -- the breaker excludes "a" up front, so only the
    chosen (non-"a") candidate is ever asked to stream."""

    name = "fake-stream"

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        raise NotImplementedError

    async def generate_stream(self, model, messages, price, tier, hint,
                              tools=None, tool_choice=None, params=None):
        yield StreamChunk(delta="hi")
        yield StreamChunk(final=GenerationResult(
            model=model, text="hi", tokens_in=1, tokens_out=1,
            latency_ms=10.0, cost_usd=0.0001, confidence=None))

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


def test_open_circuit_saves_the_wasted_attempt():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    backend = _FailForModels(S, {"a"})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(backend, reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    # Force "a" to be tried first by making it the only candidate until it trips.
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
    gw = _gw(_FakeStreamBackend(), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    records = asyncio.run(_stream_and_collect(gw, ctx))
    router = next(r for r in records if r.step == "router")
    assert router.chosen != "a"
    assert any(x.name == "a" and x.code == "circuit_open" for x in router.rejected)
