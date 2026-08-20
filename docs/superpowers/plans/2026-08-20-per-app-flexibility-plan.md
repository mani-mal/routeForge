# Plan — Per-application flexibility: `pii_detection` + streaming modes

**Design record:** `docs/architecture/per-app-config-flexibility.md` (read it first — it holds
the *why* and the governing rule). This plan implements Decision 1 (`pii_detection` per-app)
and Decision 2 (per-app streaming via `stream_mode`, all three modes).

**Template to mirror throughout:** the existing per-app `capture_io` override — schema column
→ `AppPolicyView` field → store whitelist → `PolicyIn` + write-time clamp → per-request
effective-value resolution. Copy its shape; do not invent a new one.

**Key correction to the code-map's inference:** `verified_then_stream` is post-hoc chunking of
the finished verified text (no backend streaming). But `stream_unverified` and
`incremental_verify` require **real backend token streaming** (litellm `stream=True`), because
their entire purpose is progressive output / low time-to-first-token (MillWise streams a
45–120 s generation). Do NOT implement those two as buffer-then-chunk.

---

## Global Constraints (bind every task)

1. **Fail-closed floors, enforced independently — the whole point of the design record:**
   - `pii_detection` override to `off` or `regex` is **rejected at admin write-time (422)** when
     the app's effective residency is `onprem_only` OR its effective `capture_io != "off"`
     (turning detection down while capture is on would write raw I/O to telemetry).
   - `stream_mode == "stream_unverified"` is **rejected at admin write-time (422)** when the
     app's effective residency is `onprem_only`.
   - **Runtime backstop:** `stream_unverified` and `incremental_verify` must NOT waive
     verification for a request whose `ctx.onprem_only` or `ctx.require_verification` is true —
     such a request **falls back to `verified_then_stream`** (still streams, but the complete
     response is verified). This backstop lives in the pipeline, in one place.
2. **Every request still emits a DecisionRecord/trace**, streaming included. Streaming modes
   record a verification entry stating whether verification was performed, waived, or
   incremental — the "why" must be visible in the console and Langfuse.
3. **Backwards compatible:** an app with `stream_mode` unset (NULL) keeps today's behavior —
   `stream: true` is rejected 400 when `server.reject_stream_requests` is true. Existing apps
   and tests are unaffected.
4. **The decision-stream SSE (`GET /api/stream`, admin-gated) is untouched.** It is a separate
   channel and never carries chat tokens. Do not modify `app/api/ui_routes.py`.
5. **Streaming applies to the direct (single-shot) path.** If a streaming request routes to
   `multi_step`, `stream_unverified`/`incremental_verify` fall back to `verified_then_stream`
   (buffer the whole agent result, verify, then chunk).
6. **No new dependencies.** Reuse `litellm`, the existing detectors, and the SSE frame pattern
   from `app/api/ui_routes.py:255-273` (`data: {json}\n\n`, terminal `data: [DONE]\n\n`).
7. **Conventions:** type-hint everything; Pydantic/enum validation at boundaries; structured
   values, no magic strings duplicated (use the canonical enum tuples from Task 1). New
   `AppPolicy` columns are additive and created by `create_all`; **no migration code** — the
   deploy note (Task 8) records the manual `ALTER` needed on already-deployed DBs (consistent
   with the project's no-migrations pattern).
8. **Canonical enum values (use verbatim):**
   - `PII_MODES = ("off", "regex", "presidio")`
   - `STREAM_MODES = ("off", "verified_then_stream", "stream_unverified", "incremental_verify")`
9. All work happens in the worktree `/home/manimal/projects/routeForge/.worktrees/per-app-flexibility`
   on branch `feat/per-app-flexibility`. Run tests there (`uv run pytest`). Each task commits.

---

## Task 1 — Schema: two `AppPolicy` columns + view + store + canonical enums

**Goal:** add `pii_detection` and `stream_mode` as nullable per-app override columns, threaded
through the ORM → view → store, plus the canonical enum tuples used by later tasks.

**Edits:**
- `app/apps/models.py` — in `AppPolicy` (class at L90), after `capture_io` (L115) add:
  ```python
  pii_detection: Mapped[str | None] = mapped_column(String(16), nullable=True)
  # null = inherit live global; one of PII_MODES (off|regex|presidio)
  stream_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
  # null = inherit (reject when server.reject_stream_requests); one of STREAM_MODES
  ```
- `app/apps/policy.py` — `AppPolicyView` (frozen dataclass, L22-33): add
  `pii_detection: str | None = None` and `stream_mode: str | None = None`. In
  `policy_view_from(app)` (L40-56) add the two passthrough lines (mirror `capture_io` L54-55).
- `app/apps/store.py` — `upsert_policy` (L235-247): add `"pii_detection"` and `"stream_mode"`
  to the `allowed` whitelist set (L241-243).
- `app/policy/enums.py` — add the two canonical tuples `PII_MODES` and `STREAM_MODES` exactly
  as in Global Constraint 8. (Read the file first; place them beside the existing canonical
  enums. If a PII-mode source already exists there, reuse/extend it rather than duplicating.)

**Tests:** extend the apps/store tests — round-trip an `AppPolicy` with `pii_detection="regex"`
and `stream_mode="stream_unverified"` through `upsert_policy` → reload → `policy_view_from`
carries both; NULL fields round-trip as `None`.

**Do NOT** touch admin validation (Task 2) or the pipeline (Task 3) here.

---

## Task 2 — Admin API: `PolicyIn` fields, write-time clamps, round-trip, deployment ceiling flag

**Goal:** accept and validate the two new fields on `PUT /admin/applications/{app_id}/policy`,
enforcing the fail-closed floors from Global Constraint 1, and add the deployment-ceiling
switch for PII downgrades.

**Edits — `app/api/admin_routes.py`:**
- `PolicyIn` (L92-100): add `pii_detection: str | None = None`, `stream_mode: str | None = None`.
- `_policy_dict(p)` (L38-44): add both fields (round-trip display).
- `set_policy` handler (L316-342), add validation after the residency check (L323-324),
  mirroring the `capture_io == "full"` clamp block (L325-333) for effective-value resolution
  (body → existing policy → `app.default_residency` for residency; body → existing → global
  `settings...capture_io` for capture):
  - `pii_detection`: if not None and not in `PII_MODES` → 422. If in `("off","regex")`:
    - effective residency `== "onprem_only"` → 422 `"pii_detection cannot be downgraded for onprem_only apps"`.
    - effective `capture_io != "off"` → 422 `"cannot reduce PII detection while capture_io is enabled"`.
    - deployment ceiling: if `settings.serving.policy` flag `allow_app_pii_downgrade` is false → 422 `"per-app PII downgrade disabled on this deployment"`.
  - `stream_mode`: if not None and not in `STREAM_MODES` → 422. If `== "stream_unverified"` and
    effective residency `== "onprem_only"` → 422 `"stream_unverified not allowed for onprem_only apps"`.
  - Get `settings` from `request.app.state.settings` (confirm how the handler currently reaches
    settings/global capture_io — the `full` clamp at L329-333 already resolves the langfuse
    project via `load_app_langfuse(app)`; follow the same access pattern).

**Edits — deployment ceiling flag (`allow_app_pii_downgrade`, default `true`):**
- `config/serving.yaml` — under `policy:` add `allow_app_pii_downgrade: true` with a one-line
  comment (a locked-down deployment sets false to forbid apps weakening detection).
- `app/config/schema.py` — add the bool to the `policy` settings model (near `pii_detection`
  L158 / `fail_closed`). 
- `app/config/loader.py` — load it (default `true` if absent, mirror L77-79). Optional env
  override `SP_ALLOW_APP_PII_DOWNGRADE` following the L48-53 pattern is nice-to-have, not required.
- Confirm the config lint (see `CLAUDE.md §Config lint`) still passes — this is a new field
  owned solely by `serving.yaml`, so no cross-file duplication.

**Tests:** admin route tests — (a) valid `pii_detection="regex"` on an `any`-residency app with
capture off → 200; (b) `pii_detection="off"` on an `onprem_only` app → 422; (c)
`pii_detection="off"` when `capture_io="redacted"` → 422; (d) `stream_mode="stream_unverified"`
on `onprem_only` → 422; (e) invalid enum value → 422; (f) `allow_app_pii_downgrade: false`
blocks a downgrade → 422; (g) round-trip GET shows the persisted values.

---

## Task 3 — Per-app `pii_detection` wiring in the pipeline + invariant tests

**Goal:** honor an app's `pii_detection` override by selecting the app's base detector at
request time (memoized), keeping the global instance as default.

**Edits — `app/pipeline.py`:**
- `Pipeline.__init__` (L30-57): add a memo `self._detectors: dict[str, Detector] = {settings.serving.pii_detection: self._detector}` and store `self._global_pii_mode = settings.serving.pii_detection`.
- Add `def _detector_for(self, mode: str | None) -> Detector`: if `mode is None or mode == self._global_pii_mode` → return `self._detector`; else return `self._detectors.get(mode)` or build via `make_detector(mode)` (import from `app.policy.detectors`), call `.warmup()`, cache, return. (Lazy build + memoize — Presidio is expensive; never build per request.)
- In `run()` (detector composition L89-97) and `_run_multi_step` (the analogous gate call): compute `base = self._detector_for(app_policy.pii_detection if app_policy else None)` and wrap the app-selected base instead of `self._detector`:
  - profile present: `det = EffectiveDetector(base, list(profile.recognizers))`
  - no profile: `det = base` (rules `None`)
  - Verify `det` still flows to the egress `verify()` calls (L138 single-shot, L224 multi-step) — the same `det` must be used for both intake and egress.

**Tests (`tests/`):**
- Invariant (the load-bearing one): an `onprem_only` `AppPolicyView` with `pii_detection="off"`
  still yields `ctx.onprem_only is True` and `allowed_providers` excludes cloud providers, and a
  `PERSON` in the prompt passes through **un-redacted** (proves detection-off cannot leak to
  cloud — residency floor is independent).
- `pii_detection="regex"`: `PERSON`/`DATE_TIME` not detected (pass through), `US_SSN` still
  detected+redacted.
- `pii_detection=None`: inherits the global detector (same instance via `_detector_for`).
- `_detector_for` memoizes (second call returns the same object).

---

## Task 4 — Streaming scaffolding + `verified_then_stream` mode

**Goal:** add OpenAI-style SSE emission and the stream-mode dispatch in the chat route, and
implement `verified_then_stream` (full verified pipeline, then chunk the final text). Set up the
dispatch structure Tasks 6–7 plug into.

**New file `app/api/streaming.py`:**
- `openai_chunk(request_id, model, *, delta=None, role=None, finish_reason=None) -> dict` —
  builds a `{"id", "object": "chat.completion.chunk", "created", "model", "choices":[{"index":0,"delta":{...},"finish_reason":...}]}` frame. (`created` must not use forbidden time calls in tests; use the resp's existing `created` if present, else `int(time.time())` in app code is fine — app code may call time; only workflow scripts can't.)
- `sse_frame(obj) -> str` → `f"data: {json.dumps(obj)}\n\n"`; `SSE_DONE = "data: [DONE]\n\n"`.
- `async def chunk_text(request_id, model, text, *, chunk_size=48) -> AsyncIterator[str]` —
  yields a role frame, then content frames splitting `text` (by ~`chunk_size` chars on word
  boundaries), then a `finish_reason="stop"` frame, then `SSE_DONE`.
- `def sse_response(agen) -> StreamingResponse` (media_type `text/event-stream`).

**Edits — `app/api/routes.py`:**
- Replace the reject guard (L90-91). Add a helper `_resolve_stream_mode(body, app_policy, s) -> str | None`:
  returns `None` for a normal (non-stream) request; for `body.stream`: `mode = app_policy.stream_mode`;
  if `mode in (None, "off")` and `s.serving.reject_stream_requests` → raise `HTTPException(400, "streaming is not supported for this application")` (backwards-compatible); else return `mode`.
  - NOTE: auth/`app_policy` currently resolves at L99, after the L90 guard. Move the stream-mode
    decision to **after** auth (so `app_policy` is available) but before the pipeline call. Keep a
    fast-path: if `body.stream` and there's genuinely no app policy, preserve the 400.
- **Refactor the post-model-call processing (L185-356) into a helper** `async def _finalize(request, resp, *, tenant, app_policy, app_langfuse, session_id, s, request_id) -> None` that does telemetry/`export_trace`, snapshot, usage record, and bus publish exactly as today. The non-stream path calls `_finalize` then `return resp` (behavior unchanged). This helper is reused by every streaming mode.
- **`verified_then_stream`:** run the pipeline fully (same `pipeline.run(...)` call), call `_finalize(...)` (records usage + decision), then `return sse_response(chunk_text(request_id, resp["model"], resp["choices"][0]["message"]["content"]))`. In this task, map `stream_unverified` and `incremental_verify` to `verified_then_stream` as a **safe interim fallback** (Tasks 6/7 replace the fallback with real streaming).

**Tests:** (a) app `stream_mode="verified_then_stream"` + `stream:true` → 200 `text/event-stream`; concatenated `delta`s equal the full non-stream answer; stream ends with `[DONE]`; a usage row is recorded. (b) app `stream_mode=None` + `stream:true` → 400 (backwards compat). (c) non-stream requests unchanged (regression).

---

## Task 5 — Real backend token streaming plumbing (`generate_stream` / `gateway` / `pipeline.run_stream`)

**Goal:** add a true streaming path from litellm through the gateway and pipeline that yields
token deltas and skips step-9 verify — the substrate Tasks 6–7 consume. No route changes here.

**Edits:**
- `app/gateway/backends/base.py` — add to the backend Protocol a streaming method and a small
  result type. Define `@dataclass class StreamChunk: delta: str | None = None; final: GenerationResult | None = None` (terminal chunk carries `final`). Add
  `async def generate_stream(self, model, messages, **kwargs) -> AsyncIterator[StreamChunk]: ...`.
- `app/gateway/backends/litellm_backend.py` — implement `generate_stream`: build the call like
  `generate()` (L62-77) but with `stream=True`, `stream_options={"include_usage": True}`, and
  **omit `logprobs`** (unavailable while streaming; confidence will be `None`). `async for chunk
  in await acompletion(**call)`: pull `chunk.choices[0].delta.content` (may be None/empty — skip
  empties), `yield StreamChunk(delta=content)`; accumulate text + capture `chunk.usage` from the
  final chunk. After the loop, compute cost from pricing (same source `generate()` uses — pricing
  from `pricing.yaml`, never `completion_cost`) and `yield StreamChunk(final=GenerationResult(model, text, tokens_in, tokens_out, latency_ms, cost_usd, confidence=None, tool_calls=[]))`.
- `app/gateway/gateway.py` — add `async def run_stream(self, ctx, messages, tokens_after, complexity, hint) -> tuple[AsyncIterator[StreamChunk], list[DecisionRecord]]` (or an async generator that also exposes records): perform the SAME routing/candidate selection as `run()` (read `run()` to mirror scoring + candidate filtering + records), then delegate to `backend.generate_stream` on the chosen candidate. Emit the routing DecisionRecords (chosen + rejected `sp.candidates`) exactly as `run()` does. Do NOT add a verify record.
- `app/pipeline.py` — add `async def run_stream(self, request_id, tenant_id, request, app_policy=None, profile=None)`:
  - Run the gate with the per-app detector (reuse Task 3's `_detector_for` composition).
  - `mode = decide_mode(ctx, request)`; if `mode == "multi_step"` → raise a dedicated
    `StreamFallback` exception (define it in pipeline) — the route will fall back to
    `verified_then_stream`.
  - **Runtime floor (Global Constraint 1):** if `ctx.onprem_only or ctx.require_verification` →
    raise `StreamFallback` (so the caller verifies the complete response).
  - Else: compress, call `gateway.run_stream`, and yield token deltas (strings). Accumulate the
    full text + final `GenerationResult`. Expose a way for the caller to retrieve the final
    metadata dict (records, cost via the two-lever accounting, tokens, model, ctx,
    `sanitized_prompt`, `raw_completion`, and a **waived**/incremental verification marker) — e.g.
    `run_stream` is an async generator yielding `str` deltas and stashing the final dict on a
    passed-in mutable holder, or returns `(agen, get_meta)`. Choose one and document it; keep it
    testable.

**Tests:** extend `tests/_litellm_fake.py` with a streaming fake (yields deltas + a final usage
chunk). Assert `pipeline.run_stream` on a benign request yields deltas in order, never calls
`verify`, and produces final metadata with a non-zero cost and a verification marker of "waived".
Assert `StreamFallback` is raised when `ctx.require_verification` is true and when the request
routes multi-step.

---

## Task 6 — `stream_unverified` route wiring + runtime floor fallback

**Goal:** wire the real streaming substrate into the route for `stream_unverified`, with the
runtime floor falling back to `verified_then_stream`, and finalize telemetry/usage after the
stream.

**Edits — `app/api/routes.py`:**
- In the stream dispatch (Task 4 structure), for `stream_unverified`:
  - Call `pipeline.run_stream(...)`. Wrap it so that on `StreamFallback` the route transparently
    falls back to the `verified_then_stream` path (full verified pipeline, then `chunk_text`).
  - On the streaming path: build a `StreamingResponse` whose generator (a) emits a role frame,
    (b) forwards each delta as an `openai_chunk(delta=...)` frame, (c) on completion emits the
    finish frame + `SSE_DONE`, then (d) calls `_finalize(...)` with a `resp` dict assembled from
    the accumulated text + final metadata, verification recorded as **waived**
    (`VerificationRecord(required=?, performed=False, passed=True, reason="waived: stream_unverified")`),
    so the DecisionRecord, usage row, and bus publish still happen (Global Constraint 2).
  - Ensure `_finalize` runs even if the client disconnects mid-stream (best-effort in a
    `finally`), so usage/decision are not lost.

**Tests:** (a) app `stream_mode="stream_unverified"`, benign prompt → real streamed deltas (use
the streaming fake), no verify performed, decision trace/usage row shows verification waived.
(b) same app, prompt containing a restricted entity that sets `require_verification` → falls back
to `verified_then_stream`: verify **is** performed, output still streamed. (c) request that routes
multi-step → falls back to `verified_then_stream`.

---

## Task 7 — `incremental_verify` mode (hold-back streaming verifier)

**Goal:** stream real tokens but never release un-verified text — buffer a hold-back window,
run leakage detection on the buffer, and release only the redacted safe prefix.

**New file `app/output/streaming_verify.py`:**
- `class StreamingVerifier` built from `(detector, ctx)` with `HOLDBACK_CHARS = 48`:
  - `feed(delta: str) -> str`: append to an internal buffer; run `detector.analyze(buffer)`;
    compute `safe = len(buffer) - HOLDBACK_CHARS`; do not release past any entity that extends
    beyond `safe` (an entity still forming at the boundary stays buffered); redact (via
    `detector.anonymize`) entities fully inside the releasable region; return the redacted safe
    prefix and retain the remainder in the buffer.
  - `flush() -> str`: analyze + redact the entire remaining buffer and return it.
- Document the limitation in the module docstring and the design doc (Task 8): incremental
  verification protects against **entity leakage** across chunk boundaries; a holistic
  whole-response verdict (e.g. the `UNVERIFIABLE` marker in `app/output/verify.py:19-24`) cannot
  be enforced once tokens have streamed — record it in the decision trace as "incremental".

**Edits — `app/api/routes.py`:** for `incremental_verify`, use `pipeline.run_stream` (real
streaming, same `StreamFallback` handling as Task 6), but pipe every delta through
`StreamingVerifier.feed(...)` before emitting, and emit `flush()` at the end. Finalize with a
verification record `reason="incremental"`, `performed=True`.

**Tests:** (a) deltas that split a US SSN across a chunk boundary (`"my ssn is 123-45-"`,
`"6789 ok"`) → the concatenated **released** stream never contains the raw SSN; it appears as
`<US_SSN_1>`. (b) a benign stream is released in order (accounting for the hold-back delay) and
`flush` releases the tail. (c) an entity fully inside one delta is redacted.

---

## Task 8 — Docs, config, and the CLAUDE.md streaming-rule update

**Goal:** make the decision authoritative and consistent with the code, and record the deploy
delta. (Per the user's explicit direction, the "No inbound streaming" stance in CLAUDE.md is
being changed — this task updates it.)

**Edits:**
- `docs/architecture/per-app-config-flexibility.md` — flip Status/next-steps to *implemented*;
  correct the "post-hoc chunking" framing (verified_then_stream chunks; the other two do real
  backend streaming); document the `incremental_verify` holistic-verdict limitation; note the
  `allow_app_pii_downgrade` ceiling flag.
- `CLAUDE.md` — update the **"No inbound streaming"** paragraph (§Architecture): inbound
  `stream: true` is still rejected by default (`server.reject_stream_requests`) and for apps with
  no `stream_mode`; describe the per-app `stream_mode` (`off | verified_then_stream |
  stream_unverified | incremental_verify`), the fail-closed floor (onprem_only /
  require_verification can never waive step-9 verification — they fall back to
  `verified_then_stream`), and that the decision-stream SSE is still a separate channel. Keep it
  tight and consistent with the rest of the rules.
- A short operator how-to: add a section to `docs/onboarding-models.md` **or** a new
  `docs/per-app-config.md` covering the two new per-app knobs, the floors, and the manual
  `ALTER TABLE app_policies ADD COLUMN pii_detection ... / stream_mode ...` needed on
  already-deployed SQLite/Postgres DBs (no migration tool — cite the no-migrations pattern).
- If any new env var was added (e.g. `SP_ALLOW_APP_PII_DOWNGRADE`), add it to `.env.example`.

**Tests:** none (docs/config). Re-run the full suite to confirm the config lint still boots and
nothing regressed.

---

## Suggested model per task (Model Selection)

| Task | Nature | Model |
|---|---|---|
| 1 | mechanical, multi-file, clear spec | sonnet |
| 2 | validation judgment | sonnet |
| 3 | integration + invariant tests | sonnet |
| 4 | integration, new SSE code | sonnet |
| 5 | subtle async streaming plumbing | opus |
| 6 | streaming integration + floor | opus |
| 7 | correctness-critical buffer logic | opus |
| 8 | docs/config | sonnet |

Final whole-branch review: opus.
</content>
