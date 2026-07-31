"""Offline stand-in for litellm.acompletion. Two ways to monkeypatch
app.gateway.backends.litellm_backend.acompletion in tests:

- `fake_acompletion` — a bare async function. Use directly via
  `monkeypatch.setattr(litellm_backend, "acompletion", fake_acompletion)`
  when a default canned `make_response()` is fine.
- `make_recording_acompletion(response)` — a factory. Use via
  `monkeypatch.setattr(litellm_backend, "acompletion",
  make_recording_acompletion(resp))` when the test needs a specific
  `FakeResponse` (tool_calls / logprobs / token counts).

Both record every call's kwargs in the module-level CALLS list so tests can
assert the pinned model string / kwargs the backend passed. Clear CALLS in a
fixture between tests.

`_DeterministicLiteLLM` is a third seam: a `LiteLLMBackend` subclass that
overrides `generate` to reproduce the old `MockBackend`'s seeded
tokens/cost/confidence logic (hashed on model+prompt, `hint["mock_confidence"]`,
`hint["mock_fail"]`) without ever calling `acompletion` — it's what the
cost/two-lever/display/controls tests (and Task 6's `test_traffic_honesty`)
use in place of `MockBackend(settings)`."""
from __future__ import annotations

import hashlib

from app.gateway.backends.base import count_tokens, seeded_latency_ms
from app.gateway.backends.litellm_backend import LiteLLMBackend
from app.models import GenerationResult

CALLS: list[dict] = []


class FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def make_response(*, content: str | None = "ok", tool_calls: list[dict] | None = None,
                  prompt_tokens: int = 10, completion_tokens: int = 5,
                  logprobs: list[float] | None = None) -> FakeResponse:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    lp = None
    if logprobs is not None:
        lp = {"content": [{"logprob": v} for v in logprobs]}
    return FakeResponse({
        "choices": [{"message": message, "logprobs": lp}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


async def fake_acompletion(**kwargs) -> FakeResponse:
    """Drop-in default fake: `monkeypatch.setattr(litellm_backend,
    "acompletion", fake_acompletion)` — no wrapper, no partial. Records the
    call and returns a default make_response()."""
    CALLS.append(kwargs)
    return make_response()


def make_recording_acompletion(response: FakeResponse):
    """Factory for a fake that returns a specific FakeResponse:
    `monkeypatch.setattr(litellm_backend, "acompletion",
    make_recording_acompletion(resp))`."""
    async def _call(**kwargs):
        CALLS.append(kwargs)
        return response
    return _call


class _DeterministicLiteLLM(LiteLLMBackend):
    """Drop-in replacement for the deleted `MockBackend(settings)` in tests.

    Overrides `generate` to reproduce the old seeded logic exactly (same
    hash-derived token counts / cost, same `hint["mock_confidence"]` and
    `hint["mock_fail"]` behaviour) — never calls `acompletion`, so it needs
    no litellm monkeypatch. Used by cost/two-lever/display/controls tests
    (and Task 6's test_traffic_honesty) that need deterministic, hashed
    generation rather than a canned `make_response()`.
    """

    name = "mock"  # cosmetic; nothing in the pipeline branches on backend.name

    async def generate(self, model, messages, price, tier, hint, tools=None):
        if hint.get("mock_fail"):
            raise RuntimeError("mock backend forced failure")
        prompt = "\n".join(m.content for m in messages)
        seed = int(hashlib.sha256((model + prompt).encode()).hexdigest(), 16)
        tokens_in = sum(count_tokens(m.content) for m in messages)
        tokens_out = 40 + seed % 161
        cost = (tokens_in * price.input + tokens_out * price.output) / 1_000_000
        confidence = float(hint.get("mock_confidence", 0.95))
        return GenerationResult(
            model=model, text=f"[mock:{model}] answer",
            tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=seeded_latency_ms(tier), cost_usd=cost,
            confidence=confidence)
