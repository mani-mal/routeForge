from app.compression import compress
from app.models import ChatRequest, ExecutionPolicyContext, Message
from app.routing.task_mode import decide

CTX = ExecutionPolicyContext(
    request_id="r", tenant_id="default", sensitivity="public", onprem_only=False,
    allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
    max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
    require_verification=False, detected_entities=(), redaction_map_id=None)


def test_compression_is_passthrough():
    msgs = [Message(role="user", content="a" * 400)]
    res = compress(msgs, CTX)
    assert res.tokens_after == res.tokens_before
    assert res.record["applied"] is False
    assert res.record["ccr_retrievals_to_cloud"] == 0


def test_task_mode_is_direct():
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    assert decide(CTX, req) == "direct"
