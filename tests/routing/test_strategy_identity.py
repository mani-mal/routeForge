import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st

from app.config.schema import PoolCandidate, SignalsCfg
from app.routing.signals import SignalStore
from app.routing.strategy import DefaultRoutingStrategy

WEIGHTS = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2}
_L_LEGACY = {"cheap": 0.2, "mid": 0.5, "frontier": 1.0}


def _legacy_score(cand, est, max_cost, q, weights):
    """The pre-P2a formula, copied verbatim from app/routing/strategy.py."""
    norm = est / max_cost if max_cost > 0 else 0.0
    return (weights["quality"] * q - weights["cost"] * norm
            - weights["latency"] * _L_LEGACY[cand.tier]
            + weights["capacity"] * 1.0)


@hyp_settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.sampled_from(["cheap", "mid", "frontier"]),
                          st.floats(min_value=1e-9, max_value=1.0,
                                    allow_nan=False, allow_infinity=False),
                          st.floats(min_value=0.0, max_value=1.0)),
                min_size=1, max_size=6))
def test_empty_signal_store_reproduces_the_legacy_scores_exactly(quality_resolver_factory, rows):
    """THE identity property (spec §6.2). Equal SCORES, not merely equal order.

    If this ever fails, a refactor has broken the guarantee that turning
    measurement on changes nothing until a measurement exists -- and the
    successor slice loses its control arm."""
    eligible = [(PoolCandidate(name=f"m{i}", provider="onprem", tier=tier), est)
                for i, (tier, est, _q) in enumerate(rows)]
    qmap = {f"m{i}": q for i, (_t, _e, q) in enumerate(rows)}
    resolver = quality_resolver_factory(qmap)

    cfg = SignalsCfg()
    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=resolver, signals=SignalStore(cfg), signals_cfg=cfg)

    max_cost = max(est for _c, est in eligible)
    legacy_weights = {**WEIGHTS, "capacity": WEIGHTS["availability"]}
    for sc in result.ordered:
        expected = _legacy_score(sc.candidate, sc.est_cost_usd, max_cost,
                                 qmap[sc.candidate.name], legacy_weights)
        assert sc.score == pytest.approx(expected, rel=1e-12, abs=1e-12)


@hyp_settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=1, max_value=6), st.integers(min_value=0, max_value=50))
def test_ranking_never_changes_membership_whatever_the_signals_say(quality_resolver_factory, n, seed):
    """A strategy ranks; it never filters in or out. Signals are the first
    thing that could plausibly breach that invariant."""
    import random
    rnd = random.Random(seed)
    eligible = [(PoolCandidate(name=f"m{i}", provider="onprem", tier="cheap"),
                 0.001 * (i + 1)) for i in range(n)]
    cfg = SignalsCfg(min_samples=1)
    store = SignalStore(cfg)
    for i in range(n):
        for k in range(3):
            if rnd.random() < 0.5:
                store.observe_error(f"m{i}", now=float(k))
            else:
                store.observe_success(f"m{i}", rnd.uniform(1.0, 9_000.0), now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    assert len(result.ordered) == len(eligible)
    assert {sc.candidate.name for sc in result.ordered} == {c.name for c, _ in eligible}


def test_a_measured_fast_model_outranks_a_measured_slow_peer(quality_resolver_factory):
    """The whole point: two same-tier, same-price models now differentiate."""
    eligible = [(PoolCandidate(name="slow", provider="onprem", tier="cheap"), 0.001),
                (PoolCandidate(name="fast", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg(min_samples=1)
    store = SignalStore(cfg)
    # No explicit `now`: `rank()` queries signals.latency/.availability with
    # the real clock (see DefaultRoutingStrategy), so observations must be
    # recorded on that same clock or the staleness check (comparing a real
    # `now` against a synthetic one) would always call them stale.
    for _ in range(5):
        store.observe_success("slow", 3_000.0)
        store.observe_success("fast", 120.0)

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    assert result.ordered[0].candidate.name == "fast"
    assert result.ordered[0].latency.source == "measured"


def test_disabled_signals_restore_tier_defaults_and_say_so(quality_resolver_factory):
    eligible = [(PoolCandidate(name="slow", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg(enabled=False, min_samples=1)
    store = SignalStore(cfg)
    for k in range(5):
        store.observe_success("slow", 9_000.0, now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    assert result.signals_enabled is False
    assert result.ordered[0].latency.source == "disabled"
    assert result.ordered[0].latency.value == 200.0


def test_cost_cached_pricing_reflects_whether_cached_input_is_declared(quality_resolver_factory):
    """Carried finding from the Task 6 review: cached_pricing/cost_cached_pricing/
    cost_norm had zero test coverage, and the specific trap is keying the dict
    wrong (display_name instead of name) -- which would silently leave every
    candidate "unpriced" forever with nothing going red."""
    eligible = [(PoolCandidate(name="priced-m", provider="onprem", tier="cheap"), 0.002),
                (PoolCandidate(name="bare-m", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg()
    cached_pricing = {"priced-m": "priced", "bare-m": "unpriced"}

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=SignalStore(cfg),
        signals_cfg=cfg, cached_pricing=cached_pricing)

    by_name = {sc.candidate.name: sc for sc in result.ordered}
    assert by_name["priced-m"].cost_cached_pricing == "priced"
    assert by_name["bare-m"].cost_cached_pricing == "unpriced"
    # cost_norm: also zero-covered before this test.
    max_cost = max(est for _c, est in eligible)
    assert by_name["priced-m"].cost_norm == pytest.approx(0.002 / max_cost)
    assert by_name["bare-m"].cost_norm == pytest.approx(0.001 / max_cost)


def test_cost_cached_pricing_is_keyed_by_name_through_router_select():
    """The keying is what matters -- run it through Router.select with a real
    registry/pricing lookup, not just the dict plumbing. PoolCandidate has both
    `name` and `display_name`; if the router ever built cached_pricing keyed by
    display_name, this test would catch a candidate wrongly staying
    "unpriced" while its declared cached_input rate went unused."""
    import asyncio

    from app.apps.db import Database
    from app.apps import store as apps_store
    from app.apps.registry import ModelRegistry
    from app.config.loader import load_settings
    from app.models import ExecutionPolicyContext
    from app.routing.router import Router

    settings = load_settings()

    async def _reg_with_cached_pricing():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as s:
            await apps_store.upsert_onboarded(
                s, "cached-m", display_name="Cached Model (display name != slug)",
                provider="onprem", tier="cheap", endpoint="http://cached-m/v1",
                api_key_env=None, price_input=0.05, price_output=0.05,
                price_cached_input=0.01, enabled=True, probe_status="healthy",
                allowed_app_ids=None)
            await apps_store.upsert_onboarded(
                s, "bare-m", display_name="Bare Model (display name != slug)",
                provider="onprem", tier="cheap", endpoint="http://bare-m/v1",
                api_key_env=None, price_input=0.05, price_output=0.05,
                enabled=True, probe_status="healthy", allowed_app_ids=None)
            await s.commit()
        reg = ModelRegistry(settings)
        await reg.refresh(db)
        return reg

    reg = asyncio.run(_reg_with_cached_pricing())
    pool = reg.effective_pool()
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(c.name for c in pool),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)

    d = Router(settings, registry=reg).select(ctx, tokens_after=20, complexity=0.1)

    by_name = {sc.candidate.name: sc for sc in d.scoring.ordered}
    assert by_name["cached-m"].cost_cached_pricing == "priced"
    assert by_name["bare-m"].cost_cached_pricing == "unpriced"


def test_one_pathologically_slow_endpoint_is_clamped(quality_resolver_factory):
    eligible = [(PoolCandidate(name="awful", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg(min_samples=1, max_latency_norm=3.0)
    store = SignalStore(cfg)
    # Real clock again -- see the comment in
    # test_a_measured_fast_model_outranks_a_measured_slow_peer.
    for _ in range(5):
        store.observe_success("awful", 500_000.0)

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    # quality*q - cost*1.0 - latency*3.0 (clamped) + availability*1.0
    assert result.ordered[0].score == pytest.approx(
        WEIGHTS["quality"] * result.ordered[0].quality.value
        - WEIGHTS["cost"] * 1.0 - WEIGHTS["latency"] * 3.0
        + WEIGHTS["availability"] * 1.0)
