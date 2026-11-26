"""Contract: the backend's `finish_reason` reaches the client (B113), and a
completion that used the client's whole output budget reads as `length` (B116).

An agent client switches on `finish_reason`. A truncated turn (`"length"`) or a
filtered one (`"content_filter"`) that arrives as `"stop"` reads as a finished
answer, so the agent acts on a cut-off reply. Before B113 every client-facing
path derived the reason from "were there tool calls?" alone, while the outcome
row recorded the backend's real `length`.

Rule (`app.models.client_finish_reason`), on EVERY path: a backend `length` /
`content_filter` wins, even over tool calls; otherwise `"tool_calls"` when the
turn ends in tool calls, else `"stop"`.

B116: a backend can label a cut-off turn `tool_calls` (vLLM's tool parser
finds a call in the truncated text). When `completion_tokens` reaches the
client's own output cap (`max_completion_tokens`, else `max_tokens`) the
client gets `length` whatever the backend said; the `tool_calls` array is left
as-is. With no client cap there is nothing to compare against and the rule is
B113's alone.

Every test drives the full `/v1/chat/completions` stack with a fake LiteLLM
backend that ends with the given finish reason and asserts what the CLIENT
receives on the wire (or, for the recorded response, what the console shows).
"""
from __future__ import annotations

import pytest

from app.gateway.backends import litellm_backend
from app.models import client_finish_reason
from tests._litellm_fake import (
    make_recording_acompletion, make_response, make_streaming_acompletion,
    make_streaming_tool_acompletion,
)
from tests.test_stream_keepalive import _fast_keepalive, _slow_run
from tests.test_stream_tool_calls import (
    _TOOL_CALL, _TOOLS, _frag_args, _frag_first, _make_app, _make_live_app,
    _parse_sse, _post_stream, _snapshot, _streamed_calls, _validate_chunks,
)

_AUTH = {"Authorization": "Bearer demo-key"}
_TEXT_TURN = [{"role": "user", "content": "write a long essay about rivers"}]


def _nonstream(client, *, headers=_AUTH, tools: bool = False,
               extra: dict | None = None) -> dict:
    body: dict = {"model": "auto", "messages": _TEXT_TURN, **(extra or {})}
    if tools:
        body["tools"] = _TOOLS
    r = client.post("/v1/chat/completions", headers=headers, json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    pytest.importorskip("openai.types.chat").ChatCompletion.model_validate(out)
    return out


def _stream(client, token, *, tools: bool = False,
            extra: dict | None = None) -> list:
    body: dict = {"model": "auto", "stream": True, "messages": _TEXT_TURN,
                  **(extra or {})}
    if tools:
        body["tools"] = _TOOLS
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"}, json=body)
    assert r.status_code == 200, r.text
    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    _validate_chunks(frames)
    return frames


def _final_reason(frames: list) -> str:
    """The one finish_reason a client sees: exactly one frame carries it."""
    reasons = [f["choices"][0]["finish_reason"] for f in frames[:-1]
               if f.get("choices") and f["choices"][0]["finish_reason"] is not None]
    assert len(reasons) == 1, reasons
    return reasons[0]


# --- the rule itself -------------------------------------------------------

@pytest.mark.parametrize(("backend", "tools", "expected"), [
    ("length", False, "length"),
    ("length", True, "length"),            # truncated tool call stays truncated
    ("content_filter", False, "content_filter"),
    ("content_filter", True, "content_filter"),
    ("stop", False, "stop"),
    ("tool_calls", True, "tool_calls"),
    ("stop", True, "tool_calls"),          # backend said stop but sent calls
    (None, False, "stop"),                 # unreported: pre-B113 behaviour
    (None, True, "tool_calls"),
    ("other", False, "stop"),
])
def test_client_finish_reason_rule(backend, tools, expected):
    assert client_finish_reason(backend, tools) == expected


# --- non-stream ------------------------------------------------------------

@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_nonstream_text_carries_backend_reason(client, monkeypatch, reason):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers are long bodies of", finish_reason=reason)))
    out = _nonstream(client)
    assert out["choices"][0]["finish_reason"] == reason


def test_nonstream_tool_call_truncated_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="length")))
    out = _nonstream(client, tools=True)
    choice = out["choices"][0]
    assert choice["message"]["tool_calls"]
    assert choice["finish_reason"] == "length"


def test_nonstream_normal_stop_and_tool_calls_unchanged(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers flow.", finish_reason="stop")))
    assert _nonstream(client)["choices"][0]["finish_reason"] == "stop"
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls")))
    assert _nonstream(client, tools=True)["choices"][0]["finish_reason"] == "tool_calls"


# --- verified_then_stream (chunk_text) -------------------------------------

def test_verified_then_stream_text_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers are long bodies of", finish_reason="length")))
    token = _make_app(client, "fr-vts-text", stream_mode="verified_then_stream")
    assert _final_reason(_stream(client, token)) == "length"


def test_verified_then_stream_tool_call_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="length")))
    token = _make_app(client, "fr-vts-tool", stream_mode="verified_then_stream")
    frames = _stream(client, token, tools=True)
    assert _streamed_calls(frames)
    assert _final_reason(frames) == "length"


def test_verified_then_stream_normal_turns_unchanged(client, monkeypatch):
    token = _make_app(client, "fr-vts-normal", stream_mode="verified_then_stream")
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers flow.", finish_reason="stop")))
    assert _final_reason(_stream(client, token)) == "stop"
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls")))
    assert _final_reason(_stream(client, token, tools=True)) == "tool_calls"


def test_keepalive_stream_reports_length(client, monkeypatch):
    """The keepalive path (answer not ready within one interval) chunks the
    finished response with `chunk_text` too: it must carry the reason."""
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers are long bodies of", finish_reason="length")))
    token = _make_app(client, "fr-keepalive", stream_mode="verified_then_stream")
    _fast_keepalive(client)
    _slow_run(monkeypatch, client, delay=0.3)
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "messages": _TEXT_TURN})
    assert r.status_code == 200, r.text
    assert ": keepalive" in r.text
    data = r.text.replace(": keepalive\n\n", "")
    assert _final_reason(_parse_sse(data)) == "length"


# --- stream_unverified (live tokens) ---------------------------------------

@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_live_stream_text_carries_backend_reason(client, monkeypatch, reason):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(
        ["Rivers are ", "long bodies of"], finish_reason=reason))
    token = _make_live_app(client, f"fr-live-text-{reason}")
    frames = _stream(client, token)
    assert _final_reason(frames) == reason
    # The recorded response (console snapshot) says the same thing.
    snap = _snapshot(client, frames[0]["id"])
    assert snap["choices"][0]["finish_reason"] == reason


def test_live_stream_tool_call_reports_length_and_records_it(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion(
        [_frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Pa')],
        finish_reason="length"))
    token = _make_live_app(client, "fr-live-tool")
    frames = _parse_sse(_post_stream(client, token).text)
    _validate_chunks(frames)
    assert _streamed_calls(frames)
    assert _final_reason(frames) == "length"
    # The recorded response of a live tool turn (`_finalize_stream`).
    snap = _snapshot(client, frames[0]["id"])
    choice = snap["choices"][0]
    assert choice["message"]["tool_calls"]
    assert choice["finish_reason"] == "length"


def test_live_stream_normal_turns_unchanged(client, monkeypatch):
    token = _make_live_app(client, "fr-live-normal")
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(
        ["Rivers ", "flow."], finish_reason="stop"))
    frames = _stream(client, token)
    assert _final_reason(frames) == "stop"
    assert _snapshot(client, frames[0]["id"])["choices"][0]["finish_reason"] == "stop"
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion(
        [_frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Paris"}')]))
    frames = _parse_sse(_post_stream(client, token).text)
    assert _final_reason(frames) == "tool_calls"
    assert _snapshot(client, frames[0]["id"])["choices"][0]["finish_reason"] == "tool_calls"


# --- incremental_verify (live tokens through the hold-back redactor) -------

def test_incremental_verify_text_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(
        ["Rivers are ", "long bodies of"], finish_reason="length"))
    token = _make_app(client, "fr-iv-text", stream_mode="incremental_verify")
    frames = _stream(client, token)
    assert _final_reason(frames) == "length"
    assert _snapshot(client, frames[0]["id"])["choices"][0]["finish_reason"] == "length"


def test_incremental_verify_normal_stop_unchanged(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(
        ["Rivers ", "flow."], finish_reason="stop"))
    token = _make_app(client, "fr-iv-normal", stream_mode="incremental_verify")
    assert _final_reason(_stream(client, token)) == "stop"


# --- B116: the client's output cap reached -> "length" ----------------------
#
# The production case: completion_tokens == max_tokens, backend label
# `tool_calls`. Each path below serves that turn and must report `length`.

_CAP = 64


@pytest.mark.parametrize(("backend", "tools", "used", "cap", "expected"), [
    ("tool_calls", True, _CAP, _CAP, "length"),       # the B116 case
    ("tool_calls", True, _CAP + 5, _CAP, "length"),   # over the cap (reasoning etc.)
    ("stop", False, _CAP, _CAP, "length"),            # a text answer at the cap too
    (None, True, _CAP, _CAP, "length"),               # unreported backend reason
    ("tool_calls", True, _CAP - 1, _CAP, "tool_calls"),  # under the cap: unchanged
    ("stop", False, _CAP - 1, _CAP, "stop"),
    ("tool_calls", True, 32000, None, "tool_calls"),  # no client cap: nothing to compare
    ("tool_calls", True, None, _CAP, "tool_calls"),   # tokens unreported: unchanged
    ("content_filter", True, _CAP, _CAP, "content_filter"),  # B113 rule 1 still first
])
def test_client_finish_reason_cap_rule(backend, tools, used, cap, expected):
    assert client_finish_reason(backend, tools, completion_tokens=used,
                                max_tokens=cap) == expected


@pytest.mark.parametrize("cap_key", ["max_tokens", "max_completion_tokens"])
def test_nonstream_tool_call_at_cap_reports_length(client, monkeypatch, cap_key):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls",
                      completion_tokens=_CAP)))
    out = _nonstream(client, tools=True, extra={cap_key: _CAP})
    choice = out["choices"][0]
    assert choice["message"]["tool_calls"] == [_TOOL_CALL]   # left as-is
    assert choice["finish_reason"] == "length"


def test_nonstream_max_completion_tokens_wins_over_max_tokens(client, monkeypatch):
    """`max_completion_tokens` is the cap when both are sent (the router's
    `client_output_cap` reading); a completion under it is not truncated."""
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls",
                      completion_tokens=_CAP)))
    out = _nonstream(client, tools=True,
                     extra={"max_tokens": _CAP, "max_completion_tokens": _CAP * 2})
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_nonstream_under_cap_or_no_cap_unchanged(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls",
                      completion_tokens=_CAP - 1)))
    assert _nonstream(client, tools=True, extra={"max_tokens": _CAP}
                      )["choices"][0]["finish_reason"] == "tool_calls"
    # No client cap: RouteForge invents none, whatever the token count.
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls",
                      completion_tokens=32000)))
    assert _nonstream(client, tools=True)["choices"][0]["finish_reason"] == "tool_calls"


def test_nonstream_text_at_cap_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers are long bodies of", finish_reason="stop",
                      completion_tokens=_CAP)))
    out = _nonstream(client, extra={"max_tokens": _CAP})
    assert out["choices"][0]["finish_reason"] == "length"


def test_verified_then_stream_tool_call_at_cap_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content=None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls",
                      completion_tokens=_CAP)))
    token = _make_app(client, "fr116-vts-tool", stream_mode="verified_then_stream")
    frames = _stream(client, token, tools=True, extra={"max_tokens": _CAP})
    assert _streamed_calls(frames)
    assert _final_reason(frames) == "length"


def test_keepalive_stream_at_cap_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(
        make_response(content="Rivers are long bodies of", finish_reason="stop",
                      completion_tokens=_CAP)))
    token = _make_app(client, "fr116-keepalive", stream_mode="verified_then_stream")
    _fast_keepalive(client)
    _slow_run(monkeypatch, client, delay=0.3)
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "messages": _TEXT_TURN,
                          "max_tokens": _CAP})
    assert r.status_code == 200, r.text
    assert ": keepalive" in r.text
    data = r.text.replace(": keepalive\n\n", "")
    assert _final_reason(_parse_sse(data)) == "length"


@pytest.mark.parametrize("cap_key", ["max_tokens", "max_completion_tokens"])
def test_live_stream_tool_call_at_cap_reports_length_and_records_it(
        client, monkeypatch, cap_key):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion(
        [_frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Paris"}')],
        finish_reason="tool_calls", completion_tokens=_CAP))
    token = _make_live_app(client, f"fr116-live-tool-{cap_key}")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "tools": _TOOLS,
                          "messages": [{"role": "user", "content": "weather in Paris?"}],
                          cap_key: _CAP})
    assert r.status_code == 200, r.text
    frames = _parse_sse(r.text)
    _validate_chunks(frames)
    assert _streamed_calls(frames)
    assert _final_reason(frames) == "length"
    snap = _snapshot(client, frames[0]["id"])
    choice = snap["choices"][0]
    assert choice["message"]["tool_calls"]
    assert choice["finish_reason"] == "length"


def test_live_stream_under_cap_unchanged(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_tool_acompletion(
        [_frag_first(0, "call-1", "get_weather"), _frag_args(0, '{"city": "Paris"}')],
        finish_reason="tool_calls", completion_tokens=_CAP - 1))
    token = _make_live_app(client, "fr116-live-under")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True, "tools": _TOOLS,
                          "messages": [{"role": "user", "content": "weather in Paris?"}],
                          "max_tokens": _CAP})
    frames = _parse_sse(r.text)
    assert _final_reason(frames) == "tool_calls"
    assert _snapshot(client, frames[0]["id"])["choices"][0]["finish_reason"] == "tool_calls"


def test_live_stream_text_at_cap_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(
        ["Rivers are ", "long bodies of"], finish_reason="stop", completion_tokens=_CAP))
    token = _make_live_app(client, "fr116-live-text")
    frames = _stream(client, token, extra={"max_tokens": _CAP})
    assert _final_reason(frames) == "length"
    assert _snapshot(client, frames[0]["id"])["choices"][0]["finish_reason"] == "length"


def test_incremental_verify_text_at_cap_reports_length(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(
        ["Rivers are ", "long bodies of"], finish_reason="stop", completion_tokens=_CAP))
    token = _make_app(client, "fr116-iv-text", stream_mode="incremental_verify")
    frames = _stream(client, token, extra={"max_completion_tokens": _CAP})
    assert _final_reason(frames) == "length"
    assert _snapshot(client, frames[0]["id"])["choices"][0]["finish_reason"] == "length"
