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
    # onprem entries need no override (default <prefix>/<name>)
    assert by_name["local-8b"].litellm_model is None


def test_allow_backend_fallback_field_removed():
    s = load_settings()
    assert not hasattr(s.serving, "allow_backend_fallback")


def test_default_inference_backend_is_vllm(monkeypatch):
    # tests/conftest.py pins SP_INFERENCE_BACKEND=mock so the suite stays
    # offline/fast (see its comment) — unset it here to check the shipped
    # config/serving.yaml default itself, same pattern as
    # test_compression_config.py::test_sp_context_opt_env_override.
    monkeypatch.delenv("SP_INFERENCE_BACKEND", raising=False)
    s = load_settings()
    assert s.serving.inference_backend == "vllm"
