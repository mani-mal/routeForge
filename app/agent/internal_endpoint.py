"""Internal router-backed OpenAI SSE endpoint for the agent runtime
(Phase 5, Task 2).

The NeMo Agent Toolkit agent runs as a real LangChain `ChatOpenAI` client
whose base_url points at this endpoint: every LLM call the agent makes must
re-enter our router (step 6) so each hop gets its own routing decision and
`DecisionRecord`, not just the outer request.

The agent runtime stashes the request's `ExecutionPolicyContext` in the
module-global `HOPS` collector before starting the agent (`HOPS.start(run_id,
ctx)`); this route looks it up per hop so residency (e.g. `onprem_only`)
holds for every call the agent makes, not just the first one.

Not part of the customer-facing API: `include_in_schema=False` keeps it out
of the public OpenAPI surface, and it is loopback-only in practice (only the
agent runtime process calls it).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.agent.mock_agent import decide_agent_step
from app.agent.openai_sse import sse_final, sse_tool_call
from app.gateway.backends.base import count_tokens
from app.models import DecisionRecord, ExecutionPolicyContext


@dataclass
class _RunState:
    ctx: ExecutionPolicyContext
    hop_index: int = 0
    records: list[DecisionRecord] = field(default_factory=list)


class HopCollector:
    """Process-global, run-keyed store of per-hop `DecisionRecord`s.

    The agent runtime calls `start(run_id, ctx)` once before kicking off a
    run, stashing the `ExecutionPolicyContext` so every subsequent hop
    (an internal LLM call re-entering the router) can route with it. Each
    hop calls `record(run_id, rec)`; `pop(run_id)` drains and returns the
    accumulated records once the run completes.
    """

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}

    def start(self, run_id: str, ctx: ExecutionPolicyContext) -> None:
        self._runs[run_id] = _RunState(ctx=ctx)

    def context(self, run_id: str) -> ExecutionPolicyContext:
        return self._runs[run_id].ctx

    def next_hop_index(self, run_id: str) -> int:
        state = self._runs[run_id]
        state.hop_index += 1
        return state.hop_index

    def record(self, run_id: str, rec: DecisionRecord) -> None:
        self._runs[run_id].records.append(rec)

    def pop(self, run_id: str) -> list[DecisionRecord]:
        state = self._runs.pop(run_id, None)
        return state.records if state else []


HOPS = HopCollector()

internal_router = APIRouter(include_in_schema=False)


@internal_router.post("/internal/v1/{run_id}/chat/completions")
async def internal_chat_completions(run_id: str, request: Request):
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    tools: list[dict] = body.get("tools", [])

    ctx = HOPS.context(run_id)
    model_router = request.app.state.model_router

    tokens_after = sum(count_tokens(m["content"]) for m in messages if m.get("content"))
    complexity = min(1.0, tokens_after / 2000)
    decision = model_router.select(ctx, tokens_after, complexity, None)

    hop_index = HOPS.next_hop_index(run_id)
    HOPS.record(run_id, DecisionRecord(
        step="agent_hop", chosen=decision.chosen.name, reason=decision.reason,
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        hop_index=hop_index, tokens_priced=tokens_after))

    step = decide_agent_step(messages, tools)
    if step["kind"] == "tool_call":
        gen = sse_tool_call(
            model=decision.chosen.name, call_id=f"call-{hop_index}",
            tool_name=step["tool_name"], arguments_json=json.dumps(step["arguments"]))
    else:
        gen = sse_final(model=decision.chosen.name, content=step["content"])

    return StreamingResponse(gen, media_type="text/event-stream")
