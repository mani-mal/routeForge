from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from app.config.schema import ContextOptimizationCfg, Settings
from app.gateway.backends.base import count_tokens
from app.gateway.gateway import Gateway
from app.models import (
    ChatRequest, Choice, ExecutionPolicyContext, Message, Usage,
)
from app.output.verify import verify
from app.policy.detectors import Detector
from app.policy.effective import EffectiveDetector
from app.policy.gate import Gate
from app.apps.privacy import AppPrivacyProfile, effective_rules
from app.compression import compress
from app.compression.ccr import CCRStore
from app.routing.task_mode import decide as decide_mode

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry


class PipelineRejected(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway, self_port: int,
                 co_cfg_provider: Callable[[], ContextOptimizationCfg],
                 ccr: CCRStore, registry: "ModelRegistry | None" = None) -> None:
        self._s = settings
        self._gate = gate
        self._detector = detector
        self._gateway = gateway
        # Onboarded on-prem models (e.g. local-8b) live only in the registry's
        # effective pool, not the static YAML model_pool -- _display_name must
        # consult it too, or their human-facing name silently falls back to
        # the raw slug (see model-registry-onboarding plan Task 4).
        self._reg = registry
        # Port THIS app is listening on. The agent runtime (multi_step path)
        # drives a real NAT agent whose LLM calls self-call our own internal
        # endpoint at 127.0.0.1:{self._self_port} — see app/agent/runtime.py.
        self._self_port = self_port
        # Step 5 (compression) config, read dynamically on every call instead
        # of a frozen value captured at construction — so a runtime toggle
        # via POST /api/controls (which mutates app.state.co_cfg) takes
        # effect on the NEXT direct-path request without a restart. The
        # internal per-hop endpoint (app/agent/internal_endpoint.py) already
        # reads request.app.state.co_cfg directly; main.py wires this
        # provider to the SAME mutable app.state.co_cfg so both paths share
        # one source of truth. The local, TTL-bounded CCR store used by the
        # direct path — see app/compression/__init__.py, ccr.py.
        self._co_cfg_provider = co_cfg_provider
        self._ccr = ccr

    def _baseline(self, tokens_after: int) -> float:
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return tokens_after / 1_000_000 * (p.input + p.output)

    def _display_name(self, model_slug: str) -> str:
        """Resolve a routing-pool slug (e.g. `local-8b`) to its human-facing
        model name (e.g. `Qwen2.5-7B-Instruct (local)`) for the UI. Falls back
        to the slug when no display name is configured or the slug is unknown
        (e.g. the multi-step `"auto"` sentinel). Reads the registry's merged
        pool (static YAML + enabled onboarded rows) when available, since
        onboarded on-prem models never appear in the static
        `settings.routing.model_pool`."""
        pool = self._reg.effective_pool() if self._reg else self._s.routing.model_pool
        for c in pool:
            if c.name == model_slug:
                return c.display_name or c.name
        return model_slug

    async def run(self, request_id: str, tenant_id: str,
                  request: ChatRequest, app_policy=None,
                  profile: AppPrivacyProfile | None = None) -> dict:
        # Per-app effective detector + rules (Task 7): when the caller supplies
        # the app's privacy profile, layer its custom recognizers on top of the
        # base detector and use its resolved sensitivity rules for BOTH
        # classification (the gate) and egress verification (verify(), both
        # call sites below) -- a custom entity type the base detector can't
        # see must not silently skip the output leakage check either.
        if profile is not None:
            det = EffectiveDetector(self._detector, list(profile.recognizers))
            rules = effective_rules(profile, self._s)
        else:
            det, rules = self._detector, None
        ctx, sanitized, _map = self._gate.evaluate(
            request_id, tenant_id, request, app_policy=app_policy,
            detector=det, rules=rules)
        if ctx.requested_model is not None:
            pool = self._reg.effective_pool() if self._reg else self._s.routing.model_pool
            permitted = {
                c.name for c in pool
                if c.provider in ctx.allowed_providers
                and (not ctx.model_scope_active or c.name in ctx.allowed_models)
                and (self._reg is None or self._reg.is_allowed_for_app(c.name, tenant_id))
                and (c.provider != "onprem" or self._reg is None
                     or self._reg.is_enabled(c.name))
            }
            if ctx.requested_model not in permitted:
                raise PipelineRejected(
                    f"requested model {ctx.requested_model!r} is not permitted or unavailable")
        # NOTE: task-mode is a rules-only stub today. If a real LLM classifier replaces it,
        # it must receive SANITIZED content (not the raw request) — see CLAUDE.md.
        mode = decide_mode(ctx, request)

        san_messages = [Message(role=request.messages[-1].role, content=sanitized)]

        if mode == "multi_step":
            # No outer compress() here: the agent runtime re-enters
            # compression per hop (app/agent/internal_endpoint.py, Task 7 /
            # M3), so an outer pass on san_messages alone would be wasted
            # work and its tokens_after would not reflect the real per-hop
            # pricing basis. san_messages (sanitized, not yet compressed)
            # still goes to the agent — each hop compresses its own view.
            return await self._run_multi_step(request_id, ctx, request, san_messages, det)

        comp = compress(san_messages, ctx, self._co_cfg_provider(), ccr=self._ccr)
        complexity = min(1.0, comp.tokens_after / 2000)

        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint)

        text, vrec = verify(det, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        total = sum(r.actual_cost_usd or 0.0 for r in records)
        # Two-lever accounting (CLAUDE.md: "don't attribute all savings to
        # routing"): baseline_usd prices tokens_before (true all-frontier
        # baseline before either lever runs); compression_saved is the drop
        # from tokens_before->tokens_after at that baseline price;
        # routing_saved is the remaining drop from baseline_after->actual
        # total. With compression off, tokens_before == tokens_after, so
        # baseline_usd is unchanged from before and compression_saved == 0.
        #
        # routing_saved is intentionally NOT clamped to 0: under escalation
        # `total` can exceed `baseline_after` (a costlier tier was chosen on
        # purpose, e.g. a high-risk re-route), and clamping routing_saved to
        # 0 while compression_saved stays positive would break the additive
        # identity compression_saved + routing_saved == baseline_usd - total
        # and show fake positive net savings. compression_saved IS clamped,
        # but harmlessly so — tokens_after <= tokens_before always holds, so
        # it is >= 0 anyway.
        baseline_usd = self._baseline(comp.tokens_before)
        baseline_after = self._baseline(comp.tokens_after)
        compression_saved = max(0.0, baseline_usd - baseline_after)
        routing_saved = baseline_after - total
        trace = [r.model_dump() for r in records] + [vrec.model_dump()]
        trace.append(comp.record.model_dump())

        # Only the SERVED attempt's quality counts here, not every attempt's.
        # Gateway.run appends one step="router" DecisionRecord PER ATTEMPT, so
        # under a confidence escalation (config/routing.yaml max_escalations)
        # `records` holds >1 quality-bearing record for a single response.
        # usage.total_tokens above reflects only the FINAL served attempt, so
        # summing quality across attempts would inflate Token Intelligence
        # Density against a denominator that never grew. Retried low-confidence
        # attempts delivered nothing to the user; their quality isn't "quality
        # delivered". (Contrast with the multi-step path below, where each hop
        # IS a served call and both numerator and token denominator legitimately
        # sum over all hops — do not apply this change there.)
        qrecs = [r for r in records if r.quality_score is not None]
        chosen_q = qrecs[-1] if qrecs else None
        quality_sum = chosen_q.quality_score if chosen_q else 0.0

        return {
            "id": request_id,
            "model": result.model,
            "model_display": self._display_name(result.model),
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            "usage": Usage(prompt_tokens=result.tokens_in,
                           completion_tokens=result.tokens_out,
                           total_tokens=result.tokens_in + result.tokens_out
                           ).model_dump(),
            "trace": trace,
            "cost": {"total_usd": total, "baseline_usd": baseline_usd,
                     "routing_saved_usd": routing_saved,
                     "compression_saved_usd": compression_saved},
            "quality": {"sum": quality_sum,
                        "chosen": chosen_q.quality_score if chosen_q else None,
                        "source": chosen_q.quality_source if chosen_q else None},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities)},
        }

    async def _run_multi_step(self, request_id: str, ctx: ExecutionPolicyContext,
                              request: ChatRequest, san_messages: list[Message],
                              det: Detector) -> dict:
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

        text, vrec = verify(det, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        total = sum(
            h.actual_cost_usd if h.actual_cost_usd is not None else (h.est_cost_usd or 0.0)
            for h in result.hops)
        # Two-lever accounting for multi-step (M3 / Task 7): each hop now
        # re-enters compression itself (app/agent/internal_endpoint.py), so
        # `result.compressions` is 1:1 with `result.hops` instead of a single
        # outer compression of the initial request.
        #
        # baseline_after prices the SAME (post-compression) token basis as
        # `total` above, via each hop's `tokens_priced` — consistent with the
        # direct path's self._baseline(comp.tokens_after).
        baseline_after = sum(self._baseline(h.tokens_priced) for h in result.hops)
        # baseline_before prices each hop's PRE-compression token count — the
        # true all-frontier baseline before either lever runs. Falls back to
        # baseline_after (so compression_saved_usd == 0) only when there are NO
        # compression records at all, i.e. zero hops. (A hop that *bypassed*
        # compression still emits a record with tokens_before == tokens_after,
        # so it contributes equally to both sums and needs no special-casing.)
        if result.compressions:
            baseline_before = sum(self._baseline(cr.tokens_before) for cr in result.compressions)
        else:
            baseline_before = baseline_after
        compression_saved_usd = max(0.0, baseline_before - baseline_after)
        # NOT clamped to 0 — see the direct path's identical rationale above:
        # under escalation `total` can exceed `baseline_after`, and clamping
        # here while compression_saved_usd stays positive would break the
        # additive identity compression_saved_usd + routing_saved_usd ==
        # baseline_usd - total_usd.
        routing_saved_usd = baseline_after - total

        trace = [h.model_dump() for h in result.hops]
        trace += [cr.model_dump() for cr in result.compressions]
        trace.append(vrec.model_dump())

        qhops = [h for h in result.hops if h.quality_score is not None]
        quality_sum = sum(h.quality_score for h in qhops)
        chosen_q = qhops[-1] if qhops else None

        model = result.hops[-1].chosen if result.hops else "auto"
        completion_tokens = count_tokens(result.text)
        # Prompt tokens actually priced across all hops (post-compression) —
        # there is no longer a single outer comp.tokens_after now that each
        # hop compresses its own view.
        prompt_tokens = sum(h.tokens_priced for h in result.hops)

        return {
            "id": request_id,
            "model": model,
            "model_display": self._display_name(model),
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            "usage": Usage(prompt_tokens=prompt_tokens,
                           completion_tokens=completion_tokens,
                           total_tokens=prompt_tokens + completion_tokens
                           ).model_dump(),
            "trace": trace,
            # baseline_usd is reported pre-both-levers (baseline_before), so
            # the headline baseline is consistent with the direct path's
            # baseline_usd (self._baseline(comp.tokens_before)).
            "cost": {"total_usd": total, "baseline_usd": baseline_before,
                     "routing_saved_usd": routing_saved_usd,
                     "compression_saved_usd": compression_saved_usd},
            "quality": {"sum": quality_sum,
                        "chosen": chosen_q.quality_score if chosen_q else None,
                        "source": chosen_q.quality_source if chosen_q else None},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities)},
        }
