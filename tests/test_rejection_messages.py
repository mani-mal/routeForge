from tests.onboarding.clients import qwen_code_turn
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, onboard,
)


def _detail(r):
    return r.json()["detail"]


def test_onprem_only_with_no_onprem_model_names_residency_and_onboarding():
    with conformance_client() as c:
        disable_seeded_models(c)
        app = onboard(c, "msg-res", residency="onprem_only")
        r = app.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 422
    assert "no candidates after residency filter" in _detail(r)
    assert "residency" in _detail(r) and "onboard" in _detail(r)


def test_scope_miss_names_the_allowlist():
    with conformance_client() as c:
        app = onboard(c, "msg-scope", allowed_models=["no-such-model"])
        r = app.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 422 and "allowed_models" in _detail(r)


def test_budget_rejection_shows_estimate_cap_and_field():
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "msg-budget")
        r = app.chat({**qwen_code_turn(), "stream": False})
    d = _detail(r)
    assert r.status_code == 422 and "no candidates after budget filter" in d
    assert "max_cost_usd_per_request" in d and "conf-model" in d and "$0.0500" in d


def test_pinned_model_rejection_names_the_gate():
    with conformance_client() as c:
        app = onboard(c, "msg-pin", allowed_tiers=["cheap"])
        r = app.chat({"model": "local-32b", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 422
    assert _detail(r).startswith("model request rejected: requested model 'local-32b'")
    assert "allowed_tiers" in _detail(r)


def test_502_carries_the_upstream_error(client, monkeypatch):
    from app.gateway.backends import litellm_backend

    async def refuse(**k):
        raise RuntimeError("connection refused by 10.0.0.5:8000")

    monkeypatch.setattr(litellm_backend, "acompletion", refuse)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 502
    assert _detail(r).startswith("no backend could serve this request")
    assert "connection refused by 10.0.0.5:8000" in _detail(r)


def test_stream_rejection_names_stream_mode():
    with conformance_client() as c:
        app = onboard(c, "msg-stream")
        r = app.chat({"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "stream_mode" in _detail(r)
