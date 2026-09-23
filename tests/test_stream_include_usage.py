"""B53: streamed responses never carried the terminal `usage` frame, even
when the client sent `stream_options: {"include_usage": true}`. Clients that
drive context accounting from reported usage (Qwen Code) then fell back to a
character estimate that undercounts token-dense history >= 1.58x, compacted
too late, and overflowed the model window.

OpenAI's contract: with include_usage, one extra chunk after the
finish_reason chunk and before `[DONE]`, with `choices: []` and `usage`.
Without it the stream is byte-identical to before. Asserted against real SSE
bytes off a real TestClient, for every stream mode.
"""
from __future__ import annotations

import json

import pytest

from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_streaming_acompletion
from tests.test_admin_api import _login

_DELTAS = ["Hello", ", ", "streamed", " ", "world", "!"]
_MSGS = [{"role": "user", "content": "hi there"}]


def _make_app(client, app_id: str, *, stream_mode: str) -> str:
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"stream_mode": stream_mode})
    assert pr.status_code == 200, pr.text
    return r.json()["token"]


def _frames(text: str) -> list:
    out = []
    for block in text.strip("\n").split("\n\n"):
        block = block.strip()
        if block and not block.startswith(":"):
            payload = block[len("data: "):]
            out.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return out


def _patch_backend(monkeypatch, mode):
    # verified_then_stream runs the normal (non-streamed) backend call and
    # chunks the finished answer; only the real-token modes need the
    # streaming fake.
    if mode != "verified_then_stream":
        monkeypatch.setattr(litellm_backend, "acompletion",
                            make_streaming_acompletion(_DELTAS))


def _post(client, token, *, include_usage: bool | None, stream: bool = True):
    body = {"model": "auto", "messages": _MSGS, "stream": stream}
    if include_usage is not None:
        body["stream_options"] = {"include_usage": include_usage}
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {token}"}, json=body)


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify",
                                  "verified_then_stream"])
def test_include_usage_emits_terminal_usage_frame(client, monkeypatch, mode):
    _patch_backend(monkeypatch, mode)
    token = _make_app(client, f"iu-{mode}", stream_mode=mode)
    r = _post(client, token, include_usage=True)
    assert r.status_code == 200, r.text
    frames = _frames(r.text)

    assert frames[-1] == "[DONE]"
    usage_frame = frames[-2]
    assert usage_frame["choices"] == []
    assert usage_frame["object"] == "chat.completion.chunk"
    u = usage_frame["usage"]
    assert u["prompt_tokens"] > 0 and u["completion_tokens"] > 0
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"]
    assert "cached_prompt_tokens" in u          # same shape as the non-stream body
    # after the finish_reason frame, same id/created/model as every other frame
    finish = frames[-3]
    assert finish["choices"][0]["finish_reason"] in ("stop", "tool_calls")
    for f in frames[:-1]:
        assert f["id"] == usage_frame["id"]
        assert f["created"] == usage_frame["created"]
        assert f["model"] == usage_frame["model"]


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify",
                                  "verified_then_stream"])
@pytest.mark.parametrize("include_usage", [None, False])
def test_without_include_usage_no_usage_frame(client, monkeypatch, mode, include_usage):
    _patch_backend(monkeypatch, mode)
    token = _make_app(client, f"nu-{mode}-{include_usage}", stream_mode=mode)
    frames = _frames(_post(client, token, include_usage=include_usage).text)
    assert frames[-1] == "[DONE]"
    assert all("usage" not in f for f in frames[:-1])
    assert frames[-2]["choices"][0]["finish_reason"] in ("stop", "tool_calls")


def test_stream_usage_matches_nonstream_usage(client, monkeypatch):
    token = _make_app(client, "iu-match", stream_mode="verified_then_stream")
    streamed = _frames(_post(client, token, include_usage=True).text)[-2]["usage"]
    plain = _post(client, token, include_usage=None, stream=False).json()["usage"]
    assert streamed == plain


def test_stream_options_not_forwarded_upstream(client, monkeypatch):
    from tests import _litellm_fake
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_acompletion(_DELTAS))
    _litellm_fake.CALLS.clear()
    token = _make_app(client, "iu-fwd", stream_mode="stream_unverified")
    _post(client, token, include_usage=False)
    # the backend always asks upstream for usage (its own metering), whatever
    # the client sent -- the client's False is never forwarded
    assert _litellm_fake.CALLS[-1]["stream_options"] == {"include_usage": True}
