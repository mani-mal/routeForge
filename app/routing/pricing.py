"""The one place a call's USD cost is computed.

This formula used to exist in three copies -- `Router._est_cost` and both the
streaming and non-streaming paths of `LiteLLMBackend` -- which is how a fourth
pricing axis (cached prompt tokens) would have silently applied in two places
out of three. Every caller now funnels through `price_call`.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.schema import PriceCfg


def price_call(price: PriceCfg, *, prompt_tokens: int, completion_tokens: int,
               cached_prompt_tokens: int | None = 0) -> float:
    """USD for one call.

    `cached_prompt_tokens` are billed at `price.cached_input` when the operator
    declared one, and at the full `price.input` rate otherwise. The cached count
    is clamped into `[0, prompt_tokens]`: providers occasionally over-report it,
    and an unclamped value would produce negative uncached tokens and a cost
    below zero.
    """
    cached = max(0, min(int(cached_prompt_tokens or 0), int(prompt_tokens)))
    uncached = int(prompt_tokens) - cached
    cached_rate = price.input if price.cached_input is None else price.cached_input
    return (uncached * price.input
            + cached * cached_rate
            + int(completion_tokens) * price.output) / 1_000_000


@dataclass(frozen=True)
class Savings:
    """The three savings levers against one uncached baseline (B0)."""

    uncached_baseline_usd: float   # B0
    compression_saved_usd: float   # B0 - B1, >= 0
    routing_saved_usd: float       # B1 - C0, may be negative
    prompt_cache_saved_usd: float  # C0 - C, >= 0


def split_savings(*, baseline: PriceCfg, served: PriceCfg | None,
                  tokens_before: int, tokens_after: int,
                  served_prompt_tokens: int, completion_tokens: int,
                  cached_prompt_tokens: int | None, total_usd: float) -> Savings:
    """Split B0 - total into compression, routing and prompt-cache savings.

    Telescoping order: B0 -> B1 (compression) -> C0 (routing) -> C (prompt
    cache), so compression + routing + prompt_cache == B0 - total_usd exactly.
    B0 prices `tokens_before` and B1 `tokens_after` at the baseline model, both
    uncached. C0 is the served cost had nothing been cached. Routing is NOT
    clamped: a dearer served model yields a negative figure. Compression is
    clamped at 0. Prompt-cache saving is 0 when the served price is unknown or
    the provider reported no cached count.
    """
    b0 = price_call(baseline, prompt_tokens=tokens_before,
                    completion_tokens=completion_tokens, cached_prompt_tokens=0)
    b1 = price_call(baseline, prompt_tokens=tokens_after,
                    completion_tokens=completion_tokens, cached_prompt_tokens=0)
    cache = 0.0
    if served is not None and cached_prompt_tokens is not None:
        cache = (price_call(served, prompt_tokens=served_prompt_tokens,
                            completion_tokens=completion_tokens,
                            cached_prompt_tokens=0)
                 - price_call(served, prompt_tokens=served_prompt_tokens,
                              completion_tokens=completion_tokens,
                              cached_prompt_tokens=cached_prompt_tokens))
    c0 = total_usd + cache
    compression = max(0.0, b0 - b1)
    # Keep the identity exact if compression was clamped (after > before).
    routing = (b0 - c0) - compression
    return Savings(uncached_baseline_usd=b0, compression_saved_usd=compression,
                   routing_saved_usd=routing, prompt_cache_saved_usd=cache)
