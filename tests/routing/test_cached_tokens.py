from types import SimpleNamespace

import pytest

from app.gateway.backends.litellm_backend import _cached_tokens, _usage_dict


def test_usage_dict_reads_cached_tokens_from_a_dict_shape():
    u = {"prompt_tokens": 1000, "completion_tokens": 50,
         "prompt_tokens_details": {"cached_tokens": 768}}
    assert _usage_dict(u) == {"prompt_tokens": 1000, "completion_tokens": 50,
                              "cached_tokens": 768}


def test_usage_dict_reads_cached_tokens_from_an_object_shape():
    u = SimpleNamespace(prompt_tokens=1000, completion_tokens=50,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=768))
    assert _usage_dict(u)["cached_tokens"] == 768


def test_a_provider_that_reports_no_details_yields_none():
    """B75: "not reported" is a different fact from "0 cached"."""
    u = {"prompt_tokens": 1000, "completion_tokens": 50}
    assert _usage_dict(u)["cached_tokens"] is None


def test_a_null_details_block_yields_none():
    u = {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": None}
    assert _usage_dict(u)["cached_tokens"] is None


@pytest.mark.parametrize("make", [
    lambda d: d,
    lambda d: SimpleNamespace(prompt_tokens_details=(
        None if d is None else SimpleNamespace(**d))),
    lambda d: SimpleNamespace(model_dump=lambda: {"prompt_tokens_details": d},
                              prompt_tokens_details=(
                                  None if d is None else SimpleNamespace(**d))),
], ids=["dict", "object", "pydantic-like"])
def test_cached_tokens_none_zero_and_value_on_every_shape(make):
    def wrap(d):
        u = make(d)
        return {"prompt_tokens_details": d} if isinstance(u, dict) else u
    assert _cached_tokens(wrap(None)) is None            # absent details
    assert _cached_tokens(wrap({})) is None              # details, no cached_tokens
    assert _cached_tokens(wrap({"cached_tokens": None})) is None
    assert _cached_tokens(wrap({"cached_tokens": 0})) == 0
    assert _cached_tokens(wrap({"cached_tokens": 1234})) == 1234
