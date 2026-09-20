"""Step-6 scoring seam.

`Router.select` (app/routing/router.py) FILTERS the pool down to an eligible
`(candidate, est_cost)` list — residency, per-app scope, tier target, and
budget (CLAUDE.md rules 3/8). That filtering never moves here. A
`RoutingStrategy` only RANKS the already-filtered eligible set: it receives
exactly the candidates `select` decided are admissible and returns them
ordered best-first. It has no way to add a candidate that wasn't in its
input, so it can never reintroduce a residency-filtered (e.g. cloud, under
`onprem_only`) candidate -- that's the residency invariant this seam exists
to preserve while still letting the ranking itself be swapped (e.g. for a
future NeMo Switchyard strategy).

**P2a: measured signals and provenance (spec §6.2, §7.3).** Scoring now
consumes a `SignalStore` (app/routing/signals.py) for per-model latency and
availability instead of hardcoded constants, and reports -- per candidate,
per term -- not just the value used but where it came from (`measured`,
`tier_default`, `neutral`, `disabled`, or a `QUALITY_SOURCES` member). The
whole reason this is worth doing is the identity property: with an empty
`SignalStore`, the formula below reproduces the pre-P2a scores exactly (see
`DefaultRoutingStrategy` and `tests/routing/test_strategy_identity.py`), so
every score change post-deploy is attributable to a measurement that
actually exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.config.schema import PoolCandidate

if TYPE_CHECKING:
    from app.config.schema import SignalsCfg
    from app.models import ExecutionPolicyContext
    from app.routing.quality import QualityResolver
    from app.routing.signals import SignalStore


@dataclass(frozen=True)
class TermValue:
    """One scoring term's value AND where the value came from.

    The provenance is the point. A router that reports `latency: 342ms
    (measured, n=128)` and one that reports `latency: 200ms (tier_default,
    n=0)` have made very different claims, and before P2a they were
    indistinguishable on the wire.
    """
    value: float
    source: str
    samples: int = 0


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: PoolCandidate
    est_cost_usd: float
    score: float
    quality: TermValue          # source from QUALITY_SOURCES
    latency: TermValue          # ms; measured | tier_default | disabled
    availability: TermValue     # [0,1]; measured | neutral | disabled
    cost_norm: float
    cost_cached_pricing: str    # "priced" | "unpriced"


@dataclass(frozen=True)
class RankResult:
    """Everything needed to RECOMPUTE the ranking offline from the record
    alone -- no SignalStore, no live config. See spec §7.3."""
    ordered: list[ScoredCandidate]      # best first
    weights: dict[str, float]
    signals_enabled: bool
    latency_scale_ms: float
    max_latency_norm: float


class RoutingStrategy(Protocol):
    def rank(self, eligible: list[tuple[PoolCandidate, float]], *,
             ctx: "ExecutionPolicyContext", tokens_after: int,
             weights: dict[str, float],
             quality_resolver: "QualityResolver",
             signals: "SignalStore",
             signals_cfg: "SignalsCfg",
             cached_pricing: dict[str, str] | None = None) -> RankResult:
        """Order `eligible` best-first and report how each score was reached.

        MUST return exactly the same members it was given, only reordered -- a
        strategy ranks, it never filters-in. `eligible` is already the
        post-residency/scope/tier/budget-filtered set (see module docstring);
        `signals` can reorder it but can never add to or remove from it.
        """
        ...


class DefaultRoutingStrategy:
    """Scores within the target tier: prefer high quality, low
    (tier-max-normalized) cost, low MEASURED latency, high availability;
    tie-break by raw est_cost ascending.

    **The identity property.** Latency normalizes against a FIXED
    `latency_scale_ms`, never the candidate-set max. With the shipped defaults
    (`tier_latency_ms` 200/500/1000, `latency_scale_ms` 1000) an unmeasured
    candidate's latency term is exactly 0.2/0.5/1.0 -- the pre-P2a `_L`
    constants -- and unmeasured availability is exactly 1.0, the pre-P2a
    hardcoded constant. So with an empty SignalStore this produces numerically
    identical scores to the pre-P2a formula, and every behaviour change is
    attributable to a measurement actually existing.

    Do NOT switch latency to set-max normalization "for consistency" with cost.
    Cost is comparative (dollars vary by orders of magnitude between models);
    latency is absolute (a millisecond means the same thing everywhere). Set-max
    on latency would rescale the term whenever the eligible set's tier mix
    changed, breaking the identity property for reasons unrelated to any
    measurement. `tests/routing/test_strategy_identity.py` pins this.
    """

    def rank(self, eligible, *, ctx, tokens_after, weights, quality_resolver,
             signals, signals_cfg, cached_pricing=None) -> RankResult:
        max_cost = max((est for _, est in eligible), default=0.0)
        scored: list[ScoredCandidate] = []

        for cand, est in eligible:
            q = quality_resolver.resolve(cand.name)
            lat = signals.latency(cand.name, cand.tier)
            avail = signals.availability(cand.name)

            cost_norm = est / max_cost if max_cost > 0 else 0.0
            lat_norm = min(lat.value / signals_cfg.latency_scale_ms,
                           signals_cfg.max_latency_norm)
            score = (weights["quality"] * q.score
                     - weights["cost"] * cost_norm
                     - weights["latency"] * lat_norm
                     + weights["availability"] * avail.value)

            scored.append(ScoredCandidate(
                candidate=cand, est_cost_usd=est, score=score,
                quality=TermValue(q.score, q.source),
                latency=TermValue(lat.value, lat.source, lat.samples),
                availability=TermValue(avail.value, avail.source, avail.samples),
                cost_norm=cost_norm,
                cost_cached_pricing=(cached_pricing or {}).get(cand.name, "unpriced")))

        scored.sort(key=lambda sc: (-sc.score, sc.est_cost_usd))
        return RankResult(ordered=scored, weights=dict(weights),
                          signals_enabled=signals_cfg.enabled,
                          latency_scale_ms=signals_cfg.latency_scale_ms,
                          max_latency_norm=signals_cfg.max_latency_norm)


_STRATEGIES: dict[str, type[RoutingStrategy]] = {"default": DefaultRoutingStrategy}


def build_strategy(name: str) -> RoutingStrategy:
    try:
        cls = _STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"unknown routing.strategy {name!r}; known strategies: "
            f"{sorted(_STRATEGIES)}") from None
    return cls()
