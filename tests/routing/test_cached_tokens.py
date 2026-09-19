from types import SimpleNamespace

from app.gateway.backends.litellm_backend import _usage_dict


def test_usage_dict_reads_cached_tokens_from_a_dict_shape():
    u = {"prompt_tokens": 1000, "completion_tokens": 50,
         "prompt_tokens_details": {"cached_tokens": 768}}
    assert _usage_dict(u) == {"prompt_tokens": 1000, "completion_tokens": 50,
                              "cached_tokens": 768}


def test_usage_dict_reads_cached_tokens_from_an_object_shape():
    u = SimpleNamespace(prompt_tokens=1000, completion_tokens=50,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=768))
    assert _usage_dict(u)["cached_tokens"] == 768


def test_a_provider_that_reports_no_details_yields_zero():
    """Indistinguishable from a genuine cache miss -- documented, not guessed."""
    u = {"prompt_tokens": 1000, "completion_tokens": 50}
    assert _usage_dict(u)["cached_tokens"] == 0


def test_a_null_details_block_yields_zero():
    u = {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": None}
    assert _usage_dict(u)["cached_tokens"] == 0
