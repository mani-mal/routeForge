from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.config.schema import PriceCfg
from app.models import GenerationResult, Message


def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# Per-tier modeled latency, used ONLY by the deterministic test fake
# (tests/_litellm_fake.py's _DeterministicLiteLLM.generate) so a given tier
# gets a stable, non-zero latency in tests. The real LiteLLMBackend.generate
# measures actual wall-clock latency, and the agent per-hop path
# (app/agent/internal_endpoint.py) now makes a REAL backend.generate call and
# records that measured latency -- so multi-step requests report real per-hop
# latency, not 0 ms (B5, resolved). This table is the single source the fake
# reads; production never uses it.
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
                        tier: str, hint: dict,
                        tools: list[dict] | None = None) -> GenerationResult: ...

    async def health(self) -> BackendHealth: ...
