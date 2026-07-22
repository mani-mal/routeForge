"""Deterministic agent-step decision for the Phase 5 mock agent LLM endpoint.

Pure decision logic only — no NAT, no FastAPI, no router. Given the OpenAI-
format message history and the tool schemas an agent was offered, decides
whether to emit a tool call or a final answer:

  - Any `role == "tool"` message present → the tool has already run once;
    return a final answer that echoes the last tool result.
  - Otherwise → emit a tool call against the first offered tool, deriving
    arguments from the last user message (an arithmetic expression for a
    calculator tool, else a fixed query string).
"""
import re

_EXPRESSION_RE = re.compile(r"\d+\s*[-+*/]\s*\d+")


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return (message.get("content") or "").strip()
    return ""


def decide_agent_step(messages: list[dict], tools: list[dict]) -> dict:
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    if tool_messages:
        last_result = tool_messages[-1].get("content", "")
        return {"kind": "final", "content": f"The tool reported: {last_result}"}

    first_tool = tools[0] if tools else {}
    tool_name = first_tool.get("function", {}).get("name", "")
    user_text = _last_user_text(messages)

    arguments: dict = {"query": user_text}
    if tool_name == "calculator":
        match = _EXPRESSION_RE.search(user_text)
        if match:
            arguments = {"expression": match.group(0)}

    return {"kind": "tool_call", "tool_name": tool_name, "arguments": arguments}
