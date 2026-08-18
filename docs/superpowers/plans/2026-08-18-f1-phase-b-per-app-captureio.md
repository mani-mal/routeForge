# F1 Phase B — Per-App `capture_io` + Strict Residency Lint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `capture_io` from a single global switch to a per-application setting (`off | redacted | full`, null = inherit global), with a strict residency lint so an application may store **raw** telemetry (`full`) only when it is non-`onprem_only` AND has its own provisioned Langfuse project — and never silently degrade `full` to `redacted`.

**Architecture:** Per-app value lives on `AppPolicy.capture_io`, surfaced through `AppPolicyView` and resolved per-request in `routes.py` (app value else global). Three enforcement layers, all fail-closed: (1) **write-time lint** in `set_policy` rejects an ineligible `full`; (2) **runtime clamp** in `routes.py` downgrades a stored `full` to `redacted` if the app is not eligible at request time; (3) **per-app-exporter raw-passthrough** — a full app's own Langfuse exporter is built to skip I/O redaction, while the shared exporter always redacts I/O, so raw can only ever land in the app's own residency-local project. `_set_io` authoring stays per-call.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), Pydantic v2, pytest, OpenTelemetry (in-memory exporter in tests), React/TypeScript (admin UI).

## Global Constraints

- **Tri-state enum:** `off | redacted | full`; `null`/absent on the policy row = inherit the global `serving.observability.langfuse.capture_io`. Invalid/unknown → fail closed to `off` at resolution.
- **`full` eligibility (the load-bearing rule):** an app resolves to `full` only if `residency != "onprem_only"` (using `p.residency or app.default_residency`) AND `load_app_langfuse(app) is not None` (own provisioned project). Otherwise: write-time PUT is rejected, and runtime clamps to `redacted`. (F1 spec §2 invariant 2; CLAUDE.md rules #2/#6/#8 — residency beats eval.)
- **`redacted` ≥ gate; metadata always-on** — unchanged from Phase A; `sp.*` always emitted regardless of mode.
- **Raw only to own project:** a `full` app always has its own project (eligibility), so its trace uses the per-app exporter (`get_app_tracer`); the shared exporter always redacts I/O. Raw never lands in the shared project.
- **No migration tool:** `Base.metadata.create_all` is the schema source of truth; the new column is additive (new DBs get it automatically). Deployed DBs need a manual `ALTER TABLE app_policies ADD COLUMN capture_io VARCHAR(16)` — call this out in the PR description (memory `routeforge-schema-no-migrations`).
- **`capture_io` is a POLICY field** (`PolicyIn` / `PUT /admin/applications/{id}/policy`), alongside `compression_enabled` — NOT `UpdateAppIn`/`PATCH /applications/{id}`.
- Tests: regex detector (`SP_PII_DETECTION=regex`), `_SSN = "482-73-1946"` → `<US_SSN_1>` (gate numbers placeholders).

---

## File Structure

- `app/apps/models.py` — add `AppPolicy.capture_io` column. (Task 1)
- `app/apps/policy.py` — `AppPolicyView.capture_io` field + `policy_view_from` mapping. (Task 1)
- `app/apps/store.py` — `upsert_policy` allowlist gains `capture_io`. (Task 1)
- `app/api/admin_routes.py` — `PolicyIn.capture_io`, `_policy_dict`, and the write-time lint in `set_policy`. (Task 1 for plumbing, Task 4 for lint)
- `ui/src/api.ts` — `AdminPolicy.capture_io`. (Task 1)
- `app/api/routes.py` — per-request resolution + fail-closed clamp. (Task 2)
- `app/obs/otel.py` — `RedactingSpanExporter(skip_io=...)`; `get_app_tracer(..., capture_io_full)`. (Task 3)
- `app/obs/export.py` — thread the effective `capture_io` into the per-app tracer selection. (Task 3)
- `ui/src/admin/ApplicationsPanel.tsx` — `capture_io` tri-state select with `fullDisabled`. (Task 5)
- Tests: `tests/test_app_policy.py`, `tests/test_capture_io.py`, `tests/test_admin_api.py`.

---

### Task 1: Persist and surface `capture_io` on the app policy (plumbing)

**Files:**
- Modify: `app/apps/models.py:108`, `app/apps/policy.py:33,54`, `app/apps/store.py:182`, `app/api/admin_routes.py:44,97`, `ui/src/api.ts:201`
- Test: `tests/test_app_policy.py`

**Interfaces:**
- Produces: `AppPolicy.capture_io: str | None`; `AppPolicyView.capture_io: str | None` (None = inherit); PUT `/admin/applications/{id}/policy` accepts+returns `capture_io`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app_policy.py` (reuse its `_with_db`, `_seed_policy`, `create_app`, `TestClient` helpers):

```python
def test_capture_io_persists_and_round_trips_via_policy_api(tmp_path):
    db_url, restore = _with_db(tmp_path)
    try:
        from tests.test_admin_api import _login  # admin session helper
        with TestClient(create_app()) as c:
            _login(c)
            c.post("/admin/applications", json={"id": "acme", "name": "Acme"})
            r = c.put("/admin/applications/acme/policy", json={"capture_io": "redacted"})
            assert r.status_code == 200
            assert r.json()["policy"]["capture_io"] == "redacted"
    finally:
        restore()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app_policy.py::test_capture_io_persists_and_round_trips_via_policy_api -v`
Expected: FAIL — `KeyError: 'capture_io'` (not in `_policy_dict`) or the field is dropped (not in `PolicyIn`/allowlist).

- [ ] **Step 3: Add the column, view field, allowlist, PolicyIn field, serializer, UI type**

`app/apps/models.py` — after line 108 (`compression_enabled`):

```python
    # Per-app eval-capture override (F1 Phase B). null = inherit the live global
    # switch (s.serving.observability.langfuse.capture_io). "off"|"redacted"|"full".
    # "full" is gated: only an app that is NOT onprem_only AND has its own
    # provisioned Langfuse project may resolve to it (write-time lint in
    # admin_routes.set_policy + runtime clamp in api/routes.py).
    capture_io: Mapped[str | None] = mapped_column(String(16), nullable=True)
```

`app/apps/policy.py` — add field at line 33 and mapping at line 54:

```python
    capture_io: str | None = None             # None = inherit global capture_io
```
```python
        capture_io=p.capture_io,
```

`app/apps/store.py:182` — add `"capture_io"` to the `allowed` set:

```python
    allowed = {"allowed_tiers", "allowed_providers", "allowed_models",
               "residency", "max_spend_usd_per_day", "max_rpm",
               "compression_enabled", "capture_io"}
```

`app/api/admin_routes.py` — `PolicyIn` (after line 97) and `_policy_dict` (line 44):

```python
    capture_io: str | None = None   # None = inherit global; off|redacted|full
```
```python
            "compression_enabled": p.compression_enabled, "capture_io": p.capture_io}
```

`ui/src/api.ts:201` — widen `AdminPolicy`:

```ts
  capture_io: string | null;   // null = inherit global capture_io
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app_policy.py -v`
Expected: PASS (new test green; existing policy tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add app/apps/models.py app/apps/policy.py app/apps/store.py app/api/admin_routes.py ui/src/api.ts tests/test_app_policy.py
git commit -m "feat(apps): per-app capture_io policy field, persisted + surfaced via policy API (B14)"
```

---

### Task 2: Per-request resolution + fail-closed clamp

**Files:**
- Modify: `app/api/routes.py:194`
- Test: `tests/test_app_policy.py`

**Interfaces:**
- Consumes: `AppPolicyView.capture_io` (Task 1), `app_policy` and `app_langfuse` (both already in scope at `routes.py:95`).
- Produces: the effective `capture_io` passed to `export_trace` is `app_policy.capture_io or global`, then clamped: `full` → `redacted` unless `app_policy.residency != "onprem_only"` AND `app_langfuse is not None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app_policy.py`. The observable: a per-app `redacted` override makes an otherwise-`off` global request attach `input.value`; and a per-app `full` on an ineligible (onprem, no own project) app must NOT store raw (clamped). Use the `_wire`/`_root_attrs` helpers from `tests/test_capture_io.py`.

```python
def test_per_app_capture_io_overrides_global_and_clamps_full(tmp_path, monkeypatch):
    from tests.test_capture_io import _root_attrs, _wire
    _SSN = "482-73-1946"
    db_url, restore = _with_db(tmp_path)
    try:
        monkeypatch.setenv("SP_PII_DETECTION", "regex")
        # global stays off; per-app override drives capture. `hospital` is
        # onprem_only with no own Langfuse project -> `full` must clamp to redacted.
        _seed_policy(db_url, "hospital", capture_io="full")
        with TestClient(create_app()) as c:
            mem, provider = _wire(monkeypatch, redacting=True)
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer hospital-key"},
                       json={"model": "auto",
                             "messages": [{"role": "user", "content": f"ssn {_SSN}"}]})
            assert r.status_code == 200
            provider.force_flush()
            attrs = _root_attrs(mem)
            # capture happened (override beat the global 'off') ...
            assert "input.value" in attrs
            # ... but clamped to redacted: raw SSN absent despite capture_io='full'
            assert _SSN not in str(attrs)
            assert "<US_SSN_1>" in attrs["input.value"]
    finally:
        restore()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app_policy.py::test_per_app_capture_io_overrides_global_and_clamps_full -v`
Expected: FAIL — `input.value` absent (global `off` used; per-app value ignored).

- [ ] **Step 3: Resolve + clamp in routes.py**

Replace `app/api/routes.py:194` (`capture_io = s.serving.observability.langfuse.capture_io`) with:

```python
        # Per-app capture_io (B14) overrides the global default; null = inherit.
        capture_io = app_policy.capture_io or s.serving.observability.langfuse.capture_io
        # Fail-closed clamp: `full` stores RAW, permitted only for a non-onprem app
        # with its OWN Langfuse project (so raw lands only in a residency-local
        # project). Otherwise downgrade to `redacted` — never store raw (rules #2/#6/#8).
        if capture_io == "full" and (
                app_policy.residency == "onprem_only" or app_langfuse is None):
            capture_io = "redacted"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app_policy.py tests/test_capture_io.py -v`
Expected: PASS (clamp works; Phase A capture-io tests unaffected — they use the global path with no per-app override).

- [ ] **Step 5: Commit**

```bash
git add app/api/routes.py tests/test_app_policy.py
git commit -m "feat(obs): resolve capture_io per-app with a fail-closed full→redacted clamp (B14)"
```

---

### Task 3: Per-app-exporter raw-passthrough (make eligible `full` actually store raw)

**Files:**
- Modify: `app/obs/otel.py` (`RedactingSpanExporter.__init__`, `_redact`; `get_app_tracer`), `app/obs/export.py` (thread effective capture_io into the tracer selection)
- Test: `tests/test_capture_io.py`

**Interfaces:**
- Consumes: the effective (post-clamp) `capture_io` already passed to `export_trace` (Task 2).
- Produces: `RedactingSpanExporter(inner, skip_io: bool = False)`; `get_app_tracer(host, public_key, secret_key, capture_io_full: bool = False)` builds the per-app exporter with `skip_io=capture_io_full` and caches per `(public_key, capture_io_full)`. A full app's own project stores raw; the shared exporter (global path) is unchanged and always uses the module `_capture_io_full`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_capture_io.py`. An eligible full app (own project) stores raw via its per-app exporter, even though the process-global `_capture_io_full` is False:

```python
def test_per_app_full_stores_raw_via_own_exporter(monkeypatch):
    # Global flag OFF; the per-app exporter is built with skip_io=True, so an
    # eligible full app's OWN project keeps raw I/O while the shared path would not.
    monkeypatch.setattr(otel, "_capture_io_full", False)
    mem, provider = _wire(monkeypatch, redacting=True)

    # Route the per-app tracer's spans into our in-memory provider.
    monkeypatch.setattr(otel, "get_app_tracer",
                        lambda host, pub, sec, capture_io_full=False:
                        (otel._install_skip_io_probe(provider, capture_io_full)))
    # Simpler: assert at the exporter level instead (see _redact unit test below).
```

Because wiring a fake per-app tracer is fiddly, assert at the exporter unit level instead — replace the above with a direct `RedactingSpanExporter` test:

```python
def test_redacting_exporter_skip_io_keeps_raw_only_when_armed(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    _SSN = "482-73-1946"

    def _run(skip_io):
        mem = InMemorySpanExporter()
        prov = TracerProvider()
        prov.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem, skip_io=skip_io)))
        monkeypatch.setattr(otel, "_capture_io_full", False)  # global OFF
        with prov.get_tracer("t").start_as_current_span("serving.request") as s:
            s.set_attribute("input.value", f"raw {_SSN}")
        prov.force_flush()
        return next(iter(mem.get_finished_spans())).attributes["input.value"]

    assert _SSN in _run(skip_io=True)       # per-app full exporter: raw kept
    assert _SSN not in _run(skip_io=False)  # default exporter: raw scrubbed (defence in depth)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capture_io.py::test_redacting_exporter_skip_io_keeps_raw_only_when_armed -v`
Expected: FAIL — `RedactingSpanExporter.__init__()` takes no `skip_io` argument.

- [ ] **Step 3: Add per-instance `skip_io`**

`app/obs/otel.py` — give `RedactingSpanExporter` a `skip_io` param that ORs with the module global, and use it in `_redact`:

```python
    def __init__(self, inner, skip_io: bool = False) -> None:
        self._inner = inner
        self._skip_io = skip_io
```

In `_redact` (currently `skip = _IO_KEYS if _capture_io_full else frozenset()`):

```python
            # Per-app full exporters pass skip_io=True; the shared exporter falls
            # back to the process-global flag. Either way, non-I/O keys always redact.
            skip = _IO_KEYS if (self._skip_io or _capture_io_full) else frozenset()
```

`get_app_tracer` — thread the flag and include it in the cache key:

```python
def get_app_tracer(host: str, public_key: str, secret_key: str,
                   capture_io_full: bool = False):
    cache_key = (public_key, capture_io_full)
    if cache_key in _app_providers:
        p = _app_providers[cache_key]
        ...
    base = _build_exporter_kv(host, public_key, secret_key)
    exporter = RedactingSpanExporter(base, skip_io=capture_io_full)
    ...
    _app_providers[cache_key] = provider
```

(Adjust the existing `_app_providers[public_key]` lookups/writes in `get_app_tracer` to the `(public_key, capture_io_full)` tuple key. The global `get_tracer()`/shared `RedactingSpanExporter` construction is unchanged — it keeps `skip_io=False` and relies on the module `_capture_io_full`.)

`app/obs/export.py` — pass the effective capture_io through to the per-app tracer (the `export_trace` call in `routes.py` already passes the clamped `capture_io`):

```python
        if app_langfuse is not None:
            per_app = get_app_tracer(app_langfuse.host, app_langfuse.public_key,
                                     app_langfuse.secret_key,
                                     capture_io_full=(capture_io == "full"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_capture_io.py -v`
Expected: PASS (new exporter test green; `test_full_authored_raw_is_still_scrubbed_when_flag_off` still passes — default `skip_io=False` exporter still scrubs; all prior tests green).

- [ ] **Step 5: Commit**

```bash
git add app/obs/otel.py app/obs/export.py tests/test_capture_io.py
git commit -m "feat(obs): per-app Langfuse exporter passes raw I/O through only when that app is full (B14)"
```

---

### Task 4: Write-time residency lint on the policy PUT

**Files:**
- Modify: `app/api/admin_routes.py` (`set_policy`, ~lines 265-274)
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: `PolicyIn.capture_io` (Task 1), `load_app_langfuse` (`app/apps/langfuse_provision.py`), the app's residency.
- Produces: `PUT /admin/applications/{id}/policy` with `capture_io="full"` returns **422** unless the app is non-`onprem_only` AND has its own Langfuse project; otherwise `200`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_api.py` (reuse `_login`, the `client` fixture):

```python
def test_policy_full_capture_rejected_for_ineligible_app(client):
    _login(client)
    client.post("/admin/applications", json={"id": "onp", "name": "Onp",
                                             "default_residency": "onprem_only"})
    r = client.put("/admin/applications/onp/policy", json={"capture_io": "full"})
    assert r.status_code == 422
    assert "residency" in r.text.lower() or "project" in r.text.lower()
    # redacted is always allowed
    assert client.put("/admin/applications/onp/policy",
                      json={"capture_io": "redacted"}).status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_admin_api.py::test_policy_full_capture_rejected_for_ineligible_app -v`
Expected: FAIL — PUT returns 200 (no lint).

- [ ] **Step 3: Add the write-time lint**

In `app/api/admin_routes.py` `set_policy`, before the `upsert_policy` call (line ~271), after confirming the app exists:

```python
        app = await store.get_application_with_policy(session, app_id)
        if body.capture_io == "full":
            from app.apps.langfuse_provision import load_app_langfuse
            residency = (body.residency or app.policy.residency if app.policy else None) \
                or app.default_residency
            if residency == "onprem_only" or load_app_langfuse(app) is None:
                raise HTTPException(
                    422, "capture_io='full' stores raw telemetry and is allowed only "
                    "for a non-onprem application with its own provisioned Langfuse "
                    "project (residency beats eval; CLAUDE.md rule 6). Use 'redacted'.")
```

(Replace the existing `if await store.get_application(...) is None: 404` guard with the `get_application_with_policy` fetch above so the eligibility check has the policy + langfuse columns; keep the 404 when it returns None.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_admin_api.py tests/test_app_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_routes.py tests/test_admin_api.py
git commit -m "feat(admin): reject capture_io='full' for onprem / no-own-project apps at write time (B14)"
```

---

### Task 5: Per-app `capture_io` tri-state in the admin UI

**Files:**
- Modify: `ui/src/admin/ApplicationsPanel.tsx` (after the compression control, ~line 159)
- Verify: `cd ui && npm run typecheck`

**Interfaces:** consumes `AdminPolicy.capture_io` (Task 1) and `app.default_residency` / `app.langfuse_project_id` (already on `AdminApp`). No API-client change (the policy PUT spreads `p`).

- [ ] **Step 1: Add the control**

In `ui/src/admin/ApplicationsPanel.tsx`, mirror the Headroom tri-state (lines 143-159). After it, add — where `p` is the policy state and `app` the `AdminApp`:

```tsx
      {(() => {
        const fullDisabled =
          (p.residency ?? app.default_residency) === "onprem_only" ||
          !app.langfuse_project_id;
        return (
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500 w-20">eval capture</span>
            <select
              value={p.capture_io ?? ""}
              onChange={(e) => setP({ ...p, capture_io: e.target.value || null })}
              className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
              <option value="">(global default)</option>
              <option value="off">off</option>
              <option value="redacted">redacted</option>
              <option value="full" disabled={fullDisabled && p.capture_io !== "full"}>
                full{fullDisabled ? " — needs own Langfuse + non-onprem" : ""}
              </option>
            </select>
            <span className="text-[10px] text-gray-600">
              raw (full) is stored only in this app's own residency-local project
            </span>
          </div>
        );
      })()}
```

- [ ] **Step 2: Verify typecheck**

Run: `cd ui && npm run typecheck`
Expected: PASS. (Static UI; no component-test runner — typecheck + manual read is the verification, as in Phase A Task 3.)

- [ ] **Step 3: Commit**

```bash
git add ui/src/admin/ApplicationsPanel.tsx
git commit -m "feat(ui): per-app eval-capture tri-state with full gated by residency + own project (B14)"
```

---

## Self-Review

**Spec coverage (F1 spec Phase B):**
- New `AppPolicy.capture_io` column (+ manual ALTER noted) → Task 1. ✓
- `AppPolicyView` + store allowlist + PolicyIn + serializer → Task 1. ✓
- Per-request resolution threaded into `export_trace` → Task 2. ✓
- Per-app residency lint replacing the global one → **write-time** (Task 4) + **runtime clamp** (Task 2). The existing *global* boot lint (`app/config/lint.py:107`) stays for the global default and is untouched (an app inherits the global when its override is null). ✓
- Symmetric UI display / disabled `full` state → Task 5 (the display symmetry of request/response bodies is Phase C, per the spec). ✓

**Placeholder scan:** none — every step carries real code, paths, commands. The Task 3 Step-1 first draft is explicitly discarded in favor of the exporter-level unit test (kept the discard visible so the executor doesn't wire the fiddly fake tracer).

**Type consistency:** `capture_io: str | None` is identical across the column, `AppPolicyView`, `PolicyIn`, `_policy_dict`, `AdminPolicy` (TS `string | null`). `get_app_tracer(..., capture_io_full: bool)` and `RedactingSpanExporter(inner, skip_io: bool)` names/types match between Task 3's otel.py edits and the export.py call site.

**Fail-closed check:** three independent layers — write-time 422 (Task 4), runtime clamp (Task 2), and default-`skip_io=False` exporter that scrubs raw unless explicitly armed (Task 3). A stored `full` that becomes ineligible (residency change) is caught by the runtime clamp; raw can never reach the shared project because eligibility requires an own project and only the own exporter is armed.

**Note for executor:** `capture_io` is a POLICY field — all edits target `PolicyIn`/`PUT …/policy`/`upsert_policy`, never `UpdateAppIn`/`PATCH /applications/{id}`. Do not touch `app/config/lint.py`'s global check; the per-app lint is new code in `set_policy` + the runtime clamp.
