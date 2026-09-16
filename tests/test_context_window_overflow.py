"""Contract: a request that is too large for the model's context window is the
CLIENT's error, not an outage.

An agent client whose conversation outgrows the backend's context window gets
an upstream 400 ("This model's maximum context length is ..."). RouteForge
must return that as a 400 carrying the upstream reason -- never a 502, which
reads as "server down", makes clients retry the identical oversized request,
and hides the one action that fixes it (compact the history / lower
max_tokens). A genuine backend outage still returns 502."""
import asyncio
import json

import pytest
from litellm import ContextWindowExceededError

from app.errors import BackendUnavailable, ContextWindowExceeded
from app.gateway.gateway import Gateway
from app.models import Message
from app.routing.router import Router
from tests.onboarding.harness import conformance_client, onboard
from tests.test_gateway_lateral_failover import S, _ctx, _DeterministicLiteLLM, _onprem, _reg

_VLLM_MESSAGE = (
    "This model's maximum context length is 262144 tokens. However, you requested "
    "16000 output tokens and your prompt contains at least 251905 input tokens, for "
    "a total of at least 267905 tokens. Please reduce the length of the input prompt "
    "or the number of requested output tokens. (parameter=input_tokens, value=251905)")


async def _overflow(**kwargs):
    raise ContextWindowExceededError(
        message=f"OpenAIException - {_VLLM_MESSAGE}", model=kwargs.get("model", "m"),
        llm_provider="openai")


def test_context_overflow_is_a_400_with_the_upstream_reason(client, monkeypatch):
    from app.gateway.backends import litellm_backend
    monkeypatch.setattr(litellm_backend, "acompletion", _overflow)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}],
                          "max_tokens": 16000})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail.startswith("request exceeds the model's context window")
    assert "maximum context length is 262144 tokens" in detail
    assert "251905 input tokens" in detail
    assert "max_tokens" in detail


def test_backend_outage_is_still_a_502(client, monkeypatch):
    from app.gateway.backends import litellm_backend

    async def refuse(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(litellm_backend, "acompletion", refuse)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 502


def test_streamed_agent_turn_reports_overflow_as_400_error_frame(monkeypatch):
    from app.gateway.backends import litellm_backend

    async def slow_overflow(**kwargs):
        await asyncio.sleep(0.3)
        await _overflow(**kwargs)

    turn = {"stream": True, "max_tokens": 16000,
            "messages": [{"role": "user", "content": "summarise the repo"}],
            "tools": [{"type": "function", "function": {
                "name": "read_file", "description": "read a file",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}}}}}]}
    with conformance_client() as c:
        app = onboard(c, "ctx-overflow-stream", stream_mode="verified_then_stream")
        s = c.app.state.settings
        c.app.state.settings = s.model_copy(update={
            "serving": s.serving.model_copy(update={"stream_keepalive_s": 0.05})})
        monkeypatch.setattr(litellm_backend, "acompletion", slow_overflow)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers, json=turn) as r:
            body = "".join(r.iter_text())
    data = [e for e in body.split("\n\n") if e.startswith("data: ")]
    assert data[-1] == "data: [DONE]"
    err = json.loads(data[-2][6:])
    assert err["error"]["code"] == 400
    assert err["error"]["message"].startswith("request exceeds the model's context window")


# --- gateway: failover still runs; only an all-overflow exhaustion is a 400 ---

class _Scripted(_DeterministicLiteLLM):
    def __init__(self, settings, errors: dict[str, Exception]) -> None:
        super().__init__(settings)
        self._errors = errors
        self.calls: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(model)
        if model in self._errors:
            raise self._errors[model]
        return await super().generate(model, messages, price, tier, {})


def _gw_run(backend, reg):
    gw = Gateway(S, backend, Router(S, registry=reg), reg)
    return asyncio.run(gw.run(_ctx({"onprem"}, onprem=True),
                              [Message(role="user", content="hello")], 20, 0.1, {}))


def test_overflow_on_one_model_still_fails_over_to_a_peer():
    reg = _reg({"small-ctx": _onprem("small-ctx"), "big-ctx": _onprem("big-ctx")})
    backend = _Scripted(S, {"small-ctx": ContextWindowExceeded("small-ctx: too long")})
    result, records = _gw_run(backend, reg)
    assert next(r for r in records if r.step == "router").chosen == "big-ctx"


def test_every_model_overflowing_raises_context_window_exceeded():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    backend = _Scripted(S, {"a": ContextWindowExceeded("a: too long"),
                            "b": ContextWindowExceeded("b: too long")})
    with pytest.raises(ContextWindowExceeded):
        _gw_run(backend, reg)


def test_overflow_mixed_with_an_outage_stays_backend_unavailable():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    backend = _Scripted(S, {"a": ContextWindowExceeded("a: too long"),
                            "b": RuntimeError("connection refused")})
    with pytest.raises(BackendUnavailable) as info:
        _gw_run(backend, reg)
    assert not isinstance(info.value, ContextWindowExceeded)
