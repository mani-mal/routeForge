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

from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agent.openai_sse import sse_final, sse_tool_call
from app.models import CompressionRecord, DecisionRecord, ExecutionPolicyContext, Message
from app.obs import metrics
from app.routing.signals import derive_signal_tier

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
    # Refresh the registry the same way the public /v1/chat/completions route
    # does (app/api/routes.py) -- without this, an onboarded on-prem model
    # (which lives only in the registry, never in the static YAML pool) is
    # invisible to every hop's routing decision, silently pushing multi-step
    # traffic onto cloud tiers regardless of onprem_only/residency.
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        await registry.ensure_loaded(request.app.state.db)
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
    hop_index = HOPS.next_hop_index(run_id)
    settings = request.app.state.settings
    stage_cfg = settings.routing.stage_signals
    sig = derive_signal_tier(messages, hop_index, stage_cfg) if stage_cfg.enabled else None
    signal_tier = sig.min_tier if sig else None
    with metrics.time_routing():
        decision = model_router.select(ctx, tokens_after, complexity, signal_tier)

    backend = request.app.state.backend
    price = (registry.price_for(decision.chosen.name) if registry
             else settings.pricing.models[decision.chosen.name])
    # The model must receive the COMPRESSED content, not the raw hop_msgs —
    # compress() returns a NEW list and never mutates its input, so passing
    # hop_msgs here would send uncompressed text to the backend while
    # tokens_priced/tokens_after (used for routing/pricing bookkeeping above)
    # reflect the compressed count. That divergence breaks the two-lever cost
    # story and CLAUDE.md's "router must price post-compression tokens; the
    # model gets compressed content" rule. Matches the direct path
    # (app/pipeline.py passes comp.messages into Gateway.run -> backend.generate).
    gen = await backend.generate(
        decision.chosen.name, comp.messages, price, decision.chosen.tier, {}, tools=tools)

    HOPS.record(run_id, DecisionRecord(
        step="agent_hop", chosen=decision.chosen.name,
        chosen_display_name=decision.chosen.display_name,
        chosen_tier=decision.chosen.tier, chosen_provider=decision.chosen.provider,
        reason=(f"{decision.reason} · {sig.reason}" if sig and sig.reason
                else decision.reason),
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        actual_cost_usd=gen.cost_usd,        # REAL per-hop cost (was None + seeded latency)
        latency_ms=gen.latency_ms,           # REAL latency (no more seeded_latency_ms)
        hop_index=hop_index, tokens_priced=tokens_after, tokens_out=gen.tokens_out,
        quality_score=decision.quality_score,
        quality_source=decision.quality_source))
    HOPS.record_compression(run_id, comp.record)
    metrics.observe_routing_decision(decision.chosen.tier, escalated=False)

    # Per-hop = ONE real call, no escalation/fallback here — escalation stays a
    # direct-path (Gateway.run) feature, unchanged from the previously-faked
    # multi-step behaviour. Serialize the REAL completion to SSE: a tool_calls
    # message drives NAT's tool execution; otherwise it's the final answer.
    if gen.tool_calls:
        first = gen.tool_calls[0]
        stream = sse_tool_call(
            model=decision.chosen.name, call_id=first.get("id", f"call-{hop_index}"),
            tool_name=first["function"]["name"], arguments_json=first["function"]["arguments"])
    else:
        stream = sse_final(model=decision.chosen.name, content=gen.text)
    return StreamingResponse(stream, media_type="text/event-stream")
