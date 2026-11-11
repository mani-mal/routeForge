"""B85: the same backend usage payload yields the same token fields whether it
arrives in a non-stream response or in a stream's final usage chunk."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config.loader import load_settings
from app.config.schema import PriceCfg
from app.gateway.backends import litellm_backend
from app.gateway.backends.litellm_backend import LiteLLMBackend
from tests._litellm_fake import (FakeResponse, _FakeStreamChunk, make_test_registry)

_PRICE = PriceCfg(input=0.1, output=0.2, tier="cheap", provider="onprem")

_USAGES = {
    "nested_both": {"prompt_tokens": 100, "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 64},
                    "completion_tokens_details": {"reasoning_tokens": 25}},
    "cached_only": {"prompt_tokens": 100, "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 0}},
    "reasoning_only": {"prompt_tokens": 100, "completion_tokens": 40,
                       "completion_tokens_details": {"reasoning_tokens": 7}},
    "absent": {"prompt_tokens": 100, "completion_tokens": 40},
    "null_details": {"prompt_tokens": 100, "completion_tokens": 40,
                     "prompt_tokens_details": None,
                     "completion_tokens_details": None},
}


def _backend() -> LiteLLMBackend:
    settings = load_settings()
    return LiteLLMBackend(settings, registry=make_test_registry(settings))


def _fields(r) -> tuple:
    return (r.tokens_in, r.tokens_out, r.cached_prompt_tokens, r.reasoning_tokens)


@pytest.mark.parametrize("shape", ["dict", "namespace"])
@pytest.mark.parametrize("name", sorted(_USAGES))
def test_stream_final_matches_non_stream_token_fields(monkeypatch, name, shape):
    usage = _USAGES[name]

    async def _nonstream(**kw):
        return FakeResponse({"choices": [{"message": {"content": "hi"}}], "usage": usage})

    def _as_obj(d):
        return SimpleNamespace(**{k: (SimpleNamespace(**v) if isinstance(v, dict) else v)
                                  for k, v in d.items()})

    stream_usage = usage if shape == "dict" else _as_obj(usage)

    async def _stream(**kw):
        async def _gen():
            yield _FakeStreamChunk(content="hi")
            yield _FakeStreamChunk(empty_choices=True, usage=stream_usage)
        return _gen()

    b = _backend()   # builds its registry with asyncio.run: outside the loop

    async def _run():
        monkeypatch.setattr(litellm_backend, "acompletion", _nonstream)
        plain = await b.generate("local-8b", [], _PRICE, "cheap", {})
        monkeypatch.setattr(litellm_backend, "acompletion", _stream)
        final = None
        async for c in b.generate_stream("local-8b", [], _PRICE, "cheap", {}):
            final = c.final or final
        return plain, final

    plain, final = asyncio.run(_run())
    assert _fields(final) == _fields(plain)
    assert final.cost_usd == plain.cost_usd
    if name == "nested_both":
        assert _fields(final) == (100, 40, 64, 25)
    if name == "absent":
        assert _fields(final) == (100, 40, None, None)
