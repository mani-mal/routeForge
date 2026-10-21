import pytest

from app.config.schema import PriceCfg
from app.routing.pricing import price_call


def _p(**kw) -> PriceCfg:
    base = {"input": 3.0, "output": 15.0, "tier": "frontier", "provider": "bedrock"}
    base.update(kw)
    return PriceCfg(**base)


def _legacy(price, tokens_in, tokens_out):
    """The formula as it existed in three places before this task."""
    return (tokens_in * price.input + tokens_out * price.output) / 1_000_000


def test_no_cached_rate_declared_matches_the_legacy_formula_exactly():
    price = _p()
    assert price_call(price, prompt_tokens=1000, completion_tokens=200) == \
        _legacy(price, 1000, 200)


def test_cached_tokens_bill_at_full_input_rate_when_no_rate_declared():
    """RouteForge never invents a discount."""
    price = _p()
    assert price_call(price, prompt_tokens=1000, completion_tokens=200,
                      cached_prompt_tokens=800) == _legacy(price, 1000, 200)


def test_declared_cached_rate_applies_to_the_cached_portion_only():
    price = _p(cached_input=0.30)
    got = price_call(price, prompt_tokens=1000, completion_tokens=200,
                     cached_prompt_tokens=800)
    expected = (200 * 3.0 + 800 * 0.30 + 200 * 15.0) / 1_000_000
    assert got == pytest.approx(expected)


def test_cached_equal_to_input_rate_is_the_onprem_declaration_and_changes_nothing():
    price = _p(cached_input=3.0)
    assert price_call(price, prompt_tokens=1000, completion_tokens=200,
                      cached_prompt_tokens=800) == pytest.approx(_legacy(price, 1000, 200))


def test_cached_tokens_are_clamped_to_the_prompt_total():
    """A provider that over-reports must never produce negative uncached tokens."""
    price = _p(cached_input=0.0)
    got = price_call(price, prompt_tokens=100, completion_tokens=0,
                     cached_prompt_tokens=999)
    assert got == pytest.approx(0.0)
    assert got >= 0.0


def test_negative_cached_is_treated_as_zero():
    price = _p(cached_input=0.0)
    assert price_call(price, prompt_tokens=100, completion_tokens=0,
                      cached_prompt_tokens=-5) == pytest.approx(_legacy(price, 100, 0))


def test_unknown_cached_count_prices_like_zero():
    """B75: None (provider did not report) bills at full input -- no discount."""
    p = _p(cached_input=0.3)
    assert price_call(p, prompt_tokens=1000, completion_tokens=100,
                      cached_prompt_tokens=None) == price_call(
        p, prompt_tokens=1000, completion_tokens=100, cached_prompt_tokens=0)
