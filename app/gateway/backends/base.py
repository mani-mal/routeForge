from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from pydantic import BaseModel, Field

from app.config.schema import PriceCfg
from app.models import GenerationResult, Message


def count_tokens(text: str | None) -> int:
    # `text or ""`: a tool-calling round-trip carries messages whose content is
    # None (an assistant turn that only requested tools), and every token-count
    # call site funnels through here — coercing None once keeps them all safe.
    return max(1, len(text or "") // 4)


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


@dataclass
class StreamChunk:
    """One item off a backend token stream (step 8, real streaming path).

    A chunk is EITHER a token delta (``delta`` set, ``final`` None) OR the
    terminal chunk carrying the completed ``GenerationResult`` (``final`` set,
    ``delta`` None). The terminal chunk's ``GenerationResult`` has
    ``confidence=None`` -- logprobs are unavailable while streaming -- and its
    ``cost_usd`` is priced from ``pricing.yaml`` (never litellm.completion_cost),
    exactly like the non-streaming ``generate()`` path.
    """
    delta: str | None = None
    final: GenerationResult | None = None


class InferenceBackend(Protocol):
    name: str

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict,
                        tools: list[dict] | None = None,
                        tool_choice: str | dict | None = None) -> GenerationResult: ...

    def generate_stream(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict,
                        tools: list[dict] | None = None,
                        tool_choice: str | dict | None = None) -> AsyncIterator[StreamChunk]:
        """Real token streaming (step 8). Yields token-delta ``StreamChunk``s in
        order, then exactly one terminal ``StreamChunk`` carrying ``final``.
        Mirrors ``generate()``'s call construction but with ``stream=True`` and
        no logprobs (so ``final.confidence`` is None)."""
        ...

    async def health(self) -> BackendHealth: ...
