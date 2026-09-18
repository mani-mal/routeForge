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
4. **Redact** — replace entities with stable placeholders (`<SSN_1>`). Not tokenize;
   see the return-path note below.
5. **Determine residency** — `any | private_cloud_ok | onprem_only`; set
   `onprem_only` when policy requires it.
6. **Emit `ExecutionPolicyContext`** — immutable, travels with the request.

## The compiled effective policy — read it, don't re-derive it

Since P1a, one pure function decides every axis: `compile_policy` in
`app/policy/compile.py`. The gate, the pipeline, the exporter, step 9 and the admin
API all read its `EffectivePolicy`; **nothing branches on `policy_mode` any more.**

- **Placement** is `residency`, and only `residency`. No protection axis may appear in
  a placement computation, ever. The one cross-axis path is `apply_signals` applying a
  declared `sensitivity_rules` entry — raise-only, gated by the app's
  `escalate_residency_on_detection`, and named in `Placement.sources`.
- **Protection** is `model_input`, `client_output`, `telemetry_io`, `detection`,
  `content_integrity`, `stream_mode`. Each consumer selects **its own view** from the
  frozen `Representations` record the gate returns (`raw`, `redacted`,
  `detected_entity_types`): the model by `model_input`, the client by `client_output`,
  the exporter by `telemetry_io`. **No consumer reads another's view** — that coupling
  was counterexample CE-1, which exported raw text under a `redacted` label.
- **A detector is built when any consumer needs one** (`model_input: redact`, or
  `client_output: redact`, or `telemetry_io: redacted`, or escalation). `detection`
  chooses its kind. `text_redacted` is `None` **exactly when no detector ran**, so
  nothing can silently substitute the raw view for a redacted one.
- **Everything unparseable denies** — an axis value, a deployment default, a ceiling.
  `resolve_residency` is shared with `app/policy/context.py` so the compiler and the
  placement path cannot disagree about one app's placement.

`tests/policy_compiler/` is the property harness over this seam. Run it when touching
`compile.py`, `context.py`, `gate.py` or `export.py`.

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

**Return path — placeholders, and only placeholders.** When the model is shown a
redacted view it sees `<SSN_1>`, its completion contains `<SSN_1>`, and that is what
the client gets. State this out loud so nobody files it as a bug.

> **There is no tokenization and no detokenization in RouteForge. Do not document
> one.** The map is built during `anonymize()` and then **discarded** — the binding
> in `app/pipeline.py` is `_map`, deliberately unused, and nothing anywhere
> re-inserts an original value. There is no broker, no `redaction_map_id` lookup on
> egress, and no config switch that turns one on. `model_input: "tokenize"` and
> `client_output: "authorized_rehydrate"` are named in the P1a design and are
> **rejected with a 422** (`AXIS_VALUE_NOT_IMPLEMENTED`) rather than faked.
>
> The practical consequence, which a customer will hit: **a redacted value cannot be
> a lookup key.** `<US_SSN_1>` is a placeholder, not a reference. An app whose tool
> must look a record up by SSN needs `model_input: "raw"` — that is what the axis is
> for — or another identifier. Saying otherwise promises a protection that does not
> run.

**What *is* built, and is the right answer for that customer:** the data-handling
axes are independent. `model_input: "raw"` with `client_output: "redact"` shows the
in-boundary model the real value while the HTTP response and the trace carry none.
See `docs/reference/per-app-config.md`.

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
input never had (a model retrieves a record; a tool returns a customer row). Egress is
its own decision, on its own axis — **`client_output`, never `model_input`**:
`app/output/verify.py::egress_detector` is the single place that choice is made, so
`verify()`, the tool-call argument redactor and the streaming hold-back redactor cannot
drift apart. An app running `model_input: raw` + `client_output: redact` gets full egress
redaction over a completion the model produced from real values.

Telemetry is a **third, independent** projection. `app/obs/export.py::_set_io` builds its
own redacted copy from the raw values with the app's detector; it never reuses the view
the gate handed the model. `resolve_capture_io` is the one place the mode is resolved and
it fails closed twice: `full` with no own Langfuse project → `redacted`; `redacted` with
no detector → **`off`**, never raw. Exporter wiring lives in `app/obs/export.py` /
`app/obs/otel.py`.

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
