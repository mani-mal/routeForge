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
    assert r.json()["detail"].startswith("no backend could serve this request")
