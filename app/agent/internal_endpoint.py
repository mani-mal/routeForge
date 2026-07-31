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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agent.mock_agent import decide_agent_step
from app.agent.openai_sse import sse_final, sse_tool_call
from app.gateway.backends.base import seeded_latency_ms
from app.models import CompressionRecord, DecisionRecord, ExecutionPolicyContext, Message

# Hosts allowed to call the internal endpoint. Real self-calls come from the
# agent runtime's own process via 127.0.0.1 (see app/agent/runtime.py). The
# `TestClient`-based test suite presents host "testclient" (Starlette's
# default ASGI-transport client tuple) — that's safe to allow because it is
# not a network identity a remote caller can forge: TestClient drives the
# ASGI app in-process, never over a real socket, so no external client can
# ever cause `request.client.host` to read "testclient".
_ALLOWED_INTERNAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_loopback(host: str | None) -> bool:
    return host is not None and host in _ALLOWED_INTERNAL_HOSTS


@dataclass
class _RunState:
    ctx: ExecutionPolicyContext
    hop_index: int = 0
    records: list[DecisionRecord] = field(default_factory=list)
    compressions: list[CompressionRecord] = field(default_factory=list)


class HopCollector:
    """Process-global, run-keyed store of per-hop `DecisionRecord`s and
    `CompressionRecord`s.

    The agent runtime calls `start(run_id, ctx)` once before kicking off a
    run, stashing the `ExecutionPolicyContext` so every subsequent hop
    (an internal LLM call re-entering compression and the router) can
    compress/route with it. Each hop calls `record(run_id, rec)` and
    `record_compression(run_id, rec)`; `pop_all(run_id)` drains and returns
    both accumulated lists once the run completes. `pop(run_id)` is kept for
    back-compat (decision records only) where callers don't need
    compressions.
    """

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}

    def start(self, run_id: str, ctx: ExecutionPolicyContext) -> None:
        self._runs[run_id] = _RunState(ctx=ctx)

    def has(self, run_id: str) -> bool:
        return run_id in self._runs

    def context(self, run_id: str) -> ExecutionPolicyContext:
        return self._runs[run_id].ctx

    def next_hop_index(self, run_id: str) -> int:
        state = self._runs[run_id]
        state.hop_index += 1
        return state.hop_index

    def record(self, run_id: str, rec: DecisionRecord) -> None:
        self._runs[run_id].records.append(rec)

    def record_compression(self, run_id: str, rec: CompressionRecord) -> None:
        self._runs[run_id].compressions.append(rec)

    def pop(self, run_id: str) -> list[DecisionRecord]:
        state = self._runs.pop(run_id, None)
        return state.records if state else []

    def pop_all(self, run_id: str) -> tuple[list[DecisionRecord], list[CompressionRecord]]:
        """Atomically drain and delete the run state, returning both the
        per-hop decision records and per-hop compression records. Used by
        the agent runtime instead of `pop()` so compressions aren't lost to
        the same deletion that drains decisions."""
        state = self._runs.pop(run_id, None)
        if state is None:
            return [], []
        return state.records, state.compressions


HOPS = HopCollector()

internal_router = APIRouter(include_in_schema=False)


@internal_router.post("/internal/v1/{run_id}/chat/completions")
async def internal_chat_completions(run_id: str, request: Request):
    # Loopback guard: this route is not part of the customer-facing API and
    # must be unreachable by external/customer clients even though it shares
    # the same 0.0.0.0:8080 listener as the public API (include_in_schema
    # only hides it from OpenAPI, it doesn't restrict who can call it).
    client_host = request.client.host if request.client else None
    if not _is_loopback(client_host):
        raise HTTPException(status_code=403, detail="internal endpoint: loopback only")

    if not HOPS.has(run_id):
        raise HTTPException(status_code=404, detail="unknown run")

    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    tools: list[dict] = body.get("tools", [])

    ctx = HOPS.context(run_id)
    model_router = request.app.state.model_router

    # Local import: avoids an import cycle at module load (app.compression ->
    # ... -> app.agent.internal_endpoint would be circular if imported at
    # top level; app/pipeline.py imports compress() at top level because it
    # has no such cycle back into this module).
    from app.compression import compress

    # M3 must-fix: every LLM call the agent makes must re-enter compression
    # (step 5), not just the outer request — see CLAUDE.md. Compress THIS
    # hop's messages before pricing/routing so tokens_priced is the
    # post-compression count, matching the direct path.
    co_cfg = request.app.state.co_cfg
    ccr = getattr(request.app.state, "ccr", None)
    hop_msgs = [Message(role=m.get("role", "user"), content=m["content"])
                for m in messages if m.get("content")]
    comp = compress(hop_msgs, ctx, co_cfg, ccr=ccr)
    tokens_after = comp.tokens_after
    complexity = min(1.0, tokens_after / 2000)
    decision = model_router.select(ctx, tokens_after, complexity, None)

    hop_index = HOPS.next_hop_index(run_id)
    # Attribute the same per-tier modeled latency tests/_litellm_fake.py's
    # _DeterministicLiteLLM.generate uses — this endpoint fakes the completion
    # and never calls generate, so without this every agent_hop record carries
    # 0 ms and multi-step requests report 0 ms total latency across the console.
    HOPS.record(run_id, DecisionRecord(
        step="agent_hop", chosen=decision.chosen.name,
        chosen_display_name=decision.chosen.display_name, reason=decision.reason,
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        latency_ms=seeded_latency_ms(decision.chosen.tier),
        hop_index=hop_index, tokens_priced=tokens_after))
    HOPS.record_compression(run_id, comp.record)

    # SSE step decision (tool-call vs final-answer) MUST run on the ORIGINAL
    # raw `messages` — tool-call detection needs the raw role/content
    # structure, not the compressed text. Only pricing/routing above use the
    # compressed count.
    step = decide_agent_step(messages, tools)
    if step["kind"] == "tool_call":
        gen = sse_tool_call(
            model=decision.chosen.name, call_id=f"call-{hop_index}",
            tool_name=step["tool_name"], arguments_json=json.dumps(step["arguments"]))
    else:
        gen = sse_final(model=decision.chosen.name, content=step["content"])

    return StreamingResponse(gen, media_type="text/event-stream")
