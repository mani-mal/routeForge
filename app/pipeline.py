from __future__ import annotations

from app.config.schema import Settings
from app.gateway.backends.base import count_tokens
from app.gateway.gateway import Gateway
from app.models import (
    ChatRequest, Choice, ExecutionPolicyContext, Message, Usage,
)
from app.output.verify import verify
from app.policy.detectors import Detector
from app.policy.gate import Gate
from app.compression import compress
from app.routing.task_mode import decide as decide_mode


class PipelineRejected(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway, self_port: int) -> None:
        self._s = settings
        self._gate = gate
        self._detector = detector
        self._gateway = gateway
        # Port THIS app is listening on. The agent runtime (multi_step path)
        # drives a real NAT agent whose LLM calls self-call our own internal
        # endpoint at 127.0.0.1:{self._self_port} — see app/agent/runtime.py.
        self._self_port = self_port

    def _baseline(self, tokens_after: int) -> float:
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return tokens_after / 1_000_000 * (p.input + p.output)

    async def run(self, request_id: str, tenant_id: str,
                  request: ChatRequest) -> dict:
        ctx, sanitized, _map = self._gate.evaluate(request_id, tenant_id, request)
        # NOTE: task-mode is a rules-only stub today. If a real LLM classifier replaces it,
        # it must receive SANITIZED content (not the raw request) — see CLAUDE.md.
        mode = decide_mode(ctx, request)

        san_messages = [Message(role=request.messages[-1].role, content=sanitized)]
        comp = compress(san_messages, ctx)

        if mode == "multi_step":
            return await self._run_multi_step(request_id, ctx, request, san_messages, comp)

        complexity = min(1.0, comp.tokens_after / 2000)

        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint)

        text, vrec = verify(self._detector, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        total = sum(r.actual_cost_usd or 0.0 for r in records)
        baseline = self._baseline(comp.tokens_after)
        trace = [r.model_dump() for r in records] + [vrec.model_dump()]
        trace.append({"step": "compression", **comp.record})

        return {
            "id": request_id,
            "model": result.model,
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            "usage": Usage(prompt_tokens=result.tokens_in,
                           completion_tokens=result.tokens_out,
                           total_tokens=result.tokens_in + result.tokens_out
                           ).model_dump(),
            "trace": trace,
            "cost": {"total_usd": total, "baseline_usd": baseline,
                     "routing_saved_usd": max(0.0, baseline - total)},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities)},
        }

    async def _run_multi_step(self, request_id: str, ctx: ExecutionPolicyContext,
                              request: ChatRequest, san_messages: list[Message],
                              comp) -> dict:
        # Lazy import: nat is an optional extra; importing app.pipeline must not
        # require it to be installed (mirrors app/agent/runtime.py's own lazy
        # import of the nat package itself).
        from app.agent.runtime import run as agent_run

        # The agent gets SANITIZED content only, never the raw request — the
        # gate has already redacted it above; this mirrors the direct path's
        # san_messages and keeps CLAUDE.md rule 1/2 (nothing downstream of the
        # gate sees raw PII) true for multi-step too.
        sanitized_request = request.model_copy(update={"messages": san_messages})
        # AgentCapExceeded (fail closed on cap-exceed) is intentionally not
        # caught here: it propagates to the route, which maps any
        # non-PipelineRejected exception to a handled error response.
        result = await agent_run(ctx, sanitized_request, port=self._self_port)

        text, vrec = verify(self._detector, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        total = sum(
            h.actual_cost_usd if h.actual_cost_usd is not None else (h.est_cost_usd or 0.0)
            for h in result.hops)
        # Per-hop token counts aren't carried on DecisionRecord, so the
        # all-frontier baseline is computed over the final answer's tokens
        # (acceptable per design — see task-5 brief) rather than summed hop
        # tokens.
        baseline = self._baseline(count_tokens(result.text))
        trace = [h.model_dump() for h in result.hops] + [vrec.model_dump()]
        trace.append({"step": "compression", **comp.record})

        model = result.hops[-1].chosen if result.hops else "auto"
        completion_tokens = count_tokens(result.text)

        return {
            "id": request_id,
            "model": model,
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            "usage": Usage(prompt_tokens=comp.tokens_after,
                           completion_tokens=completion_tokens,
                           total_tokens=comp.tokens_after + completion_tokens
                           ).model_dump(),
            "trace": trace,
            "cost": {"total_usd": total, "baseline_usd": baseline,
                     "routing_saved_usd": max(0.0, baseline - total)},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities)},
        }
