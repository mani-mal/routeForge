"""Baseline and router selection strategies for FeasibleRouteBench (spec §5
step 3).

A strategy is `Callable[[StrategyInput], str | None]`: given a case's already
-computed eligible set (never re-derived here -- global constraint "no
eligibility bypass"), it picks the endpoint slug to look up in the sweep
matrix, or `None` when it makes no choice (empty eligible set, or the router
finds nothing feasible). No strategy makes a model call; every outcome is a
matrix lookup the runner performs afterwards.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config.schema import PoolCandidate, Settings
from app.errors import NoEligibleCandidate
from app.eval.cases import Case
from app.models import ExecutionPolicyContext
from app.routing.observations import Observation
from app.routing.pricing import price_call
from app.routing.quality import QualityResolver
from app.routing.router import Router
from app.routing.signals import SignalStore

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry
    from app.eval.oracle import MatrixRow


@dataclass(frozen=True)
class StrategyInput:
    case: Case
    ctx: ExecutionPolicyContext
    eligible: tuple[PoolCandidate, ...]
    tokens_after: int          # prompt tokens the sweep saw (first successful
                                # row), else count_tokens of the sanitized prompt
    output_tokens: int         # app/routing/router.py::expected_output_tokens


Strategy = Callable[[StrategyInput], str | None]


def _est_cost(registry: "ModelRegistry", cand: PoolCandidate,
              tokens_after: int, output_tokens: int) -> float:
    """The router's own pre-call cost estimate (app/routing/router.py
    `Router._est_cost`): `price_call` over `registry.price_for`, zero cached
    tokens assumed. Reused here rather than re-derived so `cheapest` and
    `best_published_quality` rank candidates by the same number the real
    router would have used to filter/score them."""
    return price_call(registry.price_for(cand.name), prompt_tokens=tokens_after,
                      completion_tokens=output_tokens)


def cheapest(registry: "ModelRegistry") -> Strategy:
    """Lowest estimated cost among eligible; ties broken by slug."""
    def _cheapest(inp: StrategyInput) -> str | None:
        if not inp.eligible:
            return None
        best = min(inp.eligible,
                   key=lambda c: (_est_cost(registry, c, inp.tokens_after,
                                            inp.output_tokens), c.name))
        return best.name
    return _cheapest


def best_published_quality(registry: "ModelRegistry", settings: Settings) -> Strategy:
    """Highest quality per the router's own `QualityResolver` (registry
    quality, else the tier-default rung) -- reused, not re-derived. Ties
    broken by the same cost estimate as `cheapest`, then by slug."""
    resolver = QualityResolver(settings, registry=registry)

    def _best_published_quality(inp: StrategyInput) -> str | None:
        if not inp.eligible:
            return None
        def key(c: PoolCandidate):
            q = resolver.resolve(c.name).score
            return (-q, _est_cost(registry, c, inp.tokens_after, inp.output_tokens), c.name)
        return min(inp.eligible, key=key).name
    return _best_published_quality


def fixed(slug: str, fallback: Strategy) -> Strategy:
    """`slug` when it is eligible for the case, else `fallback(inp)` -- the
    runner is the one that notices and records `fallback_used` by comparing
    the returned slug against `slug`."""
    def _fixed(inp: StrategyInput) -> str | None:
        if any(c.name == slug for c in inp.eligible):
            return slug
        return fallback(inp)
    return _fixed


def router(settings: Settings, registry: "ModelRegistry",
          signals: SignalStore) -> Strategy:
    """The real `Router.select`, exactly as the serving path calls it: same
    complexity expression (`app/pipeline.py:905,1210` -- `min(1.0,
    tokens_after / 2000)`), same `expected_output_tokens`-derived
    `output_tokens`. `NoEligibleCandidate` (e.g. every eligible candidate over
    budget after the tier target is fixed) maps to `None`, not an exception --
    a strategy that makes no choice is a valid outcome the oracle already
    handles (`Choice.endpoint = None`)."""
    rt = Router(settings, registry=registry, signals=signals)

    def _router(inp: StrategyInput) -> str | None:
        complexity = min(1.0, inp.tokens_after / 2000)
        try:
            decision = rt.select(inp.ctx, inp.tokens_after, complexity,
                                 output_tokens=inp.output_tokens)
        except NoEligibleCandidate:
            return None
        return decision.chosen.name
    return _router


def router_variant(base_settings: Settings, weights: dict[str, float],
                   registry: "ModelRegistry", signals: SignalStore) -> Strategy:
    """`router`, but scored with an alternative `routing.weights` mapping
    (the instrument for backlog B45's `router_weights:<name>`). Deep-copies
    `base_settings` -- weights are read live off `Settings` (see
    `interfaces.md`), so a variant needs its own `Settings` object, never a
    mutation of the one other strategies share."""
    settings = copy.deepcopy(base_settings)
    settings.routing.weights = weights
    return router(settings, registry, signals)


def warm_signals(settings: Settings, rows: Iterable["MatrixRow"],
                 wall_offset: float = 0.0) -> SignalStore:
    """Build a `SignalStore` and replay `rows` (sweep matrix rows, in sweep
    order) into it via `SignalStore.replay` -- never direct `observe_*` calls,
    so the same row-shape-validation and ascending-`ts` discipline that
    governs the real boot-seed path governs this one.

    One `Observation` per row: `ok` (no `error`) contributes a success with
    its `latency_ms`; `error` (any non-None `error`) contributes a failure.
    Never streamed -- the harness's simulated/live backends never stream
    (spec §5.1/§5.2).

    `Router.select` has no `now` override, so it always reads signals against
    the REAL wall clock (`now=None`). For a replayed observation to read as
    `measured` rather than decayed back to neutral / gone stale, its
    reconstructed monotonic time must land close to whenever the caller next
    evaluates a router strategy against this store -- so rows are anchored to
    THIS process's `time.monotonic()` right now, spaced 1 s apart ascending,
    the last row landing at (`now` - `wall_offset`). Call this immediately
    before evaluating `router(...)`/`router_variant(...)` against the
    returned store, the way the runner's sweep-then-score sequencing already
    does; `wall_offset` (default 0.0) lets a caller push the whole batch
    further into the past (e.g. to exercise staleness) without changing row
    order or spacing.
    """
    store = SignalStore(settings.routing.signals)
    materialized = list(rows)
    n = len(materialized)
    mono_now = time.monotonic()
    observations = [
        Observation(
            ts=(mono_now - wall_offset - (n - 1 - i)) + store.wall_offset,
            model=row.endpoint,
            outcome="error" if row.error is not None else "ok",
            latency_ms=None if row.error is not None else row.latency_ms,
            streamed=False,
            ttft_ms=None,
        )
        for i, row in enumerate(materialized)
    ]
    store.replay(observations)
    return store
