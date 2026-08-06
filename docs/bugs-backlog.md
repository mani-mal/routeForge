# Bugs backlog — work later

Running list of known defects/observations to pick up later. Newest first.
Sibling backlogs: `ui-open-items.md`, `eval-logging-backlog.md`.

---

## B1 — Multi-step 503s mislabeled "policy evaluation unavailable"

**Severity:** medium (misleading, not data-unsafe)
**Found:** 2026-07-31, DGX burst on `:0.6.0`

`app/api/routes.py:104` wraps the **entire** `pipeline.run()` in one broad
`except Exception -> HTTPException(503, "policy evaluation unavailable")`. The
gate (`app/policy/gate.py`) actually clears fine; for multi-step requests the
exception is raised **downstream** (agent hop / backend call / timeout), but
the caller still sees the gate's fail-closed message. This points debugging at
the wrong step (step 2) when the real failure is in steps 5–8.

**Fix idea:** narrow the fail-closed catch to the gate/context stage only;
give downstream pipeline failures their own label + status (e.g. 502/504 for
backend/timeout), so the gate 503 means what it says. Keep fail-closed
semantics for anything before the gate clears. Own SDD task.

## B2 — Multi-step burst saturates single vLLM (real inference capacity)

**Severity:** medium (capacity characteristic, not a correctness bug)
**Found:** 2026-07-31, DGX burst on `:0.6.0`

Post-LiteLLM-swap, each agent hop does **real** ~16–24s vLLM inference (was
instant fake). Under a burst, dozens of long-lived multi-step requests overlap
on the **single** `vllm-local-8b` pod; some exceed `vllm.timeout_s: 60` →
exception → B1 503. **Lowering rps does not help** — the requests are
long-lived, so they overlap regardless of arrival rate (19/150 failed at rps=3
vs 21/150 at rps=8, all `multi_step`).

**Levers (pick per goal):**
- Scale vLLM (2–3 replicas behind the `vllm-local-8b` service) — real fix, needs GPU + manifest change.
- Trim the `multi_step` share in `tests/personas.yaml` for the burst demo — cheap, keeps the "500-req burst" moment clean.
- Raise `vllm.timeout_s` and/or bound agent concurrency — mitigates, doesn't remove.
- Blocked from scaling the **API** tier by B3.

## B3 — In-memory dashboard state blocks horizontal API scaling

**Severity:** medium (architectural; blocks B2's API-scaling lever)
**Found:** 2026-07-31

`app/api/history.py` (ring buffer) and `app/api/bus.py` (SSE decision stream)
are **process-local**. Running >1 API replica load-balances requests across
pods, so the flow visualizer and cost dashboard each render only the fraction
of traffic that hit their pod — the dashboard splits. So the API tier cannot
be scaled out for throughput without first moving history + the decision bus
to a **shared store** (DB or Redis pub/sub). Until then, single API replica is
a hard constraint for a coherent demo.

## B4 — Presidio SSN recognizer misses raw SSN on egress

**Severity:** low (pre-existing, unrelated to the LiteLLM swap)
**Found:** 2026-07-31 (also present on `:0.5.0`)

In one multi-step output the egress redactor masked `PERSON` but left a raw
`123-45-6789` in the completion. Egress redaction (CLAUDE.md rule 2) missed
the SSN pattern in model-generated text. Needs a recognizer/tuning pass on the
**egress** path (the input gate caught it; egress didn't).

## B5 — Multi-step latency reported as 0 ms  ✅ RESOLVED (2026-08-06, Cluster C)

**Severity:** medium (resolved)
Per-hop latency now flows end-to-end: each `agent_hop` DecisionRecord records
the real measured `latency_ms` (`app/agent/internal_endpoint.py`, from the
backend's wall-clock timing), and `app/api/history.py` + the DB usage row sum
it into the request total; the UI drilldown reads the summed value. Closed out
in Cluster C with hardened `> 0` latency assertions
(`tests/test_litellm_multistep.py`, `tests/test_agent_hop_compression.py`) and a
corrected `app/gateway/backends/base.py` comment. Cross-ref memory
`routeforge-multistep-latency-zero-bug` (now stale/resolved).

## B6 — Can't rename an application

**Severity:** medium (admin usability)
**Found:** 2026-08-04, UI

Editing an application's name in the admin UI doesn't persist (or the field is
not editable). Operators can't correct/rename an app after creation. Needs a
working edit path (UI field + PATCH/PUT to the app record) in
`ui/src/admin/ApplicationsPanel.tsx` and the corresponding API route.

## B7 — Can't delete an application

**Severity:** medium (admin usability)
**Found:** 2026-08-04, UI

No working way to delete an application from the admin UI. Operators accumulate
stale/test apps with no cleanup path. Needs a delete action in
`ui/src/admin/ApplicationsPanel.tsx` + a DELETE API route (and decide cascade
behavior for the app's policy/rows).

## B8 — Application setup doesn't show the cost currency unit

**Severity:** low (cosmetic/clarity)
**Found:** 2026-08-04, UI

In the application setup screen, cost values render without a `$` unit, so the
number is ambiguous. Add the `$` prefix (and confirm USD/1M framing matches
`pricing.yaml`) on the setup UI.

## B9 — Cost density explanation text is truncated

**Severity:** low (cosmetic/clarity)
**Found:** 2026-08-04, UI

Under Cost, the "Token Intelligence Density" and "Cost Intelligence Density"
StatTiles have `sub` explanation text ("quality delivered per 1k tokens / per
$ — needs a per-model quality score") that gets clipped and doesn't display
fully (`ui/src/console/sections/CostSection.tsx:135-144`). Either show the full
text (let the tile wrap/expand) or move the explanation into a tooltip/info
balloon (`?` hover) so the label stays compact.

## B10 — Router shows disabled cloud providers as spurs, mislabeled "residency"

**Severity:** medium (misleading demo trace + wrong rejection reason)
**Found:** 2026-08-04, UI (Model Router card, decision drilldown)

The Model Router card lists *GPT-4o mini (Azure)* and *Claude Sonnet (Bedrock)*
as rejected spurs labeled **"excluded · residency"**, even though both cloud
providers are disabled at the infra kill-switch (`config/serving.yaml:61-62`,
`azure/bedrock enabled: false`). Two distinct defects:

1. **Disabled providers appear as candidates at all.**
   `ModelRegistry.effective_pool()` (`app/apps/registry.py:60-67`)
   unconditionally merges the entire static YAML cloud `model_pool`
   (`config/routing.yaml:60-61`) without consulting
   `providers.{azure,bedrock}.enabled`. So a killed cloud path still enters the
   candidate list. Per CLAUDE.md, "cloud path availability =
   providers.{azure,bedrock}.enabled" — a disabled provider shouldn't be a
   candidate. These aren't onboarded registry models either, which is why they
   read as phantom entries.

2. **Wrong rejection reason.** When the router drops a candidate whose provider
   is absent from `ctx.allowed_providers` (`app/routing/router.py:61-66`), it
   hardcodes `reason="excluded by policy (residency)"`. But
   `ctx.allowed_providers` is `serving.providers.*.enabled` filtered by
   residency (`app/policy/context.py:56-72`) — so a provider dropped by the
   kill-switch is mislabeled "residency" even for a non-sensitive request with
   no PII. The two causes (disabled vs. residency) are conflated.

**Fix idea:** exclude candidates whose provider is disabled in `serving.yaml`
from `effective_pool()` (or before the router loop) so they don't show as spurs
at all; and/or split the `allowed_providers` rejection into distinct reasons
("provider disabled" vs. "excluded · residency") so the label tells the truth.
Decide whether disabled providers should be hidden entirely or shown greyed with
an honest "provider off" reason. Own SDD task.

## B11 — "What happened" summary card labels request "Cloud-eligible" when cloud was excluded

**Severity:** medium (misleading demo headline; contradicts the panel below it)
**Found:** 2026-08-04, request drilldown (support-ticket demo)
Cross-ref: B10 (same request, router-card side of the same confusion).

The *What happened* PRIVACY card headlines a request **"Cloud-eligible — no
sensitive data found, so every provider was eligible"** while, on the very same
screen, SERVING PROVIDERS greys out Azure and Bedrock and the Model Router
rejects them. In the ticket demo cloud is excluded because
`providers.{azure,bedrock}.enabled: false` in `config/serving.yaml`, so
`ctx.allowed_providers` is `onprem` only — yet the summary still says every
provider was eligible. Two smaller wrongs compound it: the card also shows a
`PERSON` chip right beside "no sensitive data found," and the sensitivity is
`confidential`.

**Root cause:** the summary card derives "cloud-eligible" from the gate's
`onprem_only` flag alone (`false` ⇒ "every provider eligible"), instead of from
the **effective candidate pool** (`ctx.allowed_providers`) after infra
kill-switch + residency filtering. `onprem_only=false` only means *residency
didn't force on-prem*; it does not mean cloud is actually reachable.

**Fix idea:** compute the card's eligibility line from `ctx.allowed_providers`
(does it contain any cloud provider?), not from `onprem_only`. When cloud is
absent, say why (e.g. "cloud providers disabled" vs. "kept on-prem by
residency"). Reconcile the "no sensitive data found" wording with the detected
entity chips so a `PERSON`/`confidential` request doesn't read as clean. Pairs
naturally with B10. Own SDD task.

## B12 — "All-frontier baseline" omits completion tokens → routing_saved goes negative

**Severity:** medium (undercuts the flagship cost-savings claim; number goes
negative on the screen meant to prove savings)
**Found:** 2026-08-04, request drilldown + Overview (support-ticket demo)

A direct request to the mid model (152 in / 972 out) shows `actual spend
$0.0105`, `all-frontier baseline $0.0023`, and `routing saved $-0.0082` —
routing appears to *cost* money. The Overview simultaneously reports `+$0.0255 /
27.4%` saved across the 6 calls, and the per-request baseline ($0.0023) is ~6.7×
smaller than the implied aggregate baseline (`spend+saved` ⇒ ~$0.0155/req).

**Root cause:** `Pipeline._baseline()` (`app/pipeline.py:58-60`) prices a single
token count at the blended `(input+output)` rate, and the direct path feeds it
`comp.tokens_before` (`app/pipeline.py:150`) — the **pre-completion** token count
(~the prompt, ~128 tokens). So the baseline excludes the 972 output tokens the
model actually generated, while `total` (actual spend) includes them.
`routing_saved = baseline_usd - total` (`app/pipeline.py:153`) is therefore
comparing frontier-price-of-the-prompt against mid-price-of-prompt+completion —
apples to oranges — and goes negative. A real frontier model answering the same
request would emit ~972 output tokens too (~972 × $15/1M ≈ $0.015), so the honest
all-frontier baseline is *higher* than actual and routing_saved should be
**positive** (~+$0.005).

Two compounding defects:
1. **Wrong token basis.** Baseline is priced on `tokens_before` (input only), not
   the actual input+output the request consumed.
2. **Wrong rate model.** `_baseline` multiplies tokens by `(input+output)` summed
   rate, charging every token both prices, instead of
   `in_tokens×input_rate + out_tokens×output_rate`.

The per-request-vs-aggregate mismatch is almost certainly the same root cause
surfacing differently between the drilldown value and the dashboard aggregation
(`app/api/history.py` / cost section) — verify both read the same corrected
figure once (1) and (2) are fixed.

**Fix idea:** price the baseline on the *actual* completion's input+output token
counts (same basis as `total`), with input and output priced at their respective
frontier rates; keep the additive identity `compression_saved + routing_saved ==
baseline_usd - total`. Add a unit test that a same-tier request yields
`routing_saved == 0` and a cheaper-tier request yields `routing_saved > 0`.
Re-verify the Overview aggregate against summed per-request values. Own SDD task.

## B13 — `capture_io: redacted` stores raw application input in Langfuse (weaker than the gate); UI claims it never does

**Severity:** medium (privacy-story integrity; Rule #2. Demo lab uses synthetic
data so not a live breach, but it undercuts the central claim on-camera)
**Found:** 2026-08-04, verified by curling the DGX-lab Langfuse trace
`6d892726ae704ab83320dd3a3d3d8a27` (`serving.request`).

The DGX lab overrides the shipped default: `SP_CAPTURE_IO=redacted`
(`deploy/dgx_lab/routeforge-app.yaml:127`), so `input.value`/`output.value` are
attached. Two problems surfaced:

1. **Input is stored essentially raw.** The trace `input.value` is the verbatim
   application payload — `{"customer_id":"CUS-204","region":"Austin","subject":
   "Cannot connect after router replacement","summary":"Customer replaced a home
   router…","ticket_id":"TCK-1001",…}` — byte-for-byte the source ticket. Notably
   `"region":"Austin"` is stored **unredacted** even though the Privacy tab
   flagged `LOCATION` and the model's *own* prompt had Austin masked (the
   completion echoes it as `<PERSON_1>`). So telemetry redaction is **weaker than
   the gate's**: `routes.py:122` passes the **raw** `body.messages[-1].content` to
   `export_trace`, which re-runs Presidio (`_set_io` → `redact`, `app/obs/export.py:31-33`)
   on the compact JSON and under-detects. Business fields (subject, summary,
   customer_id) aren't entity types at all, so they pass through untouched. Net:
   `redacted` mode ≈ raw application input in Langfuse.

2. **The UI claim is false in this mode.** The drilldown Request card states *"The
   raw prompt is never stored or shown — nothing external sees it until the gate
   clears it"* (`ui/src/console/RequestDrilldown.tsx:149`), unconditionally — but
   Langfuse (an external sink) has the prompt whenever `capture_io != off`. The
   copy needs to reflect the mode, or the guarantee is misleading.

**Root cause:** the telemetry path redacts the *raw request body* independently
instead of reusing the gate's already-sanitized text + redaction map. The gate
and the exporter therefore disagree on what's PII.

**Fix idea:** feed `export_trace` the gate's **sanitized** prompt + its redaction
map (the same masking the model received), not `body.messages[-1].content`; then
`redacted` telemetry can never be weaker than what the model saw. For the UI copy:
**B14 supersedes the copy-only patch** — instead of just relabeling the Request
card, drive the actual request+response *display* from the per-app setting
(symmetric: both bodies shown redacted/raw, or metadata-only when `off`). Short-term
demo mitigation: set `SP_CAPTURE_IO=off` on the DGX deploy to restore blank Input.
Cross-ref `docs/eval-logging-backlog.md` and B14. Own SDD task.

## B14 — FEATURE: per-application capture_io (off | redacted | full) in app settings

**Type:** feature (with a design-bug component: the current global-only switch is
too coarse for a multi-tenant product).
**Severity:** medium (unblocks per-app eval/audit without breaking the privacy
story; requested by owner 2026-08-04).
**Depends on:** B13 (redaction gap) — per-app `redacted` is only trustworthy once
`redacted` actually redacts. Cross-ref `docs/eval-logging-backlog.md`.

**What:** move `capture_io` from a single global switch
(`serving.observability.langfuse.capture_io` + `SP_CAPTURE_IO`) to a
**per-application** setting chosen in the app's settings UI, so each application
decides whether *its* Langfuse traces store `off` (no I/O), `redacted` (values
masked to `<TYPE>`), or `full` (raw prompt + completion). Langfuse then stores
per the owning app's choice.

**Why it fits routeForge:** the product is multi-tenant/multi-app; per-app policy
already exists (`AppPolicy`/`AppPrivacyProfile`: `allowed_providers`,
`allowed_tiers`, custom recognizers), and `app/obs/export.py:export_trace`
**already routes each app's trace to its own Langfuse project** (`app_langfuse`),
so raw data for app X is already isolated to X's project. The global switch is the
outlier: it forces all-or-nothing and makes `full` a boot failure whenever *any*
tenant is `onprem_only` (`app/config/lint.py:107-117`), even for apps that carry
no sensitive data. Per-app capture removes that false coupling.

**Design:**
- New field on the app's policy/profile (DB-backed `AppPolicy` or
  `AppPrivacyProfile`), e.g. `capture_io: off | redacted | full`, default =
  inherit global / `off`. Surfaced in the Applications settings panel
  (`ui/src/admin/ApplicationsPanel.tsx`).
- Thread the app's value into `export_trace(capture_io=…)` per request instead of
  reading the global (`app/api/routes.py:121`).
- **Per-app residency lint (the load-bearing guardrail).** The global "reject
  `full` if any onprem_only tenant" check becomes per-app: an app may select
  `full` only if it is NOT onprem_only / cannot carry `restricted` traffic, OR its
  `app_langfuse` target is residency-local + access-controlled. The setting is
  **opt-in within policy, never a policy override** — an onprem_only app cannot use
  it to send raw PHI to a shared project (Rules #2/#6/#8; residency beats eval).
- **Fail closed:** missing/invalid/unknown → most-restrictive (`off`/`redacted`),
  never raw.
- **RLHF amendment (see B16 / `docs/rlhf-readiness.md`):** `capture_io` gates only
  the raw prompt/completion **I/O**. Router-diagnostic metadata (eligible/rejected
  candidate snapshot, selection reason, routing-policy version) is **not PII** and
  must be emitted **always-on**, independent of the tri-state — even an `off` app must
  still yield router-training signal. Add these to the always-emitted `sp.*` set.
- **The setting governs the routeForge UI display too, not just Langfuse.** Today
  the drilldown shows the *response* but hides the *request* behind a (false)
  "never stored" caption — asymmetric and inconsistent. Drive both surfaces from
  the per-app setting, symmetrically: `off` → show neither body (metadata only);
  `redacted` → show request **and** response, masked; `full` → show both raw. This
  **supersedes B13's copy-only fix** (`RequestDrilldown.tsx:149`) — fix the
  *display*, not just the caption.
- **Trust-boundary nuance (keep display and export distinct in the schema).** The
  routeForge UI is *first-party* (owning tenant/operator viewing their own request
  inside the boundary); Langfuse is *egress* (external sink, possibly a shared
  project). "Raw in the UI" is a lighter risk than "raw in Langfuse". Couple them
  under one tri-state in v1 for simplicity, but model *display* vs *export* as two
  concepts so a later split (UI raw / Langfuse redacted) needs no rewrite. The
  residency guardrail binds the **export/full** side, not the first-party UI view.
- Carry the `full` warning per app: raw store must be access-controlled with a
  retention TTL.

**Scope guard:** this captures *data* (in scope). Eval/regression *pipelines* on
top = Improvement Plane = out of scope (CLAUDE.md).

**Gotcha:** new AppPolicy/AppPrivacyProfile column needs a manual `ALTER` (or DB
recreate) on deployed DBs — `create_all` never migrates (see memory
`routeforge-schema-no-migrations`). Own SDD task; plan first.

## B15 — FEATURE: per-application Langfuse project (isolation) + admin cross-app reporting

**Type:** feature (tenancy/observability).
**Severity:** medium (multi-tenant data separation + precondition for B14 raw
capture; currently all apps land in the shared `demo-project`).
**Relates to:** B14 (per-app capture_io) — this is the store-isolation half of the
same workstream. Langfuse is **v3** (`deploy/dgx_lab/langfuse.yaml:277`).

**Current state:** on the DGX box every application exports to the single shared
project `demo-project` — so any project viewer sees *all* apps' traces. The per-app
machinery already exists but is unused: `export_trace(app_langfuse=…)`
(`app/obs/export.py`) routes to an app's own project, `get_app_tracer(host,
public_key, secret_key)` builds a per-app exporter keyed by its keys, and
`app_trace_url(project_id,…)` (`app/obs/otel.py:51-56`) builds per-project
deep-links. Today nothing populates `app_langfuse` per app.

**What:** make **project-per-application** the product default — each app exports
to its own Langfuse project, and its owner has keys/access to only that project.

**Pros:** real tenant isolation (Langfuse v3 project = the RBAC + data boundary);
**enables per-app residency** (an `onprem_only` app points at a residency-local
project while others use shared/cloud — the precondition B14's `full` mode needs; a
single shared project cannot satisfy it); per-app retention/TTL, quotas, blast
radius; clean owner deep-links.

**Cons:** **no native cross-project aggregation in Langfuse v3** (dashboards +
Metrics API are project-scoped; org roles grant per-project access, not a merged
view) — see admin reporting below; operational overhead (N key pairs to provision,
store, rotate; automate project+key creation on app onboarding via Langfuse Admin
API); fragmented per-project dashboards.

**Admin cross-app / app-wise reporting (the key design point):**
1. **Primary: report from routeForge's OWN admin store, not Langfuse.** routeForge
   computes every DecisionRecord and already aggregates across all apps (Overview/
   Cost/Traffic/Privacy tabs). Cross-app + app-wise rollups belong here — source of
   truth, not gated on Langfuse, and keeps raw tenant data out of any shared store.
2. **Optional: a redacted, metadata-only "admin rollup" Langfuse project.** Second
   copy carrying only `sp.*` (entity **types**, cost, model, sensitivity — never raw
   I/O), so it's residency-safe to co-mingle; filter/group by `sp.application_id`.
   Cost: double-write + a second store to govern.
3. External aggregation over each project's Metrics API into a warehouse/Grafana —
   most flexible, most work, duplicates (1).

**Design:** per-app Langfuse creds on the app record (host/public_key/secret_key/
project_id), surfaced/managed in admin onboarding; provision project+keys on app
create (Langfuse Admin API); **fail-closed** — no per-app project configured ⇒ fall
back to `off`/shared-redacted, **never raw-to-shared**. The per-app residency lint
(B14) binds the export target. Admin cross-app reporting served from routeForge's
admin console (1), with the redacted rollup project (2) optional.

**RLHF amendment (see B16 / `docs/rlhf-readiness.md`):** feedback scores must be
written to the **same Langfuse project as the trace** they annotate (scores are
trace-linked within a project), using that app's `app_langfuse` creds. The redacted
rollup project suffices for cross-app router eval; LLM fine-tuning reads the app's own
`full` project.

**Sequencing (whole workstream):** B13 (honest redaction) → B14 (per-app capture_io
governing storage + symmetric UI display) → B15 (per-app projects + admin rollup).
Shared files/data model — plan together. Own SDD task; plan first.

## B16 — FEATURE (future): RLHF-readiness — feedback capture, router-decision export, dataset export

**Type:** feature (future; design in `docs/rlhf-readiness.md`).
**Severity:** low-now / high-later (unlocks feedback-driven router + model
improvement; owner flagged 2026-08-05 as a future build).
**Scope boundary:** routeForge builds only the **capture/feedback/export substrate**.
Reward-model/DPO/PPO training, LLM fine-tuning, policy *retraining*, eval gates, and
promotion are the **Improvement Plane = out of scope** (CLAUDE.md), external via
TRL/OpenRLHF. Captures data; does not train or auto-promote (rule #4).
**Builds on:** B13/B14/B15 (the capture substrate) — with the two amendments noted in
B14 (router metadata always-on) and B15 (feedback lands in the trace's project).

Langfuse is the feedback-data + eval layer (traces, scores, annotation queues,
corrected outputs, datasets, experiments, export) — not the trainer. Two improvement
targets: **(A) the routeForge router** (highest value; trains a small classifier/
reranker/bandit or better rules from feedback — works on **redacted** data), and
**(B) an underlying LLM** (SFT/DPO from corrected outputs — needs **`full`,
residency-local** capture). See `docs/rlhf-readiness.md` §2/§5.

**Three gaps to close (grounded in code):**
1. **Eligible/rejected snapshot never reaches Langfuse.** `app/routing/router.py`
   builds `RouterDecision{chosen, reason, rejected[]}` with per-candidate reasons, but
   `app/obs/export.py` lifts only `sp.chosen`/`sp.reason`. Export the full candidate
   snapshot as an always-on structured attr (e.g. `sp.candidates`). *The make-or-break
   field for training/diagnosing the router.*
2. **No routing-policy version.** Nothing stamps one (`app/routing`/`app/policy`/
   `app/config/schema.py`). Add `routing_policy_version` (hash/semver of routing +
   scoring config) on every trace so Langfuse experiments can compare versions.
3. **No feedback ingestion path.** Add `POST /v1/feedback` (+ UI thumbs/reviewer form)
   → Langfuse **score linked to the trace** in the app's project; return the
   trace/request id on every chat response so async feedback can attach later.

**Also:** routing-specific score configs (`route_correct`, `preferred_model`,
`failure_type`); dataset-export readiness (traces + corrected outputs → Langfuse
datasets → external TRL prompt/chosen/rejected transform). Full field list, matrix,
and pipeline progression in `docs/rlhf-readiness.md`. Own SDD task; plan first
(after/with B13–B15).

## B17 — FEATURE: group a client-driven workflow's requests under one parent row (correlation id)

**Type:** feature (observability/UX).
**Severity:** low-now / medium-later (real-integration ergonomics; most customers
orchestrate client-side).
**Found:** 2026-08-05, walking the Requests tab with `test_app/routeforge_sample`.

**Observation.** `test_app/routeforge_sample` is a **client-side** LangChain workflow
(`agent.py` `RunnableSequence`: plan → synthesize → review). Each stage does its own
`model.invoke`, so **one run = three separate direct `/v1/chat/completions` requests**
to RouteForge, and two runs render as **six independent rows** (each `hops = 1`). From
RouteForge's view these are unrelated direct calls — the "agent" lives in the client,
NOT in RouteForge's own step-4 agent runtime. RouteForge therefore has **no link**
between the three calls that make up one investigation.

**Do NOT confuse two mechanisms:**
- **Server-side multi-step** (step 4 → agent runtime): all hops already land in ONE
  request's `resp["trace"]` and ONE `RequestSummary` row (`app/api/history.py`, `hops`
  field). Nesting these in the list is pure UI over existing data. The sample does not
  exercise this path.
- **Client-side workflow** (the sample): N independent direct requests. Grouping them
  needs a correlation id the client sends; RouteForge does not capture one today.

**What:** let a client tag the calls of one workflow with a **correlation/session id**;
RouteForge captures it, stores it on each request, and the Requests list groups tagged
rows into an **expandable parent → children tree** (parent = the workflow/session with
summed cost + latency + call count; children = the individual LLM calls with per-call
model/tier/cost/latency).

**Design sketch (own SDD spec — plan first):**
- **Client contract:** accept an OpenAI-compatible carrier — prefer the standard `user`
  field and/or a header (e.g. `X-RouteForge-Session`) — so any OpenAI SDK / LangChain
  client can set it without a bespoke API. Update `test_app/routeforge_sample` to send a
  per-investigation id across its three `model.invoke` calls (reference integration).
- **Server:** capture the id on the request, thread it into the `DecisionRecord` /
  `RequestSummary` (`session_id: str | None`), and expose it on the `/requests` list +
  drilldown payloads. No policy/routing behaviour change; it is a label only. Redaction
  rules still apply (a session id is operator-supplied, not PII, but treat it as opaque).
- **Langfuse sessions (same id, second surface).** `app/obs/export.py:export_trace`
  currently sets only `sp.*` + `input/output` on the root span and **never sets a session
  id**, so Langfuse shows N ungrouped traces and no Session (observed 2026-08-05: six
  traces, zero sessions, for two sample runs). Stamp the correlation id as Langfuse's OTel
  **session attribute** on the root span (e.g. `langfuse.session.id` / `session.id` —
  verify the exact key against the running Langfuse v3 OTel mapping) so the workflow's
  calls group under one Session automatically. **Granularity: one session = one workflow
  run, NOT one application** — a session-per-app would merge every request the app ever
  makes into a single session. One correlation id drives both the RouteForge Requests-tab
  tree and the Langfuse Session grouping.
- **UI:** in the Requests list, collapse rows sharing a `session_id` under one expandable
  parent; ungrouped (no id) requests render flat as today. Reuse the drilldown's per-hop
  presentation for the children. Mock:

  ```
  ▾ investigation TCK-1001 · 3 calls   confidential   $0.0342   11.6s
       ├─ 1  nemotron-nano-8b  cheap  $0.0097  2716 ms
       ├─ 2  nemotron-nano-8b  cheap  $0.0134  3327 ms
       └─ 3  nemotron-nano-8b  cheap  $0.0111  5271 ms
     direct-qa                        public         $0.0021  0.9s   (no session, flat)
  ```

**Scope guard:** captures/labels/displays a correlation id (in scope). No workflow
*execution* moves into RouteForge — the client still orchestrates (pure-router boundary,
CLAUDE.md rule #4). Cross-ref B3 (a shared session store is needed before this survives
multi-replica API scale-out, same as history/bus). Own SDD task; plan first.
