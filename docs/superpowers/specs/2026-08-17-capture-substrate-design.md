# Design spec — Langfuse capture substrate (B13 → B16)

**Date:** 2026-08-17
**Status:** design, pending owner review
**Program roadmap:** `docs/superpowers/specs/2026-08-18-self-improving-program-roadmap.md`
(this is **F1**, the trunk)
**Scope decisions upstream:** `docs/architecture/self-improving-agent-feature-fit.md`
**Backlog write-ups:** `docs/bugs-backlog.md` B13–B16; `docs/rlhf-readiness.md`;
`docs/eval-logging-backlog.md`
**Boundary:** control-plane, orchestrated-not-hosted (CLAUDE.md rule #4, amended 2026-08-17).
This spec captures + exports data and adds a feedback endpoint. It **trains nothing** and
adds no orchestration hook (that is a later slice).

---

## 1. Problem & goal

RouteForge's Langfuse export is the eval/RLHF substrate, but four gaps block it from being
trustworthy and per-tenant safe:

- **B13** — `redacted` telemetry is *weaker than the gate*: `app/api/routes.py:195` hands the
  **raw** request body to `export_trace`, which independently re-redacts the compact JSON and
  under-detects, while the gate's already-sanitized text is discarded (`app/pipeline.py:95`
  computes `sanitized, _map` then drops the map). The UI also claims the prompt is "never
  stored," which is false when `capture_io != off`.
- **B14** — `capture_io` is a single **global** switch (`serving.observability.langfuse` +
  `SP_CAPTURE_IO`). It forces all-or-nothing across tenants and makes `full` a boot failure
  whenever *any* tenant is `onprem_only`. A multi-tenant product needs it **per-application**.
- **B15 (remainder)** — per-app Langfuse **project isolation is already built** (auto-provision
  on app create, `app/apps/langfuse_provision.py`). What's missing: **admin cross-app
  reporting** and a **manual per-app-creds path** for air-gapped operators with no org key.
- **B16 (gap 3)** — no **feedback ingestion**. Gaps 1 & 2 (candidate snapshot,
  `routing_policy_version`) shipped in 0.11.0. Missing: `POST /v1/feedback` → trace-linked
  Langfuse score, a reviewer correction-output form, named score configs, and dataset-export
  readiness.

**Goal:** make Langfuse a per-tenant-safe substrate where (a) `redacted` is never weaker than
what the model saw, (b) each app chooses its own capture mode under a strict residency lint,
(c) raw `full` is only ever stored per-app residency-local, (d) router metadata is always-on
regardless of mode, and (e) human/reviewer feedback attaches to traces for external router/LLM
improvement.

**Non-goals (explicit):** training, reward modelling, auto-promotion, the orchestration hook,
DSPy, and the memory module. All are later slices or out of scope.

---

## 2. Invariants (must hold at every phase)

1. **Metadata always-on.** `sp.candidates`, `sp.routing_policy_version`, `sp.reason`, feedback
   scores, cost/model/sensitivity/entity-**types** are emitted **regardless of `capture_io`** —
   they are not PII and are the router-training signal (rules unaffected; already true for the
   `sp.*` set).
2. **`full` is residency-bound.** Raw prompt/completion may be stored **only** when the app has
   its **own provisioned residency-local project** AND the app is **not `onprem_only`** and
   cannot carry `restricted` traffic. Otherwise the save is rejected and the runtime fails
   closed to `redacted` (never raw). (Rules #2/#6/#8 — residency beats eval.)
3. **`redacted` ≥ gate.** Telemetry redaction reuses the gate's sanitized text + map; it can
   never reveal an entity the model didn't see.
4. **Fail closed.** Missing/invalid/unknown `capture_io` → most-restrictive (`off`).
5. **Display = export, modelled separately.** The per-app tri-state governs BOTH the routeForge
   UI display and the Langfuse export, but they are two concepts in the schema so a later split
   (UI raw / Langfuse redacted) needs no rewrite. The residency lint binds the **export/full**
   side only (first-party UI view is a lighter risk).

---

## 3. Phased design

### Phase A — B13 honest redaction (foundation; unblocks trusting `redacted`)

**Change the data flow, not the redactor.** Surface the gate's sanitized prompt (and map) from
the pipeline and feed *that* to the exporter.

- `app/pipeline.py`: add `resp["sanitized_prompt"] = sanitized` to both return dicts (direct
  `:181` where `sanitized` is in scope; multi-step `:287` via `san_messages[-1].content`).
  Thread the **text only** (map is belt-and-suspenders; the exporter already re-redacts on
  egress). The completion is already `text`; its egress redaction stays in `_set_io`.
- `app/api/routes.py`: keep `prompt=` as the **raw** last message (so `full` still stores raw —
  the distillation substrate) and add a **separate** `sanitized_prompt=resp.pop("sanitized_
  prompt","")` to the `export_trace(...)` call. Pop it so the internal field never reaches the
  client.
- `app/obs/export.py:_set_io`: `redacted` → use the supplied `sanitized_prompt` (fall back to
  `redact(prompt)` when absent, for backward-compat); `full` → raw `prompt` unchanged; `off` →
  nothing. Net: `redacted` telemetry shows exactly what the model saw, `full` still stores raw.
- **UI copy (superseded by Phase C display, but corrected here as interim):**
  `ui/src/console/RequestDrilldown.tsx:149` — the unconditional "never stored" caption is
  removed / made mode-aware.

**Accepted limitation (documented, not a bug):** entity-based redaction masks *entities*, not
arbitrary business fields (subject/summary/customer_id). Those reached the model too, so showing
them in `redacted` telemetry is not weaker-than-the-gate. Masking arbitrary business fields is
out of scope (would need field-level policy, not entity detection).

**Tests:** a `redacted` request whose telemetry `input.value` equals the model's sanitized input
(byte-for-byte), and never contains an entity value the gate masked.

### Phase B — B14 per-application `capture_io`

- **Schema:** new nullable column `AppPolicy.capture_io: str | None`
  (`off | redacted | full`, null = inherit global). Add to `AppPolicyView`
  (`app/apps/policy.py`) and the store update allowlist (`app/apps/store.py:180`).
  **Migration:** manual `ALTER TABLE app_policies ADD COLUMN capture_io VARCHAR(16)` on
  deployed DBs — `create_all` never migrates (memory `routeforge-schema-no-migrations`).
- **Resolution (per request):** effective mode = app value if set else global
  `serving.observability.langfuse.capture_io` else `off`. Thread into
  `export_trace(capture_io=…)` at `routes.py:201` instead of reading the global.
- **Per-app residency lint (load-bearing).** At save time (admin PATCH) AND at runtime resolve:
  an app may resolve to `full` **only if** `load_app_langfuse(app) is not None` (own project)
  AND the app is not `onprem_only` / cannot carry `restricted`. Else reject the save with a
  named violation; at runtime clamp to `redacted`. This replaces the global
  `lint.py:107-117` "reject full if any onprem tenant" with a per-app check.
- **UI:** tri-state control in `ui/src/admin/ApplicationsPanel.tsx` app settings, with the
  `full` warning (raw store, access-controlled, TTL) and a disabled+explained state when the
  residency lint forbids `full`.
- **Follow-ups carried from Phase A final review (2026-08-18):** (1) **`full` currently captures
  only the last raw message** — `app/api/routes.py:195` sources `prompt=body.messages[-1].content`.
  Phase A made `redacted` reflect the *full* sanitized prompt, but `full` still stores only the
  last message, an incomplete distillation substrate for multi-message requests. B14/`full` must
  capture the whole conversation (raw), symmetric with the gate's full-concatenation view.
  (2) **Placeholder-format symmetry:** `input.value` uses the gate's numbered placeholders
  (`<MRN_1>`) while `output.value` uses the exporter's bare `redact()` form (`<MRN>`) — a cosmetic
  within-trace inconsistency to reconcile when Phase C drives symmetric display.

**Tests:** resolution precedence; lint rejects `full` for an onprem_only app and for an app with
no own project; runtime clamp; `full` allowed only for own-project + non-onprem app.

### Phase C — B14 symmetric UI display + B15 remainder

- **Symmetric display (supersedes B13 copy fix):** drive the drilldown's request+response
  display from the effective mode — `off` → metadata only (neither body); `redacted` → both
  bodies, masked; `full` → both raw. `RequestDrilldown.tsx`.
- **B15 admin cross-app reporting:** served from routeForge's **own** durable store (already
  aggregates all apps — Overview/Cost/Traffic/Privacy). Add an app-wise rollup view; source of
  truth is our store, not Langfuse. **No second Langfuse rollup project** (declined).
- **B15 manual creds path:** admin form to enter a per-app Langfuse `{host, public_key,
  secret_key, project_id}` when auto-provision returned None (air-gapped / no org key). Secret
  encrypted via existing `encrypt_secret`. Fail-closed: no project configured ⇒ `off`/shared-
  redacted, never raw-to-shared.

**Tests:** display matrix per mode; manual creds round-trip (encrypt → `load_app_langfuse`);
admin rollup numbers reconcile with summed per-request values.

### Phase D — B16 feedback + dataset substrate (fuller)

- **`POST /v1/feedback`** — body `{request_id | trace_id, score_name, value, comment?,
  corrected_output?}`. Writes a Langfuse **score linked to the trace** in the **app's own
  project** (that app's `app_langfuse` creds; scores are project-scoped). `corrected_output`
  stored as trace-linked (SFT/DPO substrate). Session-scoped auth like `/v1/chat/completions`.
- **Return trace id on every chat response** — add `request_id`/`trace_id` to the chat response
  so async feedback can attach later (already have `resp["id"]`; expose it + the Langfuse url
  already set as `resp["langfuse_url"]`).
- **Named score configs:** `route_correct`, `preferred_model`, `failure_type` (+ freeform
  quality). Documented enum; created in Langfuse on first use.
- **UI:** thumbs up/down on the request drilldown → `POST /v1/feedback`; a reviewer correction
  form (`corrected_output` + `preferred_model`).
- **Dataset-export readiness:** document the trace → Langfuse dataset → external TRL
  `{prompt, chosen, rejected}` transform (Target B needs `full`, residency-local). No export
  *pipeline* built (out of scope) — readiness only.

**Tests:** feedback posts a trace-linked score to the *app's* project (not shared); trace id
returned on chat responses; fail-closed when the app has no project (score dropped/logged, never
mis-routed); corrected_output stored.

#### Phase D — build addendum (2026-08-19, owner-decided)

Grounded in the code as it actually is (no Langfuse SDK usage anywhere — telemetry is OTel spans
+ REST; no durable `request_id ↔ trace_id` map; console is admin/session-gated, `/v1/*` is
app-token). Concrete decisions:

1. **Gated raw-completion capture (owner decision — "gated capture").** Today `capture_io=full`
   stores raw *prompts* but step-9 `verify()` masks *completions* before anything stores them
   (`app/output/verify.py`), so `full` completions are masked. Fix: the pipeline surfaces the
   **pre-verification** raw completion (`result.text`, both direct and multi-step paths) as
   `resp["raw_completion"]`; `routes.py` **pops it** (like `sanitized_prompt`) so it is never
   client-visible, never in `resp["display"]`, never in `HistoryStore`. It is threaded into
   `export_trace` only, and `_set_io` uses it for `output.value` **only on the `full` branch** —
   which the Phase-B clamp + per-app `skip_io` exporter already restrict to a non-`onprem_only`
   app with its **own residency-local project**. So the client reply and the shared/redacted
   telemetry stay masked; raw completions land **only** in the app's own residency-local project,
   symmetric with raw prompts. Rule #2 is **not** relaxed for shared sinks or the client.
2. **Scores via Langfuse REST**, not the SDK: `POST {host}/api/public/scores` with the app's
   `{public_key, secret_key}` Basic auth (mirrors `LangfuseProvisioner`'s httpx pattern). Scores
   are project-scoped by which creds post them → inherently the app's own project. Fail-closed:
   `app_langfuse is None` ⇒ drop + log, never post to a shared project.
3. **Expose `trace_id`** (32-hex) on the chat response (`export_trace` returns it alongside the
   URL; `routes.py` sets `resp["trace_id"]`). `/v1/feedback` keys on `trace_id` (reliable; no
   in-memory lookup). The admin UI route keys on `request_id` → reads `trace_id` from the stored
   history `resp`.
4. **Two feedback entry points over one helper.** `POST /v1/feedback` (app-token, the substrate
   API for apps/SDKs) and `POST /api/requests/{request_id}/feedback` (admin session cookie, for
   the console UI — resolves the request's app + creds server-side). Both call one
   `post_score(app_langfuse, trace_id, name, value, comment)` helper. Score-name enum validated
   server-side: `route_correct | preferred_model | failure_type | quality | corrected_output`.
   `corrected_output` is stored as its own score (text in `comment`) — the DPO "chosen".
5. **Named score configs:** best-effort lazy creation via `POST /api/public/score-configs` on
   first use (non-fatal if it fails); documented enum is the contract.

Implementation plan: `docs/superpowers/plans/2026-08-19-f1-phase-d-feedback-and-datasets.md`.

---

## 4. Sequencing & risk

A → B → C → D, in order — shared data model (`AppPolicy`, `export_trace`, `RequestDrilldown`).
A is the foundation (everything else trusts `redacted`). B changes the DB (manual ALTER — call
out in deploy notes). D is additive (new endpoint + UI), lowest risk.

Each phase is an SDD task with its own tests, built subagent-per-task with review between
(per global execution rule). No phase weakens an invariant in §2.

---

## 5. Open questions for review

1. Phase A: thread the redaction **map** to the exporter for defence-in-depth, or just the
   sanitized text? (Leaning: text only — simpler; map is belt-and-suspenders.)
2. Phase D: create score configs eagerly at boot vs lazily on first feedback? (Leaning: lazy.)
3. Is the **admin rollup**'s decline (own-store only, no second Langfuse project) final, or keep
   the redacted-rollup-project option open as a documented future?
