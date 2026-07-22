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


def test_multi_step_run_yields_hops(live_internal_server):
    from app.agent.runtime import run

    req = ChatRequest(model="auto", messages=[Message(role="user", content="What is 12 times 7?")])
    result = asyncio.run(run(_ctx(), req, port=live_internal_server.port))

    assert result.hops, "expected >=1 routed hop"
    assert all(h.step == "agent_hop" and h.chosen for h in result.hops)
    assert result.text  # a final answer came back
