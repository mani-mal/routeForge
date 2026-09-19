"""Task 1 — pluggable RoutingStrategy seam. Behavior-preserving refactor of
Router.select's step-5 scoring (see .superpowers/sdd/quality-axis-and-strategy/
task-1-brief.md). Four invariants:

1. behavior-preserving: DefaultRoutingStrategy reproduces the pre-refactor
   inline scoring exactly, for a spread of inputs.
2. residency (load-bearing): a strategy's `rank` is handed ONLY the already
   -filtered eligible set -- it never sees, and so can never pick, a
   residency-filtered cloud candidate.
3. config: `routing.strategy: default` builds DefaultRoutingStrategy; an
   unknown name is a boot ConfigLintError; an injected strategy overrides
   config.
4. a strategy only orders -- it can't filter-in extra candidates.
"""
from __future__ import annotations

import copy

import pytest

from app.config.lint import ConfigLintError, enforce
from app.config.loader import load_settings
from app.config.schema import PoolCandidate
from app.models import ExecutionPolicyContext
from app.routing.quality import QualityResolver
from app.routing.router import Router, expected_output_tokens
from app.routing.strategy import DefaultRoutingStrategy, build_strategy
from tests._litellm_fake import make_test_registry

S = load_settings()
_prov = dict(S.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S = S.model_copy(update={"serving": S.serving.model_copy(update={"providers": _prov})})
REG = make_test_registry(S)


def _ctx(providers, risk="low", onprem=False):
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        # The eligible set these tests have always assumed: an app that narrows
        # nothing, so every pool candidate whose provider is permitted is a
        # candidate -- what `build_context` computes for such an app. Stated
        # because `ctx.allowed_models` is binding on the router, with no flag
        # that can turn it off (app/policy/feasible.py).
        allowed_models=frozenset(c.name for c in REG.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


# --- 1. behavior-preserving -------------------------------------------------

def _pre_refactor_score(c: PoolCandidate, est: float, max_cost: float,
                         w: dict, quality_resolver: QualityResolver, _L: dict) -> float:
    """The exact inline `score` fn Router.select used before this refactor
    (CLAUDE.md task-1-brief.md lines 20-23), reproduced verbatim as the
    golden oracle."""
    norm = est / max_cost if max_cost > 0 else 0.0
    q = quality_resolver.resolve(c.name).score
    return (w["quality"] * q - w["cost"] * norm
            - w["latency"] * _L[c.tier] + w["capacity"] * 1.0)


@pytest.mark.parametrize("tokens_after,complexity,min_tier", [
    (20, 0.1, None),      # cheap tier
    (200, 0.1, "mid"),    # mid tier, cost tie-break relevant
    (20, 0.9, None),      # frontier tier
])
def test_default_strategy_matches_pre_refactor_scoring(tokens_after, complexity, min_tier):
    ctx = _ctx({"onprem", "azure", "bedrock"})
    router = Router(S, registry=REG)

    # Reconstruct the same eligible set Router.select would build, by asking
    # Router.select itself for the decision and independently recomputing the
    # golden ordering over the tier it landed on.
    decision = router.select(ctx, tokens_after=tokens_after, complexity=complexity,
                              min_tier=min_tier)
    pool = REG.effective_pool()
    tier = decision.chosen.tier
    out_tokens = expected_output_tokens(S, None)
    eligible = []
    for c in pool:
        if c.provider not in ctx.allowed_providers or c.tier != tier:
            continue
        est = router._est_cost(c, tokens_after, out_tokens)
        if est <= ctx.max_cost_usd:
            eligible.append((c, est))

    _L = {"cheap": 0.2, "mid": 0.5, "frontier": 1.0}
    max_cost = max((e for _, e in eligible), default=0.0)
    golden = sorted(
        eligible,
        key=lambda it: (-_pre_refactor_score(it[0], it[1], max_cost, S.routing.weights,
                                              router._quality, _L), it[1]))

    strategy_out = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=tokens_after, weights=S.routing.weights,
        quality_resolver=router._quality)

    assert [c.name for c, _ in strategy_out] == [c.name for c, _ in golden]
    assert strategy_out[0][0].name == decision.chosen.name
    assert strategy_out[0][1] == pytest.approx(decision.est_cost_usd)


def test_higher_quality_wins_reproduced_via_default_strategy():
    # Same setup as tests/test_router.py::test_higher_quality_wins_within_tier_when_cost_latency_equal,
    # asserted directly against the extracted strategy to prove no drift.
    from app.routing.quality import QualityScore
    qr = QualityResolver(S, overrides={
        "azure-gpt-mid": QualityScore(1.0, "customer"),
        "local-32b": QualityScore(0.1, "customer")})
    router = Router(S, quality_resolver=qr, registry=REG)
    decision = router.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=200,
                              complexity=0.1, min_tier="mid")
    assert decision.chosen.name == "azure-gpt-mid"
    assert decision.quality_source == "customer"


# --- 2. residency invariant (load-bearing) ----------------------------------

class _SpyStrategy:
    """Records exactly which candidates `rank` was handed, then delegates to
    the real default ordering so `select` still returns a sane decision."""

    def __init__(self):
        self.seen: list[tuple[PoolCandidate, float]] = []

    def rank(self, eligible, *, ctx, tokens_after, weights, quality_resolver):
        self.seen = list(eligible)
        return DefaultRoutingStrategy().rank(
            eligible, ctx=ctx, tokens_after=tokens_after, weights=weights,
            quality_resolver=quality_resolver)


def test_strategy_never_sees_residency_filtered_cloud_candidate():
    spy = _SpyStrategy()
    router = Router(S, registry=REG, strategy=spy)
    ctx = _ctx({"onprem"}, onprem=True)  # onprem_only: cloud must be filtered pre-strategy
    decision = router.select(ctx, tokens_after=20, complexity=0.1)

    assert decision.chosen.provider == "onprem"
    seen_names = {c.name for c, _ in spy.seen}
    seen_providers = {c.provider for c, _ in spy.seen}
    assert "azure" not in seen_providers
    assert "bedrock" not in seen_providers
    assert seen_names, "spy should have seen at least the on-prem candidate(s)"
    # Cross-check against the pool: every cloud candidate in the raw pool was
    # withheld from the strategy entirely.
    cloud_in_pool = {c.name for c in REG.effective_pool() if c.provider != "onprem"}
    assert not (cloud_in_pool & seen_names)


def test_strategy_never_sees_residency_filtered_candidate_high_risk():
    # High risk + onprem_only: risk floor would normally push to frontier, but
    # residency clamps first (CLAUDE.md rule 8) -- the spy must still never
    # see a cloud candidate.
    spy = _SpyStrategy()
    router = Router(S, registry=REG, strategy=spy)
    ctx = _ctx({"onprem"}, risk="high", onprem=True)
    decision = router.select(ctx, tokens_after=20, complexity=0.1)

    assert decision.chosen.provider == "onprem"
    seen_providers = {c.provider for c, _ in spy.seen}
    assert seen_providers <= {"onprem"}


# --- 3. config strategy selection -------------------------------------------

def test_config_strategy_default_builds_default_strategy():
    assert isinstance(build_strategy("default"), DefaultRoutingStrategy)


def test_config_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown routing.strategy"):
        build_strategy("nemo-switchyard-not-implemented-yet")


def test_lint_rejects_unknown_routing_strategy():
    s = copy.deepcopy(load_settings())
    s.routing.strategy = "does-not-exist"
    with pytest.raises(ConfigLintError, match="routing.strategy"):
        enforce(s)


def test_current_config_strategy_is_default():
    s = load_settings()
    assert s.routing.strategy == "default"
    enforce(copy.deepcopy(s))  # does not raise


def test_router_uses_config_selected_strategy_by_default():
    router = Router(S, registry=REG)
    assert isinstance(router._strategy, DefaultRoutingStrategy)


def test_injected_strategy_overrides_config():
    spy = _SpyStrategy()
    router = Router(S, registry=REG, strategy=spy)
    assert router._strategy is spy
    router.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert spy.seen  # the injected strategy actually ran, not the config default


# --- 4. a strategy only orders, never filters-in ----------------------------

def test_default_strategy_returns_same_members_only_reordered():
    router = Router(S, registry=REG)
    ctx = _ctx({"onprem", "azure", "bedrock"})
    pool = REG.effective_pool()
    tier = "mid"
    out_tokens = expected_output_tokens(S, None)
    eligible = [(c, router._est_cost(c, 200, out_tokens)) for c in pool
                if c.provider in ctx.allowed_providers and c.tier == tier]
    assert len(eligible) >= 2  # otherwise this test proves nothing

    ordered = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=200, weights=S.routing.weights,
        quality_resolver=router._quality)

    assert len(ordered) == len(eligible)
    assert {c.name for c, _ in ordered} == {c.name for c, _ in eligible}
    assert {(c.name, e) for c, e in ordered} == {(c.name, e) for c, e in eligible}
