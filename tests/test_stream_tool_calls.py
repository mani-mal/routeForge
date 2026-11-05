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


# --- backend-level: streamed fragments assemble to the non-streamed shape ----

def _backend():
    from app.config.loader import load_settings
    from app.gateway.backends.litellm_backend import LiteLLMBackend
    from tests._litellm_fake import make_test_registry
    settings = load_settings()
    return LiteLLMBackend(settings, registry=make_test_registry(settings))


async def _drain(backend, model="local-8b"):
    from app.config.schema import PriceCfg
    out = []
    async for c in backend.generate_stream(
            model, [], PriceCfg(input=0.1, output=0.2, tier="cheap", provider="onprem"), "cheap", {}, tools=_TOOLS):
        out.append(c)
    return out


def test_backend_assembles_streamed_tool_calls(monkeypatch):
    import asyncio
    from tests._litellm_fake import make_streaming_tool_acompletion
    items = [
        [{"index": 0, "id": "call-a", "type": "function",
          "function": {"name": "get_weather", "arguments": ""}}],
        [{"index": 1, "id": "call-b", "type": "function",
          "function": {"name": "get_time", "arguments": ""}}],
        [{"index": 0, "function": {"arguments": '{"city": '}}],
        [{"index": 1, "function": {"arguments": '{"tz": "UTC"}'}}],
        [{"index": 0, "function": {"arguments": '"Paris"}'}}],
    ]
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion(items))
    chunks = asyncio.run(_drain(_backend()))
    assert [c.tool_calls_delta is not None for c in chunks[:-1]] == [True] * 5
    final = chunks[-1].final
    assert final is not None and chunks[-1].delta is None
    assert final.finish_reason == "tool_calls"
    assert final.tool_calls == [
        {"id": "call-a", "type": "function",
         "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
        {"id": "call-b", "type": "function",
         "function": {"name": "get_time", "arguments": '{"tz": "UTC"}'}},
    ]


def test_text_only_stream_unchanged(monkeypatch):
    import asyncio
    from tests._litellm_fake import make_streaming_acompletion
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_acompletion(["he", "llo"]))
    chunks = asyncio.run(_drain(_backend()))
    assert [c.delta for c in chunks[:-1]] == ["he", "llo"]
    assert all(c.tool_calls_delta is None for c in chunks)
    assert chunks[-1].final.text == "hello"
    assert chunks[-1].final.tool_calls == []


# --- live tool-turn streaming (stream_unverified + client_output=raw) --------

import asyncio  # noqa: E402

from tests._litellm_fake import make_streaming_tool_acompletion  # noqa: E402

_SSN = "482-73-1946"


def _frag_first(idx, call_id, name):
    return [{"index": idx, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": ""}}]


def _frag_args(idx, args):
    return [{"index": idx, "function": {"arguments": args}}]


def _make_live_app(client, app_id: str, **policy) -> str:
    """A passthrough app (compiled client_output=raw) streaming unverified."""
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    body = {"stream_mode": "stream_unverified", "policy_mode": "passthrough", **policy}
    pr = client.put(f"/admin/applications/{app_id}/policy", json=body)
    assert pr.status_code == 200, pr.text
    return r.json()["token"]


def _post_stream(client, token, *, messages=None, include_usage=False):
    payload = {"model": "auto", "stream": True, "tools": _TOOLS,
               "messages": messages or [{"role": "user", "content": "weather in Paris?"}]}
    if include_usage:
        payload["stream_options"] = {"include_usage": True}
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {token}"}, json=payload)


def _streamed_calls(frames) -> list[dict]:
    """Merge streamed tool_call deltas by index the way an OpenAI client does."""
    merged: dict[int, dict] = {}
    for f in frames[:-1]:
        if not f.get("choices"):
            continue
        for tc in f["choices"][0]["delta"].get("tool_calls") or []:
            cur = merged.setdefault(tc["index"], {"id": None, "type": "function",
                                                  "function": {"name": None, "arguments": ""}})
            cur["id"] = cur["id"] or tc.get("id")
            fn = tc.get("function") or {}
            cur["function"]["name"] = cur["function"]["name"] or fn.get("name")
            cur["function"]["arguments"] += fn.get("arguments") or ""
    return [merged[i] for i in sorted(merged)]


def test_live_stream_tool_call_reassembles_to_nonstream(client, monkeypatch):
    _litellm_fake.CALLS.clear()
    token = _make_live_app(client, "live-reasm")
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion([
        _frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": '),
        _frag_args(0, '"Paris"}')]))
    r = _post_stream(client, token)
    assert r.status_code == 200, r.text
    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    _validate_chunks(frames)
    streamed = _streamed_calls(frames)
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"

    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content=None, tool_calls=[_TOOL_CALL])))
    nr = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                     json={"model": "auto", "tools": _TOOLS,
                           "messages": [{"role": "user", "content": "weather in Paris?"}]})
    assert nr.status_code == 200, nr.text
    assert streamed == nr.json()["choices"][0]["message"]["tool_calls"]


def test_live_stream_first_frame_arrives_before_completion(client, monkeypatch):
    """Drive the ASGI app directly (Starlette's TestClient buffers the whole
    body) and order the events: the first content frame must be sent before
    the backend finishes."""
    token = _make_live_app(client, "live-order")
    events: list[str] = []
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion([
        "Checking. ", lambda: events.append("backend-paused"), 0.2,
        _frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Paris"}'),
        lambda: events.append("backend-done")]))
    body = json.dumps({"model": "auto", "stream": True, "tools": _TOOLS,
                       "messages": [{"role": "user", "content": "weather in Paris?"}]}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/v1/chat/completions",
             "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
             "headers": [(b"authorization", f"Bearer {token}".encode()),
                         (b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())],
             "client": ("testclient", 1), "server": ("testserver", 80)}

    async def drive():
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)

        async def send(msg):
            if msg["type"] == "http.response.body" and b"Checking." in msg.get("body", b""):
                events.append("first-content-frame")

        await client.app(scope, receive, send)

    asyncio.run(drive())
    assert "first-content-frame" in events and "backend-done" in events, events
    assert events.index("first-content-frame") < events.index("backend-done"), events


def test_live_stream_sends_structured_messages_and_tools_to_backend(client, monkeypatch):
    _litellm_fake.CALLS.clear()
    token = _make_live_app(client, "live-struct")
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_tool_acompletion(["It is sunny."]))
    r = _post_stream(client, token, messages=[
        {"role": "user", "content": "weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [_TOOL_CALL]},
        {"role": "tool", "tool_call_id": "call-1", "content": "sunny, 21C"}])
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert call["stream"] is True
    assert [m["role"] for m in call["messages"]] == ["user", "assistant", "tool"]
    assert call["messages"][1]["tool_calls"] == [_TOOL_CALL]
    assert call["messages"][2]["tool_call_id"] == "call-1"
    assert call["tools"] == _TOOLS


def test_parallel_tool_calls_stream_live(client, monkeypatch):
    token = _make_live_app(client, "live-par")
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion([
        _frag_first(0, "call-a", "get_weather"), _frag_first(1, "call-b", "get_weather"),
        _frag_args(0, '{"city": "Paris"}'), _frag_args(1, '{"city": "Rome"}')]))
    frames = _parse_sse(_post_stream(client, token).text)
    _validate_chunks(frames)
    calls = _streamed_calls(frames)
    assert [c["id"] for c in calls] == ["call-a", "call-b"]
    assert [json.loads(c["function"]["arguments"])["city"] for c in calls] == ["Paris", "Rome"]
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"


def test_text_then_tool_call_streams_both(client, monkeypatch):
    token = _make_live_app(client, "live-both")
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion([
        "Let me ", "check. ", _frag_first(0, "call-1", "get_weather"),
        _frag_args(0, '{"city": "Paris"}')]))
    frames = _parse_sse(_post_stream(client, token).text)
    _validate_chunks(frames)
    text = "".join(f["choices"][0]["delta"].get("content") or "" for f in frames[:-1])
    assert text == "Let me check. "
    assert len(_streamed_calls(frames)) == 1
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"


def test_redact_app_tool_stream_stays_buffered_and_redacted(client, monkeypatch):
    """client_output=redact (the enforced default) never takes the live path:
    the complete answer is verified, so an SSN in model-generated tool-call
    arguments is redacted (PR #49) before it reaches the SSE."""
    _litellm_fake.CALLS.clear()
    _login(client)
    r = client.post("/admin/applications", json={"id": "live-redact", "name": "r"})
    token = r.json()["token"]
    assert client.put("/admin/applications/live-redact/policy",
                      json={"stream_mode": "incremental_verify",
                            "pii_detection": "regex"}).status_code == 200
    args = json.dumps({"city": _SSN})
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "get_weather", "arguments": args}}])))
    resp = _post_stream(client, token)
    assert resp.status_code == 200, resp.text
    assert _SSN not in resp.text
    assert not _litellm_fake.CALLS[-1].get("stream"), "buffered path must not stream upstream"


def test_onprem_only_passthrough_tool_stream_stays_buffered(client, monkeypatch):
    """An SSN in the conversation escalates placement (onprem_only +
    require_verification) on a passthrough app that names a detector. The
    step-9 floor is absolute: the turn is served complete, never live."""
    _litellm_fake.CALLS.clear()
    token = _make_live_app(client, "live-onprem", pii_detection="regex")
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL])))
    resp = _post_stream(client, token, messages=[
        {"role": "user", "content": f"weather for the customer with ssn {_SSN}?"}])
    assert resp.status_code == 200, resp.text
    frames = _parse_sse(resp.text)
    assert _streamed_calls(frames)[0]["function"] == _TOOL_CALL["function"]
    assert not _litellm_fake.CALLS[-1].get("stream"), "floored request must not stream live"


def test_verification_required_stays_buffered(monkeypatch):
    """Isolated require_verification floor (onprem_only False): a tool
    conversation from an otherwise live-eligible app falls back."""
    from app.pipeline import StreamFallback
    from app.apps.policy import AppPolicyView
    from tests.test_run_stream_pipeline import (
        _pipeline, _settings_require_verify_without_onprem)
    from tests._litellm_fake import make_streaming_acompletion
    from app.models import ChatRequest, Message

    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(["x"]))
    pipe = _pipeline(_settings_require_verify_without_onprem())

    async def _run():
        req = ChatRequest(messages=[Message(role="user", content=f"ssn {_SSN}")],
                          tools=_TOOLS)
        await pipe.run_stream(
            "req-1", "demo", req,
            app_policy=AppPolicyView(residency="any", stream_mode="stream_unverified",
                                     policy_mode="passthrough", pii_detection="regex"))

    with pytest.raises(StreamFallback):
        asyncio.run(_run())


def test_usage_frame_after_live_tool_stream(client, monkeypatch):
    token = _make_live_app(client, "live-usage")
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion([
        _frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Paris"}')],
        prompt_tokens=30, completion_tokens=9))
    frames = _parse_sse(_post_stream(client, token, include_usage=True).text)
    assert frames[-1] == "[DONE]"
    assert frames[-3]["choices"][0]["finish_reason"] == "tool_calls"
    assert frames[-2]["choices"] == []
    assert frames[-2]["usage"]["prompt_tokens"] == 30
    assert frames[-2]["usage"]["completion_tokens"] == 9


def test_outcome_row_records_tool_call_reply(client, monkeypatch):
    token = _make_live_app(client, "live-outcome")
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion([
        _frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Paris"}')]))
    assert _post_stream(client, token).status_code == 200
    _login(client)
    r = client.get("/admin/outcomes/export", params={"application_id": "live-outcome"})
    assert r.status_code == 200, r.text
    rows = [json.loads(line)["outcome"] for line in r.text.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["reply_tool_calls"] == 1
    assert rows[0]["finish_reason"] == "tool_calls"
    assert "Paris" not in r.text
