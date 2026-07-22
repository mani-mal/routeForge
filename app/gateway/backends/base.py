from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.config.schema import PriceCfg
from app.models import GenerationResult, Message


def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class BackendHealth(BaseModel):
    ok: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)


class InferenceBackend(Protocol):
    name: str

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult: ...

    async def health(self) -> BackendHealth: ...
