import pytest
from pydantic import ValidationError

from app.models import ExecutionPolicyContext, ChatRequest


def test_context_is_frozen():
    ctx = ExecutionPolicyContext(
        request_id="r1", tenant_id="default", sensitivity="public",
        onprem_only=False, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=0.05, max_latency_ms=15000,
        risk_class="low", require_verification=False,
        detected_entities=(), redaction_map_id=None)
    with pytest.raises(ValidationError):
        ctx.onprem_only = True


def test_chat_request_allows_mock_hooks():
    req = ChatRequest.model_validate(
        {"messages": [{"role": "user", "content": "hi"}], "mock_confidence": 0.3})
    assert req.messages[0].content == "hi"
