"""Shared OpenAI-compatible mapping helper (design §10.6/§10.7, Phase 9 Task 3).

Both `VllmBackend` and `DynamoBackend` speak the same OpenAI
`/v1/chat/completions` + `/v1/models` surface -- they differ only in base
URL / path prefix. This module is the single source of that mapping so the
two adapters never carry duplicated logic.
"""

from __future__ import annotations

import math
import time

import httpx

from app.config.schema import PriceCfg
from app.gateway.backends.base import BackendHealth
from app.models import GenerationResult, Message

# Documented default confidence when the server doesn't return logprobs
# (e.g. logprobs disabled server-side) -- a safe, non-committal signal.
# Above confidence_escalation_threshold so a missing signal never escalates
# (routing-and-gateway skill: "fail toward not spending more").
DEFAULT_CONFIDENCE = 0.9


async def openai_generate(client: httpx.AsyncClient, chat_path: str, model: str,
                           messages: list[Message], price: PriceCfg, tier: str,
                           logprob_temperature: float = 1.0) -> GenerationResult:
    payload = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "logprobs": True,
    }
    start = time.monotonic()
    resp = await client.post(chat_path, json=payload)
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
        # exp(mean_logprob) is the geometric-mean per-token probability, which
        # sits ~0.4-0.6 for perfectly fluent text and would trip the escalation
        # threshold on almost every answer. Soften by a configured temperature
        # (routing.confidence_logprob_temperature) so only genuinely uncertain
        # (high-perplexity) output escalates — see routeforge-router-targets-mid.
        t = logprob_temperature if logprob_temperature > 0 else 1.0
        confidence = min(1.0, math.exp(mean_logprob / t))
    else:
        confidence = DEFAULT_CONFIDENCE

    return GenerationResult(
        model=model, text=text,
        tokens_in=tokens_in, tokens_out=tokens_out,
        latency_ms=latency_ms, cost_usd=cost_usd,
        confidence=confidence)


async def openai_health(client: httpx.AsyncClient, models_path: str) -> BackendHealth:
    try:
        resp = await client.get(models_path)
        resp.raise_for_status()
        data = resp.json()
        models = [m["id"] for m in data["data"]]
        return BackendHealth(ok=True, models=models)
    except Exception as e:
        return BackendHealth(ok=False, detail=str(e))
