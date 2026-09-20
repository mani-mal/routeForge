import json
import time

import pytest

from app.config.schema import PoolCandidate, SignalsCfg
from app.routing.quality import QUALITY_SOURCES
from app.routing.signals import SignalStore
from app.routing.strategy import DefaultRoutingStrategy

WEIGHTS = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2}

def _replay(record: dict) -> list[str]:
    """Recompute the ranking from the emitted record ALONE -- no SignalStore,
    no live config. This function is deliberately a naive re-implementation:
    if it needs anything the record does not carry, the record is insufficient
    and the successor slice would be measuring a fiction."""
    w = record["weights"]
    scale = record["latency_scale_ms"]
    cap = record["max_latency_norm"]
    out = []
    for c in record["candidates"]:
        score = (w["quality"] * c["quality"]["v"]
                 - w["cost"] * c["cost"]["norm"]
                 - w["latency"] * min(c["latency"]["ms"] / scale, cap)
                 + w["availability"] * c["availability"]["v"])
        out.append((score, c["cost"]["est_usd"], c["name"]))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [name for _s, _e, name in out]

@pytest.mark.parametrize("seed", range(20))
def test_the_record_alone_reproduces_the_router_order(seed, quality_resolver_factory):
    import random
    rnd = random.Random(seed)
    tiers = ["cheap", "mid", "frontier"]
    eligible = [(PoolCandidate(name=f"m{i}", provider="onprem",
                               tier=rnd.choice(tiers)), rnd.uniform(1e-6, 1e-3))
                for i in range(rnd.randint(1, 6))]
    cfg = SignalsCfg(min_samples=1)
    store = SignalStore(cfg)
    # `rank()` reads signals on the real `time.monotonic()` clock (no clock
    # seam -- see backlog B40), so observations must be anchored to that same
    # clock or the staleness check compares a real `now` against a synthetic
    # one and every observation reads as stale. On a host with meaningful
    # uptime, `now=float(k)` (k in [0, 6)) is always far in the "past" of a
    # `time.monotonic()` that started counting at boot, so every candidate
    # fell back to `tier_default`/`neutral` and the replay property was pinned
    # only where both new terms are constants -- the region it exists to
    # cover was never exercised. Same idiom as
    # tests/routing/test_strategy_identity.py:89,105,200.
    t0 = time.monotonic()
    for c, _e in eligible:
        for k in range(rnd.randint(0, 6)):
            if rnd.random() < 0.3:
                store.observe_error(c.name, now=t0 + k)
            else:
                store.observe_success(c.name, rnd.uniform(50.0, 5_000.0), now=t0 + k)

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store,
        signals_cfg=cfg, cached_pricing={})

    actual = [sc.candidate.name for sc in result.ordered]
    # The property must hold on the WIRE form, not just the raw Python dict --
    # `sp.scoring` and the response certificate both carry this record through
    # a json.dumps/json.loads round-trip (app/obs/export.py, the HTTP
    # response body), so pin sufficiency on what actually ships.
    wire_record = json.loads(json.dumps(result.to_record()))
    assert _replay(wire_record) == actual

def test_the_record_carries_provenance_for_every_term(quality_resolver_factory):
    eligible = [(PoolCandidate(name="m", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg()
    rec = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=SignalStore(cfg),
        signals_cfg=cfg, cached_pricing={}).to_record()

    c = rec["candidates"][0]
    assert c["latency"]["src"] == "tier_default"
    assert c["availability"]["src"] == "neutral"
    assert c["quality"]["src"] == "tier_default"
    assert c["cost"]["cached"] == "unpriced"
    assert c["latency"]["n"] == 0

def test_the_record_contains_no_free_text(quality_resolver_factory):
    """Emitted regardless of capture_io, so it must carry ids/enums/numbers only."""
    eligible = [(PoolCandidate(name="m", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg()
    rec = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=SignalStore(cfg),
        signals_cfg=cfg, cached_pricing={}).to_record()

    allowed_srcs = {"measured", "tier_default", "neutral", "disabled",
                    "customer", "calibrated", "published"}
    for c in rec["candidates"]:
        assert c["latency"]["src"] in allowed_srcs
        assert c["availability"]["src"] in allowed_srcs
        assert c["quality"]["src"] in QUALITY_SOURCES
        assert c["cost"]["cached"] in {"priced", "unpriced"}
