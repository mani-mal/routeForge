import sys

from app.config.loader import load_settings
from app.config.schema import Settings
from app.policy.enums import POLICY_MODES
from app.policy.enums import RESIDENCY as valid_residency
from app.prompts.store import load_artifact
from app.routing.strategy import _STRATEGIES as _KNOWN_STRATEGIES


class ConfigLintError(Exception):
    """Raised when a startup config invariant is violated."""


def lint_settings(s: Settings) -> list[str]:
    errs: list[str] = []

    serv_prov = set(s.serving.providers)
    pool_prov = {c.provider for c in s.routing.model_pool}
    price_prov = {p.provider for p in s.pricing.models.values()}
    # Subset, not equality: every provider USED by the pool/pricing must be
    # DECLARED in serving, but serving may declare a provider with no YAML pool
    # member (e.g. onprem) — its members come from onboarding at runtime, not
    # the static config.
    if not (pool_prov <= serv_prov and price_prov <= serv_prov):
        errs.append(f"PROVIDER_UNDECLARED pool={sorted(pool_prov)} "
                    f"pricing={sorted(price_prov)} not subset of serving={sorted(serv_prov)}")

    tiers = set(s.routing.tiers)
    pool_tiers = [c.tier for c in s.routing.model_pool]
    for t in s.routing.risk_floors.values():
        if t not in tiers:
            errs.append(f"RISK_FLOOR_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in set(pool_tiers):
        if t not in tiers:
            errs.append(f"POOL_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    # An empty YAML tier is legal now (no TIER_HAS_NO_MEMBER check) — on-prem
    # tiers are populated by onboarding at runtime, not the static pool.
    for name, p in s.pricing.models.items():
        if p.tier not in tiers:
            errs.append(f"PRICING_TIER_UNKNOWN {name}:{p.tier}")
    for role, t in s.routing.stage_signals.role_tiers.items():
        if t not in tiers:
            errs.append(f"ROLE_TIER_UNKNOWN {role}:{t} not in {sorted(tiers)}")

    # Pool-slug <-> pricing-slug cross-check: every cloud built-in in the pool
    # must have a matching pricing row, or LiteLLMBackend.generate's
    # `price.models[cand.name]` lookup KeyErrors at request time instead of at
    # boot. (Onboarded on-prem rows carry their own price via the registry, so
    # they are exempt — this only covers static YAML pool members.)
    price_names = set(s.pricing.models)
    for c in s.routing.model_pool:
        if c.name not in price_names:
            errs.append(f"POOL_MODEL_NO_PRICE {c.name}")

    for t in tiers:
        if t not in s.routing.tier_quality:
            errs.append(f"TIER_QUALITY_MISSING {t}")
    for t, q in s.routing.tier_quality.items():
        if not (0.0 <= q <= 1.0):
            errs.append(f"TIER_QUALITY_OUT_OF_RANGE {t}={q}")
    for c in s.routing.model_pool:
        if c.quality is not None and not (0.0 <= c.quality <= 1.0):
            errs.append(f"POOL_QUALITY_OUT_OF_RANGE {c.name}={c.quality}")

    scale = set(s.policy.sensitivity_scale)
    if s.policy.default_sensitivity not in scale:
        errs.append(f"DEFAULT_SENSITIVITY_UNKNOWN {s.policy.default_sensitivity}")
    for r in s.policy.sensitivity_rules:
        if r.sensitivity not in scale:
            errs.append(f"RULE_SENSITIVITY_UNKNOWN {r.sensitivity}")

    residency_values = [("rule", r.residency) for r in s.policy.sensitivity_rules]
    residency_values += [(f"tenant:{name}", t.default_residency)
                         for name, t in s.policy.tenants.items()]
    for where, val in residency_values:
        if val not in valid_residency:
            errs.append(
                f"RESIDENCY_UNKNOWN {where}={val} not in {sorted(valid_residency)}")

    # Every ENABLED provider must have a LiteLLM route, or the backend can't
    # reach it. Disabled providers are skipped (their models are filtered out
    # of any request's candidate pool upstream). onprem availability is now
    # per-model (onboarded rows, probed at onboard time), not a boot-time
    # backend axis, so this is just providers.*.enabled.
    for pname, pcfg in s.serving.providers.items():
        enabled = pcfg.enabled
        if enabled and not pcfg.litellm_prefix:
            errs.append(f"PROVIDER_NO_LITELLM_MAP {pname} has no litellm_prefix")

    # Feature 3 (DSPy prompt-optimization) Task 1: every routing.prompt_opt
    # pin must resolve to a real, well-formed artifact on disk. Re-resolves
    # independently from disk (not from `s.active_prompts`) so this check is
    # authoritative even when the loader deferred (see
    # app/config/loader.py::load_settings) because SOME other pin failed --
    # fail closed, never a silent skip (CLAUDE.md rule 5).
    for prompt_id, label in s.routing.prompt_opt.active.items():
        try:
            artifact = load_artifact(s.routing.prompt_opt.artifacts_dir, prompt_id, label)
        except ValueError as e:
            errs.append(f"PROMPT_ARTIFACT_INVALID {prompt_id}:{label} {e}")
            continue
        if artifact is None:
            errs.append(f"PROMPT_ACTIVE_MISSING {prompt_id}:{label}")

    return errs


def enforce(s: Settings) -> None:
    # routing.strategy names a real RoutingStrategy (app/routing/strategy.py's
    # registry) or boot fails loud -- an unknown name must never silently fall
    # back to "default" (CLAUDE.md rule 5, fail closed).
    if s.routing.strategy not in _KNOWN_STRATEGIES:
        raise ConfigLintError(
            f"routing.strategy={s.routing.strategy!r} is not a known strategy; "
            f"known strategies: {sorted(_KNOWN_STRATEGIES)}")

    lf = s.serving.observability.langfuse
    if lf.enabled and not lf.redact_on_export:
        raise ConfigLintError(
            "observability.langfuse.redact_on_export must be true when langfuse is "
            "enabled (egress redaction is mandatory; see CLAUDE.md rule 2)")

    # Eval-capture mode. Enum first (a typo must fail closed with a named
    # violation, not silently behave as `off`).
    _valid_capture = ("off", "redacted", "full")
    if lf.capture_io not in _valid_capture:
        raise ConfigLintError(
            f"observability.langfuse.capture_io must be one of {_valid_capture}; "
            f"got {lf.capture_io!r}")
    # `full` writes RAW prompts/completions into telemetry — a second copy of
    # sensitive data (CLAUDE.md rule 2/6). Residency beats eval convenience: if
    # ANY tenant is onprem_only, that tenant's raw PHI would land in the (single,
    # shared) Langfuse project, leaving the boundary. Reject at boot rather than
    # leak silently. `redacted` is the eval-safe mode for a shared project.
    if lf.enabled and lf.capture_io == "full":
        onprem_tenants = sorted(
            name for name, t in s.policy.tenants.items()
            if t.default_residency == "onprem_only")
        if onprem_tenants:
            raise ConfigLintError(
                "observability.langfuse.capture_io='full' captures RAW request I/O "
                "into telemetry, but these tenants are onprem_only and would leave "
                f"the residency boundary: {onprem_tenants}. Use 'redacted', or point "
                "at a residency-local Langfuse with no onprem_only tenant "
                "(CLAUDE.md rule 6 — residency beats the eval-capture convenience).")
        # No onprem tenant, but raw capture is still on — make it loud.
        import logging
        logging.getLogger("serving.obs").warning(
            "observability.langfuse.capture_io='full': RAW request/response I/O is "
            "being written to Langfuse for eval. Ensure the target project is "
            "residency-local and access-controlled with a retention TTL.")

    # Deployment-wide protection posture. Enum first, same rationale as
    # capture_io above: a typo (`passthru`) currently loads fine and resolves to
    # "not passthrough" — safe in direction, but SILENT, and a silent
    # misconfiguration of the axis that decides whether the gate runs at all is
    # exactly what the boot lint exists to turn into a named boot failure
    # (CLAUDE.md "Config lint at startup"; rule 5 — fail closed, loudly).
    if s.serving.default_policy_mode not in POLICY_MODES:
        raise ConfigLintError(
            f"POLICY_MODE_UNKNOWN policy.default_policy_mode must be one of "
            f"{list(POLICY_MODES)}; got {s.serving.default_policy_mode!r}")

    ccr = s.serving.context_optimization.ccr
    if ccr.enabled and ccr.residency != "local_only":
        raise ConfigLintError(
            "context_optimization.ccr.residency must be 'local_only' "
            f"(CCR is a local cache of originals; got {ccr.residency!r})")
    if ccr.enabled and ccr.export_to_telemetry is not False:
        raise ConfigLintError(
            "context_optimization.ccr.export_to_telemetry must be false "
            "(CCR contents are originals and must never reach telemetry)")

    # Feature 3 (DSPy prompt-optimization) Task 1: fail-closed human-gate
    # guard. A managed prompt may only go live once an operator has marked it
    # `verified: true` AND pinned it active (CLAUDE.md rule 4 -- human-gated
    # promotion; rule 5 -- fail closed). Reuse `s.active_prompts` when the
    # loader already resolved it (the all-success case); otherwise load the
    # pin fresh -- a resolution failure here is skipped, not raised, because
    # `lint_settings` above/below already reports it by name
    # (PROMPT_ACTIVE_MISSING/PROMPT_ARTIFACT_INVALID) and this guard's job is
    # only to judge `verified`, not existence/validity.
    for prompt_id, label in s.routing.prompt_opt.active.items():
        artifact = s.active_prompts.get(prompt_id)
        if artifact is None:
            try:
                artifact = load_artifact(s.routing.prompt_opt.artifacts_dir, prompt_id, label)
            except ValueError:
                continue  # reported as PROMPT_ARTIFACT_INVALID by lint_settings
            if artifact is None:
                continue  # reported as PROMPT_ACTIVE_MISSING by lint_settings
        if not artifact.verified:
            raise ConfigLintError(
                f"routing.prompt_opt pins an UNVERIFIED prompt {prompt_id}:{label}; "
                "a managed prompt must be human-verified (verified: true) before "
                "activation (CLAUDE.md rule 5 — fail closed; rule 4 — human-gated "
                "promotion)")
    # Persistent memory (Feature 5) — same CCR-class posture, plus an
    # enforced-TTL guard the CCR block doesn't need (CCR always sets a
    # ttl_hours > 0 by convention; memory adds an explicit boot-fail so an
    # unbounded memory cache can never ship). CLAUDE.md rule 6.
    mem = s.serving.memory
    if mem.enabled and mem.residency != "local_only":
        raise ConfigLintError(
            "memory.residency must be 'local_only' (memory is a local cache "
            f"of sanitized summaries; got {mem.residency!r}; CLAUDE.md rule 6)")
    if mem.enabled and mem.export_to_telemetry is not False:
        raise ConfigLintError(
            "memory.export_to_telemetry must be false (memory contents must "
            "never reach telemetry — only a counts-only MemoryRecord may; "
            "CLAUDE.md rule 6)")
    if mem.enabled and mem.ttl_hours <= 0:
        raise ConfigLintError(
            "memory.ttl_hours must be > 0 (enforced TTL; an unbounded memory "
            "cache defeats the residency claim; CLAUDE.md rule 6)")
    # Feature #6 semantic cache — same CCR-class guards (CLAUDE.md rule 6), plus
    # an enforced-TTL check CCR doesn't need to repeat (CCR's TTL has no
    # equivalent boot guard; this store's design gate calls it out explicitly —
    # see docs/architecture/specs/semantic-cache-residency-spec.md C5). Only
    # load-bearing when the cache is enabled — a drifted value on a disabled
    # cache does not fail the boot, mirroring the ccr guards above.
    sc = s.serving.semantic_cache
    if sc.enabled and sc.residency != "local_only":
        raise ConfigLintError(
            "semantic_cache.residency must be 'local_only' (a cache hit skips "
            f"the model entirely, so this is a local-only store; got {sc.residency!r})")
    if sc.enabled and sc.export_to_telemetry is not False:
        raise ConfigLintError(
            "semantic_cache.export_to_telemetry must be false (cache contents "
            "are cached responses and must never reach telemetry — only a "
            "counts-only CacheRecord may)")
    if sc.enabled and sc.ttl_hours <= 0:
        raise ConfigLintError(
            "semantic_cache.ttl_hours must be > 0 when enabled (enforced TTL, "
            f"not an unbounded retention liability; got {sc.ttl_hours})")

    errs = lint_settings(s)
    if errs:
        for e in errs:
            print(f"CONFIG_LINT: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    enforce(load_settings())
    print("CONFIG_LINT: ok")
