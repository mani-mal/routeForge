import json
from app.agent.openai_sse import sse_tool_call, sse_final
from app.agent.mock_agent import decide_agent_step

def _parse(chunks):
    events = []
    for line in "".join(chunks).splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        events.append(body if body == "[DONE]" else json.loads(body))
    return events

def test_sse_tool_call_shape():
    ev = _parse(sse_tool_call(model="local-8b", call_id="call_1",
                              tool_name="calculator", arguments_json='{"expression": "12 * 7"}'))
    assert ev[-1] == "[DONE]"
    first = ev[0]
    assert first["object"] == "chat.completion.chunk"
    tc = first["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "calculator"
    assert json.loads(tc["function"]["arguments"]) == {"expression": "12 * 7"}
    assert any(e != "[DONE]" and e["choices"][0].get("finish_reason") == "tool_calls" for e in ev)

def test_sse_final_shape():
    ev = _parse(sse_final(model="local-8b", content="The answer is 84."))
    assert ev[-1] == "[DONE]"
    assert "".join(e["choices"][0]["delta"].get("content", "")
                   for e in ev if e != "[DONE]") == "The answer is 84."
    assert any(e != "[DONE]" and e["choices"][0].get("finish_reason") == "stop" for e in ev)

def test_decide_tool_call_then_final():
    tools = [{"type": "function", "function": {"name": "calculator",
              "parameters": {"properties": {"expression": {"type": "string"}}}}}]
    step1 = decide_agent_step([{"role": "user", "content": "What is 12 times 7?"}], tools)
    assert step1["kind"] == "tool_call" and step1["tool_name"] == "calculator"
    step2 = decide_agent_step(
        [{"role": "user", "content": "What is 12 times 7?"},
         {"role": "assistant", "tool_calls": [{"id": "c1"}]},
         {"role": "tool", "content": "Result: 84.0", "tool_call_id": "c1"}], tools)
    assert step2["kind"] == "final" and "84" in step2["content"]
