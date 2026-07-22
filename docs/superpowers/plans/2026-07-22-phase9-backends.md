# Phase 9 — Real Inference Backends (vLLM + Dynamo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `inference_backend: vllm | dynamo | none` real — code-complete OpenAI-compatible adapters (offline-tested via mocked HTTP, run for real on GPU/DGX), startup health-validation with configurable fallback, and the load-bearing `none` + `onprem_only` → rejection — all behind config with no pipeline changes.

**Architecture:** One `InferenceBackend` Protocol (extended with `health()`), four implementations. `mock` stays the demo default. `vllm`/`dynamo` are thin `httpx.AsyncClient` adapters over an OpenAI-compatible `/v1/chat/completions` + `/v1/models`, differing only in endpoint/config. `none` disables the on-prem provider so `onprem_only` requests are rejected by the existing residency filter. A `make_backend(settings)` factory + a startup health check (fallback to mock if `allow_backend_fallback`, else exit non-zero) wire it in `main.py`.

**Tech Stack:** Python 3.12, httpx (async), Pydantic v2, pytest. Tests use `httpx.MockTransport` to simulate the vLLM/Dynamo OpenAI server — **no GPU, no network, offline**. Real vLLM/Dynamo servers are exercised on the DGX H200 by pointing `vllm.base_url`/`dynamo.endpoint` at them (no code change).

## Global Constraints

- **One serving-path axis** (inference-backends skill): `inference_backend: mock|vllm|dynamo|none` is the ONLY on-prem selector. Do NOT add a parallel `serving_paths` block. Cloud availability stays `providers.{azure,bedrock}.enabled`.
- **`none` + `onprem_only` ⇒ REJECT** with a clear error, never silently routed to cloud (CLAUDE.md backend rule; inference-backends skill). This inverts the privacy claim if wrong — test it explicitly (closes the Phase 7-documented gap).
- **Startup validation**: `await backend.health()`; if not ok → fall back to `MockBackend` **only if** `allow_backend_fallback: true` (log a warning), else `sys.exit(1)`. Never silently degrade in a customer demo. Print the active backend + model list in the startup banner.
- **`mock` is a first-class path** — the demo runs on it. Do not break it. Default config stays `inference_backend: mock`; the full existing suite must stay green.
- **Adapters are thin + real**: `vllm`/`dynamo` speak OpenAI `/v1/chat/completions`. Cost = `(tokens_in*price.input + tokens_out*price.output)/1e6` (same formula as mock, using real usage). Confidence from logprobs when available, else a documented default. Never fabricate token counts — use the server's `usage`.
- **Offline tests only** — `httpx.MockTransport` simulates the server. No test may require a GPU or real network. The real-server smoke is a documented manual step for the DGX.
- `generate` signature stays `generate(self, model, messages, price: PriceCfg, tier, hint) -> GenerationResult` (the Gateway calls it that way — do not change the call site).
- Providers `onprem|azure|bedrock`; tiers `cheap|mid|frontier`; canonical everywhere.

---

### Task 1: Protocol `health()` + config surface + `none` rejection

**Files:**
- Modify: `app/gateway/backends/base.py` (add `BackendHealth`, `health()` to Protocol)
- Modify: `app/gateway/backends/mock.py` (implement `health()`)
- Modify: `app/config/schema.py` (`VllmCfg`, `DynamoCfg` on `ServingCfg`) + `app/config/loader.py`
- Modify: `app/policy/context.py` (drop `onprem` from enabled providers when `inference_backend == "none"`)
- Test: `tests/test_backends.py` (extend), `tests/test_none_backend.py` (new)

**Interfaces:**
- Produces:
  - `class BackendHealth(BaseModel)`: `ok: bool`, `detail: str = ""`, `models: list[str] = []`.
  - `InferenceBackend` Protocol gains `async def health(self) -> BackendHealth: ...`.
  - `MockBackend.health()` → `BackendHealth(ok=True, detail="mock", models=[<pool onprem model names>])`.
  - `VllmCfg(base_url: str, timeout_s: int)`, `DynamoCfg(endpoint: str, router_mode: str, timeout_s: int)` on `ServingCfg` (values already in `config/serving.yaml`).
- `context.py`: compute `enabled` providers, then if `settings.serving.inference_backend == "none"`, discard `"onprem"` from `enabled` (on-prem serving is off). An `onprem_only` request then has `allowed = enabled & onprem_providers == {}`, which the router already rejects ("no candidates after residency filter") → surfaces as a clear 503.

- [ ] **Step 1: Failing tests.** `tests/test_none_backend.py`:

```python
import copy, pytest
from app.config.loader import load_settings
from app.policy.context import build_context

def _settings(backend):
    s = copy.deepcopy(load_settings())
    object.__setattr__(s.serving, "inference_backend", backend) if False else None
    return s.model_copy(update={"serving": s.serving.model_copy(update={"inference_backend": backend})})

def test_none_backend_drops_onprem_for_onprem_only_request():
    # An onprem_only request under inference_backend=none has NO allowed provider
    # (on-prem serving disabled, cloud excluded by residency) -> must be rejected.
    from app.models import Entity
    s = _settings("none")
    ssn = [Entity(type="US_SSN", start=0, end=11, value="x")]
    ctx = build_context(s, "r", "hospital", ssn, None)
    assert ctx.onprem_only is True
    assert ctx.allowed_providers == frozenset()   # nothing to serve on -> reject downstream

def test_none_backend_public_request_unaffected_when_cloud_enabled():
    # a public request can still use cloud if a cloud provider is enabled
    s = _settings("none")
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    ctx = build_context(s, "r", "acme", [], None)
    assert "onprem" not in ctx.allowed_providers   # on-prem off
    assert "azure" in ctx.allowed_providers
```

Add to `tests/test_backends.py`: `MockBackend` satisfies the extended Protocol (has `health`), and `asyncio.run(MockBackend(S).health()).ok is True`.

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Add `BackendHealth` + `health()` to base.py**; implement `MockBackend.health()`.
- [ ] **Step 4: Surface `VllmCfg`/`DynamoCfg`** in schema.py + loader.py (read the `vllm:`/`dynamo:` blocks from serving.yaml).
- [ ] **Step 5: `context.py` `none` handling** — after computing `enabled`, `if settings.serving.inference_backend == "none": enabled = enabled - {"onprem"}`. Add a short comment citing the CLAUDE.md rule.
- [ ] **Step 6: Run tests → PASS. Full suite green** (baseline 130 passed, 1 skipped — mock path unchanged). Commit.

---

### Task 2: `VllmBackend` (OpenAI-compatible, offline-tested)

**Files:**
- Rewrite: `app/gateway/backends/vllm.py`
- Test: `tests/test_vllm_backend.py` (new, `httpx.MockTransport`)

**Interfaces:**
- `class VllmBackend`: `name = "vllm"`; `__init__(self, cfg: VllmCfg, *, client: httpx.AsyncClient | None = None)` (inject client for tests; else build one at `cfg.base_url`, `timeout=cfg.timeout_s`).
  - `async generate(model, messages, price, tier, hint) -> GenerationResult`: POST `{base_url}/chat/completions` with `{"model": model, "messages": [{role,content}...], "logprobs": true}` (logprobs opt-in for a confidence signal). Measure latency with `time.monotonic()`. Parse `choices[0].message.content`, `usage.prompt_tokens`/`completion_tokens`. `cost_usd = (pt*price.input + ct*price.output)/1e6`. `confidence`: mean token logprob mapped to (0,1] via `exp(mean_logprob)` if logprobs present, else `0.9` (documented default). Return `GenerationResult(...)`.
  - `async health() -> BackendHealth`: GET `{base_url}/models`; ok if 200; `models=[m["id"] for m in data["data"]]`. Any exception → `BackendHealth(ok=False, detail=str(e))`.
- Note: `cfg.base_url` already ends in `/v1` (config: `http://vllm:8000/v1`), so POST `{base_url}/chat/completions` and GET `{base_url}/models`.

- [ ] **Step 1: Failing test** `tests/test_vllm_backend.py`:

```python
import asyncio, httpx
from app.config.schema import VllmCfg, PriceCfg
from app.gateway.backends.vllm import VllmBackend
from app.models import Message

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://vllm:8000/v1")

def test_generate_maps_openai_response():
    def handler(req):
        assert req.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "42"},
                         "logprobs": {"content": [{"logprob": -0.1}, {"logprob": -0.2}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    cfg = VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60)
    b = VllmBackend(cfg, client=_client(handler))
    price = PriceCfg(input=3.0, output=15.0, tier="cheap", provider="onprem")
    r = asyncio.run(b.generate("local-8b", [Message(role="user", content="q")], price, "cheap", {}))
    assert r.text == "42" and r.tokens_in == 10 and r.tokens_out == 3
    assert abs(r.cost_usd - (10*3.0 + 3*15.0)/1e6) < 1e-12
    assert 0 < r.confidence <= 1.0

def test_health_ok_and_unreachable():
    ok = VllmBackend(VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60),
                     client=_client(lambda req: httpx.Response(200, json={"data": [{"id": "local-8b"}]})))
    h = asyncio.run(ok.health())
    assert h.ok and "local-8b" in h.models
    def boom(req): raise httpx.ConnectError("refused")
    bad = VllmBackend(VllmCfg(base_url="http://vllm:8000/v1", timeout_s=60), client=_client(boom))
    assert asyncio.run(bad.health()).ok is False
```

(Confirm `PriceCfg`'s exact fields via `app/config/schema.py` and match the constructor.)

- [ ] **Step 2: Run → FAIL. Step 3: Implement vllm.py. Step 4: Run → PASS. Full suite green. Commit.**

---

### Task 3: `DynamoBackend` (OpenAI-compatible, offline-tested)

**Files:**
- Rewrite: `app/gateway/backends/dynamo.py`
- Test: `tests/test_dynamo_backend.py` (new)

**Interfaces:**
- `class DynamoBackend`: `name = "dynamo"`; `__init__(self, cfg: DynamoCfg, *, client=None)`. Dynamo's frontend is OpenAI-compatible; the adapter is the vLLM adapter with `cfg.endpoint` as base and (optionally) a `router_mode` note. `cfg.endpoint` is `http://dynamo-frontend:8080` (no `/v1`), so POST `{endpoint}/v1/chat/completions`, GET `{endpoint}/v1/models`. `generate`/`health` mirror vLLM. Add a one-line comment that Dynamo deployment uses NVIDIA's verified skills (dynamo-recipe-runner etc.), NOT hand-rolled here.
- To avoid duplication, factor the shared OpenAI-mapping logic into a small helper (e.g. `app/gateway/backends/openai_compat.py` with `async def openai_generate(client, path, model, messages, price, tier)` and `async def openai_health(client, path)`) and have both vllm.py and dynamo.py call it. (Refactor vllm.py from Task 2 to use it — keep Task 2's tests green.)

- [ ] **Step 1: Failing test** `tests/test_dynamo_backend.py` — mirror the vLLM test with the Dynamo base path (`/v1/chat/completions` under `endpoint`), asserting the mapping + health.
- [ ] **Step 2-4: Implement (shared helper), run → PASS, full suite green, commit.**

---

### Task 4: `make_backend` factory + startup health-validation/fallback + banner

**Files:**
- Create: `app/gateway/backends/factory.py`
- Modify: `app/main.py` (use the factory; async startup health check; fallback/exit; banner)
- Test: `tests/test_backend_selection.py` (new)

**Interfaces:**
- `def make_backend(settings) -> InferenceBackend`: returns `MockBackend(settings)` for `mock`; `VllmBackend(settings.serving.vllm)` for `vllm`; `DynamoBackend(settings.serving.dynamo)` for `dynamo`; `MockBackend(settings)` for `none` (a valid backend object — the `none` semantics are enforced in `context.py` (Task 1), so the pipeline still functions for public/cloud requests while onprem_only rejects). Document this clearly.
- `async def resolve_backend(settings) -> InferenceBackend`: `b = make_backend(settings)`; `h = await b.health()`; if `not h.ok`: if `settings.serving.allow_backend_fallback` → log warning + return `MockBackend(settings)`; else `sys.exit(1)`. `mock`/`none` health is always ok (no external server), so they never fall back. Only `vllm`/`dynamo` can fail health.
- `main.py`: `create_app` runs `resolve_backend` — since create_app is sync and health is async, run it via `asyncio.run(...)` guarded in a try (or a small sync helper). Log a startup banner: `"backend=<name> models=<...>"`. Keep the existing pipeline wiring; just swap `backend = MockBackend(settings)` → the resolved backend.

- [ ] **Step 1: Failing test** `tests/test_backend_selection.py`:

```python
import asyncio, pytest, httpx
from app.config.loader import load_settings
from app.gateway.backends.factory import make_backend, resolve_backend
from app.gateway.backends.mock import MockBackend
from app.gateway.backends.vllm import VllmBackend

def _s(backend, fallback=True):
    s = load_settings()
    return s.model_copy(update={"serving": s.serving.model_copy(
        update={"inference_backend": backend, "allow_backend_fallback": fallback})})

def test_factory_selects_by_config():
    assert isinstance(make_backend(_s("mock")), MockBackend)
    assert isinstance(make_backend(_s("vllm")), VllmBackend)

def test_unreachable_vllm_with_fallback_returns_mock(monkeypatch):
    s = _s("vllm", fallback=True)
    # force health() unhealthy
    async def bad(self): 
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="refused")
    monkeypatch.setattr(VllmBackend, "health", bad)
    b = asyncio.run(resolve_backend(s))
    assert isinstance(b, MockBackend)

def test_unreachable_vllm_no_fallback_exits(monkeypatch):
    s = _s("vllm", fallback=False)
    async def bad(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="refused")
    monkeypatch.setattr(VllmBackend, "health", bad)
    with pytest.raises(SystemExit):
        asyncio.run(resolve_backend(s))
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement factory.py + wire main.py. Step 4: Run → PASS.**
- [ ] **Step 5: Full suite green** (default config = mock → banner "backend=mock", no behavior change; 130+ passed). Add a DGX smoke doc note in `app/gateway/backends/README.md` or the plan: to run real, set `SP_INFERENCE_BACKEND=vllm` + `vllm.base_url` at a live vLLM server. Commit.

---

## Self-Review notes (controller)
- `none` rejection is enforced structurally in context.py (empty allowed set → existing router rejection), the least-surprise place — no new reject path. Test asserts it directly.
- vLLM/Dynamo share one OpenAI-mapping helper — thin, real, offline-tested via MockTransport; the DGX runs them for real by pointing config at a live server (no code change).
- Startup validation only ever fails for vllm/dynamo (mock/none health is trivially ok), matching the skill: unreachable + no-fallback → exit(1); + fallback → mock + warning.
- Confidence from logprobs is a real signal when the server returns them; the `0.9` default is documented, not fabricated per-request.
- Deferred (needs GPU, documented): real DGX end-to-end smoke; Dynamo router-mode tuning + NVIDIA dynamo-* skills for K8s deploy (referenced, not hand-rolled per the skill).
