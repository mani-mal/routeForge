import json
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    return TestClient(create_app())

def _ctx():
    from app.models import ExecutionPolicyContext
    return ExecutionPolicyContext(
        request_id="req-x", tenant_id="acme", sensitivity="internal",
        onprem_only=False, allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False, detected_entities=(),
        redaction_map_id=None)

def test_hop_routes_records_and_streams(client):
    from app.agent.internal_endpoint import HOPS
    run_id = "run-abc"
    HOPS.start(run_id, _ctx())
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "What is 12 times 7?"}],
            "tools": [{"type": "function", "function": {"name": "calculator",
                       "parameters": {"properties": {"expression": {"type": "string"}}}}}]}
    r = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "data: [DONE]" in r.text
    # a tool_call was streamed (no tool result yet) and a hop was recorded
    assert "tool_calls" in r.text
    recs = HOPS.pop(run_id)
    assert len(recs) == 1 and recs[0].step == "agent_hop" and recs[0].chosen is not None

def test_onprem_only_hop_stays_onprem(client):
    from app.agent.internal_endpoint import HOPS
    from app.models import ExecutionPolicyContext
    run_id = "run-hosp"
    HOPS.start(run_id, ExecutionPolicyContext(
        request_id="r", tenant_id="hospital", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="high",
        require_verification=True, detected_entities=("US_SSN",), redaction_map_id=None))
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "diagnose please"}], "tools": []}
    client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    rec = HOPS.pop(run_id)[0]
    # chosen model must be an on-prem-tier model (provider onprem)
    from app.config.loader import load_settings
    pool = {c.name: c.provider for c in load_settings().routing.model_pool}
    assert pool[rec.chosen] == "onprem"
