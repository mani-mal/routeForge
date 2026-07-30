from __future__ import annotations

import hashlib

from app.config.schema import PriceCfg, Settings
from app.gateway.backends.base import BackendHealth, count_tokens, seeded_latency_ms
from app.models import GenerationResult, Message


class MockBackend:
    name = "mock"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult:
        if hint.get("mock_fail"):
            raise RuntimeError("mock backend forced failure")
        prompt = "\n".join(m.content for m in messages)
        seed = int(hashlib.sha256((model + prompt).encode()).hexdigest(), 16)
        tokens_in = sum(count_tokens(m.content) for m in messages)
        tokens_out = 40 + seed % 161
        cost = (tokens_in * price.input + tokens_out * price.output) / 1_000_000
        confidence = float(hint.get("mock_confidence", 0.95))
        return GenerationResult(
            model=model, text=f"[mock:{model}] answer",
            tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=seeded_latency_ms(tier), cost_usd=cost,
            confidence=confidence)

    async def health(self) -> BackendHealth:
        models = [c.name for c in self._settings.routing.model_pool
                  if c.provider == "onprem"]
        return BackendHealth(ok=True, detail="mock", models=models)
