from __future__ import annotations

from typing import AsyncIterator, Callable, TYPE_CHECKING

from app.config.schema import ContextOptimizationCfg, Settings
from app.errors import PolicyGateError
from app.gateway.backends.base import count_tokens
from app.gateway.gateway import Gateway
from app.models import (
    ChatRequest, Choice, ExecutionPolicyContext, GenerationResult, MemoryRecord,
    Message, Usage, VerificationRecord,
)
from app.output.verify import verify
from app.policy.detectors import Detector, make_detector
from app.policy.effective import EffectiveDetector
from app.policy.gate import Gate
from app.apps.privacy import AppPrivacyProfile, effective_rules
from app.compression import compress
from app.compression.ccr import CCRStore
from app.memory import recall_memory, write_memory
from app.memory.store import MemoryStore
from app.routing.task_mode import decide as decide_mode

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry


class PipelineRejected(Exception):
    pass


class StreamFallback(Exception):
    """Raised by `Pipeline.run_stream` when a request MUST be verified as a
    complete response and therefore cannot be streamed unverified. The caller
    (the chat route, Tasks 6-7) catches this and falls back to
    `verified_then_stream` (buffer the full answer, run step-9 verification,
    then chunk the finished text).

    Raised in exactly ONE place -- `run_stream` -- for exactly two reasons, both
    the privacy backstop from Global Constraint 1:
      * the request routes `multi_step` (streaming is direct-path only), or
      * `ctx.onprem_only` or `ctx.require_verification` is set (a residency- or
        risk-floored request is never streamed without a complete-response
        verification).
    """
    pass


class Pipeline:
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway, self_port: int,
                 co_cfg_provider: Callable[[], ContextOptimizationCfg],
                 ccr: CCRStore, registry: "ModelRegistry | None" = None,
                 memory: MemoryStore | None = None) -> None:
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
        # Persistent memory (Feature 5, Task 2): parallels self._ccr above --
        # a single shared, always-constructed store (app/main.py builds one
        # `DeterministicMemoryStore` regardless of the flag, like CCRStore),
        # but only READ/WRITTEN when `settings.serving.memory.enabled`. No
        # runtime toggle endpoint exists for memory (unlike co_cfg_provider,
        # which tracks POST /api/controls), so the config is read directly
        # off `self._s` rather than through a mutable provider callback.
        self._memory = memory
        # Per-app pii_detection override (Task 3): an app may select a
        # cheaper/weaker base detector ("off"/"regex") than the process-wide
        # global one. Presidio is expensive to construct (loads a spaCy
        # model), so non-global modes are built lazily on first use and
        # memoized here for the life of the process -- never rebuilt per
        # request. The global instance is seeded into the memo up front so
        # `_detector_for` never constructs a second, redundant instance of
        # the mode the process already booted with.
        self._global_pii_mode = settings.serving.pii_detection
        self._detectors: dict[str, Detector] = {self._global_pii_mode: self._detector}

    def _detector_for(self, mode: str | None) -> Detector:
        """Resolve the base detector for an app's `pii_detection` override.

        `None` (no override) or a mode matching the global config both return
        the SAME global `self._detector` instance -- no new object, no
        redundant warmup. A distinct mode is built via `make_detector`,
        warmed up once, cached in `self._detectors`, and that same cached
        instance is returned on every subsequent call for that mode. This
        residency-independent selection only ever changes WHAT is detected;
        the onprem_only/allowed_providers floor is computed from
        `app_policy.residency` in `app/policy/context.py` and never consults
        `pii_detection` at all -- an app set to "off" cannot leak a cloud
        provider into `allowed_providers` by weakening detection (see the
        invariant test in tests/test_pii_detection_pipeline.py).
        """
        if mode is None or mode == self._global_pii_mode:
            return self._detector
        cached = self._detectors.get(mode)
        if cached is not None:
            return cached
        built = make_detector(mode)
        built.warmup()
        self._detectors[mode] = built
        return built

    def _baseline(self, in_tokens: int, out_tokens: int) -> float:
        """All-frontier baseline: input and output priced at the baseline
        (most-expensive frontier) model's respective rates — the SAME token
        basis `total` uses, so routing_saved compares like with like (B12)."""
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return (in_tokens * p.input + out_tokens * p.output) / 1_000_000

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
                  profile: AppPrivacyProfile | None = None,
                  session_id: str | None = None) -> dict:
        # Per-app effective detector + rules (Task 7): when the caller supplies
        # the app's privacy profile, layer its custom recognizers on top of the
        # base detector and use its resolved sensitivity rules for BOTH
        # classification (the gate) and egress verification (verify(), both
        # call sites below) -- a custom entity type the base detector can't
        # see must not silently skip the output leakage check either.
        #
        # Per-app pii_detection override (Task 3): the BASE detector being
        # layered is now the app's selected mode (`_detector_for`), not
        # unconditionally the global `self._detector` -- an app set to
        # "off"/"regex" actually gets that detection mode instead of always
        # inheriting the process-wide default.
        try:
            base = self._detector_for(app_policy.pii_detection if app_policy else None)
            if profile is not None:
                det = EffectiveDetector(base, list(profile.recognizers))
                rules = effective_rules(profile, self._s)
            else:
                det, rules = base, None
            ctx, sanitized, _map = self._gate.evaluate(
                request_id, tenant_id, request, app_policy=app_policy,
                detector=det, rules=rules)
        except PolicyGateError:
            raise
        except Exception as e:
            raise PolicyGateError("policy evaluation failed") from e
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

        # Memory READ (Feature 5, Task 2): after san_messages (post-gate,
        # sanitized) and before compress() -- recalled context is prepended
        # so it gets compressed + priced like any other request tokens, no
        # separate untracked token path. Runs for BOTH the direct and
        # multi_step branches below, since both consume `san_messages`.
        #
        # Partition key note: `run()` is never handed a distinct
        # application_id -- "the application id equals the tenant slug"
        # (CLAUDE.md/routes.py._authenticate), so `tenant_id` doubles as
        # `application_id` here. `session_id` is the per-session partition key
        # (`x-routeforge-session`, app/api/routes.py::_read_session_id),
        # threaded in by the caller; a request with no session header (or an
        # unsanitizable one) falls back to `session_id or ""`, the shared
        # partition -- so distinct end-user sessions of one app no longer
        # collide, and no session header keeps the old (pre-session) behavior.
        mem_cfg = self._s.serving.memory
        recall = recall_memory(self._memory, mem_cfg, ctx, san_messages,
                               tenant_id=tenant_id, application_id=tenant_id,
                               session_id=session_id or "")
        if recall.messages:
            san_messages = recall.messages + san_messages

        if mode == "multi_step":
            # No outer compress() here: the agent runtime re-enters
            # compression per hop (app/agent/internal_endpoint.py, Task 7 /
            # M3), so an outer pass on san_messages alone would be wasted
            # work and its tokens_after would not reflect the real per-hop
            # pricing basis. san_messages (sanitized, not yet compressed)
            # still goes to the agent — each hop compresses its own view.
            return await self._run_multi_step(request_id, ctx, request, san_messages, det,
                                              recall.record)

        comp = compress(san_messages, ctx, self._co_cfg_provider(), ccr=self._ccr)
        complexity = min(1.0, comp.tokens_after / 2000)

        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint)

        text, vrec = verify(det, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        # Memory WRITE (Feature 5, Task 2): success path ONLY (a rejected/
        # failed request writes nothing -- unreachable above since a failed
        # verify already raised). R1 sanitized-only: built from `sanitized`
        # (the gate's redacted input) and `text` (the post-verify output)
        # ONLY -- never `result.text` (pre-verify raw), `raw_completion`, or
        # the raw request. See write_memory()'s docstring for the contract.
        write_memory(self._memory, mem_cfg, ctx, sanitized, text,
                    tenant_id=tenant_id, application_id=tenant_id,
                    session_id=session_id or "")

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
        baseline_usd = self._baseline(comp.tokens_before, result.tokens_out)
        baseline_after = self._baseline(comp.tokens_after, result.tokens_out)
        compression_saved = max(0.0, baseline_usd - baseline_after)
        routing_saved = baseline_after - total
        trace = [r.model_dump() for r in records] + [vrec.model_dump()]
        trace.append(comp.record.model_dump())
        if recall.record is not None:
            trace.append(recall.record.model_dump())

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
            "sanitized_prompt": sanitized,
            "raw_completion": result.text,
        }

    async def run_stream(self, request_id: str, tenant_id: str,
                         request: ChatRequest, app_policy=None,
                         profile: AppPrivacyProfile | None = None,
                         session_id: str | None = None
                         ) -> tuple[AsyncIterator[str], Callable[[], dict]]:
        """Real backend token-streaming substrate for the direct (single-shot)
        path (Tasks 6-7 wire the route to this). Skips step-9 verification --
        the verification marker in the final metadata is "waived".

        Interface (documented design choice): `run_stream` is a coroutine that
        eagerly runs the gate, task-mode decision, and the runtime floor, then
        returns `(deltas, get_meta)`:
          * `deltas` -- an async generator yielding ordered token-delta `str`s.
          * `get_meta` -- a zero-arg callable returning the final metadata dict
            (records/trace, two-lever cost, tokens, model, ctx-derived policy,
            `sanitized_prompt`, `raw_completion`, and the "waived" verification
            marker). It is populated only once `deltas` is fully consumed, and
            raises if called before then.

        Why a coroutine returning a pair (not a bare async generator): the gate
        and the runtime floor MUST run BEFORE any token is emitted, so that a
        `StreamFallback` (multi_step, onprem_only, or require_verification) is
        raised eagerly -- before the caller has committed to a StreamingResponse
        -- letting the route fall back to `verified_then_stream` cleanly. A bare
        async generator would defer those checks to first-iteration, after the
        response has started.

        Nothing external sees the raw request before the gate: the gate (with
        the per-app `_detector_for` base detector + optional profile recognizers,
        mirroring `run()`) runs first; compression runs strictly after it.
        """
        # Gate first -- identical composition to run() (per-app pii_detection
        # base detector + optional profile recognizers/rules; fail closed).
        try:
            base = self._detector_for(app_policy.pii_detection if app_policy else None)
            if profile is not None:
                det = EffectiveDetector(base, list(profile.recognizers))
                rules = effective_rules(profile, self._s)
            else:
                det, rules = base, None
            ctx, sanitized, _map = self._gate.evaluate(
                request_id, tenant_id, request, app_policy=app_policy,
                detector=det, rules=rules)
        except PolicyGateError:
            raise
        except Exception as e:
            raise PolicyGateError("policy evaluation failed") from e

        # Streaming is the direct/single-shot path only.
        mode = decide_mode(ctx, request)
        if mode == "multi_step":
            raise StreamFallback(
                "multi_step request cannot stream unverified; verify the "
                "complete agent response")
        # Runtime floor (Global Constraint 1) -- the privacy backstop, in ONE
        # place: an onprem_only or verification-required request must be verified
        # as a COMPLETE response, never streamed unverified.
        if ctx.onprem_only or ctx.require_verification:
            raise StreamFallback(
                "residency/verification floor: response must be verified as a "
                "complete response before egress")

        san_messages = [Message(role=request.messages[-1].role, content=sanitized)]
        # Memory READ only (Feature 5, Task 2) -- mirrors run()'s hook. NEVER
        # a WRITE on this path: step-9 verify() never runs while streaming
        # (waived, by design -- see this method's docstring), so there is no
        # verified output to build a sanitized-only summary from. Writing
        # `result.text` here would be writing un-verified content, which R1 /
        # this task's "never on any path" rule forbids.
        mem_cfg = self._s.serving.memory
        recall = recall_memory(self._memory, mem_cfg, ctx, san_messages,
                               tenant_id=tenant_id, application_id=tenant_id,
                               session_id=session_id or "")
        if recall.messages:
            san_messages = recall.messages + san_messages
        comp = compress(san_messages, ctx, self._co_cfg_provider(), ccr=self._ccr)
        complexity = min(1.0, comp.tokens_after / 2000)
        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        stream, records = await self._gateway.run_stream(
            ctx, comp.messages, comp.tokens_after, complexity, hint)

        meta: dict = {}

        async def _deltas() -> AsyncIterator[str]:
            result: GenerationResult | None = None
            async for chunk in stream:
                if chunk.delta is not None:
                    yield chunk.delta
                if chunk.final is not None:
                    result = chunk.final
            if result is None:
                raise PipelineRejected("stream ended without a final result")
            meta.update(self._assemble_stream_meta(
                request_id, ctx, sanitized, comp, records, result, recall.record))

        def get_meta() -> dict:
            if not meta:
                raise RuntimeError(
                    "run_stream metadata is available only after the delta "
                    "stream is fully consumed")
            return meta

        # Surface the EFFECTIVE detector + ctx the gate just produced so a
        # streaming verifier (incremental_verify, Task 7) can redact egress
        # tokens with the SAME per-app detector this request used at intake --
        # not a global one, which would make intake/egress inconsistent for an
        # app with a custom `pii_detection` or custom recognizers. Both are
        # known eagerly HERE (before any delta is consumed), unlike the meta
        # dict which is only populated once `_deltas()` is drained -- so they
        # are attached to `get_meta` rather than returned via it. Attribute
        # access keeps `run_stream`'s `(deltas, get_meta)` contract unchanged
        # for the `stream_unverified` caller (Task 6).
        get_meta.detector = det   # type: ignore[attr-defined]
        get_meta.ctx = ctx        # type: ignore[attr-defined]
        return _deltas(), get_meta

    def _assemble_stream_meta(self, request_id: str, ctx: ExecutionPolicyContext,
                              sanitized: str, comp, records, result: GenerationResult,
                              mem_record: MemoryRecord | None = None) -> dict:
        """Assemble the final streaming metadata dict, mirroring run()'s record,
        two-lever cost, and quality assembly. Step-9 verification is WAIVED on
        this path: a VerificationRecord(performed=False, passed=True) is recorded
        in the trace so the "why" stays visible (Global Constraint 2), and the
        top-level `verification` marker is "waived" (Tasks 6/7 refine it)."""
        total = sum(r.actual_cost_usd or 0.0 for r in records)
        # Same two-lever accounting as run(): baseline prices tokens_before
        # (pre-both-levers), compression_saved is the tokens_before->tokens_after
        # drop at baseline price, routing_saved is the remaining drop to actual
        # total. routing_saved is intentionally NOT clamped (see run()).
        baseline_usd = self._baseline(comp.tokens_before, result.tokens_out)
        baseline_after = self._baseline(comp.tokens_after, result.tokens_out)
        compression_saved = max(0.0, baseline_usd - baseline_after)
        routing_saved = baseline_after - total

        vrec = VerificationRecord(
            required=ctx.require_verification, performed=False, passed=True,
            entities_found=(), reason="waived: streaming (unverified)")
        trace = [r.model_dump() for r in records] + [vrec.model_dump()]
        trace.append(comp.record.model_dump())
        if mem_record is not None:
            trace.append(mem_record.model_dump())

        qrecs = [r for r in records if r.quality_score is not None]
        chosen_q = qrecs[-1] if qrecs else None
        quality_sum = chosen_q.quality_score if chosen_q else 0.0

        return {
            "id": request_id,
            "model": result.model,
            "model_display": self._display_name(result.model),
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
            "sanitized_prompt": sanitized,
            "raw_completion": result.text,
            # Step-9 verification marker: "waived" this task. Tasks 6/7 refine to
            # "waived" (stream_unverified) / "incremental" (incremental_verify).
            "verification": "waived",
        }

    async def _run_multi_step(self, request_id: str, ctx: ExecutionPolicyContext,
                              request: ChatRequest, san_messages: list[Message],
                              det: Detector,
                              mem_record: MemoryRecord | None = None) -> dict:
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

        # Memory WRITE is intentionally NOT wired for multi-step (Task 2 scope
        # decision -- see task-2-report.md): unlike the direct path, there is
        # no single (sanitized, verified) exchange here to summarize -- the
        # agent may run several hops, each with its own tool I/O, and
        # `sanitized`/`text` are not threaded into this method. READ still
        # runs (san_messages, above, was already prepended with recalled
        # context in run() before this method was called) -- only WRITE is
        # deferred. Never writes raw/un-verified content on this path.
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
        # direct path's self._baseline(comp.tokens_after, result.tokens_out).
        baseline_after = sum(self._baseline(h.tokens_priced, h.tokens_out)
                             for h in result.hops)
        # baseline_before prices each hop's PRE-compression token count — the
        # true all-frontier baseline before either lever runs. Falls back to
        # baseline_after (so compression_saved_usd == 0) only when there are NO
        # compression records at all, i.e. zero hops. (A hop that *bypassed*
        # compression still emits a record with tokens_before == tokens_after,
        # so it contributes equally to both sums and needs no special-casing.)
        # baseline_before pairs each compression's pre-compression input with its
        # hop's output (compressions are 1:1 with hops; see comment above).
        if result.compressions:
            baseline_before = sum(
                self._baseline(cr.tokens_before, h.tokens_out)
                for cr, h in zip(result.compressions, result.hops))
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
        if mem_record is not None:
            trace.append(mem_record.model_dump())

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
            # baseline_usd (self._baseline(comp.tokens_before, result.tokens_out)).
            "cost": {"total_usd": total, "baseline_usd": baseline_before,
                     "routing_saved_usd": routing_saved_usd,
                     "compression_saved_usd": compression_saved_usd},
            "quality": {"sum": quality_sum,
                        "chosen": chosen_q.quality_score if chosen_q else None,
                        "source": chosen_q.quality_source if chosen_q else None},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities)},
            "sanitized_prompt": san_messages[-1].content if san_messages else "",
            "raw_completion": result.text,
        }
