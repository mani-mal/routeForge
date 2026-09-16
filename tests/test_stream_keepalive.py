"""Contract: a streamed agent turn that has to wait for a complete, verified
answer keeps the connection alive. A client transport timeout that only
covers "first response bytes" must not fire while the model is still working,
and a failure after the stream opened still reaches the client as an error."""
import asyncio
import json

import pytest

from tests.onboarding.harness import conformance_client, onboard

_TURN = {"stream": True, "messages": [{"role": "user", "content": "summarise the repo"}],
         "tools": [{"type": "function", "function": {
             "name": "read_file", "description": "read a file",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]}


def _fast_keepalive(c, seconds=0.05):
    s = c.app.state.settings
    c.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"stream_keepalive_s": seconds})})


def _slow_run(monkeypatch, c, *, delay=0.3, result=None, exc=None):
    real = c.app.state.pipeline.run

    async def slow(*a, **k):
        await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return await real(*a, **k)

    monkeypatch.setattr(c.app.state.pipeline, "run", slow)


def _events(text):
    return [block for block in text.split("\n\n") if block]


def test_slow_streamed_turn_sends_keepalives_before_the_answer(monkeypatch):
    with conformance_client() as c:
        app = onboard(c, "ka-slow", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.3)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = "".join(r.iter_text())
    events = _events(body)
    assert events[0] == ": keepalive"
    data = [e for e in events if e.startswith("data: ")]
    assert data[-1] == "data: [DONE]"
    chunks = [json.loads(e[6:]) for e in data[:-1]]
    openai = pytest.importorskip("openai.types.chat")
    for ch in chunks:
        openai.ChatCompletionChunk.model_validate(ch)


def test_fast_streamed_turn_has_no_keepalive(monkeypatch):
    with conformance_client() as c:
        app = onboard(c, "ka-fast", stream_mode="verified_then_stream")
        _fast_keepalive(c, seconds=5.0)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            body = "".join(r.iter_text())
    assert ": keepalive" not in body
    assert body.rstrip().endswith("data: [DONE]")


def test_fast_policy_rejection_keeps_its_http_status():
    with conformance_client() as c:
        app = onboard(c, "ka-reject", stream_mode="verified_then_stream",
                      allowed_models=["no-such-model"])
        r = c.post("/v1/chat/completions", headers=app.headers, json=_TURN)
    assert r.status_code == 422
    assert "allowed_models" in r.json()["detail"]


def test_late_failure_after_stream_opened_is_an_sse_error_frame(monkeypatch):
    from app.errors import BackendUnavailable
    with conformance_client() as c:
        app = onboard(c, "ka-late-error", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.3,
                  exc=BackendUnavailable("backend failed on all reachable candidates"))
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
    data = [e for e in _events(body) if e.startswith("data: ")]
    err = json.loads(data[-2][6:])
    assert data[-1] == "data: [DONE]"
    assert err["error"]["code"] == 502
    assert err["error"]["message"].startswith("no backend could serve this request")


def test_openai_sdk_raises_on_the_late_error_frame(monkeypatch):
    openai = pytest.importorskip("openai")
    from app.errors import BackendUnavailable
    with conformance_client() as c:
        app = onboard(c, "ka-sdk-error", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.2, exc=BackendUnavailable("boom"))
        client = openai.OpenAI(base_url="http://testserver/v1", api_key="x",
                               http_client=c, max_retries=0)
        client._client = c  # route SDK traffic through the TestClient transport
        with pytest.raises(openai.APIError):
            for _ in client.chat.completions.create(
                    model="auto", stream=True, messages=_TURN["messages"],
                    extra_headers=app.headers):
                pass


def test_late_agent_cap_exceeded_is_rejected_not_error(monkeypatch):
    """AgentCapExceeded maps to (500, ...) but is still a KNOWN/mapped serving
    exception -- the metrics label must follow whether _error_status_detail
    matched, not the numeric status, so it is recorded "rejected" here and
    "error" is reserved for a genuinely unmapped exception."""
    from app.agent.runtime import AgentCapExceeded
    from app.obs import metrics
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics, "observe_request",
                        lambda model, status: calls.append((model, status)))
    with conformance_client() as c:
        app = onboard(c, "ka-agent-cap", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.3, exc=AgentCapExceeded())
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=_TURN) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
    data = [e for e in _events(body) if e.startswith("data: ")]
    err = json.loads(data[-2][6:])
    assert data[-1] == "data: [DONE]"
    assert err["error"]["code"] == 500
    assert err["error"]["message"] == "agent did not converge within its step limit"
    assert ("-", "rejected") in calls


def test_config_default_and_older_serving_yaml(tmp_path):
    from app.config.loader import load_settings
    # Shipped config carries the key and loads 5.0.
    assert load_settings().serving.stream_keepalive_s == 5.0

    # Upgrade safety: a mounted serving.yaml that predates this feature (DGX
    # ConfigMap, Helm/HPE serving-config) has no `stream_keepalive_s` key at
    # all -- the .get()-with-default in the loader must still boot it at the
    # same default, not KeyError. Same pattern as
    # test_config_loader.py::test_boot_survives_serving_yaml_without_policy_mode_keys.
    import pathlib

    import yaml

    raw = yaml.safe_load(pathlib.Path("config/serving.yaml").read_text())
    raw["server"].pop("stream_keepalive_s", None)
    (tmp_path / "serving.yaml").write_text(yaml.safe_dump(raw))
    for f in ("policy.yaml", "routing.yaml", "pricing.yaml"):
        (tmp_path / f).write_text(pathlib.Path("config", f).read_text())
    s = load_settings(config_dir=str(tmp_path))  # must not raise
    assert s.serving.stream_keepalive_s == 5.0
