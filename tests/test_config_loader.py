from app.config.loader import load_settings


def test_loads_real_config():
    s = load_settings()
    assert s.routing.tiers == ["cheap", "mid", "frontier"]
    assert s.policy.default_sensitivity == "public"
    assert s.pricing.baseline_model == "bedrock-frontier"
    assert {c.provider for c in s.routing.model_pool} == {"onprem", "azure", "bedrock"}


def test_env_override(monkeypatch):
    monkeypatch.setenv("SP_INFERENCE_BACKEND", "none")
    assert load_settings().serving.inference_backend == "none"


def test_routing_config_exposes_quality_fields():
    from app.config.loader import load_settings
    s = load_settings()
    # tier_quality covers every canonical tier, promoted from the old hardcoded _Q
    assert set(s.routing.tier_quality) == set(s.routing.tiers)
    assert s.routing.tier_quality["cheap"] == 0.5
    assert s.routing.tier_quality["frontier"] == 1.0
    # per-model published quality is optional; shipped config leaves it unset
    # (tier-default regression — populating it is an onboarding step)
    assert all(c.quality is None for c in s.routing.model_pool)
