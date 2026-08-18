# F1 Phase A — Honest Redaction (B13) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `redacted` Langfuse telemetry show exactly what the model saw — the gate's sanitized prompt — instead of independently re-redacting the raw request body (which showed *less* than the model saw and could under-detect).

**Architecture:** The gate already computes the full sanitized prompt (`app/pipeline.py:95`, `sanitized`) but the pipeline discards it and `app/api/routes.py:195` hands the **raw** `body.messages[-1].content` to `export_trace`, which re-runs detection on just the last message. Fix: thread the gate's `sanitized` text through the response dict into `export_trace`, and have `_set_io` use it for the `redacted` branch while keeping raw for the `full` branch (raw is the point of `full` — distillation substrate, residency-gated by B14 later).

**Tech Stack:** Python 3.12, FastAPI, pytest, OpenTelemetry (in-memory span exporter for tests), React/TypeScript (UI caption).

## Global Constraints

- **Metadata always-on; `off` captures no I/O** — do not change the `off` behavior (blank Input). (`docs/architecture/self-improving-agent-feature-fit.md`)
- **`full` stores RAW** — `full` mode must keep storing the raw prompt/completion (residency-local distillation substrate). This task must NOT make `full` store sanitized text. (F1 spec §2 invariant 2)
- **`redacted` ≥ gate** — telemetry redaction must never reveal an entity the model did not see; reuse the gate's sanitized text. (F1 spec §2 invariant 3)
- **Backward-compatible signature** — `sanitized_prompt` is a new keyword arg defaulting to `""`; when absent, `_set_io` falls back to today's `redact(prompt)` so existing callers/tests are unaffected.
- **Don't leak internal fields to the client** — `sanitized_prompt` is threaded on the internal `resp` dict but popped before the response is returned/recorded.
- Tests use the **regex** detector (`SP_PII_DETECTION=regex`); `_SSN = "482-73-1946"` masks to `<US_SSN>`.

---

## File Structure

- `app/obs/export.py` — `export_trace` + `_set_io` gain a `sanitized_prompt` param; `_set_io`'s `redacted` branch prefers it. (Task 1)
- `app/pipeline.py` — both `run()` return dicts expose `sanitized_prompt`. (Task 2)
- `app/api/routes.py` — thread `resp["sanitized_prompt"]` into `export_trace`; pop it before the response leaves. (Task 2)
- `ui/src/console/RequestDrilldown.tsx` — replace the false unconditional "never stored" caption with honest, mode-aware copy. (Task 3)
- `tests/test_capture_io.py` — extend with Task 1 + Task 2 assertions.

---

### Task 1: `_set_io` uses the gate's sanitized prompt for `redacted`, raw for `full`

**Files:**
- Modify: `app/obs/export.py:37-62` (`_set_io`), `app/obs/export.py:65-71` (`export_trace` signature), `app/obs/export.py:108` (call into `_set_io`)
- Test: `tests/test_capture_io.py`

**Interfaces:**
- Produces: `export_trace(..., sanitized_prompt: str = "")` — the gate's already-masked prompt. `_set_io(root, capture_io, prompt, completion, recognizers=None, sanitized_prompt="")`.
- `redacted` → `input.value` = `sanitized_prompt` if provided, else `redact(prompt)` (fallback). `full` → `input.value` = raw `prompt` (unchanged). `off` → nothing.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_capture_io.py`:

```python
def test_redacted_uses_gate_sanitized_prompt(monkeypatch):
    # When the gate's sanitized prompt is supplied, redacted mode stores THAT
    # verbatim (never weaker than what the model saw) rather than re-redacting raw.
    mem, provider = _wire(monkeypatch, redacting=False)
    _export(capture_io="redacted",
            prompt=f"my SSN is {_SSN}",                 # raw (ignored in redacted)
            sanitized_prompt="my SSN is <US_SSN>")       # gate output
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert attrs["input.value"] == "my SSN is <US_SSN>"
    assert _SSN not in attrs["input.value"]


def test_full_ignores_sanitized_prompt_and_keeps_raw(monkeypatch):
    # full stores RAW for distillation; sanitized_prompt must NOT override it.
    monkeypatch.setattr(otel, "_capture_io_full", True)
    mem, provider = _wire(monkeypatch, redacting=True)
    _export(capture_io="full",
            prompt=f"my SSN is {_SSN}",
            sanitized_prompt="my SSN is <US_SSN>")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert _SSN in attrs["input.value"]                  # raw, on purpose
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capture_io.py::test_redacted_uses_gate_sanitized_prompt tests/test_capture_io.py::test_full_ignores_sanitized_prompt_and_keeps_raw -v`
Expected: FAIL with `TypeError: export_trace() got an unexpected keyword argument 'sanitized_prompt'`.

- [ ] **Step 3: Add the param and branch logic**

In `app/obs/export.py`, change `_set_io`'s signature and `redacted` branch:

```python
def _set_io(root, capture_io: str, prompt: str, completion: str,
            recognizers=None, sanitized_prompt: str = "") -> None:
```

Replace the `redacted` branch body (currently `prompt = redact(prompt, ...)`) with:

```python
    if capture_io == "redacted":
        # Prefer the gate's already-sanitized prompt so telemetry can never be
        # weaker than what the model saw (B13). Fall back to re-redacting raw
        # for callers that don't supply it (backward-compatible).
        prompt = sanitized_prompt or redact(prompt, recognizers=recognizers)
        completion = redact(completion, recognizers=recognizers)
    # `full`: leave prompt/completion raw (lint-gated, residency-local only).
```

Add `sanitized_prompt: str = ""` to `export_trace`'s signature (after `completion: str = ""`) and thread it at the call site (`export.py:108`):

```python
            _set_io(root, capture_io, prompt, completion,
                    recognizers=recognizers, sanitized_prompt=sanitized_prompt)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_capture_io.py -v`
Expected: PASS (new tests pass; `test_redacted_masks_pii`, `test_redacted_capture_end_to_end` still pass via the fallback).

- [ ] **Step 5: Commit**

```bash
git add app/obs/export.py tests/test_capture_io.py
git commit -m "feat(obs): export_trace accepts gate-sanitized prompt for redacted capture (B13)"
```

---

### Task 2: Thread the gate's sanitized prompt pipeline → routes → export

**Files:**
- Modify: `app/pipeline.py` (direct return dict ~`:181`, multi-step return dict ~`:287`)
- Modify: `app/api/routes.py:195,201-205`
- Test: `tests/test_capture_io.py`

**Interfaces:**
- Consumes: `export_trace(..., sanitized_prompt=...)` from Task 1.
- Produces: `resp["sanitized_prompt"]: str` on the pipeline result (the gate's masked prompt). Popped in `routes.py` before the response is returned — never sent to the client.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_capture_io.py` (end-to-end; a 2-message request proves telemetry now reflects the *full* model input, not just the last message):

```python
def test_redacted_capture_reflects_full_model_input(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    monkeypatch.setenv("SP_CAPTURE_IO", "redacted")
    from app.main import create_app
    client = TestClient(create_app())

    mem, provider = _wire(monkeypatch, redacting=True)
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto", "messages": [
                        {"role": "user", "content": f"patient SSN is {_SSN}"},
                        {"role": "user", "content": "please summarize"}]})
    assert r.status_code == 200
    # Internal plumbing field must NOT leak to the client:
    assert "sanitized_prompt" not in r.json()
    provider.force_flush()
    attrs = _root_attrs(mem)
    # Old code redacted only messages[-1] ("please summarize") — the SSN from the
    # FIRST message was absent from telemetry. Now telemetry carries the whole
    # sanitized prompt the model actually received.
    assert "<US_SSN>" in attrs["input.value"]
    assert "summarize" in attrs["input.value"]
    assert _SSN not in str(attrs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capture_io.py::test_redacted_capture_reflects_full_model_input -v`
Expected: FAIL — `input.value` contains `"please summarize"` only, so `assert "<US_SSN>" in attrs["input.value"]` fails.

- [ ] **Step 3: Expose `sanitized_prompt` from the pipeline**

In `app/pipeline.py`, add to the **direct** return dict (the one starting near `:181`, which is inside `run()` where `sanitized` is in scope) a new key:

```python
            "sanitized_prompt": sanitized,
```

In the **multi-step** return dict (inside `_run_multi_step`, near `:287`), which receives `san_messages`, add:

```python
            "sanitized_prompt": san_messages[-1].content if san_messages else "",
```

- [ ] **Step 4: Thread it through routes and pop it**

In `app/api/routes.py`, replace line 195:

```python
        prompt = body.messages[-1].content if body.messages else ""
```

with (keep raw `prompt` for `full`; add the gate-sanitized value and remove it from the client response):

```python
        prompt = body.messages[-1].content if body.messages else ""
        sanitized_prompt = resp.pop("sanitized_prompt", "")
```

and add `sanitized_prompt=sanitized_prompt,` to the `export_trace(...)` call (alongside `capture_io=capture_io, prompt=prompt, completion=completion,`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_capture_io.py -v`
Expected: PASS (new e2e test passes; all prior capture-io tests still pass).

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py app/api/routes.py tests/test_capture_io.py
git commit -m "feat(obs): telemetry uses the gate's full sanitized prompt, not the raw last message (B13)"
```

---

### Task 3: Correct the false "never stored" UI caption

**Files:**
- Modify: `ui/src/console/RequestDrilldown.tsx:148-150`

**Interfaces:** none (static copy). Interim fix — B14 Phase C replaces this with per-mode symmetric display.

- [ ] **Step 1: Replace the caption**

The current text (`:148-150`) claims unconditionally that the prompt is never stored, which is false whenever `capture_io != off`. Replace:

```tsx
          <p className="text-xs text-gray-500 pt-2">
            The raw prompt is never stored or shown — nothing external sees it until the gate clears it.
          </p>
```

with:

```tsx
          <p className="text-xs text-gray-500 pt-2">
            Nothing external sees the request until the policy gate clears it. If telemetry
            capture is on, only the gate-sanitized prompt (entity values masked) is stored —
            never more than the model itself received.
          </p>
```

- [ ] **Step 2: Verify the UI still type-checks and builds**

Run: `cd ui && npm run typecheck`
Expected: PASS (no type errors). This is a static copy change; the UI has no component-test runner, so the build/typecheck plus a manual read of the drilldown is the verification.

- [ ] **Step 3: Commit**

```bash
git add ui/src/console/RequestDrilldown.tsx
git commit -m "fix(ui): honest telemetry caption on the request drilldown (B13)"
```

---

## Self-Review

**Spec coverage (F1 spec Phase A):**
- "Surface the gate's sanitized prompt from the pipeline" → Task 2 (Step 3). ✓
- "pass sanitized (not raw body) to export_trace" → Task 2 (Step 4), corrected so `full` still passes raw. ✓ (Refines the spec, which as written would have made `full` store sanitized — see Global Constraints.)
- "_set_io: when a pre-sanitized prompt is supplied, do not re-run Presidio on the raw body" → Task 1. ✓
- "UI copy corrected" → Task 3. ✓
- Accepted limitation (business fields aren't entities) → unchanged; not a task. ✓

**Placeholder scan:** none — every step carries real code, exact paths, and runnable commands.

**Type consistency:** `sanitized_prompt: str` is the same name/type across `_set_io`, `export_trace`, the `resp` dict key, and the `routes.py` local. `resp.pop("sanitized_prompt", "")` matches the key set in `pipeline.py`.

**Note for executor:** the F1 spec's Phase A said to pass the sanitized prompt *as* `prompt=`; this plan deliberately threads it as a **separate** `sanitized_prompt=` so `full` mode keeps storing raw (distillation substrate). This is the correct, forward-compatible design; the spec text is the looser version.
