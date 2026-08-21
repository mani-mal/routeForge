"""Task 4: streaming scaffolding + `verified_then_stream` mode (per-app
`stream_mode`, see docs/architecture/per-app-config-flexibility.md).

`verified_then_stream` runs the full verified pipeline (identical to the
non-stream path -- step 9 verification is NOT skipped) and only chunks the
finished text out as OpenAI-style SSE. `stream_unverified`/
`incremental_verify` map to this same safe fallback in this task (Tasks 5-7
add real backend token streaming with the runtime verification floor).
"""
from __future__ import annotations

import json

import pytest

from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response
from tests.test_admin_api import _login

_LONG_TEXT = ("The quick brown fox jumps over the lazy dog. " * 4).strip()


def _make_app(client, app_id: str, *, stream_mode: str | None) -> str:
    """Create an app + token, optionally set `stream_mode` via the admin
    policy endpoint (None leaves the policy row's stream_mode unset -> the
    default 'inherit' behavior)."""
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    if stream_mode is not None:
        pr = client.put(f"/admin/applications/{app_id}/policy",
                        json={"stream_mode": stream_mode})
        assert pr.status_code == 200, pr.text
    return token


def _fixed_backend(monkeypatch, content: str) -> None:
    """Fixed-content fake backend -- content doesn't depend on the (already
    gate-sanitized) prompt, so both the streamed and non-stream calls in a
    test get the exact same completion text to compare against."""
    async def _fixed(**kwargs):
        return make_response(content=content)
    monkeypatch.setattr(litellm_backend, "acompletion", _fixed)


def _parse_sse(text: str) -> list[dict | str]:
    """Parse a full SSE body (as read by TestClient) into a list of decoded
    `data:` payloads, `"[DONE]"` for the terminal frame."""
    frames: list[dict | str] = []
    for block in text.strip("\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: "), block
        payload = block[len("data: "):]
        frames.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return frames


# --------------------------------------------------------------------------- #
# (a) verified_then_stream: real SSE bytes, matches non-stream answer, usage
# --------------------------------------------------------------------------- #
def test_verified_then_stream_matches_nonstream_and_records_usage(client, monkeypatch):
    _fixed_backend(monkeypatch, _LONG_TEXT)
    token = _make_app(client, "stream-vts", stream_mode="verified_then_stream")
    msg = {"model": "auto", "messages": [{"role": "user", "content": "tell me a story"}]}

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={**msg, "stream": True})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"  # stream ends with [DONE]
    assert frames[0]["object"] == "chat.completion.chunk"
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames[0]["choices"][0]["finish_reason"] is None

    finish_frame = frames[-2]
    assert finish_frame["choices"][0]["delta"] == {}
    assert finish_frame["choices"][0]["finish_reason"] == "stop"

    content_frames = frames[1:-2]
    assert len(content_frames) > 1  # actually split into multiple chunks
    for f in content_frames:
        assert f["choices"][0]["finish_reason"] is None
        assert set(f["choices"][0]["delta"]) == {"content"}
    streamed = "".join(f["choices"][0]["delta"]["content"] for f in content_frames)
    assert streamed == _LONG_TEXT  # exact reconstruction, no dropped/duplicated text

    # Concatenated deltas equal the full (non-stream) answer for the same app.
    r2 = client.post("/v1/chat/completions",
                     headers={"Authorization": f"Bearer {token}"}, json=msg)
    assert r2.status_code == 200, r2.text
    assert r2.json()["choices"][0]["message"]["content"] == _LONG_TEXT
    assert streamed == r2.json()["choices"][0]["message"]["content"]

    # A usage row was recorded for the streamed request too (both calls ok).
    _login(client)
    usage = client.get("/admin/usage?application_id=stream-vts").json()
    assert usage["count"] == 2


def test_stream_unverified_and_incremental_verify_fall_back_to_verified_then_stream(
        client, monkeypatch):
    """Interim safe fallback (Task 4): both future modes stream via the exact
    same chunk-the-verified-answer path until Tasks 5-7 add real backend
    token streaming."""
    _fixed_backend(monkeypatch, _LONG_TEXT)
    for mode in ("stream_unverified", "incremental_verify"):
        token = _make_app(client, f"stream-fallback-{mode}", stream_mode=mode)
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"model": "auto",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": True})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _parse_sse(r.text)
        assert frames[-1] == "[DONE]"
        streamed = "".join(
            f["choices"][0]["delta"]["content"] for f in frames[1:-2])
        assert streamed == _LONG_TEXT


# --------------------------------------------------------------------------- #
# (b) backwards compatibility: no per-app stream_mode -> 400, unchanged
# --------------------------------------------------------------------------- #
def test_stream_without_app_override_still_400s(client):
    # "acme-key" (seeded, app/apps/bootstrap.py) has no AppPolicy.stream_mode
    # override -> today's behavior: reject_stream_requests (default true)
    # rejects with 400, exactly as before this task.
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer acme-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}],
                          "stream": True})
    assert r.status_code == 400
    assert "streaming is not supported" in r.json()["detail"]


def test_stream_with_invalid_token_still_400s_not_401(client):
    # The old guard ran unconditionally before auth; preserved for this edge
    # case too -- a stream request never leaks whether the token was valid.
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer not-a-real-token"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}],
                          "stream": True})
    assert r.status_code == 400


def test_non_stream_request_with_invalid_token_still_401s(client):
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer not-a-real-token"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# (c) non-stream requests: unchanged (regression)
# --------------------------------------------------------------------------- #
def test_non_stream_request_unchanged(client, monkeypatch):
    _fixed_backend(monkeypatch, "a plain answer")
    token = _make_app(client, "stream-none", stream_mode=None)
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "a plain answer"
    assert "usage" in body and "trace" in body and "cost" in body
    assert "display" in body  # _finalize still ran (unchanged post-processing)
    # never-client-visible internal keys stay stripped, exactly as before.
    assert "sanitized_prompt" not in body
    assert "raw_completion" not in body


def test_non_stream_request_with_verified_then_stream_app_is_unaffected(client, monkeypatch):
    # An app that opted into verified_then_stream still gets the plain JSON
    # response shape when it doesn't set stream:true on a given request.
    _fixed_backend(monkeypatch, "a plain answer")
    token = _make_app(client, "stream-vts-plain", stream_mode="verified_then_stream")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["choices"][0]["message"]["content"] == "a plain answer"
