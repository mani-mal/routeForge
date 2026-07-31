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
fixture between tests."""
from __future__ import annotations

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
