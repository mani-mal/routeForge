"""Confidence calibration for real backends (routeforge-router-targets-mid).

A real vLLM/Dynamo backend reports mean token logprob. The naive mapping
confidence = exp(mean_logprob) is the geometric-mean per-token probability,
which sits ~0.4-0.6 even for perfectly fluent answers — so it trips the 0.60
escalation threshold on nearly every response and every cheap-tier answer gets
escalated cheap->mid, collapsing the demo's "~70% to cheap" story.

The fix softens the logprob by routing.confidence_logprob_temperature:
confidence = exp(mean_logprob / T). These tests pin the calibrated behaviour
and prove the factory wires the configured T into the adapters.
"""
import asyncio
import math

import httpx

from app.config.loader import load_settings
from app.config.schema import DynamoCfg, PriceCfg, VllmCfg
from app.gateway.backends.dynamo import DynamoBackend
from app.gateway.backends.factory import make_backend
from app.gateway.backends.vllm import VllmBackend

_PRICE = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
_THRESHOLD = 0.60  # config/routing.yaml confidence_escalation_threshold


def _client(mean_logprob: float):
    # Two tokens both at mean_logprob so their mean is exactly mean_logprob.
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "logprobs": {"content": [{"logprob": mean_logprob},
                                                  {"logprob": mean_logprob}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="http://vllm:8000/v1")


def _confidence(mean_logprob: float, temperature: float) -> float:
    from app.models import Message
    b = VllmBackend(VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60),
                    client=_client(mean_logprob), logprob_temperature=temperature)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")],
                               _PRICE, "cheap", {}))
    return r.confidence


def test_raw_temperature_escalates_a_fluent_answer():
    # T=1 (the old behaviour) on a fluent answer (mean logprob -0.7, perplexity
    # ~2.0) yields ~0.50 — BELOW threshold, so it would escalate. This is the bug.
    c = _confidence(-0.7, temperature=1.0)
    assert math.isclose(c, math.exp(-0.7), rel_tol=1e-6)
    assert c < _THRESHOLD


def test_calibrated_temperature_keeps_a_fluent_answer_on_tier():
    # T=4 on the same fluent answer clears the threshold — no escalation.
    c = _confidence(-0.7, temperature=4.0)
    assert math.isclose(c, math.exp(-0.7 / 4.0), rel_tol=1e-6)
    assert c > _THRESHOLD


def test_calibrated_temperature_still_escalates_genuine_uncertainty():
    # A genuinely high-perplexity answer (mean logprob -2.5, perplexity ~12)
    # still falls below the threshold at T=4 — the escalation feature survives.
    assert _confidence(-2.5, temperature=4.0) < _THRESHOLD


def test_nonpositive_temperature_falls_back_to_raw():
    assert math.isclose(_confidence(-0.7, temperature=0.0), math.exp(-0.7),
                        rel_tol=1e-6)


def test_dynamo_backend_honours_temperature():
    from app.models import Message

    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "logprobs": {"content": [{"logprob": -0.7}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="http://dynamo-frontend:8080")
    b = DynamoBackend(DynamoCfg(endpoint="http://dynamo-frontend:8080",
                                router_mode="kv", timeout_s=60),
                      client=client, logprob_temperature=4.0)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")],
                               _PRICE, "cheap", {}))
    assert r.confidence > _THRESHOLD


def test_factory_wires_configured_temperature(monkeypatch):
    # make_backend must inject routing.confidence_logprob_temperature into the
    # adapter, or the calibration silently never applies in production.
    monkeypatch.setenv("SP_INFERENCE_BACKEND", "vllm")
    s = load_settings()
    assert s.routing.confidence_logprob_temperature == 4.0  # shipped default
    b = make_backend(s)
    assert isinstance(b, VllmBackend)
    assert b._logprob_temperature == 4.0
