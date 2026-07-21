from __future__ import annotations

from app.config.schema import Settings
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
                 gateway: Gateway) -> None:
        self._s = settings
        self._gate = gate
        self._detector = detector
        self._gateway = gateway

    def _baseline(self, tokens_after: int) -> float:
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return tokens_after / 1_000_000 * (p.input + p.output)

    async def run(self, request_id: str, tenant_id: str,
                  request: ChatRequest) -> dict:
        ctx, sanitized, _map = self._gate.evaluate(request_id, tenant_id, request)
        # NOTE: task-mode is a rules-only stub today. If a real LLM classifier replaces it,
        # it must receive SANITIZED content (not the raw request) — see CLAUDE.md.
        _mode = decide_mode(ctx, request)

        san_messages = [Message(role=request.messages[-1].role, content=sanitized)]
        comp = compress(san_messages, ctx)
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
