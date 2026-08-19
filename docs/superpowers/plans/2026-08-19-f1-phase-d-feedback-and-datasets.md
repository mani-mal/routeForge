# F1 Phase D — Feedback API + Gated Raw-Completion Capture + Dataset Substrate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the F1 capture substrate: capture pre-verification raw completions into full-eligible apps' own residency-local projects, expose trace ids, add a trace-linked feedback API (+ admin UI), and document the dataset-export path — the inputs the external RLHF repo consumes.

**Architecture:** Five moving parts. (1) The pipeline surfaces the pre-verify raw completion; routes.py pops it and threads it into `export_trace` only, so it lands solely in the app's residency-local Langfuse project. (2) `export_trace` also returns the hex trace id, set on the chat response. (3) A `post_score` helper posts trace-linked scores to Langfuse via REST using the app's own keys. (4) Two endpoints (`POST /v1/feedback` app-token; `POST /api/requests/{id}/feedback` admin) sit over that helper. (5) UI thumbs + correction form, and dataset-export docs.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Pydantic v2 / httpx; React + TS. No new deps (httpx + langfuse already present; we use Langfuse REST, not the SDK).

## Global Constraints

- **Raw completion isolation (the load-bearing privacy property).** `resp["raw_completion"]` (pre-verify `result.text`) must be **popped in routes.py before the client response**, must **never** appear in `resp["display"]`, `HistoryStore`, the shared exporter, or any `redacted`/`off` path. It reaches Langfuse **only** via `export_trace`'s `full` branch — which the Phase-B clamp + per-app `skip_io` exporter already restrict to a non-`onprem_only` app with its **own residency-local project**. The client reply (`resp["choices"]`) and the admin `display` stay masked (step-9 verified). Rule #2 is not relaxed for shared sinks or the client.
- **Scores fail closed.** `post_score` with `app_langfuse is None` must **drop + log**, never POST to a shared/default project. Scores are project-scoped by the creds that post them.
- **Auth boundaries.** `/v1/feedback` is **app-token** authed (reuse `_authenticate`, like `/v1/chat/completions`). `/api/requests/{id}/feedback` is **admin/session** gated (`Depends(require_admin)`), for the console. Never mix them.
- **Score-name enum** (validated server-side, 422 on unknown): `route_correct | preferred_model | failure_type | quality | corrected_output`. No other names accepted.
- **Telemetry never breaks serving.** Raw-completion threading and trace-id exposure must be swallowed/guarded exactly like the existing `export_trace` block (`app/api/routes.py`, `except Exception` around it).
- **Canonical values unchanged.** `capture_io ∈ off|redacted|full`; provider `onprem`; tiers `cheap|mid|frontier`; sensitivity enum single-sourced. No config-file ownership added.
- **UI has no test framework** — UI tasks gate on a clean `cd ui && npm run build`.
- **Langfuse REST target:** scores → `POST {host}/api/public/scores`, Basic auth `(public_key, secret_key)`; score configs → `POST {host}/api/public/score-configs`. Verify the exact body against langfuse>=3.0; tests mock httpx (follow `tests/test_langfuse_provision.py`'s `httpx.MockTransport` pattern), so they assert our request shape, not a live server.

---

### Task 1: Gated raw-completion capture (pre-verification → residency-local only)

**Files:**
- Modify: `app/pipeline.py` (direct path ~`:138,181-203`; multi-step ~`:221-223,277-302`)
- Modify: `app/api/routes.py` (~`:203-236`)
- Modify: `app/obs/export.py` (`export_trace` signature; `_set_io` `:37-65`)
- Test: `tests/test_raw_completion_capture.py` (create)

**Interfaces:**
- Produces: `resp["raw_completion"]: str` (pre-verify `result.text`) from both pipeline paths. `export_trace(..., raw_completion: str = "")`; `_set_io(..., raw_completion: str = "")` uses it for `output.value` **only** when `capture_io == "full"` (else the masked `completion`, unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_raw_completion_capture.py
# Proves: full-eligible app -> raw (pre-verify) completion in export output.value,
# but the client response + display stay masked, and raw_completion is never
# client-visible. Uses an echo backend that leaks a PII entity into the completion.
from tests.test_capture_io import _wire  # span-capture harness (InMemorySpanExporter)

# See tests/test_request_display.py for the _echo_backend monkeypatch pattern and
# tests/test_capture_io.py::test_full_* for how a full-eligible app is wired.

def test_full_export_stores_pre_verify_raw_completion(full_eligible_app, echo_backend, spans):
    # echo backend makes the completion contain "482-73-1946" (an SSN)
    resp = full_eligible_app.chat("please repeat: 482-73-1946")
    # client reply is MASKED (step-9 verified)
    assert "482-73-1946" not in resp["choices"][0]["message"]["content"]
    # raw_completion is NOT in the client response (popped)
    assert "raw_completion" not in resp
    # display stays masked (Phase C honesty)
    assert "482-73-1946" not in (resp["display"]["output"] or "")
    # BUT the app's own residency-local export output.value has the RAW completion
    out = spans.output_value_for(resp["id"])
    assert "482-73-1946" in out

def test_redacted_export_never_gets_raw_completion(redacted_app, echo_backend, spans):
    resp = redacted_app.chat("please repeat: 482-73-1946")
    out = spans.output_value_for(resp["id"])
    assert "482-73-1946" not in out  # masked in redacted mode
```

(Adapt the fixtures to the real harness in `tests/test_capture_io.py` / `tests/test_request_display.py`. The essential assertions: raw present in `full` export `output.value`; absent from client `choices`, `resp["raw_completion"]` (must not exist), `display.output`, and any `redacted` export.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_raw_completion_capture.py -v`
Expected: FAIL — export `output.value` is masked (raw not threaded yet).

- [ ] **Step 3: Surface pre-verify raw completion from the pipeline**

In `app/pipeline.py`, direct path (where `text, vrec = verify(det, ctx, result.text)`), add to the return dict: `"raw_completion": result.text`. Same in the multi-step path (its `result.text` before the return dict). `result.text` is the pre-verify raw completion in both.

- [ ] **Step 4: Pop + thread it in routes.py**

In `app/api/routes.py`, right after `sanitized_prompt = resp.pop("sanitized_prompt", "")`, add:

```python
        raw_completion = resp.pop("raw_completion", "")   # pre-verify; NEVER client-visible
```

Pass it into the `export_trace(...)` call: add `raw_completion=raw_completion,`. Do **not** use it in the `display` block (display stays masked — Phase C honesty). Because it's popped, it's absent from the returned `resp` and from `history.record(resp, ...)`.

- [ ] **Step 5: Use raw completion only on the `full` branch of `_set_io`**

In `app/obs/export.py`: add `raw_completion: str = ""` to `export_trace`'s signature and pass it to `_set_io`. In `_set_io`, change the `full` handling so `output.value` uses the raw completion when present:

```python
def _set_io(root, capture_io, prompt, completion, recognizers=None,
            sanitized_prompt="", raw_completion=""):
    if capture_io == "off":
        return
    if capture_io == "redacted":
        prompt = sanitized_prompt or redact(prompt, recognizers=recognizers)
        completion = redact(completion, recognizers=recognizers)
    elif capture_io == "full":
        # Residency-local, own-project only (Phase-B clamp + skip_io exporter).
        # Prefer the pre-verify RAW completion; fall back to whatever was passed.
        completion = raw_completion or completion
    if prompt:
        root.set_attribute("input.value", prompt)
    if completion:
        root.set_attribute("output.value", completion)
```

- [ ] **Step 6: Run to verify it passes + regression**

Run: `uv run pytest tests/test_raw_completion_capture.py tests/test_capture_io.py tests/test_request_display.py tests/test_output_controls.py -v`
Expected: PASS (raw only in full export; masked everywhere else; existing capture_io/display/verify tests still green).

- [ ] **Step 7: Commit**

```bash
git add app/pipeline.py app/api/routes.py app/obs/export.py tests/test_raw_completion_capture.py
git commit -m "feat(capture): gated pre-verify raw-completion capture into residency-local project (F1 Phase D)"
```

---

### Task 2: Expose the Langfuse trace id on the chat response

**Files:**
- Modify: `app/obs/export.py` (`export_trace` return; trace-id extraction `:148-153`)
- Modify: `app/api/routes.py` (set `resp["trace_id"]`)
- Test: `tests/test_trace_id_exposed.py` (create)

**Interfaces:**
- Produces: `export_trace(...)` returns a `TraceExport(url: str | None, trace_id: str | None)` (namedtuple), where `trace_id` is the 32-hex id (`f"{trace_id:032x}"`); `None` when telemetry is off. `routes.py` sets `resp["trace_id"] = te.trace_id` and `resp["langfuse_url"] = te.url`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_id_exposed.py
def test_chat_response_includes_trace_id(client_app_with_telemetry):
    resp = client_app_with_telemetry.chat("hello")
    assert resp.get("trace_id") and len(resp["trace_id"]) == 32
    int(resp["trace_id"], 16)  # is hex

def test_trace_id_absent_when_telemetry_off(client_app_no_telemetry):
    resp = client_app_no_telemetry.chat("hello")
    assert resp.get("trace_id") in (None, "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_trace_id_exposed.py -v` → FAIL (`trace_id` not on resp).

- [ ] **Step 3: Return the hex trace id from `export_trace`**

In `app/obs/export.py`, define near the top: `from collections import namedtuple` and `TraceExport = namedtuple("TraceExport", ["url", "trace_id"])`. In the trace-id block (`:148-153`), build `hex_id = f"{trace_id:032x}"` and return `TraceExport(url, hex_id)` (both the app-project and default-project branches). Return `TraceExport(None, None)` on the telemetry-off / exception paths (replace the bare `return None`).

- [ ] **Step 4: Set it on the response in routes.py**

Where `lf_url = export_trace(...)` is assigned, change to `te = export_trace(...)`, then:

```python
        if te.url:
            resp["langfuse_url"] = te.url
        if te.trace_id:
            resp["trace_id"] = te.trace_id
```

(Keep this inside the existing `try/except` guard.)

- [ ] **Step 5: Run to verify it passes + regression**

Run: `uv run pytest tests/test_trace_id_exposed.py tests/test_langfuse_deeplink.py -v`
Expected: PASS (update `test_langfuse_deeplink.py` call sites if they destructured the old return — it now returns a namedtuple; `te.url` is the old value).

- [ ] **Step 6: Commit**

```bash
git add app/obs/export.py app/api/routes.py tests/test_trace_id_exposed.py
git commit -m "feat(obs): expose hex trace_id on chat response for feedback linkage (F1 Phase D)"
```

---

### Task 3: `post_score` Langfuse-REST helper + `POST /v1/feedback` (app-token)

**Files:**
- Create: `app/obs/scores.py`
- Modify: `app/api/routes.py` (add `POST /v1/feedback`) OR create `app/api/feedback_routes.py` and register it in the app factory
- Test: `tests/test_feedback_api.py` (create)

**Interfaces:**
- Produces: `async def post_score(app_langfuse, trace_id, name, value, comment=None) -> bool` — posts to `{host}/api/public/scores` with Basic auth; returns `True` on 2xx, `False` (drop + log) when `app_langfuse is None` or on non-2xx. `SCORE_NAMES: frozenset` = the enum. `POST /v1/feedback` body `FeedbackIn{trace_id: str, score_name: str, value: float | str, comment: str | None, corrected_output: str | None}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_feedback_api.py
from tests.test_admin_api import _login  # noqa: F401

def test_feedback_posts_score_to_app_project(client, monkeypatch):
    # create app WITH its own langfuse project (manual creds path from Phase C),
    # capture the httpx POST via monkeypatch/MockTransport (see test_langfuse_provision.py),
    # then POST /v1/feedback and assert the score hit {host}/api/public/scores with the
    # app's public_key basic-auth and body {traceId, name:"route_correct", value:1}.
    ...

def test_feedback_unknown_score_name_422(client):
    app_id, token = _make_app(client)
    r = client.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                    json={"trace_id": "a"*32, "score_name": "bogus", "value": 1})
    assert r.status_code == 422

def test_feedback_no_project_fails_closed(client):
    # app WITHOUT a langfuse project -> score dropped, endpoint returns 200 {stored: false}
    app_id, token = _make_app(client)  # no manual creds
    r = client.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                    json={"trace_id": "a"*32, "score_name": "route_correct", "value": 1})
    assert r.status_code == 200 and r.json()["stored"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_feedback_api.py -v` → FAIL (404 / module missing).

- [ ] **Step 3: Implement `post_score`**

`app/obs/scores.py`: build an `httpx.AsyncClient` (or accept an injected one, mirroring `LangfuseProvisioner` in `app/apps/langfuse_provision.py` for testability), POST `{app_langfuse.host}/api/public/scores` with `auth=(app_langfuse.public_key, app_langfuse.secret_key)` and JSON `{"traceId": trace_id, "name": name, "value": value, "comment": comment}` (use `stringValue`/`dataType="CATEGORICAL"` when `value` is a str; `dataType="NUMERIC"` otherwise — verify against langfuse>=3.0). Return `True` on 2xx. If `app_langfuse is None`: `log.info("feedback dropped: no project for trace %s", trace_id); return False`. Wrap httpx errors → log + `False`. Define `SCORE_NAMES = frozenset({"route_correct","preferred_model","failure_type","quality","corrected_output"})`.

- [ ] **Step 4: Implement `POST /v1/feedback`**

Reuse `_authenticate` (app token → `(tenant, policy, app_langfuse)`). Body `FeedbackIn`. Validate `score_name in SCORE_NAMES` (else `HTTPException(422)`). Call `stored = await post_score(app_langfuse, body.trace_id, body.score_name, body.value, body.comment)`. If `body.corrected_output`, also `await post_score(app_langfuse, body.trace_id, "corrected_output", 1, body.corrected_output)`. Return `{"stored": stored}`. Fail-closed: never post when `app_langfuse is None` (helper handles it → `stored=False`).

- [ ] **Step 5: Run to verify + regression**

Run: `uv run pytest tests/test_feedback_api.py tests/test_admin_api.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/obs/scores.py app/api/*.py tests/test_feedback_api.py
git commit -m "feat(feedback): POST /v1/feedback + Langfuse-REST post_score helper, fail-closed (F1 Phase D / B16)"
```

---

### Task 4: Admin-gated feedback route + lazy score-config

**Files:**
- Modify: `app/api/admin_routes.py` (`POST /api/requests/{request_id}/feedback`)
- Modify: `app/obs/scores.py` (add `ensure_score_config`)
- Test: `tests/test_admin_feedback.py` (create)

**Interfaces:**
- Consumes: `post_score` (Task 3); `HistoryStore.get(request_id)` → stored `resp` (has `trace_id` from Task 2); the request's tenant → `store.get_application` → `load_app_langfuse`.
- Produces: `POST /api/requests/{request_id}/feedback` body `{score_name, value, comment?, corrected_output?}` (admin session). `ensure_score_config(app_langfuse, name)` best-effort lazy `POST /api/public/score-configs` (non-fatal).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_feedback.py
from tests.test_admin_api import _login

def test_admin_feedback_posts_for_known_request(client, monkeypatch):
    # seed a chat request (creates a history entry with trace_id), capture httpx,
    # then admin POST /api/requests/{rid}/feedback and assert post_score was called
    # with that request's trace_id and the app's creds.
    ...

def test_admin_feedback_unknown_request_404(client):
    _login(client)
    r = client.post("/api/requests/req-deadbeef/feedback",
                    json={"score_name": "route_correct", "value": 1})
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify they fail** → 404/route missing.

- [ ] **Step 3: Implement the admin route**

`Depends(require_admin)`. Look up `resp = request.app.state.history.get(request_id)`; `if resp is None: raise HTTPException(404)`. Get `trace_id = resp.get("trace_id")` (404 or `{"stored": false, "reason": "no trace"}` if absent). Resolve the app: the request's tenant is `resp`'s application; fetch it via `store.get_application(session, tenant)` and `load_app_langfuse(app)`. Validate `score_name`. Call the same `post_score` (+ `corrected_output`). Best-effort `ensure_score_config` first. Return `{"stored": bool}`.

- [ ] **Step 4: Implement `ensure_score_config`** (best-effort)

`app/obs/scores.py`: `async def ensure_score_config(app_langfuse, name) -> None` — POST `{host}/api/public/score-configs` `{"name": name, "dataType": ...}`; swallow all errors (log at debug). Never raises. Call it before `post_score` in both feedback routes (or once, guarded).

- [ ] **Step 5: Run to verify + regression**

Run: `uv run pytest tests/test_admin_feedback.py tests/test_feedback_api.py tests/test_admin_api.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/admin_routes.py app/obs/scores.py tests/test_admin_feedback.py
git commit -m "feat(feedback): admin console feedback route + lazy score-config (F1 Phase D / B16)"
```

---

### Task 5: UI — thumbs + reviewer correction form on the drilldown

**Files:**
- Modify: `ui/src/api.ts` (add `api.feedback(requestId, body)`)
- Modify: `ui/src/console/RequestDrilldown.tsx`
- Verify: `cd ui && npm run build`

**Interfaces:**
- Consumes: `POST /api/requests/{id}/feedback` (admin, same-origin cookie — mirror `api.request(id)`'s `credentials: "same-origin"`).
- Produces: `api.feedback(requestId, {score_name, value, comment?, corrected_output?})`; a thumbs up/down control (posts `route_correct` value 1/0) + a collapsible reviewer correction form (`corrected_output` textarea + optional `preferred_model` → posts `preferred_model` categorical) on the drilldown.

- [ ] **Step 1: Add the client function**

In `ui/src/api.ts`, add to the `api` object (same-origin, like `request`):
```ts
feedback: (requestId: string, body: { score_name: string; value: number | string;
           comment?: string; corrected_output?: string }) =>
  fetch(`/api/requests/${requestId}/feedback`, { method: "POST",
    credentials: "same-origin", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then(r => r.json()),
```

- [ ] **Step 2: Add the thumbs control** — in `RequestDrilldown.tsx` `PlainSummary` (the verify/outcome region), render 👍/👎 buttons that call `api.feedback(detail.id, {score_name:"route_correct", value: up?1:0})`, with a transient "thanks" state. Disable when `!detail?.id`.

- [ ] **Step 3: Add the correction form** — a collapsible section (in `TechnicalTrace`, near the Langfuse link) with a `corrected_output` textarea + a `preferred_model` text input; on submit call `api.feedback(detail.id, {score_name:"preferred_model", value: model, corrected_output: text})` (and/or a `quality` score). Show a saved indicator.

- [ ] **Step 4: Build** — `cd ui && npm run build` (rtk proxy if the hook mangles it). Clean tsc.

- [ ] **Step 5: Commit**

```bash
git add ui/src/api.ts ui/src/console/RequestDrilldown.tsx
git commit -m "feat(console): reviewer feedback (thumbs + correction form) on request drilldown (F1 Phase D / B16)"
```

---

### Task 6: Dataset-export readiness docs

**Files:**
- Modify: `docs/rlhf-readiness.md`
- Create: `docs/help/exporting-eval-datasets.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Document the transform** — In `docs/help/exporting-eval-datasets.md`: the trace → Langfuse dataset → external TRL `{prompt, chosen, rejected}` mapping. `prompt` = trace `input.value` (raw for `full`-eligible apps after Task 1); `rejected` = trace `output.value` (raw model completion, now captured); `chosen` = the `corrected_output` score's comment (human correction). Scores (`route_correct`, `quality`, `preferred_model`, `failure_type`) become filter/label columns. Note Target B (SFT/DPO) needs `full` + residency-local, and that the raw completion is now captured (Task 1) — resolving the prior gap.

- [ ] **Step 2: Update `rlhf-readiness.md`** — mark the three prior gaps (candidate snapshot, policy version, feedback API) plus the raw-completion gap as addressed by 0.11.0 + Phase D; point to the new doc and the `x-routeforge-session` grouping (`docs/help/integrating-an-app.md` §7).

- [ ] **Step 3: Commit**

```bash
git add docs/rlhf-readiness.md docs/help/exporting-eval-datasets.md
git commit -m "docs(rlhf): dataset-export readiness — trace->TRL transform, raw-completion gap closed (F1 Phase D)"
```

---

## Self-Review

**Spec coverage** (spec §Phase D + build addendum):
- Gated raw-completion capture → Task 1. ✅
- Trace id on chat response → Task 2. ✅
- `POST /v1/feedback` trace-linked score, app project, fail-closed → Task 3. ✅
- Named score configs (lazy) + admin UI route → Task 4. ✅
- UI thumbs + correction form → Task 5. ✅
- Dataset-export readiness → Task 6. ✅

**Privacy check:** raw_completion popped before client response (Task 1 Step 4), absent from display/history/redacted/shared, only in `full` export branch (Step 5) which is residency-local by the Phase-B clamp. Scores fail closed on no-project (Task 3 Step 3). `/v1/feedback` app-token, admin route session-gated (separate). Score-name enum validated.

**Type consistency:** `TraceExport(url, trace_id)` used in export.py + routes.py (Task 2). `post_score`/`SCORE_NAMES` shared by Tasks 3 & 4. `FeedbackIn` fields ↔ `api.feedback` body ↔ admin route body (Tasks 3/4/5). `raw_completion` threaded pipeline → routes → export_trace → _set_io (Task 1).

**Sequencing:** 1 (raw capture) → 2 (trace id) → 3 (helper + app route) → 4 (admin route, needs 2's trace_id in history + 3's helper) → 5 (UI, needs 4) → 6 (docs). Order respected.
