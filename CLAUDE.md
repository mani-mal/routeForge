# CLAUDE.md — RouteForge

## What this project is

RouteForge is a **self-hostable, OpenAI-compatible, multi-tenant model router and
policy gateway** that customers deploy on their own infrastructure (typically on-prem,
often air-gapped). Applications register, get API tokens, and call the OpenAI-compatible
`/v1/chat/completions`; an admin configures per-application allowed models, providers,
residency, and budgets, and views usage. On-prem models are onboarded at runtime — the
pool boots empty and the operator points RouteForge at their own OpenAI-compatible
endpoints (see `docs/guides/onboarding-models.md`).

Internally it implements **steps 1–10 of the Unified LLM Orchestration architecture —
the Serving Plane** (the diagram's canonical count; use "steps 1–10" everywhere, not
"1–8" or "1–9"). Those ten steps are the module architecture, kept as swappable seams
because the seams are the product's value. RouteForge delivers three guarantees:

1. **Privacy-first** — sensitive requests never leave the customer's infrastructure.
2. **Cost reduction** — per-call routing sends most traffic to cheap/local models.
3. **Observable** — every decision is captured in Langfuse via RouteForge's own
   OpenTelemetry exporter.

**Two product boundaries — by design, not by immaturity:**

- **Pure router.** RouteForge points at OpenAI-compatible endpoints and deploys, scales,
  or load-balances **no** inference itself. Operators stand up their own serving
  (vLLM/Dynamo/NIM/any OpenAI-compatible server) and onboard it. If asked to make
  RouteForge deploy or scale inference, say it is out of scope and stop.
- **Serving Plane, control-plane role — orchestrated, not hosted.** The Improvement Plane
  (training, evaluation gates, promotion) runs on **external compute**. RouteForge may capture
  the training substrate, **trigger** an external training/distillation job, and **onboard the
  result** — but it runs no training loop, hosts no training or inference compute, and has no
  in-process promotion path. Onboarding stays the only path a model enters. If asked to make
  RouteForge **host** training or inference compute (not just orchestrate it), say it is out of
  scope and stop. (See `docs/architecture/decisions/self-improving-agent-feature-fit.md` for the full
  scope decision, 2026-08-17.)

## Definition of done

Product acceptance criteria for a customer deployment:

- Deploys to customer infrastructure — Docker Compose for a single node, Helm for
  Kubernetes / HPE GreenLake — and boots with the cross-file config lint passing.
- An operator onboards an OpenAI-compatible on-prem endpoint at runtime: the pool boots
  empty, onboarding is probe-gated, and it takes effect with no restart.
- Applications authenticate with API tokens; per-application policy narrows routing;
  usage is metered durably (DB-backed).
- Every request emits a `DecisionRecord` — branch taken, policy verdict, model chosen,
  cost, latency — surfaced in the console and in Langfuse with PII redacted.
- Privacy invariants hold under test: `onprem_only` rejects when no healthy on-prem
  model exists, the gate fails closed, and telemetry is redacted on egress.
- Cost dashboard shows spend vs. an "all-frontier" baseline, with routing savings and
  compression savings tracked separately.

## Non-negotiable rules

These come from the architecture's hard rules. Violating them breaks the product's core
guarantees.

1. **Nothing external before the policy gate.** No classifier, no cloud model, no
   tool, and no telemetry sink sees the raw request until step 2 completes. Ordering
   is load-bearing, not stylistic.
2. **Telemetry is redacted on egress — except raw capture an operator
   explicitly opts an app into.** Redact at the exporter by default — tool outputs
   and model completions leak PII the input gate never saw. The one exception: an
   operator may configure a specific app with `capture_io=full`, sending raw I/O
   **only** to that app's **own** provisioned Langfuse project. It requires the
   deployment ceiling to permit it (`allow_app_pii_downgrade` for
   `pii_detection=off`, `allow_app_passthrough` for `policy_mode=passthrough`) and
   the app to be in a declared no-protection posture. The shared project never
   receives raw I/O; the exporter fails closed to `redacted` if the app's project
   is missing.

   *Amended 2026-09-05.* This rule used to key the exception on residency — a
   **non-`onprem_only`** app only. That encoded an assumption the product no longer
   makes: that on-prem implies maximum protection. It forced customers who want
   RouteForge as a pure router/meter/tracer over their own data to misdeclare
   residency to obtain a protection posture, which is strictly less safe (see rule
   2a). Residency is placement; posture is protection.

2a. **Placement and protection are separate axes; both are explicit.**
   There is exactly one **placement** axis — `residency` — and it governs *where
   compute may run*, nothing else. Everything else is **protection**, and each
   protection axis is independent: `model_input` (`raw | redact` — what the model
   is shown), `client_output` (`raw | redact` — what the client receives),
   `capture_io` (`off | redacted | full` — what telemetry stores), `pii_detection`
   (`off | regex | presidio` — which detector), `content_integrity`
   (`transformations_allowed | exact` — whether lossy compression may run) and
   `stream_mode`. `policy_mode` is a **preset**, not a seventh axis: `enforced`
   resolves to `model_input: redact` + `client_output: redact`, `passthrough` to
   `raw` + `raw` (and, only when the app names no detector, `pii_detection: off`).

   One pure function resolves all of it — `compile_policy` in
   `app/policy/compile.py`. Precedence, highest first: **deployment ceiling →
   explicit per-app value → `policy_mode` preset → deployment default**; anything
   unparseable **denies** to that axis's protective end and the write is refused.
   Every consumer reads the compiled result; nothing re-derives a posture.

   **Neither kind of axis may be inferred from the other.** A protection setting
   must never move placement — `passthrough` must never widen `allowed_providers`,
   clear `onprem_only`, or otherwise touch placement — and a placement setting must
   never change a protection value. The *only* cross-axis path is declared and
   raise-only: `config/policy.yaml`'s `sensitivity_rules` may **tighten** residency
   on a detected entity, gated per app by `escalate_residency_on_detection`, and
   every rule that fires is named on the response and the trace. `onprem_only` +
   `passthrough` is a valid, honest configuration; so is `onprem_only` +
   `model_input: raw` + `client_output: redact` (trusted in-boundary compute, safe
   client response) — the configuration the single-switch design could not express.
   A no-protection posture is never silent: it is folded into `app_policy_version`,
   stamped on every response, and emitted on the trace, along with the full
   per-axis decision certificate (`sp.placement`, `sp.model_input_mode`,
   `sp.client_output_mode`, `sp.telemetry_io_mode`, `sp.detection_mode`,
   `sp.cross_axis_rules_fired`, …). See `docs/reference/per-app-config.md`.
3. **`onprem_only` is absolute.** If policy sets it, a cloud provider must be
   unreachable by construction — filtered out of the candidate pool before the router
   scores anything. Never a post-hoc check. Unchanged by rule 2a: `policy_mode`
   has no bearing on placement.
4. **RouteForge hosts no compute; it orchestrates and consumes.** It routes. It never stands
   up or scales inference — it points at OpenAI-compatible endpoints that operators onboard.
   The Improvement Plane is **orchestrated, not hosted**: RouteForge may capture training
   substrate, trigger an external training/distillation job, and onboard the resulting model,
   but it runs no training compute and has no in-process promotion path. Onboarding remains the
   only path a model enters.
5. **Fail closed.** If the policy gate errors, reject the request. Never fall through
   to "no policy = allow".
6. **Headroom CCR stays inside the residency boundary.** CCR caches *originals*
   locally for retrieval — that cache is a second copy of sensitive data. It must
   never leave the approved boundary, never be exported to telemetry, and must have
   an enforced TTL. If residency can't be guaranteed for a tenant, disable CCR or
   bypass compression for them.
7. **Compression runs after the gate, never before.** Headroom must only ever see
   sanitized content. Moving it earlier puts raw PII into the CCR cache.
8. **Residency beats the risk floor.** The residency filter runs before the risk
   floor. A high-risk `onprem_only` request floors to the best *allowed* (on-prem)
   tier and sets `require_verification` — it is **never** lifted to a cloud tier to
   satisfy the floor. (High-risk + PHI is the hospital persona — the most important case
   to get right; both step 6 routing and step 9 verification obey this.)

## Architecture — the ten steps

Each is a separate module with a narrow interface. Keep them swappable; the product's
value is showing the seams.

| # | Step | Module | Notes |
|---|------|--------|-------|
| 1 | Incoming Request | `app/api/routes.py` | FastAPI; OpenAI-compatible `/v1/chat/completions` |
| 2 | Identity, Privacy & Policy Gate | `app/policy/gate.py` | Presidio + NeMo Guardrails + OPA-style rules. **Runs first.** |
| 3 | Execution Policy Context | `app/policy/context.py` | Immutable; travels with request |
| 4 | Task Mode Router | `app/routing/task_mode.py` | always **direct**; agent loops are client-driven |
| 5 | Conditional Context Optimization | `app/compression/` | **Headroom.** Compress tool outputs/history. Conditional. |
| 6 | Adaptive Model & Backend Router | `app/routing/router.py` | Every LLM call. Scores **post-compression** cost |
| 7 | Model Gateway & Provider Controls | `app/gateway/` | Adapters, quotas, breakers, fallback |
| 8 | Approved Inference Serving Paths | `app/gateway/backends/` | Onboarded on-prem endpoints (via `ModelRegistry`) + managed cloud. routeForge deploys none of it — see `docs/guides/onboarding-models.md` |
| 9 | Output Controls & Verification | `app/output/verify.py` | Leakage checks, escalation, sampling |
| 10 | Governed Response / Action | `app/api/routes.py` | Response + full decision trace |

Agent loops belong to the calling application. Every model call an application's loop
makes arrives as its own request and re-enters steps 2–9, so compression (5) and the
router (6) stay **per-call**. This is the single most important behaviour to get
visibly right in the UI.

**Inbound streaming is per-app, floored by step 9.** Step 9 verifies the *complete*
response, which token streaming can't satisfy without a partial-leak window — so
`stream: true` is still rejected by default (`server.reject_stream_requests`) and for
any app with no `stream_mode` set. An app may opt in via `AppPolicy.stream_mode`
(`off | verified_then_stream | stream_unverified | incremental_verify`):
`verified_then_stream` runs the full verified pipeline and chunks the finished answer
(guarantee intact, no latency win); `stream_unverified` streams real backend tokens and
explicitly waives verification (recorded in the trace); `incremental_verify` streams
real tokens through a hold-back redactor that never releases raw entity text across a
chunk boundary, but cannot enforce a whole-response verdict. The floor is absolute and
independent of the app's choice: `onprem_only` or `require_verification` can never waive
step-9 verification — those requests fall back to `verified_then_stream` regardless of
`stream_mode`. The UI's decision stream (SSE of DecisionRecords) is a separate channel
and stays on. See `docs/reference/per-app-config.md`.

**Routing scopes** — three distinct routers; don't conflate them:
- **Switchyard** (step 6) selects model + backend
- **vLLM Semantic Router** (step 8) selects a model path within a vLLM pool
- **Dynamo** (step 8) selects/scales inference workers and KV placement

**Routing observability & signals** (shipped `0.11.0`; see
`docs/reference/routing-observability.md`): (1) `GET /metrics` is an in-process Prometheus
exporter — five `routeforge_*` families, bounded labels only, **no
tenant/PII**, opt-in `observability` extra (503 without it), no bundled server;
(2) every trace carries `sp.candidates` (chosen + rejected set) and
`sp.routing_policy_version` (routing+pricing content hash), emitted regardless
of `capture_io`; (3) since P2a, each router hop also carries `sp.scoring` — every
**eligible** candidate's four scoring terms with, per term, the value used and
where it came from (`measured` / `tier_default` / `neutral` / `disabled`). The
record is sufficient to recompute that hop's ranking offline; the response
certificate carries the chosen candidate's terms only. It says nothing about
whether the ranking was *good* — only what it was and what it rested on. Boot
seeding replays the per-attempt `endpoint_observations` log (endpoint health
only — no tenant or content), so a seeded signal is a reconstruction, across
replicas, of what the live stores saw, at its true age. Since P2b-2, every routed request
also writes a content-free `routing_outcomes` record (plus local feedback scores) that
nothing reads back into routing — see `docs/reference/routing-outcomes.md`. Since P3a,
`GET /admin/config-snapshot` exports a content-free config snapshot that the offline
`scripts/policy_simulate.py` replays those records against, to predict a policy change's
effect before anyone applies it — see `docs/reference/policy-simulator.md`.

## Configuration — everything switchable at startup

Four YAMLs, env-var overridable. **No code changes to switch modes.** **One owner per
concern** — never restate a value across files; the config lint (below) fails the boot
if you do:

| File | Owns |
|---|---|
| `config/serving.yaml` | Infrastructure switches only: compression infra, observability, provider on/off, server |
| `config/policy.yaml` | Budgets, the sensitivity enum + default, residency, tenants |
| `config/routing.yaml` | Scoring weights, thresholds, the canonical `tiers` list, `model_pool`, escalation |
| `config/pricing.yaml` | Per-model USD/1M and the savings baseline |

```yaml
# serving.yaml — infra only
context_optimization:
  enabled: false               # Headroom. Default OFF; operators toggle to measure before/after
  ccr: {enabled: true, residency: local_only}
observability: {langfuse: {enabled: true}}
policy: {pii_detection: presidio}   # presidio | regex | off
providers:                     # availability only; tier membership lives in routing.yaml
  onprem:  {enabled: true, litellm_prefix: openai, api_key_env: SP_ONPREM_API_KEY}
  azure:   {enabled: false}    # managed_cloud path
  bedrock: {enabled: false}    # managed_cloud path
```

On-prem models are onboarded at runtime, not declared in YAML — see
`docs/guides/onboarding-models.md`. `providers.onprem.enabled` is an infra kill-switch, not a
backend selector: on-prem endpoints, prices, and tiers come from the `ModelRegistry`
(YAML cloud pool ∪ enabled onboarded rows), never a static `base_url` here.

Onboarding also records three **data-handling declarations** per endpoint — `region`,
`prompt_retention`, `accepts_raw_sensitive` — resolved per-model → provider default →
`unknown`, which an app's `allowed_regions` / `require_no_retention` are enforced against
in `app/policy/feasible.py` before the router scores anything. **RouteForge records what
an operator declared about an endpoint and enforces policy against that declaration — it
cannot verify a provider's contractual claim.** See
`docs/guides/onboarding-models.md` §(e2) and `docs/reference/per-app-config.md`.

**Canonical values that used to drift — now single-sourced:**
- Provider name is `onprem` (one word) in every file. Residency flag is `onprem_only`.
- Tiers are exactly `cheap | mid | frontier`. There is **no `strong` tier**.
- Sensitivity enum is `public | internal | confidential | restricted`, defined once in
  `policy.yaml`.
- There is no `inference_backend` axis and no separate `serving_paths` block. On-prem
  candidates are whatever the operator has onboarded (`docs/guides/onboarding-models.md`);
  cloud path availability = `providers.{azure,bedrock}.enabled`.

### Config lint at startup — the highest-leverage guardrail

A Pydantic settings layer loads all four YAMLs and **cross-validates**, exiting
non-zero with a named violation on any of:
- provider names identical across `serving`/`routing`/`pricing` (`onprem` everywhere);
- every tier named in `risk_floors` and every `model_pool` tier exists in the canonical
  `tiers` list. An empty YAML tier is legal — on-prem tiers are populated by
  onboarding, not the static pool, so there is no "every tier needs ≥1 YAML member"
  check any more;
- every `model_pool` entry has a price in `pricing.yaml`;
- no concern owned by two files (no duplicated `max_spend_usd`, thresholds, etc.);
- one sensitivity enum, used by name wherever sensitivity is matched.

This turns every drift class into a **boot failure instead of a silent misconfiguration**,
and keeps working as configs evolve. It is the single highest-leverage guardrail in the
config layer.

**No on-prem model onboarded ⇒ derived reject, not a backend value.** There is no
`inference_backend: none` to set any more. Instead: if no onboarded model is currently
`enabled`, `ModelRegistry.has_healthy_onprem()` is false, which drops `onprem` from
`allowed_providers` in the policy context — an `onprem_only` request then has no
eligible candidate and is **rejected**, never silently sent to cloud. This is the same
privacy guarantee the old `none` backend gave; it now falls naturally out of onboarding
state (empty pool, or every onboarded model unreachable/disabled) instead of a static
config flag. See `docs/guides/onboarding-models.md` and the `inference-backends` skill.

## Tech stack — use these, don't substitute

- **Python 3.12**, FastAPI, Pydantic v2, `uv` for deps
- **Headroom** (`headroom-ai`) — conditional context compression, step 5
- **Presidio** — PII detection
- **Langfuse** — self-hosted via compose; RouteForge exports traces to it through its
  own OpenTelemetry exporter (`app/obs/export.py`)
- **React + Vite + Tailwind + Recharts** — UI
- **Docker Compose** (single-node) and **Helm** (Kubernetes / GreenLake)

## Skills available

Read the relevant skill before working on that area — they carry constraints not
repeated here:

- `.claude/skills/privacy-gate/` — steps 2/3, Presidio, redaction, fail-closed, redaction map
- `.claude/skills/headroom-compression/` — step 5, Headroom, CCR, bypass rules
- `.claude/skills/routing-and-gateway/` — steps 6/7/8 (+ step-4 task mode), scoring, adapters, confidence
- `.claude/skills/output-controls/` — step 9, leakage checks, `require_verification`, streaming policy
- `.claude/skills/inference-backends/` — what an operator runs behind an onboarded on-prem endpoint (vLLM/Dynamo/TensorRT-LLM); owns the full Dynamo skills list
- `.claude/skills/demo-ui/` — flow visualizer + cost dashboard
- `.claude/skills/traffic-and-tests/` — personas, traffic generator, test suite
- `.claude/skills/greenlake-deploy/` — Docker, Helm, HPE GreenLake

NVIDIA also publishes Dynamo skills — install them rather than reinventing. The **full
list lives in the `inference-backends` skill** (single source); it's the one to read
before any Dynamo work.

## Build order

The product is built. This is how it was layered — and the dependency order to respect
when extending it. Each layer rests on the ones above.

1. **Skeleton** — FastAPI, config loader **with the cross-file config lint** (see
   Configuration above — build this first; it makes every drift class a boot failure),
   ten module stubs, `/health`. Verify boot.
2. **Policy gate** — Presidio, policy context, fail-closed. **Test before routing.**
3. **Router + mock gateway** — scoring, model pool, decision trace.
4. **Observability** — OpenTelemetry + Langfuse; confirm redaction actually happened.
5. **Agent runtime** — **removed 2026-09-17 (agent loops are client-driven)**.
5b. **Compression** — Headroom behind the flag; verify router prices post-compression
    tokens and CCR stays local.
6. **UI** — flow visualizer, then cost dashboard.
7. **Traffic + tests** — personas, generator, branch assertions.
8. **Packaging** — Dockerfile, compose, Helm.
9. **Real backends** — operators onboard their own on-prem endpoints (vLLM/Dynamo/NIM/
   any OpenAI-compatible server) at runtime; routeForge deploys none of it.

## Sales / evaluation walkthrough

This is **one optional demonstration path** — a five-minute story for a sales or
evaluation session. It is not the point of the product and not something feature work
must serve; it's a useful end-to-end exercise of every branch.

**Before the walkthrough:** the on-prem pool boots empty — there is no bundled model.
The operator onboards their own on-prem endpoint first (Infrastructure → Onboard, or
`POST /admin/infra/models`; see `docs/guides/onboarding-models.md`) so step 1 below has a
cheap tier to land on.

1. Simple question → cheap local model → fractions of a cent.
2. Question with an SSN → gate detects PII → `onprem_only` → **UI shows cloud
   providers greyed out as ineligible** → served locally → Langfuse trace shows
   redaction.
3. **Toggle Headroom on** → re-run a long-context request (the `large_context`
   persona) → the pasted context compresses, token count drops sharply, same answer.
   Two savings levers, shown separately.
4. Run 500-request traffic burst → dashboard shows ~70% to cheap tier, and cost saving
   vs all-frontier baseline split into **routing savings** and **compression savings**.
5. Open Langfuse → full trajectory, cost, latency, zero raw PII, no CCR contents.

## Coding conventions

- Every step that makes a decision emits a `DecisionRecord` (what was decided, why,
  alternatives rejected, cost, latency). The UI and Langfuse both consume it. **The "why"
  is the whole point.** (Step 4 is the exception: it is a constant `direct` and emits
  none.)
- Type-hint everything; Pydantic models at boundaries.
- No secrets in code or compose. `.env` only, `.env.example` committed.
- Structured JSON logs with a `request_id` correlating to the Langfuse trace.
- Prefer boring, readable code over clever code. It gets read by customers' engineers
  and on a projector.

## Pitfalls seen in this build

- Don't let the router see a request the gate hasn't cleared — easy to break when
  refactoring, and it silently invalidates the whole privacy claim.
- Don't compute cost only at the end; accumulate per call or the totals lie.
- Don't make the UI poll for state; stream decisions over WebSocket/SSE or the flow
  visualizer feels dead.
- Don't hard-code provider names in the router; read the pool from the
  `ModelRegistry` or the empty-on-prem-pool case breaks.
- Don't let the router score pre-compression token counts — every cost figure becomes
  wrong, and it's invisible until someone checks the math.
- Don't attribute all savings to routing once Headroom is on. Track the two levers
  separately or the customer can't tell which one is working.
