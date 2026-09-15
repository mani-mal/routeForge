"""Request shapes captured from real OpenAI-compatible clients. Every onboarding
incident so far was a shape like these that no test had sent. Synthetic
content only -- never real customer data."""
from __future__ import annotations

# ~108K chars -> ~27K tokens by count_tokens (len // 4): a real Qwen Code
# system prompt is ~27K chars of instructions plus tool docs.
BIG_SYSTEM = "You are Qwen Code, an interactive CLI agent. " * 2400

TOOL_NAMES = [
    "read_file", "write_file", "edit", "list_directory", "glob", "grep",
    "run_shell_command", "web_fetch", "web_search", "save_memory", "todo_write",
    "task", "read_many_files", "exit_plan_mode", "lsp_hover", "lsp_definition",
    "lsp_references", "notebook_edit", "ask_user", "skill", "diff", "apply_patch",
]
TOOLS = [
    {"type": "function", "function": {
        "name": name,
        "description": f"{name}: operate on the user's workspace. " * 10,
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path"]}}}
    for name in TOOL_NAMES
]


def qwen_code_turn(model: str = "auto") -> dict:
    """Qwen Code 0.23: content parts with cache_control, 22 tools, stream,
    max_tokens 8192, ~27K-token system prompt."""
    return {
        "model": model, "stream": True, "max_tokens": 8192, "temperature": 0.3,
        "tools": TOOLS,
        "messages": [
            {"role": "system", "content": [
                {"type": "text", "text": BIG_SYSTEM, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [
                {"type": "text", "text": "Reply with exactly: ROUTEFORGE_OK"}]},
        ],
    }


def openai_sdk_tool_replay(model: str = "auto") -> dict:
    """openai SDK agent loop, second turn: assistant tool_call (content None)
    and the tool result replayed back."""
    return {
        "model": model, "tools": TOOLS[:3],
        "messages": [
            {"role": "system", "content": "You are a helpful agent."},
            {"role": "user", "content": "Read README.md and summarise it."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "# RouteForge\nA router."},
        ],
    }


def chat_ui_null_content(model: str = "auto") -> dict:
    """A chat UI that sends a trailing user turn with content null (B24)."""
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": None},
        ],
    }


def langchain_parts_chat(model: str = "auto") -> dict:
    """LangChain ChatOpenAI: content parts, temperature 0, no tools."""
    return {
        "model": model, "temperature": 0,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "Be brief."}]},
            {"role": "user", "content": [{"type": "text", "text": "What is the capital of France?"}]},
        ],
    }


def plain_stream_chat(model: str = "auto") -> dict:
    """Any chat UI with streaming on and no tools."""
    return {"model": model, "stream": True,
            "messages": [{"role": "user", "content": "Say hi"}]}


CLIENTS = {
    "qwen_code_turn": qwen_code_turn,
    "openai_sdk_tool_replay": openai_sdk_tool_replay,
    "chat_ui_null_content": chat_ui_null_content,
    "langchain_parts_chat": langchain_parts_chat,
    "plain_stream_chat": plain_stream_chat,
}
STREAM_CLIENTS = frozenset({"qwen_code_turn", "plain_stream_chat"})
