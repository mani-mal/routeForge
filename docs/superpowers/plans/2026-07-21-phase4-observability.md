# Phase 4 — Observability (NAT-compatible OTel → Langfuse) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every request's decision trace is exported to a self-hosted Langfuse via OpenTelemetry, with mandatory egress PII redaction, verifiable locally.

**Architecture:** The deterministic pipeline already builds a `trace: list[dict]` (DecisionRecords + VerificationRecord + compression record) and a `policy`/`cost` summary. Phase 4 adds an OTel export path: one root span per request with a child span per trace record, sent OTLP/HTTP to Langfuse's `/api/public/otel` endpoint. Redaction is applied **before any attribute is set** (the authored-span guarantee) **and** by a `RedactingSpanExporter` wrapper (defence-in-depth, and the mechanism NAT's Phase-5 spans will reuse). Telemetry is best-effort: an export failure is logged and swallowed — it must never fail a request (fail-closed governs the policy gate, not the exporter).

**Tech Stack:** OpenTelemetry SDK + `opentelemetry-exporter-otlp-proto-http`, self-hosted Langfuse (Postgres + ClickHouse + Redis + MinIO) via compose. New deps live behind an opt-in `[observability]` extra so the core still runs without them (same pattern as `presidio`).

## Global Constraints

- **Redaction on egress is mandatory.** `serving.observability.langfuse.redact_on_export` must be `true`; the config lint fails the boot if Langfuse is enabled and it is not `true`. (CLAUDE.md rule 2.)
- **Never export raw PII, redaction-map values, or CCR contents.** Trace records are already types-only (`DecisionRecord`, `VerificationRecord.entities_found`); the exporter must additionally redact any free-text field (`reason`) and must never read `Entity.value` or the redaction map. (CLAUDE.md rules 2, 6.)
- **Telemetry never blocks or fails a request.** All export calls are wrapped so any exception is logged and swallowed. A telemetry outage degrades observability, never availability.
- **Opt-in extra.** `opentelemetry-*` and `langfuse` are in a `[observability]` extra. `import app.obs.*` must succeed and no-op cleanly when the extra is absent or `langfuse.enabled` is false — the offline pytest suite runs without the extra installed except where a test explicitly needs it.
- **Langfuse OTLP surface (verified 2026-07-21):** endpoint `{host}/api/public/otel/v1/traces`; headers `Authorization: Basic base64("{public_key}:{secret_key}")` and `x-langfuse-ingestion-version: 4`. Local host default `http://localhost:3000`.
- **No secrets in code or compose.** Keys come from env (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`); `.env.example` is committed, `.env` is gitignored.
- **One owner per concern.** Observability config lives only in `serving.yaml`. Do not restate values elsewhere.

---

### Task 1: Observability config surface + lint rule

**Files:**
- Modify: `app/config/schema.py` (add `LangfuseCfg`, `ObservabilityCfg`; add `observability` to `ServingCfg`)
- Modify: `app/config/loader.py` (map `serving.yaml observability`, env-expand host, read keys from env)
- Modify: `app/config/lint.py` (enforce `redact_on_export` true when enabled)
- Test: `tests/test_config_obs.py`

**Interfaces:**
- Consumes: existing `ServingCfg`, `load_settings(config_dir)`, `enforce(settings)`.
- Produces: `settings.serving.observability.langfuse` with fields `enabled: bool`, `host: str`, `redact_on_export: bool`, `public_key: str | None`, `secret_key: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_obs.py
import os
import pytest
from app.config.loader import load_settings
from app.config.lint import enforce, ConfigLintError

def test_observability_loaded_with_defaults(monkeypatch):
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    s = load_settings()
    lf = s.serving.observability.langfuse
    assert lf.enabled is True
    assert lf.redact_on_export is True
    assert lf.public_key == "pk-test"
    assert lf.secret_key == "sk-test"
    # host has a ${LANGFUSE_HOST:-...} default in yaml; unset env -> the default
    assert lf.host == "http://langfuse:3000"

def test_host_env_expansion(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    s = load_settings()
    assert s.serving.observability.langfuse.host == "http://localhost:3000"

def test_lint_rejects_redact_off(tmp_path, monkeypatch):
    # redact_on_export must be true when langfuse is enabled
    s = load_settings()
    object.__setattr__(s.serving.observability.langfuse, "redact_on_export", False)
    with pytest.raises(ConfigLintError, match="redact_on_export"):
        enforce(s)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_obs.py -v`
Expected: FAIL — `ServingCfg` has no `observability` attribute.

- [ ] **Step 3: Implement**

Add to `app/config/schema.py`:

```python
class LangfuseCfg(BaseModel):
    enabled: bool
    host: str
    redact_on_export: bool
    public_key: str | None = None
    secret_key: str | None = None


class ObservabilityCfg(BaseModel):
    langfuse: LangfuseCfg
```

Add the field to `ServingCfg` (after `context_optimization_enabled`):

```python
    observability: ObservabilityCfg
```

In `app/config/loader.py`, add an env-expansion helper and build the model. Insert near the top:

```python
import re

_ENV_PAT = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(2) or "")
    return _ENV_PAT.sub(repl, value)
```

Add the import of the new models to the `from app.config.schema import (...)` block: `LangfuseCfg, ObservabilityCfg`.

Build `observability` before constructing `ServingCfg`, and pass it in:

```python
    lf_raw = serving_raw["observability"]["langfuse"]
    observability = ObservabilityCfg(
        langfuse=LangfuseCfg(
            enabled=lf_raw["enabled"],
            host=_expand_env(str(lf_raw["host"])),
            redact_on_export=lf_raw["redact_on_export"],
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        )
    )
```

Add `observability=observability,` to the `ServingCfg(...)` constructor call.

In `app/config/lint.py`, add a check inside `enforce` (use the module's existing `ConfigLintError`):

```python
    lf = settings.serving.observability.langfuse
    if lf.enabled and not lf.redact_on_export:
        raise ConfigLintError(
            "observability.langfuse.redact_on_export must be true when langfuse is "
            "enabled (egress redaction is mandatory; see CLAUDE.md rule 2)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_obs.py tests/test_config_loader.py tests/test_config_lint.py -v`
Expected: PASS. (Verify the existing lint/loader tests still pass — they construct `Settings`; if any build `ServingCfg` directly they must add the new `observability` field.)

- [ ] **Step 5: Commit**

```bash
git add app/config/schema.py app/config/loader.py app/config/lint.py tests/test_config_obs.py
git commit -m "feat(obs): observability config surface + mandatory-redaction lint"
```

---

### Task 2: Egress redaction

**Files:**
- Create: `app/obs/redact.py`
- Test: `tests/test_obs_redact.py`

**Interfaces:**
- Produces: `redact(text: str) -> str` and `redact_value(v)` (passes through non-str). Reuses the same entity patterns as `RegexDetector` so redaction is consistent with intake.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_obs_redact.py
from app.obs.redact import redact, redact_value

def test_redacts_ssn_email_phone():
    out = redact("SSN 123-45-6789, mail a@b.com, call 415-555-0100")
    assert "123-45-6789" not in out
    assert "a@b.com" not in out
    assert "415-555-0100" not in out
    assert "<US_SSN>" in out or "<SSN>" in out

def test_passes_through_clean_text():
    assert redact("just a normal sentence") == "just a normal sentence"

def test_redact_value_non_str_passthrough():
    assert redact_value(42) == 42
    assert redact_value(None) is None
    assert redact_value(1.5) == 1.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_obs_redact.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

```python
# app/obs/redact.py
"""Egress PII redaction for telemetry (CLAUDE.md rule 2).

Reuses the RegexDetector so exported traces are redacted the same way intake is.
Never reads Entity.value into telemetry — it maps detected spans to <TYPE> tags.
"""
from __future__ import annotations

from app.policy.detectors import RegexDetector

_detector = RegexDetector()


def redact(text: str) -> str:
    if not text:
        return text
    entities = _detector.analyze(text)
    if not entities:
        return text
    # Replace right-to-left so earlier offsets stay valid.
    out = text
    for e in sorted(entities, key=lambda x: x.start, reverse=True):
        out = out[: e.start] + f"<{e.type}>" + out[e.end :]
    return out


def redact_value(v):
    return redact(v) if isinstance(v, str) else v
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_obs_redact.py -v`
Expected: PASS. (If the SSN placeholder tag differs, align the assertion to `RegexDetector`'s entity `type` — confirm the type name it emits for an SSN and use `<TYPE>` accordingly.)

- [ ] **Step 5: Commit**

```bash
git add app/obs/redact.py tests/test_obs_redact.py
git commit -m "feat(obs): egress PII redaction reusing the regex detector"
```

---

### Task 3: OTel tracer provider + RedactingSpanExporter

**Files:**
- Create: `app/obs/otel.py`
- Modify: `pyproject.toml` (add `[observability]` extra)
- Test: `tests/test_obs_otel.py`

**Interfaces:**
- Consumes: `settings.serving.observability.langfuse`, `app.obs.redact.redact_value`.
- Produces:
  - `REDACT_KEYS: tuple[str, ...]` — span attribute keys whose values get redacted.
  - `class RedactingSpanExporter(SpanExporter)` — wraps a delegate exporter, redacts `REDACT_KEYS` on a copy of each span's attributes before delegating.
  - `init_tracing(settings) -> TracerProvider | None` — idempotent; returns `None` (no-op) if disabled or the extra is missing.
  - `get_tracer()` — returns the configured tracer or a no-op tracer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_obs_otel.py
import pytest

otel = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from app.obs.otel import RedactingSpanExporter, REDACT_KEYS

def test_redacting_exporter_strips_pii_from_attributes():
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(mem)))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("root") as span:
        span.set_attribute(REDACT_KEYS[0], "patient SSN 123-45-6789")
    provider.force_flush()
    spans = mem.get_finished_spans()
    assert len(spans) == 1
    dumped = str(dict(spans[0].attributes))
    assert "123-45-6789" not in dumped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv sync --extra observability && uv run pytest tests/test_obs_otel.py -v`
Expected: FAIL — `app.obs.otel` does not exist.

- [ ] **Step 3: Implement**

Add to `pyproject.toml` under `[project.optional-dependencies]`:

```toml
observability = [
  "opentelemetry-sdk>=1.27",
  "opentelemetry-exporter-otlp-proto-http>=1.27",
  "langfuse>=3.0",
]
```

```python
# app/obs/otel.py
"""OTel wiring: pipeline spans -> OTLP -> Langfuse, with egress redaction.

Redaction happens twice: authored spans are redacted before set_attribute in
export.py (the guarantee), and RedactingSpanExporter re-redacts on the way out
(defence in depth, and the mechanism NAT's Phase-5 spans reuse).
"""
from __future__ import annotations

import base64
import logging

from app.config.schema import Settings
from app.obs.redact import redact_value

log = logging.getLogger("serving.obs")

# Attribute keys that may carry free text. Includes OTel-GenAI / NAT span keys so
# Phase-5 agent spans are covered by the same exporter.
REDACT_KEYS: tuple[str, ...] = (
    "sp.reason", "sp.input", "sp.output",
    "input.value", "output.value", "tool.output", "gen_ai.prompt", "gen_ai.completion",
)

_provider = None  # module-level singleton


def _build_exporter(lf):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    auth = base64.b64encode(f"{lf.public_key}:{lf.secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=f"{lf.host.rstrip('/')}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {auth}",
            "x-langfuse-ingestion-version": "4",
        },
    )


def init_tracing(settings: Settings):
    """Idempotent. Returns the provider, or None when disabled / extra missing."""
    global _provider
    if _provider is not None:
        return _provider
    lf = settings.serving.observability.langfuse
    if not lf.enabled:
        return None
    if not (lf.public_key and lf.secret_key):
        log.warning("langfuse enabled but LANGFUSE_PUBLIC_KEY/SECRET_KEY unset; "
                    "telemetry export disabled")
        return None
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("observability extra not installed; telemetry export disabled")
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": "serving-plane"}))
    exporter = RedactingSpanExporter(_build_exporter(lf)) if lf.redact_on_export \
        else _build_exporter(lf)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _provider = provider
    return provider


def get_tracer():
    if _provider is None:
        from opentelemetry.trace import NoOpTracer
        return NoOpTracer()
    return _provider.get_tracer("serving-plane")


def _make_redacting_exporter():
    """Deferred so importing app.obs.otel never requires the extra."""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class RedactingSpanExporter(SpanExporter):
        def __init__(self, delegate):
            self._delegate = delegate

        def export(self, spans):
            for span in spans:
                attrs = getattr(span, "_attributes", None)
                if attrs:
                    for k in list(attrs.keys()):
                        if k in REDACT_KEYS:
                            attrs[k] = redact_value(attrs[k])
            return self._delegate.export(spans)

        def shutdown(self):
            return self._delegate.shutdown()

        def force_flush(self, timeout_millis: int = 30000):
            return self._delegate.force_flush(timeout_millis)

    return RedactingSpanExporter


# Bind the class at import time only if the SDK is present; tests importorskip.
try:  # pragma: no cover - exercised via the extra
    RedactingSpanExporter = _make_redacting_exporter()
except Exception:  # SDK not installed
    RedactingSpanExporter = None  # type: ignore
```

Note for the implementer: the test mutates `span._attributes`, a private OTel field. **Verify against the installed `opentelemetry-sdk` that finished `ReadableSpan`s expose a mutable `_attributes` mapping** before relying on it; if the field name differs in the pinned version, adjust `RedactingSpanExporter.export` and the test's `dumped` assertion together. The authored-span redaction in Task 4 is the primary guarantee; this wrapper is defence-in-depth.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_obs_otel.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/obs/otel.py pyproject.toml tests/test_obs_otel.py
git commit -m "feat(obs): OTel provider + RedactingSpanExporter for Langfuse OTLP"
```

---

### Task 4: Real export() from a trace

**Files:**
- Modify: `app/obs/export.py` (replace the no-op)
- Test: `tests/test_obs_export.py`

**Interfaces:**
- Consumes: `get_tracer()`, `redact_value`, the pipeline's response dict fields (`id`, `model`, `cost`, `policy`, `trace`).
- Produces: `export_trace(*, request_id, tenant_id, model, cost, policy, trace) -> None`. Best-effort: any exception is logged and swallowed. Creates a root span per request and a child span per trace record; sets **types-only** attributes; redacts `reason` via `sp.reason`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_obs_export.py
import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import app.obs.otel as otel
import app.obs.export as export_mod

@pytest.fixture
def mem_provider(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem

def _sample():
    return dict(
        request_id="req-1", tenant_id="hospital", model="local-32b",
        cost={"total_usd": 0.0004, "baseline_usd": 0.012, "routing_saved_usd": 0.0116},
        policy={"sensitivity": "restricted", "onprem_only": True,
                "allowed_providers": ["onprem"], "detected_entities": ["US_SSN"]},
        trace=[{"step": "route", "chosen": "local-32b",
                "reason": "leaked SSN 123-45-6789 in reason text", "hop_index": 0,
                "actual_cost_usd": 0.0004, "latency_ms": 12.0}],
    )

def test_export_emits_spans_without_raw_pii(mem_provider):
    export_mod.export_trace(**_sample())
    otel._provider.force_flush()
    spans = mem_provider.get_finished_spans()
    assert len(spans) >= 2  # root + one record
    blob = "".join(str(dict(s.attributes)) + s.name for s in spans)
    assert "123-45-6789" not in blob
    assert any(s.name == "serving.request" for s in spans)

def test_export_noop_and_safe_when_disabled(monkeypatch):
    monkeypatch.setattr(otel, "_provider", None)
    # must not raise even though nothing is configured
    export_mod.export_trace(**_sample())

def test_export_swallows_exporter_errors(mem_provider, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("otlp down")
    monkeypatch.setattr(otel, "get_tracer", boom)
    export_mod.export_trace(**_sample())  # logged + swallowed, no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_obs_export.py -v`
Expected: FAIL — `export_trace` does not exist.

- [ ] **Step 3: Implement**

```python
# app/obs/export.py
"""Export a request's decision trace to Langfuse via OTel. Best-effort: never
raises into the request path (telemetry != policy gate)."""
from __future__ import annotations

import logging

from app.obs.otel import get_tracer
from app.obs.redact import redact_value

log = logging.getLogger("serving.obs")


def export_trace(*, request_id: str, tenant_id: str, model: str,
                 cost: dict, policy: dict, trace: list[dict]) -> None:
    try:
        tracer = get_tracer()
        with tracer.start_as_current_span("serving.request") as root:
            root.set_attribute("sp.request_id", request_id)
            root.set_attribute("sp.tenant_id", tenant_id)
            root.set_attribute("sp.model", model)
            root.set_attribute("sp.cost_total_usd", float(cost.get("total_usd", 0.0)))
            root.set_attribute("sp.cost_baseline_usd", float(cost.get("baseline_usd", 0.0)))
            root.set_attribute("sp.sensitivity", str(policy.get("sensitivity", "")))
            root.set_attribute("sp.onprem_only", bool(policy.get("onprem_only", False)))
            root.set_attribute("sp.detected_entities",
                               ",".join(policy.get("detected_entities", [])))  # TYPES only
            for i, rec in enumerate(trace):
                step = str(rec.get("step", f"step-{i}"))
                with tracer.start_as_current_span(f"serving.{step}") as sp:
                    sp.set_attribute("sp.step", step)
                    if rec.get("chosen") is not None:
                        sp.set_attribute("sp.chosen", str(rec["chosen"]))
                    if "reason" in rec:
                        sp.set_attribute("sp.reason", redact_value(str(rec["reason"])))
                    for k in ("hop_index", "actual_cost_usd", "est_cost_usd", "latency_ms"):
                        if rec.get(k) is not None:
                            sp.set_attribute(f"sp.{k}", float(rec[k]))
    except Exception:  # telemetry must never break serving
        log.exception("trace export failed for %s (swallowed)", request_id)
```

Note: `reason` is redacted before `set_attribute`, so raw PII never enters a span — the RedactingSpanExporter is the second line. Never set `Entity.value` or redaction-map contents.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_obs_export.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add app/obs/export.py tests/test_obs_export.py
git commit -m "feat(obs): export decision trace to Langfuse (types-only, redacted, best-effort)"
```

---

### Task 5: Wire export into the request path + startup init

**Files:**
- Modify: `app/main.py` (call `init_tracing(settings)` at startup)
- Modify: `app/api/routes.py` (call `export_trace(...)` after a successful pipeline run)
- Test: `tests/test_obs_wiring.py`

**Interfaces:**
- Consumes: `init_tracing`, `export_trace`, the response dict from `pipeline.run`.
- Produces: no new public interface; a served request now emits a trace when obs is configured, and export failure never changes the HTTP result.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_obs_wiring.py
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import app.obs.otel as otel

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    app = create_app()
    return TestClient(app)

def test_pii_request_exports_redacted_trace(client, monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user",
                                        "content": "my SSN is 123-45-6789"}]})
    assert r.status_code == 200
    provider.force_flush()
    blob = "".join(str(dict(s.attributes)) + s.name
                   for s in mem.get_finished_spans())
    assert "123-45-6789" not in blob
    assert "serving.request" in blob

def test_request_succeeds_even_if_export_raises(client, monkeypatch):
    import app.api.routes as routes
    monkeypatch.setattr(routes, "export_trace",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer demo-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_obs_wiring.py -v`
Expected: FAIL — `routes` has no `export_trace`, no export happens.

- [ ] **Step 3: Implement**

In `app/main.py`, after `enforce(settings)`:

```python
    from app.obs.otel import init_tracing
    init_tracing(settings)
```

In `app/api/routes.py`, import at top: `from app.obs.export import export_trace`. Then in `chat_completions`, replace the direct `return await ...` with a capture-then-export:

```python
    try:
        resp = await request.app.state.pipeline.run(request_id, tenant, body)
    except PipelineRejected as e:
        raise HTTPException(422, f"response failed verification: {e}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "policy evaluation unavailable")
    try:
        export_trace(request_id=resp["id"], tenant_id=tenant, model=resp["model"],
                     cost=resp["cost"], policy=resp["policy"], trace=resp["trace"])
    except Exception:
        pass  # export_trace already swallows; this is belt-and-suspenders
    return resp
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_obs_wiring.py -v && uv run pytest -q`
Expected: PASS; full suite still green (48+ tests).

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/api/routes.py tests/test_obs_wiring.py
git commit -m "feat(obs): emit redacted Langfuse trace per request; export never fails a request"
```

---

### Task 6: Local Langfuse stack + observability smoke

**Files:**
- Create: `deploy/compose/langfuse.yml`
- Create: `.env.example`
- Create: `scripts/obs-up.sh`, `scripts/smoke-obs.sh`
- Modify: `scripts/README.md`

**Interfaces:**
- Produces: a one-command local Langfuse and a smoke script that proves a PII request lands in Langfuse **with the SSN absent**. This is the Phase-4 "confirm redaction actually happened" gate. Not part of offline pytest.

- [ ] **Step 1: Write the Langfuse compose stack**

```yaml
# deploy/compose/langfuse.yml
# Self-hosted Langfuse for local observability testing (Phase 4).
# Brings up Langfuse v3 (web+worker) with Postgres, ClickHouse, Redis, MinIO.
# Full app stack (api image + this) is assembled in Phase 8.
name: serving-plane-obs
services:
  langfuse-web:
    image: langfuse/langfuse:3
    depends_on: [postgres, clickhouse, redis, minio]
    ports: ["3000:3000"]
    environment:
      DATABASE_URL: postgresql://postgres:postgres@postgres:5432/postgres
      CLICKHOUSE_URL: http://clickhouse:8123
      CLICKHOUSE_USER: default
      CLICKHOUSE_PASSWORD: clickhouse
      REDIS_CONNECTION_STRING: redis://redis:6379
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://minio:9000
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: minio
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: miniosecret
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
      NEXTAUTH_URL: http://localhost:3000
      NEXTAUTH_SECRET: local-dev-secret
      SALT: local-dev-salt
      ENCRYPTION_KEY: 0000000000000000000000000000000000000000000000000000000000000000
      LANGFUSE_INIT_ORG_ID: demo-org
      LANGFUSE_INIT_PROJECT_ID: demo-project
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:-pk-lf-local}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_SECRET_KEY:-sk-lf-local}
      LANGFUSE_INIT_USER_EMAIL: demo@local
      LANGFUSE_INIT_USER_PASSWORD: demodemo
  langfuse-worker:
    image: langfuse/langfuse-worker:3
    depends_on: [postgres, clickhouse, redis, minio]
    environment:
      DATABASE_URL: postgresql://postgres:postgres@postgres:5432/postgres
      CLICKHOUSE_URL: http://clickhouse:8123
      CLICKHOUSE_USER: default
      CLICKHOUSE_PASSWORD: clickhouse
      REDIS_CONNECTION_STRING: redis://redis:6379
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://minio:9000
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: minio
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: miniosecret
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
      SALT: local-dev-salt
      ENCRYPTION_KEY: 0000000000000000000000000000000000000000000000000000000000000000
  postgres:
    image: postgres:16
    environment: {POSTGRES_PASSWORD: postgres}
    volumes: [obs-pg:/var/lib/postgresql/data]
  clickhouse:
    image: clickhouse/clickhouse-server:24
    environment: {CLICKHOUSE_PASSWORD: clickhouse}
    volumes: [obs-ch:/var/lib/clickhouse]
  redis:
    image: redis:7
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment: {MINIO_ROOT_USER: minio, MINIO_ROOT_PASSWORD: miniosecret}
    volumes: [obs-minio:/data]
volumes: {obs-pg: {}, obs-ch: {}, obs-minio: {}}
```

- [ ] **Step 2: Write `.env.example`**

```bash
# .env.example — copy to .env for local Langfuse. .env is gitignored.
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
```

- [ ] **Step 3: Write `scripts/obs-up.sh`**

```bash
#!/usr/bin/env bash
# Bring up local Langfuse (Phase 4). First boot takes ~1-2 min (migrations).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a
docker compose -f deploy/compose/langfuse.yml up -d
echo "Langfuse coming up at $LANGFUSE_HOST (login demo@local / demodemo)"
echo "waiting for health..."
for i in $(seq 1 90); do
  curl -sf "$LANGFUSE_HOST/api/public/health" >/dev/null 2>&1 && { echo "ready"; exit 0; }
  sleep 2
done
echo "timed out waiting for Langfuse" >&2; exit 1
```

- [ ] **Step 4: Write `scripts/smoke-obs.sh`**

```bash
#!/usr/bin/env bash
# End-to-end observability proof (Phase 4): a PII request lands in Langfuse with
# the SSN absent. Requires: scripts/obs-up.sh already ran.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export SP_PII_DETECTION=regex

uv sync --extra observability >/dev/null
uv run uvicorn app.main:app --port 8080 >/tmp/sp-obs-api.log 2>&1 &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT
for i in $(seq 1 30); do curl -sf localhost:8080/health >/dev/null 2>&1 && break; sleep 0.5; done

SSN="123-45-6789"
RID=$(curl -s localhost:8080/v1/chat/completions \
  -H 'authorization: Bearer hospital-key' -H 'content-type: application/json' \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"my SSN is $SSN\"}]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "request $RID sent; flushing to Langfuse..."
sleep 8  # allow BatchSpanProcessor + Langfuse ingestion

AUTH=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64)
TRACES=$(curl -s "$LANGFUSE_HOST/api/public/traces?limit=50" -H "authorization: Basic $AUTH")
echo "$TRACES" | grep -q "$SSN" && { echo "FAIL: raw SSN found in Langfuse traces"; exit 1; }
echo "$TRACES" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("data"), "no traces ingested"' \
  || { echo "FAIL: no traces ingested"; exit 1; }
echo "SMOKE-OBS: trace ingested, SSN absent — PASS"
```

- [ ] **Step 5: Make scripts executable, document, commit**

```bash
chmod +x scripts/obs-up.sh scripts/smoke-obs.sh
git add deploy/compose/langfuse.yml .env.example scripts/obs-up.sh scripts/smoke-obs.sh scripts/README.md
git commit -m "feat(obs): local Langfuse stack + redaction smoke proof"
```

Add to `scripts/README.md`:
```
- obs-up.sh       — start local Langfuse (Postgres/ClickHouse/Redis/MinIO)
- smoke-obs.sh    — prove a PII request lands in Langfuse with the SSN redacted
```

---

## Self-Review

**Spec coverage:** design §10.4 (Langfuse redacting exporter, self-hosted, types-only) → Tasks 2–6; §10.3 telemetry-source note (NAT) → deferred to Phase 5, REDACT_KEYS already includes NAT/GenAI span keys so the exporter covers them. CLAUDE.md rule 2 (egress redaction) → Task 1 lint + Task 2/4 redaction + Task 6 proof. Definition-of-done "Langfuse contains every trace, PII redacted" → Task 5 wiring + Task 6 smoke.

**Placeholder scan:** none — every step has full code or a concrete command. The two version-sensitive spots (`ReadableSpan._attributes`, Langfuse OTLP header names) carry explicit verify-against-installed-version notes, which are legitimate TDD steps, not placeholders.

**Type consistency:** `export_trace(**)` keyword names match the response dict keys built in `pipeline.py` (`id`→request_id, `model`, `cost`, `policy`, `trace`). `LangfuseCfg` fields consumed identically in `otel.py`. `RedactingSpanExporter` used the same way in Tasks 3, 4, 5 tests.
