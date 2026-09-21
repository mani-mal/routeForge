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

class TestRouterVariantAndWarmSignals:
    def test_availability_weight_flips_choice_between_slow_available_and_fast_flaky(
            self, tmp_path):
        settings, registry, case, ctx, eligible = _build(tmp_path, [
            _entry("slow-available", price_input=0.08, price_output=0.08),
            _entry("fast-flaky", price_input=0.08, price_output=0.08),
        ])
        inp = _strategy_input(case, ctx, eligible)
        assert {c.name for c in inp.eligible} == {"slow-available", "fast-flaky"}

        # min_samples defaults to 5: exactly 5 rows per model so both
        # signals report as "measured", not "neutral"/"tier_default".
        rows = (
            [MatrixRow(case_id=f"warm-{i}", endpoint="slow-available",
                       quality=1.0, passed=True, cost_usd=0.0001,
                       latency_ms=900.0)
             for i in range(5)]
            + [MatrixRow(case_id=f"warm-{i}", endpoint="fast-flaky",
                         quality=0.0, passed=False, cost_usd=0.0001,
                         latency_ms=None, error="BackendUnavailable")
               for i in range(5)]
        )
        signals = warm_signals(settings, rows)

        base_weights = {"quality": 1.0, "cost": 0.6, "latency": 0.3,
                        "availability": 0.0}
        cold_avail = router_variant(settings, base_weights, registry, signals)(inp)
        assert cold_avail == "fast-flaky"

        hot_weights = {**base_weights, "availability": 5.0}
        hot_avail = router_variant(settings, hot_weights, registry, signals)(inp)
        assert hot_avail == "slow-available"

        # `router_variant` must not mutate the base Settings weights.
        assert settings.routing.weights != hot_weights

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
