"""Baseline and router selection strategies for FeasibleRouteBench (spec §5
step 3; failover-aware attempt orders and signal arms, B48 spec §5-§6).

Two shapes live here:

- **`Strategy`** = `Callable[[StrategyInput], str | None]` -- the P2b-1
  single choice (`cheapest`, `best_published_quality`, `fixed`, `router`,
  `router_variant`). The runner no longer scores these; they stay as the
  single-choice reference the attempt forms' first element must agree with.
- **`AttemptStrategy`** = `Callable[[StrategyInput, Callable[[str], bool]],
  tuple[str, ...]]` -- the failover-aware form the runner scores. The second
  argument, `failed(slug) -> bool`, is the runner's matrix lookup ("did this
  endpoint's sweep call on THIS case fail?"). The strategy returns the
  attempts it would have made, in order, stopping at the first attempt that
  does not fail or at `max_attempts` (`attempt_cap(base_settings)` = `1 +`
  the DEPLOYMENT's `max_failover_attempts`, read before `harness_settings`
  zeroes it); `()` is "no choice". This mirrors `Gateway.run`'s failover loop.

Given a case's already-computed eligible set (never re-derived here -- global
constraint "no eligibility bypass"), no strategy makes a model call; every
outcome is a matrix lookup the runner performs.

Router signal arms (B48 spec §6): `router_cold` (an empty store),
`OnlineArm` (`router_online`: only this arm's own attempts on cases `< k`,
applied after each case is scored) and `warm_prior_signals`
(`router_warm_prior`: every matrix row of cases `< k`). Both non-cold arms
read on one synthetic case clock -- case `k` is scored at `t_k = k *
seconds_per_case` (spec §6, amended). No arm replays the case being scored;
`warm_signals` stays as a generic replay primitive only.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config.schema import PoolCandidate, RoutingCfg, Settings, SignalsCfg
from app.errors import NoEligibleCandidate
from app.eval.cases import Case
from app.eval.endpoints import deployment_failover_cap
from app.gateway.breaker import CircuitBreaker
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
AttemptStrategy = Callable[[StrategyInput, Callable[[str], bool]], tuple[str, ...]]


def attempt_cap(base_settings: Settings) -> int:
    """Attempts per case: `1 + deployment_failover_cap(base_settings)` --
    the gateway's own bound (`failover_attempts > max_failover_attempts`
    exhausts). `base_settings` is the DEPLOYMENT's settings, never the
    harness's (which sets `max_failover_attempts=0` so each sweep call is
    single-shot)."""
    return 1 + deployment_failover_cap(base_settings)


def take_attempts(order: Iterable[str], failed: Callable[[str], bool],
                  max_attempts: int) -> tuple[str, ...]:
    """Walk `order`, attempting each slug until one does not fail or
    `max_attempts` attempts have been made. The baselines' failover: their
    own ranking IS their retry order."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    chain: list[str] = []
    for slug in order:
        if len(chain) >= max_attempts:
            break
        chain.append(slug)
        if not failed(slug):
            break
    return tuple(chain)


def _est_cost(registry: "ModelRegistry", cand: PoolCandidate,
              tokens_after: int, output_tokens: int) -> float:
    """The router's own pre-call cost estimate (app/routing/router.py
    `Router._est_cost`): `price_call` over `registry.price_for`, zero cached
    tokens assumed. Reused here rather than re-derived so `cheapest` and
    `best_published_quality` rank candidates by the same number the real
    router would have used to filter/score them."""
    return price_call(registry.price_for(cand.name), prompt_tokens=tokens_after,
                      completion_tokens=output_tokens)


def _cheapest_order(registry: "ModelRegistry", inp: StrategyInput) -> list[str]:
    """Every eligible slug, lowest estimated cost first; ties by slug."""
    ranked = sorted(inp.eligible,
                    key=lambda c: (_est_cost(registry, c, inp.tokens_after,
                                             inp.output_tokens), c.name))
    return [c.name for c in ranked]


def _best_quality_order(registry: "ModelRegistry", resolver: QualityResolver,
                        inp: StrategyInput) -> list[str]:
    """Every eligible slug, highest resolved quality first; ties by the
    `cheapest` cost estimate, then slug."""
    def key(c: PoolCandidate):
        q = resolver.resolve(c.name).score
        return (-q, _est_cost(registry, c, inp.tokens_after, inp.output_tokens), c.name)
    return [c.name for c in sorted(inp.eligible, key=key)]


def cheapest(registry: "ModelRegistry") -> Strategy:
    """Lowest estimated cost among eligible; ties broken by slug."""
    def _cheapest(inp: StrategyInput) -> str | None:
        order = _cheapest_order(registry, inp)
        return order[0] if order else None
    return _cheapest


def cheapest_attempts(registry: "ModelRegistry", max_attempts: int) -> AttemptStrategy:
    """`cheapest` with failover: next cheapest after each failed attempt."""
    def _cheapest_attempts(inp: StrategyInput, failed: Callable[[str], bool]):
        return take_attempts(_cheapest_order(registry, inp), failed, max_attempts)
    return _cheapest_attempts


def best_published_quality(registry: "ModelRegistry", settings: Settings) -> Strategy:
    """Highest quality per the router's own `QualityResolver` (registry
    quality, else the tier-default rung) -- reused, not re-derived. Ties
    broken by the same cost estimate as `cheapest`, then by slug."""
    resolver = QualityResolver(settings, registry=registry)

    def _best_published_quality(inp: StrategyInput) -> str | None:
        order = _best_quality_order(registry, resolver, inp)
        return order[0] if order else None
    return _best_published_quality


def best_published_quality_attempts(registry: "ModelRegistry", settings: Settings,
                                    max_attempts: int) -> AttemptStrategy:
    """`best_published_quality` with failover: next best after each failure."""
    resolver = QualityResolver(settings, registry=registry)

    def _best_attempts(inp: StrategyInput, failed: Callable[[str], bool]):
        return take_attempts(_best_quality_order(registry, resolver, inp),
                             failed, max_attempts)
    return _best_attempts


def fixed(slug: str, fallback: Strategy) -> Strategy:
    """`slug` when it is eligible for the case, else `fallback(inp)` -- the
    runner is the one that notices and records `fallback_used` by comparing
    the returned slug against `slug`."""
    def _fixed(inp: StrategyInput) -> str | None:
        if any(c.name == slug for c in inp.eligible):
            return slug
        return fallback(inp)
    return _fixed


def fixed_attempts(slug: str, registry: "ModelRegistry",
                   max_attempts: int) -> AttemptStrategy:
    """`slug` first when it is eligible, then `cheapest`'s order over the
    rest (B48 spec §5); when `slug` is not eligible, `cheapest`'s order
    alone (the runner records `fallback_used` from `attempts[0] != slug`)."""
    def _fixed_attempts(inp: StrategyInput, failed: Callable[[str], bool]):
        rest = [s for s in _cheapest_order(registry, inp) if s != slug]
        pinned = [slug] if any(c.name == slug for c in inp.eligible) else []
        return take_attempts(pinned + rest, failed, max_attempts)
    return _fixed_attempts


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


def _router_chain(rt: Router, inp: StrategyInput, failed: Callable[[str], bool],
                  max_attempts: int,
                  open_circuits: Callable[[], frozenset[str]] | None = None
                  ) -> tuple[str, ...]:
    """`Gateway.run`'s failover loop over the REAL `Router.select`: re-select
    with every already-failed slug in `exclude` until an attempt does not
    fail, `max_attempts` attempts are made, or `NoEligibleCandidate` (every
    reachable candidate excluded) ends the chain. Every attempt before the
    last failed, so `exclude` is exactly the chain so far.

    `open_circuits`, when given, is called fresh on EVERY hop (never cached
    across hops) and its result passed straight through as
    `Router.select(open_circuits=...)` -- the step-7 breaker's open set as of
    right now, same as the gateway's own failover loop reads it. `None`
    (every strategy except `OnlineArm` with a breaker) reproduces the
    pre-breaker chain exactly: `Router.select`'s own `open_circuits` default
    is an empty `frozenset`."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    complexity = min(1.0, inp.tokens_after / 2000)
    chain: list[str] = []
    while len(chain) < max_attempts:
        try:
            decision = rt.select(inp.ctx, inp.tokens_after, complexity,
                                 exclude=frozenset(chain),
                                 output_tokens=inp.output_tokens,
                                 open_circuits=(open_circuits() if open_circuits
                                               is not None else frozenset()))
        except NoEligibleCandidate:
            break
        slug = decision.chosen.name
        chain.append(slug)
        if not failed(slug):
            break
    return tuple(chain)


def router_attempts(settings: Settings, registry: "ModelRegistry",
                    signals: SignalStore, max_attempts: int) -> AttemptStrategy:
    """`router` with the gateway's failover (see `_router_chain`). `signals`
    is read, never written -- the cold arm passes an empty store; the online
    arm is `OnlineArm`."""
    rt = Router(settings, registry=registry, signals=signals)

    def _router_attempts(inp: StrategyInput, failed: Callable[[str], bool]):
        return _router_chain(rt, inp, failed, max_attempts)
    return _router_attempts


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
    return router(variant_settings(base_settings, weights), registry, signals)


def variant_settings(base_settings: Settings, weights: dict[str, float]) -> Settings:
    """A deep copy of `base_settings` with `routing.weights` replaced by a
    copy of `weights`, validated as `router_variant` documents."""
    settings = copy.deepcopy(base_settings)
    settings.routing = RoutingCfg.model_validate(
        {**settings.routing.model_dump(), "weights": dict(weights)})
    missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in settings.routing.weights]
    if missing:
        raise ValueError(
            f"router_variant: weights is missing required key(s) {missing} "
            f"(required: {list(_REQUIRED_WEIGHT_KEYS)})")
    return settings


def router_variant_attempts(base_settings: Settings, weights: dict[str, float],
                            registry: "ModelRegistry", signals: SignalStore,
                            max_attempts: int) -> AttemptStrategy:
    """`router_attempts` scored with alternative weights (`variant_settings`)."""
    return router_attempts(variant_settings(base_settings, weights), registry,
                           signals, max_attempts)


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

    @property
    def frozen_now(self) -> float:
        return self._frozen_now

    def advance_read_clock(self, to: float) -> None:
        """Move the frozen read anchor forward to `to` (never backwards --
        the monotonic clock this stands in for cannot run back)."""
        if to < self._frozen_now:
            raise ValueError(
                f"read clock cannot move backwards ({self._frozen_now} -> {to})")
        self._frozen_now = to


def warm_signals(settings: Settings, rows: Iterable["MatrixRow"],
                 wall_offset: float = 0.0) -> SignalStore:
    """Build a `SignalStore` and replay `rows` (sweep matrix rows, in sweep
    order) into it via `SignalStore.replay` -- never direct `observe_*` calls,
    so the same row-shape-validation and ascending-`ts` discipline that
    governs the real boot-seed path governs this one.

    One `Observation` per row: `ok` (no `error`) contributes a success with
    its `latency_ms`; `error` contributes a failure ONLY when
    `row.counts_unhealth is True`, matching production's own
    `SignalStore.observe_error` gate (`app/gateway/gateway.py`) -- any other
    error row (`counts_unhealth` `False` or `None`; see
    `app/eval/runner.py::_backend_exception`) is skipped entirely: neither a
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
    # Filter FIRST, then assign timestamps: a skipped row must not consume a
    # 1 s slot, or the last APPLIED observation would sit older than
    # `wall_offset` says (B48 Task 2 review note).
    applied = [row for row in rows if _counts_as_observation(row)]
    n = len(applied)
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
        for i, row in enumerate(applied)
    ]
    store.replay(observations)
    return store


def _counts_as_observation(row: "MatrixRow") -> bool:
    """A success, or an error production's `counts_as_endpoint_unhealth`
    would have counted (`counts_unhealth is True`); anything else -- a 4xx,
    or a failure with no endpoint verdict -- is no observation at all."""
    return row.error is None or row.counts_unhealth is True




# Synthetic case clock shared by `warm_prior_signals` and `OnlineArm` (B48
# spec §6, amended 2026-09-22): case `k` happens at `t_k = k *
# seconds_per_case`; its observations sit at `t_k + j * _SUBSTEP_S`, `j`
# counting the observations APPLIED within that case, and case `k` is scored
# by reading at `t_k` -- so every observation applied before it is `< t_k`.
# Decay therefore depends on case count and `seconds_per_case`, never wall
# time -- and no live clock is read, so a seeded run is byte-identical.
_SUBSTEP_S = 0.001
DEFAULT_SECONDS_PER_CASE = 30.0


def check_seconds_per_case(seconds_per_case: float) -> float:
    """`seconds_per_case` as a float, or `ValueError` unless it is `> 0`."""
    value = float(seconds_per_case)
    if not value > 0:
        raise ValueError(f"seconds_per_case must be > 0, got {seconds_per_case!r}")
    return value


def min_seconds_per_case(n_substeps: int) -> float:
    """The largest `seconds_per_case` value at which `_case_time` is
    guaranteed to refuse a case needing `n_substeps` applied observations
    (final review F3): `_case_time` refuses sub-step `j` once `j *
    _SUBSTEP_S >= seconds_per_case`, so a case that may apply up to
    `n_substeps` observations (one per endpoint swept, or one per attempt an
    online arm makes) needs `seconds_per_case > n_substeps * _SUBSTEP_S`. The
    runner calls this once `len(specs)` and `max_attempts` are both known, so
    a too-small `--seconds-per-case` fails fast at load time (exit 1) instead
    of mid-run."""
    return _SUBSTEP_S * n_substeps


def case_read_time(case_index: int, seconds_per_case: float) -> float:
    """`t_k`: when case `k` is scored on the synthetic case clock."""
    return case_index * seconds_per_case


def _case_time(case_index: int, j: int, seconds_per_case: float) -> float:
    """The `j`-th applied observation of case `case_index`: `t_k + j ms`.
    Refuses a sub-step that would reach the next case's `t_{k+1}`."""
    offset = j * _SUBSTEP_S
    if offset >= seconds_per_case:
        raise ValueError(
            f"observation {j} of case {case_index} would reach the next case at "
            f"seconds_per_case={seconds_per_case} ({_SUBSTEP_S * 1000:g} ms sub-steps)")
    return case_read_time(case_index, seconds_per_case) + offset


class LeakageError(RuntimeError):
    """A signal arm was about to score case `k` holding an observation from
    case `k` or later (B48 global constraint "no leakage")."""


def warm_prior_signals(settings: Settings, rows_before_case: Iterable["MatrixRow"],
                       *, case_index: Mapping[str, int], scoring_case: int,
                       seconds_per_case: float) -> _FrozenReadSignalStore:
    """`router_warm_prior`'s store (B48 spec §6): every matrix row of cases
    `< k` -- all eligible endpoints, not just the ones any strategy tried --
    a full-information reference (not an upper bound). The runner passes only
    prior rows.

    Rows are replayed through `SignalStore.replay` (the same path as
    `warm_signals`), counting an error only when `counts_unhealth` is true,
    on the synthetic case clock: a row of case `i` sits at `_case_time(i, j,
    seconds_per_case)`, `j` = its position among that case's APPLIED rows.
    The read clock is frozen at `t_k = scoring_case * seconds_per_case`
    (spec §6, amended) -- production reads at "now", and time passes between
    requests even when nothing is observed.

    `case_index` maps `case_id` -> suite position and `scoring_case` is the
    case about to be scored; both are required, so the leak guard cannot be
    skipped: any row at or after `scoring_case` raises `LeakageError`
    instead of being replayed."""
    seconds_per_case = check_seconds_per_case(seconds_per_case)
    per_case: dict[int, int] = {}
    observations: list[Observation] = []
    last_t = float("-inf")
    for row in rows_before_case:
        idx = case_index[row.case_id]
        if idx >= scoring_case:
            raise LeakageError(
                f"row {row.case_id}/{row.endpoint} is case {idx}, not before "
                f"scoring case {scoring_case}")
        if not _counts_as_observation(row):
            continue
        if row.error is None and row.latency_ms is None:
            raise ValueError(
                f"success row {row.case_id}/{row.endpoint} has no latency_ms")
        j = per_case.get(idx, 0)
        per_case[idx] = j + 1
        t = _case_time(idx, j, seconds_per_case)
        if t < last_t:
            raise ValueError(
                f"rows are not in case order: {row.case_id} (case {idx}) after t={last_t}")
        last_t = t
        observations.append(Observation(
            ts=t, model=row.endpoint,
            outcome="error" if row.error is not None else "ok",
            latency_ms=None if row.error is not None else row.latency_ms,
            streamed=False, ttft_ms=None))
    # wall_offset=0.0: the synthetic clock IS the log clock, so replay's
    # `ts - wall_offset` is exact (no per-process float rounding).
    store = _FrozenReadSignalStore(
        settings.routing.signals,
        frozen_now=case_read_time(scoring_case, seconds_per_case), wall_offset=0.0)
    store.replay(observations)
    return store


@dataclass(frozen=True)
class AppliedObservation:
    """One observation `OnlineArm` applied to its store."""

    case_index: int
    endpoint: str
    outcome: str      # "ok" | "error"
    t: float          # synthetic case-clock time (`_case_time`)


class OnlineArm:
    """`router_online` (B48 spec §6) -- the reported arm: what production
    would learn. Its store holds ONLY the attempts this arm itself made on
    cases `0..k-1` (failovers included), applied in attempt order through
    the real `observe_success` / `observe_error`, an error counted only when
    `counts_unhealth` is true (the gateway's `counts_as_endpoint_unhealth`
    gate). No other arm's attempts, no unattempted endpoint's row.

    Protocol per case `k`: `choose(inp, failed, case_index=k)` FIRST, then
    `observe(k, attempts, rows_by_endpoint)` with the chain `choose`
    returned -- so case `k` is always scored before anything from it is
    known. `case_index` is required: `choose` raises `LeakageError` when
    case `k` has already been observed (`k <= ` the last observed case),
    and `observe` rejects a case index that does not strictly increase.

    Reads: `choose` first advances the frozen read clock to `t_k = k *
    seconds_per_case` (spec §6, amended) and scores there; every applied
    observation is `< t_k`.

    Difference from production, stated: the gateway calls `observe_error`
    mid-request, so within one request the failed model's signal has
    already moved when the next hop is selected. Here a case's observations
    land after its whole chain is chosen. The failed model is in `exclude`
    for every later hop of that chain either way, so its own signal cannot
    change that chain's remaining picks.

    The step-7 `CircuitBreaker` (Task 2), when this arm owns one, is fed the
    exact same way: ONLY this arm's own prior attempts, on the same case
    clock, applied from `observe` after a case is scored -- never from a
    matrix row this arm did not itself attempt. It is therefore covered by
    the same leak guard as the signal store: nothing about case `k` can
    reach the breaker before `choose(..., case_index=k)` has already run."""

    def __init__(self, settings: Settings, registry: "ModelRegistry",
                 max_attempts: int, weights: dict[str, float] | None = None, *,
                 seconds_per_case: float, breaker: bool | None = None) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        scored = settings if weights is None else variant_settings(settings, weights)
        self._max_attempts = max_attempts
        self._seconds_per_case = check_seconds_per_case(seconds_per_case)
        self._store = _FrozenReadSignalStore(scored.routing.signals, frozen_now=0.0,
                                             wall_offset=0.0)
        self._router = Router(scored, registry=registry, signals=self._store)
        self._last_case: int | None = None
        self._applied: list[AppliedObservation] = []
        use_breaker = (settings.routing.breaker.enabled if breaker is None else breaker)
        self._breaker = CircuitBreaker(scored.routing.breaker) if use_breaker else None

    @property
    def signals(self) -> SignalStore:
        return self._store

    @property
    def applied(self) -> tuple[AppliedObservation, ...]:
        return tuple(self._applied)

    @property
    def read_clock(self) -> float:
        return self._store.frozen_now

    @property
    def seconds_per_case(self) -> float:
        return self._seconds_per_case

    def choose(self, inp: StrategyInput, failed: Callable[[str], bool], *,
               case_index: int) -> tuple[str, ...]:
        if self._last_case is not None and case_index <= self._last_case:
            raise LeakageError(
                f"scoring case {case_index} after case {self._last_case} was "
                "already observed")
        t = case_read_time(case_index, self._seconds_per_case)
        self._store.advance_read_clock(t)
        open_circuits = (lambda: self._breaker.open_set(now=t)) if self._breaker else None
        return _router_chain(self._router, inp, failed, self._max_attempts,
                             open_circuits=open_circuits)

    def observe(self, case_index: int, attempts: tuple[str, ...],
                rows_by_endpoint: Mapping[str, "MatrixRow"]) -> None:
        if self._last_case is not None and case_index <= self._last_case:
            raise ValueError(
                f"observe(case {case_index}) after case {self._last_case}: "
                "cases must be observed once each, in order")
        if len(attempts) > self._max_attempts:
            raise ValueError(
                f"case {case_index}: {len(attempts)} attempts exceed "
                f"max_attempts={self._max_attempts}")
        if len(set(attempts)) != len(attempts):
            raise ValueError(f"case {case_index}: repeated attempt in {attempts}")
        rows: list["MatrixRow"] = []
        for pos, slug in enumerate(attempts):   # validate the whole chain first
            row = rows_by_endpoint.get(slug)
            if row is None:
                raise ValueError(f"no matrix row for attempt {slug!r} in case {case_index}")
            if row.error is None:
                if pos != len(attempts) - 1:
                    raise ValueError(
                        f"attempt {slug!r} served case {case_index} but is not "
                        "the last attempt of the chain")
                if row.latency_ms is None:
                    raise ValueError(
                        f"success row {row.case_id}/{slug} has no latency_ms")
            rows.append(row)
        self._last_case = case_index
        j = 0
        for slug, row in zip(attempts, rows):
            if row.error is None:
                t = _case_time(case_index, j, self._seconds_per_case)
                self._store.observe_success(slug, float(row.latency_ms), now=t)
                if self._breaker is not None:
                    self._breaker.record_success(slug, now=t)
            elif row.counts_unhealth is True:
                t = _case_time(case_index, j, self._seconds_per_case)
                self._store.observe_error(slug, now=t)
                if self._breaker is not None:
                    self._breaker.record_failure(slug, now=t)
            else:
                continue
            j += 1
            self._applied.append(AppliedObservation(
                case_index, slug, "ok" if row.error is None else "error", t))
            self._store.advance_read_clock(t)
