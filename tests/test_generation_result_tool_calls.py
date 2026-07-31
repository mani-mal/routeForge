from app.models import GenerationResult


def test_generation_result_carries_tool_calls():
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression": "2+2"}'}}]
    g = GenerationResult(model="local-8b", text="", tokens_in=3, tokens_out=0,
                         latency_ms=12.0, cost_usd=0.0, confidence=0.9, tool_calls=tc)
    assert g.tool_calls == tc


def test_generation_result_tool_calls_defaults_none():
    g = GenerationResult(model="local-8b", text="hi", tokens_in=3, tokens_out=1,
                         latency_ms=12.0, cost_usd=0.0, confidence=0.9)
    assert g.tool_calls is None


def test_litellm_is_importable():
    import litellm  # noqa: F401
