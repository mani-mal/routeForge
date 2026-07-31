# LiteLLM Gateway Swap + Real Multi-Step Egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four hand-rolled backend adapters with a single LiteLLM egress path reaching onprem vLLM/Dynamo + Azure + Bedrock per call, drop the mock backend, and wire the multi-step agent path to call real models per hop.

**Architecture:** One `LiteLLMBackend` implements the existing `InferenceBackend` Protocol and slots into `Gateway.run` unchanged; it maps the router's chosen `(provider, name)` to a pinned LiteLLM model string and calls `litellm.acompletion`. The internal agent endpoint gains access to that backend and calls it per hop instead of faking completions. Cost stays sourced from `pricing.yaml`; the router/escalation/DecisionRecord layer is untouched.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LiteLLM 1.93 (SDK, not proxy), NeMo Agent Toolkit, pytest with monkeypatched `litellm.acompletion` for offline isolation.

**Design spec:** `docs/superpowers/specs/2026-07-30-litellm-gateway-swap-design.md`.

## Global Constraints

Every task's requirements implicitly include these (copied verbatim from the spec / CLAUDE.md hard rules):

- **Guardrail 1 — behind the gate.** `LiteLLMBackend` is only ever called from `Gateway.run` or the internal hop endpoint, both downstream of the step-2 gate. Never an ingress proxy.
- **Guardrail 2 — pinned, not self-routing.** Only `litellm.acompletion()` is used, always pinned to the single model the router chose, with `num_retries=0`. **Never** `litellm.Router`, never LiteLLM's own fallback/load-balancing. `Gateway.run` remains the only escalation/fallback authority.
- **Guardrail 3 — `onprem_only` = cloud stripped upstream.** The gate already empties cloud from `ctx.allowed_providers`; the backend's `_map()` additionally **raises** if the chosen model's provider isn't enabled+mapped. A `RecordingBackend`-style test asserts a PII/`onprem_only` request never calls a cloud provider through LiteLLM.
- **Cost from `pricing.yaml` only.** `cost_usd = (tokens_in*price.input + tokens_out*price.output) / 1_000_000`. Never call `litellm.completion_cost`.
- **Confidence** = `min(1.0, exp(mean_logprob / T))` with `T = routing.confidence_logprob_temperature`; when the response carries no logprobs, use `DEFAULT_CONFIDENCE = 0.9` (never escalate on a missing signal).
- **LiteLLM's Langfuse callback stays OFF.** Do not set `litellm.success_callback` / `litellm.failure_callback`. Redacted traces still flow via `app/obs/export.py` (rule 2).
- **Single-owner config.** `pricing.yaml` owns USD, `routing.yaml` owns the pool/tiers, `serving.yaml` owns infra/transport. The startup config lint must stay green. No secrets committed — creds via env only. Provider names spelled `onprem` everywhere.
- **Response parsing** reads a plain dict via `resp.model_dump()` (mirrors the old `openai_compat.py` `resp.json()` access) so it is robust to LiteLLM object shape.

---

## File Structure

**Created:**
- `app/gateway/backends/litellm_backend.py` — the single `LiteLLMBackend`.
- `tests/_litellm_fake.py` — shared `fake_acompletion` + `FakeResponse` used by every test that needs offline isolation.
- `tests/test_litellm_backend.py` — unit tests for the backend.
- `tests/test_litellm_multistep.py` — real multi-step egress tests.

**Modified:**
- `app/models.py` — `GenerationResult.tool_calls`.
- `app/gateway/backends/base.py` — `InferenceBackend.generate` gains `tools` param.
- `app/gateway/backends/factory.py` — `make_backend`/`resolve_backend` return `LiteLLMBackend`; drop mock fallback.
- `app/config/schema.py` — provider connection fields, `PoolCandidate.litellm_model`, drop `allow_backend_fallback`.
- `app/config/lint.py` — drop `mock`, drop fallback handling, require connection for enabled providers.
- `app/agent/internal_endpoint.py` — real per-hop backend call.
- `app/main.py` — `app.state.backend`.
- `config/serving.yaml`, `config/routing.yaml`, `.env.example`, `pyproject.toml`, `uv.lock`.
- `tests/conftest.py` + re-pointed test files (see Task 6).

**Deleted:**
- `app/gateway/backends/mock.py`, `vllm.py`, `dynamo.py`, `openai_compat.py`.
- `app/agent/mock_agent.py`.
- `tests/test_vllm_backend.py`, `tests/test_dynamo_backend.py` (replaced by `test_litellm_backend.py`).

---

## Task 1: Foundation — litellm dependency + `GenerationResult.tool_calls`

**Files:**
- Modify: `pyproject.toml` (dependencies list), `uv.lock`
- Modify: `app/models.py:122-129`
- Test: `tests/test_generation_result_tool_calls.py`

**Interfaces:**
- Produces: `GenerationResult.tool_calls: list[dict] | None` (default `None`); `litellm` importable as a direct dep.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generation_result_tool_calls.py
from app.models import GenerationResult


def test_generation_result_carries_tool_calls():
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression": "2+2"}'}}]
    g = GenerationResult(model="local-8b", text="", tokens_in=3, tokens_out=0,
                         latency_ms=12.0, cost_usd=0.0, confidence=0.9, tool_calls=tc)
    assert g.tool_calls == tc


def test_generation_result_tool_calls_defaults_none():
    g = GenerationResult(model="local-8b", text="hi", tokens_in=3, tokens_out=1,
                         latency_ms=12.0, cost_usd=0.0, confidence=0.9)
    assert g.tool_calls is None


def test_litellm_is_importable():
    import litellm  # noqa: F401
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_generation_result_tool_calls.py -q`
Expected: FAIL (`tool_calls` unexpected keyword / litellm maybe importable already).

- [ ] **Step 3: Add the field**

In `app/models.py`, `GenerationResult`, append after `confidence: float`:

```python
    # Tool calls the model requested this turn (multi-step agent path). None on
    # the direct path (which never passes tools). Each item is an OpenAI-shape
    # tool_call dict: {id, type, function:{name, arguments}}.
    tool_calls: list[dict] | None = None
```

- [ ] **Step 4: Add litellm as a direct pinned dependency**

In `pyproject.toml` `[project].dependencies`, append:

```toml
  # Egress gateway (steps 7-8): single multi-provider path (onprem/Azure/Bedrock).
  # Used as an SDK behind the policy gate, never as an ingress proxy. Already
  # present transitively via nvidia-nat[langchain]; pinned here as a direct dep.
  "litellm>=1.93,<2",
```

Run: `uv lock` then `uv sync` (no version bump expected — already resolved at 1.93.0).

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_generation_result_tool_calls.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock app/models.py tests/test_generation_result_tool_calls.py
git commit -m "feat(gateway): litellm direct dep + GenerationResult.tool_calls"
```

---

## Task 2: Config — provider→LiteLLM map (schema + YAML)

**Files:**
- Modify: `app/config/schema.py:68-70` (`ProviderCfg`), `:4-11` (`PoolCandidate`), `:128-138` (`ServingCfg`)
- Modify: `config/serving.yaml`, `config/routing.yaml`
- Test: `tests/test_litellm_config.py`

**Interfaces:**
- Produces:
  - `ProviderCfg(enabled: bool, litellm_prefix: str | None = None, api_base_env: str | None = None, api_key_env: str | None = None, api_version_env: str | None = None)`
  - `PoolCandidate.litellm_model: str | None = None`
  - `ServingCfg` **no longer has** `allow_backend_fallback`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_litellm_config.py
from app.config.loader import load_settings


def test_serving_providers_carry_litellm_mapping():
    s = load_settings()
    onprem = s.serving.providers["onprem"]
    assert onprem.litellm_prefix == "openai"
    azure = s.serving.providers["azure"]
    assert azure.litellm_prefix == "azure"
    assert azure.api_base_env == "AZURE_API_BASE"
    assert azure.api_key_env == "AZURE_API_KEY"
    assert azure.api_version_env == "AZURE_API_VERSION"
    assert s.serving.providers["bedrock"].litellm_prefix == "bedrock"


def test_pool_candidates_can_carry_litellm_model():
    s = load_settings()
    by_name = {c.name: c for c in s.routing.model_pool}
    assert by_name["azure-gpt-mid"].litellm_model == "azure/gpt-4o-mini"
    assert by_name["bedrock-frontier"].litellm_model == "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    # onprem entries need no override (default <prefix>/<name>)
    assert by_name["local-8b"].litellm_model is None


def test_allow_backend_fallback_field_removed():
    s = load_settings()
    assert not hasattr(s.serving, "allow_backend_fallback")


def test_default_inference_backend_is_vllm():
    s = load_settings()
    assert s.serving.inference_backend == "vllm"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_litellm_config.py -q`
Expected: FAIL.

- [ ] **Step 3: Extend the schema**

`app/config/schema.py` — `ProviderCfg`:

```python
class ProviderCfg(BaseModel):
    enabled: bool
    # LiteLLM connection (transport owner: serving.yaml). `litellm_prefix` is the
    # LiteLLM provider route (openai | azure | bedrock). The *_env fields name the
    # environment variables holding creds/endpoints (values never in config —
    # secrets via .env only). onprem derives its api_base from the vllm/dynamo
    # block per `inference_backend`, so it needs only a key env; bedrock reads
    # standard AWS_* env, so it needs neither.
    litellm_prefix: str | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    api_version_env: str | None = None
```

`PoolCandidate` — append:

```python
    # Downstream LiteLLM model id when it differs from the routing slug (cloud
    # deployment/model names). Defaults to f"{provider_prefix}/{name}" in the
    # backend. onprem's served-model-name == `name`, so it needs no override.
    litellm_model: str | None = None
```

`ServingCfg` — **remove** the `allow_backend_fallback: bool` line.

- [ ] **Step 4: Update the YAML**

`config/serving.yaml`:
- `inference_backend: mock` → `inference_backend: vllm` and update the inline comment to `# vllm | dynamo | none   (mock removed — LiteLLM is the single egress path)`.
- **Delete** the `allow_backend_fallback: true` line.
- Replace the `providers:` block with:

```yaml
providers:
  onprem:  {enabled: true,  litellm_prefix: openai,  api_key_env: SP_ONPREM_API_KEY}
  azure:   {enabled: false, litellm_prefix: azure,   api_base_env: AZURE_API_BASE, api_key_env: AZURE_API_KEY, api_version_env: AZURE_API_VERSION}
  bedrock: {enabled: false, litellm_prefix: bedrock}
```

`config/routing.yaml` — add `litellm_model` to the two cloud pool entries (leave onprem entries as-is):

```yaml
  - {name: azure-gpt-mid,    provider: azure,   tier: mid,      display_name: "GPT-4o mini (Azure)",   litellm_model: "azure/gpt-4o-mini"}
  - {name: bedrock-frontier, provider: bedrock, tier: frontier, display_name: "Claude Sonnet (Bedrock)", litellm_model: "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"}
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_litellm_config.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add app/config/schema.py config/serving.yaml config/routing.yaml tests/test_litellm_config.py
git commit -m "feat(config): provider->LiteLLM map, drop allow_backend_fallback, default backend vllm"
```

---

## Task 3: Config lint — drop mock, drop fallback, require connection config

**Files:**
- Modify: `app/config/lint.py:56` (`_VALID_BACKENDS`), `lint_settings`/`enforce`
- Test: `tests/test_litellm_lint.py`

**Interfaces:**
- Consumes: `ProviderCfg.litellm_prefix` (Task 2).
- Produces: named lint violations `BACKEND_MOCK_REMOVED` (via existing enum check), `PROVIDER_NO_LITELLM_MAP`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_litellm_lint.py
import pytest
from app.config.loader import load_settings
from app.config.lint import enforce, lint_settings, ConfigLintError


def test_mock_backend_is_now_rejected():
    s = load_settings()
    s = s.model_copy(update={
        "serving": s.serving.model_copy(update={"inference_backend": "mock"})})
    with pytest.raises(ConfigLintError):
        enforce(s)


def test_enabled_provider_without_litellm_prefix_is_a_violation():
    s = load_settings()
    providers = dict(s.serving.providers)
    providers["azure"] = providers["azure"].model_copy(
        update={"enabled": True, "litellm_prefix": None})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": providers})})
    errs = lint_settings(s)
    assert any("PROVIDER_NO_LITELLM_MAP" in e and "azure" in e for e in errs)


def test_disabled_provider_without_prefix_is_ok():
    s = load_settings()  # azure/bedrock disabled by default, bedrock has prefix anyway
    assert not any("PROVIDER_NO_LITELLM_MAP" in e for e in lint_settings(s))


def test_shipped_config_lints_clean():
    enforce(load_settings())  # must not raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_litellm_lint.py -q`
Expected: FAIL (mock still valid; no PROVIDER_NO_LITELLM_MAP rule).

- [ ] **Step 3: Update the lint**

`app/config/lint.py`:
- `_VALID_BACKENDS = ("vllm", "dynamo", "none")` (drop `"mock"`).
- In `lint_settings`, before `return errs`, add:

```python
    # Every ENABLED provider must have a LiteLLM route, or the backend can't
    # reach it. Disabled providers are skipped (their models are filtered out
    # of any request's candidate pool upstream). onprem enabled-ness is the
    # inference_backend axis; azure/bedrock is providers.*.enabled.
    for pname, pcfg in s.serving.providers.items():
        enabled = pcfg.enabled and not (pname == "onprem" and s.serving.inference_backend == "none")
        if enabled and not pcfg.litellm_prefix:
            errs.append(f"PROVIDER_NO_LITELLM_MAP {pname} has no litellm_prefix")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_litellm_lint.py tests/test_config_lint.py -q`
Expected: PASS (new file; existing lint tests still green — if any asserted `mock` valid, this task fixes them).

- [ ] **Step 5: Commit**

```bash
git add app/config/lint.py tests/test_litellm_lint.py
git commit -m "feat(lint): reject mock backend, require litellm map for enabled providers"
```

---

## Task 4: `LiteLLMBackend.generate` — mapping, cost, confidence, tool_calls

**Files:**
- Create: `app/gateway/backends/litellm_backend.py`
- Modify: `app/gateway/backends/base.py:33-39` (Protocol `tools` param)
- Create: `tests/_litellm_fake.py`, `tests/test_litellm_backend.py`

**Interfaces:**
- Consumes: `Settings`, `PriceCfg`, `GenerationResult.tool_calls` (Task 1), provider map (Task 2).
- Produces:
  - `class LiteLLMBackend` with `name = "litellm"`, `__init__(settings, *, logprob_temperature=1.0)`, `async generate(model, messages, price, tier, hint, tools=None) -> GenerationResult`, `_map(model) -> tuple[str, dict]`, `async health() -> BackendHealth`.
  - `tests/_litellm_fake.py`: `FakeResponse` + `fake_acompletion(**kwargs)` (async) capturing calls in a module list `CALLS`.
- `InferenceBackend.generate` signature gains `tools: list[dict] | None = None`.

- [ ] **Step 1: Write the shared fake**

```python
# tests/_litellm_fake.py
"""Offline stand-in for litellm.acompletion. Monkeypatch
app.gateway.backends.litellm_backend.acompletion with fake_acompletion (or a
functools.partial of it) in tests. Records every call in CALLS so tests can
assert the pinned model string / kwargs the backend passed."""
from __future__ import annotations

CALLS: list[dict] = []


class FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def make_response(*, content: str | None = "ok", tool_calls: list[dict] | None = None,
                  prompt_tokens: int = 10, completion_tokens: int = 5,
                  logprobs: list[float] | None = None) -> FakeResponse:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    lp = None
    if logprobs is not None:
        lp = {"content": [{"logprob": v} for v in logprobs]}
    return FakeResponse({
        "choices": [{"message": message, "logprobs": lp}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


def fake_acompletion(*, response: FakeResponse | None = None, **kwargs):
    async def _call(**call_kwargs):
        CALLS.append(call_kwargs)
        return response if response is not None else make_response()
    # Support both patch styles: partial(response=...) OR direct monkeypatch.
    if response is not None or not kwargs:
        return _call(**kwargs) if kwargs else _call
    return _call
```

Note to implementer: the simplest monkeypatch in each test is
`monkeypatch.setattr(litellm_backend, "acompletion", make_recording_acompletion(resp))`
where `make_recording_acompletion` returns an `async def` appending to `CALLS`
and returning `resp`. Prefer that explicit helper if the dual-mode `fake_acompletion`
above proves awkward — the requirement is only: an async callable the backend can
`await`, that records kwargs and returns a `FakeResponse`. Clear `CALLS` in a fixture.

- [ ] **Step 2: Write the failing backend test**

```python
# tests/test_litellm_backend.py
import math
import pytest
from app.config.loader import load_settings
from app.config.schema import PriceCfg
from app.models import Message
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response, CALLS


def _price(name):
    return load_settings().pricing.models[name]


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()
    yield
    CALLS.clear()


def _patch(monkeypatch, resp):
    async def _call(**kwargs):
        CALLS.append(kwargs)
        return resp
    monkeypatch.setattr(litellm_backend, "acompletion", _call)


def test_onprem_maps_to_openai_prefix(monkeypatch):
    _patch(monkeypatch, make_response(content="hi", prompt_tokens=10, completion_tokens=5))
    b = litellm_backend.LiteLLMBackend(load_settings())
    import asyncio
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price("local-8b"), "cheap", {}))
    assert CALLS[0]["model"] == "openai/local-8b"
    assert r.cost_usd == pytest.approx((10 * 0.05 + 5 * 0.05) / 1_000_000)
    assert r.tool_calls is None


def test_azure_and_bedrock_use_litellm_model(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response())
    b = litellm_backend.LiteLLMBackend(load_settings())
    asyncio.run(b.generate("azure-gpt-mid", [Message(role="user", content="hi")],
                           _price("azure-gpt-mid"), "mid", {}))
    assert CALLS[0]["model"] == "azure/gpt-4o-mini"
    CALLS.clear()
    asyncio.run(b.generate("bedrock-frontier", [Message(role="user", content="hi")],
                           _price("bedrock-frontier"), "frontier", {}))
    assert CALLS[0]["model"].startswith("bedrock/anthropic.claude")


def test_no_logprobs_uses_default_confidence(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response(logprobs=None))
    b = litellm_backend.LiteLLMBackend(load_settings())
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price("local-8b"), "cheap", {}))
    assert r.confidence == 0.9


def test_logprobs_softened_by_temperature(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response(logprobs=[-1.0, -1.0, -1.0]))
    b = litellm_backend.LiteLLMBackend(load_settings(), logprob_temperature=4.0)
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="hi")],
                               _price("local-8b"), "cheap", {}))
    assert r.confidence == pytest.approx(min(1.0, math.exp(-1.0 / 4.0)))


def test_tool_calls_surfaced(monkeypatch):
    import asyncio
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'}}]
    _patch(monkeypatch, make_response(content=None, tool_calls=tc))
    b = litellm_backend.LiteLLMBackend(load_settings())
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="2+2")],
                               _price("local-8b"), "cheap", {}, tools=[{"type": "function"}]))
    assert r.tool_calls == tc
    assert CALLS[0]["tools"] == [{"type": "function"}]
    assert CALLS[0]["num_retries"] == 0


def test_disabled_provider_raises(monkeypatch):
    import asyncio
    _patch(monkeypatch, make_response())
    s = load_settings()  # azure disabled by default
    b = litellm_backend.LiteLLMBackend(s)
    with pytest.raises(Exception):
        asyncio.run(b.generate("azure-gpt-mid", [Message(role="user", content="hi")],
                               _price("azure-gpt-mid"), "mid", {}))
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_litellm_backend.py -q`
Expected: FAIL (module doesn't exist).

- [ ] **Step 4: Add the `tools` param to the Protocol**

`app/gateway/backends/base.py`, `InferenceBackend.generate`:

```python
    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict,
                        tools: list[dict] | None = None) -> GenerationResult: ...
```

- [ ] **Step 5: Implement the backend**

```python
# app/gateway/backends/litellm_backend.py
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

    def __init__(self, settings: Settings, *, logprob_temperature: float = 1.0) -> None:
        self._s = settings
        self._t = logprob_temperature if logprob_temperature > 0 else 1.0
        self._pool = {c.name: c for c in settings.routing.model_pool}

    def _map(self, model: str) -> tuple[str, dict]:
        cand = self._pool[model]
        pcfg = self._s.serving.providers.get(cand.provider)
        onprem_disabled = cand.provider == "onprem" and self._s.serving.inference_backend == "none"
        if pcfg is None or not pcfg.enabled or onprem_disabled or not pcfg.litellm_prefix:
            raise RuntimeError(
                f"provider {cand.provider!r} for model {model!r} is not enabled/mapped "
                "(guardrail 3: cloud must be unreachable by construction)")
        litellm_model = cand.litellm_model or f"{pcfg.litellm_prefix}/{cand.name}"
        kwargs: dict = {}
        # onprem: base_url from the vllm/dynamo block per inference_backend.
        if cand.provider == "onprem":
            ib = self._s.serving.inference_backend
            kwargs["api_base"] = (self._s.serving.vllm.base_url if ib == "vllm"
                                  else self._s.serving.dynamo.endpoint)
        if pcfg.api_base_env:
            kwargs["api_base"] = os.environ.get(pcfg.api_base_env) or kwargs.get("api_base")
        if pcfg.api_key_env:
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
        # Implemented in Task 5 (probes the onprem endpoint). Placeholder here so
        # the class satisfies the Protocol; Task 5 replaces this body.
        return BackendHealth(ok=True, detail="litellm")
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_litellm_backend.py -q`
Expected: PASS (6 tests).

- [ ] **Step 7: Commit**

```bash
git add app/gateway/backends/litellm_backend.py app/gateway/backends/base.py tests/_litellm_fake.py tests/test_litellm_backend.py
git commit -m "feat(gateway): LiteLLMBackend.generate (mapping, cost, confidence, tool_calls)"
```

---

## Task 5: Cutover — health + factory + main wiring, delete old adapters, re-point the suite

This is the atomic cutover: it starts green (old adapters still present, `LiteLLMBackend` added in Task 4) and ends green (adapters gone, every test on `LiteLLMBackend`). Large but coherent — review the whole diff.

**Files:**
- Modify: `app/gateway/backends/litellm_backend.py` (`health`), `app/gateway/backends/factory.py`, `app/main.py:98,103` (add `app.state.backend`)
- Modify: `tests/conftest.py:19,47`, `tests/test_gateway.py`, `tests/test_two_lever_cost.py`, `tests/test_display_name.py`, `tests/test_controls.py`, `tests/test_backend_selection.py`, `tests/test_confidence_calibration.py`, `tests/test_backends.py`
- Delete: `app/gateway/backends/mock.py`, `vllm.py`, `dynamo.py`, `openai_compat.py`, `tests/test_vllm_backend.py`, `tests/test_dynamo_backend.py`

**Interfaces:**
- Consumes: `LiteLLMBackend` (Task 4).
- Produces: `make_backend(settings) -> LiteLLMBackend`; `resolve_backend` exits non-zero when the onprem endpoint is unreachable (no mock fallback); `app.state.backend` set in `create_app`.

- [ ] **Step 1: Implement `LiteLLMBackend.health`**

Replace the placeholder `health()` with a real onprem probe (skipped for `none`):

```python
    async def health(self) -> BackendHealth:
        ib = self._s.serving.inference_backend
        if ib == "none":
            return BackendHealth(ok=True, detail="onprem disabled (none)")
        base = self._s.serving.vllm.base_url if ib == "vllm" else self._s.serving.dynamo.endpoint
        models_url = base.rstrip("/") + "/models" if ib == "vllm" else base.rstrip("/") + "/v1/models"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.get(models_url)
                resp.raise_for_status()
                models = [m["id"] for m in resp.json().get("data", [])]
            return BackendHealth(ok=True, models=models)
        except Exception as e:
            return BackendHealth(ok=False, detail=str(e))
```

- [ ] **Step 2: Write the failing factory test (re-point `test_backend_selection.py`)**

Rewrite `tests/test_backend_selection.py` so:
- `make_backend(_s("vllm"))`, `make_backend(_s("none"))` both return `LiteLLMBackend`.
- `make_backend(_s("mock"))` is removed (mock is gone; lint rejects it anyway).
- unreachable onprem health → `resolve_backend` calls `sys.exit(1)` (monkeypatch `LiteLLMBackend.health` to return `ok=False`, assert `SystemExit`). There is **no** fallback-to-mock path anymore.
- reachable onprem health → returns `LiteLLMBackend`.
- keep the loop-safety test but assert against `LiteLLMBackend`.

```python
# core of the rewrite
from app.gateway.backends.factory import make_backend, resolve_backend
from app.gateway.backends.litellm_backend import LiteLLMBackend

def test_make_backend_is_litellm():
    assert isinstance(make_backend(_s("vllm")), LiteLLMBackend)
    assert isinstance(make_backend(_s("none")), LiteLLMBackend)

def test_unreachable_onprem_exits(monkeypatch):
    async def bad(self): 
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="down")
    monkeypatch.setattr(LiteLLMBackend, "health", bad)
    with pytest.raises(SystemExit):
        asyncio.run(resolve_backend(_s("vllm")))
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_backend_selection.py -q`
Expected: FAIL.

- [ ] **Step 4: Rewrite the factory**

`app/gateway/backends/factory.py` — replace `make_backend` and the fallback path:

```python
from app.gateway.backends.litellm_backend import LiteLLMBackend

def make_backend(settings: Settings) -> LiteLLMBackend:
    temp = settings.routing.confidence_logprob_temperature
    return LiteLLMBackend(settings, logprob_temperature=temp)


async def resolve_backend(settings: Settings) -> LiteLLMBackend:
    # Probe with a throwaway instance (loop-safety, see README), serve with a
    # fresh one. No mock fallback exists any more: an unreachable onprem backend
    # is a fail-closed boot error (never silently degrade in a customer deploy).
    probe = make_backend(settings)
    health = await probe.health()
    if not health.ok:
        log.error("backend_unavailable_no_fallback backend=%s detail=%s",
                  settings.serving.inference_backend, health.detail)
        sys.exit(1)
    log.info("serving.backend active backend=litellm inference=%s models=%s",
             settings.serving.inference_backend, health.models)
    return make_backend(settings)
```

Update the module docstring: remove the `mock`/`none` MockBackend description; document that `none` skips the onprem probe (health ok=True) and that onprem_only rejection is still enforced in `context.py`. (`_aclose` is no longer needed — `LiteLLMBackend` holds no persistent client; remove it and its call.)

- [ ] **Step 5: Wire `app.state.backend` in `main.py`**

After `backend = asyncio.run(resolve_backend(settings))` add `app.state.backend = backend`. Update the nearby comment that says "trigger fallback-to-mock or sys.exit(1)" → "an unreachable vllm/dynamo endpoint is a fail-closed sys.exit(1); mock is gone."

- [ ] **Step 6: Delete old adapters + re-point every remaining consumer**

Delete `app/gateway/backends/{mock,vllm,dynamo,openai_compat}.py` and `tests/test_vllm_backend.py`, `tests/test_dynamo_backend.py`.

Re-point constructions of `MockBackend(...)` to a monkeypatched `LiteLLMBackend`:
- `tests/conftest.py`: replace `from app.gateway.backends.mock import MockBackend` and `backend = MockBackend(settings)` (line 47) with `LiteLLMBackend(settings)`. Because fixtures build a real pipeline that will call `acompletion`, add an autouse fixture in `conftest.py` that monkeypatches `app.gateway.backends.litellm_backend.acompletion` with a recording async fake returning `make_response()` (import from `tests._litellm_fake`). Tests that need specific content/confidence override it locally.
- `tests/test_gateway.py`: `_AlwaysLowBackend(MockBackend)` → subclass `LiteLLMBackend` and override `generate` to return a fixed low-confidence `GenerationResult` (it already overrode behavior; keep that pattern, no `acompletion` needed since `generate` is overridden).
- `tests/test_two_lever_cost.py`, `tests/test_display_name.py`, `tests/test_controls.py`: `MockBackend(...)` → `LiteLLMBackend(...)` + the recording-acompletion monkeypatch (or a `generate` override returning a deterministic `GenerationResult`, whichever the existing assertions need — these assert on cost/two-lever/display, so a deterministic `generate` override keyed off tier/price is cleanest; provide a small `_DeterministicLiteLLM(LiteLLMBackend)` helper in each that mirrors the old MockBackend seeded logic).
- `tests/test_backends.py`: rewrite as a thin `LiteLLMBackend.generate` smoke test using the fake (or fold into `test_litellm_backend.py` and delete).
- `tests/test_confidence_calibration.py`: replace `VllmBackend`/`DynamoBackend` construction + `make_backend` assertion with `LiteLLMBackend` + the fake returning chosen logprobs; assert `make_backend` injects `routing.confidence_logprob_temperature` (construct via `make_backend(s)` and check the resulting confidence under a known logprob).

> Implementer note: a shared `_DeterministicLiteLLM` (a `LiteLLMBackend` subclass overriding `generate` to reproduce the old MockBackend seeded tokens/cost/`hint["mock_confidence"]`/`hint["mock_fail"]`) placed in `tests/_litellm_fake.py` will satisfy `test_gateway`, `test_two_lever_cost`, `test_display_name`, `test_controls`, `test_traffic_honesty` (Task 6) with one helper. Prefer it over per-file duplication. It never calls `acompletion` — it is the deterministic seam those cost/honesty tests need.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS except pre-existing unrelated `test_nat_workflow_config.py::test_workflow_validates` (nat CLI not installed — fails identically on `main`). No import errors for removed modules.

- [ ] **Step 8: Commit**

```bash
git add -A app/gateway/backends app/main.py tests/
git rm app/gateway/backends/mock.py app/gateway/backends/vllm.py app/gateway/backends/dynamo.py app/gateway/backends/openai_compat.py tests/test_vllm_backend.py tests/test_dynamo_backend.py
git commit -m "refactor(gateway): cut over to LiteLLMBackend, delete hand-rolled adapters"
```

> Staging note: use explicit paths, not a bare `git add -A` on the repo root. Stage the `app/gateway/backends/` dir, `app/main.py`, and the touched `tests/` files by name.

---

## Task 6: Multi-provider egress tests (Azure, Bedrock, onprem_only-never-cloud)

**Files:**
- Modify: `tests/test_traffic_honesty.py:39,49-108,143,156,229` (RecordingBackend wraps `_DeterministicLiteLLM`)
- Create: `tests/test_litellm_egress.py`

**Interfaces:**
- Consumes: `_DeterministicLiteLLM` (Task 5), `RecordingBackend` (existing).

- [ ] **Step 1: Re-point `test_traffic_honesty.py`**

Replace `from app.gateway.backends.mock import MockBackend` with the `_DeterministicLiteLLM` helper, and every `RecordingBackend(MockBackend(settings))` with `RecordingBackend(_DeterministicLiteLLM(settings))`. The `RecordingBackend` wrapper and all `models_called` assertions are **unchanged** — this is the privacy guard and must keep passing verbatim, including `test_bedrock_enabled_pii_stays_onprem_while_clean_request_can_reach_cloud`.

- [ ] **Step 2: Write the new egress test**

```python
# tests/test_litellm_egress.py
import asyncio
import pytest
from app.config.loader import load_settings
from app.models import Message
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response, CALLS


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear(); yield; CALLS.clear()


def _enable_cloud(s):
    providers = dict(s.serving.providers)
    providers["azure"] = providers["azure"].model_copy(update={"enabled": True})
    providers["bedrock"] = providers["bedrock"].model_copy(update={"enabled": True})
    return s.model_copy(update={"serving": s.serving.model_copy(update={"providers": providers})})


def test_azure_egress_pins_azure_model(monkeypatch):
    async def _call(**k): CALLS.append(k); return make_response()
    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    s = _enable_cloud(load_settings())
    b = litellm_backend.LiteLLMBackend(s)
    asyncio.run(b.generate("azure-gpt-mid", [Message(role="user", content="hi")],
                           s.pricing.models["azure-gpt-mid"], "mid", {}))
    assert CALLS[0]["model"] == "azure/gpt-4o-mini"


def test_bedrock_egress_pins_bedrock_model(monkeypatch):
    async def _call(**k): CALLS.append(k); return make_response()
    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    s = _enable_cloud(load_settings())
    b = litellm_backend.LiteLLMBackend(s)
    asyncio.run(b.generate("bedrock-frontier", [Message(role="user", content="hi")],
                           s.pricing.models["bedrock-frontier"], "frontier", {}))
    assert CALLS[0]["model"].startswith("bedrock/anthropic.claude")


def test_onprem_only_pool_has_no_cloud_candidate_to_call():
    # Belt to Task 4's braces: with cloud disabled (shipped default), a request
    # can only ever be handed onprem models — asserting the map refuses cloud.
    s = load_settings()
    b = litellm_backend.LiteLLMBackend(s)
    with pytest.raises(RuntimeError):
        b._map("azure-gpt-mid")   # azure disabled by default
```

- [ ] **Step 3: Run**

Run: `uv run pytest tests/test_litellm_egress.py tests/test_traffic_honesty.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_litellm_egress.py tests/test_traffic_honesty.py
git commit -m "test(gateway): multi-provider egress + onprem_only-never-cloud through LiteLLM"
```

---

## Task 7: Real multi-step egress in the internal endpoint

**Files:**
- Modify: `app/agent/internal_endpoint.py:26-28,140-174`
- Test: `tests/test_litellm_multistep.py` (part 1 — internal endpoint)

**Interfaces:**
- Consumes: `request.app.state.backend` (Task 5), `GenerationResult.tool_calls` (Task 1).
- Produces: `agent_hop` DecisionRecords carrying **real** `latency_ms`/`actual_cost_usd`; SSE built from the real completion.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_litellm_multistep.py
from app.agent.internal_endpoint import HOPS
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response


def test_hop_records_real_cost_and_latency(client, monkeypatch):
    # client fixture (conftest) already monkeypatches acompletion; override with
    # a known usage so we can assert real (non-seeded) cost is recorded.
    calls = {"n": 0}
    async def _call(**k):
        calls["n"] += 1
        return make_response(content="the answer is 4", prompt_tokens=20, completion_tokens=10)
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

    from app.models import ExecutionPolicyContext
    run_id = "run-ms-1"
    HOPS.start(run_id, ExecutionPolicyContext(
        request_id="r", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None))
    try:
        resp = client.post(f"/internal/v1/{run_id}/chat/completions",
                           json={"messages": [{"role": "user", "content": "2+2"}], "tools": []})
        assert resp.status_code == 200
        rec = HOPS._runs[run_id].records[-1]
        assert rec.actual_cost_usd is not None and rec.actual_cost_usd > 0  # real, not None
        assert rec.latency_ms >= 0  # from the real call, not seeded_latency_ms
        assert calls["n"] == 1  # the backend was actually called
    finally:
        if HOPS.has(run_id):
            HOPS.pop(run_id)


def test_tool_call_result_emits_tool_call_sse(client, monkeypatch):
    tc = [{"id": "call-1", "type": "function",
           "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'}}]
    async def _call(**k):
        return make_response(content=None, tool_calls=tc, prompt_tokens=5, completion_tokens=2)
    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    from app.models import ExecutionPolicyContext
    run_id = "run-ms-tc"
    HOPS.start(run_id, ExecutionPolicyContext(
        request_id="r", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None))
    try:
        resp = client.post(f"/internal/v1/{run_id}/chat/completions",
                           json={"messages": [{"role": "user", "content": "2+2"}],
                                 "tools": [{"type": "function",
                                            "function": {"name": "calculator"}}]})
        body = resp.text
        assert "tool_calls" in body and "calculator" in body
        assert "finish_reason" in body and "tool_calls" in body
    finally:
        if HOPS.has(run_id):
            HOPS.pop(run_id)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_litellm_multistep.py -q`
Expected: FAIL (endpoint still fakes; `actual_cost_usd` is None, no backend call).

- [ ] **Step 3: Rewrite the completion half of the endpoint**

In `app/agent/internal_endpoint.py`:
- Remove `from app.agent.mock_agent import decide_agent_step` and `from app.gateway.backends.base import seeded_latency_ms`.
- After `decision = model_router.select(...)` and `hop_index = HOPS.next_hop_index(run_id)`, replace the faked `DecisionRecord`/`decide_agent_step`/`sse_*` block with:

```python
    settings = request.app.state.settings
    backend = request.app.state.backend
    price = settings.pricing.models[decision.chosen.name]
    hop_msgs = [Message(role=m.get("role", "user"), content=m["content"])
                for m in messages if m.get("content")]
    gen = await backend.generate(
        decision.chosen.name, hop_msgs, price, decision.chosen.tier, {}, tools=tools)

    HOPS.record(run_id, DecisionRecord(
        step="agent_hop", chosen=decision.chosen.name,
        chosen_display_name=decision.chosen.display_name, reason=decision.reason,
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        actual_cost_usd=gen.cost_usd,        # REAL per-hop cost (was None + seeded latency)
        latency_ms=gen.latency_ms,           # REAL latency (no more seeded_latency_ms)
        hop_index=hop_index, tokens_priced=tokens_after))
    HOPS.record_compression(run_id, comp.record)

    # Per-hop = ONE real call, no escalation/fallback here — escalation stays a
    # direct-path (Gateway.run) feature, unchanged from the previously-faked
    # multi-step behaviour. Serialize the REAL completion to SSE: a tool_calls
    # message drives NAT's tool execution; otherwise it's the final answer.
    if gen.tool_calls:
        first = gen.tool_calls[0]
        stream = sse_tool_call(
            model=decision.chosen.name, call_id=first.get("id", f"call-{hop_index}"),
            tool_name=first["function"]["name"], arguments_json=first["function"]["arguments"])
    else:
        stream = sse_final(model=decision.chosen.name, content=gen.text)
    return StreamingResponse(stream, media_type="text/event-stream")
```

(Keep the existing compression block above and the `Message` import. `tools` is already parsed from the body; note `hop_msgs` here is the raw hop messages for the backend — the compressed `comp.messages` are what set `tokens_after` for pricing, consistent with the direct path pricing post-compression while sending the model the hop content. If the existing code already builds `hop_msgs` for `compress()`, reuse that variable rather than rebuilding.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_litellm_multistep.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/agent/internal_endpoint.py tests/test_litellm_multistep.py
git commit -m "feat(agent): internal endpoint calls the real backend per hop"
```

---

## Task 8: Remove `mock_agent` from production + real multi-step e2e

**Files:**
- Delete: `app/agent/mock_agent.py`
- Modify: `app/gateway/backends/base.py` (drop `seeded_latency_ms`/`TIER_LATENCY_MS` only if now unused — check `tests/test_agent_hop_compression.py:66`)
- Test: `tests/test_litellm_multistep.py` (part 2 — e2e via `live_internal_server`), plus a test fake driving `tool_call → final`

**Interfaces:**
- Consumes: `live_internal_server` fixture, the NAT agent runtime.

- [ ] **Step 1: Confirm `seeded_latency_ms` consumers**

Run: `grep -rn "seeded_latency_ms\|TIER_LATENCY_MS\|mock_agent" app/ tests/ | grep -v __pycache__`
- If the only remaining reference is `tests/test_agent_hop_compression.py`, keep `TIER_LATENCY_MS`/`seeded_latency_ms` in `base.py` (a test still uses it) OR update that test to not depend on it and remove them. Decide based on what the grep shows; document the choice in the commit. `mock_agent.py` must have **zero** production references after Task 7.

- [ ] **Step 2: Write the failing e2e test**

The NAT agent runtime (`live_internal_server`) drives itself over real HTTP; the "model" is our internal endpoint, which now calls `litellm.acompletion`. Monkeypatch `acompletion` process-wide (the live server runs in-process on a thread, same interpreter) to drive a `tool_call → final` sequence, reproducing the old `decide_agent_step` logic **in the test**:

```python
# tests/test_litellm_multistep.py (append)
import asyncio
from app.agent import runtime as agent_runtime
from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_response


def _scripted_acompletion():
    """Reproduces the old mock_agent.decide_agent_step in the test: first call
    (no prior tool result) -> a calculator tool_call; once a tool message is
    present -> a final answer echoing it."""
    async def _call(**k):
        msgs = k.get("messages", [])
        if any(m.get("role") == "tool" for m in msgs):
            last = next(m for m in reversed(msgs) if m.get("role") == "tool")
            return make_response(content=f"The tool reported: {last.get('content','')}",
                                 prompt_tokens=15, completion_tokens=8)
        tc = [{"id": "call-1", "type": "function",
               "function": {"name": "calculator", "arguments": '{"expression": "2+2"}'}}]
        return make_response(content=None, tool_calls=tc, prompt_tokens=20, completion_tokens=6)
    return _call


def test_multistep_hits_real_backend_and_stays_onprem(live_internal_server, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion", _scripted_acompletion())
    from app.models import ChatRequest, Message, ExecutionPolicyContext
    ctx = ExecutionPolicyContext(
        request_id="r-ms", tenant_id="hospital", sensitivity="restricted",
        onprem_only=True, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=60000,
        risk_class="high", require_verification=True, detected_entities=(),
        redaction_map_id=None)
    req = ChatRequest(messages=[Message(role="user", content="what is 2+2?")])
    result = asyncio.run(agent_runtime.run(ctx, req, port=live_internal_server.port))
    assert result.hops, "expected >=1 real hop"
    assert all(h.actual_cost_usd is not None for h in result.hops)  # real cost recorded
    # onprem_only stayed on-prem: every chosen model is an onprem-tier slug.
    assert all(h.chosen in {"local-8b", "local-32b"} for h in result.hops)
```

Requires `--extra agent` (nat installed). If nat is unavailable in the sandbox, mark
`pytestmark = pytest.mark.skipif(<nat not importable>)` mirroring the existing
multi-step tests' skip pattern, and note it in the report — the assertion logic is
still delivered.

- [ ] **Step 3: Run to verify it fails, then delete `mock_agent.py`**

Run: `uv run pytest tests/test_litellm_multistep.py -q` (expect the e2e to fail/skip pre-deletion). Then `git rm app/agent/mock_agent.py` and any now-dead references confirmed in Step 1.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_litellm_multistep.py -q`
Expected: PASS (or documented skip when nat is absent).

- [ ] **Step 5: Commit**

```bash
git add app/agent tests/test_litellm_multistep.py app/gateway/backends/base.py
git rm app/agent/mock_agent.py
git commit -m "feat(agent): real multi-step egress e2e; remove scripted mock_agent from production"
```

---

## Task 9: Docs, env, and skill reconciliation

**Files:**
- Modify: `.env.example`, `config/serving.yaml` (comments), `.claude/skills/inference-backends/` (mock-removed note), `BUILD_PLAN.md` (if it references mock backend)
- Test: none (docs); verification via grep asserts in Step 3

- [ ] **Step 1: Update `.env.example`**

Append (placeholders only — never real creds):

```bash
# LiteLLM egress providers. onprem (vLLM) uses a dummy key; cloud providers
# read standard LiteLLM env. Only needed when that provider is enabled.
SP_ONPREM_API_KEY=sk-noauth
AZURE_API_BASE=https://your-resource.openai.azure.com
AZURE_API_KEY=
AZURE_API_VERSION=2024-06-01
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION_NAME=us-east-1
```

- [ ] **Step 2: Reconcile prose**

- `config/serving.yaml`: ensure the `inference_backend` comment and the block header reflect "LiteLLM is the single egress path; mock removed; unreachable onprem = fail-closed boot error." Remove any lingering `allow_backend_fallback` mention.
- `.claude/skills/inference-backends/`: update the `mock | vllm | dynamo | none` description to note `mock` is removed and all providers now egress via LiteLLM (SDK, behind the gate). Keep the Dynamo skills list intact.
- `BUILD_PLAN.md`: if Phase 9 / backend text names the mock default or the hand-rolled adapters, add a one-line "superseded by the LiteLLM gateway swap (2026-07-30)" note. Do not rewrite history.

- [ ] **Step 3: Verify no stale references**

Run:
```bash
grep -rn "MockBackend\|allow_backend_fallback\|backends.mock\|backends.vllm\|backends.dynamo\|openai_compat\|decide_agent_step" app/ config/ --include=*.py --include=*.yaml | grep -v __pycache__
```
Expected: no matches in `app/` or `config/`. (Skills/docs may mention them historically.)

- [ ] **Step 4: Commit**

```bash
git add .env.example config/serving.yaml .claude/skills/inference-backends BUILD_PLAN.md
git commit -m "docs(gateway): env + skill/build-plan reconciliation for the LiteLLM swap"
```

---

## Final review focus

For the whole-branch review (most capable model), verify:
- **Guardrails hold end-to-end:** no `litellm.Router`; `num_retries=0` on every call; `_map` raises for disabled providers; `RecordingBackend` PII-never-cloud test green; LiteLLM Langfuse callbacks never set.
- **Cost integrity:** every USD figure still derives from `pricing.yaml`; two-lever test green.
- **Fail-closed:** unreachable onprem → `sys.exit(1)`; no mock fallback anywhere; config lint rejects `mock`.
- **Multi-step:** hops carry real `actual_cost_usd`/`latency_ms`; `onprem_only` multi-step stays on onprem slugs; `max_iterations`/`AgentCapExceeded` intact.
- **No stale references** to the deleted modules in `app/`.
- Full suite green except the pre-existing `test_nat_workflow_config.py::test_workflow_validates` (nat CLI absent — fails identically on `main`).
