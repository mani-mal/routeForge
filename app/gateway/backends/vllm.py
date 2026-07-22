"""vLLM backend — thin OpenAI-compatible adapter (design §10.6, Phase 9 Task 2).

Speaks the OpenAI `/v1/chat/completions` + `/v1/models` surface that vLLM's
built-in server exposes. Offline-tested via httpx.MockTransport; point
`cfg.base_url` at a real vLLM server (e.g. http://vllm:8000/v1) on the DGX
for the real thing -- no code change required.

The actual OpenAI-mapping (request shape, usage/cost/confidence parsing) is
factored into `openai_compat.py` and shared with `DynamoBackend` -- this
module only supplies vLLM's paths and client construction.
"""

from __future__ import annotations

import httpx

from app.config.schema import PriceCfg, VllmCfg
from app.gateway.backends.base import BackendHealth
from app.gateway.backends.openai_compat import openai_generate, openai_health
from app.models import GenerationResult, Message


class VllmBackend:
    name = "vllm"

    def __init__(self, cfg: VllmCfg, *, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.timeout_s)

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult:
        return await openai_generate(self._client, "chat/completions", model, messages, price, tier)

    async def health(self) -> BackendHealth:
        return await openai_health(self._client, "models")
