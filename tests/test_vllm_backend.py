import asyncio
import httpx

from app.config.schema import PriceCfg, VllmCfg
from app.gateway.backends.vllm import VllmBackend
from app.models import Message


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://vllm:8000/v1")


def test_generate_maps_openai_response():
    def handler(req):
        assert req.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "42"},
                         "logprobs": {"content": [{"logprob": -0.1}, {"logprob": -0.2}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    cfg = VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60)
    b = VllmBackend(cfg, client=_client(handler))
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
    cfg = VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60)
    b = VllmBackend(cfg, client=_client(handler))
    price = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")], price, "cheap", {}))
    assert r.confidence == 0.9


def test_generate_without_usage_defaults_to_zero_tokens():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    cfg = VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60)
    b = VllmBackend(cfg, client=_client(handler))
    price = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")], price, "cheap", {}))
    assert r.tokens_in == 0 and r.tokens_out == 0
    assert r.cost_usd == 0.0
    assert r.latency_ms >= 0.0


def test_health_ok_and_unreachable():
    ok = VllmBackend(VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60),
                      client=_client(lambda req: httpx.Response(200, json={"data": [{"id": "local-8b"}]})))
    h = asyncio.run(ok.health())
    assert h.ok and "local-8b" in h.models

    def boom(req):
        raise httpx.ConnectError("refused")
    bad = VllmBackend(VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60), client=_client(boom))
    result = asyncio.run(bad.health())
    assert result.ok is False
    assert result.detail
