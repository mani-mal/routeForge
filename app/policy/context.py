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


def build_context(settings: Settings, request_id: str, tenant_id: str,
                  entities: list[Entity], redaction_map_id: str | None,
                  app_policy: "AppPolicyView | None" = None,
                  app_rules: list | None = None,
                  registry: "ModelRegistry | None" = None,
                  ) -> ExecutionPolicyContext:
    etypes = {e.type for e in entities}
    if app_rules is not None:
        rule = _match_rule(app_rules, etypes)
    else:
        rule = _match_rule(settings.policy.sensitivity_rules, etypes)

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
    residency = _stricter(rule.residency, app_residency)
    onprem_only = residency == "onprem_only"

    enabled = {p for p, c in settings.serving.providers.items() if c.enabled}
    # On-prem availability is derived from the registry, not a boot flag: with no
    # healthy onboarded on-prem model, drop "onprem" from `enabled` so an
    # onprem_only request resolves to an empty allowed set and is rejected
    # downstream by the router's "no candidates after residency filter" path --
    # never silently routed to cloud (CLAUDE.md backend rule; replaces the old
    # inference_backend == "none" drop).
    if registry is not None and not registry.has_healthy_onprem():
        enabled = enabled - {"onprem"}
    if onprem_only:
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

    def _model_ok(c) -> bool:
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

    # Onboarded on-prem models never appear in the static YAML
    # `settings.routing.model_pool` -- read the registry's merged pool when
    # available, or every onboarded model would be silently excluded here
    # regardless of the app's own allowed_tiers/allowed_models policy.
    pool = registry.effective_pool() if registry is not None else settings.routing.model_pool
    allowed_models = frozenset(c.name for c in pool if _model_ok(c))
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
        max_cost_usd=settings.policy.budgets["default_max_spend_usd"],
        max_latency_ms=int(settings.policy.budgets["default_max_latency_ms"]),
        risk_class=rule.risk_class,
        require_verification=rule.require_verification,
        detected_entities=tuple(sorted(etypes)),
        redaction_map_id=redaction_map_id,
        compression_override=(app_policy.compression_enabled
                              if app_policy is not None else None),
        app_policy_version=(app_policy.version() if app_policy is not None else ""),
    )
