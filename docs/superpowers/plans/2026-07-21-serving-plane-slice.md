# Serving Plane — Core Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an OpenAI-compatible Serving Plane API on the mock backend that takes a request through gate → task-mode → compression(stub) → router → gateway → output-verification and returns a governed response with a full DecisionRecord trace, proving the privacy and cost claims with a green pytest suite and a live smoke run.

**Architecture:** A **deterministic outer pipeline** (`app/pipeline.py`) calls single-responsibility modules in a fixed, security-load-bearing order. The policy gate runs first and computes `allowed_providers` so the router can never reach an excluded provider. NAT/Headroom/Langfuse/vLLM/Dynamo are typed stubs this pass (design §10); Presidio is real (with a regex sibling the tests use for offline determinism).

**Tech Stack:** Python 3.12, `uv`, FastAPI, Pydantic v2, uvicorn, Presidio, PyYAML, httpx, pytest.

## Global Constraints

- **Python 3.12**; dependency management via `uv`; Pydantic v2; type-hint everything.
- **Config values are single-sourced in `config/*.yaml`** — never hard-code tiers, weights, thresholds, provider names, or prices in code. Read them from `Settings`.
- **Provider names:** `onprem | azure | bedrock` (one word). **Residency:** `any | private_cloud_ok | onprem_only`. **Tiers:** `cheap | mid | frontier` (no `strong`). **Sensitivity:** `public | internal | confidential | restricted`.
- **Hard rules (CLAUDE.md):** gate runs before anything external; `onprem_only` filtered by construction in the gate, never post-hoc; fail closed (gate error → reject); residency beats risk floor; router prices post-compression tokens; every step emits a `DecisionRecord`.
- **Detected entities and traces carry entity TYPES only, never values.** The redaction map never leaves the gate and never enters a trace/response.
- **Demo mode returns placeholders** (no detokenization this pass).
- **Tests run offline** with `pii_detection: regex` and the `mock` backend; no GPU, no network.
- Commits are local; pushing to `origin/feat/core-slice` is done out-of-band with the token helper. Keep commits small and frequent.

---

## File structure (locked before tasks)

```
app/
  main.py              FastAPI factory; startup loads+lints config, warms detector
  pipeline.py          deterministic orchestrator: gate→mode→compress→route→gateway→verify
  models.py            Pydantic models + enums (built from config scales)
  config/
    schema.py          typed Settings models
    loader.py          load 4 YAMLs + env override → Settings
    lint.py            cross-file invariants → SystemExit on violation
  api/routes.py        /v1/chat/completions, /health
  policy/
    detectors.py       Detector protocol: Regex | Presidio | Null
    gate.py            evaluate() → ExecutionPolicyContext
    context.py         builder for ExecutionPolicyContext + allowed_providers
  routing/
    task_mode.py       rules-first → "direct"
    router.py          residency filter → floor → score → select
  gateway/
    gateway.py         dispatch + fallback + confidence escalation loop
    backends/base.py   InferenceBackend Protocol + registry
    backends/mock.py   seeded backend
    backends/vllm.py   STUB
    backends/dynamo.py STUB
  output/verify.py     leakage re-scan + require_verification + stream reject helper
  compression/__init__.py  STUB pass-through
  agent/__init__.py        STUB → "direct"
  obs/export.py            STUB no-op exporter
tests/                 pytest suite + traffic/ stub
scripts/               dev-backend, dev-frontend, test, smoke, lint-config, traffic
ui/ deploy/            scaffolded skeletons
```

---

### Task 1: uv project + FastAPI skeleton + `/health`

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/main.py`, `app/api/__init__.py`, `app/api/routes.py`
- Create: `scripts/dev-backend.sh`, `scripts/test.sh`
- Test: `tests/__init__.py`, `tests/conftest.py`, `tests/test_health.py`

**Interfaces:**
- Produces: `app.main:create_app() -> FastAPI`; `GET /health -> {"status","backend","models"}`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "serving-plane"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pydantic>=2.9",
  "pyyaml>=6.0",
  "httpx>=0.27",
]

[dependency-groups]
dev = ["pytest>=8.3", "presidio-analyzer>=2.2", "presidio-anonymizer>=2.2"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.uv]
package = false
```

- [ ] **Step 2: Create the app factory `app/main.py`**

```python
from fastapi import FastAPI
from app.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 3: Create `app/api/routes.py` with `/health`**

```python
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": "mock", "models": []}
```

- [ ] **Step 4: Create empty `app/__init__.py`, `app/api/__init__.py`, `tests/__init__.py`**

Each is an empty file.

- [ ] **Step 5: Create `tests/conftest.py`**

```python
import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())
```

- [ ] **Step 6: Write the failing test `tests/test_health.py`**

```python
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

- [ ] **Step 7: Install deps and run the test**

Run: `uv sync && uv run pytest tests/test_health.py -v`
Expected: PASS (`test_health_ok`).

- [ ] **Step 8: Create the dev scripts**

`scripts/dev-backend.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
exec uv run uvicorn app.main:app --reload --port 8080
```
`scripts/test.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
exec uv run pytest "$@"
```
Then: `chmod +x scripts/*.sh`

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock app tests scripts
git commit -m "feat: FastAPI skeleton with /health and dev scripts"
```

---

### Task 2: Config schema + loader

**Files:**
- Create: `app/config/__init__.py`, `app/config/schema.py`, `app/config/loader.py`
- Test: `tests/test_config_loader.py`

**Interfaces:**
- Produces:
  - `app.config.schema.Settings` with attrs `serving, routing, policy, pricing` (typed submodels below).
  - `app.config.loader.load_settings(config_dir: str = "config") -> Settings` — reads the 4 YAMLs, applies `SP_`-prefixed env overrides for scalars.
  - Key typed fields other tasks rely on:
    - `Settings.routing.tiers: list[str]`, `.weights: dict[str,float]`, `.complexity_thresholds: dict[str,float]`, `.confidence_escalation_threshold: float`, `.max_escalations: int`, `.risk_floors: dict[str,str]`, `.model_pool: list[PoolCandidate]`, `.score_on: str`.
    - `PoolCandidate(name: str, provider: str, tier: str)`.
    - `Settings.policy.sensitivity_scale: list[str]`, `.default_sensitivity: str`, `.sensitivity_rules: list[SensitivityRule]`, `.budgets: dict`, `.tenants: dict[str,TenantCfg]`.
    - `SensitivityRule(entities: list[str], sensitivity: str, residency: str, risk_class: str, require_verification: bool)`.
    - `TenantCfg(display_name: str, default_residency: str)`.
    - `Settings.pricing.models: dict[str,PriceCfg]` where `PriceCfg(input: float, output: float, tier: str, provider: str)`, `.baseline_model: str`.
    - `Settings.serving.inference_backend: str`, `.allow_backend_fallback: bool`, `.providers: dict[str,ProviderCfg]` where `ProviderCfg(enabled: bool)`, `.pii_detection: str`, `.reject_stream_requests: bool`, `.context_optimization_enabled: bool`.

- [ ] **Step 1: Write `app/config/schema.py`**

```python
from pydantic import BaseModel


class PoolCandidate(BaseModel):
    name: str
    provider: str
    tier: str


class RoutingCfg(BaseModel):
    tiers: list[str]
    score_on: str
    weights: dict[str, float]
    complexity_thresholds: dict[str, float]
    confidence_escalation_threshold: float
    max_escalations: int
    risk_floors: dict[str, str]
    model_pool: list[PoolCandidate]
    fallback: dict[str, int]


class SensitivityRule(BaseModel):
    entities: list[str]
    sensitivity: str
    residency: str
    risk_class: str
    require_verification: bool


class TenantCfg(BaseModel):
    display_name: str
    default_residency: str


class PolicyCfg(BaseModel):
    sensitivity_scale: list[str]
    default_sensitivity: str
    sensitivity_rules: list[SensitivityRule]
    budgets: dict[str, float]
    agent_budgets: dict[str, float]
    tenants: dict[str, TenantCfg]


class PriceCfg(BaseModel):
    input: float
    output: float
    tier: str
    provider: str


class PricingCfg(BaseModel):
    models: dict[str, PriceCfg]
    baseline_model: str


class ProviderCfg(BaseModel):
    enabled: bool


class ServingCfg(BaseModel):
    inference_backend: str
    allow_backend_fallback: bool
    providers: dict[str, ProviderCfg]
    pii_detection: str
    reject_stream_requests: bool
    context_optimization_enabled: bool


class Settings(BaseModel):
    serving: ServingCfg
    routing: RoutingCfg
    policy: PolicyCfg
    pricing: PricingCfg
```

- [ ] **Step 2: Write `app/config/loader.py`**

```python
import os
from pathlib import Path

import yaml

from app.config.schema import (
    PolicyCfg, PricingCfg, RoutingCfg, ServingCfg, Settings,
)


def _read(config_dir: str, name: str) -> dict:
    return yaml.safe_load(Path(config_dir, name).read_text()) or {}


def load_settings(config_dir: str = "config") -> Settings:
    serving_raw = _read(config_dir, "serving.yaml")
    routing_raw = _read(config_dir, "routing.yaml")
    policy_raw = _read(config_dir, "policy.yaml")
    pricing_raw = _read(config_dir, "pricing.yaml")

    serving = ServingCfg(
        inference_backend=os.environ.get(
            "SP_INFERENCE_BACKEND", serving_raw["inference_backend"]),
        allow_backend_fallback=serving_raw["allow_backend_fallback"],
        providers=serving_raw["providers"],
        pii_detection=os.environ.get(
            "SP_PII_DETECTION", serving_raw["policy"]["pii_detection"]),
        reject_stream_requests=serving_raw["server"]["reject_stream_requests"],
        context_optimization_enabled=serving_raw["context_optimization"]["enabled"],
    )
    routing = RoutingCfg(**routing_raw)
    policy = PolicyCfg(**policy_raw)
    pricing = PricingCfg(**pricing_raw)
    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing)
```

- [ ] **Step 3: Create empty `app/config/__init__.py`**

- [ ] **Step 4: Write the failing test `tests/test_config_loader.py`**

```python
from app.config.loader import load_settings


def test_loads_real_config():
    s = load_settings()
    assert s.routing.tiers == ["cheap", "mid", "frontier"]
    assert s.policy.default_sensitivity == "public"
    assert s.pricing.baseline_model == "bedrock-frontier"
    assert {c.provider for c in s.routing.model_pool} == {"onprem", "azure", "bedrock"}


def test_env_override(monkeypatch):
    monkeypatch.setenv("SP_INFERENCE_BACKEND", "none")
    assert load_settings().serving.inference_backend == "none"
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: PASS (both).

- [ ] **Step 6: Commit**

```bash
git add app/config tests/test_config_loader.py
git commit -m "feat: typed config schema + loader with env override"
```

---

### Task 3: Cross-file config lint + startup wiring

**Files:**
- Create: `app/config/lint.py`
- Modify: `app/main.py` (call lint at startup), `app/api/routes.py` (`/health` reports real backend/models)
- Create: `scripts/lint-config.sh`
- Test: `tests/test_config_lint.py`

**Interfaces:**
- Produces: `app.config.lint.lint_settings(s: Settings) -> list[str]` (returns violation messages, empty if clean); `app.config.lint.enforce(s: Settings) -> None` (raises `SystemExit(1)` printing named violations); runnable as `python -m app.config.lint`.

- [ ] **Step 1: Write `app/config/lint.py`**

```python
import sys

from app.config.loader import load_settings
from app.config.schema import Settings

_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}


def lint_settings(s: Settings) -> list[str]:
    errs: list[str] = []

    serv_prov = set(s.serving.providers)
    pool_prov = {c.provider for c in s.routing.model_pool}
    price_prov = {p.provider for p in s.pricing.models.values()}
    if not (serv_prov == pool_prov == price_prov):
        errs.append(
            f"PROVIDER_NAME_DRIFT serving={sorted(serv_prov)} "
            f"routing={sorted(pool_prov)} pricing={sorted(price_prov)}")

    tiers = set(s.routing.tiers)
    pool_tiers = [c.tier for c in s.routing.model_pool]
    for t in s.routing.risk_floors.values():
        if t not in tiers:
            errs.append(f"RISK_FLOOR_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in set(pool_tiers):
        if t not in tiers:
            errs.append(f"POOL_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in tiers:
        if t not in pool_tiers:
            errs.append(f"TIER_HAS_NO_MEMBER {t}")
    for name, p in s.pricing.models.items():
        if p.tier not in tiers:
            errs.append(f"PRICING_TIER_UNKNOWN {name}:{p.tier}")

    scale = set(s.policy.sensitivity_scale)
    if s.policy.default_sensitivity not in scale:
        errs.append(f"DEFAULT_SENSITIVITY_UNKNOWN {s.policy.default_sensitivity}")
    for r in s.policy.sensitivity_rules:
        if r.sensitivity not in scale:
            errs.append(f"RULE_SENSITIVITY_UNKNOWN {r.sensitivity}")
        if "on_prem" in r.residency:
            errs.append(f"RESIDENCY_SPELLING {r.residency} (use onprem_only)")

    return errs


def enforce(s: Settings) -> None:
    errs = lint_settings(s)
    if errs:
        for e in errs:
            print(f"CONFIG_LINT: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    enforce(load_settings())
    print("CONFIG_LINT: ok")
```

- [ ] **Step 2: Wire lint + settings into `app/main.py`**

```python
from fastapi import FastAPI

from app.api.routes import router
from app.config.lint import enforce
from app.config.loader import load_settings


def create_app() -> FastAPI:
    settings = load_settings()
    enforce(settings)
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.state.settings = settings
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 3: Make `/health` report the real backend + model list**

Replace `app/api/routes.py` `health()`:
```python
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state.settings
    return {
        "status": "ok",
        "backend": s.serving.inference_backend,
        "models": [c.name for c in s.routing.model_pool],
    }
```

- [ ] **Step 4: Write the failing test `tests/test_config_lint.py`**

```python
import copy
import pytest

from app.config.lint import enforce, lint_settings
from app.config.loader import load_settings


def test_current_config_is_clean():
    assert lint_settings(load_settings()) == []


def test_provider_drift_is_caught():
    s = load_settings()
    s = copy.deepcopy(s)
    s.routing.model_pool[0].provider = "on_prem"  # drifted spelling
    errs = lint_settings(s)
    assert any("PROVIDER_NAME_DRIFT" in e for e in errs)


def test_enforce_exits_on_violation():
    s = copy.deepcopy(load_settings())
    s.routing.risk_floors["high"] = "strong"  # nonexistent tier
    with pytest.raises(SystemExit):
        enforce(s)
```

- [ ] **Step 5: Run the tests + the CLI**

Run: `uv run pytest tests/test_config_lint.py -v && uv run python -m app.config.lint`
Expected: tests PASS; CLI prints `CONFIG_LINT: ok`.

- [ ] **Step 6: Create `scripts/lint-config.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
exec uv run python -m app.config.lint
```
Then `chmod +x scripts/lint-config.sh`.

- [ ] **Step 7: Commit**

```bash
git add app/config/lint.py app/main.py app/api/routes.py tests/test_config_lint.py scripts/lint-config.sh
git commit -m "feat: startup config lint enforcing cross-file invariants"
```

---

### Task 4: Scaffold stub tree (NAT, Headroom, obs, backends, ui, deploy, traffic)

**Files:**
- Create stubs: `app/agent/__init__.py`, `app/compression/__init__.py`, `app/obs/export.py`, `app/gateway/__init__.py`, `app/gateway/backends/__init__.py`, `app/gateway/backends/vllm.py`, `app/gateway/backends/dynamo.py`
- Create: `ui/README.md`, `deploy/compose/docker-compose.yml`, `deploy/helm/serving-plane/README.md`, `tests/traffic/__init__.py`, `tests/traffic/__main__.py`, `tests/traffic/README.md`
- Create: `scripts/dev-frontend.sh`, `scripts/traffic.sh`, `scripts/README.md`
- Test: `tests/test_stubs.py`

**Interfaces:**
- Produces (interfaces later tasks/phases fill; used now only to prove importability):
  - `app.compression.compress(messages, ctx) -> CompressionResult` (real signature defined in Task 10; here a placeholder that raises is NOT used — Task 10 replaces this file).
  - `app.agent.decide_mode() -> str` returning `"direct"`.
  - `app.obs.export.export(trace) -> None` no-op.

- [ ] **Step 1: Write `app/agent/__init__.py`**

```python
"""NAT agent runtime — STUB (design §10.3). Multi-step lands in Phase 5."""


def decide_mode() -> str:
    return "direct"
```

- [ ] **Step 2: Write `app/obs/export.py`**

```python
"""Langfuse/NAT OTel exporter — STUB (design §10.4). No-op this pass."""
from __future__ import annotations


def export(trace: list) -> None:  # redacting exporter lands in Phase 4
    return None
```

- [ ] **Step 3: Write the backend stubs**

`app/gateway/backends/vllm.py`:
```python
"""vLLM backend — STUB (design §10.6). Phase 9."""


class VLLMBackend:
    name = "vllm"

    async def generate(self, *a, **k):
        raise NotImplementedError("vLLM backend lands in Phase 9")
```
`app/gateway/backends/dynamo.py`:
```python
"""Dynamo + TensorRT-LLM backend — STUB (design §10.7). Phase 9."""


class DynamoBackend:
    name = "dynamo"

    async def generate(self, *a, **k):
        raise NotImplementedError("Dynamo backend lands in Phase 9")
```

- [ ] **Step 4: Create empty `__init__.py` files**

`app/gateway/__init__.py`, `app/gateway/backends/__init__.py`, `app/compression/__init__.py` (empty for now — Task 10 fills it), `app/obs/__init__.py`, `tests/traffic/__init__.py` — all empty.

- [ ] **Step 5: Create the non-Python scaffolds**

`tests/traffic/__main__.py`:
```python
"""Traffic generator — STUB (traffic-and-tests skill). Phase 7."""

if __name__ == "__main__":
    print("traffic generator not implemented yet (Phase 7)")
```
`tests/traffic/README.md`: one line — `Stub. See .claude/skills/traffic-and-tests. Built in Phase 7.`
`ui/README.md`: one line — `Vite/React/Tailwind UI skeleton. See .claude/skills/demo-ui. Built in Phase 6.`
`deploy/compose/docker-compose.yml`:
```yaml
# Skeleton — see greenlake-deploy skill. Full compose lands in Phase 8.
services:
  api:
    build: ..
    ports: ["8080:8080"]
    environment: [SP_INFERENCE_BACKEND=mock]
  # langfuse / postgres / clickhouse / vllm added in Phase 8
```
`deploy/helm/serving-plane/README.md`: one line — `Helm chart skeleton. See greenlake-deploy skill. Built in Phase 8.`

- [ ] **Step 6: Create the remaining scripts**

`scripts/dev-frontend.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../ui" && exec npm run dev
```
`scripts/traffic.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
exec uv run python -m tests.traffic "$@"
```
`scripts/README.md`:
```markdown
# scripts
- dev-backend.sh  — run the API (uvicorn, reload)
- dev-frontend.sh — run the UI dev server (stub UI this pass)
- test.sh         — run pytest
- smoke.sh        — boot API + live httpx checks
- lint-config.sh  — validate the 4 config YAMLs
- traffic.sh      — run the traffic generator (stub this pass)
```
Then `chmod +x scripts/*.sh`.

- [ ] **Step 7: Write the failing test `tests/test_stubs.py`**

```python
def test_agent_stub_returns_direct():
    from app.agent import decide_mode
    assert decide_mode() == "direct"


def test_obs_export_is_noop():
    from app.obs.export import export
    assert export([]) is None


def test_backend_stubs_import():
    from app.gateway.backends.vllm import VLLMBackend
    from app.gateway.backends.dynamo import DynamoBackend
    assert VLLMBackend().name == "vllm"
    assert DynamoBackend().name == "dynamo"
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_stubs.py -v`
Expected: PASS (3).

- [ ] **Step 9: Commit**

```bash
git add app/agent app/obs app/gateway app/compression ui deploy tests/traffic scripts tests/test_stubs.py
git commit -m "chore: scaffold stub tree for NAT, Headroom, obs, backends, ui, deploy"
```

---

### Task 5: Shared models + enums

**Files:**
- Create: `app/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - Enums built from config: `make_enums(settings) -> (Sensitivity, RiskClass, Residency)` — but to keep imports simple, define **plain `str` constants + validation sets** instead of dynamic enums:
    - `SENSITIVITY_SCALE`, `RISK_CLASSES = ("low","medium","high")`, `RESIDENCIES = ("any","private_cloud_ok","onprem_only")` — validated against config at app startup (Task 8).
  - `Message(role: str, content: str)`
  - `ChatRequest(model: str = "auto", messages: list[Message], stream: bool = False)` with `model_config = ConfigDict(extra="allow")` so mock test-hooks pass through.
  - `Usage(prompt_tokens: int, completion_tokens: int, total_tokens: int)`
  - `Choice(index: int, message: Message, finish_reason: str)`
  - `ChatResponse(id, object, model, choices, usage, trace: list[dict])`
  - `Entity(type: str, start: int, end: int, value: str)` — `value` used only inside the gate.
  - `ExecutionPolicyContext` (frozen) — fields per design §4.
  - `RejectedCandidate(name, provider, tier, reason)`
  - `DecisionRecord(step, chosen, reason, rejected, est_cost_usd, actual_cost_usd, latency_ms, hop_index)`
  - `VerificationRecord(step, required, performed, passed, entities_found, escalated, escalations_used, reason, latency_ms)`
  - `GenerationResult(model, text, tokens_in, tokens_out, latency_ms, cost_usd, confidence)`

- [ ] **Step 1: Write `app/models.py`**

```python
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

RISK_CLASSES = ("low", "medium", "high")
RESIDENCIES = ("any", "private_cloud_ok", "onprem_only")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = "auto"
    messages: list[Message]
    stream: bool = False


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: str = "stop"


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: str
    choices: list[Choice]
    usage: Usage
    trace: list[dict] = Field(default_factory=list)


class Entity(BaseModel):
    type: str
    start: int
    end: int
    value: str  # never leaves the gate


class ExecutionPolicyContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    max_cost_usd: float
    max_latency_ms: int
    risk_class: str
    require_verification: bool
    detected_entities: tuple[str, ...]  # TYPES only
    redaction_map_id: str | None


class RejectedCandidate(BaseModel):
    name: str
    provider: str
    tier: str
    reason: str


class DecisionRecord(BaseModel):
    step: str
    chosen: str | None
    reason: str
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    est_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    latency_ms: float = 0.0
    hop_index: int = 0


class VerificationRecord(BaseModel):
    step: str = "verify"
    required: bool
    performed: bool
    passed: bool
    entities_found: tuple[str, ...] = ()
    escalated: bool = False
    escalations_used: int = 0
    reason: str = ""
    latency_ms: float = 0.0


class GenerationResult(BaseModel):
    model: str
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    cost_usd: float
    confidence: float
```

- [ ] **Step 2: Write the failing test `tests/test_models.py`**

```python
import pytest
from pydantic import ValidationError

from app.models import ExecutionPolicyContext, ChatRequest


def test_context_is_frozen():
    ctx = ExecutionPolicyContext(
        request_id="r1", tenant_id="default", sensitivity="public",
        onprem_only=False, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=0.05, max_latency_ms=15000,
        risk_class="low", require_verification=False,
        detected_entities=(), redaction_map_id=None)
    with pytest.raises(ValidationError):
        ctx.onprem_only = True


def test_chat_request_allows_mock_hooks():
    req = ChatRequest.model_validate(
        {"messages": [{"role": "user", "content": "hi"}], "mock_confidence": 0.3})
    assert req.messages[0].content == "hi"
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS (2).

- [ ] **Step 4: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: shared Pydantic models and enums"
```

---

### Task 6: Detectors (regex, presidio, null)

**Files:**
- Create: `app/policy/__init__.py`, `app/policy/detectors.py`
- Test: `tests/test_detectors.py`

**Interfaces:**
- Produces:
  - `Detector` protocol: `analyze(text: str) -> list[Entity]`, `anonymize(text: str, entities: list[Entity]) -> tuple[str, dict[str,str]]` (returns redacted text + `{placeholder: value}` map).
  - `RegexDetector`, `NullDetector`, `PresidioDetector`.
  - `make_detector(mode: str) -> Detector` factory (`regex | off | presidio`).

- [ ] **Step 1: Write `app/policy/detectors.py`**

```python
from __future__ import annotations

import re
from typing import Protocol

from app.models import Entity

_PATTERNS = {
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "EMAIL_ADDRESS": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "PHONE_NUMBER": re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b"),
    "US_BANK_NUMBER": re.compile(r"\b\d{8,17}\b"),
}


class Detector(Protocol):
    def analyze(self, text: str) -> list[Entity]: ...
    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]: ...


class RegexDetector:
    def analyze(self, text: str) -> list[Entity]:
        out: list[Entity] = []
        for etype, pat in _PATTERNS.items():
            for m in pat.finditer(text):
                out.append(Entity(type=etype, start=m.start(), end=m.end(),
                                  value=m.group()))
        # longest-match wins on overlap; keep highest-priority by pattern order
        out.sort(key=lambda e: (e.start, -(e.end - e.start)))
        deduped: list[Entity] = []
        last_end = -1
        for e in out:
            if e.start >= last_end:
                deduped.append(e)
                last_end = e.end
        return deduped

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        redacted = text
        mapping: dict[str, str] = {}
        counters: dict[str, int] = {}
        for e in sorted(entities, key=lambda e: e.start, reverse=True):
            counters[e.type] = counters.get(e.type, 0) + 1
            ph = f"<{e.type}_{counters[e.type]}>"
            mapping[ph] = e.value
            redacted = redacted[:e.start] + ph + redacted[e.end:]
        return redacted, mapping


class NullDetector:
    def analyze(self, text: str) -> list[Entity]:
        return []

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        return text, {}


class PresidioDetector:
    """Real Presidio path (demo default). Lazy import so tests never need it."""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()

    def analyze(self, text: str) -> list[Entity]:
        results = self._analyzer.analyze(text=text, language="en")
        return [Entity(type=r.entity_type, start=r.start, end=r.end,
                       value=text[r.start:r.end]) for r in results]

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        # reuse the deterministic placeholder scheme for map symmetry
        return RegexDetector().anonymize(text, entities)


def make_detector(mode: str) -> Detector:
    if mode == "regex":
        return RegexDetector()
    if mode == "off":
        return NullDetector()
    if mode == "presidio":
        return PresidioDetector()
    raise ValueError(f"unknown pii_detection mode: {mode}")
```

- [ ] **Step 2: Write the failing test `tests/test_detectors.py`**

```python
from app.policy.detectors import RegexDetector, make_detector


def test_regex_detects_and_redacts_ssn():
    d = RegexDetector()
    ents = d.analyze("my ssn is 123-45-6789 ok")
    assert any(e.type == "US_SSN" for e in ents)
    redacted, mapping = d.anonymize("my ssn is 123-45-6789 ok", ents)
    assert "123-45-6789" not in redacted
    assert "<US_SSN_1>" in redacted
    assert mapping["<US_SSN_1>"] == "123-45-6789"


def test_null_detector_finds_nothing():
    assert make_detector("off").analyze("123-45-6789") == []


def test_clean_text_no_entities():
    assert RegexDetector().analyze("what is the capital of france") == []
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_detectors.py -v`
Expected: PASS (3).

- [ ] **Step 4: Commit**

```bash
git add app/policy/__init__.py app/policy/detectors.py tests/test_detectors.py
git commit -m "feat: pluggable PII detectors (regex default for tests, presidio real)"
```

---

### Task 7: Policy context builder + `allowed_providers`

**Files:**
- Create: `app/policy/context.py`
- Test: `tests/test_context.py`

**Interfaces:**
- Consumes: `Settings` (Task 2), `Entity`/`ExecutionPolicyContext` (Task 5).
- Produces: `app.policy.context.build_context(settings, request_id, tenant_id, entities, redaction_map_id) -> ExecutionPolicyContext`.
  - Chooses the **highest-severity** matching `sensitivity_rule` by entity overlap; falls back to the last rule (`entities: []`, `public`).
  - `residency` = the stricter of the matched rule's residency and the tenant's `default_residency` (`onprem_only` > `private_cloud_ok` > `any`).
  - `onprem_only = residency == "onprem_only"`.
  - `allowed_providers`: start from `serving.providers` where `enabled`; if `onprem_only`, keep only providers whose pool tiers are on-prem (provider == `onprem`); if `private_cloud_ok`, keep onprem + enabled cloud; if `any`, all enabled.

- [ ] **Step 1: Write `app/policy/context.py`**

```python
from __future__ import annotations

from app.config.schema import Settings
from app.models import Entity, ExecutionPolicyContext

_RESIDENCY_RANK = {"any": 0, "private_cloud_ok": 1, "onprem_only": 2}


def _stricter(a: str, b: str) -> str:
    return a if _RESIDENCY_RANK[a] >= _RESIDENCY_RANK[b] else b


def _match_rule(settings: Settings, etypes: set[str]):
    for rule in settings.policy.sensitivity_rules:
        if rule.entities and etypes & set(rule.entities):
            return rule
    return settings.policy.sensitivity_rules[-1]  # the public/empty rule


def build_context(settings: Settings, request_id: str, tenant_id: str,
                  entities: list[Entity], redaction_map_id: str | None
                  ) -> ExecutionPolicyContext:
    etypes = {e.type for e in entities}
    rule = _match_rule(settings, etypes)

    tenant = settings.policy.tenants.get(
        tenant_id, settings.policy.tenants["default"])
    residency = _stricter(rule.residency, tenant.default_residency)
    onprem_only = residency == "onprem_only"

    enabled = {p for p, c in settings.serving.providers.items() if c.enabled}
    onprem_providers = {c.provider for c in settings.routing.model_pool
                        if c.provider == "onprem"}
    if onprem_only:
        allowed = enabled & onprem_providers
    else:
        allowed = enabled  # private_cloud_ok and any both allow enabled cloud here

    allowed_models = frozenset(
        c.name for c in settings.routing.model_pool if c.provider in allowed)

    return ExecutionPolicyContext(
        request_id=request_id,
        tenant_id=tenant_id,
        sensitivity=rule.sensitivity,
        onprem_only=onprem_only,
        allowed_providers=frozenset(allowed),
        allowed_models=allowed_models,
        max_cost_usd=settings.policy.budgets["default_max_spend_usd"],
        max_latency_ms=int(settings.policy.budgets["default_max_latency_ms"]),
        risk_class=rule.risk_class,
        require_verification=rule.require_verification,
        detected_entities=tuple(sorted(etypes)),
        redaction_map_id=redaction_map_id,
    )
```

- [ ] **Step 2: Write the failing test `tests/test_context.py`**

```python
from app.config.loader import load_settings
from app.models import Entity
from app.policy.context import build_context

S = load_settings()


def test_ssn_forces_onprem_only():
    ents = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = build_context(S, "r1", "default", ents, "map1")
    assert ctx.sensitivity == "restricted"
    assert ctx.onprem_only is True
    assert "azure" not in ctx.allowed_providers
    assert "bedrock" not in ctx.allowed_providers
    assert ctx.require_verification is True


def test_clean_request_full_pool():
    ctx = build_context(S, "r1", "default", [], None)
    assert ctx.sensitivity == "public"
    assert ctx.onprem_only is False
    assert "onprem" in ctx.allowed_providers


def test_tenant_residency_tightens():
    # hospital defaults to onprem_only even with no entities
    ctx = build_context(S, "hospital", "hospital", [], None)
    assert ctx.onprem_only is True
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_context.py -v`
Expected: PASS (3).

- [ ] **Step 4: Commit**

```bash
git add app/policy/context.py tests/test_context.py
git commit -m "feat: policy context builder computing allowed_providers in the gate"
```

---

### Task 8: Gate.evaluate + fail-closed, wired into the route

**Files:**
- Create: `app/policy/gate.py`
- Modify: `app/main.py` (build gate at startup, validate enums), `app/api/routes.py` (POST `/v1/chat/completions` calls the gate; fail-closed 503)
- Test: `tests/test_policy_gate.py`

**Interfaces:**
- Consumes: `Detector` (Task 6), `build_context` (Task 7), `Settings`.
- Produces:
  - `app.policy.gate.Gate(settings, detector)` with `evaluate(request_id, tenant_id, request) -> tuple[ExecutionPolicyContext, str, dict]` returning `(ctx, sanitized_text, redaction_map)`. The redaction map is returned to the **caller inside the app process only** (never serialized to the response); the ctx carries only `redaction_map_id`.
  - `Gate.evaluate` raises on internal failure; the route converts that to HTTP 503.

- [ ] **Step 1: Write `app/policy/gate.py`**

```python
from __future__ import annotations

import uuid

from app.config.schema import Settings
from app.models import ChatRequest, ExecutionPolicyContext
from app.policy.context import build_context
from app.policy.detectors import Detector


class Gate:
    def __init__(self, settings: Settings, detector: Detector) -> None:
        self._settings = settings
        self._detector = detector

    def evaluate(self, request_id: str, tenant_id: str, request: ChatRequest
                 ) -> tuple[ExecutionPolicyContext, str, dict[str, str]]:
        # scan the latest user message (the live prompt) + prior content
        text = "\n".join(m.content for m in request.messages)
        entities = self._detector.analyze(text)
        redacted, mapping = self._detector.anonymize(text, entities)
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id)
        return ctx, redacted, mapping
```

- [ ] **Step 2: Add tenant auth + gate construction to `app/main.py`**

```python
from fastapi import FastAPI

from app.api.routes import router
from app.config.lint import enforce
from app.config.loader import load_settings
from app.policy.detectors import make_detector
from app.policy.gate import Gate

# static demo api-key -> tenant map (design §3). Unknown/missing -> default.
API_KEYS = {"acme-key": "acme", "hospital-key": "hospital", "demo-key": "default"}


def create_app() -> FastAPI:
    settings = load_settings()
    enforce(settings)
    detector = make_detector(settings.serving.pii_detection)
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.state.settings = settings
    app.state.api_keys = API_KEYS
    app.state.gate = Gate(settings, detector)
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 3: Add POST `/v1/chat/completions` (gate-only for now) to `app/api/routes.py`**

```python
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.models import ChatRequest

router = APIRouter()


def _tenant_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    key = auth.removeprefix("Bearer ").strip()
    return request.app.state.api_keys.get(key, "default")


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state.settings
    return {"status": "ok", "backend": s.serving.inference_backend,
            "models": [c.name for c in s.routing.model_pool]}


@router.post("/v1/chat/completions")
def chat_completions(body: ChatRequest, request: Request) -> dict:
    s = request.app.state.settings
    if body.stream and s.serving.reject_stream_requests:
        raise HTTPException(400, "streaming is not supported in the demo")
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    tenant = _tenant_of(request)
    try:
        ctx, _sanitized, _map = request.app.state.gate.evaluate(
            request_id, tenant, body)
    except Exception:
        raise HTTPException(503, "policy evaluation unavailable")
    # pipeline wiring lands in Task 14; return the gate verdict for now
    return {"request_id": request_id, "sensitivity": ctx.sensitivity,
            "onprem_only": ctx.onprem_only,
            "allowed_providers": sorted(ctx.allowed_providers),
            "detected_entities": list(ctx.detected_entities)}
```

- [ ] **Step 4: Write the failing test `tests/test_policy_gate.py`**

```python
def test_ssn_request_is_onprem_only(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "verify SSN 123-45-6789"}]})
    body = r.json()
    assert body["onprem_only"] is True
    assert body["allowed_providers"] == ["onprem"]
    assert "US_SSN" in body["detected_entities"]


def test_clean_request_full_pool(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    body = r.json()
    assert body["onprem_only"] is False
    assert body["sensitivity"] == "public"


def test_stream_rejected(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 400


def test_no_ssn_value_in_response(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "SSN 123-45-6789"}]})
    assert "123-45-6789" not in r.text
```

- [ ] **Step 5: Run the tests** (force regex detector so it's offline)

Run: `SP_PII_DETECTION=regex uv run pytest tests/test_policy_gate.py -v`
Expected: PASS (4).

- [ ] **Step 6: Make regex the test default** — add to `tests/conftest.py`:

```python
import os
os.environ.setdefault("SP_PII_DETECTION", "regex")
```
(place at the very top of the file, before the app import).

- [ ] **Step 7: Commit**

```bash
git add app/policy/gate.py app/main.py app/api/routes.py tests/test_policy_gate.py tests/conftest.py
git commit -m "feat: policy gate wired into /v1/chat/completions, fail-closed"
```

---

### Task 9: Backend Protocol + Mock backend

**Files:**
- Create: `app/gateway/backends/base.py`, rewrite `app/gateway/backends/mock.py`
- Test: `tests/test_backends.py`

**Interfaces:**
- Consumes: `GenerationResult` (Task 5), `Settings` pricing.
- Produces:
  - `InferenceBackend` Protocol: `generate(model, messages, price, tier, hint) -> GenerationResult`, `name: str`.
  - `MockBackend(settings)`. Deterministic by prompt hash. Reads optional `hint` dict (from `ChatRequest` extras) keys `mock_confidence` (float) and `mock_fail` (bool). Latency by tier: cheap 200, mid 500, frontier 2000 ms. `tokens_out` seeded from hash in [40, 200]. `cost_usd = (tokens_in*price.input + tokens_out*price.output)/1_000_000`. `confidence = hint["mock_confidence"] if present else 0.95`.
  - `count_tokens(text) -> int` = `max(1, len(text)//4)`.

- [ ] **Step 1: Write `app/gateway/backends/base.py`**

```python
from __future__ import annotations

from typing import Protocol

from app.config.schema import PriceCfg
from app.models import GenerationResult, Message


def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class InferenceBackend(Protocol):
    name: str

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict) -> GenerationResult: ...
```

- [ ] **Step 2: Write `app/gateway/backends/mock.py`**

```python
from __future__ import annotations

import hashlib

from app.config.schema import PriceCfg, Settings
from app.gateway.backends.base import count_tokens
from app.models import GenerationResult, Message

_LATENCY_MS = {"cheap": 200.0, "mid": 500.0, "frontier": 2000.0}


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
            latency_ms=_LATENCY_MS.get(tier, 500.0), cost_usd=cost,
            confidence=confidence)
```

- [ ] **Step 3: Write the failing test `tests/test_backends.py`**

```python
import asyncio

from app.config.loader import load_settings
from app.gateway.backends.mock import MockBackend
from app.models import Message

S = load_settings()


def _gen(model, tier, hint=None):
    price = S.pricing.models[model]
    return asyncio.run(MockBackend(S).generate(
        model, [Message(role="user", content="hello world")], price, tier, hint or {}))


def test_mock_is_deterministic():
    a = _gen("local-8b", "cheap")
    b = _gen("local-8b", "cheap")
    assert a.tokens_out == b.tokens_out and a.cost_usd == b.cost_usd


def test_local_tier_is_free():
    assert _gen("local-8b", "cheap").cost_usd == 0.0


def test_confidence_hint_respected():
    assert _gen("local-8b", "cheap", {"mock_confidence": 0.3}).confidence == 0.3
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_backends.py -v`
Expected: PASS (3).

- [ ] **Step 5: Commit**

```bash
git add app/gateway/backends/base.py app/gateway/backends/mock.py tests/test_backends.py
git commit -m "feat: InferenceBackend protocol + deterministic mock backend"
```

---

### Task 10: Compression stub + task_mode

**Files:**
- Rewrite: `app/compression/__init__.py`
- Create: `app/routing/__init__.py`, `app/routing/task_mode.py`
- Test: `tests/test_compression_and_mode.py`

**Interfaces:**
- Consumes: `Message`, `CompressionRecord` fields, `count_tokens`.
- Produces:
  - `app.compression.compress(messages, ctx) -> CompressionResult` where `CompressionResult(messages, tokens_before, tokens_after, record: dict)`. Pass-through: `messages` unchanged, `tokens_after == tokens_before`, `record = {"enabled": False, "applied": False, "bypass_reason": "disabled", "tokens_before": n, "tokens_after": n, "reduction_pct": 0.0, "ccr_retrievals_to_cloud": 0}`.
  - `app.routing.task_mode.decide(ctx, request) -> str` → always `"direct"` this pass (classifier off).

- [ ] **Step 1: Write `app/compression/__init__.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from app.gateway.backends.base import count_tokens
from app.models import ExecutionPolicyContext, Message


@dataclass
class CompressionResult:
    messages: list[Message]
    tokens_before: int
    tokens_after: int
    record: dict


def compress(messages: list[Message], ctx: ExecutionPolicyContext) -> CompressionResult:
    """Headroom STUB — pass-through. Router prices tokens_after (== before now)."""
    n = sum(count_tokens(m.content) for m in messages)
    record = {"enabled": False, "applied": False, "bypass_reason": "disabled",
              "tokens_before": n, "tokens_after": n, "reduction_pct": 0.0,
              "ccr_retrievals_to_cloud": 0}
    return CompressionResult(messages=messages, tokens_before=n, tokens_after=n,
                             record=record)
```

- [ ] **Step 2: Write `app/routing/task_mode.py`**

```python
from __future__ import annotations

from app.models import ChatRequest, ExecutionPolicyContext


def decide(ctx: ExecutionPolicyContext, request: ChatRequest) -> str:
    """Rules-first task mode. Classifier is OFF this pass (design §10.3)."""
    return "direct"
```

- [ ] **Step 3: Create empty `app/routing/__init__.py`**

- [ ] **Step 4: Write the failing test `tests/test_compression_and_mode.py`**

```python
from app.compression import compress
from app.models import ChatRequest, ExecutionPolicyContext, Message
from app.routing.task_mode import decide

CTX = ExecutionPolicyContext(
    request_id="r", tenant_id="default", sensitivity="public", onprem_only=False,
    allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
    max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
    require_verification=False, detected_entities=(), redaction_map_id=None)


def test_compression_is_passthrough():
    msgs = [Message(role="user", content="a" * 400)]
    res = compress(msgs, CTX)
    assert res.tokens_after == res.tokens_before
    assert res.record["applied"] is False
    assert res.record["ccr_retrievals_to_cloud"] == 0


def test_task_mode_is_direct():
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    assert decide(CTX, req) == "direct"
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_compression_and_mode.py -v`
Expected: PASS (2).

- [ ] **Step 6: Commit**

```bash
git add app/compression/__init__.py app/routing/__init__.py app/routing/task_mode.py tests/test_compression_and_mode.py
git commit -m "feat: compression pass-through stub + rules-first task mode"
```

---

### Task 11: Router (residency filter → target tier → score within tier)

**Files:**
- Create: `app/routing/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Settings`, `ExecutionPolicyContext`, `PoolCandidate`, `RejectedCandidate`.
- Produces: `app.routing.router.Router(settings)` with `select(ctx, tokens_after, complexity, min_tier=None) -> RoutingDecision` where `RoutingDecision(chosen: PoolCandidate, est_cost_usd: float, rejected: list[RejectedCandidate], reason: str)`.
  - Order (residency beats risk floor). The complexity/risk floor picks a **target tier**, and scoring happens **within that tier** (choosing the provider/model), NOT across all higher tiers — otherwise a free higher tier always wins and low-complexity requests never land on `cheap`. Higher tiers are reached only via `min_tier` (escalation) or when the target tier has no allowed member.
    1. **Residency filter:** drop candidates whose provider ∉ `ctx.allowed_providers` → `rejected` reason `"excluded by policy (residency)"`.
    2. **Target tier:** `target = max(complexity_tier(complexity), risk_floors[ctx.risk_class], min_tier)` by tier order, then **clamp to the best allowed tier** (can't exceed what residency permits). `tier_pick` = smallest allowed tier `>= target` (nearest-up if the exact target has no allowed member).
    3. **Tier split:** candidates not in `tier_pick` → `rejected` reason `"below target tier"` / `"above target tier"`.
    4. **Budget:** within `tier_pick`, drop candidates whose est cost > `ctx.max_cost_usd` → reason `"over budget"`.
    5. **Score** the survivors (all in one tier, so this chooses provider/model); pick max; others → reason `"lower score"`. Ties broken by cheaper est cost.
  - `complexity_tier`: `cheap` if `≤ cheap_max`, `mid` if `≤ mid_max`, else `frontier`.
  - `est_cost = tokens_after/1e6 * (price.input + price.output)` (rough symmetric estimate).
  - `score = W["quality"]*Q[tier] - W["cost"]*norm_cost - W["latency"]*L[tier] + W["capacity"]*1.0`, with `Q={cheap:0.5,mid:0.75,frontier:1.0}`, `L={cheap:0.2,mid:0.5,frontier:1.0}`, `norm_cost = est_cost / max_est_cost_over_survivors (0 if all zero)`. Within one tier `Q`/`L` are constant, so scoring reduces to preferring the cheaper (on-prem, free) model.

- [ ] **Step 1: Write `app/routing/router.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field

from app.config.schema import PoolCandidate, Settings
from app.models import ExecutionPolicyContext, RejectedCandidate

_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}
_Q = {"cheap": 0.5, "mid": 0.75, "frontier": 1.0}
_L = {"cheap": 0.2, "mid": 0.5, "frontier": 1.0}


@dataclass
class RoutingDecision:
    chosen: PoolCandidate
    est_cost_usd: float
    reason: str
    rejected: list[RejectedCandidate] = field(default_factory=list)


class Router:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def _complexity_tier(self, complexity: float) -> str:
        t = self._s.routing.complexity_thresholds
        if complexity <= t["cheap_max"]:
            return "cheap"
        if complexity <= t["mid_max"]:
            return "mid"
        return "frontier"

    def _est_cost(self, cand: PoolCandidate, tokens_after: int) -> float:
        p = self._s.pricing.models[cand.name]
        return tokens_after / 1_000_000 * (p.input + p.output)

    def select(self, ctx: ExecutionPolicyContext, tokens_after: int,
               complexity: float, min_tier: str | None = None) -> RoutingDecision:
        rejected: list[RejectedCandidate] = []
        pool = self._s.routing.model_pool

        # 1. residency filter (residency beats everything below)
        allowed = []
        for c in pool:
            if c.provider in ctx.allowed_providers:
                allowed.append(c)
            else:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    reason="excluded by policy (residency)"))
        if not allowed:
            raise ValueError("no candidates after residency filter")

        # 2. target tier from complexity + risk floor + escalation, clamped to
        #    the best residency-permitted tier
        targets = [self._complexity_tier(complexity),
                   self._s.routing.risk_floors[ctx.risk_class]]
        if min_tier:
            targets.append(min_tier)
        target = max(targets, key=lambda t: _ORDER[t])
        best_allowed_order = max(_ORDER[c.tier] for c in allowed)
        if _ORDER[target] > best_allowed_order:
            target = next(t for t, o in _ORDER.items() if o == best_allowed_order)
        avail = sorted({c.tier for c in allowed}, key=lambda t: _ORDER[t])
        tier_pick = next((t for t in avail if _ORDER[t] >= _ORDER[target]), avail[-1])

        # 3. keep only the target tier; 4. drop over-budget
        eligible: list[tuple] = []
        for c in allowed:
            if c.tier != tier_pick:
                reason = ("below target tier" if _ORDER[c.tier] < _ORDER[tier_pick]
                          else "above target tier")
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier, reason=reason))
                continue
            est = self._est_cost(c, tokens_after)
            if est > ctx.max_cost_usd:
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    reason="over budget"))
            else:
                eligible.append((c, est))
        if not eligible:
            raise ValueError("no candidates after budget filter")

        # 5. score within the tier (chooses provider/model — cheaper on-prem wins)
        w = self._s.routing.weights
        max_cost = max((e for _, e in eligible), default=0.0)

        def score(item):
            c, est = item
            norm = est / max_cost if max_cost > 0 else 0.0
            return (w["quality"] * _Q[c.tier] - w["cost"] * norm
                    - w["latency"] * _L[c.tier] + w["capacity"] * 1.0)

        eligible.sort(key=lambda it: (-score(it), it[1]))
        chosen, est = eligible[0]
        for c, _e in eligible[1:]:
            rejected.append(RejectedCandidate(
                name=c.name, provider=c.provider, tier=c.tier, reason="lower score"))
        return RoutingDecision(
            chosen=chosen, est_cost_usd=est,
            reason=f"tier {chosen.tier} (target {target})", rejected=rejected)
```

- [ ] **Step 2: Write the failing test `tests/test_router.py`**

```python
import pytest

from app.config.loader import load_settings
from app.models import ExecutionPolicyContext
from app.routing.router import Router

S = load_settings()
R = Router(S)


def _ctx(providers, risk="low", onprem=False):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def test_clean_low_complexity_picks_cheap():
    d = R.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert d.chosen.tier == "cheap"


def test_onprem_only_excludes_cloud_with_reason():
    d = R.select(_ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert any(r.provider in {"azure", "bedrock"}
               and "residency" in r.reason for r in d.rejected)


def test_high_risk_cloud_allowed_picks_frontier():
    d = R.select(_ctx({"onprem", "azure", "bedrock"}, risk="high"),
                 tokens_after=20, complexity=0.1)
    assert d.chosen.tier == "frontier"


def test_high_risk_onprem_only_stays_on_prem():
    d = R.select(_ctx({"onprem"}, risk="high", onprem=True),
                 tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert d.chosen.tier == "mid"  # best on-prem tier, not frontier
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_router.py -v`
Expected: PASS (4).

- [ ] **Step 4: Commit**

```bash
git add app/routing/router.py tests/test_router.py
git commit -m "feat: adaptive router with residency-beats-risk-floor selection"
```

---

### Task 12: Gateway (dispatch + fallback + confidence escalation)

**Files:**
- Create: `app/gateway/gateway.py`
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `Router` (Task 11), `MockBackend` (Task 9), `Settings`, `ExecutionPolicyContext`, `DecisionRecord`.
- Produces: `app.gateway.gateway.Gateway(settings, backend, router)` with async `run(ctx, messages, tokens_after, complexity, hint) -> tuple[GenerationResult, list[DecisionRecord]]`.
  - Loop: route → generate. If `confidence < confidence_escalation_threshold` and escalations remain, re-route with `min_tier` = next tier above the chosen, re-generate. Cap at `max_escalations`. Each attempt appends a `DecisionRecord` with incrementing `hop_index`.
  - On backend failure (`mock_fail` or exception): escalate to the next tier and re-route (router re-enforces `ctx.allowed_providers`); if already at `frontier`, re-raise — record reason.
  - `actual_cost_usd` set from the `GenerationResult`.

- [ ] **Step 1: Write `app/gateway/gateway.py`**

```python
from __future__ import annotations

from app.config.schema import Settings
from app.gateway.backends.base import InferenceBackend
from app.models import DecisionRecord, ExecutionPolicyContext, GenerationResult, Message
from app.routing.router import Router

_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}
_NEXT = {"cheap": "mid", "mid": "frontier", "frontier": None}


class Gateway:
    def __init__(self, settings: Settings, backend: InferenceBackend,
                 router: Router) -> None:
        self._s = settings
        self._backend = backend
        self._router = router

    async def run(self, ctx: ExecutionPolicyContext, messages: list[Message],
                  tokens_after: int, complexity: float, hint: dict
                  ) -> tuple[GenerationResult, list[DecisionRecord]]:
        records: list[DecisionRecord] = []
        threshold = self._s.routing.confidence_escalation_threshold
        max_esc = self._s.routing.max_escalations
        min_tier: str | None = None
        escalations = 0
        result: GenerationResult | None = None

        while True:
            decision = self._router.select(ctx, tokens_after, complexity, min_tier)
            cand = decision.chosen
            price = self._s.pricing.models[cand.name]
            try:
                result = await self._backend.generate(
                    cand.name, messages, price, cand.tier, hint)
            except Exception:
                records.append(DecisionRecord(
                    step="gateway", chosen=None,
                    reason=f"{cand.name} failed; escalating/fallback",
                    rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                    latency_ms=0.0, hop_index=len(records)))
                nxt = _NEXT[cand.tier]
                if nxt is None:
                    raise
                min_tier = nxt
                continue

            records.append(DecisionRecord(
                step="router", chosen=cand.name, reason=decision.reason,
                rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                actual_cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                hop_index=len(records)))

            if (result.confidence < threshold and escalations < max_esc
                    and _NEXT[cand.tier] is not None):
                escalations += 1
                min_tier = _NEXT[cand.tier]
                hint = {k: v for k, v in hint.items() if k != "mock_confidence"}
                continue
            break

        return result, records
```

- [ ] **Step 2: Write the failing test `tests/test_gateway.py`**

```python
import asyncio

from app.config.loader import load_settings
from app.gateway.backends.mock import MockBackend
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, Message
from app.routing.router import Router

S = load_settings()


def _ctx(providers, risk="low"):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _run(ctx, hint):
    gw = Gateway(S, MockBackend(S), Router(S))
    msgs = [Message(role="user", content="hello")]
    return asyncio.run(gw.run(ctx, msgs, 20, 0.1, hint))


def test_happy_path_one_hop():
    result, records = _run(_ctx({"onprem", "azure", "bedrock"}), {})
    assert result.confidence == 0.95
    assert sum(1 for r in records if r.step == "router") == 1


def test_low_confidence_escalates_exactly_once():
    result, records = _run(_ctx({"onprem", "azure", "bedrock"}),
                           {"mock_confidence": 0.3})
    router_hops = [r for r in records if r.step == "router"]
    assert len(router_hops) == 2  # initial + one escalation, then stop
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_gateway.py -v`
Expected: PASS (2).

- [ ] **Step 4: Commit**

```bash
git add app/gateway/gateway.py tests/test_gateway.py
git commit -m "feat: gateway with confidence escalation and fallback"
```

---

### Task 13: Output verification (leakage + require_verification)

**Files:**
- Create: `app/output/__init__.py`, `app/output/verify.py`
- Test: `tests/test_output_controls.py`

**Interfaces:**
- Consumes: `Detector` (Task 6), `ExecutionPolicyContext`, `VerificationRecord`.
- Produces: `app.output.verify.verify(detector, ctx, completion_text) -> tuple[str, VerificationRecord]`.
  - Re-scan `completion_text`; if entities found, redact them (egress redaction) → `redacted_text`.
  - If `ctx.require_verification`: mark `performed=True`. Escalation on verification failure happens in the gateway, not here. For the slice, "unverifiable" is simulated when `completion_text` contains the literal token `"UNVERIFIABLE"` → `passed=False` and the returned text is empty, and the caller (Task 14) returns a safe error; otherwise `passed=True` with the redacted text.
  - Non-required requests: `performed = (sampling)`; for determinism the slice sets `performed=True` only when required, else `False`.

- [ ] **Step 1: Write `app/output/verify.py`**

```python
from __future__ import annotations

from app.models import ExecutionPolicyContext, VerificationRecord
from app.policy.detectors import Detector


def verify(detector: Detector, ctx: ExecutionPolicyContext, completion_text: str
           ) -> tuple[str, VerificationRecord]:
    entities = detector.analyze(completion_text)
    etypes = tuple(sorted({e.type for e in entities}))
    redacted, _map = detector.anonymize(completion_text, entities)

    if not ctx.require_verification:
        return redacted, VerificationRecord(
            required=False, performed=False, passed=True,
            entities_found=etypes, reason="not required")

    # required path
    unverifiable = "UNVERIFIABLE" in completion_text
    passed = not unverifiable
    return (redacted if passed else ""), VerificationRecord(
        required=True, performed=True, passed=passed,
        entities_found=etypes,
        reason="verified" if passed else "failed closed: unverifiable")
```

- [ ] **Step 2: Create empty `app/output/__init__.py`**

- [ ] **Step 3: Write the failing test `tests/test_output_controls.py`**

```python
from app.models import ExecutionPolicyContext
from app.output.verify import verify
from app.policy.detectors import RegexDetector

D = RegexDetector()


def _ctx(require):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="high",
        require_verification=require, detected_entities=(), redaction_map_id=None)


def test_pii_in_completion_is_redacted():
    text, rec = verify(D, _ctx(False), "here is 123-45-6789")
    assert "123-45-6789" not in text
    assert "US_SSN" in rec.entities_found


def test_required_unverifiable_fails_closed():
    text, rec = verify(D, _ctx(True), "UNVERIFIABLE answer")
    assert rec.passed is False
    assert text == ""


def test_required_clean_passes():
    text, rec = verify(D, _ctx(True), "the answer is 4")
    assert rec.passed is True
    assert rec.performed is True
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_output_controls.py -v`
Expected: PASS (3).

- [ ] **Step 5: Commit**

```bash
git add app/output tests/test_output_controls.py
git commit -m "feat: output verification with egress redaction and fail-closed"
```

---

### Task 14: Assemble the deterministic pipeline + wire the route

**Files:**
- Create: `app/pipeline.py`
- Modify: `app/main.py` (build backend, router, gateway, pipeline at startup), `app/api/routes.py` (call the pipeline, return `ChatResponse` with trace + cost)
- Test: `tests/test_e2e_slice.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `app.pipeline.Pipeline(settings, gate, detector, gateway)` with async `run(request_id, tenant_id, request) -> dict` returning the full response payload:
  - `{"id","model","choices":[...],"usage":{...},"trace":[...], "cost":{"total_usd","baseline_usd","routing_saved_usd"}}`.
  - Steps in order: gate → task_mode → compress → gateway.run → verify. If verification fails closed, raise a `PipelineRejected` the route maps to HTTP 422.
  - `complexity = min(1.0, tokens_after / 2000)`.
  - `baseline_usd = tokens_after/1e6 * (p.input+p.output)` for `pricing.baseline_model`.

- [ ] **Step 1: Write `app/pipeline.py`**

```python
from __future__ import annotations

from app.config.schema import Settings
from app.gateway.gateway import Gateway
from app.models import (
    ChatRequest, Choice, ExecutionPolicyContext, Message, Usage,
)
from app.output.verify import verify
from app.policy.detectors import Detector
from app.policy.gate import Gate
from app.compression import compress
from app.routing.task_mode import decide as decide_mode


class PipelineRejected(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway) -> None:
        self._s = settings
        self._gate = gate
        self._detector = detector
        self._gateway = gateway

    def _baseline(self, tokens_after: int) -> float:
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return tokens_after / 1_000_000 * (p.input + p.output)

    async def run(self, request_id: str, tenant_id: str,
                  request: ChatRequest) -> dict:
        ctx, sanitized, _map = self._gate.evaluate(request_id, tenant_id, request)
        _mode = decide_mode(ctx, request)

        san_messages = [Message(role=request.messages[-1].role, content=sanitized)]
        comp = compress(san_messages, ctx)
        complexity = min(1.0, comp.tokens_after / 2000)

        hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                if hasattr(request, k)}
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint)

        text, vrec = verify(self._detector, ctx, result.text)
        if not vrec.passed:
            raise PipelineRejected(vrec.reason)

        total = sum(r.actual_cost_usd or 0.0 for r in records)
        baseline = self._baseline(comp.tokens_after)
        trace = [r.model_dump() for r in records] + [vrec.model_dump()]
        trace.append({"step": "compression", **comp.record})

        return {
            "id": request_id,
            "model": result.model,
            "choices": [Choice(index=0,
                               message=Message(role="assistant", content=text)
                               ).model_dump()],
            "usage": Usage(prompt_tokens=result.tokens_in,
                           completion_tokens=result.tokens_out,
                           total_tokens=result.tokens_in + result.tokens_out
                           ).model_dump(),
            "trace": trace,
            "cost": {"total_usd": total, "baseline_usd": baseline,
                     "routing_saved_usd": max(0.0, baseline - total)},
            "policy": {"sensitivity": ctx.sensitivity, "onprem_only": ctx.onprem_only,
                       "allowed_providers": sorted(ctx.allowed_providers),
                       "detected_entities": list(ctx.detected_entities)},
        }
```

- [ ] **Step 2: Build the pipeline in `app/main.py`**

Add imports and wiring inside `create_app` (after the gate is built):
```python
from app.gateway.backends.mock import MockBackend
from app.gateway.gateway import Gateway
from app.pipeline import Pipeline
from app.routing.router import Router
```
```python
    backend = MockBackend(settings)          # 'none'/vllm/dynamo handled in later phases
    router = Router(settings)
    gateway = Gateway(settings, backend, router)
    app.state.pipeline = Pipeline(settings, gate, detector, gateway)
```

- [ ] **Step 3: Replace the route body in `app/api/routes.py`**

```python
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.models import ChatRequest
from app.pipeline import PipelineRejected

router = APIRouter()


def _tenant_of(request: Request) -> str:
    key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return request.app.state.api_keys.get(key, "default")


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state.settings
    return {"status": "ok", "backend": s.serving.inference_backend,
            "models": [c.name for c in s.routing.model_pool]}


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest, request: Request) -> dict:
    s = request.app.state.settings
    if body.stream and s.serving.reject_stream_requests:
        raise HTTPException(400, "streaming is not supported in the demo")
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    tenant = _tenant_of(request)
    try:
        return await request.app.state.pipeline.run(request_id, tenant, body)
    except PipelineRejected as e:
        raise HTTPException(422, f"response failed verification: {e}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "policy evaluation unavailable")
```

- [ ] **Step 4: Write the failing test `tests/test_e2e_slice.py`**

```python
def test_simple_qa_uses_cheap_tier(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    body = r.json()
    assert r.status_code == 200
    router_hop = next(t for t in body["trace"] if t["step"] == "router")
    assert router_hop["chosen"] in {"local-8b"}
    assert body["cost"]["baseline_usd"] >= body["cost"]["total_usd"]


def test_pii_request_excludes_cloud_in_trace(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "verify SSN 123-45-6789"}]})
    body = r.json()
    assert body["policy"]["onprem_only"] is True
    router_hop = next(t for t in body["trace"] if t["step"] == "router")
    assert any(rej["provider"] in {"azure", "bedrock"}
               and "residency" in rej["reason"] for rej in router_hop["rejected"])
    assert "123-45-6789" not in r.text


def test_cost_accumulates_and_baseline_present(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}]})
    cost = r.json()["cost"]
    assert cost["total_usd"] == 0.0        # cheap on-prem tier is free
    assert cost["baseline_usd"] > 0.0      # frontier baseline is not
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS (all tests across all files).

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py app/main.py app/api/routes.py tests/test_e2e_slice.py
git commit -m "feat: assemble deterministic pipeline end-to-end with trace and cost"
```

---

### Task 15: Live smoke script + final verification

**Files:**
- Create: `scripts/smoke.sh`
- Test: manual live run (documented expected output)

**Interfaces:**
- Consumes: the running app.
- Produces: `scripts/smoke.sh` that boots uvicorn, runs four httpx checks, prints PASS/FAIL, tears down.

- [ ] **Step 1: Write `scripts/smoke.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
export SP_PII_DETECTION="${SP_PII_DETECTION:-regex}"
uv run uvicorn app.main:app --port 8080 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 30); do
  curl -sf http://localhost:8080/health >/dev/null && break || sleep 0.5
done

echo "== health =="
curl -sf http://localhost:8080/health

echo; echo "== clean request (expect cheap tier) =="
curl -sf http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"capital of France?"}]}' \
  | uv run python -c "import sys,json; d=json.load(sys.stdin); h=[t for t in d['trace'] if t['step']=='router'][0]; print('chosen:', h['chosen']); print('total/baseline:', d['cost']['total_usd'], d['cost']['baseline_usd'])"

echo; echo "== SSN request (expect onprem_only, cloud excluded, no raw SSN) =="
OUT=$(curl -sf http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"verify SSN 123-45-6789"}]}')
echo "$OUT" | uv run python -c "import sys,json; d=json.load(sys.stdin); print('onprem_only:', d['policy']['onprem_only']); assert '123-45-6789' not in json.dumps(d), 'RAW SSN LEAKED'; print('no raw SSN in response: OK')"

echo; echo "== stream:true (expect 400) =="
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"stream":true}')
test "$code" = "400" && echo "stream rejected: OK ($code)" || { echo "FAIL: $code"; exit 1; }

echo; echo "SMOKE: all checks passed"
```
Then `chmod +x scripts/smoke.sh`.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass, no failures.

- [ ] **Step 3: Run the config lint**

Run: `uv run python -m app.config.lint`
Expected: `CONFIG_LINT: ok`.

- [ ] **Step 4: Run the live smoke**

Run: `bash scripts/smoke.sh`
Expected output ends with `SMOKE: all checks passed`, showing `chosen: local-8b`, `onprem_only: True`, `no raw SSN in response: OK`, `stream rejected: OK (400)`.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke.sh
git commit -m "feat: live smoke script exercising the demo path end-to-end"
```

---

## Self-review notes (author)

- **Spec coverage:** §2 layout → Tasks 1,4; §3 flow → Tasks 8–14; §4 models → Task 5; §5 config lint → Task 3; §6 detectors → Task 6; §7 test plan → Tasks 3,6,7,8,9,11,12,13,14; §8 success criteria → Tasks 3,14,15; §10 tool stubs → Task 4 + stub modules in 9/10/13. All covered.
- **Deferred (correctly not in this slice):** real Presidio in CI (regex used), NAT agent loop, Headroom real compression, Langfuse export, vLLM/Dynamo, UI, traffic, packaging content.
- **Type consistency check:** `ExecutionPolicyContext`, `DecisionRecord`, `GenerationResult`, `RoutingDecision`, `CompressionResult`, `VerificationRecord` field/method names used in Tasks 11–14 match their definitions in Tasks 5, 9, 10, 11, 13.
- **Known slice simplification:** `verify()` uses the literal `"UNVERIFIABLE"` marker to simulate an unverifiable completion deterministically (documented in §9 open questions); the mock backend never emits it, so normal requests always pass.
```
