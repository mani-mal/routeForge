"""B75 item 4: the all-frontier baseline prices the cached share at the
baseline model's cached-input rate."""
import asyncio

import pytest

from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


def _pipe(backend_cls=_DeterministicLiteLLM):
    s = load_settings()
    registry = make_test_registry(s)
    det = make_detector("regex")
    co = s.serving.context_optimization
    return s, Pipeline(s, Gate(s, det, registry=registry), det,
                       Gateway(s, backend_cls(s), Router(s, registry=registry), registry),
                       co_cfg_provider=lambda: co, ccr=CCRStore(3600), registry=registry)


def _rates(s):
    p = s.pricing.models[s.pricing.baseline_model]
    assert p.cached_input == 0.30
    return p


def test_baseline_unreported_cache_is_unchanged():
    s, pipe = _pipe()
    p = _rates(s)
    assert pipe._baseline(1000, 200, None) == (1000 * p.input + 200 * p.output) / 1e6
    assert pipe._baseline(1000, 200) == pipe._baseline(1000, 200, None)


def test_baseline_prices_cached_share_at_cached_rate():
    s, pipe = _pipe()
    p = _rates(s)
    got = pipe._baseline(1000, 200, 400)
    assert got == pytest.approx((400 * p.cached_input + 600 * p.input + 200 * p.output) / 1e6)
    assert got < pipe._baseline(1000, 200, None)
    assert pipe._baseline(1000, 200, 0) == pipe._baseline(1000, 200, None)


def test_baseline_cached_above_input_clamps():
    s, pipe = _pipe()
    p = _rates(s)
    assert pipe._baseline(100, 10, 5000) == pytest.approx(
        (100 * p.cached_input + 10 * p.output) / 1e6)


class _CachedBackend(_DeterministicLiteLLM):
    CACHED = 3

    async def generate(self, *a, **k):
        r = await super().generate(*a, **k)
        return r.model_copy(update={"cached_prompt_tokens": self.CACHED}) \
            if hasattr(r, "model_copy") else r


def _run(backend_cls):
    _, pipe = _pipe(backend_cls)
    req = ChatRequest(messages=[Message(role="user", content="what is the capital of France today please")])
    return asyncio.run(pipe.run("req-b75", "acme", req))


def test_pipeline_stamps_lower_baseline_when_cache_reported():
    plain = _run(_DeterministicLiteLLM)
    cached = _run(_CachedBackend)
    assert cached["usage"]["prompt_tokens"] == plain["usage"]["prompt_tokens"]
    assert cached["cost"]["baseline_usd"] < plain["cost"]["baseline_usd"]
