import asyncio

from app.config.loader import load_settings
from app.gateway.backends.mock import MockBackend
from app.models import Message

S = load_settings()


def _gen(model, tier, hint=None):
    price = S.pricing.models[model]
    return asyncio.run(MockBackend(S).generate(
        model, [Message(role="user", content="hello world")], price, tier, hint or {}))


def test_mock_is_deterministic():
    a = _gen("local-8b", "cheap")
    b = _gen("local-8b", "cheap")
    assert a.tokens_out == b.tokens_out and a.cost_usd == b.cost_usd


def test_local_tier_is_free():
    assert _gen("local-8b", "cheap").cost_usd == 0.0


def test_confidence_hint_respected():
    assert _gen("local-8b", "cheap", {"mock_confidence": 0.3}).confidence == 0.3


def test_mock_backend_health_ok():
    h = asyncio.run(MockBackend(S).health())
    assert h.ok is True
    assert "local-8b" in h.models
    assert "local-32b" in h.models
