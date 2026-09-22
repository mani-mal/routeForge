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
from app.eval.endpoints import (
    build_registry,
    deployment_failover_cap,
    harness_settings,
    load_endpoints,
)
from app.eval.graders import GraderSpec
from app.eval.oracle import MatrixRow, attempt_outcome
from app.eval.strategies import (
    LeakageError,
    OnlineArm,
    StrategyInput,
    attempt_cap,
    best_published_quality,
    best_published_quality_attempts,
    cheapest,
    cheapest_attempts,
    fixed,
    fixed_attempts,
    router,
    router_attempts,
    router_variant,
    router_variant_attempts,
    take_attempts,
    warm_prior_signals,
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
                     latency_ms=None, error="BackendUnavailable",
                     counts_unhealth=True)
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

    def test_warm_signals_skips_error_rows_that_do_not_count_as_unhealth(self, tmp_path):
        """B48c: an error row with `counts_unhealth=False` (e.g. the root
        exception was a 4xx -- the request was wrong, not the endpoint) must
        leave availability completely untouched, not merely unpenalized --
        `warm_signals` must not even call `observe_error` for it. A row with
        `counts_unhealth=True` still lowers availability exactly as before."""
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("only-model"),
        ])

        healthy_error_row = MatrixRow(
            case_id="c1", endpoint="only-model", quality=0.0, passed=False,
            cost_usd=0.0, latency_ms=None, error="BadRequest: oversized prompt",
            counts_unhealth=False)
        skipped_signals = warm_signals(settings, [healthy_error_row])
        assert "only-model" not in skipped_signals.snapshot()

        unreachable_root_row = MatrixRow(
            case_id="c1", endpoint="only-model", quality=0.0, passed=False,
            cost_usd=0.0, latency_ms=None, error="NoEligibleCandidate: exhausted",
            counts_unhealth=None)
        also_skipped = warm_signals(settings, [unreachable_root_row])
        assert "only-model" not in also_skipped.snapshot()

        unhealth_row = MatrixRow(
            case_id="c1", endpoint="only-model", quality=0.0, passed=False,
            cost_usd=0.0, latency_ms=None, error="BackendUnavailable: down",
            counts_unhealth=True)
        counted_signals = warm_signals(settings, [unhealth_row])
        state = counted_signals.snapshot()["only-model"]
        assert state["availability_n"] == 1
        assert state["availability"] < 1.0


# --------------------------------------------------------------------------
# router_variant weight validation -- fix round 1, item 2 (revised in fix
# round 2: the required-key check moved from `RoutingCfg` itself to
# `router_variant`, so a real `routing.yaml` boot's `CONFIG_LINT:
# WEIGHT_MISSING` behavior is untouched -- app/config/schema.py is reverted
# to its Task-5-start state; only app/eval/strategies.py owns this check).
# `weights` still goes through the real `RoutingCfg` validation for the
# `capacity` -> `availability` rename, then a local required-key check
# errors clearly at variant-construction time rather than `KeyError`ing deep
# inside `DefaultRoutingStrategy.rank`.
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
        # returned strategy is called. A plain ValueError from
        # router_variant itself (not a pydantic.ValidationError from
        # RoutingCfg) -- this check is eval-local, per fix round 2.
        with pytest.raises(ValueError, match="availability") as exc_info:
            router_variant(settings, incomplete_weights, registry, signals)
        assert not isinstance(exc_info.value, pydantic.ValidationError)


# ==========================================================================
# B48 Task 4 -- attempt orders, the online router arm, the prior-only warm
# arm (spec 2026-09-22 §5-§6). Every chain is checked against the rule it
# encodes: failover on failure only, capped at 1 + the DEPLOYMENT's
# max_failover_attempts, router chains = repeated real Router.select with
# exclude, and no arm scoring case k holds an observation from case >= k.
# ==========================================================================

_AVAIL_OFF = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.0}
_AVAIL_HOT = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 5.0}


def _ok(case_id, endpoint, latency_ms=100.0):
    return MatrixRow(case_id=case_id, endpoint=endpoint, quality=1.0, passed=True,
                     cost_usd=0.0001, latency_ms=latency_ms)


def _err(case_id, endpoint, *, counts_unhealth=True):
    return MatrixRow(case_id=case_id, endpoint=endpoint, quality=0.0, passed=False,
                     cost_usd=0.0, latency_ms=None, error="BackendUnavailable",
                     counts_unhealth=counts_unhealth, failure_latency_ms=100.0)


def _failing(*slugs):
    dead = set(slugs)
    return lambda slug: slug in dead


def _four_priced(tmp_path):
    return _build(tmp_path, [
        _entry("a-model", price_input=0.01, price_output=0.01, quality=0.5),
        _entry("b-model", price_input=0.02, price_output=0.02, quality=0.9),
        _entry("c-model", price_input=0.03, price_output=0.03, quality=0.7),
        _entry("d-model", price_input=0.04, price_output=0.04, quality=0.6),
    ])


class TestAttemptCap:
    def test_cap_comes_from_the_deployment_not_the_harness(self, tmp_path):
        settings, *_ = _four_priced(tmp_path)
        # The harness zeroes failover for its single-shot sweep; the cap
        # must still be the deployment's 1 + max_failover_attempts.
        assert settings.routing.max_failover_attempts == 0
        assert deployment_failover_cap(BASE_SETTINGS) == 2
        assert attempt_cap(BASE_SETTINGS) == 3
        assert attempt_cap(settings) == 1   # what reading the wrong object gives

    def test_take_attempts_stops_at_first_success_and_at_the_cap(self):
        assert take_attempts(["x", "y", "z"], _failing("x"), 3) == ("x", "y")
        assert take_attempts(["x", "y", "z"], _failing(), 3) == ("x",)
        assert take_attempts(["w", "x", "y", "z"], _failing("w", "x", "y", "z"), 3) \
            == ("w", "x", "y")
        assert take_attempts([], _failing(), 3) == ()
        with pytest.raises(ValueError):
            take_attempts(["x"], _failing(), 0)


class TestBaselineAttemptOrders:
    def test_cheapest_fails_over_in_cost_order(self, tmp_path):
        _settings, registry, case, ctx, eligible = _four_priced(tmp_path)
        inp = _strategy_input(case, ctx, eligible)
        strat = cheapest_attempts(registry, attempt_cap(BASE_SETTINGS))
        assert strat(inp, _failing()) == ("a-model",)
        assert strat(inp, _failing("a-model")) == ("a-model", "b-model")
        # First element agrees with the single-choice baseline.
        assert strat(inp, _failing())[0] == cheapest(registry)(inp)

    def test_best_quality_fails_over_in_quality_order(self, tmp_path):
        settings, registry, case, ctx, eligible = _four_priced(tmp_path)
        inp = _strategy_input(case, ctx, eligible)
        strat = best_published_quality_attempts(registry, settings, attempt_cap(BASE_SETTINGS))
        assert strat(inp, _failing("b-model")) == ("b-model", "c-model")
        assert strat(inp, _failing())[0] == best_published_quality(registry, settings)(inp)

    def test_fixed_then_cheapest_order_without_repeating_the_pin(self, tmp_path):
        _settings, registry, case, ctx, eligible = _four_priced(tmp_path)
        inp = _strategy_input(case, ctx, eligible)
        cap = attempt_cap(BASE_SETTINGS)
        assert fixed_attempts("c-model", registry, cap)(inp, _failing("c-model", "a-model")) \
            == ("c-model", "a-model", "b-model")
        # Pin not eligible -> cheapest's order alone.
        assert fixed_attempts("not-onboarded", registry, cap)(inp, _failing("a-model")) \
            == ("a-model", "b-model")

    def test_cap_three_failing_candidates_is_exhausted_after_three(self, tmp_path):
        _settings, registry, case, ctx, eligible = _four_priced(tmp_path)
        inp = _strategy_input(case, ctx, eligible)
        all_dead = _failing("a-model", "b-model", "c-model", "d-model")
        chain = cheapest_attempts(registry, attempt_cap(BASE_SETTINGS))(inp, all_dead)
        assert chain == ("a-model", "b-model", "c-model")
        rows = {s: _err(case.id, s) for s in ("a-model", "b-model", "c-model", "d-model")}
        out = attempt_outcome(rows, chain)
        assert out["exhausted"] is True and out["attempts"] == 3

    def test_empty_eligible_is_no_choice(self, tmp_path):
        settings, registry, case, ctx, _ = _four_priced(tmp_path)
        inp = StrategyInput(case=case, ctx=ctx, eligible=(), tokens_after=5,
                            output_tokens=64)
        cap = attempt_cap(BASE_SETTINGS)
        assert cheapest_attempts(registry, cap)(inp, _failing()) == ()
        assert best_published_quality_attempts(registry, settings, cap)(inp, _failing()) == ()
        assert fixed_attempts("a-model", registry, cap)(inp, _failing()) == ()


def _direct_chain(settings, registry, signals, inp, failed, cap):
    """The gateway's loop written out by hand against a fresh Router."""
    rt = Router(settings, registry=registry, signals=signals)
    complexity = min(1.0, inp.tokens_after / 2000)
    chain = []
    while len(chain) < cap:
        try:
            d = rt.select(inp.ctx, inp.tokens_after, complexity,
                          exclude=frozenset(chain), output_tokens=inp.output_tokens)
        except NoEligibleCandidate:
            break
        chain.append(d.chosen.name)
        if not failed(d.chosen.name):
            break
    return tuple(chain)


class TestRouterAttemptChains:
    @pytest.mark.parametrize("dead", [(), ("a-model",), ("a-model", "b-model"),
                                      ("a-model", "b-model", "c-model", "d-model")])
    def test_chain_equals_repeated_direct_router_select(self, tmp_path, dead):
        settings, registry, case, ctx, eligible = _four_priced(tmp_path)
        inp = _strategy_input(case, ctx, eligible)
        signals = SignalStore(settings.routing.signals)
        cap = attempt_cap(BASE_SETTINGS)
        got = router_attempts(settings, registry, signals, cap)(inp, _failing(*dead))
        assert got == _direct_chain(settings, registry, signals, inp, _failing(*dead), cap)
        assert 1 <= len(got) <= cap
        assert len(set(got)) == len(got)            # exclude: never retried
        assert all(s in dead for s in got[:-1])     # failover on failure only

    def test_all_failing_router_chain_is_capped(self, tmp_path):
        settings, registry, case, ctx, eligible = _four_priced(tmp_path)
        inp = _strategy_input(case, ctx, eligible)
        chain = router_attempts(settings, registry, SignalStore(settings.routing.signals),
                                attempt_cap(BASE_SETTINGS))(
            inp, _failing("a-model", "b-model", "c-model", "d-model"))
        assert len(chain) == 3

    def test_no_eligible_candidate_ends_the_chain(self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("only-model")])
        inp = _strategy_input(case, ctx, eligible)
        chain = router_attempts(settings, registry, SignalStore(settings.routing.signals),
                                attempt_cap(BASE_SETTINGS))(inp, _failing("only-model"))
        assert chain == ("only-model",)

    def test_variant_chain_uses_the_variant_weights(self, tmp_path):
        settings, registry, inp, rows = _slow_fast_setup(tmp_path)
        signals = warm_signals(settings, rows)
        cap = attempt_cap(BASE_SETTINGS)
        assert router_variant_attempts(settings, _COLD_WEIGHTS, registry, signals, cap)(
            inp, _failing("fast-flaky")) == ("fast-flaky", "slow-available")
        assert router_variant_attempts(settings, _HOT_WEIGHTS, registry, signals, cap)(
            inp, _failing("fast-flaky")) == ("slow-available",)


# --------------------------------------------------------------------------
# The online arm. Scenario: two same-tier, same-price endpoints; `dead`
# (the one the cold router picks first) fails every case with an endpoint
# -unhealth error, `live` always serves.
# --------------------------------------------------------------------------

def _two_endpoint_setup(tmp_path):
    settings, registry, case, ctx, eligible = _build(tmp_path, [
        _entry("a-model", price_input=0.08, price_output=0.08),
        _entry("b-model", price_input=0.08, price_output=0.08),
    ])
    inp = _strategy_input(case, ctx, eligible)
    cold_first = router_attempts(settings, registry, SignalStore(settings.routing.signals),
                                 1)(inp, _failing())[0]
    dead = cold_first
    live = "b-model" if dead == "a-model" else "a-model"
    return settings, registry, inp, dead, live


def _case_rows(k, dead, live, *, dead_counts_unhealth=True):
    cid = f"case-{k}"
    return {dead: _err(cid, dead, counts_unhealth=dead_counts_unhealth),
            live: _ok(cid, live, latency_ms=150.0)}


def _assert_no_leak(arm: OnlineArm, k: int) -> None:
    """The no-leakage invariant, checked two independent ways: the arm's
    applied-observation log, and the store's own recorded timestamps (case
    `i` observations sit in [t_i, t_{i+1}) on the case clock, t_i = i *
    seconds_per_case)."""
    t_k = k * arm.seconds_per_case
    assert all(o.case_index < k for o in arm.applied), \
        f"case {k} scored with observations from {sorted({o.case_index for o in arm.applied})}"
    for model, st in arm.signals._state.items():
        assert st.availability_ts < t_k, f"{model} availability_ts {st.availability_ts} >= {t_k}"
        if st.latency_n:
            assert st.latency_ts < t_k, f"{model} latency_ts {st.latency_ts} >= {t_k}"
    if arm.applied:   # an empty store's anchor (0.0) reads nothing
        assert arm.read_clock < t_k


def _run_online(settings, registry, inp, dead, live, n_cases, *, weights=None,
                leak=False, check=True, dead_counts_unhealth=True, seconds_per_case=1.0):
    arm = OnlineArm(settings, registry, attempt_cap(BASE_SETTINGS), weights=weights,
                    seconds_per_case=seconds_per_case)
    chains = []
    for k in range(n_cases):
        rows = _case_rows(k, dead, live, dead_counts_unhealth=dead_counts_unhealth)
        def failed(s, rows=rows):
            return rows[s].error is not None
        if leak:
            # The injected leak: learn case k's outcome, THEN score case k.
            # The built-in guard refuses case k once it is observed, so the
            # leaking caller must also lie about the index (k + 1).
            arm.observe(k, (dead, live), rows)
        if check:
            _assert_no_leak(arm, k)
        chain = arm.choose(inp, failed, case_index=k + 1 if leak else k)
        chains.append(chain)
        if not leak:
            arm.observe(k, chain, rows)
    return arm, chains


class TestOnlineArm:
    def test_no_leakage_when_scoring_each_case(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        _run_online(settings, registry, inp, dead, live, 8, weights=_AVAIL_HOT)

    def test_injected_leak_is_caught(self, tmp_path):
        """Falsification: observing case k before scoring it must trip the
        same check that passes above -- the check is not vacuous."""
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        with pytest.raises(AssertionError, match="case 0 scored with observations"):
            _run_online(settings, registry, inp, dead, live, 8, weights=_AVAIL_HOT,
                        leak=True)

    def test_injected_leak_is_caught_by_the_builtin_guard(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm = OnlineArm(settings, registry, attempt_cap(BASE_SETTINGS), seconds_per_case=1.0)
        rows = _case_rows(0, dead, live)
        assert arm.choose(inp, lambda s: rows[s].error is not None, case_index=0)
        arm.observe(0, (dead, live), rows)
        with pytest.raises(LeakageError):
            arm.choose(inp, lambda s: rows[s].error is not None, case_index=0)
        # The next case is fine.
        arm.choose(inp, lambda s: rows[s].error is not None, case_index=1)

    def test_case_index_is_required(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm = OnlineArm(settings, registry, attempt_cap(BASE_SETTINGS), seconds_per_case=1.0)
        with pytest.raises(TypeError):
            arm.choose(inp, _failing())   # no case_index: refused, not unguarded

    def test_guard_compares_against_the_last_observed_case_not_applied(self, tmp_path):
        """A case whose attempts applied NOTHING (every error is a 4xx-shaped
        non-unhealth failure) is still observed: re-scoring it must raise,
        although the applied-observation log never mentions it."""
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm = OnlineArm(settings, registry, attempt_cap(BASE_SETTINGS), seconds_per_case=1.0)
        rows = {dead: _err("case-0", dead, counts_unhealth=False)}
        arm.choose(inp, _failing(dead), case_index=0)
        arm.observe(0, (dead,), rows)
        assert arm.applied == ()
        with pytest.raises(LeakageError):
            arm.choose(inp, _failing(dead), case_index=0)

    def test_the_leak_changes_outcomes(self, tmp_path):
        """Why the invariant matters: a leaking arm learns `dead` is down one
        case early (min_samples=5 errors reached before scoring case 4), so
        it skips the dead endpoint on case 4; the honest arm pays one more
        wasted attempt there."""
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        _, honest = _run_online(settings, registry, inp, dead, live, 6, weights=_AVAIL_HOT)
        _, leaky = _run_online(settings, registry, inp, dead, live, 6, weights=_AVAIL_HOT,
                               leak=True, check=False)
        assert honest[:5] == [(dead, live)] * 5
        assert honest[5] == (live,)
        assert leaky[4] == (live,)
        assert honest != leaky

    def test_availability_drops_after_unhealth_failures_only(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm, _ = _run_online(settings, registry, inp, dead, live, 5, weights=_AVAIL_HOT)
        av = arm.signals.availability(dead)
        assert av.source == "measured" and av.value < 0.1
        assert arm.signals.availability(live).value == 1.0

        arm2, chains2 = _run_online(settings, registry, inp, dead, live, 5,
                                    weights=_AVAIL_HOT, dead_counts_unhealth=False)
        assert dead not in arm2.signals.snapshot()
        assert arm2.signals.availability(dead).value == 1.0
        assert all(c == (dead, live) for c in chains2)   # never learns to avoid it
        assert {o.endpoint for o in arm2.applied} == {live}

    def test_observations_follow_attempt_order_on_the_case_clock(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm, _ = _run_online(settings, registry, inp, dead, live, 2)
        assert [(o.case_index, o.endpoint, o.outcome, o.t) for o in arm.applied] == [
            (0, dead, "error", 0.0), (0, live, "ok", 0.001),
            (1, dead, "error", 1.0), (1, live, "ok", 1.001)]
        assert arm.read_clock == 1.001

    def test_case_clock_uses_seconds_per_case_and_reads_at_t_k(self, tmp_path):
        """Spec §6 (amended): case k happens at t_k = k * seconds_per_case,
        1 ms sub-steps within a case, and case k is scored reading at t_k."""
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm, _ = _run_online(settings, registry, inp, dead, live, 2, seconds_per_case=30.0)
        assert [(o.case_index, o.t) for o in arm.applied] == [
            (0, 0.0), (0, 0.001), (1, 30.0), (1, 30.001)]
        arm.choose(inp, _failing(dead), case_index=4)
        assert arm.read_clock == 120.0
        assert all(o.t < 120.0 for o in arm.applied)

    def test_read_time_moves_availability_decay(self, tmp_path):
        """Reading at t_k (not at the last observation) is what lets a
        failed endpoint recover while nothing is observed about it."""
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm, _ = _run_online(settings, registry, inp, dead, live, 5, weights=_AVAIL_HOT,
                             seconds_per_case=300.0)
        arm.choose(inp, _failing(dead), case_index=5)
        early = arm.signals.availability(dead).value
        arm.choose(inp, _failing(dead), case_index=50)
        assert arm.signals.availability(dead).value > early

    def test_seconds_per_case_must_be_positive(self, tmp_path):
        settings, registry, *_ = _two_endpoint_setup(tmp_path)
        for bad in (0, -1.0):
            with pytest.raises(ValueError):
                OnlineArm(settings, registry, 3, seconds_per_case=bad)

    def test_skipped_attempts_do_not_advance_the_clock(self, tmp_path):
        settings, registry, _inp, dead, live = _two_endpoint_setup(tmp_path)
        arm = OnlineArm(settings, registry, attempt_cap(BASE_SETTINGS), seconds_per_case=1.0)
        arm.observe(0, (dead, live), _case_rows(0, dead, live, dead_counts_unhealth=False))
        assert [(o.endpoint, o.t) for o in arm.applied] == [(live, 0.0)]
        arm.observe(3, (dead,), {dead: _err("case-3", dead, counts_unhealth=False)})
        assert arm.read_clock == 0.0

    def test_observe_rejects_out_of_order_and_malformed_chains(self, tmp_path):
        settings, registry, _inp, dead, live = _two_endpoint_setup(tmp_path)
        arm = OnlineArm(settings, registry, attempt_cap(BASE_SETTINGS), seconds_per_case=1.0)
        arm.observe(2, (dead, live), _case_rows(2, dead, live))
        with pytest.raises(ValueError):
            arm.observe(2, (live,), _case_rows(2, dead, live))
        with pytest.raises(ValueError):
            arm.observe(1, (live,), _case_rows(1, dead, live))
        with pytest.raises(ValueError, match="not the last attempt"):
            arm.observe(3, (live, dead), _case_rows(3, dead, live))
        with pytest.raises(ValueError, match="no matrix row"):
            arm.observe(4, ("ghost",), _case_rows(4, dead, live))
        with pytest.raises(ValueError, match="repeated attempt"):
            arm.observe(5, (dead, dead), _case_rows(5, dead, live))
        with pytest.raises(ValueError, match="exceed max_attempts"):
            arm.observe(5, (dead, live, "c", "d"), _case_rows(5, dead, live))
        no_latency = {live: MatrixRow(case_id="case-5", endpoint=live, quality=1.0,
                                      passed=True, cost_usd=0.0, latency_ms=None)}
        with pytest.raises(ValueError, match="no latency_ms"):
            arm.observe(5, (live,), no_latency)
        # A rejected chain changed nothing: case 5 can still be observed.
        arm.observe(5, (live,), _case_rows(5, dead, live))

    def test_online_choose_equals_router_chain_over_its_own_store(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        arm, _ = _run_online(settings, registry, inp, dead, live, 3)
        failed = _failing(dead)
        assert arm.choose(inp, failed, case_index=3) == _direct_chain(
            settings, registry, arm.signals, inp, failed, attempt_cap(BASE_SETTINGS))


# --------------------------------------------------------------------------
# The prior-only warm arm.
# --------------------------------------------------------------------------

class TestWarmPriorSignals:
    def _rows(self, dead, live, n):
        rows = []
        for k in range(n):
            r = _case_rows(k, dead, live)
            rows += [r[dead], r[live]]
        return rows

    def test_prior_rows_only_and_the_read_clock(self, tmp_path):
        settings, registry, inp, dead, live = _two_endpoint_setup(tmp_path)
        index = {f"case-{k}": k for k in range(10)}
        store = warm_prior_signals(settings, self._rows(dead, live, 5),
                                   case_index=index, scoring_case=5, seconds_per_case=1.0)
        assert store.frozen_now == 5.0            # read at t_k, not at the last row
        assert all(st.availability_ts < 5 for st in store._state.values())
        assert store.availability(dead).value < 0.1
        assert router_attempts(settings, registry, store, attempt_cap(BASE_SETTINGS))(
            inp, _failing(dead)) == (live,)

    def test_rows_from_the_scored_case_raise(self, tmp_path):
        """Falsification: handing the scored case's own rows to the prior arm
        is refused, not silently replayed."""
        settings, _registry, _inp, dead, live = _two_endpoint_setup(tmp_path)
        index = {f"case-{k}": k for k in range(10)}
        with pytest.raises(LeakageError):
            warm_prior_signals(settings, self._rows(dead, live, 6),
                               case_index=index, scoring_case=5, seconds_per_case=1.0)

    def test_rows_sit_on_the_case_clock_and_the_read_is_t_k(self, tmp_path):
        settings, _registry, _inp, dead, live = _two_endpoint_setup(tmp_path)
        index = {f"case-{k}": k for k in range(10)}
        store = warm_prior_signals(settings, self._rows(dead, live, 3),
                                   case_index=index, scoring_case=3, seconds_per_case=30.0)
        assert store.frozen_now == 90.0
        assert store._state[dead].availability_ts == 60.0      # case 2, j=0
        assert store._state[live].availability_ts == pytest.approx(60.001)

    def test_empty_prior_is_a_cold_store(self, tmp_path):
        settings, *_ = _two_endpoint_setup(tmp_path)
        store = warm_prior_signals(settings, [], case_index={}, scoring_case=0,
                                   seconds_per_case=30.0)
        assert store.snapshot() == {} and store.frozen_now == 0.0

    def test_non_unhealth_errors_are_skipped_without_consuming_a_slot(self, tmp_path):
        settings, _registry, _inp, dead, live = _two_endpoint_setup(tmp_path)
        rows = [_err("case-0", dead, counts_unhealth=False), _ok("case-0", live)]
        store = warm_prior_signals(settings, rows, case_index={"case-0": 0},
                                   scoring_case=1, seconds_per_case=1.0)
        assert dead not in store.snapshot()
        assert store._state[live].availability_ts == 0.0   # live's success took slot j=0

    def test_leak_guard_arguments_are_required(self, tmp_path):
        settings, *_ = _two_endpoint_setup(tmp_path)
        with pytest.raises(TypeError):
            warm_prior_signals(settings, [], seconds_per_case=1.0)
        with pytest.raises(TypeError):
            warm_prior_signals(settings, [], case_index={}, seconds_per_case=1.0)

    def test_success_row_without_latency_raises(self, tmp_path):
        settings, _registry, _inp, _dead, live = _two_endpoint_setup(tmp_path)
        row = MatrixRow(case_id="case-0", endpoint=live, quality=1.0, passed=True,
                        cost_usd=0.0, latency_ms=None)
        with pytest.raises(ValueError, match="no latency_ms"):
            warm_prior_signals(settings, [row], case_index={"case-0": 0},
                               scoring_case=1, seconds_per_case=1.0)


class TestWarmSignalsSlotFix:
    def test_skipped_rows_do_not_consume_timestamp_slots(self, tmp_path, monkeypatch):
        """Task 2 review note: the last APPLIED row lands at the anchor
        (wall_offset=0), even when later rows were skipped."""
        settings, _registry, _inp, dead, live = _two_endpoint_setup(tmp_path)
        seen = []
        orig = SignalStore.replay
        monkeypatch.setattr(SignalStore, "replay",
                            lambda self, obs: (seen.extend(obs), orig(self, obs))[1])
        store = warm_signals(settings, [_ok("c0", live), _err("c0", dead, counts_unhealth=False),
                                        _err("c1", dead, counts_unhealth=None)])
        assert len(seen) == 1
        assert seen[0].ts - store.wall_offset == pytest.approx(store.frozen_now, abs=1e-6)


# --------------------------------------------------------------------------
# No-failure invariant (spec §8.5).
# --------------------------------------------------------------------------

class TestNoFailureInvariant:
    def test_availability_is_exactly_one_and_weight_variants_agree(self, tmp_path):
        settings, registry, inp, a, b = _two_endpoint_setup(tmp_path)
        cap = attempt_cap(BASE_SETTINGS)
        n = 8

        def rows_for(k):
            return {a: _ok(f"case-{k}", a, latency_ms=900.0),
                    b: _ok(f"case-{k}", b, latency_ms=100.0)}

        per_variant_chains = {}
        for name, weights in {"off": _AVAIL_OFF, "hot": _AVAIL_HOT, "base": None}.items():
            arm = OnlineArm(settings, registry, cap, weights=weights, seconds_per_case=30.0)
            chains = []
            for k in range(n):
                rows = rows_for(k)
                chain = arm.choose(inp, lambda s, rows=rows: rows[s].error is not None,
                                   case_index=k)
                chains.append(chain)
                arm.observe(k, chain, rows)
            for slug in (a, b):
                assert arm.signals.availability(slug).value == 1.0
            per_variant_chains[name] = chains
        assert per_variant_chains["off"] == per_variant_chains["hot"] \
            == per_variant_chains["base"]

        # Warm-prior: every endpoint's rows, both variants agree, avail 1.0.
        prior = [r for k in range(n) for r in rows_for(k).values()]
        store = warm_prior_signals(settings, prior,
                                   case_index={f"case-{k}": k for k in range(n)},
                                   scoring_case=n, seconds_per_case=30.0)
        for slug in (a, b):
            assert store.availability(slug).value == 1.0
        warm_chains = {name: router_variant_attempts(settings, w, registry, store, cap)(
                           inp, _failing())
                       for name, w in {"off": _AVAIL_OFF, "hot": _AVAIL_HOT}.items()}
        assert warm_chains["off"] == warm_chains["hot"]
        # And the cold arm.
        cold = SignalStore(settings.routing.signals)
        assert router_variant_attempts(settings, _AVAIL_OFF, registry, cold, cap)(
            inp, _failing()) == router_variant_attempts(
            settings, _AVAIL_HOT, registry, cold, cap)(inp, _failing())
