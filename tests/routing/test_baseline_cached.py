"""The all-frontier baseline (B0) is UNCACHED; provider prompt caching is its
own savings lever (prompt_cache_saved_usd)."""
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


def test_baseline_is_uncached():
    s, pipe = _pipe()
    p = _rates(s)
    assert pipe._baseline(1000, 200) == pytest.approx(
        (1000 * p.input + 200 * p.output) / 1e6)


class _CachedBackend(_DeterministicLiteLLM):
    CACHED = 3

    async def generate(self, *a, **k):
        r = await super().generate(*a, **k)
        return r.model_copy(update={"cached_prompt_tokens": self.CACHED})


def _run(backend_cls):
    _, pipe = _pipe(backend_cls)
    req = ChatRequest(messages=[Message(role="user", content="what is the capital of France today please")])
    return asyncio.run(pipe.run("req-b75", "acme", req))


def test_baseline_is_uncached_and_cache_saving_is_separate():
    s, pipe = _pipe(_CachedBackend)
    # The served on-prem test row declares no cached rate; bill it like a
    # provider that does, so the reported cached tokens earn a discount.
    pipe._served_price = lambda model: s.pricing.models["bedrock-frontier"]
    req = ChatRequest(messages=[Message(role="user", content="what is the capital of France today please")])
    out = asyncio.run(pipe.run("req-b75", "acme", req))
    plain = _run(_DeterministicLiteLLM)
    assert out["cost"]["baseline_usd"] == pytest.approx(plain["cost"]["baseline_usd"])
    assert out["cost"]["prompt_cache_saved_usd"] > 0


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
    assert (c["compression_saved_usd"] + c["routing_saved_usd"]
            + c["prompt_cache_saved_usd"]) == pytest.approx(
        c["baseline_usd"] - c["total_usd"], abs=1e-9)


def _stream_meta(cached):
    from types import SimpleNamespace
    from app.gateway.gateway import GenerationResult
    s, pipe = _pipe()
    pipe._served_price = lambda model: s.pricing.models["bedrock-frontier"]
    for name in ("_policy_mode_stamp", "_endpoint_registry_version",
                 "_endpoint_declaration", "_candidate_by_name", "_served_scoring",
                 "_eligibility_checks", "_display_name"):
        setattr(pipe, name, lambda *a, **k: None)
    pipe._certificate = lambda *a, **k: {}
    comp = SimpleNamespace(tokens_before=15000, tokens_after=10000,
                           record=SimpleNamespace(model_dump=lambda: {"step": "compression"}))
    ctx = SimpleNamespace(require_verification=False, sensitivity="public", onprem_only=False,
                          allowed_providers=set(), detected_entities=(),
                          app_policy_version="v", content_integrity="transformations_allowed")
    res = GenerationResult(model="bedrock-frontier", text="t", tokens_in=30000, tokens_out=100,
                           latency_ms=1.0, cost_usd=0.0, cached_prompt_tokens=cached)
    return pipe._assemble_stream_meta("r", ctx, "x", comp, [], res, None, None)["cost"]


def test_streaming_finaliser_reports_same_levers_as_run():
    s, pipe = _pipe()
    p = _rates(s)
    plain, cached = _stream_meta(None), _stream_meta(28000)
    assert plain["compression_saved_usd"] == pytest.approx(5000 * p.input / 1e6)
    assert cached["compression_saved_usd"] == pytest.approx(plain["compression_saved_usd"])
    assert cached["baseline_usd"] == pytest.approx(plain["baseline_usd"])
    assert plain["prompt_cache_saved_usd"] == 0.0
    assert cached["prompt_cache_saved_usd"] == pytest.approx(
        28000 * (p.input - p.cached_input) / 1e6)
    assert (cached["compression_saved_usd"] + cached["routing_saved_usd"]
            + cached["prompt_cache_saved_usd"]) == pytest.approx(
        cached["baseline_usd"] - cached["total_usd"], abs=1e-9)


def test_semantic_cache_hit_saving_is_whole_uncached_baseline():
    from tests.test_semantic_cache_pipeline import _base_settings, _pipeline
    from tests.test_traffic_honesty import RecordingBackend
    settings = _base_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, _ = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])
    asyncio.run(pipe.run("r1", "acme", req))
    hit = asyncio.run(pipe.run("r2", "acme", req))["cost"]
    assert hit["total_usd"] == 0.0
    assert hit["baseline_usd"] > 0
    assert hit["cache_saved_usd"] == pytest.approx(hit["baseline_usd"])
    assert hit["routing_saved_usd"] == 0.0
    assert hit["compression_saved_usd"] == 0.0
    assert hit["prompt_cache_saved_usd"] == 0.0


# --- the real served-price lookup (no monkeypatching of _served_price) ---

def _pipe_with_cached_onprem_row():
    """A pipeline whose registry also holds an onboarded on-prem row that
    declares its own cached-input rate."""
    from app.apps.bootstrap import seed_test_models
    from app.apps.db import Database
    from app.apps import store
    from app.apps.registry import ModelRegistry

    s = load_settings()

    async def _build():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as session:
            await seed_test_models(session)
            await store.upsert_onboarded(
                session, "onprem-cached", display_name="Cached", provider="onprem",
                tier="cheap", endpoint="http://test-local/v1", api_key_env=None,
                price_input=0.40, price_output=0.80, price_cached_input=0.10,
                enabled=True, probe_status="healthy", allowed_app_ids=None, notes="t")
            await session.commit()
        reg = ModelRegistry(s)
        await reg.refresh(db)
        return reg

    registry = asyncio.run(_build())
    det = make_detector("regex")
    co = s.serving.context_optimization
    return s, Pipeline(s, Gate(s, det, registry=registry), det,
                       Gateway(s, _DeterministicLiteLLM(s), Router(s, registry=registry), registry),
                       co_cfg_provider=lambda: co, ccr=CCRStore(3600), registry=registry)


def _savings_for(pipe, model, cached):
    from types import SimpleNamespace
    from app.gateway.gateway import GenerationResult
    comp = SimpleNamespace(tokens_before=1000, tokens_after=1000)
    res = GenerationResult(model=model, text="t", tokens_in=1000, tokens_out=100,
                           latency_ms=1.0, cost_usd=0.0, cached_prompt_tokens=cached)
    from app.routing.pricing import price_call
    p = pipe._served_price(model)
    total = price_call(p, prompt_tokens=1000, completion_tokens=100,
                       cached_prompt_tokens=cached) if p is not None else 0.0
    return pipe._savings(comp, res, total)


def test_served_price_uses_onboarded_registry_row():
    s, pipe = _pipe_with_cached_onprem_row()
    p = pipe._served_price("onprem-cached")
    assert (p.input, p.output, p.cached_input) == (0.40, 0.80, 0.10)
    sv = _savings_for(pipe, "onprem-cached", cached=600)
    # The saving uses THIS model's rates, not the baseline's.
    assert sv.prompt_cache_saved_usd == pytest.approx(600 * (0.40 - 0.10) / 1e6)


def test_served_price_uses_yaml_row_for_cloud_model():
    s, pipe = _pipe()
    assert pipe._served_price("bedrock-frontier") is s.pricing.models["bedrock-frontier"]
    p = s.pricing.models["bedrock-frontier"]
    sv = _savings_for(pipe, "bedrock-frontier", cached=600)
    assert sv.prompt_cache_saved_usd == pytest.approx(600 * (p.input - p.cached_input) / 1e6)


def test_served_price_unknown_model_is_none_and_cache_saving_zero():
    s, pipe = _pipe()
    assert pipe._served_price("no-such-model") is None
    assert _savings_for(pipe, "no-such-model", cached=600).prompt_cache_saved_usd == 0.0
