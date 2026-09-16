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


@pytest.mark.xfail(strict=True, reason="egress tool_calls verification — Task 2")
def test_model_generated_tool_call_arguments_are_verified_on_egress(client, monkeypatch):
    CALLS.clear()
    tc = {"id": "c1", "type": "function",
          "function": {"name": "find_support_ticket", "arguments": json.dumps({"ssn": SSN})}}
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content=None, tool_calls=[tc])))
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "find the ticket"}], "tools": _TOOLS})
    assert r.status_code == 200, r.text
    assert SSN not in json.dumps(r.json()["choices"])
