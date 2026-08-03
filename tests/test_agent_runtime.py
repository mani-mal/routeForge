"""Task 4: the real NAT tool_calling_agent runtime (app/agent/runtime.py).

NAT drives itself over live HTTP against our internal endpoint, so this test
boots a real uvicorn server (see `live_internal_server` fixture in
conftest.py) rather than using a bare TestClient. No pytest-asyncio/anyio
plugin is installed in this repo (see tests/test_backends.py), so the async
runtime is driven with `asyncio.run(...)` inside a sync test, matching the
existing convention.
"""
import asyncio

import pytest

from app.models import ChatRequest, ExecutionPolicyContext, Message

pytest.importorskip("nat")


def _ctx() -> ExecutionPolicyContext:
    return ExecutionPolicyContext(
        request_id="req-1", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_multi_step_run_yields_hops(live_internal_server):
    from app.agent.runtime import run

    req = ChatRequest(model="auto", messages=[Message(role="user", content="What is 12 times 7?")])
    result = asyncio.run(run(_ctx(), req, port=live_internal_server.port))

    assert result.hops, "expected >=1 routed hop"
    assert all(h.step == "agent_hop" and h.chosen for h in result.hops)
    assert result.text  # a final answer came back


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_cap_exceeded_raises_and_leaves_no_hop_state(live_internal_server, monkeypatch):
    """NAT's tool_calling_agent swallows GraphRecursionError internally and
    returns a DEGRADED "could not produce a final answer within N
    iterations" text as a normal answer instead of raising. runtime.run()
    must detect that and raise AgentCapExceeded (fail closed, CLAUDE.md
    rule 5) rather than serve it as a successful AgentResult — and must not
    leak the run's HopCollector entry when it does.

    Forces non-convergence by making the litellm.acompletion fake always emit
    a tool call (never a final answer), against a workflow config whose
    max_iterations has been forced low via a patched `load_settings` (the
    real `config/policy.yaml` stays untouched).
    """
    from app.agent import runtime
    from app.agent.internal_endpoint import HOPS
    from app.agent.runtime import AgentCapExceeded
    from app.config.loader import load_settings as real_load_settings
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_response

    # internal_endpoint.py now calls the real backend (litellm.acompletion)
    # per hop instead of the old scripted `decide_agent_step` — force
    # non-convergence by making that fake ALWAYS return a tool_call (never a
    # final answer), so NAT never stops on its own and the max_steps cap
    # (forced to 2 below) is what raises.
    async def _always_tool_call(**k):
        tools = k.get("tools") or []
        tc = [{"id": "call-1", "type": "function",
               "function": {"name": (tools[0]["function"]["name"] if tools else "calculator"),
                             "arguments": '{"expression": "1 + 1"}'}}]
        return make_response(content=None, tool_calls=tc, prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr(litellm_backend, "acompletion", _always_tool_call)

    settings = real_load_settings()
    settings.policy.agent_budgets = {**settings.policy.agent_budgets, "max_steps": 2}
    monkeypatch.setattr(runtime, "load_settings", lambda: settings)

    req = ChatRequest(model="auto", messages=[Message(role="user", content="loop forever please")])

    with pytest.raises(AgentCapExceeded):
        asyncio.run(runtime.run(_ctx(), req, port=live_internal_server.port))

    assert not HOPS._runs, "HopCollector leaked run state after cap-exceeded raise"
