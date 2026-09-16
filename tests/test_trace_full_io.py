"""`capture_io='full'` must store the WHOLE exchange in the app's own Langfuse
project, including tool calling.

Before this fix the full trace kept only the LAST message's text as input and
`message.content` as output. For an agentic client that loses almost
everything: the tools offered, earlier turns, the `role: "tool"` results, and
-- on a tool-call turn -- the assistant's `tool_calls` entirely (content is
null, so output was blank).

Now, for `full` only:
- input.value  = JSON `{"messages": [...], "tools": [...], "tool_choice": ...}`
  whenever the request has more than one message or offers tools (a single
  plain message stays plain text, as before);
- output.value = JSON assistant message with `tool_calls` when the model
  called tools (plain text otherwise, as before).
`redacted` / `off` are unchanged: raw tool arguments never leave for them.
"""
from __future__ import annotations

import json

import pytest

from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_recording_acompletion, make_response
from tests.test_admin_api import _login

pytest.importorskip("opentelemetry.sdk.trace")
from tests.test_raw_completion_capture import _wire_app_tracer  # noqa: E402

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


def _make_full_app(client, app_id: str, *, stream_mode: str | None = None) -> str:
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    lf = client.put(f"/admin/applications/{app_id}/langfuse",
                    json={"host": "http://lf.test:3000", "public_key": "pub",
                          "secret_key": "sek", "project_id": f"proj-{app_id}"})
    assert lf.status_code == 200, lf.text
    policy = {"capture_io": "full"}
    if stream_mode:
        policy["stream_mode"] = stream_mode
    pr = client.put(f"/admin/applications/{app_id}/policy", json=policy)
    assert pr.status_code == 200, pr.text
    return token


def _root_attrs(mem, provider, request_id: str) -> dict:
    provider.force_flush()
    for span in mem.get_finished_spans():
        if span.name == "serving.request" and \
                span.attributes.get("sp.request_id") == request_id:
            return dict(span.attributes)
    raise AssertionError(f"no serving.request span for {request_id!r}")


def test_full_trace_stores_tool_call_turn(client, monkeypatch):
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=None, tool_calls=[_TOOL_CALL])))
    mem, provider = _wire_app_tracer(monkeypatch)
    token = _make_full_app(client, "full-tools")

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "tools": _TOOLS, "tool_choice": "auto",
                          "messages": [{"role": "system", "content": "You are a helper."},
                                       {"role": "user", "content": "weather in Paris?"}]})
    assert r.status_code == 200, r.text
    attrs = _root_attrs(mem, provider, r.json()["id"])

    sent = json.loads(attrs["input.value"])
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["messages"][1]["content"] == "weather in Paris?"
    assert sent["tools"] == _TOOLS
    assert sent["tool_choice"] == "auto"

    answer = json.loads(attrs["output.value"])
    assert answer["role"] == "assistant"
    assert answer["tool_calls"][0]["id"] == "call-1"
    assert answer["tool_calls"][0]["function"] == _TOOL_CALL["function"]


def test_full_trace_stores_raw_tool_call_arguments_despite_egress_redaction(
        client, monkeypatch):
    """Task 2: `capture_io: full` must still see the RAW model-generated
    tool_calls even though the client-facing response now has them
    egress-redacted (step 9, this task) -- `full` = raw, own project only
    (CLAUDE.md rule 2's exception). The client's own copy -- the ENTIRE JSON
    body, including `resp["display"]["output"]` -- must stay redacted
    regardless of capture_io (fix round 1: `_full_capture_io` used to feed the
    RAW tool_calls into `display.output` too, which is not a telemetry egress
    and reaches both the calling client's own response body and the admin
    console's `/api/requests/{id}` drilldown -- neither is the app's own
    Langfuse project)."""
    ssn = "482-73-1946"
    tc = {"id": "call-1", "type": "function",
          "function": {"name": "get_weather",
                       "arguments": json.dumps({"city": "Paris", "ssn": ssn})}}
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=None, tool_calls=[tc])))
    mem, provider = _wire_app_tracer(monkeypatch)
    token = _make_full_app(client, "full-tools-ssn")

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "tools": _TOOLS, "tool_choice": "auto",
                          "messages": [{"role": "user", "content": "weather in Paris?"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    # Client-facing copy: redacted EVERYWHERE in the body, not just choices --
    # `display.output` (fix round 1) is part of this same JSON response.
    assert ssn not in json.dumps(body)
    assert "raw_completion" not in body
    assert "raw_tool_calls" not in body
    assert ssn not in (body.get("display", {}).get("output") or "")

    # The admin console's own drilldown (`request_snapshots.full_json=resp`,
    # same `resp` object) must not carry it either -- it is not the app's own
    # Langfuse project.
    snap = client.get(f"/api/requests/{body['id']}")
    assert snap.status_code == 200, snap.text
    assert ssn not in json.dumps(snap.json())

    # The app's OWN Langfuse export: raw.
    attrs = _root_attrs(mem, provider, body["id"])
    answer = json.loads(attrs["output.value"])
    assert ssn in answer["tool_calls"][0]["function"]["arguments"]

def test_full_trace_stores_tool_result_turn(client, monkeypatch):
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="It is sunny in Paris.")))
    mem, provider = _wire_app_tracer(monkeypatch)
    token = _make_full_app(client, "full-tool-result")

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "tools": _TOOLS, "messages": [
                        {"role": "user", "content": "weather in Paris?"},
                        {"role": "assistant", "content": None, "tool_calls": [_TOOL_CALL]},
                        {"role": "tool", "tool_call_id": "call-1", "content": "sunny, 21C"},
                    ]})
    assert r.status_code == 200, r.text
    attrs = _root_attrs(mem, provider, r.json()["id"])

    sent = json.loads(attrs["input.value"])
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "tool"]
    assert sent["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert sent["messages"][2] == {"role": "tool", "tool_call_id": "call-1",
                                   "content": "sunny, 21C"}
    assert attrs["output.value"] == "It is sunny in Paris."


def test_full_trace_stores_streamed_tool_call_turn(client, monkeypatch):
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=None, tool_calls=[_TOOL_CALL])))
    mem, provider = _wire_app_tracer(monkeypatch)
    token = _make_full_app(client, "full-tools-stream", stream_mode="stream_unverified")

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "tools": _TOOLS,
                          "messages": [{"role": "user", "content": "weather in Paris?"}]})
    assert r.status_code == 200, r.text
    first = json.loads(r.text.split("\n\n")[0][len("data: "):])
    attrs = _root_attrs(mem, provider, first["id"])

    assert json.loads(attrs["input.value"])["tools"] == _TOOLS
    answer = json.loads(attrs["output.value"])
    assert answer["tool_calls"][0]["function"]["name"] == "get_weather"


def test_full_trace_single_plain_message_stays_plain_text(client, monkeypatch):
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="Paris.")))
    mem, provider = _wire_app_tracer(monkeypatch)
    token = _make_full_app(client, "full-plain")

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "capital of France?"}]})
    assert r.status_code == 200, r.text
    attrs = _root_attrs(mem, provider, r.json()["id"])
    assert attrs["input.value"] == "capital of France?"
    assert attrs["output.value"] == "Paris."
