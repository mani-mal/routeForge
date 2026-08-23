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
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.config.schema import PoolCandidate

if TYPE_CHECKING:
    from app.models import ExecutionPolicyContext
    from app.routing.quality import QualityResolver

# Tier -> latency proxy used by the default scoring formula. Owned by the
# default strategy (not every strategy needs a latency proxy shaped like this).
_L = {"cheap": 0.2, "mid": 0.5, "frontier": 1.0}


class RoutingStrategy(Protocol):
    def rank(self, eligible: list[tuple[PoolCandidate, float]], *,
              ctx: "ExecutionPolicyContext", tokens_after: int,
              weights: dict[str, float],
              quality_resolver: "QualityResolver") -> list[tuple[PoolCandidate, float]]:
        """Order `eligible` best-first. MUST return exactly the same members
        (same length, same candidates) it was given, only reordered -- a
        strategy ranks, it never filters-in. `eligible` is already the
        post-residency/scope/tier/budget-filtered set (see module docstring);
        this method must never be handed, and can never produce, a candidate
        that filtering excluded."""
        ...


class DefaultRoutingStrategy:
    """Today's exact scoring, unchanged: within the target tier, prefer high
    quality, low (tier-max-normalized) cost, low latency-proxy, high
    capacity weight; tie-break by raw est_cost ascending. Byte-identical to
    the pre-refactor inline `score`/sort in `Router.select`."""

    def rank(self, eligible: list[tuple[PoolCandidate, float]], *,
              ctx: "ExecutionPolicyContext", tokens_after: int,
              weights: dict[str, float],
              quality_resolver: "QualityResolver") -> list[tuple[PoolCandidate, float]]:
        max_cost = max((est for _, est in eligible), default=0.0)

        def score(item: tuple[PoolCandidate, float]) -> float:
            c, est = item
            norm = est / max_cost if max_cost > 0 else 0.0
            q = quality_resolver.resolve(c.name).score
            return (weights["quality"] * q - weights["cost"] * norm
                    - weights["latency"] * _L[c.tier] + weights["capacity"] * 1.0)

        return sorted(eligible, key=lambda it: (-score(it), it[1]))


_STRATEGIES: dict[str, type[RoutingStrategy]] = {"default": DefaultRoutingStrategy}


def build_strategy(name: str) -> RoutingStrategy:
    try:
        cls = _STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"unknown routing.strategy {name!r}; known strategies: "
            f"{sorted(_STRATEGIES)}") from None
    return cls()
