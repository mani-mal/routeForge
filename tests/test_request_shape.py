"""B123: the content-free shape of a client's request, and no silent loss of
text-bearing content parts.

The record is counts and closed-set names only -- what the client sent, by
role and by content-part type, and how much of it RouteForge discarded --
written on the routing outcome row (`request_shape`), on the trace root span
(`sp.request.*`), and, when anything was dropped, on one WARNING log line.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.gateway.backends import litellm_backend
from app.models import ChatRequest, Message
from app.pipeline import _is_tool_conversation
from tests._litellm_fake import CALLS, make_recording_acompletion, make_response
from tests.test_outcome_hooks import (
    DEMO, SSN, _client, _columns, _failing_backend, _only,
)

SECRET_TEXT = "SENTINEL-TEXT-q7w8e9"
SECRET_TYPE = "SECRET-TYPE-xyz"
SECRET_ROLE = "SECRET-ROLE-abc"


def _shape(payload: dict) -> dict:
    req = ChatRequest.model_validate(payload)
    return req.shape(tool_mode=_is_tool_conversation(req))


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #
def test_plain_chat_shape():
    shape = _shape({"messages": [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"}]})
    assert shape == {
        "has_tools": False, "n_tools": 0, "tool_mode": False,
        "message_roles": {"system": 1, "user": 1},
        "content_parts": {"string": 2},
        "dropped_parts": {},
        "empty_messages": 0,
    }


def test_tool_conversation_shape():
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}},
             {"type": "function", "function": {"name": "write", "parameters": {}}}]
    shape = _shape({"tools": tools, "messages": [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
        {"role": "assistant", "content": "done"}]})
    assert shape["has_tools"] is True
    assert shape["n_tools"] == 2
    assert shape["tool_mode"] is True
    assert shape["message_roles"] == {"system": 1, "user": 1, "assistant": 2, "tool": 1}
    # The assistant's null content is not a part; three strings, one text part.
    assert shape["content_parts"] == {"string": 3, "text": 1}
    assert shape["dropped_parts"] == {}
    assert shape["empty_messages"] == 0


def test_replayed_tool_calls_without_tools_is_tool_mode():
    shape = _shape({"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]})
    assert shape["has_tools"] is False and shape["n_tools"] == 0
    assert shape["tool_mode"] is True


def test_input_and_output_text_parts_are_kept_as_text():
    m = ChatRequest.model_validate({"messages": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "first"},
            {"type": "output_text", "text": "second"},
            {"type": "text", "text": "third"}]}]}).messages[0]
    assert m.content == "first\nsecond\nthird"
    assert Message(role="user", content=[{"type": "input_text", "text": "x"}]).content == "x"


def test_non_text_parts_are_dropped_and_counted():
    shape = _shape({"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "describe these"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "file", "file": {"file_id": "f1"}},
            {"type": "input_audio", "input_audio": {"data": "..."}},
            {"type": "mystery_part", "text": "?"},
            "not-a-dict"]}]})
    assert shape["content_parts"] == {
        "text": 1, "image_url": 1, "file": 1, "input_audio": 1, "other": 2}
    assert shape["dropped_parts"] == {"image_url": 1, "file": 1, "input_audio": 1, "other": 2}
    assert shape["empty_messages"] == 0


def test_image_only_user_message_is_empty():
    req = ChatRequest.model_validate({"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]})
    assert req.messages[1].content == ""
    shape = req.shape(tool_mode=False)
    assert shape["empty_messages"] == 1
    assert shape["dropped_parts"] == {"image_url": 1}


def test_unknown_role_and_part_type_bucket_to_other():
    shape = _shape({"messages": [
        {"role": SECRET_ROLE, "content": "hi"},
        {"role": "user", "content": [{"type": SECRET_TYPE, "text": SECRET_TEXT}]}]})
    assert shape["message_roles"] == {"other": 1, "user": 1}
    assert shape["content_parts"] == {"string": 1, "other": 1}
    assert shape["dropped_parts"] == {"other": 1}
    blob = json.dumps(shape)
    assert SECRET_ROLE not in blob and SECRET_TYPE not in blob and SECRET_TEXT not in blob


def test_messages_built_in_code_count_their_strings():
    """A request built from Message objects (no raw JSON) still has a shape;
    nothing was dropped because no parts were ever seen."""
    req = ChatRequest(messages=[Message(role="user", content="hi"),
                                Message(role="assistant", content=None)])
    shape = req.shape(tool_mode=False)
    assert shape["content_parts"] == {"string": 1}
    assert shape["dropped_parts"] == {}


# --------------------------------------------------------------------------- #
# through the HTTP boundary
# --------------------------------------------------------------------------- #
def test_ok_row_carries_request_shape():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "x"}}]}]})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.status == "ok"
        assert row.request_shape == {
            "has_tools": False, "n_tools": 0, "tool_mode": False,
            "message_roles": {"user": 1},
            "content_parts": {"text": 1, "image_url": 1},
            "dropped_parts": {"image_url": 1},
            "empty_messages": 0,
        }


def test_backend_failed_row_carries_request_shape(monkeypatch):
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": "user", "content": "hello"}]})
        assert r.status_code == 502, r.text
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.request_shape["message_roles"] == {"user": 1}
        assert row.request_shape["content_parts"] == {"string": 1}


def test_no_eligible_row_carries_request_shape(monkeypatch):
    from tests.onboarding.harness import disable_seeded_models, onboard
    from tests.test_outcome_hooks import _with_azure_enabled
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        disable_seeded_models(c)
        app = onboard(c, "shape-no-candidate", residency="onprem_only")
        r = c.post("/v1/chat/completions", headers=app.headers, json={"messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]})
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "no_eligible"
        assert row.request_shape["message_roles"] == {"system": 1, "user": 1}


def test_input_text_reaches_the_model_and_the_gate(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="fine")))
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": "user", "content": [
                {"type": "input_text", "text": "summarise the quarterly ledger"},
                {"type": "input_text", "text": f"my SSN is {SSN}"}]}]})
        assert r.status_code == 200, r.text
        row = _only(c)
        # The gate saw the input_text part: it detected the SSN in it.
        assert row.detected_entity_types, row.detected_entity_types
        assert row.request_shape["content_parts"] == {"input_text": 2}
        assert row.request_shape["dropped_parts"] == {}
    assert CALLS, "backend was never called"
    sent = json.dumps([call.get("messages") for call in CALLS], default=str)
    assert "summarise the quarterly ledger" in sent
    assert SSN not in sent  # the DEMO app's model_input is redacted


def test_dropped_parts_log_one_warning_with_counts_only(caplog):
    caplog.set_level(logging.WARNING, logger="serving.api")
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": SECRET_ROLE, "content": SECRET_TEXT},
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": SECRET_TYPE, "text": SECRET_TEXT},
                {"type": "image_url", "image_url": {"url": SECRET_TEXT}}]}]})
        assert r.status_code == 200, r.text
        row = _only(c)
        dumped = json.dumps(_columns(row), default=str)
    lines = [rec.getMessage() for rec in caplog.records
             if rec.name == "serving.api" and "content_parts_dropped" in rec.getMessage()]
    assert len(lines) == 1, lines
    payload = json.loads(lines[0])
    assert payload["event"] == "content_parts_dropped"
    assert payload["request_id"] == row.request_id
    assert payload["dropped_parts"] == {"image_url": 1, "other": 1}
    for blob in (lines[0], dumped):
        assert SECRET_TEXT not in blob
        assert SECRET_TYPE not in blob
        assert SECRET_ROLE not in blob


def test_no_warning_when_nothing_dropped(caplog):
    caplog.set_level(logging.WARNING, logger="serving.api")
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={"messages": [{"role": "user", "content": "hello"}]})
        assert r.status_code == 200, r.text
    assert not [rec for rec in caplog.records if "content_parts_dropped" in rec.getMessage()]


# --------------------------------------------------------------------------- #
# the trace
# --------------------------------------------------------------------------- #
pytest.importorskip("opentelemetry.sdk.trace")


def _mem(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import app.obs.otel as otel
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem


def _root(mem) -> dict:
    roots = [s for s in mem.get_finished_spans() if s.name == "serving.request"]
    assert len(roots) == 1, [s.name for s in mem.get_finished_spans()]
    return dict(roots[0].attributes)


def test_trace_root_span_carries_request_shape(monkeypatch):
    mem = _mem(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
            "messages": [
                {"role": SECRET_ROLE, "content": SECRET_TEXT},
                {"role": "user", "content": [
                    {"type": "input_text", "text": "hi"},
                    {"type": SECRET_TYPE, "text": SECRET_TEXT},
                    {"type": "image_url", "image_url": {"url": "x"}}]}]})
        assert r.status_code == 200, r.text
    a = _root(mem)
    assert a["sp.request.tool_mode"] is True
    assert a["sp.request.n_tools"] == 1
    assert json.loads(a["sp.request.message_roles"]) == {"other": 1, "user": 1}
    assert json.loads(a["sp.request.content_parts"]) == {
        "string": 1, "input_text": 1, "other": 1, "image_url": 1}
    assert json.loads(a["sp.request.dropped_parts"]) == {"other": 1, "image_url": 1}
    assert a["sp.request.empty_messages"] == 0
    shape_attrs = json.dumps({k: v for k, v in a.items() if k.startswith("sp.request.")})
    for secret in (SECRET_TEXT, SECRET_TYPE, SECRET_ROLE):
        assert secret not in shape_attrs
    # The role/type sentinels are not client text the gate could redact: they
    # must not reach ANY attribute of the span.
    blob = json.dumps(a, default=str)
    assert SECRET_TYPE not in blob and SECRET_ROLE not in blob


def test_trace_omits_dropped_parts_when_nothing_dropped(monkeypatch):
    mem = _mem(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={"messages": [{"role": "user", "content": "hello"}]})
        assert r.status_code == 200, r.text
    a = _root(mem)
    assert a["sp.request.tool_mode"] is False
    assert json.loads(a["sp.request.content_parts"]) == {"string": 1}
    assert "sp.request.dropped_parts" not in a


def test_failure_trace_carries_request_shape(monkeypatch):
    mem = _mem(monkeypatch)
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={"messages": [{"role": "user", "content": "hello"}]})
        assert r.status_code == 502, r.text
    a = _root(mem)
    assert a["sp.status"] == "error"
    assert json.loads(a["sp.request.message_roles"]) == {"user": 1}


@pytest.mark.parametrize("bad_type", [{"a": 1}, ["x"], 5, None])
def test_non_string_part_type_is_dropped_not_a_500(bad_type):
    """Review of 2f47aea: an unhashable `type` (dict/list) made the set lookup
    raise TypeError, which escapes pydantic as an unhandled 500. It must be
    dropped (and counted as `other`) like any unknown part, as before B123."""
    req = ChatRequest.model_validate({"messages": [{"role": "user", "content": [
        {"type": bad_type, "text": "ignored"}, {"type": "text", "text": "hi"}]}]})
    assert req.messages[0].content == "hi"
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": "user", "content": [{"type": bad_type}, {"type": "text", "text": "hi"}]}]})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("ptype", ["text", "input_text"])
def test_non_string_text_is_a_client_error_not_a_500(ptype):
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": "user", "content": [{"type": ptype, "text": 5}]}]})
    assert 400 <= r.status_code < 500, (r.status_code, r.text)
