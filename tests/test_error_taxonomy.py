import pytest

_AUTH = {"Authorization": "Bearer demo-key"}
_MSG = {"messages": [{"role": "user", "content": "hello"}]}


def test_genuine_gate_failure_is_503(client, monkeypatch):
    # A detector/gate crash must stay the fail-closed 503 (CLAUDE.md rule 5).
    from app.policy.gate import Gate
    def boom(self, *a, **k):
        raise RuntimeError("presidio exploded")
    monkeypatch.setattr(Gate, "evaluate", boom)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 503
    assert r.json()["detail"] == "policy evaluation unavailable"


def test_no_eligible_candidate_is_422(client, monkeypatch):
    from app.errors import NoEligibleCandidate
    from app.routing.router import Router
    def empty(self, *a, **k):
        raise NoEligibleCandidate("no candidates after residency filter")
    monkeypatch.setattr(Router, "select", empty)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 422
    assert "no model is available" in r.json()["detail"]


def test_backend_failure_is_502(client, monkeypatch):
    # Backend raises on every tier -> gateway exhausts -> BackendUnavailable.
    from app.gateway.backends import litellm_backend
    async def always_fail(**k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(litellm_backend, "acompletion", always_fail)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 502
    assert r.json()["detail"] == "no backend could serve this request"


def test_agent_non_convergence_is_500(client, monkeypatch):
    # Multi-step path; agent runtime raises AgentCapExceeded (no nat needed --
    # decide_mode returns multi_step on mock_multistep, and we replace the
    # runtime's run() before it touches nat).
    import app.agent.runtime as runtime
    async def cap(*a, **k):
        raise runtime.AgentCapExceeded("did not converge")
    monkeypatch.setattr(runtime, "run", cap)
    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"mock_multistep": True, **_MSG})
    assert r.status_code == 500
    assert r.json()["detail"] == "agent did not converge within its step limit"
