"""Single LiteLLM egress backend (steps 7-8). Replaces the mock/vllm/dynamo
adapters. Called as an SDK strictly downstream of the policy gate (guardrail 1),
pinned to the model the router already chose (guardrail 2 — no litellm.Router,
num_retries=0), and refusing any provider not enabled+mapped (guardrail 3).
Cost is sourced from pricing.yaml, never litellm.completion_cost."""
from __future__ import annotations

import math
import os
import time

from litellm import acompletion

from app.config.schema import PriceCfg, Settings
from app.gateway.backends.base import BackendHealth
from app.models import GenerationResult, Message

DEFAULT_CONFIDENCE = 0.9


class LiteLLMBackend:
    name = "litellm"

    def __init__(self, settings: Settings, *, registry=None,
                 logprob_temperature: float = 1.0) -> None:
        self._s = settings
        self._reg = registry
        self._t = logprob_temperature if logprob_temperature > 0 else 1.0

    def _map(self, model: str) -> tuple[str, dict]:
        # Read the registry's effective pool so onboarded on-prem models (not in
        # the static YAML pool) are resolvable; fall back to the YAML pool.
        pool = (self._reg.effective_pool() if self._reg
                else self._s.routing.model_pool)
        cand = {c.name: c for c in pool}[model]
        pcfg = self._s.serving.providers.get(cand.provider)
        if pcfg is None or not pcfg.enabled or not pcfg.litellm_prefix:
            raise RuntimeError(
                f"provider {cand.provider!r} for model {model!r} is not enabled/mapped "
                "(guardrail 3: cloud must be unreachable by construction)")
        litellm_model = cand.litellm_model or f"{pcfg.litellm_prefix}/{cand.name}"
        kwargs: dict = {}
        # onprem: base_url + optional auth env come from the onboarded model's
        # registered endpoint (per-model reachability probed at onboard time).
        if cand.provider == "onprem":
            ep = self._reg.endpoint_for(cand.name) if self._reg else None
            if ep is None:
                raise RuntimeError(
                    f"onprem model {model!r} has no registered endpoint")
            base, key_env = ep
            kwargs["api_base"] = base
            if key_env:
                kwargs["api_key"] = os.environ.get(key_env, "sk-noauth")
        if pcfg.api_base_env:   # cloud, unchanged
            kwargs["api_base"] = os.environ.get(pcfg.api_base_env) or kwargs.get("api_base")
        if pcfg.api_key_env and "api_key" not in kwargs:
            kwargs["api_key"] = os.environ.get(pcfg.api_key_env, "sk-noauth")
        if pcfg.api_version_env and os.environ.get(pcfg.api_version_env):
            kwargs["api_version"] = os.environ.get(pcfg.api_version_env)
        return litellm_model, kwargs

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict,
                        tools: list[dict] | None = None) -> GenerationResult:
        litellm_model, conn = self._map(model)
        call: dict = {
            "model": litellm_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "logprobs": True,
            "num_retries": 0,          # guardrail 2: no LiteLLM-side retry/routing
            "drop_params": True,       # tolerate providers that reject logprobs/tools
            **conn,
        }
        if tools:
            call["tools"] = tools
        start = time.monotonic()
        resp = await acompletion(**call)
        latency_ms = (time.monotonic() - start) * 1000
        data = resp.model_dump()

        choice = data["choices"][0]
        msg = choice["message"]
        text = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or None

        usage = data.get("usage") or {}
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        cost_usd = (tokens_in * price.input + tokens_out * price.output) / 1_000_000

        logprobs = (choice.get("logprobs") or {}).get("content") if choice.get("logprobs") else None
        if logprobs:
            mean_logprob = sum(t["logprob"] for t in logprobs) / len(logprobs)
            confidence = min(1.0, math.exp(mean_logprob / self._t))
        else:
            confidence = DEFAULT_CONFIDENCE

        return GenerationResult(
            model=model, text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=latency_ms, cost_usd=cost_usd, confidence=confidence,
            tool_calls=tool_calls)

    async def health(self) -> BackendHealth:
        # Per-model reachability is probed at onboard time (registry), not at
        # boot: the single LiteLLM egress path is always "up".
        return BackendHealth(ok=True, detail="litellm")
