from __future__ import annotations

from dataclasses import dataclass

from app.gateway.backends.base import count_tokens
from app.models import ExecutionPolicyContext, Message


@dataclass
class CompressionResult:
    messages: list[Message]
    tokens_before: int
    tokens_after: int
    record: dict


def compress(messages: list[Message], ctx: ExecutionPolicyContext) -> CompressionResult:
    """Headroom STUB — pass-through. Router prices tokens_after (== before now)."""
    n = sum(count_tokens(m.content) for m in messages)
    record = {"enabled": False, "applied": False, "bypass_reason": "disabled",
              "tokens_before": n, "tokens_after": n, "reduction_pct": 0.0,
              "ccr_retrievals_to_cloud": 0}
    return CompressionResult(messages=messages, tokens_before=n, tokens_after=n,
                             record=record)
