"""Repro: tool_calls arguments bypass the gate (ingress) and step 9 (egress)."""
import json

import pytest

from tests._litellm_fake import CALLS, make_recording_acompletion, make_response
from app.gateway.backends import litellm_backend

_AUTH = {"Authorization": "Bearer demo-key"}
_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
_TOOLS = [{"type": "function", "function": {
    "name": "find_support_ticket", "description": "look up a ticket",
    "parameters": {"type": "object", "properties": {"ssn": {"type": "string"}}}}}]
SSN = "482-73-1946"


def test_replayed_tool_call_arguments_are_redacted_before_model(client, monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="found it")))
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "find the ticket"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "find_support_ticket",
                             "arguments": json.dumps({"ssn": SSN})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ticket 42"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200, r.text
    assert SSN not in json.dumps(CALLS[-1]["messages"])


def test_ssn_only_in_tool_call_arguments_escalates_to_onprem_only(client, monkeypatch):
    """CLAUDE.md rules 1/3: an SSN reachable only through a replayed tool
    call's arguments must set the same sensitivity/`onprem_only` an SSN in
    `content` would (test_policy_gate.py::test_ssn_request_is_onprem_only is
    the content-only analogue) -- classification must not blind-spot
    tool_calls."""
    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="found it")))
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "find the ticket"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "find_support_ticket",
                             "arguments": json.dumps({"ssn": SSN})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ticket 42"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"]["onprem_only"] is True
    assert "US_SSN" in body["policy"]["detected_entities"]
    assert SSN not in json.dumps(CALLS[-1]["messages"])


def test_passthrough_app_tool_call_arguments_reach_model_unchanged(client, monkeypatch):
    """Global Constraint: `policy_mode: passthrough` is the null detector, so
    redaction of tool-call arguments must naturally be a no-op there -- the
    ingress-direction analogue of the content no-op the plan requires."""
    login = client.post("/admin/login", json=_ADMIN)
    assert login.status_code == 200, login.text
    created = client.post("/admin/applications", json={"id": "passthru-in", "name": "P"})
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    put = client.put("/admin/applications/passthru-in/policy",
                     json={"policy_mode": "passthrough"})
    assert put.status_code == 200, put.text

    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="found it")))
    args = json.dumps({"ssn": SSN})
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={
        "messages": [
            {"role": "user", "content": "find the ticket"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "find_support_ticket", "arguments": args}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ticket 42"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200, r.text
    sent = CALLS[-1]["messages"]
    assistant_msg = next(m for m in sent if m.get("role") == "assistant")
    assert assistant_msg["tool_calls"][0]["function"]["arguments"] == args
    assert SSN in json.dumps(sent)  # untouched, not redacted


def test_model_generated_tool_call_arguments_are_verified_on_egress(client, monkeypatch):
    CALLS.clear()
    tc = {"id": "c1", "type": "function",
          "function": {"name": "find_support_ticket", "arguments": json.dumps({"ssn": SSN})}}
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content=None, tool_calls=[tc])))
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "find the ticket"}], "tools": _TOOLS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert SSN not in json.dumps(body["choices"])
    # Structure preserved: `id`/`type`/`function.name` untouched, only the
    # argument STRING is redacted (no JSON re-parse per the plan's constraint).
    tool_calls = body["choices"][0]["message"]["tool_calls"]
    assert tool_calls[0]["id"] == "c1"
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["function"]["name"] == "find_support_ticket"
    # Whole-body check (fix round 1): the SSN must not leak through ANY key of
    # the response, not just `choices` -- `display.output` fed the same raw
    # tool_calls JSON before that fix.
    raw_body = json.dumps(body)
    assert SSN not in raw_body
    # The internal pre-redaction keys must never reach the client, on any
    # response shape.
    assert "raw_completion" not in raw_body
    assert "raw_tool_calls" not in raw_body
    assert "raw_completion" not in body
    assert "raw_tool_calls" not in body

def test_model_generated_tool_call_arguments_stream_ssn_absent_from_sse(client, monkeypatch):
    """A client-driven tool conversation with `stream: true` is served as one
    complete completion and emitted as SSE (PR #41) -- the SSN in the model's
    tool_calls arguments must be redacted before it reaches ANY SSE frame,
    exactly as it is in the non-streaming response body."""
    from tests.test_admin_api import _login

    _login(client)
    created = client.post("/admin/applications", json={"id": "stream-tc-egress", "name": "S"})
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    pr = client.put("/admin/applications/stream-tc-egress/policy",
                    json={"stream_mode": "stream_unverified"})
    assert pr.status_code == 200, pr.text

    CALLS.clear()
    tc = {"id": "c1", "type": "function",
          "function": {"name": "find_support_ticket", "arguments": json.dumps({"ssn": SSN})}}
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content=None, tool_calls=[tc])))
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={
        "model": "auto", "stream": True, "tools": _TOOLS,
        "messages": [{"role": "user", "content": "find the ticket"}]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert SSN not in r.text
    for block in r.text.strip("\n").split("\n\n"):
        payload = block.strip()[len("data: "):]
        assert SSN not in payload
        if payload != "[DONE]":
            assert "raw_completion" not in payload
            assert "raw_tool_calls" not in payload

def test_passthrough_app_model_generated_tool_call_arguments_reach_client_unchanged(
        client, monkeypatch):
    """Global Constraint: `policy_mode: passthrough` is the null detector, so
    egress redaction of model-generated tool-call arguments must naturally be
    a no-op there too -- the egress-direction analogue of the ingress
    passthrough no-op above."""
    login = client.post("/admin/login", json=_ADMIN)
    assert login.status_code == 200, login.text
    created = client.post("/admin/applications", json={"id": "passthru-out", "name": "P"})
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    put = client.put("/admin/applications/passthru-out/policy",
                     json={"policy_mode": "passthrough"})
    assert put.status_code == 200, put.text

    CALLS.clear()
    args = json.dumps({"ssn": SSN})
    tc = {"id": "c1", "type": "function",
          "function": {"name": "find_support_ticket", "arguments": args}}
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content=None, tool_calls=[tc])))
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={
        "messages": [{"role": "user", "content": "find the ticket"}], "tools": _TOOLS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == args
    assert SSN in json.dumps(body["choices"])  # untouched, not redacted
