---
name: privacy-gate
description: Use when implementing or modifying steps 2-3 of the Serving Plane - identity, PII/PHI detection, redaction, tenant policy, residency rules, and the ExecutionPolicyContext. Trigger on any mention of Presidio, PII, redaction, policy gate, residency, onprem_only, or fail-closed behaviour.
---

# Privacy & Policy Gate (Steps 2–3)

This is the step that makes the privacy-first claim credible. Get it wrong and the
whole claim collapses.

## The ordering rule

The gate runs **before anything external**. No classifier, no cloud model, no tool,
no telemetry export sees the raw request first. When refactoring, re-verify this —
it is the easiest thing to accidentally break and the failure is silent.

Compression (step 5) sits **downstream** of this gate and must only ever see
sanitized content — otherwise Headroom's CCR cache holds raw PII. See the
`headroom-compression` skill.

## What the gate does

1. **Authenticate** — `rf_live_…` app token → application (tenant). DB-backed, fail-closed.
2. **Detect** — Presidio scan for PII/PHI/secrets.
3. **Classify** — assign a sensitivity from the **canonical enum**, defined once in
   `config/policy.yaml` as `public | internal | confidential | restricted` (least →
   most sensitive). Use these names verbatim — the compression bypass rules, the
   policy rules, and the UI all match on them; a fourth spelling silently never
   matches. `public` is the fallback when no entities are found.
4. **Redact/tokenize** — replace entities with stable placeholders (`<SSN_1>`).
5. **Determine residency** — `any | private_cloud_ok | onprem_only`; set
   `onprem_only` when policy requires it.
6. **Emit `ExecutionPolicyContext`** — immutable, travels with the request.

## ExecutionPolicyContext

```python
class ExecutionPolicyContext(BaseModel):
    model_config = ConfigDict(frozen=True)   # immutable; never mutate downstream

    request_id: str
    tenant_id: str
    sensitivity: Sensitivity
    onprem_only: bool
    allowed_providers: frozenset[str]   # pool is filtered HERE, not in the router
    allowed_models: frozenset[str]
    max_cost_usd: float
    max_latency_ms: int
    risk_class: RiskClass
    detected_entities: tuple[str, ...]   # TYPES only — never values
    redaction_map_id: str | None         # pointer; map itself never leaves the gate
```

Two details that matter:

- `allowed_providers` is **computed in the gate**. The router receives an
  already-filtered pool. A cloud provider must be *unreachable by construction* for
  an `onprem_only` request, not merely rejected later.
- `detected_entities` holds entity *types*, never values. The redaction map stays
  in the gate keyed by `redaction_map_id`.

## The redaction map is a second PII store — treat it like CCR

The map behind `redaction_map_id` holds the *original values* (`<SSN_1>` → the real
SSN). It is a second copy of sensitive data, exactly like Headroom's CCR cache, and
gets the same treatment: lives inside the residency boundary, per-request scope,
enforced TTL, **never** exported to telemetry, never returned to the caller. Assert
this with a test, the same way CCR residency is asserted.

**Return path — a deliberate decision, not an accident.** The model sees `<SSN_1>`
and its completion will contain `<SSN_1>`. Two supported behaviours, chosen by
config, never left implicit:

- **Placeholder mode (default): return placeholders.** The response keeps `<SSN_1>` — it
  *shows* redaction working. State this out loud so nobody files it as a bug.
- **Detokenize on egress.** Step 10 re-inserts values from the map **inside the
  gate's trust boundary**, after step 9's leakage checks, before the response leaves.
  Detokenization never happens outside the gate.

See the `output-controls` skill for where detokenization sits relative to the step-9
leakage check.

## Fail closed

```python
try:
    ctx = gate.evaluate(request)
except Exception:
    log.exception("policy_gate_failed", request_id=rid)
    raise HTTPException(503, "Policy evaluation unavailable")
```

Never `except: pass` into a permissive default. No policy = reject.

## Presidio notes

Default analyzers cover most demo cases. Add a recognizer for anything
customer-specific (employee IDs, account numbers) — customers notice when their own
format is missed, and it lands well when it's caught.

```python
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

results = analyzer.analyze(text=text, language="en")
anonymized = anonymizer.anonymize(text=text, analyzer_results=results)
```

Set `pii_detection: regex` as a lightweight fallback so the demo runs if the
Presidio model download fails on a conference wifi. This has happened.

## Redaction on egress — the part usually missed

The gate redacts the *input*. Model completions and tool outputs can contain PII the
input never had (a model retrieves a record; a tool returns a customer row). Run
redaction again at the telemetry exporter before anything reaches Langfuse.

Exporter wiring lives in `app/obs/export.py` / `app/obs/otel.py`.

## What to show in the UI

Per request: entity types found (never values), sensitivity, `onprem_only` flag, and
**which providers were excluded and why**. The greyed-out cloud providers are the
visual that sells the privacy story.

## Tests

- SSN/email/phone → detected, redacted, `onprem_only: true`
- Clean request → `sensitivity: public`, full pool available
- Gate exception → 503, no model call attempted
- `onprem_only` + no onboarded on-prem model enabled (`registry.has_healthy_onprem()`
  false) → rejected, **never** routed to cloud
- Langfuse payload → assert raw PII absent from the exported trace
- Redaction map → inside residency boundary, absent from every exported span, TTL enforced
