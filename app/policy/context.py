from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.models import Entity, ExecutionPolicyContext

if TYPE_CHECKING:  # avoid importing the apps layer at module load
    from app.apps.policy import AppPolicyView
    from app.apps.registry import ModelRegistry

_RESIDENCY_RANK = {"any": 0, "private_cloud_ok": 1, "onprem_only": 2}


def _stricter(a: str, b: str) -> str:
    return a if _RESIDENCY_RANK[a] >= _RESIDENCY_RANK[b] else b


def _match_rule(rules, etypes: set[str]):
    # Rules are ordered most-to-least severe; return the first whose entity set
    # overlaps. Entity sets are disjoint today, so order only matters if a future
    # entity type appears in more than one rule. `rules` may be either the config
    # SensitivityRule list (`.entities` is a list) or a list of SensitivityRuleView
    # (`.entities` is a frozenset) -- `set(...)` normalizes both for the `&`.
    for rule in rules:
        if rule.entities and etypes & set(rule.entities):
            return rule
    return rules[-1]  # the public/empty rule


def match_sensitivity_rule(settings: Settings, app_rules: list | None,
                           etypes: set[str]):
    """The one declared cross-axis rule (design §5) for these detected entity
    types: the app's own resolved `sensitivity_rules` when it has a privacy
    profile, else `config/policy.yaml`'s.

    Exposed so the gate can hand the SAME matched rule to
    `app/policy/compile.py::apply_signals` that `build_context` below uses,
    instead of each re-deriving it and drifting.
    """
    rules = app_rules if app_rules is not None else settings.policy.sensitivity_rules
    return _match_rule(rules, etypes)


def _placement_providers(settings: Settings, residency: str,
                         app_policy: "AppPolicyView | None",
                         registry: "ModelRegistry | None") -> set[str]:
    enabled = {p for p, c in settings.serving.providers.items() if c.enabled}
    # On-prem availability is derived from the registry, not a boot flag: with no
    # healthy onboarded on-prem model, drop "onprem" from `enabled` so an
    # onprem_only request resolves to an empty allowed set and is rejected
    # downstream by the router's "no candidates after residency filter" path --
    # never silently routed to cloud (CLAUDE.md backend rule; replaces the old
    # inference_backend == "none" drop).
    if registry is not None and not registry.has_healthy_onprem():
        enabled = enabled - {"onprem"}
    if residency == "onprem_only":
        # onprem_only means exactly that: the only legal provider is "onprem",
        # never derived from routing.model_pool membership (on-prem candidates
        # are onboarded at runtime — the static YAML pool may have none —
        # see registry-derived `enabled` drop above for actual availability).
        allowed = enabled & {"onprem"}
    else:
        allowed = enabled  # private_cloud_ok and any both allow enabled cloud here

    # Per-application narrowing (never widening): the app may permit only a
    # subset of providers; models may be further limited by tier and by an
    # explicit model allowlist. All null => no extra restriction.
    if app_policy is not None and app_policy.allowed_providers is not None:
        allowed = allowed & app_policy.allowed_providers
    return allowed


def _in_app_scope(c, allowed: set[str], app_policy: "AppPolicyView | None") -> bool:
    if c.provider not in allowed:
        return False
    if app_policy is not None:
        if (app_policy.allowed_tiers is not None
                and c.tier not in app_policy.allowed_tiers):
            return False
        if (app_policy.allowed_models is not None
                and c.name not in app_policy.allowed_models):
            return False
    return True


def registry_permits(registry: "ModelRegistry | None", c, tenant_id: str) -> bool:
    """Per-model registry gates on top of policy: an onboarded model restricted
    to other apps, or a disabled on-prem row, is never requestable."""
    if registry is None:
        return True
    return (registry.is_allowed_for_app(c.name, tenant_id)
            and (c.provider != "onprem" or registry.is_enabled(c.name)))


def pinned_model_reason(pool, ctx: ExecutionPolicyContext, registry: "ModelRegistry | None",
                        tenant_id: str, name: str) -> str:
    """Which gate refused a pinned `model`, phrased as what to change."""
    c = next((c for c in pool if c.name == name), None)
    not_found = "it is not onboarded, or is disabled (see GET /v1/models)"
    if c is None:
        return not_found
    if c.provider not in ctx.allowed_providers:
        return (f"provider {c.provider!r} is excluded by residency or the "
                "application's allowed_providers")
    if ctx.model_scope_active and name not in ctx.allowed_models:
        return "it is outside the application's allowed_tiers / allowed_models"
    if registry is not None and not registry.is_allowed_for_app(name, tenant_id):
        # Same text as an unknown slug: naming allowed_app_ids would disclose
        # that a model scoped to another application exists (cross-tenant).
        return not_found
    return "it is disabled"


def requestable_models(settings: Settings, registry: "ModelRegistry | None",
                       tenant_id: str, app_policy: "AppPolicyView") -> list:
    """The pool candidates this application may pin as `model` on a request
    that carries no sensitive data -- what GET /v1/models lists. Uses the same
    placement, allowlist and registry rules as the pipeline's pinned-model
    check. A request whose content escalates residency (e.g. an SSN under an
    onprem_only rule) can narrow this further, never widen it."""
    allowed = _placement_providers(settings, app_policy.residency, app_policy, registry)
    pool = registry.effective_pool() if registry is not None else settings.routing.model_pool
    return [c for c in pool
            if _in_app_scope(c, allowed, app_policy)
            and registry_permits(registry, c, tenant_id)]


def build_context(settings: Settings, request_id: str, tenant_id: str,
                  entities: list[Entity], redaction_map_id: str | None,
                  app_policy: "AppPolicyView | None" = None,
                  app_rules: list | None = None,
                  registry: "ModelRegistry | None" = None,
                  escalate_residency: bool = True,
                  ) -> ExecutionPolicyContext:
    """Step 3: the immutable ExecutionPolicyContext.

    `escalate_residency` is the app's compiled
    `escalate_residency_on_detection` (design §5) — whether the declared
    cross-axis rule may raise PLACEMENT for this app. It is the only switch
    over placement here, and it is one-directional: the rule may **tighten**
    residency and never loosen it, so `False` can only ever leave the app's own
    residency floor exactly where the operator set it. Sensitivity,
    `risk_class` and `require_verification` still come from the matched rule
    either way — they are protection/verification signals, not placement.
    No protection axis is read anywhere in this function; that is the
    noninterference property the compiler exists to make checkable.
    """
    etypes = {e.type for e in entities}
    rule = match_sensitivity_rule(settings, app_rules, etypes)

    # Residency: stricter of the entity rule and the application's own residency.
    # With a per-app policy we take residency from it (seeded to match
    # policy.yaml); otherwise fall back to the policy.yaml tenant default so the
    # internal/direct-pipeline paths keep working unchanged.
    if app_policy is not None:
        app_residency = app_policy.residency
    else:
        tenant = settings.policy.tenants.get(
            tenant_id, settings.policy.tenants["default"])
        app_residency = tenant.default_residency
    # Raise-only, and only when the app declares the escalation on (default).
    residency = (_stricter(rule.residency, app_residency) if escalate_residency
                 else app_residency)
    onprem_only = residency == "onprem_only"
    allowed = _placement_providers(settings, residency, app_policy, registry)

    # Onboarded on-prem models never appear in the static YAML
    # `settings.routing.model_pool` -- read the registry's merged pool when
    # available, or every onboarded model would be silently excluded here
    # regardless of the app's own allowed_tiers/allowed_models policy.
    pool = registry.effective_pool() if registry is not None else settings.routing.model_pool
    allowed_models = frozenset(c.name for c in pool if _in_app_scope(c, allowed, app_policy))
    model_scope_active = bool(
        app_policy is not None
        and (app_policy.allowed_tiers is not None
             or app_policy.allowed_models is not None)
    )

    return ExecutionPolicyContext(
        request_id=request_id,
        tenant_id=tenant_id,
        sensitivity=rule.sensitivity,
        onprem_only=onprem_only,
        allowed_providers=frozenset(allowed),
        allowed_models=allowed_models,
        model_scope_active=model_scope_active,
        max_cost_usd=(app_policy.max_cost_usd_per_request
                      if app_policy is not None
                      and app_policy.max_cost_usd_per_request is not None
                      else settings.policy.budgets["default_max_spend_usd"]),
        max_latency_ms=int(settings.policy.budgets["default_max_latency_ms"]),
        risk_class=rule.risk_class,
        require_verification=rule.require_verification,
        detected_entities=tuple(sorted(etypes)),
        redaction_map_id=redaction_map_id,
        compression_override=(app_policy.compression_enabled
                              if app_policy is not None else None),
        app_policy_version=(app_policy.version() if app_policy is not None else ""),
    )
