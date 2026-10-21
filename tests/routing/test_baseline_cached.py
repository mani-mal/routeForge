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
        return r.model_copy(update={"cached_prompt_tokens": self.CACHED})


def _run(backend_cls):
    _, pipe = _pipe(backend_cls)
    req = ChatRequest(messages=[Message(role="user", content="what is the capital of France today please")])
    return asyncio.run(pipe.run("req-b75", "acme", req))


def test_pipeline_stamps_lower_baseline_when_cache_reported():
    plain = _run(_DeterministicLiteLLM)
    cached = _run(_CachedBackend)
    assert cached["usage"]["prompt_tokens"] == plain["usage"]["prompt_tokens"]
    assert cached["cost"]["baseline_usd"] < plain["cost"]["baseline_usd"]


# --- final-review I1: cached share scaled once onto the post-compression basis ---

def test_cached_count_is_a_share_scaled_to_tokens_after():
    f = Pipeline._baseline_cached_count
    assert f(28000, 30000, 10000) == round(28000 / 30000 * 10000)
    assert f(None, 30000, 10000) is None
    assert f(5, 0, 10000) is None
    assert f(0, 30000, 10000) == 0


def test_cached_greater_than_tokens_after_and_than_tokens_in_clamps():
    f = Pipeline._baseline_cached_count
    assert f(28000, 30000, 100) == round(28000 / 30000 * 100)  # <= tokens_after
    assert f(99999, 30000, 100) == 100                           # share clamped to 1
    assert f(-5, 30000, 100) == 0


def _long_compressing_pipe(backend_cls):
    from app.config.loader import load_settings
    s = load_settings()
    bypass = s.serving.context_optimization.bypass_on.model_copy(update={"user_prompt": False})
    co = s.serving.context_optimization.model_copy(
        update={"enabled": True, "min_tokens": 1, "bypass_on": bypass})
    registry = make_test_registry(s)
    det = make_detector("regex")
    return s, Pipeline(s, Gate(s, det, registry=registry), det,
                       Gateway(s, backend_cls(s), Router(s, registry=registry), registry),
                       co_cfg_provider=lambda: co, ccr=CCRStore(3600), registry=registry)


class _BigCachedBackend(_DeterministicLiteLLM):
    # Far more cached tokens than the (compressed) content estimate.
    async def generate(self, *a, **k):
        r = await super().generate(*a, **k)
        return r.model_copy(update={"cached_prompt_tokens": r.tokens_in * 10})


class _HalfCachedBackend(_DeterministicLiteLLM):
    async def generate(self, *a, **k):
        r = await super().generate(*a, **k)
        return r.model_copy(update={"cached_prompt_tokens": r.tokens_in // 2})


def _run_compressed(backend_cls):
    _, pipe = _long_compressing_pipe(backend_cls)
    req = ChatRequest(messages=[Message(role="user", content="INFO log line\n" * 800)])
    return asyncio.run(pipe.run("req-b75c", "acme", req))


@pytest.mark.parametrize("cls", [_CachedBackend, _HalfCachedBackend, _BigCachedBackend])
def test_compression_saved_is_independent_of_caching(cls):
    plain = _run_compressed(_DeterministicLiteLLM)
    cached = _run_compressed(cls)
    assert plain["cost"]["compression_saved_usd"] > 0
    assert cached["cost"]["compression_saved_usd"] == pytest.approx(
        plain["cost"]["compression_saved_usd"])


@pytest.mark.parametrize("cls", [_DeterministicLiteLLM, _HalfCachedBackend, _BigCachedBackend])
def test_levers_sum_to_baseline_minus_total(cls):
    c = _run_compressed(cls)["cost"]
    assert c["compression_saved_usd"] + c["routing_saved_usd"] == pytest.approx(
        c["baseline_usd"] - c["total_usd"])


def test_streaming_finaliser_uses_the_same_scaled_count(monkeypatch):
    from types import SimpleNamespace
    from app.gateway.gateway import GenerationResult
    s, pipe = _pipe()
    for name in ("_policy_mode_stamp", "_endpoint_registry_version",
                 "_endpoint_declaration", "_candidate_by_name", "_served_scoring",
                 "_eligibility_checks", "_display_name"):
        monkeypatch.setattr(pipe, name, lambda *a, **k: None)
    monkeypatch.setattr(pipe, "_certificate", lambda *a, **k: {})
    comp = SimpleNamespace(tokens_before=15000, tokens_after=10000,
                           record=SimpleNamespace(model_dump=lambda: {"step": "compression"}))
    ctx = SimpleNamespace(require_verification=False, sensitivity="public", onprem_only=False,
                          allowed_providers=set(), detected_entities=(),
                          app_policy_version="v", content_integrity="transformations_allowed")

    def meta(cached):
        res = GenerationResult(model="m", text="t", tokens_in=30000, tokens_out=100,
                               latency_ms=1.0, cost_usd=0.0, cached_prompt_tokens=cached)
        return pipe._assemble_stream_meta("r", ctx, "x", comp, [], res, None, None)["cost"]

    plain, cached = meta(None), meta(28000)
    p = _rates(s)
    assert plain["compression_saved_usd"] == pytest.approx(5000 * p.input / 1e6)
    assert cached["compression_saved_usd"] == pytest.approx(plain["compression_saved_usd"])
    assert cached["baseline_usd"] < plain["baseline_usd"]
