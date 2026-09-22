import pytest

from tests.conftest import v1_error

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
    assert v1_error(r)["message"] == "policy evaluation unavailable"


def test_no_eligible_candidate_is_422(client, monkeypatch):
    from app.errors import NoEligibleCandidate
    from app.routing.router import Router
    def empty(self, *a, **k):
        raise NoEligibleCandidate("no candidates after residency filter")
    monkeypatch.setattr(Router, "select", empty)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 422
    assert "no model is available" in v1_error(r)["message"]


def test_backend_failure_is_502(client, monkeypatch):
    # Backend raises on every tier -> gateway exhausts -> BackendUnavailable.
    from app.gateway.backends import litellm_backend
    async def always_fail(**k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(litellm_backend, "acompletion", always_fail)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 502
    assert v1_error(r)["message"].startswith("no backend could serve this request")


def test_v1_errors_use_the_openai_envelope(client, monkeypatch) -> None:
    """Every /v1 failure body is {"error": {message, type, param, code}} (B49)."""
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer rf_live_nope"},
                    json={"model": "any", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    body = r.json()
    assert "detail" not in body
    err = body["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert err["code"] == "invalid_api_key"
    assert err["type"] == "invalid_request_error"
    assert err["param"] is None
    assert err["message"] == "invalid or missing API token"


def test_admin_errors_keep_the_detail_shape(client) -> None:
    """The console reads `detail`; /admin is deliberately not converted."""
    r = client.get("/admin/infra/models")
    assert r.status_code in (401, 403)
    assert "detail" in r.json() and "error" not in r.json()
