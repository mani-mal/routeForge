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
| 2 | the endpoint's data-handling declarations| `region_not_allowed` / `prompt_retention_not_allowed` / `raw_sensitive_not_accepted` | "excluded · region …" / "excluded · prompt retention …" / "excluded · raw sensitive content …" |
| 3 | prior failure on this request           | `prior_failure` | "excluded · prior failure" |
| 4 | the app's allowed_tiers / allowed_models| `app_scope`     | "not permitted for application" |
| 5 | the request's pinned `model`            | `not_requested` | "not requested model" |
| 6 | the registry's per-model gates          | `model_scope`   | "not permitted for application" / "disabled by admin" |
| 7 | an open circuit (step-7 breaker)        | `circuit_open`  | "excluded · circuit open" |

Stage 7 runs LAST, after every policy stage above it, so that every policy
reason wins over it and `circuit_open` only ever labels a candidate that is
otherwise fully policy-eligible. That is what makes the router's use of it
safe (`app/routing/router.py::select`): the router chooses the target tier
from the set WITHOUT stage 7 (the breaker never changes the tier), reads this
stage only to label which of that tier's final candidates are open, and
bypasses it within the tier when all of them are -- which can only restore
policy-eligible candidates, never widen placement.

The order is load-bearing, not stylistic: placement runs first so an excluded
cloud model under `onprem_only` always reads "residency" rather than a softer
reason further down (CLAUDE.md rule 3), and `exclude` sits behind it because a
failure-driven exclusion may only ever remove a candidate, never relabel a
placement decision. Stage 2 sits immediately behind placement for the same
reason `exclude` sits behind placement: a data-handling exclusion is a
contractual refusal of the endpoint, so it must not be relabelled by a
transient prior failure or by a softer app-scope reason -- but it must never
outrank residency, which is absolute.

**`onprem_only` holds at this seam itself (B52, 2026-09-23).** Stage 1 does
not trust the caller's `allowed_providers` to be consistent with
`onprem_only`: whenever the resolved `onprem_only` is true (the context's, the
explicit argument, or the policy's own residency), `feasible_set` intersects
the providers with `{onprem}` before stage 1 runs. A cloud candidate is then
rejected as `residency` even from a hand-built context that still lists a
cloud provider -- rule 3's "unreachable by construction" no longer rests on
`build_context` alone. For every context the real gate builds this is a
no-op, because `build_context` has already narrowed to `{onprem}`.

**Stage 2 -- the three data-handling rules (P1b design §4).** Each compares one
requirement against one ENDPOINT DECLARATION:

| rule          | fires when                                                          | unknown endpoint |
|---------------|---------------------------------------------------------------------|------------------|
| region        | the app set `allowed_regions` and the endpoint's region is not in it | excluded |
| retention     | the app set `require_no_retention` and retention is not `none`       | excluded |
| raw sensitive | the request sends raw sensitive content and the endpoint declares `accepts_raw_sensitive: false` | KEPT |

**Why that last column is not uniform.** It looks like an inconsistency and is
the opposite -- it is what keeps the two halves of "unknown never satisfies a
requirement, and never blocks a request that imposes none" from contradicting
each other:

* region and retention exclude an `unknown` endpoint because **the app
  declared a requirement**. An endpoint that cannot prove it does not qualify;
  that is the whole content of the requirement.
* the raw-sensitive rule has **no app-declared requirement behind it** -- it
  fires from the request's own shape (a `raw` model_input over a
  better-than-public sensitivity, or a `raw` model_input with detection off,
  where nothing classified the content at all). Excluding `unknown` there would retire
  every endpoint in every existing deployment by a rule nobody asked for, and
  break design §12's promise that an app which sets neither requirement routes
  byte-identically. So an undeclared endpoint keeps today's behaviour, and
  only an operator's explicit `false` is enforced.

  The healthcare case in the priorities doc still works exactly as written:
  "Azure US -- permitted after redaction" is a **declared `false`**, so it is
  excluded for a raw+sensitive request and eligible again the moment
  `model_input` is `redact`.

Two properties hold across all three, and both are load-bearing:

* **`unknown` is read AT THE POINT OF USE** (`_declared` below), never assumed
  to have been resolved upstream: the no-registry paths (`app/pipeline.py`,
  `app/policy/context.py`, `app/routing/quality.py`,
  `app/gateway/backends/litellm_backend.py`, `app/api/routes.py`) fall back to
  `settings.routing.model_pool` directly, whose candidates carry `None` in
  these fields because `ModelRegistry.effective_pool()` -- the one resolver --
  never ran. `None`, `""` and a whitespace token all read as `unknown` here.
  A rule that silently no-ops on the internal path would be a policy that
  appears enforced and is not.
* **They may only narrow.** No value of any of the three can add a candidate
  back, reach a provider residency excluded, or touch `allowed_providers` /
  `onprem_only`. They run after stage 1 and never feed into it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.config.schema import PoolCandidate, Settings
from app.models import ExecutionPolicyContext
from app.policy.compile import compile_policy, resolve_residency

if TYPE_CHECKING:  # avoid importing the apps layer at module load
    from app.apps.policy import AppPolicyView
    from app.apps.registry import ModelRegistry

# Stage codes -- stable identifiers for the seven stages in the table above
# (stage 2 has one code per data-handling rule, below).
RESIDENCY = "residency"
PRIOR_FAILURE = "prior_failure"
APP_SCOPE = "app_scope"
NOT_REQUESTED = "not_requested"
MODEL_SCOPE = "model_scope"
# Stage 7 (LAST) -- see `candidate_rejection`'s docstring for why it runs
# after every policy stage.
CIRCUIT_OPEN = "circuit_open"
# Stage 2 -- one code per data-handling rule (P1b design §4/§6). These join
# `sp.candidates`' rejected set, so the certificate answers "why not that
# endpoint" in the compliance officer's vocabulary rather than the router's.
REGION_NOT_ALLOWED = "region_not_allowed"
PROMPT_RETENTION_NOT_ALLOWED = "prompt_retention_not_allowed"
RAW_SENSITIVE_NOT_ACCEPTED = "raw_sensitive_not_accepted"

# The only provider `onprem_only` admits. `feasible_set` intersects placement
# with this whenever the resolved onprem_only is true (B52).
ONPREM_ONLY_PROVIDERS = frozenset({"onprem"})

# Operator-facing strings. The console renders these verbatim and
# `app/routing/router.py::_FIX_HINTS` keys on them -- they are an interface.
REASON_RESIDENCY = "excluded · residency"
REASON_APP = "not permitted for application"
REASON_PRIOR_FAILURE = "excluded · prior failure"
REASON_NOT_REQUESTED = "not requested model"
REASON_DISABLED = "disabled by admin"
REASON_CIRCUIT_OPEN = "excluded · circuit open"
# Stage 2's reasons carry a parameterised tail (what was required, what the
# endpoint declared) -- an operator cannot act on "excluded · region" alone.
# `app/routing/router.py::_fix_hint` therefore matches these by PREFIX.
REASON_REGION_PREFIX = "excluded · region"
REASON_RETENTION_PREFIX = "excluded · prompt retention"
REASON_RAW_SENSITIVE_PREFIX = "excluded · raw sensitive content"

#: code -> (reason prefix, operator fix hint). ONE source for both consumers:
#: `app/routing/router.py` matches the prefix to explain a rejected candidate,
#: and `app/policy/context.py::pinned_model_reason` looks up the code to
#: explain a refused pin. They were separate string literals for one commit and
#: promptly disagreed -- the raw-sensitive pair told an operator to "declare
#: accepts_raw_sensitive" on an endpoint that already declares it, as `false`.
DATA_HANDLING_HINTS: dict[str, tuple[str, str]] = {
    REGION_NOT_ALLOWED: (
        REASON_REGION_PREFIX,
        "declare the endpoint's region under Infrastructure, or widen the "
        "application's allowed_regions"),
    PROMPT_RETENTION_NOT_ALLOWED: (
        REASON_RETENTION_PREFIX,
        "declare the endpoint's prompt_retention under Infrastructure, or "
        "clear the application's require_no_retention"),
    RAW_SENSITIVE_NOT_ACCEPTED: (
        REASON_RAW_SENSITIVE_PREFIX,
        "this endpoint is declared accepts_raw_sensitive: false; set it true "
        "if it is trusted with unredacted content, or set the application's "
        "model_input to 'redact'"),
}

#: What an endpoint that declared nothing reads as, on every one of the three
#: attributes. It never SATISFIES an app-declared requirement (region,
#: retention), but it does not by itself trigger the request-shaped
#: raw-sensitive rule -- see the stage-2 section of the module docstring.
UNKNOWN = "unknown"

# --- P3b: requirement ids and state ids (spec §2.1 / §2.2) -------------------
#
# `candidate_rejection` answers with a stage CODE (the first stage that
# refused a candidate). `candidate_violations` answers with REQUIREMENT ids --
# every requirement the candidate violates -- which is what the conflicting
# core (`app/policy/core.py`) is computed over. The two budget ids
# (`max_cost_usd_per_request`, `risk_floor`) are the router's, not this
# seam's, and are never produced here.
REQ_RESIDENCY = "residency"
REQ_ALLOWED_PROVIDERS = "allowed_providers"
REQ_ALLOWED_REGIONS = "allowed_regions"
REQ_REQUIRE_NO_RETENTION = "require_no_retention"
REQ_RAW_SENSITIVE = "raw_sensitive"
REQ_ALLOWED_TIERS = "allowed_tiers"
REQ_ALLOWED_MODELS = "allowed_models"
REQ_ENDPOINT_APP_ALLOWLIST = "endpoint_app_allowlist"
REQ_REQUESTED_MODEL = "requested_model"

# Facts about an endpoint or this request's history -- never policy, so never
# a requirement and never in a core. `candidate_state` reports the first three;
# `no_endpoints` is a property of the whole (empty) pool.
STATE_NO_ENDPOINTS = "no_endpoints"
STATE_DISABLED = "disabled"
STATE_CIRCUIT_OPEN = "circuit_open"
STATE_PRIOR_FAILURE = "prior_failure"


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


@dataclass(frozen=True)
class DataHandling:
    """What this request REQUIRES of an endpoint's data handling (design §3/§4).

    Three values, and only three, because the seam must be able to answer the
    rules from whichever authority the caller holds -- an
    `ExecutionPolicyContext` after the gate, or an `AppPolicyView` before it.

    `allowed_regions` / `require_no_retention` are the app's own compiled
    requirements (`app/policy/compile.py`, never the raw nullable column: the
    compiler is what lowercases the tokens and DENIES an unparseable value to
    `frozenset()`).

    `sends_raw_sensitive` is the request-shaped half of the third rule, reduced
    to ONE boolean on purpose. "Would send raw sensitive content" is the
    compiled `model_input` being `raw` AND either the gate having classified
    the request above `public` or the app having turned `detection` off (an app
    that classifies nothing cannot prove its content is public -- see
    `app/policy/context.py`); computing it once (in `app/policy/context.py`,
    which holds both) and carrying the answer means the rule's condition exists
    in exactly one expression. A second copy in the router or the gateway is
    precisely what this module exists to prevent.

    The default is the no-requirement value of every field, so a caller with no
    per-app policy at all imposes nothing -- and `unknown` blocks nothing.
    """

    allowed_regions: frozenset[str] | None = None
    require_no_retention: bool = False
    sends_raw_sensitive: bool = False

    @classmethod
    def from_context(cls, ctx: ExecutionPolicyContext) -> "DataHandling":
        return cls(allowed_regions=ctx.allowed_regions,
                   require_no_retention=ctx.require_no_retention,
                   sends_raw_sensitive=ctx.sends_raw_sensitive)

    @classmethod
    def from_policy(cls, settings: Settings,
                    app_policy: "AppPolicyView | None") -> "DataHandling":
        """The app's requirements with no request in hand -- what
        `GET /v1/models` and the gate's own pre-request filter ask. There is no
        request to be sensitive, so `sends_raw_sensitive` is false: a pin that
        later arrives WITH sensitive content is narrowed again by the context
        path, never widened by this one.
        """
        eff = compile_policy(app_policy, settings)
        return cls(allowed_regions=eff.allowed_regions,
                   require_no_retention=eff.require_no_retention)


@dataclass(frozen=True)
class Scope:
    """The app-scope stage's requirements (stage 4 in the table above): which
    tiers and which models the application admits. `None` means no
    constraint. Read by `candidate_violations` to name EXACTLY which scope
    requirement a candidate violates -- `allowed_tiers`, `allowed_models`, or
    both.

    Both constructors are exact: they carry the app's own two lists.
    `Scope.from_policy` reads them off the `AppPolicyView`;
    `Scope.from_context` reads `ctx.app_allowed_tiers` /
    `ctx.app_allowed_models`, which `build_context` copies from that same view
    (`None` when there is no app policy).

    **It is NOT built from `ctx.allowed_models`.** That set stays the
    first-fail authority -- `_ctx_scope` enforces it, unchanged -- but it is
    built from first-fail CODES (`build_context` subtracts only the
    placement- and app-scope-coded rejections), so it cannot answer "does app
    scope refuse this candidate?" for a candidate an earlier stage refused
    first: a placement-refused name is absent from it whatever the scope
    says, and a stage-2-refused name is present in it whatever the scope
    says. Reading violations off it therefore both invents and misses
    `allowed_models` violations, and a core built on those is not the true
    minimum (spec §2.4).
    """

    allowed_tiers: frozenset[str] | None = None
    allowed_models: frozenset[str] | None = None

    @classmethod
    def from_policy(cls, app_policy: "AppPolicyView | None") -> "Scope":
        if app_policy is None:
            return cls()
        return cls(allowed_tiers=app_policy.allowed_tiers,
                   allowed_models=app_policy.allowed_models)

    @classmethod
    def from_context(cls, ctx: ExecutionPolicyContext) -> "Scope":
        return cls(allowed_tiers=ctx.app_allowed_tiers,
                   allowed_models=ctx.app_allowed_models)


def tier_in_scope(c: PoolCandidate, scope: Scope) -> bool:
    """The app admits this candidate's tier (or constrains no tier)."""
    return scope.allowed_tiers is None or c.tier in scope.allowed_tiers


def model_in_scope(c: PoolCandidate, scope: Scope) -> bool:
    """The app admits this candidate by name (or constrains no model)."""
    return scope.allowed_models is None or c.name in scope.allowed_models


def _declared(c, field: str) -> str:
    """One endpoint declaration, read at the POINT OF USE.

    `ModelRegistry.effective_pool()` resolves these three to a never-None
    token, but not every path holds a registry: the internal/direct pipeline
    paths fall back to `settings.routing.model_pool`, whose `PoolCandidate`s
    carry the raw, unresolved declaration (`None` when the YAML entry declares
    nothing) and an empty `data_handling_source`. Reading `None`/`""` as
    `unknown` here is what stops the rules silently no-opping on that path.

    **It does not make all three enforce identically on both paths**, and the
    gap is worth naming rather than implying away. Only `effective_pool()`
    applies the PROVIDER-level default from `serving.yaml`; a raw candidate
    carries the per-model value alone. For region and retention that is
    harmless in the strict direction -- a missing provider default reads
    `unknown`, which excludes -- but the raw-sensitive rule fires only on a
    declared `false` (see `_data_handling_rejection`), so a provider-level
    `accepts_raw_sensitive: false` would be invisible to a `registry=None`
    caller and the endpoint would be KEPT. That is unreachable today:
    `app/main.py` always builds a registry, so every request-path caller has
    one. It would start to matter if a serving path were ever wired without a
    registry, or if a deployment moved to declaring `accepts_raw_sensitive`
    only at provider level; the fix then is to give that path the registry,
    not to re-derive the provider default here.
    """
    v = getattr(c, field, None)
    if isinstance(v, bool):        # a raw onboarded row's accepts_raw_sensitive
        return "true" if v else "false"
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    return UNKNOWN


def _region_rejection(c: PoolCandidate, dh: DataHandling) -> str | None:
    """Stage 2, region rule: the reason if it fires, else `None`. `unknown`
    is excluded -- the APP declared a requirement (see the module docstring)."""
    if dh.allowed_regions is None:
        return None
    region = _declared(c, "region")
    if region in dh.allowed_regions:
        return None
    # Both sides quoted, so the mismatch is visible in the console without
    # anyone opening the application's policy to look up what it allows.
    allows = ", ".join(repr(r) for r in sorted(dh.allowed_regions))
    return (f"{REASON_REGION_PREFIX} · endpoint declares {region!r}, "
            f"application allows {allows or '(nothing)'}")


def _retention_rejection(c: PoolCandidate, dh: DataHandling) -> str | None:
    """Stage 2, retention rule: the reason if it fires, else `None`. `unknown`
    is excluded -- the APP declared a requirement."""
    if not dh.require_no_retention:
        return None
    retention = _declared(c, "prompt_retention")
    if retention == "none":
        return None
    return (f"{REASON_RETENTION_PREFIX} · endpoint declares "
            f"{retention!r}, application requires 'none'")


def _raw_sensitive_rejection(c: PoolCandidate, dh: DataHandling) -> str | None:
    """Stage 2, raw-sensitive rule: the reason if it fires, else `None`.

    `== "false"`, NOT `!= "true"`. An operator who declared an endpoint
    unwilling to receive unredacted content is refused; an endpoint that
    declared nothing is left exactly where it was -- no app declared this
    requirement, it comes from the request's own shape (module docstring).
    """
    if not dh.sends_raw_sensitive:
        return None
    raw = _declared(c, "accepts_raw_sensitive")
    if raw != "false":
        return None
    return (f"{REASON_RAW_SENSITIVE_PREFIX} · endpoint declares "
            f"accepts_raw_sensitive={raw!r}, and this request sends "
            "unredacted sensitive content")


#: Stage 2's three rules, IN ORDER (region, retention, raw-sensitive -- stable
#: so an endpoint failing two rules always reads the same first one), each with
#: its first-fail `code` and its requirement id (spec §2.1). Both
#: `_data_handling_rejection` and `_data_handling_violations` walk this one
#: table, so the two cannot disagree about what a rule is.
_DATA_HANDLING_RULES: tuple[tuple[Callable[[PoolCandidate, DataHandling], str | None],
                                  str, str], ...] = (
    (_region_rejection, REGION_NOT_ALLOWED, REQ_ALLOWED_REGIONS),
    (_retention_rejection, PROMPT_RETENTION_NOT_ALLOWED, REQ_REQUIRE_NO_RETENTION),
    (_raw_sensitive_rejection, RAW_SENSITIVE_NOT_ACCEPTED, REQ_RAW_SENSITIVE),
)


def _data_handling_rejection(c: PoolCandidate,
                             dh: DataHandling) -> tuple[str, str] | None:
    """Stage 2: `(reason, code)` for the first data-handling rule this
    candidate fails, or `None`. Order is region, retention, raw-sensitive --
    stable so an endpoint failing two rules always reads the same one.

    **The two `unknown` readings are different on purpose** (see the module
    docstring's stage-2 table for the full statement):

    * region and retention exclude on `unknown`, because the APP declared a
      requirement and an endpoint that cannot prove it does not qualify;
    * raw-sensitive excludes only on a declared `false`, because no app
      declared anything -- the requirement comes from the request's own shape
      -- so an undeclared endpoint keeps today's behaviour instead of being
      retired by a rule nobody asked for.
    """
    for rule, code, _req in _DATA_HANDLING_RULES:
        reason = rule(c, dh)
        if reason is not None:
            return (reason, code)
    return None


def _data_handling_violations(c: PoolCandidate, dh: DataHandling) -> frozenset[str]:
    """Stage 2, every rule at once: the requirement id of each of the three
    data-handling rules this candidate fails (not just the first)."""
    return frozenset(req for rule, _code, req in _DATA_HANDLING_RULES
                     if rule(c, dh) is not None)


def endpoint_allows_app(c: PoolCandidate, registry: "ModelRegistry | None",
                        tenant_id: str) -> bool:
    """The onboarded endpoint's `allowed_app_ids` admits this app (or there is
    no registry, or the row sets no allowlist)."""
    return registry is None or registry.is_allowed_for_app(c.name, tenant_id)


def is_disabled(c: PoolCandidate, registry: "ModelRegistry | None") -> bool:
    """An on-prem row the admin has disabled. STATE, not policy (spec §2.2)."""
    return (c.provider == "onprem" and registry is not None
            and not registry.is_enabled(c.name))


def registry_permits(registry: "ModelRegistry | None", c, tenant_id: str) -> bool:
    """Per-model registry gates on top of policy: an onboarded model restricted
    to other apps, or a disabled on-prem row, is never requestable."""
    return endpoint_allows_app(c, registry, tenant_id) and not is_disabled(c, registry)


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
    scope = Scope.from_policy(app_policy)

    def ok(c: PoolCandidate) -> bool:
        return tier_in_scope(c, scope) and model_in_scope(c, scope)
    return ok


def _ctx_scope(ctx: ExecutionPolicyContext) -> Callable[[PoolCandidate], bool]:
    """Stage 3 for a caller that holds only the context.

    The context does not carry the app's `allowed_tiers`/`allowed_models`; it
    carries the set those produced -- `ctx.allowed_models`, which
    `build_context` computes from stages 1 and 3 of this same filter (and from
    nothing else, so that a name missing from it means *app scope refused it*
    and stage 5 keeps its own, more specific reasons). `Scope.from_context`
    documents how that maps onto the tiers/models split.

    It is honoured unconditionally. Not gated on `ctx.model_scope_active` --
    that flag says whether the app narrowed anything (the console reads it),
    and using it as an enforcement switch is what let the router ignore a
    context-side rule for every app that set no allowlist -- and not gated on a
    "was this computed?" marker either: a flag whose default turns a privacy
    control off is the same defect wearing a different hat. Every context
    carries a binding `allowed_models`, and one that carries an empty set has
    no eligible candidate and is rejected (fail closed), never routed.
    """
    # First-fail enforcement reads the PRECOMPUTED set, never the raw lists
    # `Scope.from_context` carries -- see `Scope` for why the two differ.
    scope = Scope(allowed_tiers=None, allowed_models=ctx.allowed_models)

    def ok(c: PoolCandidate) -> bool:
        return tier_in_scope(c, scope) and model_in_scope(c, scope)
    return ok


def _resolve_placement(settings: Settings, registry: "ModelRegistry | None",
                       ctx_or_policy: "ExecutionPolicyContext | AppPolicyView | None",
                       *, allowed_providers: frozenset[str] | None = None,
                       onprem_only: bool | None = None) -> tuple[frozenset[str], bool]:
    """Stage 1's input: `(providers, onprem_only)`, resolved exactly once.

    From a context, the context's own placement; from a policy (or `None`),
    the policy's stored residency through `_placement_providers`. Explicit
    `allowed_providers` / `onprem_only` override either (`build_context`
    passes placement it has already TIGHTENED on a detected entity).

    B52: `onprem_only` binds HERE, not only in `build_context`. Whatever the
    authority's `allowed_providers` says, a resolved onprem_only admits the
    on-prem provider and nothing else, so an inconsistent context (onprem_only
    set, a cloud provider still listed) rejects every cloud candidate as
    `residency` instead of routing to it (CLAUDE.md rule 3). A no-op for every
    context the real gate builds -- it has already narrowed to {onprem}.
    `feasible_set` and `violation_matrix` both resolve placement through this
    one function, so the first-fail seam and the violation matrix can never
    disagree about which providers are placeable.
    """
    if isinstance(ctx_or_policy, ExecutionPolicyContext):
        ctx = ctx_or_policy
        providers = (allowed_providers if allowed_providers is not None
                     else ctx.allowed_providers)
        onprem = ctx.onprem_only if onprem_only is None else onprem_only
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
    providers = frozenset(providers)
    if onprem:
        providers = providers & ONPREM_ONLY_PROVIDERS
    return providers, onprem


def _rejection(c: PoolCandidate, reason: str, code: str) -> Rejection:
    return Rejection(name=c.name, provider=c.provider, tier=c.tier,
                     display_name=c.display_name, reason=reason, code=code)


def provider_placeable(c: PoolCandidate,
                       allowed_providers: frozenset[str] | set[str]) -> bool:
    """Stage 1: the candidate's provider is in the resolved placement set."""
    return c.provider in allowed_providers


def not_requested(c: PoolCandidate, requested_model: str | None) -> bool:
    """The request pins a different model."""
    return requested_model is not None and c.name != requested_model


def candidate_rejection(c: PoolCandidate, *, allowed_providers: frozenset[str] | set[str],
                        onprem_only: bool, in_scope: Callable[[PoolCandidate], bool],
                        requested_model: str | None,
                        exclude: frozenset[str],
                        registry: "ModelRegistry | None",
                        tenant_id: str,
                        data_handling: DataHandling,
                        open_circuits: frozenset[str] = frozenset()) -> Rejection | None:
    """The seven stages, in order, for one candidate. `None` means eligible.

    `data_handling` is REQUIRED, with no default. A default would make stage 2
    silently absent for any caller that forgot it -- a policy that appears
    enforced and is not -- which is exactly the defect the `FeasibleSet` seam
    was extracted to end. `DataHandling()` is the explicit no-requirement
    value for a caller that genuinely has none.

    Every check below is a named predicate shared with `candidate_violations`
    / `candidate_state` -- this function owns only the ORDER and the
    first-fail code and reason.
    """
    if not provider_placeable(c, allowed_providers):
        return _rejection(c, REASON_RESIDENCY if onprem_only else REASON_APP,
                          RESIDENCY)
    # Stage 2: the endpoint's data-handling declarations against this request's
    # requirements. Behind residency (which is absolute -- rule 3) and ahead of
    # everything else, so a contractual refusal is never relabelled by a
    # transient prior failure or by the softer "not permitted for application".
    dh = _data_handling_rejection(c, data_handling)
    if dh is not None:
        return _rejection(c, dh[0], dh[1])
    # Failure-driven lateral switch: drop a model the gateway already saw fail
    # on this request. Placed AFTER the residency check so an excluded cloud
    # model under onprem_only still reads "residency", never "prior failure" --
    # excluding a name only ever removes a candidate, so it can never weaken
    # residency.
    if c.name in exclude:
        return _rejection(c, REASON_PRIOR_FAILURE, PRIOR_FAILURE)
    if not in_scope(c):
        return _rejection(c, REASON_APP, APP_SCOPE)
    if not_requested(c, requested_model):
        return _rejection(c, REASON_NOT_REQUESTED, NOT_REQUESTED)
    if not endpoint_allows_app(c, registry, tenant_id):
        return _rejection(c, REASON_APP, MODEL_SCOPE)
    if is_disabled(c, registry):
        return _rejection(c, REASON_DISABLED, MODEL_SCOPE)
    # Stage 7: a circuit the step-7 breaker holds open (app/gateway/breaker.py).
    # LAST, after every policy stage: it only ever labels a candidate policy
    # fully admitted, so applying it can only shrink the set, and the router's
    # within-tier bypass can only restore policy-eligible candidates.
    if c.name in open_circuits:
        return _rejection(c, REASON_CIRCUIT_OPEN, CIRCUIT_OPEN)
    return None


def candidate_violations(c: PoolCandidate, *, allowed_providers: frozenset[str] | set[str],
                         onprem_only: bool, scope: Scope, requested_model: str | None,
                         registry: "ModelRegistry | None", tenant_id: str,
                         data_handling: DataHandling) -> frozenset[str]:
    """EVERY requirement id (spec §2.1, minus the two budget ids) this
    candidate violates -- not just the first, as `candidate_rejection`
    reports. Never a state id: disabled / circuit_open / prior_failure are
    `candidate_state`'s.

    Same predicates as `candidate_rejection`, so the two agree by
    construction: for a candidate with no state, `candidate_rejection` is
    `None` exactly when this is empty (tests/test_policy_violations.py).

    Stage 1 splits into two ids. `residency` when the resolved residency is
    `onprem_only` and the provider is not on-prem; otherwise `allowed_providers`
    when the provider is outside the placement set (the app's
    `allowed_providers`, the enabled providers, or no healthy on-prem model).
    Callers pass placement resolved by `_resolve_placement` (B52-narrowed), so
    under `onprem_only` a cloud provider is never in `allowed_providers` and
    the two readings of stage 1 coincide.
    """
    v: set[str] = set()
    if onprem_only and c.provider not in ONPREM_ONLY_PROVIDERS:
        v.add(REQ_RESIDENCY)
    elif not provider_placeable(c, allowed_providers):
        v.add(REQ_ALLOWED_PROVIDERS)
    v |= _data_handling_violations(c, data_handling)
    if not tier_in_scope(c, scope):
        v.add(REQ_ALLOWED_TIERS)
    if not model_in_scope(c, scope):
        v.add(REQ_ALLOWED_MODELS)
    if not_requested(c, requested_model):
        v.add(REQ_REQUESTED_MODEL)
    if not endpoint_allows_app(c, registry, tenant_id):
        v.add(REQ_ENDPOINT_APP_ALLOWLIST)
    return frozenset(v)


def candidate_state(c: PoolCandidate, *, exclude: frozenset[str],
                    registry: "ModelRegistry | None",
                    open_circuits: frozenset[str]) -> str | None:
    """The first STATE (spec §2.2) that applies to this candidate --
    `disabled`, then `circuit_open`, then `prior_failure` -- or `None` when it
    is available. State is a fact about the endpoint or this request's
    history, never policy, so it never enters a conflicting core."""
    if is_disabled(c, registry):
        return STATE_DISABLED
    if c.name in open_circuits:
        return STATE_CIRCUIT_OPEN
    if c.name in exclude:
        return STATE_PRIOR_FAILURE
    return None


@dataclass(frozen=True)
class ViolationMatrix:
    """Every pool candidate, split into STATE (not available) and the
    requirement ids each AVAILABLE candidate violates -- the input to the
    conflicting core (`app/policy/core.py`)."""
    available: dict[str, frozenset[str]]   # name -> violated requirement ids (available only)
    state: dict[str, tuple[str, ...]]      # state id -> sorted model names
    candidates: dict[str, PoolCandidate]   # every pool candidate by name
    onprem_only: bool
    allowed_providers: frozenset[str]


def _pool(settings: Settings, registry: "ModelRegistry | None") -> list[PoolCandidate]:
    # Onboarded on-prem models never appear in the static YAML
    # `settings.routing.model_pool` -- read the registry's merged pool when
    # available, or every onboarded model would be silently excluded.
    return registry.effective_pool() if registry is not None else settings.routing.model_pool


def violation_matrix(settings: Settings, registry: "ModelRegistry | None",
                     ctx: ExecutionPolicyContext, *, tenant_id: str,
                     exclude: frozenset[str] = frozenset(),
                     open_circuits: frozenset[str] = frozenset()) -> ViolationMatrix:
    """The violation matrix for one request, from the same inputs
    `feasible_set` reads for a context caller: the same pool, placement
    (`_resolve_placement`, B52 included), `DataHandling.from_context`,
    `ctx.requested_model` and the context's scope.

    Onboarded rows the admin has DISABLED are not in the effective pool, so
    they are reported from `registry.disabled_onboarded()` (those this app may
    use) under `state["disabled"]` -- explanation only: they never enter
    `available`, `candidates`, or any eligibility path, and "re-enable it" is
    the actionable fact. `no_endpoints` (`state == {"no_endpoints": ()}`,
    `available == {}`) only when the pool is empty AND no such disabled row
    exists.
    """
    pool = _pool(settings, registry)
    providers, onprem = _resolve_placement(settings, registry, ctx)
    scope = Scope.from_context(ctx)
    data_handling = DataHandling.from_context(ctx)

    available: dict[str, frozenset[str]] = {}
    by_state: dict[str, list[str]] = {}
    for c in pool:
        s = candidate_state(c, exclude=exclude, registry=registry,
                            open_circuits=open_circuits)
        if s is not None:
            by_state.setdefault(s, []).append(c.name)
            continue
        available[c.name] = candidate_violations(
            c, allowed_providers=providers, onprem_only=onprem, scope=scope,
            requested_model=ctx.requested_model, registry=registry,
            tenant_id=tenant_id, data_handling=data_handling)
    if registry is not None:
        disabled_rows = [slug for slug in registry.disabled_onboarded()
                         if registry.is_allowed_for_app(slug, tenant_id)]
        if disabled_rows:
            by_state.setdefault(STATE_DISABLED, []).extend(disabled_rows)
    state = {s: tuple(sorted(set(names))) for s, names in by_state.items()}
    if not pool and not state:
        state = {STATE_NO_ENDPOINTS: ()}
    return ViolationMatrix(available=available, state=state,
                           candidates={c.name: c for c in pool},
                           onprem_only=onprem, allowed_providers=providers)


def feasible_set(settings: Settings, registry: "ModelRegistry | None",
                 ctx_or_policy: "ExecutionPolicyContext | AppPolicyView | None",
                 *, tenant_id: str,
                 exclude: frozenset[str] = frozenset(),
                 allowed_providers: frozenset[str] | None = None,
                 onprem_only: bool | None = None,
                 data_handling: DataHandling | None = None,
                 open_circuits: frozenset[str] = frozenset()) -> FeasibleSet:
    """Every candidate this request may reach, and every one it may not.

    `ctx_or_policy` is whichever authority the caller holds: an
    `ExecutionPolicyContext` (the router, after the gate) or an `AppPolicyView`
    (the gate itself, and `GET /v1/models`). `None` means no per-app policy at
    all -- the internal/direct pipeline paths.

    `open_circuits` is stage 7 -- see the module docstring's table and
    `candidate_rejection`. Names the step-7 breaker currently holds open;
    defaults to empty, so every caller that does not yet pass a breaker's
    `open_set()` (every caller before Task 4) behaves exactly as before.

    `allowed_providers`/`onprem_only` let `build_context` pass the placement it
    has already resolved (residency may have been *tightened* by a detected
    entity, which the stored policy alone does not show). Omitted, placement is
    resolved from the policy's own residency.

    `data_handling` is stage 2's input. Omitted, it is derived from the same
    authority the caller passed: off the context (which carries the compiled
    requirements and the one precomputed `sends_raw_sensitive` boolean), or
    from the compiler for a policy-side caller, which has no request and so
    sends nothing raw. It is never derived from the RAW `AppPolicyView`
    columns -- the compiler is what lowercases a region token and denies an
    unparseable `allowed_regions` to "matches nothing".

    Onboarded on-prem models never appear in the static YAML
    `settings.routing.model_pool` -- read the registry's merged pool when
    available, or every onboarded model would be silently excluded regardless
    of the app's own policy.
    """
    pool = _pool(settings, registry)
    # Placement (stage 1's input, B52 narrowing included) resolved in ONE
    # place, shared with `violation_matrix`.
    providers, onprem = _resolve_placement(
        settings, registry, ctx_or_policy,
        allowed_providers=allowed_providers, onprem_only=onprem_only)

    if isinstance(ctx_or_policy, ExecutionPolicyContext):
        ctx = ctx_or_policy
        requested_model = ctx.requested_model
        in_scope = _ctx_scope(ctx)
        if data_handling is None:
            data_handling = DataHandling.from_context(ctx)
    else:
        policy: "AppPolicyView | None" = ctx_or_policy
        # A policy-side caller never pins a model: the pin lives on the request,
        # and reaches this function only through a context.
        requested_model = None
        in_scope = _policy_scope(policy)
        if data_handling is None:
            data_handling = DataHandling.from_policy(settings, policy)

    eligible: list[PoolCandidate] = []
    rejected: list[Rejection] = []
    for c in pool:
        r = candidate_rejection(
            c, allowed_providers=providers, onprem_only=onprem,
            in_scope=in_scope, requested_model=requested_model,
            exclude=exclude, registry=registry, tenant_id=tenant_id,
            data_handling=data_handling, open_circuits=open_circuits)
        if r is None:
            eligible.append(c)
        else:
            rejected.append(r)
    return FeasibleSet(eligible=tuple(eligible), rejected=tuple(rejected))
