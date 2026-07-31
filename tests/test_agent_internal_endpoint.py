import json
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    return TestClient(create_app())

@pytest.fixture
def remote_client(monkeypatch):
    """A TestClient whose ASGI transport presents a real, non-loopback host —
    simulates an external/customer caller hitting the internal endpoint over
    the network (as opposed to the default TestClient host "testclient")."""
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    return TestClient(create_app(), client=("203.0.113.9", 12345))

def _ctx():
    from app.models import ExecutionPolicyContext
    return ExecutionPolicyContext(
        request_id="req-x", tenant_id="acme", sensitivity="internal",
        onprem_only=False, allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False, detected_entities=(),
        redaction_map_id=None)

def test_hop_routes_records_and_streams(client, monkeypatch):
    from app.agent.internal_endpoint import HOPS
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_response

    # Task 7: the endpoint now calls the REAL backend per hop, so a tool-call
    # SSE response requires the (fake, monkeypatched) completion to actually
    # return tool_calls — the old decide_agent_step content heuristic is gone.
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression":"12*7"}'}}]
    async def _call(**k):
        return make_response(content=None, tool_calls=tc)
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

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


def test_is_loopback_predicate():
    from app.agent.internal_endpoint import _is_loopback
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert _is_loopback("localhost")
    assert _is_loopback("testclient")  # Starlette TestClient's ASGI-transport host
    assert not _is_loopback("203.0.113.9")
    assert not _is_loopback(None)


def test_non_loopback_caller_rejected_403(remote_client):
    """A caller presenting a real, non-loopback host must be rejected — this
    endpoint is internal-only (I1): only the agent runtime's own self-calls
    (127.0.0.1) and the in-process TestClient (host "testclient") may reach
    it, per the loopback allowlist in app/agent/internal_endpoint.py."""
    from app.agent.internal_endpoint import HOPS
    run_id = "run-remote"
    HOPS.start(run_id, _ctx())
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "hi"}], "tools": []}
    r = remote_client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert r.status_code == 403
    HOPS.pop(run_id)  # clean up so it doesn't leak into other tests


def test_loopback_caller_still_allowed(client):
    """Regression guard: the default TestClient (host "testclient") — which
    stands in for the agent runtime's real 127.0.0.1 self-calls — must still
    be allowed through the loopback guard."""
    from app.agent.internal_endpoint import HOPS
    run_id = "run-allowed"
    HOPS.start(run_id, _ctx())
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "hi"}], "tools": []}
    r = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert r.status_code == 200
    HOPS.pop(run_id)


def test_unknown_run_id_returns_404_not_500(client):
    """I2: a run_id that was never HOPS.start()'d (or already popped) must
    return a clean 404, not a bare 500 from an unhandled KeyError."""
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "hi"}], "tools": []}
    r = client.post("/internal/v1/does-not-exist/chat/completions", json=body)
    assert r.status_code == 404


def test_unknown_run_id_from_remote_host_is_403_not_404():
    """Guard ordering: the loopback check must run before the run_id lookup,
    so a non-loopback caller gets 403 regardless of whether the run_id is
    valid — it must never learn anything about which run_ids exist."""
    from app.main import create_app
    remote = TestClient(create_app(), client=("203.0.113.9", 12345))
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "hi"}], "tools": []}
    r = remote.post("/internal/v1/does-not-exist/chat/completions", json=body)
    assert r.status_code == 403
