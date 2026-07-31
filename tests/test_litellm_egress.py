"""Task 6: multi-provider egress tests. Proves LiteLLM egress pins the right
per-provider model string for cloud providers (Azure, Bedrock) once enabled,
and that a disabled cloud provider's `_map` refuses to produce a call at all
(guardrail 3) -- the belt to test_traffic_honesty.py's braces."""
from __future__ import annotations

import asyncio

import pytest

from app.config.loader import load_settings
from app.gateway.backends import litellm_backend
from app.models import Message
from tests._litellm_fake import CALLS, make_response


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()
    yield
    CALLS.clear()


def _enable_cloud(s):
    providers = dict(s.serving.providers)
    providers["azure"] = providers["azure"].model_copy(update={"enabled": True})
    providers["bedrock"] = providers["bedrock"].model_copy(update={"enabled": True})
    return s.model_copy(update={"serving": s.serving.model_copy(update={"providers": providers})})


def test_azure_egress_pins_azure_model(monkeypatch):
    async def _call(**k):
        CALLS.append(k)
        return make_response()

    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    s = _enable_cloud(load_settings())
    b = litellm_backend.LiteLLMBackend(s)
    asyncio.run(b.generate("azure-gpt-mid", [Message(role="user", content="hi")],
                           s.pricing.models["azure-gpt-mid"], "mid", {}))
    assert CALLS[0]["model"] == "azure/gpt-4o-mini"


def test_bedrock_egress_pins_bedrock_model(monkeypatch):
    async def _call(**k):
        CALLS.append(k)
        return make_response()

    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    s = _enable_cloud(load_settings())
    b = litellm_backend.LiteLLMBackend(s)
    asyncio.run(b.generate("bedrock-frontier", [Message(role="user", content="hi")],
                           s.pricing.models["bedrock-frontier"], "frontier", {}))
    assert CALLS[0]["model"].startswith("bedrock/anthropic.claude")


def test_onprem_only_pool_has_no_cloud_candidate_to_call():
    # Belt to Task 4's braces: with cloud disabled (shipped default), a request
    # can only ever be handed onprem models -- asserting the map refuses cloud.
    s = load_settings()
    b = litellm_backend.LiteLLMBackend(s)
    with pytest.raises(RuntimeError):
        b._map("azure-gpt-mid")   # azure disabled by default
