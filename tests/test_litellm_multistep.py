from app.agent.internal_endpoint import HOPS
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response


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
