from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Callable, TYPE_CHECKING

from app.cache.store import CacheEntry, DeterministicSemanticCache
from app.config.schema import ContextOptimizationCfg, Settings
from app.errors import PolicyGateError
from app.gateway.backends.base import count_tokens
from app.gateway.gateway import Gateway
from app.models import (
    CacheRecord, ChatRequest, Choice, CompressionRecord, DecisionRecord,
    ExecutionPolicyContext, GenerationResult, MemoryRecord,
    Message, Usage, VerificationRecord,
)
from app.output.verify import verify
from app.policy.context import pinned_model_reason, registry_permits
from app.policy.detectors import Detector, make_detector
from app.policy.effective import EffectiveDetector, FilteredDetector
from app.policy.gate import Gate, redact_tool_calls
from app.apps.privacy import AppPrivacyProfile, effective_rules
from app.compression import CompressionResult, compress
from app.compression.ccr import CCRStore
from app.memory import RecallResult, recall_memory, write_memory
from app.memory.store import MemoryStore
from app.routing.task_mode import decide as decide_mode
from app.routing.task_mode_llm import classify_task_mode

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

log = logging.getLogger("serving.cache")


class PipelineRejected(Exception):
    pass


class StreamFallback(Exception):
    """Raised by `Pipeline.run_stream` when a request MUST be verified as a
    complete response and therefore cannot be streamed unverified. The caller
    (the chat route, Tasks 6-7) catches this and falls back to
    `verified_then_stream` (buffer the full answer, run step-9 verification,
    then chunk the finished text).

    Raised in exactly ONE place -- `run_stream` -- for three reasons. Two are
    the privacy backstop from Global Constraint 1:
      * the request routes `multi_step` (streaming is direct-path only), or
      * `ctx.onprem_only` or `ctx.require_verification` is set (a residency- or
        risk-floored request is never streamed without a complete-response
        verification).
    The third is structural: a client-driven tool conversation, which only
    `run()` can serve (tools forwarded, per-message structure kept).
    """
    pass


def _is_tool_conversation(request: ChatRequest) -> bool:
    """True when the client is driving its own tool loop: it offered `tools`,
    or replays assistant tool_calls / role:"tool" results."""
    return bool(request.tools) or any(
        m.tool_calls or m.tool_call_id for m in request.messages)


class Pipeline:
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway, self_port: int,
                 co_cfg_provider: Callable[[], ContextOptimizationCfg],
                 ccr: CCRStore, registry: "ModelRegistry | None" = None,
                 memory: MemoryStore | None = None,
                 cache: "DeterministicSemanticCache | None" = None) -> None:
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
        # Feature #6 semantic cache (Task 2). Always constructed by main.py
        # (mirrors self._ccr), only USED when settings.serving.semantic_cache
        # .enabled is true (read live off self._s on every call, same pattern
        # as self._co_cfg_provider() -- there is no separate cfg param to
        # thread since Settings already carries it). Direct path only (see
        # run()); never touched by run_stream/_run_multi_step -- see the
        # comments at each of those call sites for why.
        self._cache = cache

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

    def _base_detector_for(self, app_policy) -> Detector:
        """The app's base detector (`_detector_for`) wrapped in the app's PII
        entity denylist / score threshold / value-shape exemption
        (FilteredDetector) when any is set.

        This is the ONE seam that narrows detection per app WITHOUT disabling the
        gate: it drops the false-positive entity types an app has denylisted, the
        low-confidence matches below its threshold, and the entities whose VALUE
        matches its exempt patterns, BEFORE redaction/classification, so the app
        keeps every other recognizer -- and its residency/onprem_only floor, which
        is computed from `app_policy.residency` in context.py and never consults
        detection, is untouched. US_SSN can never be suppressed
        (FilteredDetector.PROTECTED_ENTITIES), so rules 3/8 hold."""
        base = self._detector_for(app_policy.pii_detection if app_policy else None)
        deny = app_policy.pii_entity_denylist if app_policy else None
        thr = app_policy.pii_score_threshold if app_policy else None
        exempt = app_policy.pii_exempt_patterns if app_policy else None
        if deny or thr is not None or exempt:
            return FilteredDetector(base, deny, thr, exempt)
        return base

    def _policy_mode_for(self, app_policy) -> str:
        """The EFFECTIVE protection posture for this request: the app's own
        `policy_mode`, else the deployment default (`serving.yaml
        policy.default_policy_mode`) -- the same null-means-inherit resolution
        `_detector_for` gives `pii_detection`.

        Read live off `self._s` rather than snapshotted in `__init__`: nothing
        is memoized on it, unlike `_global_pii_mode`, which seeds the detector
        memo and must stay fixed for the life of the process.

        Deployment ceiling: `serving.policy.allow_app_passthrough: false`
        forbids `passthrough` install-wide, for EVERY app, regardless of the
        per-app value or the default. It resolves back to "enforced" here --
        fail closed (CLAUDE.md rule 5) -- as well as being refused at write
        time by the admin API, so a policy row (or a `default_policy_mode`)
        written before the ceiling was lowered can never run unprotected.
        """
        mode = getattr(app_policy, "policy_mode", None) if app_policy is not None else None
        if mode is None:
            mode = self._s.serving.default_policy_mode
        if mode == "passthrough" and not self._s.serving.allow_app_passthrough:
            return "enforced"
        return mode

    def _gate_inputs(self, app_policy, profile: AppPrivacyProfile | None
                     ) -> tuple[Detector, list | None, str]:
        """Compose `(detector, sensitivity rules, effective policy_mode)` for one
        request. The ONE place the gate's detector is built: `run()` and
        `run_stream()` both call it, so the two paths can never drift.

        `passthrough` means NO protection, at all. The base detector is the
        null detector regardless of the app's own `pii_detection`; the app's
        custom recognizers are NOT layered on (no `EffectiveDetector`); and its
        denylist / score floor / value-shape exemptions are moot, so no
        `FilteredDetector` either. That is a deliberate behaviour difference
        from `pii_detection: "off"`, which still runs profile recognizers -- an
        app the operator has declared untouched must not have a leftover custom
        recognizer silently redacting its data, which is exactly the class of
        bug this mode exists to kill. Nothing is detected, so nothing is
        redacted at intake, and `verify()` -- handed this SAME detector on both
        the direct and cached paths -- finds nothing to redact on egress
        either.

        What `passthrough` does NOT change is PLACEMENT. `build_context`
        derives `onprem_only` / `allowed_providers` from `app_policy.residency`
        alone and never consults detection; with no entities found,
        `_match_rule` returns the public rule (exactly as `pii_detection: "off"`
        already does) and `_stricter` still carries the app's own residency
        through. The app's sensitivity rules are still resolved here for the
        same reason -- they can only ever pin residency STRICTER. CLAUDE.md
        rule 3 holds unchanged.
        """
        mode = self._policy_mode_for(app_policy)
        rules = effective_rules(profile, self._s) if profile is not None else None
        if mode == "passthrough":
            return self._detector_for("off"), rules, mode
        base = self._base_detector_for(app_policy)
        if profile is not None:
            return EffectiveDetector(base, list(profile.recognizers)), rules, mode
        return base, rules, mode

    def _sanitize_msg(self, det: Detector, m: Message) -> Message:
        """Redact one message's text with the request's effective detector,
        preserving role + tool-calling structure. Used on the tool-calling
        path so the full structured conversation reaches the model with every
        message's PII masked (CLAUDE.md rule 1) -- the per-message analogue of
        the flattened path's single sanitized blob. A None content (an
        assistant turn that only carries tool_calls) passes through untouched.
        `tool_calls` arguments are redacted the same way via `redact_tool_calls`
        (app/policy/gate.py) -- a replayed assistant tool call whose arguments
        carry an SSN must not forward it to the model untouched."""
        content = m.content
        if content:
            content, _ = det.anonymize(content, det.analyze(content))
        return Message(role=m.role, content=content,
                       tool_calls=redact_tool_calls(m.tool_calls, det),
                       tool_call_id=m.tool_call_id, name=m.name)

    def _classifier_enabled(self) -> bool:
        """The step-4 LLM classifier is OFF unless BOTH the master switch
        (`routing.prompt_opt.enabled`) is on AND the operator has pinned +
        verified the `task_mode_classifier` artifact (`active_prompts` is
        populated by the config loader only for pins that resolved -- see
        `app/config/loader.py`). The shipped `config/routing.yaml` leaves
        `prompt_opt.enabled: false`, so this is False by default and the
        rules `decide()` path below is unchanged."""
        return (self._s.routing.prompt_opt.enabled
                and "task_mode_classifier" in self._s.active_prompts)

    async def _decide_task_mode(self, ctx: ExecutionPolicyContext, sanitized: str,
                                request: ChatRequest) -> tuple[str, DecisionRecord | None]:
        """Resolve step-4 task mode: the optional LLM classifier when enabled
        + pinned (routed ONLY via `Router.select` through the gateway's own
        router/backend -- see `classify_task_mode`'s docstring for the
        residency argument), else the unchanged rules `decide()` path.
        Returns `(mode, classifier_record_or_None)` so both call sites can
        fold the classifier's DecisionRecord (when one was produced) into the
        SAME trace the pipeline already returns -- its cost must not be
        invisible (CLAUDE.md pitfall: "don't compute cost only at the end")."""
        if not self._classifier_enabled():
            return decide_mode(ctx, request), None
        artifact = self._s.active_prompts["task_mode_classifier"]
        mode, crec = await classify_task_mode(
            ctx, sanitized, artifact, request,
            self._gateway.router, self._gateway.backend, self._s, self._reg)
        return mode, crec

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
        pool = self._pool()
        for c in pool:
            if c.name == model_slug:
                return c.display_name or c.name
        return model_slug

    def _pool(self):
        """The merged model source (static YAML pool + enabled onboarded
        rows) -- see `_display_name`'s docstring. Shared with the cache
        STORE hook's provider resolution below (`run()`), which needs the
        same merged pool to resolve `result.model` -> its producing
        provider."""
        return self._reg.effective_pool() if self._reg else self._s.routing.model_pool

    def _cache_bypass_reason(self, cfg, ctx: ExecutionPolicyContext) -> str | None:
        """Semantic-cache bypass posture (design brief §5). `bypass_on
        .user_prompt` is deliberately IGNORED -- it only means something for
        per-message compression treatment; a cache check/store is a
        whole-request decision, not a per-message one (mirrors
        SemanticCacheCfg's docstring)."""
        if cfg.bypass_on.high_risk and ctx.risk_class == "high":
            return "high_risk"
        if cfg.bypass_on.restricted_sensitivity and ctx.sensitivity == "restricted":
            return "restricted_sensitivity"
        if ctx.requested_model is not None:
            # The client pinned a specific model -- a cached response from a
            # DIFFERENT model would silently violate that pin.
            return "requested_model"
        return None

    def _serve_from_cache(self, request_id: str, ctx: ExecutionPolicyContext,
                          sanitized: str, entry: CacheEntry, det: Detector,
                          check_latency_ms: float, policy_mode: str) -> dict | None:
        """C4 keystone: re-verify the cached text against the CURRENT
        request's per-app detector + ctx before ever serving it -- this is
        the step-9 verification for the served response; compress/gateway/
        verify are NOT re-run. Returns None (never a response) when the
        re-verify fails, so the caller treats this as an ordinary MISS and
        falls through to the normal flow -- the cached text is never served
        un-re-verified (see docs/architecture/specs/semantic-cache-residency-spec.md
        C4)."""
        text, vrec = verify(det, ctx, entry.response_text)
        if not vrec.passed:
            return None
        tokens_in = count_tokens(sanitized)
        tokens_out = count_tokens(text)
        # Honest third savings lever (CLAUDE.md: "don't attribute all savings
        # to routing"): a hit skips the model entirely, so the avoided cost is
        # the FULL all-frontier baseline of this request -- never folded into
        # routing_saved_usd/compression_saved_usd, which both stay 0 here.
        baseline = self._baseline(tokens_in, tokens_out)
        cache_rec = CacheRecord(
            enabled=True, hit=True, tokens_saved=tokens_in + tokens_out,
            cache_saved_usd=baseline, entries_size=self._cache.size,
            blocked_cloud_entries=self._cache.blocked_cloud_entries,
            latency_ms=check_latency_ms)
        trace = [vrec.model_dump(), cache_rec.model_dump()]
        return {
            "id": request_id,
            "model": entry.model,
            "model_display": self._display_name(entry.model),
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            "usage": Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out,
                           total_tokens=tokens_in + tokens_out).model_dump(),
            "trace": trace,
            "cost": {"total_usd": 0.0, "baseline_usd": baseline,
                     "routing_saved_usd": 0.0, "compression_saved_usd": 0.0,
                     "cache_saved_usd": baseline},
            "quality": {"sum": 0.0, "chosen": None, "source": None},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities),
                       "app_policy_version": ctx.app_policy_version,
                       # EFFECTIVE posture (post-inherit, post-ceiling), never
                       # the raw nullable column -- see `_policy_mode_for`.
                       "policy_mode": policy_mode},
            "sanitized_prompt": sanitized,
            # No raw/pre-verify text exists for a cache hit (no model call ran
            # this request) -- `text` (the just-re-verified, redacted answer)
            # is the least-revealing honest value to pop here (see routes.py
            # `_finalize`, which never forwards this key to the client).
            "raw_completion": text,
        }

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
        #
        # Per-app policy_mode: an app resolving to "passthrough" short-circuits
        # ALL of the above to the null detector with no recognizers layered on
        # -- see `_gate_inputs`. Placement (onprem_only/allowed_providers) is
        # untouched by that; only protection is.
        try:
            det, rules, policy_mode = self._gate_inputs(app_policy, profile)
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
                and registry_permits(self._reg, c, tenant_id)
            }
            if ctx.requested_model not in permitted:
                raise PipelineRejected(
                    f"requested model {ctx.requested_model!r} is not permitted or "
                    f"unavailable: {pinned_model_reason(pool, ctx, self._reg, tenant_id, ctx.requested_model)}")
        mem_cfg = self._s.serving.memory
        # A client that supplies `tools` (or replays assistant/tool turns) is
        # driving its OWN agent loop: RouteForge serves a SINGLE completion that
        # may return tool_calls, forwarding the full structured, PER-MESSAGE-
        # redacted conversation to the model. This path is deliberately distinct
        # from the ordinary single-prompt path (which flattens the whole request
        # into one sanitized blob) and from the internal NAT agent (`multi_step`)
        # -- a client's tool loop must never be hijacked by our own agent. It
        # forces `direct` and skips task-mode, the semantic cache, and memory,
        # all of which assume that single flattened prompt.
        tools = request.tools
        tool_choice = request.tool_choice
        tool_mode = _is_tool_conversation(request)

        if tool_mode:
            mode, crec = "direct", None
            # Preserve roles + tool structure, but redact EACH message's text so
            # no raw PII reaches the model (CLAUDE.md rule 1) -- the same
            # guarantee the flattened path gives, applied message-by-message.
            san_messages = [self._sanitize_msg(det, m) for m in request.messages]
            recall = RecallResult([], None)
        else:
            # Task-mode: the optional LLM classifier (Task 2) when enabled +
            # pinned, else the unchanged rules `decide()` path. Either way this
            # receives SANITIZED content only, never the raw request — see
            # CLAUDE.md rule 1 and `classify_task_mode`'s docstring.
            mode, crec = await self._decide_task_mode(ctx, sanitized, request)

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
            # `application_id` here. `session_id` is the per-session partition key,
            # and it is the CLIENT-SUPPLIED id ONLY (`x-routeforge-session`,
            # app/api/routes.py::_read_session_id) -- routes.py separately derives
            # a session id for TRACE/CONSOLE grouping when the header is absent
            # (`_derive_session_id`, `server.derive_session_id`), but that derived
            # id is deliberately never threaded in here: memory partitioning must
            # not silently change semantics when derivation is toggled (fix
            # round 1). A request with no session header (or an unsanitizable
            # one) falls back to `session_id or ""`, the shared partition -- so
            # distinct end-user sessions of one app no longer collide, and no
            # session header keeps the old (pre-session) behavior.
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
            return await self._run_multi_step(
                request_id, ctx, request, san_messages, det, policy_mode,
                recall.record, classifier_record=crec)

        # Feature #6 — semantic cache CHECK (direct path only; see the
        # constructor comment and _run_multi_step/run_stream for why they
        # never touch this). Runs after san_messages, before compress: a hit
        # skips compress/gateway/verify entirely and short-circuits below.
        # `cache_active` mirrors C7 — with the feature off (or main.py never
        # constructing a cache) nothing here runs and nothing is added to the
        # trace/cost dict, so serving stays byte-for-byte unchanged.
        sc_cfg = self._s.serving.semantic_cache
        # `not tool_mode`: a tool-calling conversation is never cached (the cache
        # keys on a single flattened query and would drop the tool structure).
        cache_active = self._cache is not None and sc_cfg.enabled and not tool_mode
        cache_bypass_reason = (
            self._cache_bypass_reason(sc_cfg, ctx) if cache_active else None)
        cache_record: CacheRecord | None = None
        if cache_active:
            if cache_bypass_reason is not None:
                cache_record = CacheRecord(
                    enabled=True, hit=False, bypass_reason=cache_bypass_reason,
                    entries_size=self._cache.size,
                    blocked_cloud_entries=self._cache.blocked_cloud_entries)
            else:
                t0 = time.monotonic()
                entry = None
                try:
                    entry = self._cache.lookup(
                        tenant_id=tenant_id, onprem_only=ctx.onprem_only,
                        allowed_providers=ctx.allowed_providers, query=sanitized)
                except Exception:
                    # Fail-safe (CLAUDE.md rule 5 spirit): a cache error must
                    # never crash a served request -- skip the cache and serve
                    # normally, exactly as if this were a miss.
                    log.exception("semantic cache lookup failed (swallowed); "
                                  "serving normally")
                check_latency_ms = (time.monotonic() - t0) * 1000
                if entry is not None:
                    # C4 KEYSTONE: re-verify with the CURRENT request's
                    # detector/ctx before ever serving cached text. A failed
                    # re-verify is treated as an ordinary miss below (falls
                    # through to compress/gateway/verify), never served.
                    hit_resp = self._serve_from_cache(
                        request_id, ctx, sanitized, entry, det, check_latency_ms,
                        policy_mode)
                    if hit_resp is not None:
                        return hit_resp
                cache_record = CacheRecord(
                    enabled=True, hit=False, entries_size=self._cache.size,
                    blocked_cloud_entries=self._cache.blocked_cloud_entries,
                    latency_ms=check_latency_ms)

        if tool_mode:
            # No compression on a tool conversation: Headroom rewrites text and
            # would corrupt tool_call/tool_result structure. Build a bypassed
            # CompressionResult so the cost/trace accounting below is identical.
            # Tool schemas and replayed tool_call arguments are billed prompt
            # tokens too; leaving them out under-priced every agent turn.
            tb = (sum(count_tokens(m.content) for m in san_messages)
                  + sum(count_tokens(json.dumps(m.tool_calls))
                        for m in san_messages if m.tool_calls)
                  + (count_tokens(json.dumps(tools)) if tools else 0))
            comp = CompressionResult(
                san_messages, tb, tb,
                CompressionRecord(
                    enabled=self._co_cfg_provider().enabled, applied=False,
                    bypass_reason="tool_calling", tokens_before=tb, tokens_after=tb))
        else:
            comp = compress(san_messages, ctx, self._co_cfg_provider(), ccr=self._ccr)
        complexity = min(1.0, comp.tokens_after / 2000)

        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint,
            tools=tools, tool_choice=tool_choice, params=request.generation_params())
        if crec is not None:
            # Fold the classifier's own DecisionRecord into the SAME trace/
            # cost accumulation as the router's -- its cost must not be
            # invisible (CLAUDE.md pitfall). `quality_score` is unset on
            # `crec`, so it never displaces the served hop's quality below.
            records = [crec, *records]

        text, vrec = verify(det, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        # Memory WRITE (Feature 5, Task 2): success path ONLY (a rejected/
        # failed request writes nothing -- unreachable above since a failed
        # verify already raised). R1 sanitized-only: built from `sanitized`
        # (the gate's redacted input) and `text` (the post-verify output)
        # ONLY -- never `result.text` (pre-verify raw), `raw_completion`, or
        # the raw request. See write_memory()'s docstring for the contract.
        if not tool_mode:  # tool conversations are not summarized into memory
            write_memory(self._memory, mem_cfg, ctx, sanitized, text,
                        tenant_id=tenant_id, application_id=tenant_id,
                        session_id=session_id or "")
        # Feature #6 — semantic cache STORE. Reached only on a genuine MISS
        # (a hit already returned above), enabled, and not bypassed. C1:
        # cache the SANITIZED query (never raw) and the post-verify `text`
        # (never `result.text`/raw_completion). C3 residency: the store
        # itself refuses to persist a cloud-produced entry into an
        # onprem_only partition -- passing the REAL ctx values here is what
        # makes that refusal apply to real traffic.
        if cache_active and cache_bypass_reason is None:
            try:
                provider = next(
                    (c.provider for c in self._pool() if c.name == result.model), "?")
                self._cache.store(
                    tenant_id=tenant_id, onprem_only=ctx.onprem_only,
                    allowed_providers=ctx.allowed_providers, query=sanitized,
                    response_text=text, model=result.model, provider=provider)
            except Exception:
                # Fail-safe: a cache write must never crash a served request.
                log.exception("semantic cache store failed (swallowed)")

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
        if cache_record is not None:
            trace.append(cache_record.model_dump())

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

        cost = {"total_usd": total, "baseline_usd": baseline_usd,
               "routing_saved_usd": routing_saved,
               "compression_saved_usd": compression_saved}
        if cache_active:
            # Honest third lever, present whenever the feature is on (even a
            # bypassed/miss request reports 0.0 rather than omitting the key)
            # — never folded into routing_saved_usd/compression_saved_usd.
            # Omitted entirely when the feature is off (C7: byte-for-byte
            # unchanged serving behavior).
            cost["cache_saved_usd"] = 0.0

        # Tool-calling turn: the model asked to call tools, so the assistant
        # message carries `tool_calls` with `content: null` and the choice's
        # finish_reason is "tool_calls" (OpenAI clients switch on this to run
        # their tools and call back). Ordinary answers keep content + "stop".
        tool_calls = result.tool_calls or None
        assistant = Message(
            role="assistant",
            content=(text or None) if tool_calls else text,
            tool_calls=tool_calls)
        finish_reason = "tool_calls" if tool_calls else "stop"
        return {
            "id": request_id,
            "model": result.model,
            "model_display": self._display_name(result.model),
            "choices": [Choice(index=0, message=assistant,
                               finish_reason=finish_reason).model_dump()],
            "usage": Usage(prompt_tokens=result.tokens_in,
                           completion_tokens=result.tokens_out,
                           total_tokens=result.tokens_in + result.tokens_out
                           ).model_dump(),
            "trace": trace,
            "cost": cost,
            "quality": {"sum": quality_sum,
                        "chosen": chosen_q.quality_score if chosen_q else None,
                        "source": chosen_q.quality_source if chosen_q else None},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities),
                       "app_policy_version": ctx.app_policy_version,
                       # EFFECTIVE posture (post-inherit, post-ceiling), never
                       # the raw nullable column -- see `_policy_mode_for`.
                       "policy_mode": policy_mode},
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

        NOT wired to the semantic cache (feature #6, Task 2), deliberately:
        verification is waived here, so storing this response would persist
        unverified content into the cache (violates C1/C4), and a cache HIT
        can't be expressed as a token stream anyway. See `run()` for the only
        path the cache checks/stores against.

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
        the per-app detector `_gate_inputs` composes, mirroring `run()`) runs
        first; compression runs strictly after it.
        """
        # Gate first -- the SAME `_gate_inputs` composition run() uses (per-app
        # policy_mode, pii_detection base detector, optional profile
        # recognizers/rules; fail closed), so the streaming path can never
        # drift from the direct one.
        try:
            det, rules, policy_mode = self._gate_inputs(app_policy, profile)
            ctx, sanitized, _map = self._gate.evaluate(
                request_id, tenant_id, request, app_policy=app_policy,
                detector=det, rules=rules)
        except PolicyGateError:
            raise
        except Exception as e:
            raise PolicyGateError("policy evaluation failed") from e

        # A client-driven tool conversation needs run()'s structured path: it
        # forwards `tools` and keeps per-message roles/tool_call_ids, where the
        # live token path below flattens everything into one prompt and has no
        # way to stream a tool_calls answer. Serve it as one complete
        # completion; the route then emits it (tool_calls included) as SSE.
        if _is_tool_conversation(request):
            raise StreamFallback(
                "tool-calling conversation: served as one complete completion, "
                "then streamed")

        # Streaming is the direct/single-shot path only. Same optional-LLM-
        # classifier-else-rules resolution as run() (Task 2).
        mode, crec = await self._decide_task_mode(ctx, sanitized, request)
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
            ctx, comp.messages, comp.tokens_after, complexity, hint,
            params=request.generation_params())
        if crec is not None:
            # Same fold as run() -- the classifier's cost/reason reach the
            # SAME trace `_assemble_stream_meta` builds below.
            records = [crec, *records]

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
                request_id, ctx, sanitized, comp, records, result, policy_mode,
                recall.record))

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
                              policy_mode: str,
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
                       "detected_entities": list(ctx.detected_entities),
                       "app_policy_version": ctx.app_policy_version,
                       # EFFECTIVE posture (post-inherit, post-ceiling), never
                       # the raw nullable column -- see `_policy_mode_for`.
                       "policy_mode": policy_mode},
            "sanitized_prompt": sanitized,
            "raw_completion": result.text,
            # Step-9 verification marker: "waived" this task. Tasks 6/7 refine to
            # "waived" (stream_unverified) / "incremental" (incremental_verify).
            "verification": "waived",
        }

    async def _run_multi_step(self, request_id: str, ctx: ExecutionPolicyContext,
                              request: ChatRequest, san_messages: list[Message],
                              det: Detector, policy_mode: str,
                              mem_record: MemoryRecord | None = None,
                              classifier_record: DecisionRecord | None = None) -> dict:
        # NOT wired to the semantic cache (feature #6, Task 2), deliberately:
        # a multi-step run is non-deterministic and tool-driven (its final
        # text is not a pure function of the initial query), so neither a
        # cache check nor a cache store make sense here. See `run()` for the
        # only path the cache checks/stores against.
        #
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
        if classifier_record is not None:
            # Additive, not zipped with result.compressions -- deliberately
            # kept OUT of result.hops so the hops<->compressions 1:1 zip
            # below stays aligned (Task 2; classifier_record has no
            # matching per-hop CompressionRecord of its own).
            total += (classifier_record.actual_cost_usd
                     if classifier_record.actual_cost_usd is not None
                     else (classifier_record.est_cost_usd or 0.0))
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

        trace = ([classifier_record.model_dump()] if classifier_record is not None else [])
        trace += [h.model_dump() for h in result.hops]
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
                       "detected_entities": list(ctx.detected_entities),
                       "app_policy_version": ctx.app_policy_version,
                       # EFFECTIVE posture (post-inherit, post-ceiling), never
                       # the raw nullable column -- see `_policy_mode_for`.
                       "policy_mode": policy_mode},
            "sanitized_prompt": san_messages[-1].content if san_messages else "",
            "raw_completion": result.text,
        }
