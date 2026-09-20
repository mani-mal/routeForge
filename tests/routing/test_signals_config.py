import pytest

from app.config.schema import RoutingCfg


def _routing(**kw) -> dict:
    base = {
        "tiers": ["cheap", "mid", "frontier"],
        "score_on": "post_compression",
        "weights": {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2},
        "tier_quality": {"cheap": 0.5, "mid": 0.7, "frontier": 0.9},
        "complexity_thresholds": {"cheap_max": 0.3, "mid_max": 0.7},
        "confidence_escalation_threshold": 0.5,
        "max_escalations": 1,
        "risk_floors": {"low": "cheap", "medium": "mid", "high": "frontier"},
        "model_pool": [],
        "fallback": {},
    }
    base.update(kw)
    return base


def test_capacity_is_accepted_as_a_deprecated_alias_for_availability():
    """No deployed routing.yaml may fail to boot on this rename."""
    cfg = RoutingCfg(**_routing(
        weights={"quality": 1.0, "cost": 0.6, "latency": 0.3, "capacity": 0.2}))
    assert cfg.weights["availability"] == 0.2
    assert "capacity" not in cfg.weights


def test_an_explicit_availability_wins_over_a_stale_capacity_alias():
    cfg = RoutingCfg(**_routing(
        weights={"quality": 1.0, "cost": 0.6, "latency": 0.3,
                 "capacity": 0.9, "availability": 0.2}))
    assert cfg.weights["availability"] == 0.2


def test_signal_defaults_preserve_the_identity_property():
    """tier_latency_ms / latency_scale_ms must reproduce the old _L constants."""
    cfg = RoutingCfg(**_routing())
    s = cfg.signals
    assert s.latency_scale_ms == 1000.0
    assert [s.tier_latency_ms[t] / s.latency_scale_ms
            for t in ("cheap", "mid", "frontier")] == [0.2, 0.5, 1.0]
