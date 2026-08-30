"""OpenAI wire-compatibility contract for POST /v1/chat/completions.

These are the regression tests that were missing: nothing asserted the
response envelope, so a response with no `object`/`created` shipped and every
OpenAI-client turn (any client app, LangChain ChatOpenAI, the `openai` SDK) died
before it reached a tool. Likewise nothing exercised a tool-calling round-trip,
so the direct path silently dropped `tools` at the gateway and flattened the
conversation.

The strongest guard here is `_validate_openai`: it parses our response with the
REAL `openai` SDK model, exactly as a client does — so any future drift from
the OpenAI shape fails a test instead of a customer's agent.
"""
import pytest

from tests._litellm_fake import (
    CALLS, make_recording_acompletion, make_response,
)
from app.gateway.backends import litellm_backend

_AUTH = {"Authorization": "Bearer demo-key"}

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]


def _validate_openai(body: dict) -> None:
    """Parse `body` with the actual openai SDK ChatCompletion model — the same
    validation a real OpenAI client runs. Skips cleanly if openai isn't installed."""
    ChatCompletion = pytest.importorskip("openai.types.chat").ChatCompletion
    ChatCompletion.model_validate(body)  # raises on any shape violation


def test_response_has_openai_envelope(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    assert r.status_code == 200
    body = r.json()
    # The exact two fields that were missing and broke every turn.
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int) and body["created"] > 0
    # Core OpenAI fields still present and well-formed.
    assert body["id"]
    assert body["model"]
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["finish_reason"] == "stop"
    assert {"prompt_tokens", "completion_tokens", "total_tokens"} <= body["usage"].keys()
    # And it parses with the real client model — the definitive regression.
    _validate_openai(body)


def test_tools_are_threaded_and_tool_calls_returned(client, monkeypatch):
    """A request with `tools` must (a) actually reach the backend and (b) come
    back as an OpenAI tool_calls response (content null, finish_reason
    'tool_calls'). Before the fix, gateway.run dropped `tools` entirely."""
    CALLS.clear()
    tool_call = {"id": "call-1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=None, tool_calls=[tool_call])))

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "weather in Paris?"}],
        "tools": _TOOLS,
        "tool_choice": "auto",
    })
    assert r.status_code == 200
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    _validate_openai(body)

    # The backend actually received the tools and tool_choice (not dropped).
    assert CALLS, "backend was never called"
    last = CALLS[-1]
    assert last["tools"] == _TOOLS
    assert last["tool_choice"] == "auto"


def test_tool_result_roundtrip_parses_and_forwards_structure(client, monkeypatch):
    """The SECOND turn of a tool loop: the client replays an assistant message
    with content:null + tool_calls, plus a role:'tool' result. This must NOT
    422 on request parsing, and the structured messages (roles + tool_call_id)
    must reach the backend rather than being flattened into one blob."""
    CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="It is sunny in Paris.")))

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "weather in Paris?"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "get_weather",
                                          "arguments": '{"city": "Paris"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "sunny, 21C"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "It is sunny in Paris."
    _validate_openai(body)

    # Structure was preserved end-to-end: the tool result reached the backend
    # as its own role:'tool' message correlated by tool_call_id.
    wire = CALLS[-1]["messages"]
    roles = [m["role"] for m in wire]
    assert roles == ["user", "assistant", "tool"], roles
    tool_msg = wire[-1]
    assert tool_msg["tool_call_id"] == "call-1"
    assert tool_msg["content"] == "sunny, 21C"
    # The assistant turn kept its tool_calls.
    assert wire[1]["tool_calls"][0]["id"] == "call-1"


def test_plain_multiturn_without_tools_still_serves(client):
    """Regression guard for the 100+ non-tool requests: a plain multi-message
    request (no tools) still serves and still carries the OpenAI envelope."""
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "capital of France?"},
        ]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int) and body["created"] > 0
    _validate_openai(body)


def test_tools_request_is_redacted_before_egress(client, monkeypatch):
    """CLAUDE.md rule 1 on the tool path: each message is per-message redacted
    before it reaches the backend — a PII value in a tool result must not be
    forwarded raw."""
    CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="ok")))

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "look up this account"},
            {"role": "tool", "tool_call_id": "c1",
             "content": "customer SSN 482-73-1946 on file"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200
    wire_text = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
    assert "482-73-1946" not in wire_text  # redacted before egress
