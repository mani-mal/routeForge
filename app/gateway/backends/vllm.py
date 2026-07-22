"""vLLM backend — thin OpenAI-compatible adapter (design §10.6, Phase 9 Task 2).

Speaks the OpenAI `/v1/chat/completions` + `/v1/models` surface that vLLM's
built-in server exposes. Offline-tested via httpx.MockTransport; point
`cfg.base_url` at a real vLLM server (e.g. http://vllm:8000/v1) on the DGX
for the real thing -- no code change required.
"""

from __future__ import annotations

import math
import time

import httpx

from app.config.schema import PriceCfg, VllmCfg
from app.gateway.backends.base import BackendHealth
from app.models import GenerationResult, Message

# Documented default confidence when the server doesn't return logprobs
# (e.g. logprobs disabled server-side) -- a safe, non-committal signal.
_DEFAULT_CONFIDENCE = 0.9


class VllmBackend:
    name = "vllm"

    def __init__(self, cfg: VllmCfg, *, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.timeout_s)

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult:
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "logprobs": True,
        }
        start = time.monotonic()
        resp = await self._client.post("chat/completions", json=payload)
        latency_ms = (time.monotonic() - start) * 1000
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        text = choice["message"]["content"]

        usage = data.get("usage") or {}
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

        cost_usd = (tokens_in * price.input + tokens_out * price.output) / 1_000_000

        logprobs = (choice.get("logprobs") or {}).get("content")
        if logprobs:
            mean_logprob = sum(t["logprob"] for t in logprobs) / len(logprobs)
            confidence = min(1.0, math.exp(mean_logprob))
        else:
            confidence = _DEFAULT_CONFIDENCE

        return GenerationResult(
            model=model, text=text,
            tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=latency_ms, cost_usd=cost_usd,
            confidence=confidence)

    async def health(self) -> BackendHealth:
        try:
            resp = await self._client.get("models")
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data["data"]]
            return BackendHealth(ok=True, models=models)
        except Exception as e:
            return BackendHealth(ok=False, detail=str(e))
