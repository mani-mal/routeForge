"""Tests for `app.eval.strategies` (Task 5, FeasibleRouteBench).

Covers the five baselines/constructors from spec §5 step 3: `cheapest`,
`best_published_quality`, `fixed`, `router`/`router_variant`, and
`warm_signals`. Every registry is built with Task 2's `build_registry` (a
scratch, in-memory registry -- never a deployment database) and every ctx
comes from the real `Gate.evaluate`, so eligibility is never re-derived by
hand. See
docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md §5 step 3.
"""
from __future__ import annotations

import asyncio
import time

import pydantic
import pytest
import yaml

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.eval.cases import Case
from app.eval.endpoints import build_registry, harness_settings, load_endpoints
from app.eval.graders import GraderSpec
from app.eval.oracle import MatrixRow
from app.eval.strategies import (
    StrategyInput,
    best_published_quality,
    cheapest,
    fixed,
    router,
    router_variant,
    warm_signals,
)
from app.gateway.backends.base import count_tokens
from app.models import ChatRequest, Message
from app.policy.detectors import make_detector
from app.policy.feasible import feasible_set
from app.policy.gate import Gate
from app.routing.router import Router, expected_output_tokens
from app.routing.signals import SignalStore

BASE_SETTINGS = load_settings()
TIERS = BASE_SETTINGS.routing.tiers


def _write(tmp_path, doc: dict) -> str:
    path = tmp_path / "endpoints.yaml"
    path.write_text(yaml.safe_dump(doc))
    return str(path)


def _entry(slug: str, *, tier="cheap", provider="onprem", price_input=0.08,
          price_output=0.08, quality=None) -> dict:
    return {
        "slug": slug,
        "provider": provider,
        "tier": tier,
        "price": {"input": price_input, "output": price_output},
        "quality": quality,
        "simulated": {
            "quality": {"default": 0.9},
            "latency_ms": {"mean": 100, "jitter": 0},
        },
    }


def _case(case_id="case-1", content="Is the claim approved?") -> Case:
    return Case(
        id=case_id,
        messages=[{"role": "user", "content": content}],
        grader=GraderSpec(kind="exact", expected="approved"),
    )


def _build(tmp_path, entries: list[dict], residency="any"):
    """Build `(settings, registry, case, ctx, eligible)` for `entries` -- a
    real `Gate.evaluate` + `feasible_set`, exactly the pipeline the sweep
    uses ahead of any strategy call.

    Builds and disposes the scratch db inside ONE `asyncio.run` call:
    `ModelRegistry.refresh` caches every row in memory (see
    `app/apps/registry.py::_onboarded`), so nothing after `build_registry`
    needs a live connection, and disposing from a second, later `asyncio.run`
    call would tear down the StaticPool connection from a different
    (already-closed) event loop than the one it was opened on."""
    path = _write(tmp_path, {"endpoints": entries})
    specs, _ = load_endpoints(path, live=False, tiers=TIERS)
    settings = harness_settings(BASE_SETTINGS, specs)

    async def _run():
        registry, db = await build_registry(settings, specs)
        await db.dispose()
        return registry

    registry = asyncio.run(_run())

    detector = make_detector("regex")
    gate = Gate(settings, detector, registry=registry)
    case = _case()
    policy = AppPolicyView(residency=residency)
    ctx, _reps, _eff = gate.evaluate(
        "req-1", "tenant-1", ChatRequest(model="auto", messages=[
            Message(role="user", content=case.messages[0]["content"])]),
        app_policy=policy)
    eligible = feasible_set(settings, registry, ctx, tenant_id=ctx.tenant_id).eligible
    return settings, registry, case, ctx, eligible


def _strategy_input(case, ctx, eligible) -> StrategyInput:
    tokens_after = count_tokens(case.messages[0]["content"])
    output_tokens = expected_output_tokens(BASE_SETTINGS, None)
    return StrategyInput(case=case, ctx=ctx, eligible=tuple(eligible),
                         tokens_after=tokens_after, output_tokens=output_tokens)


# --------------------------------------------------------------------------
# cheapest
# --------------------------------------------------------------------------

class TestCheapest:
    def test_picks_lowest_estimated_cost(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("cheap-model", price_input=0.01, price_output=0.01),
            _entry("pricey-model", price_input=1.0, price_output=1.0),
        ])
        inp = _strategy_input(case, ctx, eligible)
        assert cheapest(registry)(inp) == "cheap-model"

    def test_ties_broken_by_slug(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("z-model", price_input=0.02, price_output=0.02),
            _entry("a-model", price_input=0.02, price_output=0.02),
        ])
        inp = _strategy_input(case, ctx, eligible)
        assert cheapest(registry)(inp) == "a-model"

    def test_empty_eligible_returns_none(self, tmp_path):
        settings, registry, case, ctx, _eligible = _build(tmp_path, [
            _entry("cheap-model"),
        ])
        inp = StrategyInput(case=case, ctx=ctx, eligible=(),
                            tokens_after=5, output_tokens=64)
        assert cheapest(registry)(inp) is None


# --------------------------------------------------------------------------
# best_published_quality
# --------------------------------------------------------------------------

class TestBestPublishedQuality:
    def test_picks_highest_published_quality(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("low-q", quality=0.4),
            _entry("high-q", quality=0.95),
        ])
        inp = _strategy_input(case, ctx, eligible)
        assert best_published_quality(registry, settings)(inp) == "high-q"

    def test_unset_quality_falls_back_to_tier_default_then_cost(self, tmp_path):
        # Both unset -> both resolve to the same cheap tier_default (0.5);
        # tie broken by cost estimate, not published quality.
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("pricier", price_input=1.0, price_output=1.0),
            _entry("cheaper", price_input=0.01, price_output=0.01),
        ])
        inp = _strategy_input(case, ctx, eligible)
        assert best_published_quality(registry, settings)(inp) == "cheaper"

    def test_empty_eligible_returns_none(self, tmp_path):
        settings, registry, case, ctx, _eligible = _build(tmp_path, [
            _entry("model-a"),
        ])
        inp = StrategyInput(case=case, ctx=ctx, eligible=(),
                            tokens_after=5, output_tokens=64)
        assert best_published_quality(registry, settings)(inp) is None


# --------------------------------------------------------------------------
# fixed
# --------------------------------------------------------------------------

class TestFixed:
    def test_returns_slug_when_eligible(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("cheap-model", price_input=0.01, price_output=0.01),
            _entry("pinned-model", price_input=1.0, price_output=1.0),
        ])
        inp = _strategy_input(case, ctx, eligible)
        calls = []
        def fallback(_inp):
            calls.append(_inp)
            return "should-not-be-used"
        strat = fixed("pinned-model", fallback)
        assert strat(inp) == "pinned-model"
        assert calls == []

    def test_falls_back_when_slug_not_eligible(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("cheap-model", price_input=0.01, price_output=0.01),
        ])
        inp = _strategy_input(case, ctx, eligible)
        fallback_strat = cheapest(registry)
        strat = fixed("not-onboarded", fallback_strat)
        assert strat(inp) == "cheap-model"


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

class TestRouter:
    def test_matches_direct_router_select_call(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("cheap-model", price_input=0.01, price_output=0.01),
            _entry("other-model", price_input=0.02, price_output=0.02),
        ])
        inp = _strategy_input(case, ctx, eligible)
        signals = SignalStore(settings.routing.signals)

        got = router(settings, registry, signals)(inp)

        complexity = min(1.0, inp.tokens_after / 2000)
        direct = Router(settings, registry=registry, signals=signals).select(
            ctx, inp.tokens_after, complexity, output_tokens=inp.output_tokens)
        assert got == direct.chosen.name

    def test_no_eligible_candidate_maps_to_none(self, tmp_path):
        # onprem_only ctx, but the only onboarded endpoint is azure -- the
        # real Router.select raises NoEligibleCandidate; the strategy must
        # swallow it into None, not propagate.
        settings, registry, case, ctx, eligible = _build(
            tmp_path, [_entry("azure-model", provider="azure")],
            residency="onprem_only")
        inp = _strategy_input(case, ctx, eligible)
        signals = SignalStore(settings.routing.signals)
        assert router(settings, registry, signals)(inp) is None
        with pytest.raises(NoEligibleCandidate):
            Router(settings, registry=registry, signals=signals).select(
                ctx, inp.tokens_after, 0.0, output_tokens=inp.output_tokens)


# --------------------------------------------------------------------------
# router_variant + warm_signals -- proves variants and warm state actually
# reach the router (spec §5 step 3 / task-5 brief Step 1).
# --------------------------------------------------------------------------

_HOT_WEIGHTS = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 5.0}
_COLD_WEIGHTS = {**_HOT_WEIGHTS, "availability": 0.0}


def _slow_fast_setup(tmp_path):
    """Two same-tier, same-price candidates differing only in measured
    latency/availability once warmed: `slow-available` (5 successes,
    `latency_ms=900`) and `fast-flaky` (5 errors). `min_samples` defaults to
    5, so exactly 5 rows per model is the minimum that reports both signals
    as `measured` rather than `neutral`/`tier_default`. Shared by every
    router_variant/warm_signals test below."""
    settings, registry, case, ctx, eligible = _build(tmp_path, [
        _entry("slow-available", price_input=0.08, price_output=0.08),
        _entry("fast-flaky", price_input=0.08, price_output=0.08),
    ])
    inp = _strategy_input(case, ctx, eligible)
    assert {c.name for c in inp.eligible} == {"slow-available", "fast-flaky"}
    rows = (
        [MatrixRow(case_id=f"warm-{i}", endpoint="slow-available",
                   quality=1.0, passed=True, cost_usd=0.0001, latency_ms=900.0)
         for i in range(5)]
        + [MatrixRow(case_id=f"warm-{i}", endpoint="fast-flaky",
                     quality=0.0, passed=False, cost_usd=0.0001,
                     latency_ms=None, error="BackendUnavailable")
           for i in range(5)]
    )
    return settings, registry, inp, rows


class TestRouterVariantAndWarmSignals:
    def test_availability_weight_flips_choice_between_slow_available_and_fast_flaky(
            self, tmp_path):
        settings, registry, inp, rows = _slow_fast_setup(tmp_path)
        signals = warm_signals(settings, rows)

        cold_avail = router_variant(settings, _COLD_WEIGHTS, registry, signals)(inp)
        assert cold_avail == "fast-flaky"

        hot_avail = router_variant(settings, _HOT_WEIGHTS, registry, signals)(inp)
        assert hot_avail == "slow-available"

        # `router_variant` must not mutate the base Settings weights.
        assert settings.routing.weights != _HOT_WEIGHTS

    def test_frozen_read_clock_is_stable_across_real_elapsed_time(self, tmp_path):
        """Fix round 1, item 1(a): scoring the same warm store twice, with a
        real sleep between, must give identical choices and identical
        availability values -- not the live-clock drift the reviewer
        measured (0.0015386 -> 0.0015773 over 50 ms) before the read clock
        was frozen."""
        settings, registry, inp, rows = _slow_fast_setup(tmp_path)
        signals = warm_signals(settings, rows)

        first_choice = router_variant(settings, _HOT_WEIGHTS, registry, signals)(inp)
        first_slow = signals.availability("slow-available")
        first_fast = signals.availability("fast-flaky")

        time.sleep(0.05)

        second_choice = router_variant(settings, _HOT_WEIGHTS, registry, signals)(inp)
        second_slow = signals.availability("slow-available")
        second_fast = signals.availability("fast-flaky")

        assert first_choice == second_choice == "slow-available"
        assert (first_slow.value, first_slow.source) == (second_slow.value, second_slow.source)
        assert (first_fast.value, first_fast.source) == (second_fast.value, second_fast.source)

    def test_frozen_read_clock_is_stable_when_the_live_clock_advances(self, tmp_path, monkeypatch):
        """Same guarantee as above, exercised via a `time.monotonic`
        monkeypatch instead of a real sleep -- faster, and pins that the
        frozen store genuinely never consults the live clock for a `now=None`
        read, rather than merely tolerating a 50 ms gap."""
        settings, registry, inp, rows = _slow_fast_setup(tmp_path)
        signals = warm_signals(settings, rows)
        before = router_variant(settings, _HOT_WEIGHTS, registry, signals)(inp)
        before_value = signals.availability("fast-flaky").value

        real_monotonic = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 10_000.0)

        after = router_variant(settings, _HOT_WEIGHTS, registry, signals)(inp)
        after_value = signals.availability("fast-flaky").value

        assert before == after == "slow-available"
        assert before_value == after_value

    def test_large_wall_offset_makes_warm_reproduce_cold(self, tmp_path):
        """Fix round 1, item 1(b): a store built with `wall_offset` pushed
        well past `staleness_s` decays the availability flip away -- warm
        scoring reproduces a plain, never-replayed `SignalStore`'s choice.
        Guards `wall_offset`'s meaning (an actual staleness knob) and warm
        -vs-cold distinctness (a *small*/default `wall_offset` still differs
        from cold, so this isn't trivially true for every `wall_offset`)."""
        settings, registry, inp, rows = _slow_fast_setup(tmp_path)

        fresh_signals = warm_signals(settings, rows)
        fresh_choice = router_variant(settings, _HOT_WEIGHTS, registry, fresh_signals)(inp)
        assert fresh_choice == "slow-available"   # the warm effect, pinned again for contrast

        far_past = 10 * settings.routing.signals.staleness_s
        stale_signals = warm_signals(settings, rows, wall_offset=far_past)
        stale_choice = router_variant(settings, _HOT_WEIGHTS, registry, stale_signals)(inp)

        cold_signals = SignalStore(settings.routing.signals)
        cold_choice = router_variant(settings, _HOT_WEIGHTS, registry, cold_signals)(inp)

        assert stale_choice == cold_choice
        # The flip itself: `fast-flaky`'s near-zero availability decayed back
        # to (essentially) neutral, and `slow-available`'s measured latency
        # went stale back to tier_default -- the two candidates score
        # identically to how an unreplayed store would see them, not merely
        # "happen to pick the same slug." Pinning the numbers (not just the
        # final choice) is what actually distinguishes "the flip disappeared"
        # from "the flip disappeared to coincidentally land on the same slug
        # anyway" (`fresh_choice` above is ALSO `slow-available`, by tie
        # -break order, so the choice alone doesn't prove the signals decayed).
        assert stale_signals.availability("fast-flaky").value == pytest.approx(1.0, abs=1e-6)
        assert stale_signals.latency("slow-available", "cheap").source == "tier_default"

    def test_warm_signals_replays_through_signalstore_replay(self, tmp_path, monkeypatch):
        """No shortcutting via direct observe_* calls -- `replay` is the only
        path in, so a row-shape bug the same way boot-seeding could hit is
        caught here too."""
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("only-model"),
        ])
        calls = []
        orig_replay = SignalStore.replay
        def spy(self, observations):
            calls.append(list(observations))
            return orig_replay(self, observations)
        monkeypatch.setattr(SignalStore, "replay", spy)

        rows = [MatrixRow(case_id="c1", endpoint="only-model", quality=1.0,
                          passed=True, cost_usd=0.0001, latency_ms=250.0)]
        warm_signals(settings, rows)

        assert len(calls) == 1
        obs = calls[0]
        assert len(obs) == 1
        assert obs[0].model == "only-model"
        assert obs[0].outcome == "ok"
        assert obs[0].latency_ms == 250.0
        assert obs[0].streamed is False


# --------------------------------------------------------------------------
# router_variant weight validation -- fix round 1, item 2. `weights` must go
# through the real `RoutingCfg` validation (the `capacity` -> `availability`
# rename, and a required-key check), erroring clearly at variant-construction
# time rather than `KeyError`ing deep inside `DefaultRoutingStrategy.rank`.
# --------------------------------------------------------------------------

class TestRouterVariantWeightValidation:
    def test_legacy_capacity_key_is_renamed_like_a_real_routing_yaml(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("cheap-model", price_input=0.01, price_output=0.01),
            _entry("other-model", price_input=0.02, price_output=0.02),
        ])
        inp = _strategy_input(case, ctx, eligible)
        signals = SignalStore(settings.routing.signals)

        capacity_weights = {"quality": 1.0, "cost": 0.6, "latency": 0.3,
                            "capacity": 0.2}
        via_capacity = router_variant(settings, capacity_weights, registry, signals)(inp)

        availability_weights = {"quality": 1.0, "cost": 0.6, "latency": 0.3,
                                "availability": 0.2}
        via_availability = router_variant(settings, availability_weights, registry, signals)(inp)

        # Same routing decision either way -- `capacity` really did become
        # `availability`, not a silently-ignored extra key.
        assert via_capacity == via_availability

    def test_weights_missing_a_required_key_raises_at_construction_not_in_rank(
            self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("cheap-model", price_input=0.01, price_output=0.01),
        ])
        inp = _strategy_input(case, ctx, eligible)
        signals = SignalStore(settings.routing.signals)

        incomplete_weights = {"quality": 1.0, "cost": 0.6, "latency": 0.3}
        # missing "availability" -- must fail HERE, not the first time a
        # returned strategy is called.
        with pytest.raises(pydantic.ValidationError, match="availability"):
            router_variant(settings, incomplete_weights, registry, signals)
