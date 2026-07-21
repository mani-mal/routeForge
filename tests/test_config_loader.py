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
