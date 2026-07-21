import asyncio

from app.config.loader import load_settings
from app.gateway.backends.mock import MockBackend
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, Message
from app.routing.router import Router

S = load_settings()


def _ctx(providers, risk="low"):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _run(ctx, hint):
    gw = Gateway(S, MockBackend(S), Router(S))
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
