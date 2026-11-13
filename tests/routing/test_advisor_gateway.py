"""B108: the gateway hands `advisor_tier` to EVERY router selection of a
request -- the first, each failover re-select and each confidence-escalation
re-select -- and nothing else changes: `exclude` only removes candidates,
escalation's `min_tier` still dominates, and `onprem_only` never reaches cloud.
"""
from __future__ import annotations

import asyncio

import pytest

from app.errors import BackendUnavailable
from app.gateway.gateway import Gateway
from app.models import Message
from app.routing.router import Router
from tests.test_gateway_lateral_failover import S, _FailForModels, _ctx, _onprem, _reg


class _SpyRouter(Router):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[dict] = []

    def select(self, ctx, tokens_after, complexity, min_tier=None, exclude=frozenset(),
               output_tokens=None, open_circuits=frozenset(), advisor_tier=None):
        self.calls.append({"advisor_tier": advisor_tier, "exclude": exclude,
                           "min_tier": min_tier})
        return super().select(ctx, tokens_after, complexity, min_tier, exclude,
                              output_tokens, open_circuits, advisor_tier=advisor_tier)


def _pool():
    return _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b"),
                 "onprem-mid": _onprem("onprem-mid", tier="mid")})


def _run(backend, ctx, reg, advisor_tier, complexity=0.0, hint=None):
    router = _SpyRouter(S, registry=reg)
    gw = Gateway(S, backend, router, reg)
    msgs = [Message(role="user", content="hello")]
    out = asyncio.run(gw.run(ctx, msgs, 20, complexity, hint or {},
                             advisor_tier=advisor_tier))
    return out, router


def test_every_failover_reselect_carries_the_advisor_tier():
    reg = _pool()
    backend = _FailForModels(S, {"onprem-mid"})
    (_result, records), router = _run(
        backend, _ctx(reg, {"onprem", "azure", "bedrock"}, onprem=True), reg, "frontier")
    # frontier clamps to the best on-prem tier (mid); mid failed; the
    # re-select still targets the advisor's tier and steps down to cheap
    # (low risk: the hard floor is cheap).
    assert [c["advisor_tier"] for c in router.calls] == ["frontier", "frontier"]
    assert router.calls[1]["exclude"] == frozenset({"onprem-mid"})
    served = [r for r in records if r.step == "router"][-1]
    assert served.chosen_tier == "cheap" and served.chosen_provider == "onprem"
    assert not any(m.startswith(("azure", "bedrock")) for m in backend.calls)


def test_onprem_only_high_risk_failover_never_reaches_cloud():
    reg = _pool()
    backend = _FailForModels(S, {"onprem-mid"})
    with pytest.raises(BackendUnavailable):
        _run(backend, _ctx(reg, {"onprem", "azure", "bedrock"}, onprem=True, risk="high"),
             reg, "cheap")
    assert backend.calls == ["onprem-mid"]


class _LowConfidenceFirst(_FailForModels):
    """Answers every call; the first answer reports low confidence, so the
    gateway escalates once."""

    async def generate(self, *a, **kw):
        result = await super().generate(*a, **kw)
        if len(self.calls) == 1:
            result = result.model_copy(update={"confidence": 0.1})
        return result


def test_escalation_reselect_carries_the_advisor_tier_and_min_tier_wins():
    reg = _pool()
    backend = _LowConfidenceFirst(S, set())
    (_result, records), router = _run(
        backend, _ctx(reg, {"onprem"}, onprem=True), reg, "cheap")
    assert [c["advisor_tier"] for c in router.calls] == ["cheap", "cheap"]
    assert [c["min_tier"] for c in router.calls] == [None, "mid"]
    served = [r for r in records if r.step == "router"]
    assert [r.chosen_tier for r in served] == ["cheap", "mid"]


def test_run_stream_passes_the_advisor_tier():
    reg = _pool()
    router = _SpyRouter(S, registry=reg)
    gw = Gateway(S, _FailForModels(S, set()), router, reg)
    _stream, records = asyncio.run(gw.run_stream(
        _ctx(reg, {"onprem"}, onprem=True), [Message(role="user", content="hi")],
        20, 0.0, {}, advisor_tier="mid"))
    assert router.calls[0]["advisor_tier"] == "mid"
    assert records[0].chosen_tier == "mid"


def test_none_is_todays_gateway():
    reg = _pool()
    a, _ = _run(_FailForModels(S, {"onprem-a"}), _ctx(reg, {"onprem"}, onprem=True),
                reg, None)
    b_router = Router(S, registry=reg)
    b = asyncio.run(Gateway(S, _FailForModels(S, {"onprem-a"}), b_router, reg).run(
        _ctx(reg, {"onprem"}, onprem=True), [Message(role="user", content="hello")],
        20, 0.0, {}))
    strip = lambda recs: [r.model_dump(exclude={"latency_ms"}) for r in recs]  # noqa: E731
    assert strip(a[1]) == strip(b[1])
