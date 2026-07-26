from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.models import Entity, ExecutionPolicyContext

if TYPE_CHECKING:  # avoid importing the apps layer at module load
    from app.apps.policy import AppPolicyView

_RESIDENCY_RANK = {"any": 0, "private_cloud_ok": 1, "onprem_only": 2}


def _stricter(a: str, b: str) -> str:
    return a if _RESIDENCY_RANK[a] >= _RESIDENCY_RANK[b] else b


def _match_rule(settings: Settings, etypes: set[str]):
    # Rules are ordered most-to-least severe in policy.yaml; return the first whose
    # entity set overlaps. Entity sets are disjoint today, so order only matters if a
    # future entity type appears in more than one rule.
    for rule in settings.policy.sensitivity_rules:
        if rule.entities and etypes & set(rule.entities):
            return rule
    return settings.policy.sensitivity_rules[-1]  # the public/empty rule


def build_context(settings: Settings, request_id: str, tenant_id: str,
                  entities: list[Entity], redaction_map_id: str | None,
                  app_policy: "AppPolicyView | None" = None
                  ) -> ExecutionPolicyContext:
    etypes = {e.type for e in entities}
    rule = _match_rule(settings, etypes)

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
    # `inference_backend: none` => no on-prem serving path exists, regardless of
    # providers.onprem.enabled. Drop it from `enabled` so an onprem_only request
    # resolves to an empty allowed set and is rejected downstream by the router's
    # existing "no candidates after residency filter" path -- never silently routed
    # to cloud (CLAUDE.md backend rule; inference-backends skill).
    if settings.serving.inference_backend == "none":
        enabled = enabled - {"onprem"}
    onprem_providers = {c.provider for c in settings.routing.model_pool
                        if c.provider == "onprem"}
    if onprem_only:
        allowed = enabled & onprem_providers
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

    allowed_models = frozenset(
        c.name for c in settings.routing.model_pool if _model_ok(c))

    return ExecutionPolicyContext(
        request_id=request_id,
        tenant_id=tenant_id,
        sensitivity=rule.sensitivity,
        onprem_only=onprem_only,
        allowed_providers=frozenset(allowed),
        allowed_models=allowed_models,
        max_cost_usd=settings.policy.budgets["default_max_spend_usd"],
        max_latency_ms=int(settings.policy.budgets["default_max_latency_ms"]),
        risk_class=rule.risk_class,
        require_verification=rule.require_verification,
        detected_entities=tuple(sorted(etypes)),
        redaction_map_id=redaction_map_id,
    )
