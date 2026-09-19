"""The one place that decides which pool candidates a request may reach.

Eligibility used to be decided twice: `app/policy/context.py::build_context`
computed `ctx.allowed_models` from the pool, and `app/routing/router.py::select`
re-derived the same decision from the pool on every call, consulting
`ctx.allowed_models` only when `ctx.model_scope_active` was true -- which is
false for every app that sets no `allowed_tiers`/`allowed_models`. A rule
written into one authority was therefore silently absent from the other, for
exactly the apps most likely to depend on it. This module is that single
authority; `build_context`, `requestable_models`, `pinned_model_reason` and
`Router.select` all read their answer from here and none re-derives it.

The filter is a fixed sequence of stages. Each stage owns one `code` (stable,
for the trace and for anything that needs to branch on *which* rule fired) and
produces the operator-facing `reason` string the console already renders:

| # | stage                                   | code            | reason |
|---|-----------------------------------------|-----------------|--------|
| 1 | residency / provider placement          | `residency`     | "excluded · residency" under onprem_only, else "not permitted for application" |
| 2 | prior failure on this request           | `prior_failure` | "excluded · prior failure" |
| 3 | the app's allowed_tiers / allowed_models| `app_scope`     | "not permitted for application" |
| 4 | the request's pinned `model`            | `not_requested` | "not requested model" |
| 5 | the registry's per-model gates          | `model_scope`   | "not permitted for application" / "disabled by admin" |

The order is load-bearing, not stylistic: placement runs first so an excluded
cloud model under `onprem_only` always reads "residency" rather than a softer
reason further down (CLAUDE.md rule 3), and `exclude` sits behind it because a
failure-driven exclusion may only ever remove a candidate, never relabel a
placement decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.config.schema import PoolCandidate, Settings
from app.models import ExecutionPolicyContext
from app.policy.compile import resolve_residency

if TYPE_CHECKING:  # avoid importing the apps layer at module load
    from app.apps.policy import AppPolicyView
    from app.apps.registry import ModelRegistry

# Stage codes -- stable identifiers for the five filters above.
RESIDENCY = "residency"
PRIOR_FAILURE = "prior_failure"
APP_SCOPE = "app_scope"
NOT_REQUESTED = "not_requested"
MODEL_SCOPE = "model_scope"

# Operator-facing strings. The console renders these verbatim and
# `app/routing/router.py::_FIX_HINTS` keys on them -- they are an interface.
REASON_RESIDENCY = "excluded · residency"
REASON_APP = "not permitted for application"
REASON_PRIOR_FAILURE = "excluded · prior failure"
REASON_NOT_REQUESTED = "not requested model"
REASON_DISABLED = "disabled by admin"


@dataclass(frozen=True)
class Rejection:
    """One candidate the filter refused, and why."""
    name: str
    provider: str
    tier: str
    display_name: str | None
    reason: str   # operator-facing; rendered in the console and the trace
    code: str     # stable stage identifier (see the table above)


@dataclass(frozen=True)
class FeasibleSet:
    eligible: tuple[PoolCandidate, ...]
    rejected: tuple[Rejection, ...]


def registry_permits(registry: "ModelRegistry | None", c, tenant_id: str) -> bool:
    """Per-model registry gates on top of policy: an onboarded model restricted
    to other apps, or a disabled on-prem row, is never requestable."""
    if registry is None:
        return True
    return (registry.is_allowed_for_app(c.name, tenant_id)
            and (c.provider != "onprem" or registry.is_enabled(c.name)))


def _placement_providers(settings: Settings, residency: str,
                         app_policy: "AppPolicyView | None",
                         registry: "ModelRegistry | None") -> set[str]:
    # Fail closed on a residency this function does not recognise. The `else`
    # branch below is the loosest placement there is, so an unparseable value
    # used to hand the request every enabled provider, cloud included, while
    # `compile_policy` denied the very same value to `onprem_only` -- CLAUDE.md
    # rules 3 and 5, and the defect the Task 10 harness found. Resolved through
    # the compiler's own `resolve_residency` so the two cannot disagree. This
    # is the second line of defence behind `build_context`'s (which resolves
    # residency before escalating it); it is kept because `feasible_set` calls
    # in here directly for a policy-only caller.
    residency, _parsed = resolve_residency(residency)
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


def _policy_scope(app_policy: "AppPolicyView | None") -> Callable[[PoolCandidate], bool]:
    """Stage 3 for a caller that holds the app policy itself."""
    def ok(c: PoolCandidate) -> bool:
        if app_policy is None:
            return True
        if (app_policy.allowed_tiers is not None
                and c.tier not in app_policy.allowed_tiers):
            return False
        if (app_policy.allowed_models is not None
                and c.name not in app_policy.allowed_models):
            return False
        return True
    return ok


def _ctx_scope(ctx: ExecutionPolicyContext) -> Callable[[PoolCandidate], bool]:
    """Stage 3 for a caller that holds only the context.

    The context does not carry the app's `allowed_tiers`/`allowed_models`; it
    carries the set those produced -- `ctx.allowed_models`, which
    `build_context` computes from stages 1 and 3 of this same filter (and from
    nothing else, so that a name missing from it means *app scope refused it*
    and stage 5 keeps its own, more specific reasons). Honouring it is NOT
    gated on `ctx.model_scope_active`: that flag says whether the app narrowed
    anything (the console reads it), and using it as an enforcement switch is
    what let the router ignore a context-side rule for every app that set no
    allowlist.

    `eligibility_resolved` is the gate's own marker that `allowed_models` is
    that computed set. A context assembled directly (tests, internal callers)
    leaves it false and carries a placeholder `allowed_models`, so stage 3 is
    skipped for it and stages 1/2/4/5 still apply -- the same answer those
    callers get today.
    """
    def ok(c: PoolCandidate) -> bool:
        if not ctx.eligibility_resolved:
            return True
        return c.name in ctx.allowed_models
    return ok


def _rejection(c: PoolCandidate, reason: str, code: str) -> Rejection:
    return Rejection(name=c.name, provider=c.provider, tier=c.tier,
                     display_name=c.display_name, reason=reason, code=code)


def candidate_rejection(c: PoolCandidate, *, allowed_providers: frozenset[str] | set[str],
                        onprem_only: bool, in_scope: Callable[[PoolCandidate], bool],
                        requested_model: str | None,
                        exclude: frozenset[str],
                        registry: "ModelRegistry | None",
                        tenant_id: str) -> Rejection | None:
    """The five stages, in order, for one candidate. `None` means eligible."""
    if c.provider not in allowed_providers:
        return _rejection(c, REASON_RESIDENCY if onprem_only else REASON_APP,
                          RESIDENCY)
    # Failure-driven lateral switch: drop a model the gateway already saw fail
    # on this request. Placed AFTER the residency check so an excluded cloud
    # model under onprem_only still reads "residency", never "prior failure" --
    # excluding a name only ever removes a candidate, so it can never weaken
    # residency.
    if c.name in exclude:
        return _rejection(c, REASON_PRIOR_FAILURE, PRIOR_FAILURE)
    if not in_scope(c):
        return _rejection(c, REASON_APP, APP_SCOPE)
    if requested_model is not None and c.name != requested_model:
        return _rejection(c, REASON_NOT_REQUESTED, NOT_REQUESTED)
    if registry is not None and not registry.is_allowed_for_app(c.name, tenant_id):
        return _rejection(c, REASON_APP, MODEL_SCOPE)
    if (c.provider == "onprem" and registry is not None
            and not registry.is_enabled(c.name)):
        return _rejection(c, REASON_DISABLED, MODEL_SCOPE)
    return None


def feasible_set(settings: Settings, registry: "ModelRegistry | None",
                 ctx_or_policy: "ExecutionPolicyContext | AppPolicyView | None",
                 *, tenant_id: str,
                 exclude: frozenset[str] = frozenset(),
                 allowed_providers: frozenset[str] | None = None,
                 onprem_only: bool | None = None) -> FeasibleSet:
    """Every candidate this request may reach, and every one it may not.

    `ctx_or_policy` is whichever authority the caller holds: an
    `ExecutionPolicyContext` (the router, after the gate) or an `AppPolicyView`
    (the gate itself, and `GET /v1/models`). `None` means no per-app policy at
    all -- the internal/direct pipeline paths.

    `allowed_providers`/`onprem_only` let `build_context` pass the placement it
    has already resolved (residency may have been *tightened* by a detected
    entity, which the stored policy alone does not show). Omitted, placement is
    resolved from the policy's own residency.

    Onboarded on-prem models never appear in the static YAML
    `settings.routing.model_pool` -- read the registry's merged pool when
    available, or every onboarded model would be silently excluded regardless
    of the app's own policy.
    """
    pool = registry.effective_pool() if registry is not None else settings.routing.model_pool

    if isinstance(ctx_or_policy, ExecutionPolicyContext):
        ctx = ctx_or_policy
        providers = (allowed_providers if allowed_providers is not None
                     else ctx.allowed_providers)
        onprem = ctx.onprem_only if onprem_only is None else onprem_only
        requested_model = ctx.requested_model
        in_scope = _ctx_scope(ctx)
    else:
        policy: "AppPolicyView | None" = ctx_or_policy
        if allowed_providers is not None:
            providers = allowed_providers
        else:
            providers = frozenset(_placement_providers(
                settings, policy.residency if policy is not None else "any",
                policy, registry))
        if onprem_only is None:
            residency, _parsed = resolve_residency(
                policy.residency if policy is not None else "any")
            onprem = residency == "onprem_only"
        else:
            onprem = onprem_only
        # A policy-side caller never pins a model: the pin lives on the request,
        # and reaches this function only through a context.
        requested_model = None
        in_scope = _policy_scope(policy)

    eligible: list[PoolCandidate] = []
    rejected: list[Rejection] = []
    for c in pool:
        r = candidate_rejection(
            c, allowed_providers=providers, onprem_only=onprem,
            in_scope=in_scope, requested_model=requested_model,
            exclude=exclude, registry=registry, tenant_id=tenant_id)
        if r is None:
            eligible.append(c)
        else:
            rejected.append(r)
    return FeasibleSet(eligible=tuple(eligible), rejected=tuple(rejected))
