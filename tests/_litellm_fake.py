"""Offline stand-in for litellm.acompletion. Monkeypatch
app.gateway.backends.litellm_backend.acompletion with fake_acompletion (or a
functools.partial of it) in tests. Records every call in CALLS so tests can
assert the pinned model string / kwargs the backend passed."""
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


def fake_acompletion(*, response: FakeResponse | None = None, **kwargs):
    async def _call(**call_kwargs):
        CALLS.append(call_kwargs)
        return response if response is not None else make_response()
    # Support both patch styles: partial(response=...) OR direct monkeypatch.
    if response is not None or not kwargs:
        return _call(**kwargs) if kwargs else _call
    return _call
