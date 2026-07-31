import json
from app.agent.openai_sse import sse_tool_call, sse_final

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
