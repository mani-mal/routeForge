from tests import _litellm_fake
from tests.onboarding.harness import conformance_client, install_dual_fake, onboard

_AUTH = {"Authorization": "Bearer demo-key"}


def test_generation_params_reach_the_model(client):
    _litellm_fake.CALLS.clear()
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 77, "temperature": 0.2, "top_p": 0.9, "stop": ["END"], "seed": 7})
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert call["max_tokens"] == 77
    assert call["temperature"] == 0.2
    assert call["top_p"] == 0.9
    assert call["stop"] == ["END"]
    assert call["seed"] == 7


def test_unknown_or_structural_extras_are_not_forwarded(client):
    _litellm_fake.CALLS.clear()
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "hello"}], "n": 3, "foo": "bar"})
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert "n" not in call and "foo" not in call
    assert call["messages"][-1]["role"] == "user"


def test_generation_params_reach_the_model_when_streaming(_fake_acompletion, monkeypatch):
    install_dual_fake(monkeypatch)
    _litellm_fake.CALLS.clear()
    with conformance_client() as c:
        app = onboard(c, "params-stream", stream_mode="stream_unverified")
        r = app.chat({"stream": True, "max_tokens": 55, "temperature": 0.1,
                      "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert call["stream"] is True and call["max_tokens"] == 55 and call["temperature"] == 0.1
