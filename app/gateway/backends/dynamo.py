"""Dynamo + TensorRT-LLM backend — thin OpenAI-compatible adapter (design §10.7, Phase 9 Task 3).

NVIDIA Dynamo's frontend is OpenAI-compatible, so this adapter differs from
`VllmBackend` only in the base URL / path prefix: `cfg.endpoint` has NO `/v1`
suffix (unlike vLLM's `base_url`), so paths are `v1/chat/completions` and
`v1/models` relative to it. Offline-tested via httpx.MockTransport; point
`cfg.endpoint` at a real Dynamo frontend (e.g. http://dynamo-frontend:8080)
on the DGX for the real thing -- no code change required.

Deploying Dynamo itself (K8s, multi-GPU router modes, disaggregated serving)
is NOT hand-rolled here -- use NVIDIA's verified skills instead
(dynamo-router-starter, dynamo-recipe-runner, dynamo-troubleshoot,
dynamo-interconnect-check; see the inference-backends skill).
"""

from __future__ import annotations

import httpx

from app.config.schema import DynamoCfg, PriceCfg
from app.gateway.backends.base import BackendHealth
from app.gateway.backends.openai_compat import openai_generate, openai_health
from app.models import GenerationResult, Message


class DynamoBackend:
    name = "dynamo"

    def __init__(self, cfg: DynamoCfg, *, client: httpx.AsyncClient | None = None,
                 logprob_temperature: float = 1.0) -> None:
        self._cfg = cfg
        # Confidence calibration (routing.confidence_logprob_temperature) — passed
        # through to openai_generate; the factory wires the configured value.
        self._logprob_temperature = logprob_temperature
        self._client = client or httpx.AsyncClient(base_url=cfg.endpoint, timeout=cfg.timeout_s)

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult:
        return await openai_generate(self._client, "v1/chat/completions", model, messages,
                                     price, tier, self._logprob_temperature)

    async def health(self) -> BackendHealth:
        return await openai_health(self._client, "v1/models")
