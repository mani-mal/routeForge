from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.schema import PoolCandidate, Settings
from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext, RejectedCandidate
from app.policy.feasible import feasible_set
from app.routing.quality import QualityResolver
from app.routing.strategy import RoutingStrategy, build_strategy

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}

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


def _explain(rejected) -> str:
    counts = Counter(r.reason for r in rejected)
    return "; ".join(f"{n} {reason} ({_FIX_HINTS.get(reason, reason)})"
                     for reason, n in counts.most_common())


def expected_output_tokens(settings: Settings, params: dict | None) -> int:
    """Output tokens to price a request at: the client's own cap, else the
    deployment's `budgets.default_estimated_output_tokens`."""
    p = params or {}
    for key in ("max_completion_tokens", "max_tokens"):
        if p.get(key):
            return int(p[key])
    return int(settings.policy.budgets["default_estimated_output_tokens"])


@dataclass
class RoutingDecision:
    chosen: PoolCandidate
    est_cost_usd: float
    reason: str
    rejected: list[RejectedCandidate] = field(default_factory=list)
    quality_score: float = 0.0
    quality_source: str = "tier_default"


class Router:
    def __init__(self, settings: Settings,
                 quality_resolver: QualityResolver | None = None,
                 registry: "ModelRegistry | None" = None,
                 strategy: RoutingStrategy | None = None) -> None:
        self._s = settings
        self._reg = registry
        # Registry-aware by default (onboarded on-prem models never appear in
        # the static YAML pool -- see QualityResolver's docstring) unless the
        # caller injects its own resolver (e.g. with DB-sourced overrides).
        self._quality = quality_resolver or QualityResolver(settings, registry=registry)
        # Step-5 scoring seam (app/routing/strategy.py). Injectable like
        # quality_resolver; config-selected otherwise (routing.strategy).
        self._strategy = strategy or build_strategy(settings.routing.strategy)

    def _complexity_tier(self, complexity: float) -> str:
        t = self._s.routing.complexity_thresholds
        if complexity <= t["cheap_max"]:
            return "cheap"
        if complexity <= t["mid_max"]:
            return "mid"
        return "frontier"

    def _est_cost(self, cand: PoolCandidate, tokens_after: int, output_tokens: int) -> float:
        p = (self._reg.price_for(cand.name) if self._reg
             else self._s.pricing.models[cand.name])
        return (tokens_after * p.input + output_tokens * p.output) / 1_000_000

    def select(self, ctx: ExecutionPolicyContext, tokens_after: int,
               complexity: float, min_tier: str | None = None,
               exclude: frozenset[str] = frozenset(),
               output_tokens: int | None = None) -> RoutingDecision:
        out_tokens = (output_tokens if output_tokens is not None
                      else expected_output_tokens(self._s, None))
        # 1. eligibility: residency/placement, prior failures, the app's own
        #    scope, the request's pinned model and the registry's per-model
        #    gates. ONE authority for all of it (app/policy/feasible.py) --
        #    this step used to re-derive the same decision the policy gate had
        #    already made, and the two could disagree.
        feasible = feasible_set(self._s, self._reg, ctx, tenant_id=ctx.tenant_id,
                                exclude=exclude)
        allowed = list(feasible.eligible)
        rejected: list[RejectedCandidate] = [
            RejectedCandidate(name=r.name, provider=r.provider, tier=r.tier,
                              display_name=r.display_name, reason=r.reason)
            for r in feasible.rejected]
        if not allowed:
            if not feasible.rejected:   # nothing filtered because there was nothing
                raise NoEligibleCandidate(
                    "no candidates after residency filter: the model pool is empty; "
                    "onboard a model (residency cannot be satisfied by an empty pool)")
            raise NoEligibleCandidate(
                f"no candidates after residency filter: {_explain(rejected)}")

        # 2. target tier from complexity + risk floor + escalation, clamped to
        #    the best residency-permitted tier
        targets = [self._complexity_tier(complexity),
                   self._s.routing.risk_floors[ctx.risk_class]]
        if min_tier:
            targets.append(min_tier)
        target = max(targets, key=lambda t: _ORDER[t])
        best_allowed_order = max(_ORDER[c.tier] for c in allowed)
        if _ORDER[target] > best_allowed_order:
            target = next(t for t, o in _ORDER.items() if o == best_allowed_order)
        avail = sorted({c.tier for c in allowed}, key=lambda t: _ORDER[t])
        # the clamp above guarantees a match exists; avail[-1] is a defensive fallback
        tier_pick = next((t for t in avail if _ORDER[t] >= _ORDER[target]), avail[-1])

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
            # tier that fits the cap. Floor = risk floor / escalation min_tier
            # (never undercut; complexity is the only thing budget may
            # override). Only `allowed` candidates are considered, so
            # residency and app scope are untouched (rules 3/8).
            floor_tiers = [self._s.routing.risk_floors[ctx.risk_class]]
            if min_tier:
                floor_tiers.append(min_tier)
            floor = max(floor_tiers, key=lambda t: _ORDER[t])
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
            raise NoEligibleCandidate(
                f"no candidates after budget filter: cheapest candidate {cheapest.name} "
                f"(tier {cheapest.tier}) is estimated at ${cheapest_est:.4f} "
                f"({tokens_after} prompt + {out_tokens} output tokens), over the per-request "
                f"cap ${ctx.max_cost_usd:.4f}. Raise the application's "
                "max_cost_usd_per_request, lower max_tokens, or lower the model's price.")

        # 5. score within the tier (chooses provider/model — cheaper on-prem
        #    wins). Delegated to the pluggable strategy (app/routing/strategy.py)
        #    -- it only ranks this already-filtered eligible set, it can never
        #    add a candidate back in.
        ordered = self._strategy.rank(
            eligible, ctx=ctx, tokens_after=tokens_after,
            weights=self._s.routing.weights, quality_resolver=self._quality)
        chosen, est = ordered[0]
        for c, _e in ordered[1:]:
            rejected.append(RejectedCandidate(
                name=c.name, provider=c.provider, tier=c.tier,
                display_name=c.display_name, reason="lower score"))
        chosen_q = self._quality.resolve(chosen.name)
        return RoutingDecision(
            chosen=chosen, est_cost_usd=est,
            reason=(f"tier {chosen.tier} (target {target}, budget fallback from {fallback_from})"
                    if fallback_from else f"tier {chosen.tier} (target {target})"),
            rejected=rejected,
            quality_score=chosen_q.score, quality_source=chosen_q.source)
