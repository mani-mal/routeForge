"""Convert RouteForge `Message`s (OpenAI chat shape) into a Switchyard request (B103).

Pure functions, no `switchyard` import. The target is Switchyard's neutral
`LlmRequest` (crate `switchyard_protocol`, `crates/protocol/src/llm.rs` at tag
v0.4.0a1; field table in `docs/reference/protocols/llm_requests.md`). The
mapping mirrors upstream's own Chat Completions codec
(`crates/switchyard-translation/src/codecs/openai_chat/buffered.rs`,
`decode_request`), so the advisor reads a history exactly as Switchyard would
had it decoded the same body itself:

    OpenAI chat                         Switchyard
    -----------                         ----------
    system / developer message     ->   instructions[] {role, content}
    user / assistant text          ->   messages[] {role, content:[{type:text}]}
    assistant tool_calls[i]        ->   {type:"tool_call", id, name, arguments}
                                        (arguments: the JSON-decoded string;
                                        undecodable -> {"raw": <string>}, as upstream)
    role "tool" + tool_call_id     ->   messages[] {role:"user", content:[
                                          {type:"tool_result", tool_call_id,
                                           content:[{type:text}], is_error:null}]}
    content None / ""              ->   content: []

Where upstream's codec quietly defaults (a missing tool-call id or name becomes
an empty string), this module refuses instead: anything it can't map faithfully
raises `_Unconvertible`, which the advisor turns into the `unconvertible`
fallback. It never guesses silently.

The request is built from scratch: only `instructions` and `messages`. No
model, tools, metadata or headers from the client cross over (design §8).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.models import Message

_INSTRUCTION_ROLES = {"system", "developer"}
_CONVERSATION_ROLES = {"user", "assistant"}


class _Unconvertible(Exception):
    """A message has no faithful Switchyard representation.

    The message carries a short, content-free reason (a field name or a role
    enum), never message text, so it is safe in a log line.
    """


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None or content == "":
        return []
    if not isinstance(content, str):
        raise _Unconvertible("content is not a string")
    return [{"type": "text", "text": content}]


def _arguments(raw: Any) -> Any:
    """Decode OpenAI's JSON-string arguments, as upstream's `parse_arguments`."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}
    if isinstance(raw, (dict, list)):
        return raw
    raise _Unconvertible("tool_call arguments has an unsupported type")


def _tool_call_block(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict):
        raise _Unconvertible("tool_call is not an object")
    function = call.get("function")
    if not isinstance(function, dict):
        raise _Unconvertible("tool_call has no function object")
    call_id = call.get("id")
    name = function.get("name")
    if not isinstance(call_id, str) or not call_id:
        raise _Unconvertible("tool_call has no id")
    if not isinstance(name, str) or not name:
        raise _Unconvertible("tool_call has no function name")
    return {
        "type": "tool_call",
        "id": call_id,
        "name": name,
        "arguments": _arguments(function.get("arguments")),
    }


def _tool_result_message(message: Message) -> dict[str, Any]:
    call_id = message.tool_call_id
    if not isinstance(call_id, str) or not call_id:
        raise _Unconvertible("tool message has no tool_call_id")
    # Upstream maps a Chat Completions tool message to a *user* turn carrying a
    # tool_result block; OpenAI has no error flag, so is_error stays null.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_call_id": call_id,
                "content": _text_blocks(message.content),
                "is_error": None,
            }
        ],
    }


def to_switchyard_request(messages: Sequence[Message]) -> dict[str, Any]:
    """Return `{"instructions": [...], "messages": [...]}` for `run_stream`.

    Raises `_Unconvertible` for anything that can't be mapped faithfully.
    """
    instructions: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Message):
            raise _Unconvertible("not a Message")
        role = message.role
        if message.tool_calls and role != "assistant":
            raise _Unconvertible("tool_calls on a non-assistant message")

        if role in _INSTRUCTION_ROLES:
            instructions.append({"role": role, "content": _text_blocks(message.content)})
        elif role == "tool":
            converted.append(_tool_result_message(message))
        elif role in _CONVERSATION_ROLES:
            content = _text_blocks(message.content)
            if message.tool_calls is not None:
                if not isinstance(message.tool_calls, list):
                    raise _Unconvertible("tool_calls is not a list")
                content += [_tool_call_block(call) for call in message.tool_calls]
            converted.append({"role": role, "content": content})
        else:
            # e.g. OpenAI's legacy "function" role, or anything unknown.
            raise _Unconvertible("unsupported role")
    return {"instructions": instructions, "messages": converted}
