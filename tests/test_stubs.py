def test_agent_stub_returns_direct():
    from app.agent import decide_mode
    assert decide_mode() == "direct"


def test_obs_export_is_noop():
    from app.obs.export import export
    assert export([]) is None


def test_backend_stubs_import():
    from app.gateway.backends.vllm import VLLMBackend
    from app.gateway.backends.dynamo import DynamoBackend
    assert VLLMBackend().name == "vllm"
    assert DynamoBackend().name == "dynamo"
