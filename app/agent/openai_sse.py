"""OpenAI `chat.completion.chunk` SSE serialization for the mock agent LLM
endpoint (Phase 5, Task 1).

Verified against a real NeMo Agent Toolkit `tool_calling_agent` run in
`scratch-nat-spike/` (see FINDINGS.md section 2 and `stub_server.py`):
LangChain's `ChatOpenAI` sends `stream: true` and requires true SSE chunk
streaming — a single non-streaming JSON body is rejected with
"No generation chunks were returned" even though the HTTP call itself
succeeds. Each stream here is two `data:` chunks (payload, then a
finish_reason-only chunk) followed by a terminal `data: [DONE]\n\n`.

Pure serialization only — no NAT, no FastAPI, no router.
"""
import json
import time
import uuid
from collections.abc import Iterator


def _chunk(*, completion_id: str, created: int, model: str, delta: dict, finish_reason: str | None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def sse_tool_call(*, model: str, call_id: str, tool_name: str, arguments_json: str) -> Iterator[str]:
    """Yield SSE chunks for an assistant message that invokes a tool.

    Two `data:` chunks — role + tool_calls delta, then a finish_reason
    ("tool_calls")-only delta — followed by `data: [DONE]`.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    delta = {
        "role": "assistant",
        "tool_calls": [
            {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments_json},
            }
        ],
    }
    yield _chunk(completion_id=completion_id, created=created, model=model, delta=delta, finish_reason=None)
    yield _chunk(completion_id=completion_id, created=created, model=model, delta={}, finish_reason="tool_calls")
    yield "data: [DONE]\n\n"


def sse_final(*, model: str, content: str) -> Iterator[str]:
    """Yield SSE chunks for a final assistant answer (no tool call).

    Two `data:` chunks — role + content delta, then a finish_reason
    ("stop")-only delta — followed by `data: [DONE]`.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    delta = {"role": "assistant", "content": content}
    yield _chunk(completion_id=completion_id, created=created, model=model, delta=delta, finish_reason=None)
    yield _chunk(completion_id=completion_id, created=created, model=model, delta={}, finish_reason="stop")
    yield "data: [DONE]\n\n"
