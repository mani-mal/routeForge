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
