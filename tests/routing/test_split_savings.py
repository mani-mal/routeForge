import pytest

from app.config.schema import PriceCfg
from app.routing.pricing import price_call, split_savings

BASE = PriceCfg(input=3.0, output=15.0, cached_input=0.30, tier="frontier", provider="bedrock")
SERVED = PriceCfg(input=0.15, output=0.60, cached_input=0.015, tier="mid", provider="azure")


def _s(served=SERVED, cached=4_000, total=None, before=10_000, after=6_000):
    if total is None:
        total = price_call(SERVED, prompt_tokens=6_200, completion_tokens=500,
                           cached_prompt_tokens=cached)
    return split_savings(baseline=BASE, served=served, tokens_before=before,
                         tokens_after=after, served_prompt_tokens=6_200,
                         completion_tokens=500, cached_prompt_tokens=cached,
                         total_usd=total)


def test_identity_holds():
    total = price_call(SERVED, prompt_tokens=6_200, completion_tokens=500,
                       cached_prompt_tokens=4_000)
    s = _s(total=total)
    assert abs(s.compression_saved_usd + s.routing_saved_usd + s.prompt_cache_saved_usd
               - (s.uncached_baseline_usd - total)) < 1e-9


def test_prompt_cache_is_served_model_discount():
    assert _s().prompt_cache_saved_usd == pytest.approx(4_000 * (0.15 - 0.015) / 1e6)


def test_no_cached_rate_means_zero_cache_saving():
    served = PriceCfg(input=0.15, output=0.60, cached_input=None, tier="mid", provider="azure")
    assert _s(served=served).prompt_cache_saved_usd == 0.0


def test_unreported_cache_is_zero():
    assert _s(cached=None, total=0.001).prompt_cache_saved_usd == 0.0


def test_overreported_cache_clamped():
    s = _s(cached=99_999)
    assert s.prompt_cache_saved_usd == pytest.approx(6_200 * 0.135 / 1e6)


def test_dearer_served_model_gives_negative_routing():
    b1 = price_call(BASE, prompt_tokens=6_000, completion_tokens=500)
    s = split_savings(baseline=BASE, served=BASE, tokens_before=10_000,
                      tokens_after=6_000, served_prompt_tokens=6_000,
                      completion_tokens=500, cached_prompt_tokens=0, total_usd=2 * b1)
    assert s.routing_saved_usd < 0
    assert abs(s.compression_saved_usd + s.routing_saved_usd + s.prompt_cache_saved_usd
               - (s.uncached_baseline_usd - 2 * b1)) < 1e-9


def test_no_compression_means_zero_compression():
    assert _s(before=6_000, after=6_000).compression_saved_usd == 0.0
