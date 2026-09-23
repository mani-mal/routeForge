from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Callable, TYPE_CHECKING

from app.apps.outcomes import outcome_context, outcome_trace
from app.cache.store import CacheEntry, DeterministicSemanticCache
from app.config.schema import ContextOptimizationCfg, PoolCandidate, Settings
from app.errors import PolicyGateError
from app.gateway.backends.base import count_tokens
from app.gateway.gateway import Gateway
from app.models import (
    CacheRecord, ChatRequest, Choice, CompressionRecord, DecisionRecord,
    ExecutionPolicyContext, GenerationResult, MemoryRecord,
    Message, Usage, VerificationRecord,
)
from app.output.verify import egress_detector, verify
from app.policy.compile import APP_SOURCE, EffectivePolicy, compile_policy
from app.policy.context import pinned_model_reason
from app.policy.feasible import feasible_set
from app.policy.detectors import Detector, detection_ran, make_detector
from app.policy.effective import EffectiveDetector, FilteredDetector
from app.policy.gate import Gate, Representations, redact_tool_calls_with_entities
from app.apps.privacy import AppPrivacyProfile, effective_rules
from app.compression import CompressionResult, compress
from app.compression.ccr import CCRStore
from app.memory import RecallResult, recall_memory, write_memory
from app.memory.store import MemoryStore

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

log = logging.getLogger("serving.cache")
_obs_log = logging.getLogger("serving.obs")


class PipelineRejected(Exception):
    pass


class StreamFallback(Exception):
    """Raised by `Pipeline.run_stream` when a request MUST be verified as a
    complete response and therefore cannot be streamed unverified. The caller
    (the chat route, Tasks 6-7) catches this and falls back to
    `verified_then_stream` (buffer the full answer, run step-9 verification,
    then chunk the finished text).

    Raised in exactly ONE place -- `run_stream` -- for exactly three reasons:
      * `ctx.onprem_only` or `ctx.require_verification` is set (the privacy
        backstop from Global Constraint 1: a residency- or risk-floored
        request is never streamed without a complete-response verification),
      * the app streams `stream_unverified` but has explicitly declared
        `client_output: redact` (design §2.5: egress redaction needs the
        complete response, which this mode cannot provide), or
      * the request is a client-driven tool conversation, which only `run()`
        can serve (tools forwarded, per-message structure kept).
    """
    pass


def _is_tool_conversation(request: ChatRequest) -> bool:
    """True when the client is driving its own tool loop: it offered `tools`,
    or replays assistant tool_calls / role:"tool" results."""
    return bool(request.tools) or any(
        m.tool_calls or m.tool_call_id for m in request.messages)


def _chosen_scoring(scoring: dict | None, chosen: str | None) -> dict | None:
    """The chosen candidate's terms, for the response body. The full eligible
    set goes to the trace only (`sp.scoring`, app/obs/export.py) -- the
    response is a client-facing surface on every chat completion and must
    stay small; see spec §7.1."""
    if not scoring or not chosen:
        return None
    for c in scoring.get("candidates", []):
        if c.get("name") == chosen:
            # `latency_scale_ms`/`max_latency_norm` complete the replay inputs
            # for THIS ONE candidate (two floats -- negligible payload). The
            # response still carries only one candidate, so a client can
            # recompute the chosen candidate's score, never the ordering
            # among candidates it was never shown (see spec §7.1 and
            # docs/reference/routing-observability.md).
            return {"weights": scoring["weights"],
                    "signals_enabled": scoring["signals_enabled"],
                    "latency_scale_ms": scoring["latency_scale_ms"],
                    "max_latency_norm": scoring["max_latency_norm"], **c}
    return None


class Pipeline:
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway,
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
        # Step 5 (compression) config, read dynamically on every call instead
        # of a frozen value captured at construction — so a runtime toggle
        # via POST /api/controls (which mutates app.state.co_cfg) takes
        # effect on the NEXT request without a restart. main.py wires this
        # provider to app.state.co_cfg so a runtime toggle applies process-
        # wide. The local, TTL-bounded CCR store used by the direct path —
        # see app/compression/__init__.py, ccr.py.
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
        # run()); never touched by run_stream -- see the comment at that call
        # site for why.
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

    def _base_detector_for(self, app_policy, kind: str) -> Detector:
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
        (FilteredDetector.PROTECTED_ENTITIES), so rules 3/8 hold.

        `kind` is the COMPILED `detection` axis (`eff.protection.detection`),
        never the raw nullable column: it has already had the deployment
        default folded in and the `allow_app_pii_downgrade` ceiling applied."""
        base = self._detector_for(kind)
        deny = app_policy.pii_entity_denylist if app_policy else None
        thr = app_policy.pii_score_threshold if app_policy else None
        exempt = app_policy.pii_exempt_patterns if app_policy else None
        if deny or thr is not None or exempt:
            return FilteredDetector(base, deny, thr, exempt)
        return base

    def _gate_inputs(self, app_policy, profile: AppPrivacyProfile | None
                     ) -> tuple[Detector, list | None, EffectivePolicy]:
        """Compose `(detector, sensitivity rules, effective policy)` for one
        request. The ONE place the gate's detector is built: `run()` and
        `run_stream()` both call it, so the two paths can never drift.

        Nothing here branches on `policy_mode` any more. `compile_policy`
        resolves every axis once (per-app value, then the `policy_mode` preset,
        then the deployment default, with the deployment ceilings on top) and
        every caller reads `eff.protection` instead of re-deriving a posture.

        **The detector is built when ANY consumer needs it** (design §2.3) --
        the model (`model_input: redact`), the client (`client_output:
        redact`), the exporter (`telemetry_io: redacted`), or placement
        escalation (`escalate_residency_on_detection`). Its KIND comes from
        `eff.protection.detection`. That split is the point: an app whose model
        sees raw text but whose CLIENT response must be redacted still gets a
        real detector, which the old `policy_mode` branch could not express.

        When no consumer needs it, the NullDetector is used and nothing is
        scanned -- what a router-only app must get. Detection resolving to
        `off` reaches the same NullDetector through `_detector_for`.

        **The no-recognizers rule, preserved.** An app the operator has
        declared untouched must not have a leftover custom recognizer silently
        redacting its data. That used to be spelled "policy_mode is
        passthrough"; it is now `detection == "off"` AND the raw/raw posture
        (`model_input` and `client_output` both `raw`). Such an app gets a bare
        NullDetector: no `EffectiveDetector` layering the profile's
        recognizers, and no `FilteredDetector` (its denylist / score floor /
        value-shape exemptions are moot with nothing to filter). That is still
        a deliberate behaviour difference from `pii_detection: "off"` alone,
        which keeps running profile recognizers.

        The new spelling is NOT the same set of apps as the old one, and the
        difference is deliberate: `passthrough` WITH an explicit
        `pii_detection` used to get the null detector and now gets the
        detector it named, recognizers included, so detection (and therefore
        placement escalation) happens again -- design §4 and counterexample
        CE-3. Nothing is redacted for such an app, since both data-handling
        axes stay `raw`, but rows in that shape exist in deployed databases:
        see the pre-roll audit in `docs/reference/per-app-config.md`. An app
        that names no `pii_detection` is unaffected -- the preset still
        resolves detection to `off`.

        `escalate_residency_on_detection` defaults to true and appears in
        `needed` below, but it is structurally inert for an app with no
        detector: `detection == "off"` resolves to the NullDetector either
        way, so the switch can only ever matter once some detector runs. That
        is not a bug -- the axes are independent by design, and the switch
        governs whether a rule may move placement, not whether scanning
        happens.

        What none of this changes is PLACEMENT. `build_context` derives
        `onprem_only` / `allowed_providers` from `app_policy.residency` plus
        the declared cross-axis rule alone, and never consults a protection
        axis. CLAUDE.md rule 3 holds unchanged.
        """
        eff = compile_policy(app_policy, self._s)
        p = eff.protection
        rules = effective_rules(profile, self._s) if profile is not None else None
        needed = (p.model_input == "redact" or p.client_output == "redact"
                  or p.telemetry_io == "redacted"
                  or eff.escalate_residency_on_detection)
        no_protection = p.model_input == "raw" and p.client_output == "raw"
        if not needed or (p.detection == "off" and no_protection):
            return self._detector_for("off"), rules, eff
        base = self._base_detector_for(app_policy, p.detection)
        if profile is not None:
            return EffectiveDetector(base, list(profile.recognizers)), rules, eff
        return base, rules, eff

    @staticmethod
    def _policy_mode_stamp(eff: EffectivePolicy) -> str:
        """The legacy `policy["policy_mode"]` stamp, derived from the compiled
        axes instead of a stored column: `passthrough` is exactly the raw/raw
        posture that mode compiles to, and anything else reports `enforced` --
        including a `passthrough` the deployment ceiling refused, so the stamp
        always reports what RAN, never what was asked for.

        It is a compatibility surface (clients read it today). The full
        per-axis certificate is design §6's job, in a later task.
        """
        p = eff.protection
        return "passthrough" if (p.model_input == "raw"
                                 and p.client_output == "raw") else "enforced"

    @staticmethod
    def _certificate(eff: EffectivePolicy, ctx: ExecutionPolicyContext,
                     det: Detector, endpoint_registry_version: str,
                     endpoint_data_handling: dict,
                     chosen_scoring: dict | None = None) -> dict:
        """The per-axis fields of the decision certificate (design P1a §6)
        common to every response shape. Reads straight off the already-
        compiled `eff` (placement + protection + the cross-axis rules that
        fired) and `ctx` (the detected entity TYPES) -- nothing here
        re-derives a policy rule, it only names what the compiler already
        decided. `eff` is the one `apply_signals` already ran over (see
        `Gate.evaluate`), so `placement`/`placement_sources` reflect any
        cross-axis escalation, not just the app's own configured residency.

        `det` is the detector `_gate_inputs` actually composed for THIS
        request, and it is here for one reason: **"not checked" must not read
        as "checked and clean."** `detection_mode` used to stamp
        `eff.protection.detection` unconditionally, but `_gate_inputs` builds
        the detector only when some consumer needs it (design §2.3), so a
        legal, reachable app -- `model_input: raw` + `client_output: raw` +
        `capture_io: off` + `escalate_residency_on_detection: false` +
        `pii_detection: presidio` -- got the NullDetector and a certificate
        reading `detection_mode: presidio` with an empty
        `detected_entity_types`: indistinguishable, to an auditor, from
        "presidio scanned this request and found nothing". The certificate
        reports what RAN, so it now stamps `off` there. The configured axis is
        not lost: it stays in `app_policy_version`, and the admin API's
        `effective.detection` (`_effective_dict`) is the compiled value.

        Same closure as Task 9's `verify()` reason fix, which stopped
        `entities_found: ()` under `client_output: raw` reading as "nobody
        found anything" when it meant "nobody looked".

        Uses the shared `detection_ran()` predicate (`app/policy/detectors.py`)
        -- the same one `Gate.evaluate` calls -- so the two structurally
        cannot disagree about whether an app with `pii_detection: off` and
        custom recognizers layered on top (`EffectiveDetector`) HAS scanned.

        Merged into the response `policy` block AND handed to `export_trace`
        (which stamps the matching `sp.*` attributes) -- one dict, two
        channels, so they can never disagree (the same pattern
        `policy_mode`/`content_integrity_mode` already use).

        `endpoint_registry_version` and `endpoint_data_handling` (P1b design
        §6) are computed by the caller (`_endpoint_registry_version`,
        `_endpoint_declaration` below) rather than here, because they need
        `self._reg`/`self._pool()` and this method stays a `@staticmethod` --
        the same reason `eff`/`ctx`/`det` are handed in rather than
        recomputed. `endpoint_registry_version` names the registry this
        decision was made against (an edited endpoint declaration becomes
        visible as a changed hash, not silent); `endpoint_data_handling`
        names what the SERVED endpoint declared and where each of the three
        values came from (model / provider_default / unknown) -- a
        declaration, never a verified fact (design §2).

        `chosen_scoring` (P2a Task 8, `_chosen_scoring` below) is the served
        candidate's own scoring terms and provenance -- already narrowed to
        one candidate by the caller. The full eligible set never appears
        here: that stays on the trace only (`sp.scoring`), because this
        certificate is on the response body of every chat completion."""
        ran = detection_ran(det)
        return {
            "placement": eff.placement.residency,
            "placement_sources": list(eff.placement.sources),
            "model_input_mode": eff.protection.model_input,
            "client_output_mode": eff.protection.client_output,
            "telemetry_io_mode": eff.protection.telemetry_io,
            "detection_mode": eff.protection.detection if ran else "off",
            "stream_mode": eff.protection.stream_mode,
            "cross_axis_rules_fired": list(eff.cross_axis_rules_fired),
            # Certificate-named duplicate of `detected_entities` (design §6's
            # own field name for it) -- both stay TYPES only, never values;
            # see `ExecutionPolicyContext.detected_entities`.
            "detected_entity_types": list(ctx.detected_entities),
            # P1b Task 6 additions (design §6) -- see docstring above.
            "endpoint_registry_version": endpoint_registry_version,
            "endpoint_data_handling": endpoint_data_handling,
            # P2a Task 8: chosen-candidate-only scoring terms + provenance.
            # None when no router ran (e.g. a semantic-cache hit) or the
            # served hop carried no scoring record.
            "scoring": chosen_scoring,
        }

    def _endpoint_registry_version(self) -> str:
        """`ModelRegistry.registry_version()`, or `""` with no registry (the
        internal/direct-pipeline paths that never construct one) -- mirrors
        `ctx.app_policy_version`'s empty-string default for "not applicable"
        rather than fabricating a hash over nothing."""
        return self._reg.registry_version() if self._reg is not None else ""

    def _endpoint_declaration(self, candidate: PoolCandidate | None) -> dict:
        """The served endpoint's three P1b declarations plus their
        provenance, read straight off the already-RESOLVED `PoolCandidate`
        (`ModelRegistry.effective_pool()` is the one place that resolves
        them -- see `app/apps/registry.py`) -- never re-derived here.
        `candidate is None` only when the model can't be found in `_pool()`
        (unreachable on a served response; defensive), and reads as
        "unknown" all round rather than raising out of a certificate build."""
        if candidate is None:
            return {"region": "unknown", "prompt_retention": "unknown",
                    "accepts_raw_sensitive": "unknown",
                    "source": {"region": "unknown", "prompt_retention": "unknown",
                              "accepts_raw_sensitive": "unknown"}}
        return {
            "region": candidate.region,
            "prompt_retention": candidate.prompt_retention,
            "accepts_raw_sensitive": candidate.accepts_raw_sensitive,
            "source": dict(candidate.data_handling_source or {}),
        }

    def _candidate_by_name(self, name: str) -> PoolCandidate | None:
        """The resolved `PoolCandidate` for a served model name, looked up
        against the same merged pool `_pool()` -- shared with
        `_display_name`'s and the cache STORE hook's provider resolution
        (see `_pool`'s docstring), so this is a third reader of the one
        lookup rather than a new one."""
        return next((c for c in self._pool() if c.name == name), None)

    def _eligible_now(self, ctx: ExecutionPolicyContext,
                      name: str) -> PoolCandidate | None:
        """The candidate called `name` in THIS request's own eligible set, or
        None if this request may not reach it.

        Asked of `app/policy/feasible.py` -- the one authority -- rather than
        re-implemented here. It used to be an inline expression in
        `_eligible_candidate` (`provider in ctx.allowed_providers and (not
        ctx.model_scope_active or name in ctx.allowed_models)`), which is a
        second copy of the eligibility rule and drifted the instant a new
        stage was added: P1b's data-handling exclusions are deliberately kept
        IN `ctx.allowed_models` so each keeps its own specific reason (see
        `build_context`), so that expression would have read a
        data-handling-EXCLUDED model as eligible.

        Returns the candidate, not a bool, because the semantic-cache path
        needs its `provider` too and one traversal of the effective pool
        should answer both questions.

        **Matches by NAME only, and every caller pairs it with a provider
        comparison** (`_eligible_candidate` below; both semantic-cache
        guards). The name is the routing identity everywhere else in the
        system -- `Router.select`'s `exclude` set, `ModelRegistry.price_for`,
        `is_enabled` and `is_allowed_for_app` all key on it alone, and
        `effective_pool()` is expected to carry each slug once. That
        expectation is not enforced anywhere, though: the pool is the YAML
        cloud entries UNION the enabled onboarded rows, so one slug could in
        principle appear under two providers. Comparing the provider at each
        call site costs nothing and means a same-slug collision degrades to a
        refusal rather than to an unchecked match.

        No `exclude` is passed: a hop the gateway fell back PAST was eligible
        when it was chosen, and the prior-failure stage is about what may be
        tried NEXT, not about what was legitimately served.
        """
        feasible = feasible_set(self._s, self._reg, ctx, tenant_id=ctx.tenant_id)
        return next((c for c in feasible.eligible if c.name == name), None)

    def _eligible_candidate(self, ctx: ExecutionPolicyContext, name: str | None,
                            provider: str | None) -> bool:
        """Was this (name, provider) pair drawn from THIS request's own
        eligible set? The single predicate `_eligibility_checks` checks the
        gateway's hops against; never a re-derivation of the router's own
        scoring, only a check of its output against the policy that bounded
        it."""
        if name is None or provider is None:
            return False
        cand = self._eligible_now(ctx, name)
        return cand is not None and cand.provider == provider

    def _eligibility_checks(self, ctx: ExecutionPolicyContext,
                            records: list[DecisionRecord]) -> dict:
        """`sp.checks` (design §6): the one per-request proof the certificate
        offers -- that the model(s) the gateway actually used were drawn from
        THIS request's own eligible set, not a re-derivation of the routing
        algorithm that chose among them. `chosen_in_eligible_set` checks the
        hop that was actually SERVED (the last router hop with a chosen
        candidate); `fallback_in_eligible_set` checks every earlier hop the
        gateway fell back or escalated PAST. Either is `None` when there is
        nothing to check this request (no served model / no fallback
        occurred) -- never a fabricated boolean standing in for "not
        applicable". Policy-WIDE invariants (monotonicity, noninterference)
        are the harness's job (design §7); this checks one request's own
        trace against its own context."""
        chosen_hops = [r for r in records if r.chosen is not None]
        if not chosen_hops:
            return {"chosen_in_eligible_set": None, "fallback_in_eligible_set": None}
        served = chosen_hops[-1]
        fallback_ok = (
            all(self._eligible_candidate(ctx, r.chosen, r.chosen_provider)
                for r in chosen_hops[:-1])
            if len(chosen_hops) > 1 else None)
        return {
            "chosen_in_eligible_set": self._eligible_candidate(
                ctx, served.chosen, served.chosen_provider),
            "fallback_in_eligible_set": fallback_ok,
        }

    @staticmethod
    def _served_scoring(records: list[DecisionRecord]) -> dict | None:
        """The SERVED hop's (last router hop with a chosen candidate --
        same rule `_eligibility_checks` uses) chosen-candidate scoring
        terms, for the response certificate. `None` when no router ran
        this request (e.g. a semantic-cache hit) or that hop carried no
        scoring record."""
        chosen_hops = [r for r in records if r.chosen is not None]
        if not chosen_hops:
            return None
        served = chosen_hops[-1]
        return _chosen_scoring(served.scoring, served.chosen)

    @staticmethod
    def _publish_telemetry_views(internals: dict | None,
                                 reps: Representations) -> None:
        """Hand the caller the two values the EXPORTER needs (design §2.6).

        Deliberately NOT on the response dict. That dict is serialized -- to
        the client, to `request_snapshots.full_json`, to a test's
        `json.dumps` -- and these two are a live detector object and the RAW
        prompt. The existing internal-only keys (`raw_completion`,
        `sanitized_prompt`) already show how that goes: every one of them
        needs a pop at exactly the right chokepoint, and a path that forgets
        leaks or crashes. An explicit out-parameter the caller owns has no
        such failure mode -- nothing to forget, nothing to serialize.

        `reps.detector` is None exactly when no detector ran. That is the
        exporter's fail-closed signal: `redacted` is not available to an app
        that scans nothing, so it exports `off` rather than raw content under
        a redacted label (CE-1). `reps.text_raw` is the gate's flattened raw
        scan text, which the exporter redacts ITSELF -- it never receives a
        view another consumer selected.

        `text_redacted` rides along as an optimisation: it is `reps.detector`'s
        redaction of `reps.text_raw`, so an exporter that would otherwise run
        the same detector over the same string can skip that pass (a whole
        spaCy NER pass, on Presidio). All three come off ONE frozen
        `Representations`, published here and nowhere else, which is what makes
        the raw/redacted pairing safe to rely on -- `redacted_copy` cannot
        check it. This is NOT the model's view: `model_input` selects that
        separately in `run()`, and under `raw` it is `text_raw` itself.
        """
        if internals is None:
            return
        internals["raw_prompt"] = reps.text_raw
        internals["redacted_prompt"] = reps.text_redacted
        internals["detector"] = reps.detector

    def _baseline(self, in_tokens: int, out_tokens: int) -> float:
        """All-frontier baseline: input and output priced at the baseline
        (most-expensive frontier) model's respective rates — the SAME token
        basis `total` uses, so routing_saved compares like with like (B12)."""
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return (in_tokens * p.input + out_tokens * p.output) / 1_000_000

    def _display_name(self, model_slug: str) -> str:
        """Resolve a routing-pool slug (e.g. `local-8b`) to its human-facing
        model name (e.g. `Qwen2.5-7B-Instruct (local)`) for the UI. Falls back
        to the slug when no display name is configured or the slug is
        unknown. Reads the registry's merged
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
                          sanitized: str, model_text: str, entry: CacheEntry,
                          det: Detector, client_output: str,
                          check_latency_ms: float, eff: EffectivePolicy) -> dict | None:
        """C4 keystone: re-verify the cached text against the CURRENT
        request's per-app detector + ctx before ever serving it -- this is
        the step-9 verification for the served response; compress/gateway/
        verify are NOT re-run. Returns None (never a response) when the
        re-verify fails, so the caller treats this as an ordinary MISS and
        falls through to the normal flow -- the cached text is never served
        un-re-verified (see docs/architecture/specs/semantic-cache-residency-spec.md
        C4).

        `client_output` is threaded through for the same reason the detector
        is: a cache hit is a SERVED response, so it must obey the egress axis
        by exactly the rule the live path uses. Hand this the request's real
        detector and its real `client_output` -- selecting one here would be a
        second copy of that rule, and a cached answer that bypassed (or
        gratuitously applied) egress redaction is precisely the drift the
        single `verify()` decision exists to prevent.

        The caller applies the SAME eligibility guard before calling (so the
        trace can name the refusal); this repeats it because `_serve_from_cache`
        is the function that actually hands cached text to a client, and an
        eligibility check that lives only in its caller is one refactor away
        from being skipped."""
        # Second guard (P1b design §5/§12): a cache hit bypasses the router
        # entirely, so nothing else on this path re-applies the request's own
        # eligible set. An entry produced by an endpoint THIS request may not
        # reach -- a region the app now excludes, a provider residency now
        # forbids, a model dropped from `allowed_models` since it was cached --
        # is a MISS, not a served response with an honest certificate saying it
        # should not have been sent. Checked against the eligible set rather
        # than folded into the cache's partition key: the key cannot know about
        # `allowed_models` or the three data-handling declarations, and a key
        # drifts out of date the moment a rule is added, where this cannot.
        served = self._eligible_now(ctx, entry.model)
        if served is None or served.provider != entry.provider:
            # The provider comparison is not redundant with the name match:
            # `CacheEntry` self-describes the provider that PRODUCED the text
            # (the same field C3's residency re-check reads), so if the slug
            # now resolves to a different provider the cached answer came from
            # somewhere this request never evaluated. See `_eligible_now`.
            return None
        text, vrec = verify(det, ctx, entry.response_text,
                            client_output=client_output)
        if not vrec.passed:
            return None
        # Priced on what the MODEL would have been sent (`model_text`), not on
        # the redacted view -- the two differ only under `model_input: raw`,
        # where the redacted copy is not what a real call would have cost.
        tokens_in = count_tokens(model_text)
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
        # No router ran this request (the cache short-circuits it) -- so there
        # is no "fallback" to check, and `chosen_in_eligible_set` is True by
        # CONSTRUCTION rather than by measurement: the guard at the top of this
        # function returned a miss for any other answer. It stays in the
        # certificate (an auditor reads one shape for every served response,
        # cached or routed) and it stays honest, because the only path that
        # reaches here is the one that just proved it.
        checks = {
            "chosen_in_eligible_set": True,
            "fallback_in_eligible_set": None,
        }
        return {
            "id": request_id,
            "model": entry.model,
            "model_display": self._display_name(entry.model),
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            # cached_prompt_tokens left at its default 0: a semantic-cache hit
            # makes no provider call, so there is no cache-usage figure to
            # report -- 0 is the truth here, not a missing value.
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
                       # EFFECTIVE posture (post-inherit, post-ceiling),
                       # derived from the COMPILED axes, never the raw
                       # nullable column -- see `_policy_mode_stamp`.
                       "policy_mode": self._policy_mode_stamp(eff),
                       # Task 8: the data-fidelity mode this request actually
                       # ran under -- "exact" means step 5 never transformed
                       # this app's content. A DATA control, distinct from
                       # `policy_mode` (a protection posture); see
                       # `ExecutionPolicyContext.content_integrity`.
                       "content_integrity_mode": ctx.content_integrity,
                       # Task 9: the decision certificate (design §6).
                       # `served` is the ALREADY-RESOLVED PoolCandidate the
                       # eligibility re-check above looked up (P1b Task 6):
                       # reuse it rather than looking `entry.model` up again.
                       **self._certificate(
                           eff, ctx, det, self._endpoint_registry_version(),
                           self._endpoint_declaration(served)),
                       "checks": checks},
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
                  session_id: str | None = None,
                  internals: dict | None = None) -> dict:
        # `internals` is an out-parameter: a dict the CALLER owns, which this
        # method fills with the values the telemetry exporter needs and nobody
        # may serialize (`_publish_telemetry_views`). Optional, so a script or
        # a test that only wants the response can ignore it entirely.
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
        # Compiled effective policy (P1a): `_gate_inputs` builds the detector
        # from the axes a consumer actually needs, and every selection below
        # reads `eff.protection` -- never a `policy_mode` string.
        #
        # Fail closed (CLAUDE.md rule 5): any exception out of the gate is a
        # PolicyGateError, which the route maps to a 503. Never a permissive
        # default, and nothing external has run at this point.
        try:
            det, rules, eff = self._gate_inputs(app_policy, profile)
            ctx, reps, eff = self._gate.evaluate(
                request_id, tenant_id, request, app_policy=app_policy,
                detector=det, rules=rules, eff=eff)
        except PolicyGateError:
            raise
        except Exception as e:
            raise PolicyGateError("policy evaluation failed") from e
        # P2b-2 (Task 3): the content-free outcome-record context, set right
        # after `ctx` exists so it is populated even when the request later
        # fails at routing (NoEligibleCandidate) -- app/apps/outcomes.py's
        # failure builder needs it either way. `complexity` is folded in once
        # compression has run, below.
        if internals is not None:
            internals["outcome_ctx"] = outcome_context(ctx)
        self._publish_telemetry_views(internals, reps)
        policy_mode = self._policy_mode_stamp(eff)
        # The egress axis, read once and handed to every consumer that returns
        # bytes to the client (step-9 verify, the tool-call redactor, the cache
        # re-verify). It is INDEPENDENT of `model_input` below: a trusted model
        # may be sent the real value while the client response is redacted.
        client_output = eff.protection.client_output
        # `sanitized` is the REDACTED view when a detector ran, and the raw
        # text otherwise -- exactly the single value the gate used to return.
        # It is what telemetry/console and memory read; it is NOT automatically
        # what the model gets. That selection is `model_input`'s, below.
        sanitized = (reps.text_redacted if reps.text_redacted is not None
                     else reps.text_raw)
        model_text = reps.text_raw if eff.protection.model_input == "raw" else sanitized
        if ctx.requested_model is not None:
            pool = self._reg.effective_pool() if self._reg else self._s.routing.model_pool
            # Asked of `app/policy/feasible.py`, not re-derived. The inline set
            # this replaced was a second authority over eligibility: it gated
            # app scope on `model_scope_active` (so a context-side rule was
            # ignored for every app that set no allowlist) and knew nothing of
            # P1b's data-handling stage, so a pinned model excluded for its
            # region read the generic "not permitted for application" instead
            # of the rule that actually refused it. `ctx.requested_model` is
            # already set, so the seam's own pin stage leaves exactly the
            # pinned candidate eligible when it survives every other stage.
            permitted = {c.name for c in feasible_set(
                self._s, self._reg, ctx, tenant_id=tenant_id).eligible}
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
        # into one sanitized blob) -- a client's tool loop must never be
        # hijacked. It skips the semantic cache and memory, both of which
        # assume that single flattened prompt.
        tools = request.tools
        tool_choice = request.tool_choice
        tool_mode = _is_tool_conversation(request)

        if tool_mode:
            # Preserve roles + tool structure. Which per-message view goes to
            # the model is `model_input`'s call, exactly as on the flattened
            # path below: `redact` sends the gate's per-message redacted copy
            # (content AND replayed tool_call arguments masked -- CLAUDE.md
            # rule 1), `raw` sends the client's messages untouched, so a
            # replayed tool call's arguments reach the model verbatim.
            # `reps.redacted` is None only when no detector ran, in which case
            # there is nothing to redact and the raw view IS the redacted one.
            san_messages = list(reps.raw if eff.protection.model_input == "raw"
                                else (reps.redacted or reps.raw))
            recall = RecallResult([], None)
        else:
            # Step 4 (task mode) always decides "direct" -- agent loops are
            # client-driven, so there is no internal multi-step mode to divert
            # to (see app/routing/task_mode.py).
            # `model_text` -- the raw flattened text under `model_input: raw`,
            # the redacted one under `redact`. Compression (step 5) still runs
            # strictly after the gate on whatever this is (CLAUDE.md rule 7).
            san_messages = [Message(role=request.messages[-1].role, content=model_text)]

            # Memory READ (Feature 5, Task 2): after san_messages (post-gate)
            # and before compress() -- recalled context is prepended
            # so it gets compressed + priced like any other request tokens, no
            # separate untracked token path.
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

        # Feature #6 — semantic cache CHECK (direct path only; see the
        # constructor comment and run_stream for why it never touches this).
        # Runs after san_messages, before compress: a hit
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
                        allowed_providers=ctx.allowed_providers, query=model_text)
                except Exception:
                    # Fail-safe (CLAUDE.md rule 5 spirit): a cache error must
                    # never crash a served request -- skip the cache and serve
                    # normally, exactly as if this were a miss.
                    log.exception("semantic cache lookup failed (swallowed); "
                                  "serving normally")
                check_latency_ms = (time.monotonic() - t0) * 1000
                # P1b design §5/§12: an entry whose model is not in THIS
                # request's eligible set is a MISS. Checked here, ahead of the
                # re-verify, so the trace can NAME the refusal (an operator
                # seeing a stubborn miss needs to know an entry existed and why
                # it was refused) and so the detector never runs over text that
                # was never servable. `_serve_from_cache` repeats the check --
                # it is the function that hands cached text to a client.
                refused: str | None = None
                if entry is not None:
                    # Name AND provider -- the entry self-describes the
                    # provider that produced it; see `_eligible_now`.
                    cand = self._eligible_now(ctx, entry.model)
                    if cand is None or cand.provider != entry.provider:
                        refused = "entry_not_in_eligible_set"
                        entry = None
                if entry is not None:
                    # C4 KEYSTONE: re-verify with the CURRENT request's
                    # detector/ctx before ever serving cached text. A failed
                    # re-verify is treated as an ordinary miss below (falls
                    # through to compress/gateway/verify), never served.
                    hit_resp = self._serve_from_cache(
                        request_id, ctx, sanitized, model_text, entry, det,
                        client_output, check_latency_ms, eff)
                    if hit_resp is not None:
                        return hit_resp
                # `refused` is recorded on the trace but deliberately NOT
                # assigned to `cache_bypass_reason`: that variable also gates
                # the STORE below, and this request's own (eligible) answer is
                # exactly the entry worth caching.
                cache_record = CacheRecord(
                    enabled=True, hit=False, bypass_reason=refused or "",
                    entries_size=self._cache.size,
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
        if internals is not None:
            internals["outcome_ctx"]["complexity"] = complexity

        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint,
            tools=tools, tool_choice=tool_choice, params=request.generation_params())

        text, vrec = verify(det, ctx, result.text, client_output=client_output)
        if not vrec.passed:
            if internals is not None:
                # P2b-2 (final review #1): a model WAS served before step 9
                # refused it, so the route's `verify_failed` outcome row is
                # attributed from this content-free trace -- ids, enums and
                # numbers only; every free-text `reason` is dropped here, not
                # merely ignored downstream.
                #
                # Guarded: this is telemetry FOR the outcome row. If it raises,
                # the client must still get the step-9 422 below -- a stash
                # failure can never turn a verification refusal into a 500.
                try:
                    internals["outcome_trace"] = outcome_trace(records, vrec)
                    internals["outcome_usage"] = {
                        "prompt_tokens": result.tokens_in,
                        "completion_tokens": result.tokens_out,
                        "cost_usd": sum(r.actual_cost_usd or 0.0 for r in records),
                    }
                except Exception:
                    _obs_log.warning(
                        "outcome trace stash failed on verify failure request=%s",
                        ctx.request_id, exc_info=True)
            raise PipelineRejected(vrec.reason)

        # Egress tool-call redaction (Task 2): verify() above only scans
        # `result.text` -- a model that answers via `tool_calls` (content is
        # often null) would otherwise reach the client with raw PII in
        # `function.arguments` untouched (CLAUDE.md rule 2). Reuse the SAME
        # `redact_tool_calls_with_entities` helper ingress uses
        # (app/policy/gate.py) -- one JSON-safe redaction implementation, both
        # directions, and no separate detection pass here (the helper runs at
        # most two `analyze` calls in total): the entity TYPES it found are
        # folded into `vrec.entities_found` (merged, sorted, de-duplicated) so
        # the trace stays honest about what egress carried, not just the text
        # half of it -- values never enter the record. `raw_tool_calls`
        # (pre-redaction) is kept ONLY for `capture_io: full`'s own-project
        # export (routes.py pops it right alongside `raw_completion`, same
        # never-client-visible contract); it is never returned to the client.
        # A None/empty `result.tool_calls` leaves `vrec` untouched.
        #
        # It is the SECOND egress surface, so it reads the SAME axis through the
        # same `egress_detector()` helper `verify()` itself uses -- a tool-call
        # argument must not be scrubbed for an app whose completion text is
        # returned verbatim, nor left raw for one whose text is redacted.
        raw_tool_calls = result.tool_calls
        tc_redaction = redact_tool_calls_with_entities(
            raw_tool_calls, egress_detector(det, client_output))
        redacted_tool_calls = tc_redaction.tool_calls
        if tc_redaction.entity_types:
            vrec = vrec.model_copy(update={
                "entities_found": tuple(sorted(
                    set(vrec.entities_found) | tc_redaction.entity_types))})

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
        # cache the MODEL-FACING query and the post-verify `text` (never
        # `result.text`/raw_completion). Keyed on what the model actually saw,
        # not on the redacted view: under `model_input: raw` two different
        # SSNs redact to the same placeholder, so keying on the redacted copy
        # would serve one customer's answer for another's question. The cache
        # therefore holds exactly what the model was sent -- which is what it
        # already held for every app before the compiler, both postures.
        # C3 residency: the store
        # itself refuses to persist a cloud-produced entry into an
        # onprem_only partition -- passing the REAL ctx values here is what
        # makes that refusal apply to real traffic.
        if cache_active and cache_bypass_reason is None:
            try:
                provider = next(
                    (c.provider for c in self._pool() if c.name == result.model), "?")
                self._cache.store(
                    tenant_id=tenant_id, onprem_only=ctx.onprem_only,
                    allowed_providers=ctx.allowed_providers, query=model_text,
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
        # delivered".
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
        # `redacted_tool_calls` (not `result.tool_calls`): the client-facing
        # message must carry egress-redacted arguments (Task 2).
        tool_calls = redacted_tool_calls or None
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
                           total_tokens=result.tokens_in + result.tokens_out,
                           cached_prompt_tokens=result.cached_prompt_tokens
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
                       # EFFECTIVE posture (post-inherit, post-ceiling),
                       # derived from the COMPILED axes, never the raw
                       # nullable column -- see `_policy_mode_stamp`.
                       "policy_mode": policy_mode,
                       # Task 8: the data-fidelity mode this request actually
                       # ran under -- "exact" means step 5 never transformed
                       # this app's content. A DATA control, distinct from
                       # `policy_mode` (a protection posture); see
                       # `ExecutionPolicyContext.content_integrity`.
                       "content_integrity_mode": ctx.content_integrity,
                       # Task 9: the decision certificate (design §6).
                       **self._certificate(
                           eff, ctx, det, self._endpoint_registry_version(),
                           self._endpoint_declaration(
                               self._candidate_by_name(result.model)),
                           self._served_scoring(records)),
                       "checks": self._eligibility_checks(ctx, records)},
            "sanitized_prompt": sanitized,
            "raw_completion": result.text,
            # Internal-only, popped in routes.py alongside `raw_completion` --
            # NEVER serialized to the client. Pre-redaction tool_calls, kept
            # only so `capture_io: full` can export the model's raw arguments
            # to the app's OWN Langfuse project (CLAUDE.md rule 2 exception).
            "raw_tool_calls": raw_tool_calls,
        }

    async def run_stream(self, request_id: str, tenant_id: str,
                         request: ChatRequest, app_policy=None,
                         profile: AppPrivacyProfile | None = None,
                         session_id: str | None = None,
                         internals: dict | None = None
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
        eagerly runs the gate and the runtime floor, then returns
        `(deltas, get_meta)`:
          * `deltas` -- an async generator yielding ordered token-delta `str`s.
          * `get_meta` -- a zero-arg callable returning the final metadata dict
            (records/trace, two-lever cost, tokens, model, ctx-derived policy,
            `sanitized_prompt`, `raw_completion`, and the "waived" verification
            marker). It is populated only once `deltas` is fully consumed, and
            raises if called before then.

        Why a coroutine returning a pair (not a bare async generator): the gate
        and the runtime floor MUST run BEFORE any token is emitted, so that a
        `StreamFallback` (onprem_only / require_verification, or a client-
        driven tool conversation) is raised eagerly -- before the caller has
        committed to a StreamingResponse -- letting the route fall back to
        `verified_then_stream` cleanly. A bare async generator would defer
        those checks to first-iteration, after the response has started.

        Nothing external sees the raw request before the gate: the gate (with
        the per-app detector `_gate_inputs` composes, mirroring `run()`) runs
        first; compression runs strictly after it.
        """
        # Gate first -- the SAME `_gate_inputs` composition run() uses (the
        # compiled effective policy, its detector, optional profile
        # recognizers/rules; fail closed), so the streaming path can never
        # drift from the direct one.
        try:
            det, rules, eff = self._gate_inputs(app_policy, profile)
            ctx, reps, eff = self._gate.evaluate(
                request_id, tenant_id, request, app_policy=app_policy,
                detector=det, rules=rules, eff=eff)
        except PolicyGateError:
            raise
        except Exception as e:
            raise PolicyGateError("policy evaluation failed") from e
        # P2b-2 (Task 3): same content-free outcome-record context as run(),
        # set right after `ctx` exists so it survives a later routing
        # failure. `complexity` is folded in once compression has run, below.
        if internals is not None:
            internals["outcome_ctx"] = outcome_context(ctx)
        self._publish_telemetry_views(internals, reps)
        policy_mode = self._policy_mode_stamp(eff)
        # Same egress axis run() reads, selecting the detector `incremental_
        # verify`'s hold-back redactor runs with (attached to `get_meta` below):
        # an app that has declared `client_output: raw` gets its tokens through
        # untouched, exactly as its non-streamed completions come back verbatim.
        egress_det = egress_detector(det, eff.protection.client_output)
        sanitized = (reps.text_redacted if reps.text_redacted is not None
                     else reps.text_raw)
        model_text = reps.text_raw if eff.protection.model_input == "raw" else sanitized

        # A client-driven tool conversation needs run()'s structured path: it
        # forwards `tools` and keeps per-message roles/tool_call_ids, where the
        # live token path below flattens everything into one prompt and has no
        # way to stream a tool_calls answer. Serve it as one complete
        # completion; the route then emits it (tool_calls included) as SSE.
        if _is_tool_conversation(request):
            raise StreamFallback(
                "tool-calling conversation: served as one complete completion, "
                "then streamed")

        # Runtime floor (Global Constraint 1) -- the privacy backstop, in ONE
        # place: an onprem_only or verification-required request must be verified
        # as a COMPLETE response, never streamed unverified.
        if ctx.onprem_only or ctx.require_verification:
            raise StreamFallback(
                "residency/verification floor: response must be verified as a "
                "complete response before egress")

        # Egress floor (design §2.5), in the same ONE place. `stream_unverified`
        # forwards backend deltas as they arrive, so no egress redaction can run
        # over the complete response -- which an app that NAMED
        # `client_output: redact` has asked for. `validate_effective` already
        # refuses that combination at write time
        # (STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT); this is the backstop for
        # a row that exists anyway (written before that rule shipped, or straight
        # into the DB), and it floors to `verified_then_stream` -- the client
        # still gets SSE, off a completed, verified, redacted answer.
        #
        # Scoped to an EXPLICIT `client_output`, deliberately and for exactly the
        # reason the write-time rule is: `redact` is also what the `enforced`
        # preset resolves to, so keying on the effective value alone would floor
        # every `stream_unverified` app in existence -- silently repealing the
        # mode instead of honouring a contradiction its operator wrote down.
        # `incremental_verify` is untouched: it DOES redact egress (hold-back),
        # so it keeps the guarantee this mode cannot.
        p = eff.protection
        if (p.stream_mode == "stream_unverified" and p.client_output == "redact"
                and p.sources.get("client_output") == APP_SOURCE):
            raise StreamFallback(
                "client_output=redact: unverified streaming cannot redact egress, "
                "so the response is verified as a complete response first")

        # `model_input` selects the model-facing view here exactly as in run().
        san_messages = [Message(role=request.messages[-1].role, content=model_text)]
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
        if internals is not None:
            internals["outcome_ctx"]["complexity"] = complexity
        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        stream, records = await self._gateway.run_stream(
            ctx, comp.messages, comp.tokens_after, complexity, hint,
            params=request.generation_params())

        meta: dict = {}

        async def _deltas() -> AsyncIterator[str]:
            result: GenerationResult | None = None
            async for chunk in stream:
                if chunk.delta is not None:
                    yield chunk.delta
                if chunk.final is not None:
                    result = chunk.final
            # The backend stream ended normally: anything raised from here on
            # is a pipeline outcome, not a backend failure (spec §4).
            get_meta.backend_completed = True   # type: ignore[attr-defined]
            if result is None:
                raise PipelineRejected("stream ended without a final result")
            meta.update(self._assemble_stream_meta(
                request_id, ctx, sanitized, comp, records, result, eff,
                det, recall.record))

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
        get_meta.detector = egress_det   # type: ignore[attr-defined]
        get_meta.ctx = ctx        # type: ignore[attr-defined]
        # The router's chosen model, known eagerly (records[0] is the
        # step="router" DecisionRecord run_stream always returns) -- the
        # route meters it on a streamed failure (spec §4), before any delta
        # (or a terminal result) exists.
        get_meta.chosen_model = records[0].chosen   # type: ignore[attr-defined]
        # Set True by `_deltas()` once the backend stream has ended normally,
        # so the route never meters a later pipeline error (e.g. assembling
        # the final metadata) as a backend failure.
        get_meta.backend_completed = False   # type: ignore[attr-defined]
        return _deltas(), get_meta

    def _assemble_stream_meta(self, request_id: str, ctx: ExecutionPolicyContext,
                              sanitized: str, comp, records, result: GenerationResult,
                              eff: EffectivePolicy, det: Detector,
                              mem_record: MemoryRecord | None = None) -> dict:
        """Assemble the final streaming metadata dict, mirroring run()'s record,
        two-lever cost, and quality assembly. Step-9 verification is WAIVED on
        this path: a VerificationRecord(performed=False, passed=True) is recorded
        in the trace so the "why" stays visible (Global Constraint 2), and the
        top-level `verification` marker is "waived" (Tasks 6/7 refine it)."""
        policy_mode = self._policy_mode_stamp(eff)
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
                           total_tokens=result.tokens_in + result.tokens_out,
                           cached_prompt_tokens=result.cached_prompt_tokens
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
                       # EFFECTIVE posture (post-inherit, post-ceiling),
                       # derived from the COMPILED axes, never the raw
                       # nullable column -- see `_policy_mode_stamp`.
                       "policy_mode": policy_mode,
                       # Task 8: the data-fidelity mode this request actually
                       # ran under -- "exact" means step 5 never transformed
                       # this app's content. A DATA control, distinct from
                       # `policy_mode` (a protection posture); see
                       # `ExecutionPolicyContext.content_integrity`.
                       "content_integrity_mode": ctx.content_integrity,
                       # Task 9: the decision certificate (design §6).
                       **self._certificate(
                           eff, ctx, det, self._endpoint_registry_version(),
                           self._endpoint_declaration(
                               self._candidate_by_name(result.model)),
                           self._served_scoring(records)),
                       "checks": self._eligibility_checks(ctx, records)},
            "sanitized_prompt": sanitized,
            "raw_completion": result.text,
            # Step-9 verification marker: "waived" this task. Tasks 6/7 refine to
            # "waived" (stream_unverified) / "incremental" (incremental_verify).
            "verification": "waived",
        }
