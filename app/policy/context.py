from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.models import Entity, ExecutionPolicyContext
from app.policy.compile import compile_policy, resolve_residency
from app.policy.feasible import (  # noqa: F401  (re-exported: app/pipeline.py imports registry_permits from here)
    APP_SCOPE,
    MODEL_SCOPE,
    REASON_DISABLED,
    RESIDENCY,
    _ctx_scope,
    _placement_providers,
    candidate_rejection,
    feasible_set,
    registry_permits,
)

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


def pinned_model_reason(pool, ctx: ExecutionPolicyContext, registry: "ModelRegistry | None",
                        tenant_id: str, name: str) -> str:
    """Which gate refused a pinned `model`, phrased as what to change.

    The gates themselves are `app/policy/feasible.py`'s, asked one candidate at
    a time with no pin and nothing excluded, so this message can never describe
    a filter the request did not actually hit.
    """
    c = next((c for c in pool if c.name == name), None)
    not_found = "it is not onboarded, or is disabled (see GET /v1/models)"
    if c is None:
        return not_found
    rejection = candidate_rejection(
        c, allowed_providers=ctx.allowed_providers, onprem_only=ctx.onprem_only,
        in_scope=_ctx_scope(ctx), requested_model=None,
        exclude=frozenset(), registry=registry, tenant_id=tenant_id)
    if rejection is None:
        return "it is disabled"
    if rejection.code == RESIDENCY:
        return (f"provider {c.provider!r} is excluded by residency or the "
                "application's allowed_providers")
    if rejection.code == APP_SCOPE:
        return "it is outside the application's allowed_tiers / allowed_models"
    if rejection.code == MODEL_SCOPE and rejection.reason == REASON_DISABLED:
        return "it is disabled"
    # A model scoped to other applications gets the same text as an unknown
    # slug: naming allowed_app_ids would disclose that a model scoped to
    # another application exists (cross-tenant).
    return not_found


def requestable_models(settings: Settings, registry: "ModelRegistry | None",
                       tenant_id: str, app_policy: "AppPolicyView") -> list:
    """The pool candidates this application may pin as `model` on a request
    that carries no sensitive data -- what GET /v1/models lists. The same one
    filter the pipeline's pinned-model check and the router run
    (`app/policy/feasible.py`), asked without a request: no pin, nothing
    excluded, and the app's own stored residency rather than a per-request
    escalation of it. A request whose content escalates residency (e.g. an SSN
    under an onprem_only rule) can narrow this further, never widen it."""
    return list(feasible_set(settings, registry, app_policy,
                             tenant_id=tenant_id).eligible)


def build_context(settings: Settings, request_id: str, tenant_id: str,
                  entities: list[Entity], redaction_map_id: str | None,
                  app_policy: "AppPolicyView | None" = None,
                  app_rules: list | None = None,
                  registry: "ModelRegistry | None" = None,
                  escalate_residency: bool = True,
                  content_integrity: str | None = None,
                  ) -> ExecutionPolicyContext:
    """Step 3: the immutable ExecutionPolicyContext.

    `content_integrity` is the app's COMPILED
    `eff.protection.content_integrity` (design §2.2), threaded in by
    `app/policy/gate.py` from the same `EffectivePolicy` every other consumer
    reads. It used to be read straight off `app_policy.content_integrity`
    here — a second resolution path for one axis, agreeing with the compiler
    only by coincidence (same shape as the residency fail-open the Hypothesis
    harness found: `_get(...) or default` in one place, a raw column read in
    another). `None` means "no caller supplied one", and the compiler is then
    run here rather than the precedence being re-implemented — so this
    function has no way to answer differently from `compile_policy`.

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
    if content_integrity is None:
        content_integrity = compile_policy(
            app_policy, settings).protection.content_integrity

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
    # Resolve through the compiler's authority BEFORE `_stricter` (which would
    # otherwise KeyError on an unparseable value) and before the placement
    # filter. An unparseable residency denies to `onprem_only`, so it can never
    # be routed as `any` -- the same answer `compile_policy` gives for the same
    # stored value, by construction.
    app_residency, _parsed = resolve_residency(app_residency)
    # Raise-only, and only when the app declares the escalation on (default).
    residency = (_stricter(rule.residency, app_residency) if escalate_residency
                 else app_residency)
    onprem_only = residency == "onprem_only"
    allowed = _placement_providers(settings, residency, app_policy, registry)

    # The one eligibility filter (`app/policy/feasible.py`), given the placement
    # this function has just resolved -- `_placement_providers` cannot re-derive
    # it, because a detected entity may have TIGHTENED residency above the one
    # the stored policy shows. There is no pin and nothing excluded here: both
    # belong to a request, and the router applies them to this same filter.
    feasible = feasible_set(settings, registry, app_policy, tenant_id=tenant_id,
                            allowed_providers=frozenset(allowed),
                            onprem_only=onprem_only)
    # PLACEMENT and APP SCOPE only (the filter's stages 1 and 3) -- that is
    # what `allowed_models` has always meant and what every consumer reads it
    # as. Every other stage's rejections stay OUT of the subtraction, so each
    # keeps its own, more specific operator-facing reason instead of being
    # relabelled "not permitted for application" when the router re-applies
    # this set: "disabled by admin" for a disabled row, a deliberately vague
    # "unknown model" for one another application owns, and (from Task 4) the
    # data-handling exclusions. A new stage added to the seam therefore needs
    # no change here.
    allowed_models = frozenset(
        [c.name for c in feasible.eligible]
        + [r.name for r in feasible.rejected
           if r.code not in (RESIDENCY, APP_SCOPE)])
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
        # The COMPILED axis, never the raw nullable column: one authority
        # (`compile_policy`), threaded in by the gate or resolved above when a
        # caller supplies none. A DATA axis only -- computed independently of
        # `residency`/`rule` above, never folded into the placement decision.
        content_integrity=content_integrity,
        app_policy_version=(app_policy.version() if app_policy is not None else ""),
    )
