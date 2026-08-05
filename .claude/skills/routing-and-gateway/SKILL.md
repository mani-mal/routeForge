---
name: routing-and-gateway
description: Use when implementing steps 6-8 of the Serving Plane - the adaptive per-call model router, scoring logic, model gateway, provider adapters, the approved model pool, and the task-mode classifier (step 4). Trigger on mentions of routing, model selection, scoring, tiers, gateway, provider adapter, fallback, escalation, task mode, or confidence.
---

# Adaptive Router & Gateway (Steps 6–8)

Covers step 6 (adaptive router), step 7 (gateway), step 8 (serving-path selection),
and the step-4 task-mode classifier. Compression is **step 5** — it owns pricing
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

The router runs on **every LLM call**, including each hop inside a multi-step agent
workflow. A 4-hop agent task hits the router 4 times and may pick a different tier
each time. Make this visible in the UI — it's what separates this from a
front-door classifier.

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

## Task mode (step 4) is a routed call, not a side channel

Step 4 decides direct vs multi-step. Rules-first is enough by default — keep
the LLM classifier **off**. If it is ever turned on, it is an LLM call like any other:
it goes through steps 5–7, respects `ctx.allowed_providers` (so it can't silently
break `onprem_only`), and emits a DecisionRecord so its cost is visible to the
accumulator the demo depends on.

## Model pool

The **effective** pool is the `ModelRegistry`'s merge of the static YAML pool
(`config/routing.yaml`'s `model_pool` + `tiers` — cloud built-ins) with every currently
**enabled** onboarded on-prem row (`docs/onboarding-models.md`). `routing.yaml` is
still the single source of truth for the cloud pool and the canonical tier list — do
not restate it here or in code. There are exactly three tiers: `cheap | mid | frontier`
(there is no `strong` tier). Read the pool through the registry and provider names from
config — never hard-code provider names, or the empty-on-prem-pool case (no model
onboarded yet, or every onboarded model disabled) breaks. See `config/pricing.yaml` for per-model costs.

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
    hop_index: int                    # which agent hop
```

`rejected` with reasons is what makes the decision legible on a projector. Populate
it properly, including "excluded by policy" entries.

## Serving paths (step 8)

The gateway selects a *provider*; the serving path decides *how* inference runs. Four
routers exist in this system and conflating them causes confusion:

| Router | Scope |
|---|---|
| NAT Router Agent (4) | workflow mode: direct vs multi-step |
| Switchyard / our router (6) | model + backend |
| vLLM Semantic Router (8) | model path within a vLLM pool |
| Dynamo (8) | inference workers, scaling, KV placement |

There is no `inference_backend` axis and no separate `serving_paths` block. On-prem
candidates come from the `ModelRegistry` — whatever the operator has onboarded and
enabled (`docs/onboarding-models.md`); routeForge deploys none of what's behind an
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
- 4-hop agent task → 4 DecisionRecords with distinct `hop_index`
