"""B103: RouteForge Message (OpenAI chat) -> Switchyard request. Pure; no Switchyard needed."""

import pytest

from app.models import Message
from app.routing.switchyard.convert import _Unconvertible, to_switchyard_request


def _call(call_id="c1", name="Write", arguments='{"path": "a.py"}'):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_text_turns_become_text_blocks():
    req = to_switchyard_request(
        [Message(role="user", content="hi"), Message(role="assistant", content="hello")]
    )
    assert req == {
        "instructions": [],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
    }


def test_list_content_parts_are_already_flattened_by_message():
    msg = Message(role="user", content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert to_switchyard_request([msg])["messages"][0]["content"] == [
        {"type": "text", "text": "a\nb"}
    ]


@pytest.mark.parametrize("content", [None, ""])
def test_none_or_empty_content_is_an_empty_block_list(content):
    req = to_switchyard_request([Message(role="assistant", content=content)])
    assert req["messages"] == [{"role": "assistant", "content": []}]


@pytest.mark.parametrize("role", ["system", "developer"])
def test_system_and_developer_go_to_instructions(role):
    req = to_switchyard_request([Message(role=role, content="be brief"), Message(role="user", content="q")])
    assert req["instructions"] == [{"role": role, "content": [{"type": "text", "text": "be brief"}]}]
    assert [m["role"] for m in req["messages"]] == ["user"]


def test_assistant_tool_calls_become_tool_call_blocks_with_parsed_arguments():
    msg = Message(role="assistant", content=None, tool_calls=[_call(), _call("c2", "Bash", '{"command": "ls"}')])
    assert to_switchyard_request([msg])["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_call", "id": "c1", "name": "Write", "arguments": {"path": "a.py"}},
                {"type": "tool_call", "id": "c2", "name": "Bash", "arguments": {"command": "ls"}},
            ],
        }
    ]


def test_assistant_text_is_kept_before_its_tool_calls():
    msg = Message(role="assistant", content="let me look", tool_calls=[_call()])
    blocks = to_switchyard_request([msg])["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "tool_call"]


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ("not json", {"raw": "not json"}),  # upstream parse_arguments behaviour
        (None, {}),
        ({"already": "parsed"}, {"already": "parsed"}),
    ],
)
def test_tool_call_argument_forms(arguments, expected):
    msg = Message(role="assistant", tool_calls=[_call(arguments=arguments)])
    assert to_switchyard_request([msg])["messages"][0]["content"][0]["arguments"] == expected


def test_tool_result_becomes_user_turn_with_tool_result_block():
    msg = Message(role="tool", content="1 failed", tool_call_id="c1", name="Bash")
    assert to_switchyard_request([msg])["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_call_id": "c1",
                    "content": [{"type": "text", "text": "1 failed"}],
                    "is_error": None,
                }
            ],
        }
    ]


def test_tool_result_with_no_content_has_empty_blocks():
    msg = Message(role="tool", content=None, tool_call_id="c1")
    assert to_switchyard_request([msg])["messages"][0]["content"][0]["content"] == []


@pytest.mark.parametrize(
    "msg",
    [
        Message(role="function", content="x"),  # legacy OpenAI role
        Message(role="banana", content="x"),
        Message(role="tool", content="x"),  # no tool_call_id
        Message(role="tool", content="x", tool_call_id=""),
        Message(role="user", content="x", tool_calls=[_call()]),  # calls on a user turn
        Message(role="assistant", tool_calls=[{"id": "c1", "function": {"arguments": "{}"}}]),  # no name
        Message(role="assistant", tool_calls=[{"function": {"name": "Write"}}]),  # no id
        Message(role="assistant", tool_calls=[{"id": "c1"}]),  # no function
        Message.model_construct(role="assistant", content=None, tool_calls=["junk"]),
        Message(role="assistant", tool_calls=[_call(arguments=42)]),
    ],
)
def test_unconvertible_messages_raise(msg):
    with pytest.raises(_Unconvertible):
        to_switchyard_request([msg])


def test_non_message_input_raises():
    with pytest.raises(_Unconvertible):
        to_switchyard_request([{"role": "user", "content": "x"}])


def test_non_string_content_raises():
    msg = Message.model_construct(role="user", content=123)
    with pytest.raises(_Unconvertible):
        to_switchyard_request([msg])


def test_unconvertible_reason_carries_no_message_text():
    secret = "SSN 123-45-6789"
    with pytest.raises(_Unconvertible) as info:
        to_switchyard_request([Message(role="tool", content=secret)])
    assert secret not in str(info.value)


def test_request_has_only_instructions_and_messages():
    req = to_switchyard_request([Message(role="user", content="x")])
    assert set(req) == {"instructions", "messages"}
