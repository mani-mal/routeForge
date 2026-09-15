"""Streaming a tool-calling conversation (`stream: true` + `tools`).

Agentic OpenAI clients (Qwen Code, the `openai` SDK's streaming helpers,
LangChain's ChatOpenAI) send `stream: true` on EVERY turn, including turns
where the model is expected to call a tool. Before this fix every streaming
mode lost the tool structure:

- `verified_then_stream` chunked only `message.content`, so a `tool_calls`
  answer streamed as an empty message with finish_reason "stop";
- `stream_unverified` / `incremental_verify` took the live token path, which
  never forwards `tools` and flattens the conversation into one prompt.

A tool conversation is now served by the full `pipeline.run` (the path that
already threads tools and preserves per-message structure) and then emitted
as SSE carrying the `tool_calls` delta and finish_reason "tool_calls".

Assertions run against REAL SSE bytes and parse every frame with the real
`openai` SDK chunk model -- the same validation a client runs.
"""
from __future__ import annotations

import json

import pytest

from app.gateway.backends import litellm_backend
from tests import _litellm_fake
from tests._litellm_fake import make_recording_acompletion, make_response
from tests.test_admin_api import _login

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}]
_TOOL_CALL = {"id": "call-1", "type": "function",
              "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}

_STREAM_MODES = ["verified_then_stream", "stream_unverified", "incremental_verify"]


def _make_app(client, app_id: str, *, stream_mode: str) -> str:
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"stream_mode": stream_mode})
    assert pr.status_code == 200, pr.text
    return token


def _parse_sse(text: str) -> list[dict | str]:
    frames: list[dict | str] = []
    for block in text.strip("\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: "), block
        payload = block[len("data: "):]
        frames.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return frames


def _validate_chunks(frames: list) -> None:
    """Every data frame must parse as an OpenAI `ChatCompletionChunk`."""
    Chunk = pytest.importorskip("openai.types.chat").ChatCompletionChunk
    for f in frames[:-1]:
        Chunk.model_validate(f)


@pytest.mark.parametrize("stream_mode", _STREAM_MODES)
def test_streamed_tool_call_reaches_client(client, monkeypatch, stream_mode):
    _litellm_fake.CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=None, tool_calls=[_TOOL_CALL])))
    token = _make_app(client, f"stc-{stream_mode}", stream_mode=stream_mode)

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "tools": _TOOLS,
                          "tool_choice": "auto",
                          "messages": [{"role": "user", "content": "weather in Paris?"}]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    _validate_chunks(frames)

    deltas = [f["choices"][0]["delta"] for f in frames[:-1]]
    streamed_calls = [tc for d in deltas for tc in d.get("tool_calls") or []]
    assert len(streamed_calls) == 1, deltas
    call = streamed_calls[0]
    assert call["index"] == 0
    assert call["id"] == "call-1"
    assert call["type"] == "function"
    assert call["function"] == _TOOL_CALL["function"]
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert not "".join(d.get("content") or "" for d in deltas)

    # The backend really received the tools (not dropped on the stream path).
    assert _litellm_fake.CALLS, "backend was never called"
    assert _litellm_fake.CALLS[-1]["tools"] == _TOOLS
    assert _litellm_fake.CALLS[-1]["tool_choice"] == "auto"

    # Still metered like any other request.
    _login(client)
    usage = client.get(f"/admin/usage?application_id=stc-{stream_mode}").json()
    assert usage["count"] == 1


@pytest.mark.parametrize("stream_mode", _STREAM_MODES)
def test_streamed_tool_result_turn_keeps_structure(client, monkeypatch, stream_mode):
    """The turn AFTER a tool ran: the replayed assistant tool_calls message and
    the role:"tool" result must reach the backend as structured messages, and
    the final text answer streams back with finish_reason "stop"."""
    _litellm_fake.CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="It is sunny in Paris.")))
    token = _make_app(client, f"stc-rt-{stream_mode}", stream_mode=stream_mode)

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "tools": _TOOLS,
                          "messages": [
                              {"role": "user", "content": "weather in Paris?"},
                              {"role": "assistant", "content": None,
                               "tool_calls": [_TOOL_CALL]},
                              {"role": "tool", "tool_call_id": "call-1",
                               "content": "sunny, 21C"},
                          ]})
    assert r.status_code == 200, r.text
    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    _validate_chunks(frames)
    text = "".join(f["choices"][0]["delta"].get("content") or "" for f in frames[:-1])
    assert text == "It is sunny in Paris."
    assert frames[-2]["choices"][0]["finish_reason"] == "stop"

    wire = _litellm_fake.CALLS[-1]["messages"]
    assert [m["role"] for m in wire] == ["user", "assistant", "tool"]
    assert wire[-1]["tool_call_id"] == "call-1"
