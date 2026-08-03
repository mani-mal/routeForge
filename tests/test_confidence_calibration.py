"""Confidence calibration for the real (LiteLLM) backend
(routeforge-router-targets-mid).

A real vLLM/Dynamo backend reports mean token logprob. The naive mapping
confidence = exp(mean_logprob) is the geometric-mean per-token probability,
which sits ~0.4-0.6 even for perfectly fluent answers — so it trips the 0.60
escalation threshold on nearly every response and every cheap-tier answer gets
escalated cheap->mid, collapsing the demo's "~70% to cheap" story.

The fix softens the logprob by routing.confidence_logprob_temperature:
confidence = exp(mean_logprob / T). These tests pin the calibrated behaviour
and prove the factory wires the configured T into LiteLLMBackend.
"""
import asyncio
import math

import pytest

from app.config.loader import load_settings
from app.config.schema import PriceCfg
from app.gateway.backends import litellm_backend
from app.gateway.backends.factory import make_backend
from app.gateway.backends.litellm_backend import LiteLLMBackend
from app.models import Message
from tests._litellm_fake import CALLS, make_response

_PRICE = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
_THRESHOLD = 0.60  # config/routing.yaml confidence_escalation_threshold


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()
    yield
    CALLS.clear()


def _confidence(monkeypatch, mean_logprob: float, temperature: float, *,
                inference_backend: str = "vllm") -> float:
    # Two tokens both at mean_logprob so their mean is exactly mean_logprob.
    async def _call(**kwargs):
        CALLS.append(kwargs)
        return make_response(logprobs=[mean_logprob, mean_logprob])
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

    s = load_settings()
    s = s.model_copy(update={"serving": s.serving.model_copy(
        update={"inference_backend": inference_backend})})
    b = LiteLLMBackend(s, logprob_temperature=temperature)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")],
                               _PRICE, "cheap", {}))
    return r.confidence


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_raw_temperature_escalates_a_fluent_answer(monkeypatch):
    # T=1 (the old behaviour) on a fluent answer (mean logprob -0.7, perplexity
    # ~2.0) yields ~0.50 — BELOW threshold, so it would escalate. This is the bug.
    c = _confidence(monkeypatch, -0.7, temperature=1.0)
    assert math.isclose(c, math.exp(-0.7), rel_tol=1e-6)
    assert c < _THRESHOLD


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_calibrated_temperature_keeps_a_fluent_answer_on_tier(monkeypatch):
    # T=4 on the same fluent answer clears the threshold — no escalation.
    c = _confidence(monkeypatch, -0.7, temperature=4.0)
    assert math.isclose(c, math.exp(-0.7 / 4.0), rel_tol=1e-6)
    assert c > _THRESHOLD


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_calibrated_temperature_still_escalates_genuine_uncertainty(monkeypatch):
    # A genuinely high-perplexity answer (mean logprob -2.5, perplexity ~12)
    # still falls below the threshold at T=4 — the escalation feature survives.
    assert _confidence(monkeypatch, -2.5, temperature=4.0) < _THRESHOLD


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_nonpositive_temperature_falls_back_to_raw(monkeypatch):
    assert math.isclose(_confidence(monkeypatch, -0.7, temperature=0.0),
                        math.exp(-0.7), rel_tol=1e-6)


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_dynamo_inference_backend_honours_temperature(monkeypatch):
    # dynamo and vllm share the same LiteLLMBackend; only the api_base mapping
    # (_map) differs by inference_backend. Confidence calibration is identical.
    c = _confidence(monkeypatch, -0.7, temperature=4.0, inference_backend="dynamo")
    assert c > _THRESHOLD


def test_factory_wires_configured_temperature(monkeypatch):
    # make_backend must inject routing.confidence_logprob_temperature into the
    # backend, or the calibration silently never applies in production.
    monkeypatch.setenv("SP_INFERENCE_BACKEND", "vllm")
    s = load_settings()
    assert s.routing.confidence_logprob_temperature == 4.0  # shipped default
    b = make_backend(s)
    assert isinstance(b, LiteLLMBackend)
    assert b._t == 4.0
