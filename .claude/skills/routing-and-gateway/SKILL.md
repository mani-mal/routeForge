---
name: routing-and-gateway
description: Use when implementing steps 6-8 of the Serving Plane - the adaptive per-call model router, scoring logic, model gateway, provider adapters, the approved model pool, and step-4 task mode (a constant direct decision - there is no classifier and no multi-step branch). Trigger on mentions of routing, model selection, scoring, tiers, gateway, provider adapter, fallback, escalation, task mode, or confidence.
---

# Adaptive Router & Gateway (Steps 6–8)

Covers step 6 (adaptive router), step 7 (gateway), step 8 (serving-path selection),
and step-4 task mode — which is a **constant `direct` decision**, not a classifier
(see "Task mode (step 4) is a constant" below). Compression is **step 5** — it owns pricing
inputs; see the `headroom-compression` skill. Router scores on the token counts
compression produces (below).

## Price post-compression tokens

If Headroom (step 5) is enabled, the router **must** score on `tokens_after`, not the
raw request size. Scoring pre-compression counts makes every cost number in the demo
wrong, and it fails silently — nobody notices until they check the math.

```python
est_cost = candidate.price_per_1k * (ctx.post_compression_tokens / 1000)
```

Keep routing savings and compression savings as **separate** counters. When both are
on, the customer needs to see which lever did what.

**Canonical tokenizer (avoids the 5↔6 cycle).** Compression must run *before* routing
so the router can price real counts, but Headroom compresses "for a target model"
whose tokenizer isn't known until routing picks it — a cycle if taken literally. Break
it: compress **once against a canonical tokenizer** (cl100k / the
cheap tier's), treat `est_cost` as an estimate, and record the *actual* count from the
response into `actual_cost_usd`. Do **not** iterate compression↔routing for marginal
per-model token accuracy. Per-model recompression is a known, documented approximation.

## Per-call, not per-request

The router runs on **every LLM call**. RouteForge answers a request with one model
call and runs no agent loop of its own, so "per-call" means: an application's 4-hop
agent loop sends 4 separate requests, hits the router 4 times, and may pick a
different tier each time. Make this visible in the UI — it's what separates this from
a front-door classifier.

## Scoring

```python
def score(candidate, ctx, task) -> float:
    if candidate.provider not in ctx.allowed_providers:
        return -inf          # defensive; gate should have filtered already
    if candidate.est_cost > ctx.max_cost_usd:      return -inf
    if candidate.est_latency_ms > ctx.max_latency_ms: return -inf

    return (W_QUALITY * expected_quality(candidate, task)
            - W_COST    * normalized_cost(candidate)
            - W_LATENCY * normalized_latency(candidate)
            + W_CAPACITY * availability(candidate))
```

Weights live in `config/routing.yaml`. Note this router is **complexity/risk-tiered, not
cost-tiered** — the target tier is `max(complexity_tier, risk_floor, min_tier)`, then
cheapest-within-tier — so a cost-weight slider would not honestly shift tiers and is
deliberately not shipped (see the `demo-ui` skill's deferred-controls note).

## Hard rules over scoring

Two overrides that are not negotiable by score:

1. **Risk floor, applied after the residency filter — residency beats risk.**
   `risk_floors` (in `config/routing.yaml`) sets the minimum tier a request may use:
   `high → frontier`, `medium → mid`, `low → cheap`. But the residency filter runs
   **first**: for an `onprem_only` request the cloud tiers are already gone, so a
   high-risk `onprem_only` request floors to the best *allowed* (on-prem) tier and
   sets `require_verification: true` (step 9) — it is **never** lifted to a cloud
   tier to satisfy the floor. This is the hospital persona (high-risk + PHI); getting
   it wrong either sends PHI to the cloud or targets an empty tier.
2. **Confidence threshold.** Below `confidence_escalation_threshold`, escalate one
   tier and re-run. Cap at `max_escalations` (default 1) or cost is unbounded.

## What "confidence" is

The escalation signal is **backend-reported**, not a mystery scalar:

- Tests: the faked LiteLLM SDK (`tests/_litellm_fake.py`) emits a seeded confidence per
  response (deterministic by prompt hash) so the `low_confidence` persona reliably
  triggers exactly one escalation.
- Real backends: mean token logprob when the backend exposes it; otherwise `1.0`
  (never escalate on missing signal — fail toward *not* spending more).
- If a judge model is ever used instead, that judge call goes **through the gateway**
  like any other LLM call (steps 5–7), respects `allowed_providers`, and emits its own
  DecisionRecord. Don't add an unrouted, unpriced side-channel call.

## Task mode (step 4) is a constant

`app/routing/task_mode.py::decide()` always returns `"direct"`. There is no
multi-step branch and no classifier: the internal agent runtime and the optional LLM
task-mode classifier were removed 2026-09-17, because agent loops belong to the
calling application. The step stays in the architecture and in the console's step
list, and it emits **no** `DecisionRecord`.

If step 4 ever becomes a real decision again, it is an LLM call like any other: it
goes through steps 5–7, respects `ctx.allowed_providers` (so it can't silently break
`onprem_only`), and emits a DecisionRecord so its cost is visible.

## Model pool

The **effective** pool is the `ModelRegistry`'s merge of the static YAML pool
(`config/routing.yaml`'s `model_pool` + `tiers` — cloud built-ins) with every currently
**enabled** onboarded on-prem row (`docs/guides/onboarding-models.md`). `routing.yaml` is
still the single source of truth for the cloud pool and the canonical tier list — do
not restate it here or in code. There are exactly three tiers: `cheap | mid | frontier`
(there is no `strong` tier). Read the pool through the registry and provider names from
config — never hard-code provider names, or the empty-on-prem-pool case (no model
onboarded yet, or every onboarded model disabled) breaks. See `config/pricing.yaml` for per-model costs.

## Eligibility is one seam — `app/policy/feasible.py`

Nothing in the router, the gateway or the gate re-derives which candidates a request may
reach. `feasible_set()` is the single authority, and it runs a fixed, ordered sequence of
stages, each with a stable `code`:

1. **residency / provider placement** (`residency`) — absolute, and always first
   (CLAUDE.md rule 3).
2. **the endpoint's data-handling declarations** — `region_not_allowed`,
   `prompt_retention_not_allowed`, `raw_sensitive_not_accepted`.
3. **prior failure on this request** (`prior_failure`).
4. **the app's `allowed_tiers` / `allowed_models`** (`app_scope`).
5. **the request's pinned `model`** (`not_requested`).
6. **the registry's per-model gates** (`model_scope`).

Stage 2 compares an application's requirement against what an **operator declared** about
an endpoint: `region`, `prompt_retention`, `accepts_raw_sensitive`, resolved once in
`ModelRegistry.effective_pool()` (per-model value → provider default → `unknown`).
**RouteForge records what an operator declared about an endpoint and enforces policy
against that declaration — it cannot verify a provider's contractual claim.**

Two things about stage 2 that look like bugs and are not:

- **`unknown` excludes for region and retention, but is kept by the raw-sensitive rule.**
  The first two fire because *the app declared a requirement* and an endpoint that cannot
  show the property does not qualify. The third has no app-declared requirement behind it
  — it comes from the request's own shape (a `raw` `model_input` over a better-than-public
  sensitivity) — so it fires **only on a declared `false`**, or it would retire every
  endpoint in every existing deployment. Do not "fix" this into uniformity.
- **These constraints may only narrow.** No value of any of them can add a candidate back,
  reach a provider residency excluded, or touch `allowed_providers` / `onprem_only`.

Failover and confidence escalation draw from the **same compiled feasible set**, so a
candidate excluded on the first pass is structurally unreachable on a retry — that is what
makes the "cloud fallback on an `onprem_only` request" bug below impossible by
construction rather than by a repeated check. An empty set is a **reject with reasons**,
never a relaxation.

Full reference: `docs/reference/per-app-config.md` (rules, codes, the app-side
`allowed_regions` / `require_no_retention`) and `docs/guides/onboarding-models.md` (the
declarations and their two input shapes).

## DecisionRecord

Every routing decision emits this. It drives the UI and Langfuse both.

```python
class DecisionRecord(BaseModel):
    step: str
    chosen: str
    reason: str                       # human-readable — this is the demo
    rejected: list[RejectedCandidate] # each with a reason
    est_cost_usd: float
    actual_cost_usd: float | None
    latency_ms: float
    hop_index: int                    # index within this request's trace
```

`rejected` with reasons is what makes the decision legible on a projector. Populate
it properly, including "excluded by policy" entries.

## Serving paths (step 8)

The gateway selects a *provider*; the serving path decides *how* inference runs. Three
routers exist in this system and conflating them causes confusion:

| Router | Scope |
|---|---|
| Switchyard / our router (6) | model + backend |
| vLLM Semantic Router (8) | model path within a vLLM pool |
| Dynamo (8) | inference workers, scaling, KV placement |

There is no `inference_backend` axis and no separate `serving_paths` block. On-prem
candidates come from the `ModelRegistry` — whatever the operator has onboarded and
enabled (`docs/guides/onboarding-models.md`); routeForge deploys none of what's behind an
onboarded endpoint, whether that's a bare vLLM server, a Dynamo-fronted TensorRT-LLM
pool, or NIM. The managed-cloud path's availability is `providers.azure.enabled` /
`providers.bedrock.enabled`, independent of on-prem onboarding state. See
`inference-backends` skill for what operators run behind an endpoint.

## Gateway (step 7)

One provider set behind a single `LiteLLMBackend` interface: `onprem` (onboarded
endpoints), `azure`, `bedrock` — there is no `mock` provider. Multi-tenant controls —
virtual keys, per-tenant budget, timeout, retry with
backoff, circuit breaker (open after N consecutive failures), and fallback to the
next-best *policy-permitted* candidate.

Fallback must re-check `ctx.allowed_providers`. A cloud fallback on an `onprem_only`
request is the single worst bug the product could have.

## Cost accounting

Accumulate per hop. Track actual token counts from the response, not estimates.
Maintain a parallel `baseline_cost_usd` = what the same traffic would have cost
all-frontier. The dashboard's headline number is the delta.

## Tests

- `onprem_only` → only on-prem candidates scored; cloud excluded with reason
- High risk (cloud allowed) → `frontier` tier even when cheap scores higher
- High risk + `onprem_only` → best on-prem tier + `require_verification`, **never** cloud
- Low confidence → exactly one escalation, then stop
- Circuit breaker opens after N failures, falls back within policy
- Task mode always resolves `direct` (agent loops are client-driven); each call
  is its own request and emits its own DecisionRecord
