import sys

from app.config.loader import load_settings
from app.config.schema import Settings
from app.policy.enums import RESIDENCY as valid_residency


class ConfigLintError(Exception):
    """Raised when a startup config invariant is violated."""


def lint_settings(s: Settings) -> list[str]:
    errs: list[str] = []

    serv_prov = set(s.serving.providers)
    pool_prov = {c.provider for c in s.routing.model_pool}
    price_prov = {p.provider for p in s.pricing.models.values()}
    if not (serv_prov == pool_prov == price_prov):
        errs.append(
            f"PROVIDER_NAME_DRIFT serving={sorted(serv_prov)} "
            f"routing={sorted(pool_prov)} pricing={sorted(price_prov)}")

    tiers = set(s.routing.tiers)
    pool_tiers = [c.tier for c in s.routing.model_pool]
    for t in s.routing.risk_floors.values():
        if t not in tiers:
            errs.append(f"RISK_FLOOR_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in set(pool_tiers):
        if t not in tiers:
            errs.append(f"POOL_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in tiers:
        if t not in pool_tiers:
            errs.append(f"TIER_HAS_NO_MEMBER {t}")
    for name, p in s.pricing.models.items():
        if p.tier not in tiers:
            errs.append(f"PRICING_TIER_UNKNOWN {name}:{p.tier}")

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
    # of any request's candidate pool upstream). onprem enabled-ness is the
    # inference_backend axis; azure/bedrock is providers.*.enabled.
    for pname, pcfg in s.serving.providers.items():
        enabled = pcfg.enabled and not (pname == "onprem" and s.serving.inference_backend == "none")
        if enabled and not pcfg.litellm_prefix:
            errs.append(f"PROVIDER_NO_LITELLM_MAP {pname} has no litellm_prefix")

    return errs


_VALID_BACKENDS = ("mock", "vllm", "dynamo", "none")


def enforce(s: Settings) -> None:
    # Fail closed on a typo'd backend with a NAMED violation rather than a raw
    # traceback from make_backend() at boot (project rule: every drift class is
    # a named boot failure).
    if s.serving.inference_backend not in _VALID_BACKENDS:
        raise ConfigLintError(
            f"serving.inference_backend must be one of {_VALID_BACKENDS}; "
            f"got {s.serving.inference_backend!r}")

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

    ccr = s.serving.context_optimization.ccr
    if ccr.enabled and ccr.residency != "local_only":
        raise ConfigLintError(
            "context_optimization.ccr.residency must be 'local_only' "
            f"(CCR is a local cache of originals; got {ccr.residency!r})")
    if ccr.enabled and ccr.export_to_telemetry is not False:
        raise ConfigLintError(
            "context_optimization.ccr.export_to_telemetry must be false "
            "(CCR contents are originals and must never reach telemetry)")

    errs = lint_settings(s)
    if errs:
        for e in errs:
            print(f"CONFIG_LINT: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    enforce(load_settings())
    print("CONFIG_LINT: ok")
