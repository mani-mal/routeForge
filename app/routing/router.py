from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.schema import PoolCandidate, Settings
from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext, RejectedCandidate
from app.policy.core import (
    REQ_MAX_COST_USD_PER_REQUEST,
    REQ_RISK_FLOOR,
    ConflictingCore,
    build_core,
)
from app.policy.feasible import (
    CIRCUIT_OPEN,
    DATA_HANDLING_HINTS,
    REQ_ALLOWED_MODELS,
    REQ_ALLOWED_PROVIDERS,
    REQ_ALLOWED_REGIONS,
    REQ_ALLOWED_TIERS,
    REQ_REQUESTED_MODEL,
    REQ_RESIDENCY,
    ViolationMatrix,
    _placement_providers,
    feasible_set,
    violation_matrix,
)
from app.routing.floor_select import (
    _TIER_ORDER as _ORDER,
    BELOW_QUALITY_FLOOR,
    CONTEXT_TOO_SMALL,
    OVER_BUDGET,
    FloorPick,
    qualify,
)
from app.routing.pricing import price_call
from app.routing.quality import QualityResolver
from app.routing.signals import SignalStore
from app.routing.strategy import RoutingStrategy, build_strategy

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry
    from app.routing.strategy import RankResult

log = logging.getLogger("serving.routing")



def _clamp_to_best_allowed(tier: str, allowed: list[PoolCandidate]) -> str:
    """`tier`, clamped DOWN to the best tier among `allowed` -- residency
    beats the risk floor (CLAUDE.md rule 8): a floor above every allowed tier
    lands on the best allowed one, never on a tier policy excluded."""
    best_allowed_order = max(_ORDER[c.tier] for c in allowed)
    if _ORDER[tier] > best_allowed_order:
        return next(t for t, o in _ORDER.items() if o == best_allowed_order)
    return tier


def complexity_tier(complexity: float, thresholds: dict) -> str:
    """The tier the length/complexity rule alone selects."""
    if complexity <= thresholds["cheap_max"]:
        return "cheap"
    if complexity <= thresholds["mid_max"]:
        return "mid"
    return "frontier"

# What an operator changes for each per-candidate rejection reason.
_FIX_HINTS = {
    "excluded · residency": "request or application is onprem_only: onboard and enable an on-prem model",
    "not permitted for application": "check the application's allowed_providers / allowed_tiers / allowed_models and the model's allowed_app_ids",
    "not requested model": "the request pinned a different model (see GET /v1/models)",
    "disabled by admin": "the model is disabled: re-enable it under Infrastructure",
    # Not an operator fix hint. Safe only because app/gateway/gateway.py rewraps
    # an exclude-driven NoEligibleCandidate into BackendUnavailable (502 with
    # the upstream error), so this text never reaches a client as a policy 422.
    "excluded · prior failure": "the model already failed on this request",
}

# P1b's data-handling reasons (app/policy/feasible.py stage 2) carry a
# parameterised tail naming the requirement and what the endpoint declared, so
# they cannot be exact keys above. Matched by PREFIX instead -- without this,
# `_explain` falls back to repeating the whole reason string as its own hint.
# The texts come from `DATA_HANDLING_HINTS`, shared with the pinned-model
# explainer in app/policy/context.py so the two cannot drift apart.
_FIX_HINT_PREFIXES = tuple(DATA_HANDLING_HINTS.values())


def _fix_hint(reason: str) -> str:
    hint = _FIX_HINTS.get(reason)
    if hint is not None:
        return hint
    for prefix, prefix_hint in _FIX_HINT_PREFIXES:
        if reason.startswith(prefix):
            return prefix_hint
    return reason


def _explain(rejected) -> str:
    counts = Counter(r.reason for r in rejected)
    return "; ".join(f"{n} {reason} ({_fix_hint(reason)})"
                     for reason, n in counts.most_common())


def _with_core(message: str, core: ConflictingCore | None) -> str:
    """The rejection message, unchanged, plus ` [<core summary>]` when a core
    was computed (P3b)."""
    return f"{message} [{core.summary()}]" if core is not None else message


def _policy_core_inputs(ctx: ExecutionPolicyContext, m: ViolationMatrix, *,
                        deployment_capacity: frozenset[str] | None = None,
                        ) -> tuple[dict[str, dict], dict[str, str]]:
    """`details` and `sources` for a policy-empty core. Content-free: policy
    values, model slugs and entity TYPES only -- never request text.
    `build_core` reads only the entries for ids that end up in the core.

    `deployment_capacity` is the placement the DEPLOYMENT alone would allow
    -- `app/policy/feasible.py::_placement_providers` computed with no app
    policy, so it reflects only `serving.providers.*.enabled` and on-prem
    health, never the app's own narrowing. When the resolved
    `allowed_providers` (`m.allowed_providers`) is empty AND that deployment
    capacity is ALSO empty, the app's own restriction (whatever it is)
    cannot be why nothing is left -- the deployment kill switch already
    emptied it before the app got a say -- so the requirement's source reads
    "deployment" instead of the default "app_policy" (final review finding
    1, 2026-09-25)."""
    residency: dict = {"value": "onprem_only"}
    sources: dict[str, str] = {}
    # `onprem_only` came from a sensitivity rule when the app's OWN residency
    # (pre-escalation, carried on the context by `build_context`) is not
    # `onprem_only` AND a rule actually raised it -- `escalation_entities`
    # names that rule's matched entity types (not every detected type). A
    # context without them (hand-built, or no rule fired) reads as the app's
    # -- never as a rule it cannot name.
    if (ctx.onprem_only and ctx.escalation_entities
            and ctx.app_residency != "onprem_only"):
        sources[REQ_RESIDENCY] = "sensitivity_rule"
        residency["entities"] = list(ctx.escalation_entities)   # types only
    if (not m.allowed_providers and deployment_capacity is not None
            and not deployment_capacity):
        sources[REQ_ALLOWED_PROVIDERS] = "deployment"
    details = {
        REQ_RESIDENCY: residency,
        REQ_ALLOWED_PROVIDERS: {"providers": sorted(m.allowed_providers)},
        REQ_ALLOWED_REGIONS: {"regions": sorted(ctx.allowed_regions or ())},
        REQ_ALLOWED_TIERS: {"tiers": sorted(ctx.app_allowed_tiers or ())},
        REQ_ALLOWED_MODELS: {"models": sorted(ctx.app_allowed_models or ())},
        REQ_REQUESTED_MODEL: {"value": ctx.requested_model},
    }
    return details, sources


def client_output_cap(params: dict | None) -> int | None:
    """The client's own output-token cap -- `max_completion_tokens`, else
    `max_tokens`, only when truthy -- or None when the client set neither.
    The one reading of the cap: `expected_output_tokens` prices at it and the
    outcome recorder (B55) stores it, so the two can never disagree."""
    p = params or {}
    for key in ("max_completion_tokens", "max_tokens"):
        if p.get(key):
            return int(p[key])
    return None


def expected_output_tokens(settings: Settings, params: dict | None) -> int:
    """Output tokens to price a request at: the client's own cap, else the
    deployment's `budgets.default_estimated_output_tokens`."""
    cap = client_output_cap(params)
    if cap is not None:
        return cap
    return int(settings.policy.budgets["default_estimated_output_tokens"])


@dataclass
class RoutingDecision:
    chosen: PoolCandidate
    est_cost_usd: float
    reason: str
    rejected: list[RejectedCandidate] = field(default_factory=list)
    quality_score: float = 0.0
    quality_source: str = "tier_default"
    scoring: "RankResult | None" = None
    circuit_bypass: bool = False
    # P2b-2: the FINAL target tier this decision was made for (post-clamp to
    # the best residency-allowed tier, pre-budget-fallback) -- the Y in
    # `reason`'s "tier X (target Y...)". None only if a future caller builds
    # a RoutingDecision without going through `_select` (no such caller
    # exists today).
    tier_target: str | None = None
    # B80: the quality-floor record (spec §4.4/§4.5) -- None when the request
    # had no floor or the mode is off. Content-free: names, numbers, enums.
    quality_floor: dict | None = None
    # B70/B71: set when endpoint state -- a prior failure on this request or
    # an open circuit -- moved the served tier BELOW the tier targeting
    # picked: `{"from": tier, "to": tier, "cause": "prior_failure" |
    # "circuit_open"}`. Only a complexity-raised (or, B108, advisor-raised)
    # tier is ever lowered this way, never below the hard floor (see
    # `Router.select`). None otherwise.
    # Content-free: tier names and a fixed cause enum.
    tier_downgrade: dict | None = None

    def scoring_record(self) -> dict | None:
        """`scoring.to_record()`, plus `quality_floor` and `tier_downgrade`
        beside it when set. The ranking part is unchanged, so it stays
        recomputable offline."""
        if self.scoring is None:
            return None
        record = self.scoring.to_record()
        if self.quality_floor is not None:
            record["quality_floor"] = self.quality_floor
        if self.tier_downgrade is not None:
            record["tier_downgrade"] = self.tier_downgrade
        return record


# B80: `RejectedCandidate.code` for the floor branch's own refusals. "over
# budget" and "below risk floor" stay uncoded, as on today's path.
_FLOOR_CODES = {BELOW_QUALITY_FLOOR: "quality_floor",
                CONTEXT_TOO_SMALL: "context_window"}
# The parenthetical in an unmet (enforce) decision's reason.
_UNMET_WORDS = {"unmet_no_qualifier": "no qualifying model",
                "unmet_context": "context window too small",
                "unmet_budget": OVER_BUDGET}
# B70/B71: `RoutingDecision.tier_downgrade["cause"]` and its reason words.
PRIOR_FAILURE_DOWNGRADE = "prior_failure"
CIRCUIT_OPEN_DOWNGRADE = "circuit_open"
_DOWNGRADE_WORDS = {PRIOR_FAILURE_DOWNGRADE: "prior failure",
                    CIRCUIT_OPEN_DOWNGRADE: "circuit open"}


class Router:
    def __init__(self, settings: Settings,
                 quality_resolver: QualityResolver | None = None,
                 registry: "ModelRegistry | None" = None,
                 strategy: RoutingStrategy | None = None,
                 signals: "SignalStore | None" = None) -> None:
        self._s = settings
        self._reg = registry
        # Registry-aware by default (onboarded on-prem models never appear in
        # the static YAML pool -- see QualityResolver's docstring) unless the
        # caller injects its own resolver (e.g. with DB-sourced overrides).
        self._quality = quality_resolver or QualityResolver(settings, registry=registry)
        # Step-5 scoring seam (app/routing/strategy.py). Injectable like
        # quality_resolver; config-selected otherwise (routing.strategy).
        self._strategy = strategy or build_strategy(settings.routing.strategy)
        # P2a measured signals (app/routing/signals.py). Injectable so callers
        # (app/main.py) can share ONE persistent, traffic-fed store across the
        # Router and the Gateway; defaults to a fresh, never-observed store so
        # every other construction site (tests, ad-hoc scripts) keeps working
        # unchanged -- reporting tier_default/neutral for everything, same as
        # pre-P2a.
        self._signals = signals or SignalStore(settings.routing.signals)

    def _complexity_tier(self, complexity: float) -> str:
        return complexity_tier(complexity, self._s.routing.complexity_thresholds)

    def _est_cost(self, cand: PoolCandidate, tokens_after: int, output_tokens: int) -> float:
        """Pre-call estimate. Deliberately assumes ZERO cached prompt tokens:
        the router cannot know the hit rate before the call, and guessing one
        biases routing toward whichever model the guess was generous about.
        Estimate high, record true (see the actual cost on the DecisionRecord)."""
        p = (self._reg.price_for(cand.name) if self._reg
             else self._s.pricing.models[cand.name])
        return price_call(p, prompt_tokens=tokens_after, completion_tokens=output_tokens)

    def select(self, ctx: ExecutionPolicyContext, tokens_after: int,
               complexity: float, min_tier: str | None = None,
               exclude: frozenset[str] = frozenset(),
               output_tokens: int | None = None,
               open_circuits: frozenset[str] = frozenset(),
               advisor_tier: str | None = None) -> RoutingDecision:
        """Choose a candidate for this call. `exclude` are models already tried
        and failed on this request (lateral failover); `open_circuits` are
        models the step-7 breaker currently holds open.

        **Endpoint state never lowers a request below its hard floor.** Tier
        targeting -- the complexity/risk-floor/escalation target, the HARD
        FLOOR `max(risk floor, min_tier)`, and the clamp of both to the best
        allowed tier (CLAUDE.md rule 8) -- is computed from the
        POLICY-eligible set: every candidate policy admits, with prior
        failures and open circuits still in it. Only policy (residency, app
        scope, declarations, registry gates) may clamp the floor down; a
        failure on this request or an open circuit is endpoint state and
        never does.

        Candidates are then drawn from what survives `exclude`. If the
        target tier has none, the lowest surviving tier above it serves
        first (upward failover -- escalation on tier exhaustion, as before;
        going up never undercuts a floor); only failing that does a target
        that only COMPLEXITY raised above the hard floor steps down, one tier
        at a time, to the highest surviving tier at or above the hard floor
        (`tier_downgrade`, cause `prior_failure`; `from` is the targeted
        tier, `to` the tier finally served, after any budget fallback or
        circuit step-down). If nothing survives at or
        above the hard floor, `NoEligibleCandidate(below_floor=...)` -- the
        gateway turns that into the exhausted 502.

        **The breaker may lower a complexity-raised tier, never the hard
        floor.** Open circuits are dropped only among the chosen tier's final
        (post-budget) candidates. If every one of those is open and the tier
        is above the hard floor, the highest lower tier at or above the hard
        floor with a closed, in-budget, non-excluded candidate serves
        (`tier_downgrade`, cause `circuit_open`). Otherwise -- the tier IS
        the hard floor, or no lower qualifying tier has a closed candidate --
        the router bypasses the breaker WITHIN the tier (`circuit_bypass`):
        trying an endpoint that may have recovered is never worse than the
        no-breaker behaviour. Neither path can restore a candidate policy did
        not admit.

        **`advisor_tier` (B108, Switchyard enforce) replaces exactly one
        input:** the length-derived `complexity_tier(complexity)`, the first
        element of the tier target's `max()` (design
        docs/architecture/switchyard-advisor-design.md §2). Nothing else reads
        it: the hard floor, the best-allowed clamp, upward failover, the
        budget fallback, the breaker and `rank()` are unchanged, so the
        advisor can neither go below the floor nor reach a candidate policy
        did not admit. The step-down above treats an advisor-raised target
        exactly like a complexity-raised one -- both are the SOFT element of
        the target, so either may step down to (never below) the hard floor.
        A value outside the canonical tiers is ignored and logged, never
        raised. Under a live B80 quality floor in `enforce` mode the floor
        path decides and the advisor is ignored. `None` (the default) is
        today's decision, byte for byte."""
        return self._select(ctx, tokens_after, complexity, min_tier, exclude,
                            output_tokens, open_circuits, advisor_tier)

    # --- P3b: the minimal conflicting core (app/policy/core.py) --------------
    #
    # The core EXPLAINS a rejection the router has already decided; it never
    # decides one. It is computed only at a raise site, after the routing
    # decision is final, and through `_safe_core`, so a failure here can only
    # cost the explanation (`core=None`, logged at ERROR), never change which
    # exception is raised or let a request through.

    @staticmethod
    def _safe_core(compute) -> ConflictingCore | None:
        try:
            return compute()
        except Exception:
            log.exception("conflicting core computation failed; raising "
                          "NoEligibleCandidate without a core")
            return None

    def _policy_core(self, ctx: ExecutionPolicyContext,
                     exclude: frozenset[str]) -> ConflictingCore:
        """The policy-empty core (spec §2.4): over every AVAILABLE pool
        candidate, with endpoint state reported separately. No open circuits:
        the router's policy set is computed without them (stage 7 only labels
        the chosen tier's final candidates), so the breaker never empties it
        and circuits are not state here. The empty-pool case is the same call;
        the matrix reports `no_endpoints`."""
        m = violation_matrix(self._s, self._reg, ctx, tenant_id=ctx.tenant_id,
                             exclude=exclude, open_circuits=frozenset())
        # Only a hand-built context can refuse an available candidate through
        # `ctx.allowed_models` while carrying no `app_allowed_*` to name why:
        # such a candidate violates nothing the matrix can express. Build the
        # core from the explainable candidates and say so -- never crash the
        # 422 over it.
        unexplained = sorted(n for n, v in m.available.items() if not v)
        if unexplained:
            log.warning("conflicting core: available candidates %s were refused "
                        "without a nameable requirement (context carries no "
                        "app_allowed_tiers/app_allowed_models); core built from "
                        "the rest", unexplained)
        violations = {n: v for n, v in m.available.items() if v}
        # The deployment's OWN placement capacity, ignoring the app's
        # narrowing entirely (`app_policy=None` short-circuits it) -- see
        # `_policy_core_inputs`'s docstring.
        deployment_capacity = frozenset(_placement_providers(
            self._s, "onprem_only" if ctx.onprem_only else "any", None, self._reg))
        details, sources = _policy_core_inputs(
            ctx, m, deployment_capacity=deployment_capacity)
        return build_core(violations, state=m.state, details=details, sources=sources)

    def _budget_core(self, ctx: ExecutionPolicyContext, allowed: list[PoolCandidate],
                     *, tier_pick: str, floor: str, tokens_after: int,
                     out_tokens: int, min_tier: str | None = None) -> ConflictingCore:
        """The budget-empty core (spec §2.3): over the policy-eligible
        candidates at or below the picked tier (the router's budget search
        space plus the lower tiers it refused). Each violates the cost cap
        when its estimate exceeds it, and the risk floor when its tier is below
        the EFFECTIVE floor -- the requested floor clamped down to the best
        allowed tier, the same clamp the router's target applies. No state:
        open circuits never block budget (the within-tier bypass handles them).

        `min_tier` is the caller's confidence-escalation floor (the same one
        `_select` folded into `floor`), passed through ONLY so this can name
        it when it -- not the request's own risk class -- is what set the
        effective floor (final review finding 2, 2026-09-25): source
        "escalation" instead of the default "risk_class", detail gains
        `min_tier` alongside `floor`.
        """
        best = max(_ORDER[c.tier] for c in allowed)
        effective_floor = (floor if _ORDER[floor] <= best
                           else next(t for t, o in _ORDER.items() if o == best))
        violations: dict[str, frozenset[str]] = {}
        over_estimates: list[float] = []   # estimates of the cost violators only
        for c in allowed:
            if _ORDER[c.tier] > _ORDER[tier_pick]:
                continue
            est = self._est_cost(c, tokens_after, out_tokens)
            v: set[str] = set()
            if est > ctx.max_cost_usd:
                v.add(REQ_MAX_COST_USD_PER_REQUEST)
                over_estimates.append(est)
            if _ORDER[c.tier] < _ORDER[effective_floor]:
                v.add(REQ_RISK_FLOOR)
            if not v:
                raise AssertionError(
                    f"budget core: candidate {c.name} (tier {c.tier}) violates neither "
                    f"max_cost_usd_per_request nor risk_floor (floor {effective_floor}, "
                    f"picked tier {tier_pick}), yet the budget stage found no "
                    "candidate -- the router's budget fallback and this core disagree")
            violations[c.name] = frozenset(v)
        # `cheapest_estimate` is over the candidates that VIOLATE the cap, so it
        # never reads below the cap beside "violates cost" (a below-floor
        # candidate that fits is refused by the floor, not the budget).
        details: dict[str, dict] = {
            REQ_RISK_FLOOR: {"floor": effective_floor, "risk_class": ctx.risk_class},
        }
        sources: dict[str, str] = {}
        risk_class_floor = self._s.routing.risk_floors[ctx.risk_class]
        if min_tier is not None and _ORDER[min_tier] > _ORDER[risk_class_floor]:
            sources[REQ_RISK_FLOOR] = "escalation"
            details[REQ_RISK_FLOOR]["min_tier"] = min_tier
        if over_estimates:
            details[REQ_MAX_COST_USD_PER_REQUEST] = {
                "cap": ctx.max_cost_usd, "cheapest_estimate": min(over_estimates)}
        return build_core(violations, state={}, details=details, sources=sources)

    def _select(self, ctx: ExecutionPolicyContext, tokens_after: int,
               complexity: float, min_tier: str | None,
               exclude: frozenset[str],
               output_tokens: int | None,
               open_circuits: frozenset[str],
               advisor_tier: str | None = None) -> RoutingDecision:
        out_tokens = (output_tokens if output_tokens is not None
                      else expected_output_tokens(self._s, None))
        # B108: the advisor may name a canonical tier and nothing else. Any
        # other value is dropped here -- never an exception, never a guess.
        if advisor_tier is not None and (not isinstance(advisor_tier, str)
                                         or advisor_tier not in _ORDER):
            log.warning("ignoring advisor_tier outside the canonical tiers "
                        "request=%s", ctx.request_id)
            advisor_tier = None
        # 1. eligibility: residency/placement, prior failures, the app's own
        #    scope, the request's pinned model and the registry's per-model
        #    gates. ONE authority for all of it (app/policy/feasible.py) --
        #    this step used to re-derive the same decision the policy gate had
        #    already made, and the two could disagree.
        #    Open circuits are deliberately NOT applied here: this is the
        #    POLICY-eligible set, and every tier decision below reads it (see
        #    `select`). feasible_set's stage 7 stays the labelling authority for
        #    which of the chosen tier's final candidates are open (step 4b).
        #    The POLICY-eligible set (B70) is the same computation with no
        #    failure exclusions: tier targeting and the hard floor read it, so
        #    a failure on this request can never clamp the floor down.
        policy = feasible_set(self._s, self._reg, ctx, tenant_id=ctx.tenant_id)
        feasible = (feasible_set(self._s, self._reg, ctx, tenant_id=ctx.tenant_id,
                                 exclude=exclude)
                    if exclude else policy)
        circuit_rejections: dict[str, RejectedCandidate] = {}
        if open_circuits:
            for r in feasible_set(self._s, self._reg, ctx, tenant_id=ctx.tenant_id,
                                  exclude=exclude,
                                  open_circuits=open_circuits).rejected:
                if r.code == CIRCUIT_OPEN:
                    circuit_rejections[r.name] = RejectedCandidate(
                        name=r.name, provider=r.provider, tier=r.tier,
                        display_name=r.display_name, reason=r.reason, code=r.code)
        allowed = list(feasible.eligible)
        policy_allowed = list(policy.eligible)
        rejected: list[RejectedCandidate] = [
            RejectedCandidate(name=r.name, provider=r.provider, tier=r.tier,
                              display_name=r.display_name, reason=r.reason,
                              code=r.code)
            for r in feasible.rejected]
        if not allowed:
            # P3b: explain the rejection with its minimal conflicting core.
            core = self._safe_core(lambda: self._policy_core(ctx, exclude))
            if not feasible.rejected:   # nothing filtered because there was nothing
                raise NoEligibleCandidate(
                    _with_core(
                        "no candidates after residency filter: the model pool is empty; "
                        "onboard a model (residency cannot be satisfied by an empty pool)",
                        core),
                    core=core)
            raise NoEligibleCandidate(
                _with_core(f"no candidates after residency filter: {_explain(rejected)}",
                           core),
                rejected=tuple((r.name, r.code) for r in rejected),
                core=core)

        # B80: a quality floor is live only with a floor for this workload and
        # mode shadow/enforce; otherwise today's tiered path, untouched.
        if (ctx.quality_floor is not None
                and ctx.quality_floor_mode in ("shadow", "enforce")):
            # B108 / design §2: an ENFORCED floor decides on its own; the
            # advisor is not consulted for this call (its unmet fallback
            # included). In floor `shadow` the tiered path decides, so the
            # advisor seam applies there as normal.
            if ctx.quality_floor_mode == "enforce":
                advisor_tier = None
            return self._select_floor(ctx, policy_allowed, allowed, rejected,
                                      circuit_rejections, tokens_after, complexity,
                                      min_tier, out_tokens, advisor_tier)
        return self._select_tiered(ctx, policy_allowed, allowed, rejected,
                                   circuit_rejections, tokens_after, complexity,
                                   min_tier, out_tokens, advisor_tier)

    def _floor_kind(self, ctx: ExecutionPolicyContext, min_tier: str | None) -> str:
        """Which requirement set the hard floor: the escalation's `min_tier`
        when it is above the risk class's floor, else the risk floor."""
        risk_floor = self._s.routing.risk_floors[ctx.risk_class]
        if min_tier is not None and _ORDER[min_tier] > _ORDER[risk_floor]:
            return "escalation floor"
        return "risk floor"

    def _hard_floor(self, ctx: ExecutionPolicyContext, min_tier: str | None,
                    policy_allowed: list[PoolCandidate]) -> str:
        """The HARD floor: `max(risk floor, min_tier)` clamped down to the best
        tier in the POLICY-eligible set (rule 8). Only policy lowers it."""
        tiers = [self._s.routing.risk_floors[ctx.risk_class]]
        if min_tier:
            tiers.append(min_tier)
        return _clamp_to_best_allowed(max(tiers, key=lambda t: _ORDER[t]), policy_allowed)

    def _below_floor(self, ctx: ExecutionPolicyContext, hard: str,
                     min_tier: str | None, allowed: list[PoolCandidate],
                     rejected: list[RejectedCandidate]) -> NoEligibleCandidate:
        """Every candidate at or above the hard floor has failed on this
        request; only lower tiers survive. Content-free: tier names only."""
        kind = self._floor_kind(ctx, min_tier)
        below = ", ".join(sorted({c.tier for c in allowed}, key=lambda t: _ORDER[t]))
        return NoEligibleCandidate(
            f"no failover candidate at or above the request's {kind} ({hard}): every "
            f"eligible endpoint at or above it has already failed on this request, and "
            f"the remaining candidates (tier {below}) are below that floor -- failover "
            f"never lowers a request below its {kind}",
            rejected=tuple((r.name, r.code) for r in rejected),
            below_floor=hard)

    def _select_tiered(self, ctx: ExecutionPolicyContext,
                       policy_allowed: list[PoolCandidate],
                       allowed: list[PoolCandidate],
                       rejected: list[RejectedCandidate],
                       circuit_rejections: dict[str, RejectedCandidate],
                       tokens_after: int, complexity: float,
                       min_tier: str | None, out_tokens: int,
                       advisor_tier: str | None = None) -> RoutingDecision:
        """Today's path, steps 2-5: tier targeting, budget, circuits, score.
        `policy_allowed` is the non-empty policy-eligible set (tier targeting
        reads it); `allowed` is its non-empty subset that survives `exclude`
        (candidates come from it). `rejected` (mutated) already holds the
        eligibility rejections."""
        # 2. target tier from complexity + risk floor + escalation, and the
        #    hard floor from risk floor + escalation -- both clamped to the
        #    best POLICY-permitted tier (rule 8), never to what survives a
        #    failure (B70). B108: a validated `advisor_tier` stands in for the
        #    complexity tier -- the first element only (see `select`).
        targets = [advisor_tier if advisor_tier is not None
                   else self._complexity_tier(complexity),
                   self._s.routing.risk_floors[ctx.risk_class]]
        if min_tier:
            targets.append(min_tier)
        target = _clamp_to_best_allowed(max(targets, key=lambda t: _ORDER[t]),
                                        policy_allowed)
        hard = self._hard_floor(ctx, min_tier, policy_allowed)
        policy_avail = sorted({c.tier for c in policy_allowed}, key=lambda t: _ORDER[t])
        # the clamp above guarantees a match exists; [-1] is a defensive fallback
        target_pick = next((t for t in policy_avail if _ORDER[t] >= _ORDER[target]),
                           policy_avail[-1])
        avail = sorted({c.tier for c in allowed}, key=lambda t: _ORDER[t])
        # Same-tier peer first, then the lowest surviving tier above it (the
        # tier escalates on exhaustion). With no failure exclusion this is
        # `target_pick` itself.
        tier_pick = next((t for t in avail if _ORDER[t] >= _ORDER[target_pick]), None)
        tier_downgrade: dict | None = None
        if tier_pick is None:
            # Nothing survives at or above the target. Step DOWN only to a tier
            # at or above the hard floor -- i.e. only what complexity alone
            # raised the target over (B70 item 3 / B71 item 2). B108: an
            # advisor-raised target is the same soft element and steps down
            # the same way; the hard floor never moves.
            lower = [t for t in avail if _ORDER[hard] <= _ORDER[t] < _ORDER[target_pick]]
            if not lower:
                raise self._below_floor(ctx, hard, min_tier, allowed, rejected)
            tier_pick = lower[-1]
            tier_downgrade = {"from": target_pick, "to": tier_pick,
                              "cause": PRIOR_FAILURE_DOWNGRADE}
        picked_tier = tier_pick   # before any budget fallback moves it (P3b core)

        fallback_from: str | None = None
        # 3. keep only the target tier; 4. drop over-budget
        eligible: list[tuple[PoolCandidate, float]] = []
        over: list[tuple[PoolCandidate, float]] = []
        for c in allowed:
            if c.tier != tier_pick:
                reason = ("below target tier" if _ORDER[c.tier] < _ORDER[tier_pick]
                          else "above target tier")
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason=reason))
                continue
            est = self._est_cost(c, tokens_after, out_tokens)
            if est > ctx.max_cost_usd:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason="over budget"))
                over.append((c, est))
            else:
                eligible.append((c, est))
        if not eligible:
            # Budget fallback: step DOWN to the most capable cheaper allowed
            # tier that fits the cap. Floor = the hard floor (risk floor /
            # escalation min_tier, clamped against the POLICY-eligible set;
            # never undercut -- complexity is the only thing budget may
            # override). Only `allowed` candidates are considered, so
            # residency and app scope are untouched (rules 3/8).
            floor = hard
            lower = sorted((t for t in avail
                            if _ORDER[floor] <= _ORDER[t] < _ORDER[tier_pick]),
                           key=lambda t: -_ORDER[t])
            skipped: list[str] = []   # lower tiers passed over: every member over budget
            for t in lower:
                fits = [(c, e) for c, e in
                        ((c, self._est_cost(c, tokens_after, out_tokens))
                         for c in allowed if c.tier == t)
                        if e <= ctx.max_cost_usd]
                if fits:
                    fits_names = {c.name for c, _ in fits}
                    relabel = {t, *skipped}
                    rejected = [r for r in rejected
                                if not (r.tier in relabel and r.reason == "below target tier")]
                    rejected.extend(
                        RejectedCandidate(name=c.name, provider=c.provider, tier=c.tier,
                                          display_name=c.display_name, reason="over budget")
                        for c in allowed
                        if c.tier in relabel and c.name not in fits_names)
                    fallback_from, tier_pick, eligible = tier_pick, t, fits
                    break
                skipped.append(t)
        if not eligible:
            cheapest, cheapest_est = min(over, key=lambda ce: ce[1])
            # P3b: `floor` was set by the fallback block above, which always
            # runs before this one (both are guarded by `not eligible`).
            core = self._safe_core(lambda: self._budget_core(
                ctx, allowed, tier_pick=picked_tier, floor=floor,
                tokens_after=tokens_after, out_tokens=out_tokens, min_tier=min_tier))
            raise NoEligibleCandidate(
                _with_core(
                    f"no candidates after budget filter: cheapest candidate {cheapest.name} "
                    f"(tier {cheapest.tier}) is estimated at ${cheapest_est:.4f} "
                    f"({tokens_after} prompt + {out_tokens} output tokens), over the "
                    f"per-request cap ${ctx.max_cost_usd:.4f}. Raise the application's "
                    "max_cost_usd_per_request, lower max_tokens, or lower the model's price. "
                    "The figure is an estimate from RouteForge's own token count, which "
                    "runs roughly 1.5x what vLLM reports for English prose, so the "
                    "request may cost less than shown.",
                    core),
                rejected=tuple((c.name, None) for c, _e in over),
                core=core)

        # 4b. drop open circuits among the chosen tier's final candidates. If
        #     every one of them is open: a tier above the hard floor steps
        #     down to the highest lower tier (>= hard floor) with a closed,
        #     in-budget candidate; otherwise bypass the breaker within the
        #     tier (see `select`).
        circuit_bypass = False
        if circuit_rejections:
            closed = [(c, e) for c, e in eligible if c.name not in circuit_rejections]
            if closed:
                rejected.extend(circuit_rejections[c.name] for c, _e in eligible
                                if c.name in circuit_rejections)
                eligible = closed
            else:
                step = self._circuit_step_down(ctx, allowed, avail, hard, tier_pick,
                                               circuit_rejections, tokens_after,
                                               out_tokens)
                if step is None:
                    circuit_bypass = True
                else:
                    t, fits = step
                    fits_names = {c.name for c, _e in fits}
                    relabel = {x for x in avail if _ORDER[t] <= _ORDER[x] < _ORDER[tier_pick]}
                    rejected = [r for r in rejected
                                if not (r.tier in relabel and r.reason == "below target tier")]
                    rejected.extend(circuit_rejections[c.name] for c, _e in eligible)
                    rejected.extend(
                        circuit_rejections[c.name] if c.name in circuit_rejections
                        else RejectedCandidate(name=c.name, provider=c.provider, tier=c.tier,
                                               display_name=c.display_name,
                                               reason="over budget")
                        for c in allowed if c.tier in relabel and c.name not in fits_names)
                    # `from` is always the tier targeting picked (never an
                    # upward-failover or budget-fallback tier); `to` is set
                    # to the served tier below.
                    tier_downgrade = {"from": target_pick, "to": t,
                                      "cause": CIRCUIT_OPEN_DOWNGRADE}
                    tier_pick, eligible = t, fits

        # 5. score within the tier (chooses provider/model — cheaper on-prem
        #    wins). Delegated to the pluggable strategy (app/routing/strategy.py)
        #    -- it only ranks this already-filtered eligible set, it can never
        #    add a candidate back in. `self._signals` is a persistent,
        #    traffic-fed store shared with the Gateway (see app/main.py) --
        #    NOT a fresh one per call.
        result = self._rank(eligible, ctx, tokens_after)
        ordered = result.ordered
        chosen, est = ordered[0].candidate, ordered[0].est_cost_usd
        for sc in ordered[1:]:
            c = sc.candidate
            rejected.append(RejectedCandidate(
                name=c.name, provider=c.provider, tier=c.tier,
                display_name=c.display_name, reason="lower score"))
        # The certificate's quality is the ranked term itself, so the two
        # agree by construction (never a second resolve).
        chosen_q = ordered[0].quality
        reason = (f"tier {chosen.tier} (target {target}, budget fallback from {fallback_from})"
                  if fallback_from else f"tier {chosen.tier} (target {target})")
        if circuit_bypass:
            reason += "; circuit bypass: every candidate in the target tier is open"
        if tier_downgrade is not None:
            # A later stage (budget fallback, circuit step-down) may have moved
            # the tier again after the downgrade: `to` is the SERVED tier.
            tier_downgrade["to"] = chosen.tier
            reason += (f"; tier downgrade from {tier_downgrade['from']}: "
                       f"{_DOWNGRADE_WORDS[tier_downgrade['cause']]}")
        return RoutingDecision(
            chosen=chosen, est_cost_usd=est,
            reason=reason,
            rejected=rejected,
            quality_score=chosen_q.value, quality_source=chosen_q.source,
            scoring=result, circuit_bypass=circuit_bypass,
            tier_target=target, tier_downgrade=tier_downgrade)

    def _circuit_step_down(self, ctx: ExecutionPolicyContext,
                           allowed: list[PoolCandidate], avail: list[str],
                           hard: str, tier: str,
                           circuit_rejections: dict[str, RejectedCandidate],
                           tokens_after: int, out_tokens: int,
                           ) -> tuple[str, list[tuple[PoolCandidate, float]]] | None:
        """B71 item 1: every final candidate in `tier` is open. When `tier` is
        above the hard floor, the highest lower tier at or above it that has a
        closed, in-budget, non-excluded candidate (`allowed` already excludes
        prior failures) -- as `(tier, fits)`. None means bypass within `tier`."""
        if _ORDER[tier] <= _ORDER[hard]:
            return None
        for t in sorted((x for x in avail if _ORDER[hard] <= _ORDER[x] < _ORDER[tier]),
                        key=lambda x: -_ORDER[x]):
            fits = [(c, e) for c, e in
                    ((c, self._est_cost(c, tokens_after, out_tokens))
                     for c in allowed
                     if c.tier == t and c.name not in circuit_rejections)
                    if e <= ctx.max_cost_usd]
            if fits:
                return t, fits
        return None

    def _rank(self, eligible: list[tuple[PoolCandidate, float]],
              ctx: ExecutionPolicyContext, tokens_after: int) -> "RankResult":
        """Step 5's strategy call over an already-filtered set."""
        cached_pricing = {}
        for c, _e in eligible:
            try:
                p = (self._reg.price_for(c.name) if self._reg
                     else self._s.pricing.models[c.name])
            except KeyError:
                continue
            cached_pricing[c.name] = "priced" if p.cached_input is not None else "unpriced"

        return self._strategy.rank(
            eligible, ctx=ctx, tokens_after=tokens_after,
            weights=self._s.routing.weights, quality_resolver=self._quality,
            signals=self._signals, signals_cfg=self._s.routing.signals,
            cached_pricing=cached_pricing)

    # --- B80: qualify-then-cheapest under a quality floor (spec §4.4) --------

    def _floor_choose(self, pick: FloorPick, ctx: ExecutionPolicyContext,
                      circuit_rejections: dict[str, RejectedCandidate],
                      tokens_after: int):
        """The cheapest qualifier after the open-circuit drop, score breaking
        ties. If every qualifier is open, bypass the breaker WITHIN the
        qualifiers (never leaves them). Returns `(scored, result, open_dropped,
        bypass)`; `scored` is the chosen ScoredCandidate."""
        qualifiers = pick.qualifiers
        open_dropped: list[RejectedCandidate] = []
        bypass = False
        if circuit_rejections:
            closed = [(c, e) for c, e in qualifiers if c.name not in circuit_rejections]
            if closed:
                open_dropped = [circuit_rejections[c.name] for c, _e in qualifiers
                                if c.name in circuit_rejections]
                qualifiers = closed
            else:
                bypass = True
        result = self._rank(qualifiers, ctx, tokens_after)
        # `ordered` is best-first, so its index is the score tie-break.
        scored = min(enumerate(result.ordered),
                     key=lambda i_sc: (i_sc[1].est_cost_usd, i_sc[0]))[1]
        return scored, result, open_dropped, bypass

    def _select_floor(self, ctx: ExecutionPolicyContext,
                      policy_allowed: list[PoolCandidate],
                      allowed: list[PoolCandidate],
                      rejected: list[RejectedCandidate],
                      circuit_rejections: dict[str, RejectedCandidate],
                      tokens_after: int, complexity: float,
                      min_tier: str | None, out_tokens: int,
                      advisor_tier: str | None = None) -> RoutingDecision:
        """Mode shadow/enforce with a floor. Qualifiers are a subset of
        `allowed` -- what survives `exclude` (rule 3) -- at or above the hard
        floor `max(risk_floor, min_tier)` clamped to the best POLICY-eligible
        tier exactly as today's target is (rule 8; B70: never to what survives
        a failure). Enforce + met: the cheapest qualifier. Anything else:
        today's decision (which obeys the same hard floor), with the floor
        record attached."""
        floor, mode, workload = ctx.quality_floor, ctx.quality_floor_mode, ctx.workload
        hard = self._hard_floor(ctx, min_tier, policy_allowed)
        pick = qualify(
            allowed, floor=floor, workload=workload, hard_tier=hard,
            prompt_tokens=tokens_after, output_tokens=out_tokens,
            max_cost_usd=ctx.max_cost_usd,
            est_cost=lambda c: self._est_cost(c, tokens_after, out_tokens),
            lower=lambda name: self._quality.resolve_lower(name, workload))
        choice = (self._floor_choose(pick, ctx, circuit_rejections, tokens_after)
                  if pick.status == "met" else None)

        def record(chosen_name: str, status: str, shadow=None) -> dict:
            lower, source = pick.bounds[chosen_name]
            return {
                "floor": floor, "mode": mode, "workload": workload, "status": status,
                "chosen_lower": lower, "chosen_lower_source": source,
                "bounds": {n: [lb, src] for n, (lb, src) in pick.bounds.items()},
                "shadow_choice": shadow.candidate.name if shadow else None,
                "shadow_est_cost_usd": shadow.est_cost_usd if shadow else None,
            }

        if mode == "enforce" and choice is not None:
            scored, result, open_dropped, bypass = choice
            chosen = scored.candidate
            rejected = list(rejected)
            rejected.extend(
                RejectedCandidate(name=c.name, provider=c.provider, tier=c.tier,
                                  display_name=c.display_name,
                                  reason=pick.rejections[c.name],
                                  code=_FLOOR_CODES.get(pick.rejections[c.name]))
                for c in allowed if c.name in pick.rejections)
            rejected.extend(open_dropped)
            rejected.extend(
                RejectedCandidate(name=sc.candidate.name, provider=sc.candidate.provider,
                                  tier=sc.candidate.tier,
                                  display_name=sc.candidate.display_name,
                                  reason="higher cost", code="higher_cost")
                for sc in result.ordered if sc is not scored)
            lower, source = pick.bounds[chosen.name]
            reason = (f"quality floor {floor:.2f} (workload {workload}): cheapest "
                      f"qualifying, lower bound {lower:.2f} ({source})")
            if bypass:
                reason += "; circuit bypass: every qualifying candidate is open"
            return RoutingDecision(
                chosen=chosen, est_cost_usd=scored.est_cost_usd, reason=reason,
                rejected=rejected,
                quality_score=scored.quality.value, quality_source=scored.quality.source,
                scoring=result, circuit_bypass=bypass, tier_target=chosen.tier,
                quality_floor=record(chosen.name, "met"))

        # `advisor_tier` is always None here in floor `enforce` (`_select`).
        decision = self._select_tiered(ctx, policy_allowed, allowed, list(rejected),
                                       circuit_rejections, tokens_after, complexity,
                                       min_tier, out_tokens, advisor_tier)
        if mode == "enforce":
            decision.reason = (f"quality floor {floor:.2f} unmet "
                               f"({_UNMET_WORDS[pick.status]}): {decision.reason}")
            decision.quality_floor = record(decision.chosen.name, pick.status)
        else:
            decision.quality_floor = record(
                decision.chosen.name, f"shadow_{pick.status}",
                shadow=choice[0] if choice is not None else None)
        return decision
