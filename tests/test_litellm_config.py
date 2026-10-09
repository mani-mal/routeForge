from app.config.loader import load_settings


def test_serving_providers_carry_litellm_mapping():
    s = load_settings()
    onprem = s.serving.providers["onprem"]
    assert onprem.litellm_prefix == "openai"
    azure = s.serving.providers["azure"]
    assert azure.litellm_prefix == "azure"
    assert azure.api_base_env == "AZURE_API_BASE"
    assert azure.api_key_env == "AZURE_API_KEY"
    assert azure.api_version_env == "AZURE_API_VERSION"
    assert s.serving.providers["bedrock"].litellm_prefix == "bedrock"


def test_pool_candidates_can_carry_litellm_model():
    s = load_settings()
    by_name = {c.name: c for c in s.routing.model_pool}
    assert by_name["azure-gpt-mid"].litellm_model == "azure/gpt-4o-mini"
    assert by_name["bedrock-frontier"].litellm_model == "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_allow_backend_fallback_field_removed():
    s = load_settings()
    assert not hasattr(s.serving, "allow_backend_fallback")


def test_inference_backend_field_removed():
    # There is no `inference_backend` axis any more — every provider egresses
    # through the single LiteLLM SDK path; on-prem availability/endpoints come
    # from the ModelRegistry (onboarded rows), not a boot-time backend switch.
    s = load_settings()
    assert not hasattr(s.serving, "inference_backend")
    assert not hasattr(s.serving, "vllm")
    assert not hasattr(s.serving, "dynamo")



# --- deployment overlay + per-provider switch (the DGX lab's cloud path) -----

OVERLAY = "deploy/dgx_lab/config-overlay"


def test_shipped_config_has_no_openrouter():
    s = load_settings()
    assert "openrouter" not in s.serving.providers
    assert not [c for c in s.routing.model_pool if c.provider == "openrouter"]


def test_overlay_adds_a_provider_off_until_switched_on(monkeypatch):
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", OVERLAY)
    s = load_settings()
    p = s.serving.providers["openrouter"]
    assert p.enabled is False
    assert (p.prompt_retention, p.accepts_raw_sensitive) == ("retained", "false")
    # shipped entries survive the merge, overlay entries are appended by name
    names = [c.name for c in s.routing.model_pool]
    assert names[:2] == ["azure-gpt-mid", "bedrock-frontier"]
    assert names[2:] == ["openrouter-mid", "openrouter-frontier"]
    assert s.pricing.models["openrouter-frontier"].input == 0.50
    assert s.serving.providers["azure"].litellm_prefix == "azure"


def test_overlay_changes_the_routing_policy_version(monkeypatch):
    base = load_settings().routing_policy_version
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", OVERLAY)
    assert load_settings().routing_policy_version != base


def test_overlaid_config_passes_the_cross_file_lint(monkeypatch):
    from app.config.lint import enforce
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", OVERLAY)
    monkeypatch.setenv("SP_PROVIDER_OPENROUTER_ENABLED", "true")
    enforce(load_settings())


def test_an_overlay_pool_entry_without_a_price_fails_the_lint(monkeypatch, tmp_path):
    import pytest
    from app.config.lint import enforce
    (tmp_path / "routing.yaml").write_text(
        "model_pool:\n  - {name: extra, provider: azure, tier: mid}\n")
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        enforce(load_settings())


def test_provider_enabled_env_switch(monkeypatch):
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", OVERLAY)
    monkeypatch.setenv("SP_PROVIDER_OPENROUTER_ENABLED", "true")
    s = load_settings()
    assert s.serving.providers["openrouter"].enabled is True
    assert s.serving.providers["azure"].enabled is False       # others untouched
    monkeypatch.setenv("SP_PROVIDER_ONPREM_ENABLED", "off")
    assert load_settings().serving.providers["onprem"].enabled is False


def test_openrouter_call_mapping_reads_the_key_from_env(monkeypatch):
    from app.gateway.backends.litellm_backend import LiteLLMBackend
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", OVERLAY)
    monkeypatch.setenv("SP_PROVIDER_OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    model, kwargs = LiteLLMBackend(load_settings())._map("openrouter-frontier")
    assert model == "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    assert kwargs == {"api_key": "test-key"}


def test_openrouter_is_unreachable_while_disabled(monkeypatch):
    import pytest
    from app.gateway.backends.litellm_backend import LiteLLMBackend
    monkeypatch.setenv("SP_CONFIG_OVERLAY_DIR", OVERLAY)
    with pytest.raises(RuntimeError, match="not enabled"):
        LiteLLMBackend(load_settings())._map("openrouter-mid")


def test_onprem_only_never_admits_a_cloud_provider():
    from app.policy.feasible import ONPREM_ONLY_PROVIDERS
    assert ONPREM_ONLY_PROVIDERS == frozenset({"onprem"})


def test_manifest_overlay_matches_the_overlay_directory():
    import yaml
    from pathlib import Path
    items = yaml.safe_load(Path("deploy/dgx_lab/routeforge-app.yaml").read_text())["items"]
    cm = next(i for i in items if i["kind"] == "ConfigMap"
              and i["metadata"]["name"] == "routeforge-config-overlay")
    for f in ("serving.yaml", "routing.yaml", "pricing.yaml"):
        assert cm["data"][f] == Path(OVERLAY, f).read_text(), f
