from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.config.schema import PriceCfg
from app.models import GenerationResult, Message


def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# Per-tier modeled latency for the mock/demo path. Single-sourced here so the
# direct path (MockBackend.generate) and the agent per-hop path
# (app/agent/internal_endpoint.py) attribute the SAME latency to a given tier —
# otherwise multi-step requests report 0 ms because the internal endpoint fakes
# the completion (it never calls generate) and so has no backend latency to sum.
TIER_LATENCY_MS = {"cheap": 200.0, "mid": 500.0, "frontier": 2000.0}


def seeded_latency_ms(tier: str) -> float:
    return TIER_LATENCY_MS.get(tier, 500.0)


class BackendHealth(BaseModel):
    ok: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)


class InferenceBackend(Protocol):
    name: str

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult: ...

    async def health(self) -> BackendHealth: ...
