"""The one place a call's USD cost is computed.

This formula used to exist in three copies -- `Router._est_cost` and both the
streaming and non-streaming paths of `LiteLLMBackend` -- which is how a fourth
pricing axis (cached prompt tokens) would have silently applied in two places
out of three. Every caller now funnels through `price_call`.
"""
from __future__ import annotations

from app.config.schema import PriceCfg


def price_call(price: PriceCfg, *, prompt_tokens: int, completion_tokens: int,
               cached_prompt_tokens: int = 0) -> float:
    """USD for one call.

    `cached_prompt_tokens` are billed at `price.cached_input` when the operator
    declared one, and at the full `price.input` rate otherwise. The cached count
    is clamped into `[0, prompt_tokens]`: providers occasionally over-report it,
    and an unclamped value would produce negative uncached tokens and a cost
    below zero.
    """
    cached = max(0, min(int(cached_prompt_tokens), int(prompt_tokens)))
    uncached = int(prompt_tokens) - cached
    cached_rate = price.input if price.cached_input is None else price.cached_input
    return (uncached * price.input
            + cached * cached_rate
            + int(completion_tokens) * price.output) / 1_000_000
