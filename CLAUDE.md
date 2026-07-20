# CLAUDE.md — Serving Plane Prototype

## What this project is

A **demo-grade prototype of the Serving Plane only** (steps 1–10 of the Unified LLM
Orchestration architecture — the diagram's canonical count; use "steps 1–10"
everywhere, not "1–8" or "1–9"). It is shown to customers to prove three claims:

1. **Privacy-first** — sensitive requests never leave the customer's infrastructure.
2. **Cost reduction** — per-call routing sends most traffic to cheap/local models.
3. **Observable** — every decision is captured in Langfuse via NeMo Agent Toolkit.

**Out of scope.** The Improvement Plane (training, evaluation gates, promotion,
registry) is NOT built here. Do not add it. If asked to "close the loop", say it is
out of scope and stop.

## Definition of done

- `docker compose up` yields a working demo in under 5 minutes on a laptop with no GPU.
- Traffic generator produces >=500 requests exercising every branch.
- UI shows, per request: branch taken, policy verdict, model chosen, cost, latency.
- Cost dashboard shows spend vs. an "all-frontier" baseline.
- Langfuse contains every trace, with PII redacted.
- Helm chart deploys to HPE GreenLake (K8s) with the same behaviour.

## Non-negotiable rules

These come from the architecture's hard rules. Violating them breaks the demo's story.

1. **Nothing external before the policy gate.** No classifier, no cloud model, no
   tool, and no telemetry sink sees the raw request until step 2 completes. Ordering
   is load-bearing, not stylistic.
2. **Telemetry is redacted on egress.** Redact at the exporter, not only at intake.
   Tool outputs and model completions leak PII the input gate never saw.
3. **`onprem_only` is absolute.** If policy sets it, a cloud provider must be
   unreachable by construction — filtered out of the candidate pool before the router
   scores anything. Never a post-hoc check.
4. **No auto-deploy of anything.** Prototype has no promotion path. It serves only.
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
   satisfy the floor. (High-risk + PHI is the hospital persona, the demo's most
   important one; both step 6 routing and step 9 verification obey this.)

## Architecture — the ten steps

Implement each as a separate module with a narrow interface. Keep them swappable;
the demo's value is showing the seams.

| # | Step | Module | Notes |
|---|------|--------|-------|
| 1 | Incoming Request | `app/api/routes.py` | FastAPI; OpenAI-compatible `/v1/chat/completions` |
| 2 | Identity, Privacy & Policy Gate | `app/policy/gate.py` | Presidio + NeMo Guardrails + OPA-style rules. **Runs first.** |
| 3 | Execution Policy Context | `app/policy/context.py` | Immutable; travels with request |
| 4 | Task Mode Router | `app/routing/task_mode.py` | direct vs multi-step. NAT Router Agent; **rules-first, classifier off** for the prototype |
| — | Agent Runtime / Orchestrator | `app/agent/runtime.py` | NAT: state, tools, caps, checkpoint/resume |
| 5 | Conditional Context Optimization | `app/compression/` | **Headroom.** Compress tool outputs/history. Conditional. |
| 6 | Adaptive Model & Backend Router | `app/routing/router.py` | Every LLM call. Scores **post-compression** cost |
| 7 | Model Gateway & Provider Controls | `app/gateway/` | Adapters, quotas, breakers, fallback |
| 8 | Approved Inference Serving Paths | `app/gateway/backends/` | Dynamo+TensorRT-LLM / vLLM+SR / managed cloud |
| 9 | Output Controls & Verification | `app/output/verify.py` | Leakage checks, escalation, sampling |
| 10 | Governed Response / Action | `app/api/routes.py` | Response + full decision trace |

Multi-step (step 4 → agent runtime) uses **NeMo Agent Toolkit**. Every LLM call the
agent makes re-enters compression (5) and the router (6) — both are **per-call**, not
per-request. This is the single most important behaviour to get visibly right in the UI.

**The step-4 classifier is a routed call.** If the rules-first classifier is ever
replaced by an LLM classifier, that call goes through steps 5–7 like any other:
sanitized input, respects `allowed_providers` (so it can't break `onprem_only`), and
emits a DecisionRecord so its cost isn't invisible. Keep it off for the prototype.

**No inbound streaming in the demo.** Step 9 verifies the *complete* response, which
token streaming can't satisfy without a partial-leak window. Reject inbound
`stream: true` with a clear error (`server.reject_stream_requests`). The UI's decision
stream (SSE of DecisionRecords) is a separate channel and stays on. Production
incremental-verification streaming is an open question, out of scope here.

**Routing scopes** — four distinct routers; don't conflate them:
- **NAT Router Agent** (step 4) selects workflow mode
- **Switchyard** (step 6) selects model + backend
- **vLLM Semantic Router** (step 8) selects a model path within a vLLM pool
- **Dynamo** (step 8) selects/scales inference workers and KV placement

## Configuration — everything switchable at startup

Four YAMLs, env-var overridable. **No code changes to switch modes.** **One owner per
concern** — never restate a value across files; the config lint (below) fails the boot
if you do:

| File | Owns |
|---|---|
| `config/serving.yaml` | Infrastructure switches only: `inference_backend`, backends, compression infra, observability, provider on/off, server |
| `config/policy.yaml` | Budgets (default + `agent_budgets`), the sensitivity enum + default, residency, tenants |
| `config/routing.yaml` | Scoring weights, thresholds, the canonical `tiers` list, `model_pool`, escalation |
| `config/pricing.yaml` | Per-model USD/1M and the savings baseline |

```yaml
# serving.yaml — infra only
inference_backend: mock        # mock | vllm | dynamo | none   (mock = demo default)
context_optimization:
  enabled: false               # Headroom. Default OFF so the demo can show before/after
  ccr: {enabled: true, residency: local_only}
observability: {langfuse: {enabled: true}}
policy: {pii_detection: presidio}   # presidio | regex | off
providers:                     # availability only; tier membership lives in routing.yaml
  onprem:  {enabled: true}     # on-prem serving path (per inference_backend)
  azure:   {enabled: false}    # managed_cloud path
  bedrock: {enabled: false}    # managed_cloud path
```

**Canonical values that used to drift — now single-sourced:**
- Provider name is `onprem` (one word) in every file. Residency flag is `onprem_only`.
- Tiers are exactly `cheap | mid | frontier`. There is **no `strong` tier**.
- Sensitivity enum is `public | internal | confidential | restricted`, defined once in
  `policy.yaml`.
- The on-prem serving path is the single `inference_backend` axis; there is no separate
  `serving_paths` block. Cloud path availability = `providers.{azure,bedrock}.enabled`.

### Config lint at startup — build this first (Phase 1)

A Pydantic settings layer loads all four YAMLs and **cross-validates**, exiting
non-zero with a named violation on any of:
- provider names identical across `serving`/`routing`/`pricing` (`onprem` everywhere);
- every tier named in `risk_floors` and every `model_pool` tier exists in the canonical
  `tiers` list, and every tier has ≥1 pool member;
- no concern owned by two files (no duplicated `max_spend_usd`, thresholds, etc.);
- one sensitivity enum, used by name wherever sensitivity is matched.

This turns every drift class into a **boot failure instead of a silent demo lie**, and
keeps working as configs evolve. It is the single highest-leverage thing in Phase 1.

**Backend rules (explicit, per the spec):**

- `mock` — deterministic canned responses, seeded latency/cost. **Default. Demo-safe.**
- `vllm` — route on-prem tier to a vLLM OpenAI-compatible endpoint.
- `dynamo` — use ai-dynamo for load distribution across TensorRT-LLM workers.
- `none` — no local inference; on-prem tier disabled; **`onprem_only` requests must
  then be REJECTED, never silently sent to cloud.** This is a privacy guarantee, not
  a fallback.

If `dynamo` or `vllm` is selected but unreachable at startup: log a clear error and
fall back to `mock` **only if** `allow_backend_fallback: true`; otherwise exit non-zero.
Never silently degrade in a customer demo.

## Tech stack — use these, don't substitute

- **Python 3.12**, FastAPI, Pydantic v2, `uv` for deps
- **NeMo Agent Toolkit** (`nvidia-nat`) — agent runtime + Langfuse telemetry export
- **Headroom** (`headroom-ai`) — conditional context compression, step 5
- **Presidio** — PII detection
- **Langfuse** — self-hosted via compose
- **React + Vite + Tailwind + Recharts** — UI
- **Docker Compose** (demo) and **Helm** (GreenLake)

## Skills available

Read the relevant skill before working on that area — they carry constraints not
repeated here:

- `.claude/skills/privacy-gate/` — steps 2/3, Presidio, redaction, fail-closed, redaction map
- `.claude/skills/headroom-compression/` — step 5, Headroom, CCR, bypass rules
- `.claude/skills/routing-and-gateway/` — steps 6/7/8 (+ step-4 task mode), scoring, adapters, confidence
- `.claude/skills/output-controls/` — step 9, leakage checks, `require_verification`, streaming policy
- `.claude/skills/nemo-agent-toolkit/` — agent runtime, Langfuse wiring, NAT config
- `.claude/skills/inference-backends/` — mock/vLLM/Dynamo/none switching (owns the full Dynamo skills list)
- `.claude/skills/demo-ui/` — flow visualizer + cost dashboard
- `.claude/skills/traffic-and-tests/` — personas, traffic generator, test suite
- `.claude/skills/greenlake-deploy/` — Docker, Helm, HPE GreenLake

NVIDIA also publishes Dynamo skills — install them rather than reinventing. The **full
list lives in the `inference-backends` skill** (single source); it's the one to read
before any Dynamo work.

## Build order

Follow this sequence. Each phase must run before starting the next.

1. **Skeleton** — FastAPI, config loader **with the cross-file config lint** (see
   Configuration above — build this first; it makes every drift class a boot failure),
   ten module stubs, `/health`. Verify boot.
2. **Policy gate** — Presidio, policy context, fail-closed. **Test before routing.**
3. **Router + mock gateway** — scoring, model pool, decision trace.
4. **Observability** — NAT + Langfuse; confirm redaction actually happened.
5. **Agent runtime** — multi-step path; confirm each hop re-enters the router.
5b. **Compression** — Headroom behind the flag; verify router prices post-compression
    tokens and CCR stays local.
6. **UI** — flow visualizer, then cost dashboard.
7. **Traffic + tests** — personas, generator, branch assertions.
8. **Packaging** — Dockerfile, compose, Helm.
9. **Real backends** — vLLM/Dynamo adapters last, behind config.

## Demo narrative — build toward this

The prototype exists to support this five-minute story. If a feature doesn't serve
it, cut it.

1. Simple question → cheap local model → fractions of a cent.
2. Question with an SSN → gate detects PII → `onprem_only` → **UI shows cloud
   providers greyed out as ineligible** → served locally → Langfuse trace shows
   redaction.
3. Complex multi-step task → agent runtime → several hops, each re-entering the
   router → mixed cheap/expensive tiers.
4. **Toggle Headroom on** → re-run the same multi-step task → tool outputs compress,
   token count drops sharply, same answer. Two savings levers, shown separately.
5. Run 500-request traffic burst → dashboard shows ~70% to cheap tier, and cost saving
   vs all-frontier baseline split into **routing savings** and **compression savings**.
6. Open Langfuse → full trajectory, cost, latency, zero raw PII, no CCR contents.

## Coding conventions

- Every step emits a `DecisionRecord` (what was decided, why, alternatives rejected,
  cost, latency). The UI and Langfuse both consume it. **The "why" is the demo.**
- Type-hint everything; Pydantic models at boundaries.
- No secrets in code or compose. `.env` only, `.env.example` committed.
- Structured JSON logs with a `request_id` correlating to the Langfuse trace.
- Prefer boring, readable code over clever code. This gets read on a projector.

## Pitfalls seen in this build

- Don't let the router see a request the gate hasn't cleared — easy to break when
  refactoring, and it silently invalidates the whole privacy claim.
- Don't compute cost only at the end; accumulate per hop or multi-step totals lie.
- Don't make the UI poll for state; stream decisions over WebSocket/SSE or the flow
  visualizer feels dead.
- Don't hard-code provider names in the router; read the pool from config or the
  `none` backend case breaks.
- Don't let the router score pre-compression token counts — every cost figure in the
  demo becomes wrong, and it's invisible until someone checks the math.
- Don't attribute all savings to routing once Headroom is on. Track the two levers
  separately or the customer can't tell which one is working.
