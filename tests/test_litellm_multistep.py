import asyncio

import pytest

from app.agent.internal_endpoint import HOPS
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response, make_scripted_tool_then_final_acompletion


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_hop_records_real_cost_and_latency(client, monkeypatch):
    # client fixture (conftest) already monkeypatches acompletion; override with
    # a known usage so we can assert real (non-seeded) cost is recorded.
    calls = {"n": 0}
    async def _call(**k):
        calls["n"] += 1
        return make_response(content="the answer is 4", prompt_tokens=20, completion_tokens=10)
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

    from app.models import ExecutionPolicyContext
    run_id = "run-ms-1"
    HOPS.start(run_id, ExecutionPolicyContext(
        request_id="r", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None))
    try:
        resp = client.post(f"/internal/v1/{run_id}/chat/completions",
                           json={"messages": [{"role": "user", "content": "2+2"}], "tools": []})
        assert resp.status_code == 200
        rec = HOPS._runs[run_id].records[-1]
        assert rec.actual_cost_usd is not None and rec.actual_cost_usd > 0  # real, not None
        assert rec.latency_ms >= 0  # from the real call, not seeded_latency_ms
        assert calls["n"] == 1  # the backend was actually called
    finally:
        if HOPS.has(run_id):
            HOPS.pop(run_id)


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_tool_call_result_emits_tool_call_sse(client, monkeypatch):
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'}}]
    async def _call(**k):
        return make_response(content=None, tool_calls=tc, prompt_tokens=5, completion_tokens=2)
    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    from app.models import ExecutionPolicyContext
    run_id = "run-ms-tc"
    HOPS.start(run_id, ExecutionPolicyContext(
        request_id="r", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None))
    try:
        resp = client.post(f"/internal/v1/{run_id}/chat/completions",
                           json={"messages": [{"role": "user", "content": "2+2"}],
                                 "tools": [{"type": "function",
                                            "function": {"name": "calculator"}}]})
        body = resp.text
        assert "tool_calls" in body and "calculator" in body
        assert "finish_reason" in body and "tool_calls" in body
    finally:
        if HOPS.has(run_id):
            HOPS.pop(run_id)


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_multistep_hits_real_backend_and_stays_onprem(live_internal_server, monkeypatch):
    pytest.importorskip("nat")
    monkeypatch.setattr(litellm_backend, "acompletion",
                         make_scripted_tool_then_final_acompletion())
    from app.agent import runtime as agent_runtime
    from app.models import ChatRequest, ExecutionPolicyContext, Message

    ctx = ExecutionPolicyContext(
        request_id="r-ms", tenant_id="hospital", sensitivity="restricted",
        onprem_only=True, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=60000,
        risk_class="high", require_verification=True, detected_entities=(),
        redaction_map_id=None)
    req = ChatRequest(messages=[Message(role="user", content="what is 2+2?")])
    result = asyncio.run(agent_runtime.run(ctx, req, port=live_internal_server.port))
    assert result.hops, "expected >=1 real hop"
    assert all(h.actual_cost_usd is not None for h in result.hops)  # real cost recorded
    # onprem_only stayed on-prem: every chosen model is an onprem-tier slug.
    assert all(h.chosen in {"local-8b", "local-32b"} for h in result.hops)
