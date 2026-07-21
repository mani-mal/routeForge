from __future__ import annotations

from app.config.schema import Settings
from app.models import Entity, ExecutionPolicyContext

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
                  entities: list[Entity], redaction_map_id: str | None
                  ) -> ExecutionPolicyContext:
    etypes = {e.type for e in entities}
    rule = _match_rule(settings, etypes)

    tenant = settings.policy.tenants.get(
        tenant_id, settings.policy.tenants["default"])
    residency = _stricter(rule.residency, tenant.default_residency)
    onprem_only = residency == "onprem_only"

    enabled = {p for p, c in settings.serving.providers.items() if c.enabled}
    onprem_providers = {c.provider for c in settings.routing.model_pool
                        if c.provider == "onprem"}
    if onprem_only:
        allowed = enabled & onprem_providers
    else:
        allowed = enabled  # private_cloud_ok and any both allow enabled cloud here

    allowed_models = frozenset(
        c.name for c in settings.routing.model_pool if c.provider in allowed)

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
