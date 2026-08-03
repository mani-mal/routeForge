import asyncio

import pytest

from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, Message
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

S = load_settings()
REG = make_test_registry(S)


class _AlwaysLowBackend(_DeterministicLiteLLM):
    async def generate(self, model, messages, price, tier, hint, tools=None):
        r = await super().generate(model, messages, price, tier, {})
        return r.model_copy(update={"confidence": 0.1})


def _ctx(providers, risk="low"):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _run(ctx, hint):
    gw = Gateway(S, _DeterministicLiteLLM(S), Router(S, registry=REG), REG)
    msgs = [Message(role="user", content="hello")]
    return asyncio.run(gw.run(ctx, msgs, 20, 0.1, hint))


def test_happy_path_one_hop():
    result, records = _run(_ctx({"onprem", "azure", "bedrock"}), {})
    assert result.confidence == 0.95
    assert sum(1 for r in records if r.step == "router") == 1


def test_low_confidence_escalates_exactly_once():
    result, records = _run(_ctx({"onprem", "azure", "bedrock"}),
                           {"mock_confidence": 0.3})
    router_hops = [r for r in records if r.step == "router"]
    assert len(router_hops) == 2  # initial + one escalation, then stop


def test_escalation_cap_holds_when_confidence_stays_low():
    gw = Gateway(S, _AlwaysLowBackend(S), Router(S, registry=REG), REG)
    msgs = [Message(role="user", content="hello")]
    _result, records = asyncio.run(
        gw.run(_ctx({"onprem", "azure", "bedrock"}), msgs, 20, 0.1, {}))
    router_hops = [r for r in records if r.step == "router"]
    assert len(router_hops) == 2  # cap=max_escalations(1): initial + exactly one escalation


def test_onprem_only_backend_failure_fails_closed_not_hang():
    # onprem_only clamps to the best on-prem tier; a persistently failing backend
    # must fail closed (raise), not loop forever.
    gw = Gateway(S, _DeterministicLiteLLM(S), Router(S, registry=REG), REG)
    msgs = [Message(role="user", content="hello")]
    ctx = _ctx({"onprem"}, risk="high")  # only onprem allowed -> tier clamps to mid
    with pytest.raises(Exception):
        asyncio.run(gw.run(ctx, msgs, 20, 0.1, {"mock_fail": True}))
