"""Thin smoke test for LiteLLMBackend.generate — the deterministic-seed
behaviour this used to cover against MockBackend now lives in
tests/test_litellm_backend.py (real generate() through a fake acompletion)
and tests/_litellm_fake.py's `_DeterministicLiteLLM` (hashed seeded tokens/
cost/confidence, used by the cost/display/controls tests). This file just
pins the on-prem tiering/pricing shape stays sane through the real
LiteLLMBackend.generate path.
"""
import asyncio

import pytest

from app.config.loader import load_settings
from app.gateway.backends import litellm_backend
from app.gateway.backends.litellm_backend import LiteLLMBackend
from app.models import Message
from tests._litellm_fake import CALLS, make_response

S = load_settings()


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()
    yield
    CALLS.clear()


def _gen(monkeypatch, model, tier, *, tokens_out=100, settings=None):
    settings = settings or S
    async def _call(**kwargs):
        CALLS.append(kwargs)
        return make_response(prompt_tokens=10, completion_tokens=tokens_out)
    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    price = settings.pricing.models[model]
    b = LiteLLMBackend(settings)
    return asyncio.run(b.generate(
        model, [Message(role="user", content="hello world")], price, tier, {}))


def test_local_tier_is_cheap_but_not_free(monkeypatch):
    # On-prem is priced at a small representative amortized rate (not $0) so the
    # cost dashboard shows believable spend + routing savings on a real backend.
    local = _gen(monkeypatch, "local-8b", "cheap").cost_usd
    settings = load_settings()
    settings.serving.providers["bedrock"].enabled = True
    frontier = _gen(monkeypatch, "bedrock-frontier", "frontier",
                    tokens_out=100, settings=settings).cost_usd
    assert local > 0.0
    assert local < frontier / 10  # local is dramatically cheaper than frontier


def test_default_confidence_when_no_logprobs(monkeypatch):
    r = _gen(monkeypatch, "local-8b", "cheap")
    assert r.confidence == 0.9  # DEFAULT_CONFIDENCE, no logprobs in make_response()
