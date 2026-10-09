"""Single LiteLLM egress backend (steps 7-8). Replaces the mock/vllm/dynamo
adapters. Called as an SDK strictly downstream of the policy gate (guardrail 1),
pinned to the model the router already chose (guardrail 2 — no litellm.Router,
num_retries=0), and refusing any provider not enabled+mapped (guardrail 3).
Cost is sourced from pricing.yaml, never litellm.completion_cost."""
from __future__ import annotations

import math
import os
import re
import time
from typing import AsyncIterator

from litellm import ContextWindowExceededError, acompletion

from app.config.schema import PriceCfg, Settings
from app.errors import ContextWindowExceeded
from app.gateway.backends.base import BackendHealth, StreamChunk
from app.models import GenerationResult, Message
from app.routing.pricing import price_call

DEFAULT_CONFIDENCE = 0.9

# litellm wraps the provider's text as "litellm.XError: litellm.YError:
# OpenAIException - <provider message>"; keep only the provider message.
_LITELLM_PREFIX = re.compile(r"^(?:litellm\.\w+: )*(?:\w+Exception - )?")


async def _acompletion(model: str, call: dict):
    """`acompletion`, with a context-window refusal translated into the
    backend-neutral `ContextWindowExceeded` so the gateway and API can tell
    "request too large" (client's error, 400) from an outage (502)."""
    try:
        return await acompletion(**call)
    except ContextWindowExceededError as exc:
        raise ContextWindowExceeded(
            f"{model}: {_LITELLM_PREFIX.sub('', str(exc))}") from exc


_FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter",
                             "function_call"})


def _finish_reason(value) -> str | None:
    """The backend's own finish reason, kept only if it is one of the known
    enum values -- it goes on the content-free outcome row, so an arbitrary
    string from a backend is recorded as `other`, never verbatim."""
    if value is None:
        return None
    return value if value in _FINISH_REASONS else "other"


def _reasoning_tokens(u) -> int | None:
    """`usage.completion_tokens_details.reasoning_tokens` when the backend
    reports it (a thinking model bills its reasoning as output); else None."""
    det = (u.get("completion_tokens_details") if isinstance(u, dict)
           else getattr(u, "completion_tokens_details", None))
    if det is None:
        return None
    n = det.get("reasoning_tokens") if isinstance(det, dict) else getattr(det, "reasoning_tokens", None)
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def _cached_tokens(u) -> int:
    """`usage.prompt_tokens_details.cached_tokens`, across the dict, pydantic
    and SimpleNamespace shapes litellm hands back. A provider that does not
    report the field yields 0 -- indistinguishable from a real cache miss, which
    is a documented limitation, not an inference."""
    det = (u.get("prompt_tokens_details") if isinstance(u, dict)
           else getattr(u, "prompt_tokens_details", None))
    if det is None:
        return 0
    raw = det.get("cached_tokens") if isinstance(det, dict) else getattr(det, "cached_tokens", 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _usage_dict(u) -> dict:
    """Normalize a litellm usage object (pydantic-ish), a plain dict, or a
    SimpleNamespace-style object into a `{prompt_tokens, completion_tokens,
    cached_tokens}` dict -- streaming's terminal usage chunk shape varies by
    provider."""
    if isinstance(u, dict):
        return {"prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "cached_tokens": _cached_tokens(u)}
    if hasattr(u, "model_dump"):
        d = u.model_dump()
        return {"prompt_tokens": d.get("prompt_tokens", 0),
                "completion_tokens": d.get("completion_tokens", 0),
                "cached_tokens": _cached_tokens(d)}
    return {"prompt_tokens": getattr(u, "prompt_tokens", 0),
            "completion_tokens": getattr(u, "completion_tokens", 0),
            "cached_tokens": _cached_tokens(u)}


def _to_wire(m) -> dict:
    """Serialize a `Message` to the OpenAI wire shape, preserving tool-calling
    structure so a client's agent loop round-trips: an assistant turn keeps its
    `tool_calls`, and a `role:"tool"` result keeps its `tool_call_id`/`name`.
    Ordinary turns serialize to just `{role, content}` exactly as before."""
    d: dict = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = m.tool_calls
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.name:
        d["name"] = m.name
    return d


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
                        tools: list[dict] | None = None,
                        tool_choice: str | dict | None = None,
                        params: dict | None = None) -> GenerationResult:
        litellm_model, conn = self._map(model)
        call: dict = {
            "model": litellm_model,
            "messages": [_to_wire(m) for m in messages],
            "logprobs": True,
            "num_retries": 0,          # guardrail 2: no LiteLLM-side retry/routing
            "drop_params": True,       # tolerate providers that reject logprobs/tools
            **conn,
        }
        if tools:
            call["tools"] = tools
        if tool_choice is not None:
            call["tool_choice"] = tool_choice
        for k, v in (params or {}).items():
            call.setdefault(k, v)   # never overrides model/messages/stream/tools
        start = time.monotonic()
        resp = await _acompletion(model, call)
        latency_ms = (time.monotonic() - start) * 1000
        data = resp.model_dump()

        choice = data["choices"][0]
        msg = choice["message"]
        text = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or None

        usage = data.get("usage") or {}
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        cached_in = _cached_tokens(usage)
        cost_usd = price_call(price, prompt_tokens=tokens_in,
                              completion_tokens=tokens_out,
                              cached_prompt_tokens=cached_in)

        logprobs = (choice.get("logprobs") or {}).get("content") if choice.get("logprobs") else None
        if logprobs:
            mean_logprob = sum(t["logprob"] for t in logprobs) / len(logprobs)
            confidence = min(1.0, math.exp(mean_logprob / self._t))
        else:
            confidence = DEFAULT_CONFIDENCE

        return GenerationResult(
            model=model, text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=latency_ms, cost_usd=cost_usd, confidence=confidence,
            tool_calls=tool_calls, cached_prompt_tokens=cached_in,
            finish_reason=_finish_reason(choice.get("finish_reason")),
            reasoning_tokens=_reasoning_tokens(usage))

    async def generate_stream(self, model: str, messages: list[Message], price: PriceCfg,
                              tier: str, hint: dict,
                              tools: list[dict] | None = None,
                              tool_choice: str | dict | None = None,
                              params: dict | None = None
                              ) -> AsyncIterator[StreamChunk]:
        """Real token streaming (step 8). Mirrors `generate()`'s call
        construction but with `stream=True`, `stream_options={"include_usage":
        True}`, and NO `logprobs` (unavailable while streaming -> confidence is
        None). Yields a `StreamChunk(delta=...)` per non-empty content delta in
        order, then exactly one terminal `StreamChunk(final=GenerationResult)`
        whose cost is priced from `pricing.yaml` (the `price` arg), never
        litellm.completion_cost -- identical cost basis to `generate()`."""
        litellm_model, conn = self._map(model)
        call: dict = {
            "model": litellm_model,
            "messages": [_to_wire(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "num_retries": 0,          # guardrail 2: no LiteLLM-side retry/routing
            "drop_params": True,       # tolerate providers that reject stream_options
            **conn,
        }
        if tools:
            call["tools"] = tools
        if tool_choice is not None:
            call["tool_choice"] = tool_choice
        for k, v in (params or {}).items():
            call.setdefault(k, v)   # never overrides model/messages/stream/tools
        start = time.monotonic()
        parts: list[str] = []
        usage: dict = {}
        finish = None
        async for chunk in await _acompletion(model, call):
            choices = getattr(chunk, "choices", None)
            content = None
            if choices:
                finish = getattr(choices[0], "finish_reason", None) or finish
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage = _usage_dict(u)
            if content:  # skip None/empty deltas (e.g. the terminal usage chunk)
                parts.append(content)
                yield StreamChunk(delta=content)
        latency_ms = (time.monotonic() - start) * 1000
        text = "".join(parts)
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        cached_in = usage.get("cached_tokens", 0)
        cost_usd = price_call(price, prompt_tokens=tokens_in,
                              completion_tokens=tokens_out,
                              cached_prompt_tokens=cached_in)
        yield StreamChunk(final=GenerationResult(
            model=model, text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=latency_ms, cost_usd=cost_usd, confidence=None,
            tool_calls=[], cached_prompt_tokens=cached_in,
            finish_reason=_finish_reason(finish)))

    async def health(self) -> BackendHealth:
        # Per-model reachability is probed at onboard time (registry), not at
        # boot: the single LiteLLM egress path is always "up".
        return BackendHealth(ok=True, detail="litellm")
