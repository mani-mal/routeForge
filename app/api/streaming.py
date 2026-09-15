"""OpenAI-compatible SSE emission for the chat streaming modes (per-app
`stream_mode`, see `app/policy/enums.py::STREAM_MODES`).

This is a *separate* channel from the decision-stream SSE feed
(`app/api/ui_routes.py::stream`, `GET /api/stream`) — that one carries
redaction-safe `DecisionRecord` events for the admin flow visualizer and is
never touched here. This module only serializes `chat.completion.chunk`
frames for `/v1/chat/completions` responses, mirroring the same
`data: {json}\\n\\n` / terminal `data: [DONE]\\n\\n` frame pattern
(`app/api/ui_routes.py:255-273`).

`verified_then_stream` (Task 4) is post-hoc chunking of an already-verified,
finished response — `chunk_text` below. Real backend token streaming
(`stream_unverified` / `incremental_verify`) is added in Tasks 5-7 and reuses
`openai_chunk`/`sse_frame`/`sse_response` from here.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

SSE_DONE = "data: [DONE]\n\n"


def openai_chunk(request_id: str, model: str, *, delta: str | None = None,
                  role: str | None = None,
                  finish_reason: str | None = None,
                  created: int | None = None,
                  tool_calls: list[dict] | None = None) -> dict:
    """Build one OpenAI-compatible `chat.completion.chunk` frame.

    `role` (e.g. "assistant") opens the stream, `delta` carries a content
    fragment, `tool_calls` carries the assistant's tool calls (each with its
    stream `index`), and `finish_reason` closes it — callers pass exactly one
    of `role`/`delta`/`tool_calls`, or none for the terminal frame.
    `created` defaults to "now" when omitted, but a multi-frame stream
    (`chunk_text` below) computes it once and threads the same value through
    every frame -- a real OpenAI stream holds `created` constant for the
    whole response, it doesn't drift frame to frame."""
    d: dict[str, object] = {}
    if role is not None:
        d["role"] = role
    if delta is not None:
        d["content"] = delta
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": d, "finish_reason": finish_reason}],
    }


def sse_frame(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _split_on_word_boundaries(text: str, chunk_size: int) -> list[str]:
    """Slice `text` into pieces of ~`chunk_size` chars, preferring to break
    right after a space so words aren't split mid-token. Slices come directly
    from `text` with no gaps, so `"".join(pieces) == text` always holds
    exactly, regardless of where the word-boundary search lands."""
    if not text:
        return []
    pieces: list[str] = []
    i, n = 0, len(text)
    while i < n:
        end = min(i + chunk_size, n)
        if end < n:
            brk = text.rfind(" ", i, end)
            if brk > i:  # a space strictly after i -> break just past it
                end = brk + 1
        pieces.append(text[i:end])
        i = end
    return pieces


async def chunk_text(request_id: str, model: str, text: str, *,
                      chunk_size: int = 48,
                      created: int | None = None,
                      tool_calls: list[dict] | None = None) -> AsyncIterator[str]:
    """Yield a full OpenAI-style SSE stream for an already-finished,
    already-verified `text`: a role-opening frame, then content frames
    splitting `text` on ~word boundaries, a `finish_reason="stop"` frame, and
    the terminal `[DONE]` frame. `created` is computed once (or taken from
    the caller, e.g. the response's own `created` if it has one) and held
    constant across every frame in this stream, not recomputed per frame.

    When the finished answer carries `tool_calls`, they go out in one frame
    after any content, each stamped with its stream `index` (OpenAI clients
    key the delta merge on it), and the stream finishes with "tool_calls"."""
    if created is None:
        created = int(time.time())
    yield sse_frame(openai_chunk(request_id, model, role="assistant", created=created))
    for piece in _split_on_word_boundaries(text, chunk_size):
        yield sse_frame(openai_chunk(request_id, model, delta=piece, created=created))
    if tool_calls:
        indexed = [{"index": i, **tc} for i, tc in enumerate(tool_calls)]
        yield sse_frame(openai_chunk(request_id, model, tool_calls=indexed,
                                     created=created))
    finish_reason = "tool_calls" if tool_calls else "stop"
    yield sse_frame(openai_chunk(request_id, model, finish_reason=finish_reason,
                                 created=created))
    yield SSE_DONE


def sse_response(agen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(agen, media_type="text/event-stream")
