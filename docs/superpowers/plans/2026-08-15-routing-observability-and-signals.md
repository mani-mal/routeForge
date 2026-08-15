# Routing Observability + Smarter Routing Signals — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Prometheus `/metrics` endpoint, a flag-gated raise-only stage/role routing signal, and always-on candidate-snapshot + routing-policy-version trace export — shipped together in one DGX redeploy at `:0.11.0`.

**Architecture:** Three independent Serving-Plane observability/routing changes. Feature ① exposes counters/histograms via `prometheus_client` (opt-in `observability` extra). Feature ② feeds agent-loop signals into the *existing* `Router.select(min_tier=...)` raise-only join so the residency clamp still caps the result. Feature ③ serialises data already present on `DecisionRecord` (`chosen`+`rejected`) plus a content hash of the scoring config onto Langfuse spans, independent of `capture_io`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, `prometheus_client`, pytest (no pytest-asyncio — async is driven with `asyncio.run(...)` inside sync `def test_...`).

## Global Constraints

- **Provider name is `onprem` (one word); tiers are exactly `cheap | mid | frontier`; no `strong` tier.** Copy these verbatim.
- **Residency beats every floor.** A routing signal (Feature ②) may only *raise* the target tier; the residency clamp in `Router.select` (router.py:104-109) runs after the `max()` and still caps it. An `onprem_only` request can never be lifted to a cloud tier by a signal. (CLAUDE.md hard rules 3 & 8.)
- **Telemetry redacted on egress.** Feature ③ emits only model ids / tier / provider enums and fixed reason strings, run through `redact_value`; never prompt text, never raw PII.
- **No PII/tenant in Prometheus labels.** Labels are bounded-cardinality enums / model ids only — never `tenant`, `application_id`, or free text (per-tenant rollups live in RouteForge's own store, B15).
- **`prometheus_client` is opt-in** under the `observability` extra; `app/obs/metrics.py` must degrade to no-ops (and `/metrics` to a clear 503) when it is not installed — mirrors how `app/obs/otel.py`/`export.py` treat the OTel/Langfuse extra.
- **Feature ② is default-OFF** (`routing.stage_signals.enabled: false`); with the flag off, every existing routing test and the demo baseline numbers are unchanged.
- **No Improvement Plane, no inference deployment, no inbound streaming, no modality axis** — all unchanged.
- **Tests:** load config via `load_settings()`; seed onboarded on-prem models via `make_test_registry(S)` from `tests._litellm_fake`; drive async with `asyncio.run(...)`. HTTP tests use `TestClient(create_app())`.
- **Commits:** stage files by name (no `git add -A`). Branch: `feat/routing-obs-and-signals` (already created; spec already committed there).

---

## File map

| File | Change | Feature |
|---|---|---|
| `pyproject.toml` | add `prometheus-client` to `[project.optional-dependencies].observability` | ① |
| `app/obs/metrics.py` | **create** — metric registry, `observe_*` helpers, `render()` | ① |
| `app/api/routes.py` | add `GET /metrics`; call `observe_request`/`observe_tokens`; stamp policy version into `export_trace` | ①③ |
| `app/gateway/gateway.py` | time `router.select`; `observe_routing_decision` | ① |
| `app/agent/internal_endpoint.py` | time `router.select`; `observe_routing_decision`; wire signal `min_tier` + reason | ①② |
| `app/config/schema.py` | add `StageSignalsCfg`; `RoutingCfg.stage_signals`; `Settings.routing_policy_version` | ②③ |
| `config/routing.yaml` | add `stage_signals:` block (default off) | ② |
| `app/routing/signals.py` | **create** — `derive_signal_tier` pure function | ② |
| `app/config/loader.py` | compute `routing_policy_version` hash; pass to `Settings` | ③ |
| `app/obs/export.py` | emit `sp.candidates` per step; `sp.routing_policy_version` on root | ③ |
| `tests/test_metrics.py` | **create** | ① |
| `tests/test_routing_signals.py` | **create** | ② |
| `tests/test_signal_wiring.py` | **create** | ② |
| `tests/test_candidate_export.py` | **create** | ③ |
| `tests/test_routing_policy_version.py` | **create** | ③ |

**Note on Feature ① label set (deliberate refinement of the spec):** the spec listed `routeforge_requests_total{model,tier,provider,status}`. In this plan `requests_total` carries `{model,status}` only — a request maps to one completion point in `routes.py` (`resp`), whereas tier/provider are *per routing decision* and in multi-step there are many per request. Tier is therefore carried by `routing_decisions_total{tier,escalated}` where it belongs. This keeps instrumentation to the site that owns the data, avoids a per-request registry lookup, and preserves the spec's intent (bounded cardinality, no PII). Everything else matches the spec.

---

## Task 1: Prometheus metrics module + `/metrics` endpoint

**Files:**
- Modify: `pyproject.toml:36-40` (observability extra)
- Create: `app/obs/metrics.py`
- Modify: `app/api/routes.py` (add route + import)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces:
  - `app/obs/metrics.py`:
    - `observe_request(model: str, status: str) -> None`
    - `observe_tokens(model: str, tokens_in: int, tokens_out: int) -> None`
    - `observe_routing_decision(tier: str, escalated: bool) -> None`
    - `time_routing()` — context manager measuring wall-time into the overhead histogram
    - `render() -> tuple[bytes, str]` — `(exposition_bytes, content_type)`; when `prometheus_client` is absent returns `(b"", "")`
    - `AVAILABLE: bool` — True when `prometheus_client` imported

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, the `observability` extra (currently lines 36-40) becomes:

```toml
observability = [
  "opentelemetry-sdk>=1.27",
  "opentelemetry-exporter-otlp-proto-http>=1.27",
  "langfuse>=3.0",
  "prometheus-client>=0.20",
]
```

Then run: `uv sync --extra observability --extra presidio --extra agent`
Expected: resolves and installs `prometheus-client`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_metrics.py`:

```python
"""Feature 1: Prometheus /metrics. prometheus_client ships in the
`observability` extra (installed in CI/test); AVAILABLE is True here."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.obs import metrics


def test_metrics_module_available():
    assert metrics.AVAILABLE is True


def test_metrics_endpoint_renders_all_families():
    metrics.observe_request("local-8b", "ok")
    metrics.observe_tokens("local-8b", 10, 5)
    metrics.observe_routing_decision("cheap", escalated=False)
    with metrics.time_routing():
        pass
    client = TestClient(create_app())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    for family in ("routeforge_requests_total",
                   "routeforge_routing_decisions_total",
                   "routeforge_routing_overhead_seconds",
                   "routeforge_tokens_total"):
        assert family in body


def test_metrics_carry_no_pii_or_tenant_labels():
    metrics.observe_request("local-8b", "ok")
    body = TestClient(create_app()).get("/metrics").text
    # Label keys must be bounded-cardinality only. Tenant/PII must never appear.
    for forbidden in ('tenant', 'application_id', 'request_id', 'prompt'):
        assert f'{forbidden}=' not in body
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — `app.obs.metrics` does not exist / no `/metrics` route.

- [ ] **Step 4: Create the metrics module**

Create `app/obs/metrics.py`:

```python
"""Feature 1 — Prometheus exposition for routing observability.

`prometheus_client` ships in the opt-in `observability` extra (same extra as
the OTel/Langfuse exporter). When it is not installed, every observe_* helper
is a no-op and render() returns empty — the app still boots and serves, and
GET /metrics reports 503 (see app/api/routes.py). Labels are bounded-cardinality
enums / model ids ONLY: never tenant, request_id, or any free text (CLAUDE.md:
per-tenant rollups live in RouteForge's own store, not in Prometheus labels)."""
from __future__ import annotations

import contextlib
import time

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest,
    )
    AVAILABLE = True
except ImportError:  # observability extra not installed
    AVAILABLE = False

if AVAILABLE:
    _requests = Counter(
        "routeforge_requests_total", "Requests served, by model and status.",
        ["model", "status"])
    _decisions = Counter(
        "routeforge_routing_decisions_total",
        "Routing decisions, by chosen tier and whether it was an escalation.",
        ["tier", "escalated"])
    _overhead = Histogram(
        "routeforge_routing_overhead_seconds",
        "Wall-time spent in Router.select().")
    _tokens = Counter(
        "routeforge_tokens_total", "Tokens processed, by model and direction.",
        ["model", "direction"])


def observe_request(model: str, status: str) -> None:
    if AVAILABLE:
        _requests.labels(model=model or "-", status=status).inc()


def observe_tokens(model: str, tokens_in: int, tokens_out: int) -> None:
    if AVAILABLE:
        m = model or "-"
        _tokens.labels(model=m, direction="in").inc(max(0, tokens_in))
        _tokens.labels(model=m, direction="out").inc(max(0, tokens_out))


def observe_routing_decision(tier: str, escalated: bool) -> None:
    if AVAILABLE:
        _decisions.labels(tier=tier, escalated=str(bool(escalated)).lower()).inc()


@contextlib.contextmanager
def time_routing():
    if not AVAILABLE:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        _overhead.observe(time.perf_counter() - start)


def render() -> tuple[bytes, str]:
    if not AVAILABLE:
        return b"", ""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
```

- [ ] **Step 5: Add the `/metrics` route**

In `app/api/routes.py`, add the import near the other `app.obs` imports (line 13 area):

```python
from app.obs import metrics
```

Add the route (place it beside `health`, after line 69):

```python
@router.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape surface. Open like /health; auth is a production
    concern (same posture as the decision-stream SSE)."""
    if not metrics.AVAILABLE:
        raise HTTPException(503, "metrics unavailable: install the observability extra")
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
```

Ensure `Response` is imported from `fastapi` at the top of the file (add to the existing `fastapi` import if absent):

```python
from fastapi import Response
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS (all three tests).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml app/obs/metrics.py app/api/routes.py tests/test_metrics.py
git commit -m "feat(obs): Prometheus /metrics endpoint (opt-in observability extra)"
```

---

## Task 2: Instrument request + routing metrics

**Files:**
- Modify: `app/api/routes.py` (request/token counters at the request boundary)
- Modify: `app/gateway/gateway.py:37,65` (routing overhead + decision counter)
- Modify: `app/agent/internal_endpoint.py:152` (routing overhead + decision counter, per hop)
- Test: `tests/test_metrics.py` (extend)

**Interfaces:**
- Consumes: `observe_request`, `observe_tokens`, `observe_routing_decision`, `time_routing` from Task 1.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metrics.py`:

```python
def test_served_request_increments_counters(client, seed_all, api_headers):
    """A completed request bumps requests_total and routing_decisions_total.
    Uses the standard app fixtures (see conftest.py: client, seed_all,
    api_headers) that back the other end-to-end tests."""
    before = TestClient(create_app()).get("/metrics").text
    r = client.post("/v1/chat/completions", headers=api_headers,
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    after = TestClient(create_app()).get("/metrics").text
    # Registry is process-global, so counters are visible from a fresh app too.
    assert "routeforge_requests_total" in after
    assert 'status="ok"' in after
    assert "routeforge_routing_decisions_total" in after
```

> Before writing this test, open `tests/conftest.py` and confirm the exact fixture names used by the existing end-to-end tests (e.g. `test_e2e_slice.py`). Use those fixture names verbatim for `client` / seeding / auth headers rather than the placeholder names above if they differ.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py::test_served_request_increments_counters -v`
Expected: FAIL — `status="ok"` label not present / counter never incremented.

- [ ] **Step 3: Instrument the request boundary in `routes.py`**

In `chat_completions`, after the success path builds `resp` and before/after the export block (around line 162), add the request + token counters. Use a `status` derived from success vs. the exception handlers.

At the top of the `try` that wraps `pipeline.run` result handling is not ideal; instead add on success right after `resp["session_id"] = session_id` (line 162):

```python
metrics.observe_request(resp["model"], "ok")
usage = resp.get("usage", {})
metrics.observe_tokens(resp["model"],
                       usage.get("prompt_tokens", 0),
                       usage.get("completion_tokens", 0))
```

And in the exception handlers (lines 139-156), record a rejected/error request. Add one line at the start of each `except` body that returns an error, e.g. for `PipelineRejected`, `NoEligibleCandidate`, `AgentCapExceeded`, `BackendUnavailable`, `PolicyGateError`:

```python
metrics.observe_request("-", "rejected")
```

and for the catch-all `except Exception:` (line 155):

```python
metrics.observe_request("-", "error")
```

(Place the `observe_request` call before the `raise HTTPException(...)` in each handler. `HTTPException` re-raise at line 153-154 needs none — it is a pass-through of an already-counted rejection.)

- [ ] **Step 4: Instrument the router call in `gateway.py`**

In `Gateway.run`, wrap the `select` call (line 37) and count the decision after the served `router` record is appended (after line 72). Add the import at the top:

```python
from app.obs import metrics
```

Replace line 37:

```python
            with metrics.time_routing():
                decision = self._router.select(ctx, tokens_after, complexity, min_tier)
```

After appending the `step="router"` record (after line 72), add:

```python
            metrics.observe_routing_decision(cand.tier, escalated=escalations > 0)
```

- [ ] **Step 5: Instrument the per-hop router call in `internal_endpoint.py`**

Add the import near the other `app.*` imports:

```python
from app.obs import metrics
```

Replace line 152:

```python
    with metrics.time_routing():
        decision = model_router.select(ctx, tokens_after, complexity, None)
```

After the `HOPS.record(...)` block (after line 179), add:

```python
    metrics.observe_routing_decision(decision.chosen.tier, escalated=False)
```

(Multi-step hops don't escalate — escalation is a direct-path Gateway feature, per the module docstring.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: PASS (metrics are additive; nothing else changes).

- [ ] **Step 8: Commit**

```bash
git add app/api/routes.py app/gateway/gateway.py app/agent/internal_endpoint.py tests/test_metrics.py
git commit -m "feat(obs): instrument request, token, and routing-decision metrics"
```

---

## Task 3: `stage_signals` config block + schema

**Files:**
- Modify: `app/config/schema.py:23-46` (`RoutingCfg`) — add `StageSignalsCfg` + field
- Modify: `config/routing.yaml` — add `stage_signals:` block
- Test: `tests/test_routing_signals.py` (config portion)

**Interfaces:**
- Produces: `app.config.schema.StageSignalsCfg` with fields
  `enabled: bool`, `tool_error_raise: bool`, `exploration_repeat: int`,
  `planner_role_raise: bool`; `RoutingCfg.stage_signals: StageSignalsCfg`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_routing_signals.py`:

```python
from app.config.loader import load_settings

S = load_settings()


def test_stage_signals_defaults_off():
    cfg = S.routing.stage_signals
    assert cfg.enabled is False
    assert cfg.exploration_repeat >= 1
    assert cfg.tool_error_raise is True
    assert cfg.planner_role_raise is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_routing_signals.py::test_stage_signals_defaults_off -v`
Expected: FAIL — `RoutingCfg` has no `stage_signals`.

- [ ] **Step 3: Add the schema**

In `app/config/schema.py`, add above `RoutingCfg` (after line 21):

```python
class StageSignalsCfg(BaseModel):
    # Feature 2: agent-loop-derived, RAISE-ONLY tier bias. Default OFF so the
    # baseline/demo numbers are unchanged; an operator toggles it on to show
    # the lever. The signal only ever joins the max() in Router.select as a
    # min_tier -- the residency clamp still caps it, so it can never lift an
    # onprem_only request to cloud (CLAUDE.md rules 3 & 8).
    enabled: bool = False
    tool_error_raise: bool = True      # a tool error in the latest result -> raise one tier
    exploration_repeat: int = 3        # same tool called >= N times -> raise one tier
    planner_role_raise: bool = True    # inferred "plan" hop (hop 1, no tools yet) -> frontier
```

Add the field to `RoutingCfg` (after `fallback`, line 46) with a default so configs
without the block still load:

```python
    stage_signals: StageSignalsCfg = StageSignalsCfg()
```

- [ ] **Step 4: Add the YAML block**

Append to `config/routing.yaml` (top-level key):

```yaml
# Feature 2 — stage/role-aware routing signals (raise-only, default OFF).
# When enabled, the multi-step path derives a min_tier bias from agent-loop
# state (tool errors, exploration, inferred hop role) and joins it into the
# router's max(). The residency clamp still caps it: an onprem_only request
# never reaches cloud. Toggle on to demonstrate the lever.
stage_signals:
  enabled: false
  tool_error_raise: true
  exploration_repeat: 3
  planner_role_raise: true
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_routing_signals.py -v`
Expected: PASS.

- [ ] **Step 6: Confirm the config lint still passes**

Run: `uv run pytest tests/test_config_lint.py tests/test_config_loader.py -v`
Expected: PASS — the new block is a routing-owned concern, no cross-file duplication.

- [ ] **Step 7: Commit**

```bash
git add app/config/schema.py config/routing.yaml tests/test_routing_signals.py
git commit -m "feat(routing): stage_signals config block (default off)"
```

---

## Task 4: `derive_signal_tier` pure function

**Files:**
- Create: `app/routing/signals.py`
- Test: `tests/test_routing_signals.py` (extend)

**Interfaces:**
- Consumes: `StageSignalsCfg` (Task 3).
- Produces:
  ```python
  @dataclass(frozen=True)
  class SignalResult:
      min_tier: str | None   # 'mid' | 'frontier' | None
      reason: str | None     # human-facing explanation, or None

  def derive_signal_tier(messages: list[dict], hop_index: int,
                         cfg: StageSignalsCfg) -> SignalResult
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing_signals.py`:

```python
from app.config.schema import StageSignalsCfg
from app.routing.signals import SignalResult, derive_signal_tier

ON = StageSignalsCfg(enabled=True)


def _tool_msg(name, content):
    return {"role": "tool", "name": name, "content": content}


def test_plan_role_raises_on_first_hop_with_no_tools():
    msgs = [{"role": "user", "content": "plan a multi-step task"}]
    res = derive_signal_tier(msgs, hop_index=1, cfg=ON)
    assert res.min_tier == "frontier"
    assert "plan" in res.reason.lower()


def test_tool_error_raises_one_tier():
    msgs = [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "Error: connection refused (traceback ...)")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier == "mid"
    assert "error" in res.reason.lower()


def test_exploration_repeat_raises():
    msgs = [{"role": "user", "content": "go"}]
    for _ in range(3):
        msgs.append({"role": "assistant", "content": ""})
        msgs.append(_tool_msg("search", "no result"))
    res = derive_signal_tier(msgs, hop_index=4, cfg=ON)
    assert res.min_tier == "mid"
    assert "exploration" in res.reason.lower()


def test_steady_execute_hop_does_not_raise():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            _tool_msg("write_file", "ok, wrote 3 lines")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier is None
    assert res.reason is None


def test_disabled_cfg_never_raises():
    msgs = [{"role": "user", "content": "plan it"}]
    res = derive_signal_tier(msgs, hop_index=1, cfg=StageSignalsCfg(enabled=False))
    assert res == SignalResult(min_tier=None, reason=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routing_signals.py -v`
Expected: FAIL — `app.routing.signals` does not exist.

- [ ] **Step 3: Implement the function**

Create `app/routing/signals.py`:

```python
"""Feature 2 — derive a RAISE-ONLY tier bias from agent-loop state.

Pure function of a hop's observable message history. Returns a suggested
min_tier ('mid' | 'frontier') that the caller feeds into Router.select as the
min_tier arg, where it only ever JOINS the max() -- the residency clamp still
caps it, so an onprem_only request can never be lifted to cloud (CLAUDE.md
rules 3 & 8). Role is a heuristic and the reason string says so ('inferred').
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.schema import StageSignalsCfg

_ERROR_MARKERS = ("error", "exception", "traceback", "failed", "failure")


@dataclass(frozen=True)
class SignalResult:
    min_tier: str | None
    reason: str | None


def _looks_like_error(content: str) -> bool:
    low = content.lower()
    return any(m in low for m in _ERROR_MARKERS)


def derive_signal_tier(messages: list[dict], hop_index: int,
                       cfg: StageSignalsCfg) -> SignalResult:
    if not cfg.enabled:
        return SignalResult(None, None)

    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    # Role signal (heuristic): a first hop with no tool activity yet is a
    # planning turn -> bias to frontier.
    if cfg.planner_role_raise and hop_index <= 1 and not tool_msgs:
        return SignalResult("frontier",
                            f"inferred role: plan (hop {hop_index}) -> frontier")

    # Stage signal (grounded): the latest tool result reports an error.
    if cfg.tool_error_raise and tool_msgs and _looks_like_error(
            str(tool_msgs[-1].get("content", ""))):
        return SignalResult("mid", "stage: tool-error -> raised to mid")

    # Stage signal (grounded): the same tool has been called >= N times ->
    # the agent is stuck exploring.
    if cfg.exploration_repeat and tool_msgs:
        names = [m.get("name") for m in tool_msgs if m.get("name")]
        for n in set(names):
            if names.count(n) >= cfg.exploration_repeat:
                return SignalResult(
                    "mid", f"stage: exploration ({n} x{names.count(n)}) -> raised to mid")

    return SignalResult(None, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_routing_signals.py -v`
Expected: PASS (all five new tests).

- [ ] **Step 5: Commit**

```bash
git add app/routing/signals.py tests/test_routing_signals.py
git commit -m "feat(routing): derive_signal_tier raise-only stage/role signal"
```

---

## Task 5: Wire the signal into the multi-step hop path

**Files:**
- Modify: `app/agent/internal_endpoint.py:151-179`
- Test: `tests/test_signal_wiring.py`

**Interfaces:**
- Consumes: `derive_signal_tier`, `SignalResult` (Task 4); `StageSignalsCfg` from
  `settings.routing.stage_signals`.
- The hop's `select` call passes `sig.min_tier` (or `None` when the flag is off);
  the recorded `DecisionRecord.reason` is augmented with `sig.reason` when it raised.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_signal_wiring.py`:

```python
"""Feature 2 wiring: the signal joins Router.select's max() as a raise-only
min_tier, and the residency clamp still caps it. These exercise the router
contract directly (the same seam the hop uses) plus a residency invariant."""
from app.config.loader import load_settings
from app.config.schema import StageSignalsCfg
from app.models import ExecutionPolicyContext
from app.routing.router import Router
from app.routing.signals import derive_signal_tier
from tests._litellm_fake import make_test_registry

S = load_settings()


def _onprem_ctx():
    # onprem_only, on-prem-only provider set (mirrors test_derived_onprem_reject).
    return ExecutionPolicyContext(
        request_id="r", tenant_id="hospital", sensitivity="restricted",
        onprem_only=True, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=True, detected_entities=(),
        redaction_map_id=None)


def test_signal_raises_tier_when_enabled():
    reg = make_test_registry(S)  # seeds on-prem local-8b (cheap) + local-32b
    ctx = _onprem_ctx()
    # A short prompt -> complexity ~0 -> would be cheap. A frontier signal
    # must raise the target above cheap (clamped to best on-prem tier).
    base = Router(S, registry=reg).select(ctx, tokens_after=10, complexity=0.01,
                                          min_tier=None)
    raised = Router(S, registry=reg).select(ctx, tokens_after=10, complexity=0.01,
                                            min_tier="frontier")
    order = {"cheap": 0, "mid": 1, "frontier": 2}
    assert order[raised.chosen.tier] >= order[base.chosen.tier]


def test_signal_never_lifts_onprem_only_to_cloud():
    reg = make_test_registry(S)
    ctx = _onprem_ctx()
    d = Router(S, registry=reg).select(ctx, tokens_after=10, complexity=0.01,
                                       min_tier="frontier")
    # Residency clamp wins: the chosen candidate stays on-prem.
    assert d.chosen.provider == "onprem"


def test_signal_is_raise_only_never_lowers():
    reg = make_test_registry(S)
    ctx = _onprem_ctx()
    # complexity forces mid; a 'cheap' signal must NOT lower it.
    order = {"cheap": 0, "mid": 1, "frontier": 2}
    d = Router(S, registry=reg).select(ctx, tokens_after=800, complexity=0.5,
                                       min_tier="cheap")
    assert order[d.chosen.tier] >= 1  # still >= mid from complexity


def test_flag_off_yields_no_signal():
    from app.routing.signals import SignalResult
    res = derive_signal_tier([{"role": "user", "content": "plan"}],
                             hop_index=1, cfg=StageSignalsCfg(enabled=False))
    assert res == SignalResult(None, None)
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `uv run pytest tests/test_signal_wiring.py -v`
Expected: the four router-contract tests actually PASS already (the router's
`min_tier` join + residency clamp exist). They are regression guards for the
invariant the wiring depends on. If any fails, STOP — the invariant is broken and
the wiring must not proceed.

- [ ] **Step 3: Wire the signal into the hop**

In `app/agent/internal_endpoint.py`, add the import:

```python
from app.routing.signals import derive_signal_tier
```

Replace the `select` block (currently lines 151-152, already wrapped in
`time_routing()` from Task 2) so it derives and passes the signal when enabled:

```python
    complexity = min(1.0, tokens_after / 2000)
    stage_cfg = settings.routing.stage_signals
    sig = (derive_signal_tier(messages, HOPS.next_hop_index(run_id) if False else 0, stage_cfg)
           if stage_cfg.enabled else None)
```

> DO NOT compute the hop index twice — `next_hop_index` mutates state. Instead
> reorder: compute `hop_index` first, then derive the signal with it, then select.
> Use this exact ordering:

```python
    complexity = min(1.0, tokens_after / 2000)
    hop_index = HOPS.next_hop_index(run_id)
    stage_cfg = settings.routing.stage_signals
    sig = derive_signal_tier(messages, hop_index, stage_cfg) if stage_cfg.enabled else None
    signal_tier = sig.min_tier if sig else None
    with metrics.time_routing():
        decision = model_router.select(ctx, tokens_after, complexity, signal_tier)
```

Note: `settings` is currently read at line 156 (`settings = request.app.state.settings`).
Move that line up to before this block so `settings.routing.stage_signals` is available.
Also DELETE the old `hop_index = HOPS.next_hop_index(run_id)` at line 154 (it is now
computed above — do not call it twice).

- [ ] **Step 4: Augment the recorded reason**

In the `HOPS.record(...)` `DecisionRecord(...)` (lines 171-179), change the `reason`
argument to fold in the signal explanation when present:

```python
        reason=(f"{decision.reason} · {sig.reason}" if sig and sig.reason
                else decision.reason),
```

- [ ] **Step 5: Add a wiring test through the internal endpoint**

Append to `tests/test_signal_wiring.py` a test that drives the internal endpoint with
the flag on and asserts the reason string surfaces. Use the existing internal-endpoint
fixture from `conftest.py` (see `test_agent_internal_endpoint.py` for the exact fixture
name and how it seeds a run via `HOPS.start`). Model the assertion on that test:

```python
def test_hop_reason_shows_signal_when_enabled(monkeypatch):
    # See tests/test_agent_internal_endpoint.py for the internal_client / run
    # fixture and the SSE-drive helper. Enable the flag on app.state.settings,
    # POST a first-hop request with no tool messages, drain HOPS, and assert the
    # recorded DecisionRecord.reason contains 'inferred role: plan'.
    ...
```

> Fill this in by copying the setup from `test_agent_internal_endpoint.py` verbatim,
> flipping `app.state.settings.routing.stage_signals` to an enabled `StageSignalsCfg`
> (via `model_copy(update=...)` on the routing cfg), then asserting on the popped
> record's `.reason`. If the internal-endpoint fixture's mechanics make a focused unit
> test cheaper, assert on `derive_signal_tier` + the reason-fold expression directly
> instead — the router-contract tests above already cover the raise-only + residency
> invariants, which are the load-bearing guarantees.

- [ ] **Step 6: Run tests + full suite**

Run: `uv run pytest tests/test_signal_wiring.py tests/test_agent_internal_endpoint.py -v`
Then: `uv run pytest -q`
Expected: PASS. With the flag default-off, all pre-existing multi-step tests are
unchanged.

- [ ] **Step 7: Commit**

```bash
git add app/agent/internal_endpoint.py tests/test_signal_wiring.py
git commit -m "feat(routing): wire raise-only stage/role signal into multi-step hops"
```

---

## Task 6: `sp.candidates` always-on export

**Files:**
- Modify: `app/obs/export.py:82-95` (per-step loop)
- Test: `tests/test_candidate_export.py`

**Interfaces:**
- Consumes: the `trace` list of `DecisionRecord.model_dump()` dicts already passed to
  `export_trace` — each carries `chosen: str | None` and
  `rejected: list[{name,provider,tier,display_name,reason}]`.
- Produces: an `sp.candidates` span attribute — a JSON string
  `[{model,tier,provider,eligible,reason}, ...]` — emitted regardless of `capture_io`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_candidate_export.py`:

```python
"""Feature 3 F2: sp.candidates is always-on metadata (independent of
capture_io). We capture span attributes with an in-memory OTel exporter and
assert the serialized candidate set. Mirrors the span-capture pattern in
tests/test_capture_io.py / test_agent_telemetry_redaction.py -- reuse that
fixture rather than re-inventing it."""
import json

from app.obs.export import _candidates_json


def test_candidates_json_shape():
    rec = {
        "step": "router",
        "chosen": "local-8b",
        "reason": "tier cheap (target cheap)",
        "rejected": [
            {"name": "bedrock-frontier", "provider": "bedrock", "tier": "frontier",
             "display_name": "Claude (bedrock)", "reason": "excluded · residency"},
        ],
    }
    out = json.loads(_candidates_json(rec))
    assert {"model": "local-8b", "eligible": True} <= _find(out, "local-8b")
    rej = _find(out, "bedrock-frontier")
    assert rej["eligible"] is False
    assert rej["reason"] == "excluded · residency"
    assert rej["tier"] == "frontier"


def test_candidates_empty_when_no_decision():
    assert _candidates_json({"step": "verify"}) is None


def _find(items, model):
    return next(i for i in items if i["model"] == model)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_candidate_export.py -v`
Expected: FAIL — `_candidates_json` does not exist.

- [ ] **Step 3: Implement the serializer + emit it**

In `app/obs/export.py`, add a module-level helper (after the imports, before
`_set_io`):

```python
import json


def _candidates_json(rec: dict) -> str | None:
    """Serialize a routing DecisionRecord's chosen + rejected set into an
    always-on `sp.candidates` attribute. Returns None for records that carry no
    routing decision (e.g. the verify step). Values are model ids / enums /
    fixed reason strings -- no PII -- so this is emitted regardless of
    capture_io (rlhf-readiness F2). Reason strings are run through the same
    egress redactor defensively at emit time."""
    chosen = rec.get("chosen")
    rejected = rec.get("rejected") or []
    if not chosen and not rejected:
        return None
    items = []
    if chosen:
        items.append({"model": chosen, "tier": rec.get("tier"),
                      "provider": rec.get("provider"), "eligible": True,
                      "reason": rec.get("reason", "")})
    for r in rejected:
        items.append({"model": r.get("name"), "tier": r.get("tier"),
                      "provider": r.get("provider"), "eligible": False,
                      "reason": r.get("reason", "")})
    return json.dumps(items)
```

> Note: `DecisionRecord` itself has no top-level `tier`/`provider` for the *chosen*
> model (only `chosen` as a name). The chosen candidate's tier/provider are not on the
> record. Emit `tier`/`provider` as `None` for the chosen entry (the model id is the
> stable key; a later join against the pool resolves tier/provider). Rejected entries
> DO carry tier/provider (from `RejectedCandidate`). This is honest and sufficient for
> the RLHF substrate — the chosen model id + the full rejected set is the decision.

In the per-step loop (inside `for i, rec in enumerate(trace):`, after the `reason`
attribute is set around line 92), add:

```python
                    cands = _candidates_json(rec)
                    if cands is not None:
                        sp.set_attribute("sp.candidates", redact_value(cands))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_candidate_export.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/obs/export.py tests/test_candidate_export.py
git commit -m "feat(obs): always-on sp.candidates snapshot on decision spans"
```

---

## Task 7: `routing_policy_version` content hash

**Files:**
- Modify: `app/config/loader.py:25-82` (compute hash, pass to `Settings`)
- Modify: `app/config/schema.py:151-156` (`Settings.routing_policy_version`)
- Modify: `app/obs/export.py:41-75` (accept + stamp on root span)
- Modify: `app/api/routes.py:176-186` (thread `s.routing_policy_version`)
- Test: `tests/test_routing_policy_version.py`

**Interfaces:**
- Produces: `Settings.routing_policy_version: str` — first 12 hex of
  `sha256(canonical_json(routing_raw + pricing_raw))`.
- `export_trace(..., routing_policy_version: str = "")` — stamps
  `sp.routing_policy_version` on the root span when non-empty.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routing_policy_version.py`:

```python
import copy

from app.config.loader import _policy_version, load_settings

S = load_settings()


def test_settings_carries_a_policy_version():
    assert isinstance(S.routing_policy_version, str)
    assert len(S.routing_policy_version) == 12


def test_version_stable_across_loads():
    assert load_settings().routing_policy_version == \
        load_settings().routing_policy_version


def test_version_changes_when_routing_changes():
    routing = {"weights": {"cost": 0.6}}
    pricing = {"baseline_model": "x"}
    v1 = _policy_version(routing, pricing)
    routing2 = copy.deepcopy(routing)
    routing2["weights"]["cost"] = 0.7
    v2 = _policy_version(routing2, pricing)
    assert v1 != v2


def test_version_ignores_serving_and_policy_files():
    # Only routing + pricing feed the hash. Same routing/pricing -> same hash,
    # regardless of what serving.yaml/policy.yaml contain.
    routing = {"a": 1}
    pricing = {"b": 2}
    assert _policy_version(routing, pricing) == _policy_version(dict(routing), dict(pricing))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routing_policy_version.py -v`
Expected: FAIL — `_policy_version` / `Settings.routing_policy_version` missing.

- [ ] **Step 3: Add the schema field**

In `app/config/schema.py`, add to `Settings` (after `pricing`, line 155):

```python
    # Feature 3 F3: short content hash of routing.yaml + pricing.yaml (the
    # scoring weights, thresholds, tiers, pool, and prices that determine a
    # routing decision). Stamped on every trace as sp.routing_policy_version so
    # decisions can later be grouped by the exact policy that produced them
    # (rlhf-readiness F3). Automatic -- no manual semver.
    routing_policy_version: str = ""
```

- [ ] **Step 4: Compute the hash in the loader**

In `app/config/loader.py`, add near the top (after imports):

```python
import hashlib
import json
```

Add the helper (module level):

```python
def _policy_version(routing_raw: dict, pricing_raw: dict) -> str:
    """First 12 hex of sha256 over the canonicalized routing + pricing config.
    Sorted-key JSON makes it stable across boots for identical config and
    change only when a scoring/pool/price value changes."""
    blob = json.dumps({"routing": routing_raw, "pricing": pricing_raw},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
```

In `load_settings`, change the final return (line 82) to pass the hash:

```python
    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing,
                    routing_policy_version=_policy_version(routing_raw, pricing_raw))
```

- [ ] **Step 5: Stamp it on the root span**

In `app/obs/export.py`, add the parameter to `export_trace` (in the signature, line
41-46):

```python
                 session_id: str | None = None,
                 routing_policy_version: str = "") -> str | None:
```

Inside the root span block, after the other `sp.*` attributes (after line 75), add:

```python
            if routing_policy_version:
                root.set_attribute("sp.routing_policy_version", routing_policy_version)
```

- [ ] **Step 6: Thread it from the route**

In `app/api/routes.py`, in the `export_trace(...)` call (lines 176-186), add the
argument:

```python
            session_id=session_id,
            routing_policy_version=s.routing_policy_version)
```

(`s` is already `request.app.state.settings`, line 74.)

- [ ] **Step 7: Run tests + full suite**

Run: `uv run pytest tests/test_routing_policy_version.py tests/test_candidate_export.py -v`
Then: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/config/loader.py app/config/schema.py app/obs/export.py app/api/routes.py tests/test_routing_policy_version.py
git commit -m "feat(obs): stamp routing_policy_version content hash on every trace"
```

---

## Task 8: Full suite + deploy readiness gate

**Files:** none (verification only)

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest -q`
Expected: PASS — all new tests green, zero regressions. With `stage_signals.enabled:
false` (default), the baseline/demo behaviour is byte-for-byte unchanged.

- [ ] **Step 2: Boot check (config lint passes with the new blocks)**

Run: `uv run python -c "from app.main import create_app; create_app(); print('boot ok')"`
Expected: `boot ok` (cross-file config lint passes with `stage_signals` +
`routing_policy_version`).

- [ ] **Step 3: Local `/metrics` smoke**

Run the app locally and `curl localhost:8080/metrics` (or drive via TestClient) after
one request; confirm all four metric families render and no `tenant=`/`request_id=`
labels appear.

- [ ] **Step 4: Signal-lever manual check (flag on)**

Temporarily set `stage_signals.enabled: true` in `config/routing.yaml`, run a
multi-step request (a prompt that triggers `decide_mode == "multi_step"`, e.g. contains
"step by step"), and confirm a first/tool-error hop's `DecisionRecord.reason` shows the
raise. Revert the flag to `false` before deploy (the operator toggles it live on DGX).

**Deploy handoff (separate, after this plan lands):** the `:0.11.0` DGX redeploy
(recreate ns + secrets + Langfuse/Postgres + RouteForge's own vLLM `vllm-local-8b.yaml`,
build+push api/ui images with a classic PAT + isolated DOCKER_CONFIG, onboard
`local-8b`, smoke-test `/metrics` + the signal lever + `sp.candidates` +
`sp.routing_policy_version`) is described in the spec's Deployment section. Follow the
`dgx-runai-ops` skill for the cluster mechanics.

---

## Self-review

**Spec coverage:**
- ① `/metrics` + 4 metrics + no-PII labels → Tasks 1-2. (Label-set refinement for
  `requests_total` documented in the File map note.)
- ② `stage_signals` config, `derive_signal_tier`, raise-only wiring, residency clamp,
  reason honesty, flag-off baseline → Tasks 3-5.
- ③ `sp.candidates` always-on, `routing_policy_version` hash → Tasks 6-7.
- Deploy readiness → Task 8.

**Placeholder scan:** Task 5 Step 5 and its through-endpoint test are intentionally
described-not-coded because they depend on the exact internal-endpoint fixture name in
`conftest.py`, which the implementer must read; the load-bearing invariants (raise-only,
residency clamp) are fully coded in Steps 1-4. Every other step contains runnable code.

**Type consistency:** `SignalResult(min_tier, reason)` is produced in Task 4 and
consumed in Task 5. `_candidates_json(rec) -> str | None` (Task 6) and
`_policy_version(routing_raw, pricing_raw) -> str` (Task 7) are used exactly as defined.
`observe_request/observe_tokens/observe_routing_decision/time_routing/render` (Task 1)
are consumed with matching signatures in Task 2. `Settings.routing_policy_version` (Task
7 schema) is set in the loader and read in the route.
