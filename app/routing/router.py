from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.schema import PoolCandidate, Settings
from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext, RejectedCandidate
from app.routing.quality import QualityResolver
from app.routing.strategy import RoutingStrategy, build_strategy

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}


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

    def _est_cost(self, cand: PoolCandidate, tokens_after: int) -> float:
        p = (self._reg.price_for(cand.name) if self._reg
             else self._s.pricing.models[cand.name])
        return tokens_after / 1_000_000 * (p.input + p.output)

    def select(self, ctx: ExecutionPolicyContext, tokens_after: int,
               complexity: float, min_tier: str | None = None,
               exclude: frozenset[str] = frozenset()) -> RoutingDecision:
        rejected: list[RejectedCandidate] = []
        pool = self._reg.effective_pool() if self._reg else self._s.routing.model_pool

        # 1. residency filter (residency beats everything below), then the
        #    per-app scope filter and the admin disable-gate for on-prem
        #    candidates that pass residency
        allowed = []
        for c in pool:
            if c.provider not in ctx.allowed_providers:
                reason = ("excluded · residency" if ctx.onprem_only
                          else "not permitted for application")
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason=reason))
                continue
            # Failure-driven lateral switch: drop a model the gateway already
            # saw fail on this request. Placed AFTER the residency check so an
            # excluded cloud model under onprem_only still reads "residency",
            # never "prior failure" -- excluding a name only ever removes a
            # candidate, so it can never weaken residency.
            if c.name in exclude:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason="excluded · prior failure"))
                continue
            if ctx.model_scope_active and c.name not in ctx.allowed_models:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name,
                    reason="not permitted for application"))
                continue
            if ctx.requested_model is not None and c.name != ctx.requested_model:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason="not requested model"))
                continue
            if (self._reg is not None
                    and not self._reg.is_allowed_for_app(c.name, ctx.tenant_id)):
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name,
                    reason="not permitted for application"))
                continue
            if (c.provider == "onprem" and self._reg is not None
                    and not self._reg.is_enabled(c.name)):
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason="disabled by admin"))
                continue
            allowed.append(c)
        if not allowed:
            raise NoEligibleCandidate("no candidates after residency filter")

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

        # 3. keep only the target tier; 4. drop over-budget
        eligible: list[tuple[PoolCandidate, float]] = []
        for c in allowed:
            if c.tier != tier_pick:
                reason = ("below target tier" if _ORDER[c.tier] < _ORDER[tier_pick]
                          else "above target tier")
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason=reason))
                continue
            est = self._est_cost(c, tokens_after)
            if est > ctx.max_cost_usd:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason="over budget"))
            else:
                eligible.append((c, est))
        if not eligible:
            raise NoEligibleCandidate("no candidates after budget filter")

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
            reason=f"tier {chosen.tier} (target {target})", rejected=rejected,
            quality_score=chosen_q.score, quality_source=chosen_q.source)
