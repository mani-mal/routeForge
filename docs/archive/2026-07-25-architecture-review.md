# Architecture Review — Serving Plane (Steps 1–10)

> **Historical snapshot (2026-07-20 / updated 2026-07-25).** This review predates the
> multi-tenant product pivot and the 2026-08-03 pure-router pivot. Its "prototype"
> framing and any `inference_backend` / `mock | vllm | dynamo | none` references describe
> the repo as it stood then — the `inference_backend` axis and the `mock` backend were
> later **removed**; on-prem models are now onboarded at runtime. Kept as a point-in-time
> record; for current state read `CLAUDE.md`, `README.md`, and
> `docs/architecture/inference-layer-boundary.md`.
>
> **Further out of date (deployed `api:0.18.0`).** The whole seven-feature program shipped
> *after* this snapshot: stage/role-aware routing signals, the failure-driven gateway
> lateral model switch, the trace-mining recommend/external-training orchestrate tools,
> DSPy prompt-optimization (with the optional step-4 LLM classifier), the CCR-class
> semantic cache, and persistent memory — plus a pluggable `RoutingStrategy` seam and a
> quality/accuracy axis on the cost dashboard. So §4 "What still needs building" and the
> "Current verdict" below both predate `api:0.18.0`; read them as of mid-2026, not now.
> For current state see `docs/routing-observability.md`,
> `docs/architecture/semantic-cache-residency-spec.md`, and
> `docs/architecture/persistent-memory-residency-spec.md`.

**Original review:** 2026-07-20, against the design package (drawio, `CLAUDE.md`,
skills, `BUILD_PLAN.md`, four configs) — *before any code existed*.
**Updated:** 2026-07-25, against the built repo (Phases 1–9 complete: skeleton +
config lint, policy gate, router + gateway, output controls, observability, agent
runtime, compression, UI, traffic + tests, packaging, real backends). This revision also
folds in the real-model milestone: **vLLM (Qwen2.5-7B) validated end to end on the DGX**,
real human-facing model names (`display_name`), a **representative amortized on-prem
cost** (so savings read as high-but-not-free), the **live Langfuse per-trace deep-link**,
and the redesigned clickable ten-step flow visualizer.

**How to read this doc.** The original review found the design was sound but riddled
with **cross-file consistency drift** (§1) and a set of **underspecified seams** (§2).
The single highest-leverage fix — a startup config lint that turns every drift class
into a boot failure — was built first, and it closed §1 wholesale. Every §1/§2 finding
is annotated below with its **current status** and where it landed in code. The
forward-looking work is consolidated in the new **§4 — What still needs building**.

**Current verdict.** The prototype is built and internally consistent. The privacy
ordering (gate → compression → router), fail-closed defaults, post-compression
pricing, and dual savings attribution are all implemented and enforced by tests and
the config lint. Real-backend validation (§4.2), the Langfuse deep-link (§4.7), and the
top demo-correctness bug (trivial questions landing on *mid*, not *cheap* — §4.1) — all
open at earlier revisions — are now **done**: the first two verified live on the DGX,
and §4.1 closed by the per-application privacy-rules feature demoting `LOCATION`/
`DATE_TIME` in the seeded demo profiles. What remains is **tuning and hardening**, not
architecture: Presidio warm-up to kill a first-request cold-start (§4.4, seen live as
1/80 transient 503s), the Dynamo/TensorRT path still unexercised on hardware (§4.2), and
a short list of deliberately-deferred production concerns (§4.3, §4.5–§4.6). Nothing
outstanding threatens the demo's honesty
claims.

**Status legend:** ✅ resolved · 🟡 open (demo-relevant) · ⬜ deferred (production-track,
intentionally out of demo scope) · ◆ resolved as an explicit product decision.

---

## 1. Consistency drift — all resolved by the config lint

The startup config lint (`app/config/lint.py`, `tests/test_config_lint.py`) loads all
four YAMLs, cross-validates, and exits non-zero with a named violation. It is the
mechanism that closed this entire section and keeps it closed as configs evolve.

### 1.1 Provider name `onprem` vs `on_prem` — ✅ resolved
`onprem` (one word) is now used in `serving.yaml`, `routing.yaml`, `pricing.yaml`, and
`policy.yaml`; the residency flag is `onprem_only`. The lint fails the boot if the
spelling drifts across files. *(Verified: no `on_prem` remains in `config/` or `app/`.)*

### 1.2 The nonexistent `strong` tier — ✅ resolved
The canonical tier list is exactly `tiers: [cheap, mid, frontier]` in `routing.yaml`;
there is no `strong` tier anywhere. `risk_floors` (`high: frontier, medium: mid, low:
cheap`) name only real tiers, and the lint asserts every `risk_floors` tier and every
`model_pool` tier exists and has ≥1 member. The **residency-beats-risk-floor**
precedence the review asked for is now written explicitly in three places: `CLAUDE.md`
non-negotiable rule 8, the `risk_floors` comment in `routing.yaml`, and the router
itself — an `onprem_only` high-risk request floors to the best *allowed* on-prem tier
and sets `require_verification`, never lifts to cloud.

### 1.3 Disagreeing budgets/thresholds across three files — ✅ resolved
One owner per concern is now enforced: `serving.yaml` = infrastructure switches only;
`policy.yaml` = budgets + sensitivity + residency + tenants; `routing.yaml` = weights,
thresholds, tiers, pool, escalation; `pricing.yaml` = per-model USD + baseline. The
lint fails the boot on any duplicated concern (`max_spend_usd`, thresholds, etc.).

### 1.4 Sensitivity enum — three variants → ✅ one enum
The enum is `public | internal | confidential | restricted`, defined once in
`policy.yaml` and referenced by name everywhere else (gate, compression bypass rules,
UI). The lint enforces single-sourcing.

---

## 2. Architecture seams — resolved or deliberately deferred

### 2.1 Redaction map has no return path — ◆ resolved as a product decision
The demo **deliberately returns placeholders** (it *shows* redaction), and egress
redaction is enforced independently: step 9 (`app/output/verify.py`) re-runs the
Presidio anonymizer on the *completion* before it returns, so PII the input gate never
saw is still caught on the way out. The redaction map stays internal to the gate
(`redaction_map_id` in `ExecutionPolicyContext`, deterministic placeholder scheme in
`app/policy/detectors.py`); it is never exported to telemetry.
*Production-track (§4.6):* if a customer needs original values re-inserted, that
detokenization step is unbuilt and would need the same residency/TTL treatment as CCR.

### 2.2 Compression ↔ routing cycle — ✅ resolved (canonical-tokenizer approximation)
The router prices `tokens_after` as an **estimate** (`Router._est_cost`), and the true
cost is taken from the response (`actual_cost_usd` in the `DecisionRecord`). No
compress↔route iteration; per-model recompression is accepted as a known approximation.

### 2.3 Two overlapping config axes (`inference_backend` vs `serving_paths`) — ✅ resolved
Collapsed to the single `inference_backend: mock | vllm | dynamo | none` axis in
`serving.yaml`. The `serving_paths` block is gone; `mock` is a first-class,
lint-validated backend value and the demo default.

### 2.4 Confidence had no defined signal — ✅ resolved
Defined as **backend-reported**: the mock backend emits a seeded confidence
(`mock_confidence`, default 0.95); the single `confidence_escalation_threshold: 0.60`
is owned by `routing.yaml`, with `max_escalations: 1`. Documented in
`routing-and-gateway`.

### 2.5 Step-4 classifier as an unrouted LLM call — ✅ resolved (built, default-off)
`app/routing/task_mode.py` is **rules-only** and the default step-4 path. The optional
LLM classifier is now **built** as `app/routing/task_mode_llm.py` — but it is
**default-off** (gated by `PromptOptCfg.enabled`, shipped with the DSPy feature) and, when
enabled, is **routed through `Router.select`** (`task_mode_llm.py:114`) exactly like any
other call: sanitized input, respects `allowed_providers` (so it cannot break
`onprem_only`), and emits a DecisionRecord. `CLAUDE.md` documents the same rule.

### 2.6 Streaming unresolved between step 1 and step 9 — ✅ resolved
`config/serving.yaml` sets `reject_stream_requests: true`; `app/api/routes.py` rejects
inbound `stream: true` with a clear error. The UI's decision SSE is a separate channel
(`stream_decisions: true`) and stays on. Production incremental verification is noted
as an open question, out of scope.

### 2.7 CCR retrieval into cloud prompts — ✅ resolved (explicit + counted)
The `ccr_retrievals_to_cloud` counter exists end to end (`app/models.py`,
`app/compression/ccr.py`) and surfaces as the **"→ cloud"** tile in the UI's
Compression panel, so the "do originals ever go to cloud?" question has an honest,
visible answer (0 for residency-bound tenants).

### 2.8 Decision stream is unauthenticated telemetry — ⬜ deferred (production-track)
Unchanged and intentional for the demo. `serving.yaml` documents the boundary
(`stream_decisions` is "tenant-scoped + auth outside demo mode — see greenlake-deploy
skill"). Auth/tenant-scoping is **not implemented** and is tracked as GreenLake work
(§4.3), which is correct — building it now would add nothing to the laptop demo.

---

## 3. Skills & docs — resolved

- **§3.1 Step-9 skill gap → ✅ resolved.** The `output-controls` skill was added, and
  step 9 is fully built (`app/output/verify.py`, `tests/test_output_controls.py`):
  egress leakage re-check, `require_verification` consumed from `policy.yaml`,
  escalate-then-fail-closed, sampling, and the streaming decision.
- **§3.2 Numbering/scope drift → ✅ resolved.** Everything now says **steps 1–10**;
  the folder is `docs/`; skill step-ranges and the Dynamo skills list are
  single-sourced (`inference-backends` owns the full list).
- **§3.3 Restated facts → ✅ resolved.** Values live in config; skills reference them.
  The config lint is the backstop that keeps restatement from silently drifting.
- **§3.4 What's genuinely good (unchanged, keep).** The seven→eight hard rules
  repeated with identical meaning, the critical assertions wired as tests
  (`tests/test_traffic_honesty.py`, `test_two_lever_cost.py`), fail-closed defaults,
  redaction-on-egress, CCR-as-second-PII-copy framing, mock-first discipline, and
  visible per-call hop loops all survived the build intact.

---

## 4. What still needs building

Architecture is done; this is the remaining work, most demo-relevant first.

### 4.1 ✅ Trivial questions land on the *mid* tier, not *cheap* — closed by per-application privacy rules
The demo's opening beat is "simple question → **cheap** local model → fractions of a
cent." This was blocked because the *global* `config/policy.yaml` mapped `LOCATION` (with
`DATE_TIME`, `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`) → `confidential` →
`risk_class: medium` → `risk_floors.medium: mid`, so "What is the capital of France?"
floored to `local-32b` (mid) purely because Presidio tagged "France" as a `LOCATION`
entity — complexity was already correctly `cheap`.

**Fix shipped:** the per-application privacy-rules feature
(`docs/superpowers/specs/2026-07-30-per-app-privacy-rules-design.md`) makes
classification **per-app** instead of global-only. Each application can now carry its own
custom recognizers and sensitivity-rule overrides (entity → sensitivity/risk/residency/
require_verification), edited via the Settings-tab **Privacy rules** panel (§6) and
enforced through an `EffectiveDetector` that both the gate (step 2) and output
verification (step 9) consult, with a `_stricter`-only backstop so no rule can loosen a
tenant's residency floor. The seeded demo profiles use this to close 4.1 directly: **RAG
Agent** (`acme`) and **Support Agent** (`default`) demote `LOCATION` and `DATE_TIME` to
`internal` / `low` / residency `any`, while `PERSON`/`EMAIL_ADDRESS`/`PHONE_NUMBER` and
real PII (`US_SSN`, financial) stay `confidential`/`restricted` as before. With that
demotion in place, "What is the capital of France?" on RAG or Support Agent no longer
trips the medium risk floor and targets **cheap → local-8b**, landing the opening beat
without weakening PII handling anywhere else. Hospital Agent (`hospital`) is unaffected —
its `onprem_only` tenant default and new custom `MRN` recognizer (`restricted`/`high`/
`onprem_only`) keep everything on-prem regardless of per-app rule edits, demonstrating the
backstop. Covered by the router regression test (the "4.1 test") and the seeding tests
that assert the demo apps get the expected profiles idempotently on first boot.

### 4.2 ✅ Real-backend validation on GPUs — done (vLLM, verified live 2026-07-25)
The backend adapters (`app/gateway/backends/`, factory + Protocol `health()` checks,
startup validation/fallback) are now **exercised end to end on the DGX H200 (Run:ai,
namespace `runai-routeforge`)**. *(Later note: the separate `vllm`/`dynamo` adapters and
the `make_backend` factory named here were subsequently removed — `app/gateway/backends/`
is now `base.py` / `factory.py` / `litellm_backend.py`, a single LiteLLM adapter that
reaches every onboarded OpenAI-compatible endpoint.)* A 80-request burst against the
live API confirmed `backend: vllm`, genuine Qwen2.5-7B completions, real non-zero cost
($0.078 actual vs $6.92 all-frontier baseline ≈ 98.9% saved), real GPU latency
(p50 ≈ 6.8 s / p95 ≈ 12.8 s, replacing the mock's fixed timings), Presidio redaction on
egress, and Langfuse populated. **Still open on the real path:** the Dynamo +
TensorRT-LLM worker path is written but not yet exercised on hardware (only vLLM has
been), and `none` + `onprem_only` → reject is enforced structurally but not yet
re-confirmed on the cluster. The mock path remains demo-complete for laptops.

### 4.3 ⬜ Decision-stream auth & tenant scoping (GreenLake)
Per §2.8 — implement auth + tenant-scoping on `/api/stream` before any multi-tenant
GreenLake deployment. Demo-laptop scope is fine as-is.

### 4.4 🟡 Presidio warm-up on startup
No warm-up call exists, so the *first* request pays the analyzer's cold-start latency —
which will skew the p95 tile on the very first demo request. **Observed live** on the
DGX: 1/80 requests in a burst returned `503 policy evaluation unavailable` (a transient
Presidio hiccup) — fail-closed working correctly, but a startup warm-up would smooth the
first/cold calls and remove the one-off error. Add a startup warm-up (the `regex`
fallback already exists for air-gapped runs). Small polish, not blocking.

### 4.5 ✅ Reconcile deferred UI controls with the skill/plan docs — done (2026-07-30)
`BUILD_PLAN.md` Phase 6 and the `demo-ui` skill used to call for a **cost-weight slider**
("best interactive moment") the build deliberately dropped: the router is
**complexity/risk-tiered, not cost-tiered**, so a cost slider wouldn't honestly shift
tiers (see `_DEFERRED_CONTROLS` in `app/api/ui_routes.py`, surfaced via `GET /api/controls`).
Provider toggles were dropped for the same reason (the per-request `rejected[]` already
greys exclusions). **Fixed:** both `BUILD_PLAN.md` Phase 6 and the `demo-ui` skill's
Control panel now document these as deliberately-not-shipped, with the reason, so the next
builder doesn't re-add a dishonest slider.

### 4.6 ◆ Production detokenization path (if ever needed)
Per §2.1 — the demo returns placeholders by design. A production mode that re-inserts
original values is unbuilt and would need its own residency/TTL/never-in-telemetry
guarantees. Out of scope until a customer requires it.

### 4.7 ✅ Langfuse deep-link — wired (live per-trace URL)
The Decision Inspector's "Langfuse trace ↗" is now a **live deep-link** built from the
request's real OTel trace id (`langfuse_url` on the response; `trace_url()` in
`app/obs/otel.py`), using the browser-reachable `public_host` (`LANGFUSE_PUBLIC_URL`,
the NodePort) rather than the in-cluster OTLP host. It shows **"Langfuse off"** when
telemetry keys are unset instead of a dead link. Verified live: the link opens the
request's trace with per-step spans, cost, latency, and zero raw PII.
*Operational note (not a bug):* Langfuse gates trace pages behind an authenticated
browser session, so the deep-link lands on the sign-in page unless the viewer is logged
in as the seeded `demo@example.com` user (member of `demo-project`). Documented in the
UI guide FAQ; a one-line UI hint or public-trace sharing would remove the surprise on
stage.

---

## 5. Overall assessment

The 2026-07-20 review warned that an implementer following the design files literally
would ship "a router that filters out every on-prem model and a high-risk path that
targets a nonexistent tier." Building the config lint first, then the phases in order,
prevented exactly that: §1 and most of §2 are closed by construction, and the honesty
mechanisms (separate savings levers, the critical assertions as tests, never-cut list)
came through the build intact.

The prototype is **demo-ready on both the mock backend and the real vLLM path** — the
latter now validated end to end on the DGX (§4.2). §4.1, the one thing standing between
it and a clean run of the five-minute narrative, is now closed: the per-application
privacy-rules feature demotes `LOCATION`/`DATE_TIME` in the seeded demo profiles so the
opening beat lands on the cheap tier. The remaining work (§4.4 Presidio warm-up, the
Dynamo/TensorRT path in §4.2, §4.3 stream auth, §4.5 doc reconciliation) is refinement
and production-widening, not architecture.
