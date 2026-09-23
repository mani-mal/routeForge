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
from app.routing.signals import SignalStore
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
            - w["latency"] * _L[c.tier] + w["availability"] * 1.0)


@pytest.mark.parametrize("tokens_after,complexity,min_tier", [
    (20, 0.1, None),      # cheap tier
    (200, 0.1, "mid"),    # mid tier, cost tie-break relevant
    (20, 0.9, None),      # frontier tier
])
def test_default_strategy_matches_pre_refactor_scoring(tokens_after, complexity, min_tier):
    # NOTE (B44): this is an ORDERING oracle over a single-tier eligible set --
    # within one tier a set-max-normalized latency term is constant across
    # candidates and can't change an ordering, so this test does NOT catch a
    # regression to set-max normalization (a Task 6 review mutation confirmed
    # it survives that mutant). The score-level pin is
    # tests/routing/test_strategy_identity.py::
    # test_empty_signal_store_reproduces_the_legacy_scores_exactly -- read that
    # one for the identity property; do not mistake this older test for
    # equivalent coverage.
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

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=tokens_after, weights=S.routing.weights,
        quality_resolver=router._quality,
        signals=SignalStore(S.routing.signals), signals_cfg=S.routing.signals)

    assert [sc.candidate.name for sc in result.ordered] == [c.name for c, _ in golden]
    assert result.ordered[0].candidate.name == decision.chosen.name
    assert result.ordered[0].est_cost_usd == pytest.approx(decision.est_cost_usd)


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

    def rank(self, eligible, *, ctx, tokens_after, weights, quality_resolver,
             signals, signals_cfg, cached_pricing=None, now=None):
        self.seen = list(eligible)
        return DefaultRoutingStrategy().rank(
            eligible, ctx=ctx, tokens_after=tokens_after, weights=weights,
            quality_resolver=quality_resolver, signals=signals,
            signals_cfg=signals_cfg, cached_pricing=cached_pricing, now=now)


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

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=200, weights=S.routing.weights,
        quality_resolver=router._quality,
        signals=SignalStore(S.routing.signals), signals_cfg=S.routing.signals)

    assert len(result.ordered) == len(eligible)
    assert {sc.candidate.name for sc in result.ordered} == {c.name for c, _ in eligible}
    assert ({(sc.candidate.name, sc.est_cost_usd) for sc in result.ordered}
            == {(c.name, e) for c, e in eligible})


# --- 5. clock seam (B40) -----------------------------------------------------

def test_staleness_cliff_is_reachable_through_rank():
    cfg = S.routing.signals
    store = SignalStore(cfg)
    router = Router(S, registry=REG)
    ctx = _ctx({"onprem"})
    cand = PoolCandidate(name="clock-seam-latency-m", provider="onprem", tier="cheap")
    eligible = [(cand, 0.001)]

    for k in range(cfg.min_samples):
        store.observe_success(cand.name, 120.0, now=float(k))

    fresh = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=20, weights=S.routing.weights,
        quality_resolver=router._quality, signals=store, signals_cfg=cfg,
        now=5.0)
    assert fresh.ordered[0].latency.source == "measured"

    stale = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=20, weights=S.routing.weights,
        quality_resolver=router._quality, signals=store, signals_cfg=cfg,
        now=4.0 + cfg.staleness_s + 1)
    assert stale.ordered[0].latency.source == "tier_default"


def test_availability_decay_is_reachable_through_rank():
    cfg = S.routing.signals
    store = SignalStore(cfg)
    router = Router(S, registry=REG)
    ctx = _ctx({"onprem"})
    cand = PoolCandidate(name="clock-seam-availability-m", provider="onprem", tier="cheap")
    eligible = [(cand, 0.001)]

    for k in range(cfg.min_samples):
        store.observe_error(cand.name, now=float(k))

    soon = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=20, weights=S.routing.weights,
        quality_resolver=router._quality, signals=store, signals_cfg=cfg,
        now=5.0)
    later = DefaultRoutingStrategy().rank(
        eligible, ctx=ctx, tokens_after=20, weights=S.routing.weights,
        quality_resolver=router._quality, signals=store, signals_cfg=cfg,
        now=5.0 + 10 * cfg.half_life_s)

    assert soon.ordered[0].availability.value < later.ordered[0].availability.value
    assert later.ordered[0].availability.value == pytest.approx(1.0, abs=1e-3)


# --- 6. peer latency prior (B51) ---------------------------------------------
# An unmeasured model used to score at the optimistic tier default (200 ms for
# cheap) while its measured tier-mates reported their real 3-18 s, so the router
# preferred every model it knew NOTHING about until each had `min_samples`
# observations. It now borrows the median measured latency of its eligible
# same-tier peers: "assume typical", not "assume ~80x faster".

def _rank(eligible, store, now=None):
    return DefaultRoutingStrategy().rank(
        eligible, ctx=_ctx({"onprem"}), tokens_after=20,
        weights=S.routing.weights, quality_resolver=Router(S, registry=REG)._quality,
        signals=store, signals_cfg=S.routing.signals, now=now)


def _measure(store, name, ms, *, start=0.0):
    for k in range(S.routing.signals.min_samples):
        store.observe_success(name, ms, now=start + k)


def _by_name(result):
    return {sc.candidate.name: sc for sc in result.ordered}


def test_unmeasured_model_borrows_median_of_measured_same_tier_peers():
    store = SignalStore(S.routing.signals)
    a, b, c, new = (PoolCandidate(name=f"b51-{n}", provider="onprem", tier="cheap")
                    for n in ("a", "b", "c", "new"))
    _measure(store, a.name, 3600.0)
    _measure(store, b.name, 16200.0)
    _measure(store, c.name, 18400.0)
    store.observe_success(new.name, 900.0, now=0.0)   # 1 sample < min_samples

    got = _by_name(_rank([(a, .001), (b, .001), (c, .001), (new, .001)], store, now=5.0))

    assert got[new.name].latency.value == 16200.0
    assert got[new.name].latency.source == "peer_prior"
    assert got[new.name].latency.samples == 1          # its own true count
    assert got[a.name].latency.source == "measured"


def test_unmeasured_model_no_longer_outranks_measured_peer_on_missing_data():
    """The B51 pick pattern: equal quality/availability, the measured model is
    cheaper, but the unmeasured one used to win on its fictional 200 ms."""
    store = SignalStore(S.routing.signals)
    known = PoolCandidate(name="b51-known", provider="onprem", tier="cheap")
    unknown = PoolCandidate(name="b51-unknown", provider="onprem", tier="cheap")
    _measure(store, known.name, 16000.0)

    result = _rank([(known, 0.0009), (unknown, 0.001)], store, now=5.0)

    assert result.ordered[0].candidate.name == known.name


def test_prior_ignores_other_tiers_and_falls_back_to_tier_default():
    store = SignalStore(S.routing.signals)
    mid = PoolCandidate(name="b51-mid", provider="onprem", tier="mid")
    cheap = PoolCandidate(name="b51-cheap", provider="onprem", tier="cheap")
    _measure(store, mid.name, 9000.0)

    got = _by_name(_rank([(mid, .001), (cheap, .001)], store, now=5.0))

    assert got[cheap.name].latency.source == "tier_default"
    assert got[cheap.name].latency.value == S.routing.signals.tier_latency_ms["cheap"]


def test_stale_model_falls_back_to_peer_prior_not_tier_default():
    cfg = S.routing.signals
    store = SignalStore(cfg)
    old = PoolCandidate(name="b51-old", provider="onprem", tier="cheap")
    live = PoolCandidate(name="b51-live", provider="onprem", tier="cheap")
    _measure(store, old.name, 4000.0)
    later = cfg.staleness_s + 100.0
    _measure(store, live.name, 12000.0, start=later)

    got = _by_name(_rank([(old, .001), (live, .001)], store,
                         now=later + cfg.min_samples))

    assert got[old.name].latency.source == "peer_prior"
    assert got[old.name].latency.value == 12000.0
    assert got[old.name].latency.samples == cfg.min_samples


def test_empty_store_is_unchanged_by_peer_prior():
    store = SignalStore(S.routing.signals)
    cands = [PoolCandidate(name=f"b51-e{i}", provider="onprem", tier="cheap")
             for i in range(3)]
    for sc in _rank([(c, .001) for c in cands], store).ordered:
        assert sc.latency.source == "tier_default"
        assert sc.latency.value == S.routing.signals.tier_latency_ms["cheap"]


def test_disabled_signals_never_use_peer_prior():
    cfg = S.routing.signals.model_copy(update={"enabled": False})
    store = SignalStore(cfg)
    a = PoolCandidate(name="b51-da", provider="onprem", tier="cheap")
    b = PoolCandidate(name="b51-db", provider="onprem", tier="cheap")
    _measure(store, a.name, 16000.0)
    result = DefaultRoutingStrategy().rank(
        [(a, .001), (b, .001)], ctx=_ctx({"onprem"}), tokens_after=20,
        weights=S.routing.weights, quality_resolver=Router(S, registry=REG)._quality,
        signals=store, signals_cfg=cfg, now=5.0)
    assert {sc.latency.source for sc in result.ordered} == {"disabled"}
