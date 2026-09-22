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

from app.config.schema import PoolCandidate, RoutingCfg, Settings, SignalsCfg
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


# The four scoring terms `DefaultRoutingStrategy.rank` reads by literal key
# (app/routing/strategy.py). Eval-local: the serving path's own required-key
# behavior for `routing.yaml` stays entirely in the existing boot-time
# `CONFIG_LINT: WEIGHT_MISSING` rule (app/config/lint.py) -- this harness
# never changes what a real deployment's boot accepts or rejects.
_REQUIRED_WEIGHT_KEYS = ("quality", "cost", "latency", "availability")


def router_variant(base_settings: Settings, weights: dict[str, float],
                   registry: "ModelRegistry", signals: SignalStore) -> Strategy:
    """`router`, but scored with an alternative `routing.weights` mapping
    (the instrument for backlog B45's `router_weights:<name>`). Deep-copies
    `base_settings` -- weights are read live off `Settings` (see
    `interfaces.md`), so a variant needs its own `Settings` object, never a
    mutation of the one other strategies share.

    `weights` is copied (`dict(weights)`, never aliased into the variant's
    `Settings`) and run through the REAL `RoutingCfg` validation --
    `RoutingCfg.model_validate`, not a bare attribute assignment, so
    `_rename_capacity_weight` (app/config/schema.py) actually runs: the
    legacy `capacity` -> `availability` alias is honored exactly as it is for
    a real `routing.yaml` load.

    A `weights` mapping still missing one of `_REQUIRED_WEIGHT_KEYS` after
    that rename raises a clear `ValueError` (naming the missing key(s)) right
    here, at variant-construction time -- not a bare `KeyError` the first
    time `DefaultRoutingStrategy.rank` reads it mid-sweep. This check is
    intentionally local to the harness, not added to `RoutingCfg` itself: a
    schema-level required-key validator would run during `load_settings()`
    too, raising a `pydantic.ValidationError` for an incomplete `routing.yaml`
    *before* `app/config/lint.py`'s `enforce()` gets a chance to print its own
    aggregated, named `CONFIG_LINT: WEIGHT_MISSING` violation -- duplicating
    that lint rule and making it unreachable from a real boot. The harness
    must not change serving-path behavior, so the check stays here.
    """
    settings = copy.deepcopy(base_settings)
    settings.routing = RoutingCfg.model_validate(
        {**settings.routing.model_dump(), "weights": dict(weights)})
    missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in settings.routing.weights]
    if missing:
        raise ValueError(
            f"router_variant: weights is missing required key(s) {missing} "
            f"(required: {list(_REQUIRED_WEIGHT_KEYS)})")
    return router(settings, registry, signals)


class _FrozenReadSignalStore(SignalStore):
    """A `SignalStore` whose `now=None` reads always resolve to a FIXED
    monotonic anchor instead of the live clock.

    `Router.select` never overrides `now` -- every read inside
    `DefaultRoutingStrategy.rank` goes through `now=None`
    (`app/routing/strategy.py`). Reading a warm store against the live clock
    makes a replayed signal's value a function of how long it happens to take
    between `warm_signals` building the store and something scoring it --
    e.g. availability's wall-clock decay (`half_life_s`) keeps moving even a
    single `time.sleep()` between two reads, and once real elapsed time
    exceeds `staleness_s` a measured latency silently reverts to
    `tier_default`. Either makes the harness's `--seed`
    byte-identical-`metrics.json` guarantee false: the same seed, run twice,
    could score differently depending on wall-clock timing alone. Freezing
    the read clock at the anchor makes every `now=None` read against this
    store -- now, or an hour from now -- return the exact same value: a pure
    function of the replayed rows, never of when they happen to be read.

    `Router.select` itself is untouched -- this is an eval-only subclass, and
    only `now=None` reads are affected; an explicit `now=` (as
    `tests/routing/test_signal_wiring.py` uses for the live/booted-store
    comparison) still passes straight through.
    """

    def __init__(self, cfg: SignalsCfg, frozen_now: float, **kwargs) -> None:
        super().__init__(cfg, **kwargs)
        self._frozen_now = frozen_now

    def _now(self, now: float | None) -> float:
        return self._frozen_now if now is None else now


def warm_signals(settings: Settings, rows: Iterable["MatrixRow"],
                 wall_offset: float = 0.0) -> SignalStore:
    """Build a `SignalStore` and replay `rows` (sweep matrix rows, in sweep
    order) into it via `SignalStore.replay` -- never direct `observe_*` calls,
    so the same row-shape-validation and ascending-`ts` discipline that
    governs the real boot-seed path governs this one.

    One `Observation` per row: `ok` (no `error`) contributes a success with
    its `latency_ms`; `error` contributes a failure ONLY when
    `row.counts_unhealth is True` -- production only ever calls
    `SignalStore.observe_error` when `counts_as_endpoint_unhealth(exc)` is
    true (`app/gateway/gateway.py`), so an error row that does NOT count as
    endpoint-unhealth (a 4xx that says the request was wrong, or one whose
    root exception the runner couldn't reach -- see
    `app/eval/runner.py::_root_cause`) is skipped entirely: neither a
    success nor a failure observation (spec §6 / B48c). Never streamed --
    the harness's simulated/live backends never stream (spec §5.1/§5.2).

    Returns a `_FrozenReadSignalStore`: `Router.select` has no `now`
    override, so without freezing, reads happen against the REAL wall clock
    and the "warm" effect would depend on exactly when it's scored relative
    to when this function ran (see that class's docstring, and the harness's
    determinism requirement -- byte-identical `metrics.json` for a fixed
    `--seed`). The read clock is frozen at THIS process's `time.monotonic()`
    right now -- call this once, immediately before scoring, and every
    subsequent `now=None` read reproduces exactly what it would have read the
    instant replay finished, however much real time actually elapses before
    (or between) those reads.

    Rows are anchored to that SAME `time.monotonic()` reading, spaced 1 s
    apart ascending, the last row landing at (`now` - `wall_offset`) -- i.e.
    `wall_offset` (default 0.0) is the age, in seconds, of the most recent
    row AS SEEN FROM the frozen read clock (which itself does not move with
    `wall_offset`). A `wall_offset` of 0 is "replay just finished" (every row
    reads as fresh as `SignalStore.replay` can make it); a `wall_offset` past
    `staleness_s`/several `half_life_s` periods deliberately makes a "warm"
    store's signals decay back to indistinguishable from a freshly
    -constructed, never-replayed one -- without changing row order or
    spacing. (An earlier draft anchored the frozen read clock to `now -
    wall_offset` too, which made `wall_offset` cancel out of the reads
    entirely -- the read and the rows always aged together, so staleness
    could never actually be exercised. Keeping the read anchor fixed at
    `now` while only the rows move is what makes `wall_offset` a genuine
    staleness knob.)

    The 1 s synthetic spacing between rows is a modeling simplification worth
    knowing about: in a long sweep (many rows), the FIRST row replayed is,
    by construction, `len(rows) - 1` synthetic seconds "older" than the
    last -- so an early error in a long sweep decays (partially recovers
    availability) measurably more than the same error placed near the end,
    purely as an artifact of sweep position, not of any real elapsed time.
    Short sweeps (a handful of rows per model, the common case) keep this
    negligible; a very large per-model row count would not.
    """
    materialized = list(rows)
    n = len(materialized)
    mono_now = time.monotonic()
    # The frozen read anchor is fixed at `mono_now` regardless of
    # `wall_offset` -- only the replayed rows' reconstructed mono times move
    # with it, so `wall_offset` is the read/write age GAP, not a shift that
    # cancels itself out (see the docstring above).
    store = _FrozenReadSignalStore(settings.routing.signals, frozen_now=mono_now)
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
        if row.error is None or row.counts_unhealth is True
    ]
    store.replay(observations)
    return store
