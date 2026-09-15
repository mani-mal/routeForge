from app.models import ChatRequest, Message


def test_string_content_unchanged():
    assert Message(role="user", content="hi").content == "hi"


def test_null_content_unchanged():
    assert Message(role="assistant", content=None).content is None


def test_text_parts_flattened_to_string():
    m = Message(role="system", content=[
        {"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "use tools"},
    ])
    assert m.content == "be brief\nuse tools"


def test_non_text_parts_dropped():
    m = Message(role="user", content=[
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "describe"},
    ])
    assert m.content == "describe"


def test_chat_request_accepts_parts_content():
    req = ChatRequest.model_validate({
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    })
    assert req.messages[0].content == "hi"
