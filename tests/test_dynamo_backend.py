import asyncio
import httpx

from app.config.schema import DynamoCfg, PriceCfg
from app.gateway.backends.dynamo import DynamoBackend
from app.models import Message


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://dynamo-frontend:8080")


def test_generate_hits_v1_chat_completions_under_endpoint():
    def handler(req):
        assert req.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "42"},
                         "logprobs": {"content": [{"logprob": -0.1}, {"logprob": -0.2}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    cfg = DynamoCfg(endpoint="http://dynamo-frontend:8080", router_mode="kv", timeout_s=60)
    b = DynamoBackend(cfg, client=_client(handler))
    price = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")], price, "cheap", {}))
    assert r.text == "42" and r.tokens_in == 10 and r.tokens_out == 3
    assert abs(r.cost_usd - (10 * 3.0 + 3 * 15.0) / 1e6) < 1e-12
    assert 0 < r.confidence <= 1.0


def test_generate_without_logprobs_uses_default_confidence():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    cfg = DynamoCfg(endpoint="http://dynamo-frontend:8080", router_mode="kv", timeout_s=60)
    b = DynamoBackend(cfg, client=_client(handler))
    price = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")], price, "cheap", {}))
    assert r.confidence == 0.9


def test_generate_without_usage_defaults_to_zero_tokens():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    cfg = DynamoCfg(endpoint="http://dynamo-frontend:8080", router_mode="kv", timeout_s=60)
    b = DynamoBackend(cfg, client=_client(handler))
    price = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")], price, "cheap", {}))
    assert r.tokens_in == 0 and r.tokens_out == 0
    assert r.cost_usd == 0.0
    assert r.latency_ms >= 0.0


def test_health_ok_and_unreachable():
    def handler(req):
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "local-8b"}]})
    cfg = DynamoCfg(endpoint="http://dynamo-frontend:8080", router_mode="kv", timeout_s=60)
    ok = DynamoBackend(cfg, client=_client(handler))
    h = asyncio.run(ok.health())
    assert h.ok and "local-8b" in h.models

    def boom(req):
        raise httpx.ConnectError("refused")
    bad = DynamoBackend(cfg, client=_client(boom))
    result = asyncio.run(bad.health())
    assert result.ok is False
    assert result.detail
