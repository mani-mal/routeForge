from __future__ import annotations

from typing import Protocol

from app.config.schema import PriceCfg
from app.models import GenerationResult, Message


def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class InferenceBackend(Protocol):
    name: str

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult: ...
