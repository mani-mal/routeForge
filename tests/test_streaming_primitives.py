"""Unit coverage for `app/api/streaming.py`'s SSE primitives, independent of
the route/pipeline integration in `tests/test_streaming.py`."""
from __future__ import annotations

import asyncio
import json

from app.api.streaming import SSE_DONE, chunk_text, openai_chunk, sse_frame


def test_openai_chunk_role_frame():
    c = openai_chunk("req-1", "local-8b", role="assistant")
    assert c["object"] == "chat.completion.chunk"
    assert c["id"] == "req-1"
    assert c["model"] == "local-8b"
    assert c["choices"] == [{"index": 0, "delta": {"role": "assistant"},
                             "finish_reason": None}]
    assert isinstance(c["created"], int)


def test_openai_chunk_content_frame():
    c = openai_chunk("req-1", "local-8b", delta="hello")
    assert c["choices"][0]["delta"] == {"content": "hello"}
    assert c["choices"][0]["finish_reason"] is None


def test_openai_chunk_finish_frame_has_empty_delta():
    c = openai_chunk("req-1", "local-8b", finish_reason="stop")
    assert c["choices"][0]["delta"] == {}
    assert c["choices"][0]["finish_reason"] == "stop"


def test_sse_frame_format():
    obj = {"a": 1}
    assert sse_frame(obj) == f"data: {json.dumps(obj)}\n\n"


def test_sse_done_constant():
    assert SSE_DONE == "data: [DONE]\n\n"


def _drain(agen):
    async def _run():
        return [chunk async for chunk in agen]
    return asyncio.run(_run())


def test_chunk_text_reconstructs_exactly_and_ends_with_done():
    text = "The quick brown fox jumps over the lazy dog " * 3
    frames = _drain(chunk_text("req-2", "local-8b", text, chunk_size=10))
    assert frames[-1] == SSE_DONE
    parsed = [json.loads(f[len("data: "):-2]) for f in frames[:-1]]
    assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop"
    content_frames = parsed[1:-1]
    assert len(content_frames) > 1  # actually chunked, not one giant piece
    reconstructed = "".join(p["choices"][0]["delta"]["content"] for p in content_frames)
    assert reconstructed == text


def test_chunk_text_empty_text_still_frames_role_finish_done():
    frames = _drain(chunk_text("req-3", "local-8b", "", chunk_size=10))
    parsed = [json.loads(f[len("data: "):-2]) for f in frames[:-1]]
    assert len(parsed) == 2  # role frame + finish frame, no content frames
    assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert parsed[1]["choices"][0]["finish_reason"] == "stop"
    assert frames[-1] == SSE_DONE


def test_chunk_text_never_splits_a_word_when_a_boundary_is_available():
    text = "alpha beta gamma delta epsilon"  # no word longer than chunk_size
    frames = _drain(chunk_text("req-4", "local-8b", text, chunk_size=8))
    parsed = [json.loads(f[len("data: "):-2]) for f in frames[:-1]]
    pieces = [p["choices"][0]["delta"]["content"] for p in parsed[1:-1]]
    assert "".join(pieces) == text  # exact reconstruction
    # every internal cut point lands right after a space in the original
    # text -- never strictly inside a word.
    offset = 0
    for piece in pieces[:-1]:
        offset += len(piece)
        assert text[offset - 1] == " ", (
            f"piece boundary at {offset} splits a word: {text!r}")
