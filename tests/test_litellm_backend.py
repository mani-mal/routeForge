import math
import pytest
from app.config.loader import load_settings
from app.models import Message
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response, fake_acompletion, make_test_registry, CALLS


def _price(reg, name):
    return reg.price_for(name)


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()
    yield
    CALLS.clear()


def _patch(monkeypatch, resp):
    async def _call(**kwargs):
        CALLS.append(kwargs)
        return resp
    monkeypatch.setattr(litellm_backend, "acompletion", _call)


def test_onprem_maps_to_openai_prefix(monkeypatch):
    _patch(monkeypatch, make_response(content="hi", prompt_tokens=10, completion_tokens=5))
    s = load_settings()
    reg = make_test_registry(s)
    b = litellm_backend.LiteLLMBackend(s, registry=reg)
    import asyncio
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price(reg, "local-8b"), "cheap", {}))
    assert CALLS[0]["model"] == "openai/local-8b"
    assert r.cost_usd == pytest.approx((10 * 0.05 + 5 * 0.05) / 1_000_000)
    assert r.tool_calls is None


def test_azure_and_bedrock_use_litellm_model(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response())
    # azure/bedrock are disabled by default in config/serving.yaml (guardrail 3
    # keeps cloud unreachable-by-construction); enable them here to exercise the
    # litellm_model mapping in isolation from that guardrail, which is covered
    # separately by test_disabled_provider_raises below.
    settings = load_settings()
    settings.serving.providers["azure"].enabled = True
    settings.serving.providers["bedrock"].enabled = True
    b = litellm_backend.LiteLLMBackend(settings)
    asyncio.run(b.generate("azure-gpt-mid", [Message(role="user", content="hi")],
                           settings.pricing.models["azure-gpt-mid"], "mid", {}))
    assert CALLS[0]["model"] == "azure/gpt-4o-mini"
    CALLS.clear()
    asyncio.run(b.generate("bedrock-frontier", [Message(role="user", content="hi")],
                           settings.pricing.models["bedrock-frontier"], "frontier", {}))
    assert CALLS[0]["model"].startswith("bedrock/anthropic.claude")


def test_no_logprobs_uses_default_confidence(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response(logprobs=None))
    s = load_settings()
    reg = make_test_registry(s)
    b = litellm_backend.LiteLLMBackend(s, registry=reg)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price(reg, "local-8b"), "cheap", {}))
    assert r.confidence == 0.9


def test_logprobs_softened_by_temperature(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response(logprobs=[-1.0, -1.0, -1.0]))
    s = load_settings()
    reg = make_test_registry(s)
    b = litellm_backend.LiteLLMBackend(s, registry=reg, logprob_temperature=4.0)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price(reg, "local-8b"), "cheap", {}))
    assert r.confidence == pytest.approx(min(1.0, math.exp(-1.0 / 4.0)))


def test_tool_calls_surfaced(monkeypatch):
    import asyncio
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'}}]
    _patch(monkeypatch, make_response(content=None, tool_calls=tc))
    s = load_settings()
    reg = make_test_registry(s)
    b = litellm_backend.LiteLLMBackend(s, registry=reg)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="2+2")],
                               _price(reg, "local-8b"), "cheap", {}, tools=[{"type": "function"}]))
    assert r.tool_calls == tc
    assert CALLS[0]["tools"] == [{"type": "function"}]
    assert CALLS[0]["num_retries"] == 0


def test_disabled_provider_raises(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response())
    s = load_settings()  # azure disabled by default
    b = litellm_backend.LiteLLMBackend(s)
    with pytest.raises(Exception):
        asyncio.run(b.generate("azure-gpt-mid", [Message(role="user", content="hi")],
                               s.pricing.models["azure-gpt-mid"], "mid", {}))


def test_bare_fake_acompletion_direct_monkeypatch(monkeypatch):
    """Covers the previously-latent bug: monkeypatching acompletion with the
    BARE fake_acompletion (no functools.partial, no wrapper) must work,
    because that is the exact form the module docstring advertises and Tasks
    5-8 rely on."""
    import asyncio
    monkeypatch.setattr(litellm_backend, "acompletion", fake_acompletion)
    s = load_settings()
    reg = make_test_registry(s)
    b = litellm_backend.LiteLLMBackend(s, registry=reg)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price(reg, "local-8b"), "cheap", {}))
    assert isinstance(r, litellm_backend.GenerationResult)
    assert len(CALLS) == 1
    assert CALLS[0]["model"] == "openai/local-8b"


def test_onprem_model_with_no_registered_endpoint_raises():
    """Guardrail 3, onprem branch: `_map` must refuse to generate for an
    on-prem candidate whose registry has no endpoint registered for it.
    `effective_pool()` only ever includes enabled onboarded rows, which
    always carry an endpoint, so this can't happen via the normal onboarding
    path -- this test guards the invariant directly (a fake registry that
    violates it) rather than relying on that always being true, replacing
    the old `inference_backend == "none"` rejection this same _map branch
    used to also serve (see app/policy/context.py for the request-level
    onprem_only rejection, now derived from `registry.has_healthy_onprem()`,
    tested in tests/test_derived_onprem_reject.py)."""
    from app.config.schema import PoolCandidate

    class _FakeRegistry:
        def effective_pool(self):
            return [PoolCandidate(name="ghost-onprem", provider="onprem", tier="cheap")]

        def endpoint_for(self, slug):
            return None

    s = load_settings()
    b = litellm_backend.LiteLLMBackend(s, registry=_FakeRegistry())
    with pytest.raises(RuntimeError, match="no registered endpoint"):
        b._map("ghost-onprem")
