# Architecture

How RouteForge works today. **Every claim in this directory is checkable
against HEAD** — it was verified during the 2026-09-10 docs reclassification
sweep.

## `architecture/` vs `decisions/` vs `specs/`

Three subdirectories, three different kinds of trust:

- **`architecture/`** (this directory) — descriptive. What RouteForge does
  today, checkable against the current code and diagrams.
- **`architecture/decisions/`** — a dated scope call: why RouteForge does or
  doesn't do X, and when that call was made. A decision doesn't expire just
  because time passes; if a later decision supersedes an earlier one, the
  superseded doc carries a banner saying so rather than being deleted.
- **`architecture/specs/`** — a binding contract for a shipped feature, cited
  by path from the application code that implements it. Treat these as
  load-bearing: a change to the code that violates one should update the spec
  in the same change.

## Files in `architecture/`

| Doc | Summary |
|---|---|
| [`logical-and-request-flow.md`](logical-and-request-flow.md) | Companion notes to the draw.io diagrams — the ten Serving-Plane steps and the end-to-end request/response flow. |
| [`sample-request-flows.md`](sample-request-flows.md) | Worked examples: real requests walked through the ten steps, branch by branch. |
| [`routing-tier-selection.md`](routing-tier-selection.md) | How the step-6 router actually picks a tier and model (length-based complexity, not semantic difficulty) and what it can't key on (no modality/capability axis). |
| [`api-load-balancing.md`](api-load-balancing.md) | How the `api` deployment scales horizontally — L4/L7 balancing layers and why per-pod in-memory state was a bug. |

## Files in `architecture/decisions/`

| Doc | Summary |
|---|---|
| [`inference-layer-boundary.md`](decisions/inference-layer-boundary.md) | Where RouteForge stops and the serving layer begins — the recurring "does RouteForge do llm-d / DRA / Gateway API / prefill-decode?" question, and why RouteForge doesn't deploy Dynamo. |
| [`compression-in-scope.md`](decisions/compression-in-scope.md) | Why context compression (Headroom, step 5) stays inside RouteForge rather than moving to the calling application, unlike agent-harness self-improvement and RLHF. |
| [`app-vs-routeforge-boundary.md`](decisions/app-vs-routeforge-boundary.md) | The general placement test for what belongs in the calling application vs. RouteForge, worked through client-side compressors, the semantic cache, and the two families of "prompt caching". |
| [`per-app-config-flexibility.md`](decisions/per-app-config-flexibility.md) | Which Serving-Plane settings can safely become per-application overrides, and the rule for classifying any new per-app knob. |
| [`self-improving-agent-feature-fit.md`](decisions/self-improving-agent-feature-fit.md) | The 2026-08-17 control-plane-vs-compute boundary decision: where self-improving-agent research (memory, prompt-opt, distillation) lands vs. RouteForge's scope — orchestrated, not hosted. |

## Files in `architecture/specs/`

| Doc | Summary |
|---|---|
| [`semantic-cache-residency-spec.md`](specs/semantic-cache-residency-spec.md) | Binding residency + correctness contract for the semantic cache (`app/cache/store.py`) — a cached answer is a second copy that must stay CCR-class and residency-partitioned. |
| [`persistent-memory-residency-spec.md`](specs/persistent-memory-residency-spec.md) | Binding residency contract for persistent memory (mem0-style, step-5 extension) — the CCR-class guarantees a cross-request memory store must honor. |

## Diagrams

See [`diagrams/README.md`](diagrams/README.md) for the three draw.io diagrams
(logical architecture, physical architecture, request flow) and how to
regenerate them from their JSON specs.

`llm orchestration loop.drawio` (this directory) is the original diagram,
superseded by the three above — retained only because
[`decisions/self-improving-agent-feature-fit.md`](decisions/self-improving-agent-feature-fit.md)
cites its "Prompt / Instructions — DSPy / NAT optimizer" node as evidence that
prompt optimization was in the design from day one; that node has no
counterpart in any of the three current diagrams, so it is the one thing the
old file still shows that the new ones don't.

## Related docs in other buckets

- **NeMo Switchyard vs. our step-6 router** — what to borrow, what to refuse,
  and the (deferred, not rejected) "should we replace our router with it?"
  question: [`../comparisons/nemo-switchyard-comparison.md`](../comparisons/nemo-switchyard-comparison.md),
  with the action-oriented incorporation plan in
  [`../comparisons/switchyard-features-to-incorporate.md`](../comparisons/switchyard-features-to-incorporate.md).
- **The cost gap left by provider-side prompt caching** (the lossy half of the
  `app-vs-routeforge-boundary.md` decision above):
  [`../comparisons/caching-cost-feature-fit.md`](../comparisons/caching-cost-feature-fit.md).
- **Which features are IP-worthy, and what prior art forecloses**:
  [`../roadmap/ip-and-research-opportunities.md`](../roadmap/ip-and-research-opportunities.md),
  with the reviewer hand-off brief at
  [`../roadmap/ip-review-handoff.md`](../roadmap/ip-review-handoff.md).

## Canonical step count: 1–10

The Serving Plane is **steps 1–10** (1 Incoming Request → 10 Governed Response). All
prose uses this count — not "1–8" or "1–9". The Improvement Plane (training,
evaluation, promotion, registry) is out of scope and not in this diagram.

## Serving paths: on-prem models are onboarded at runtime

Step 8 shows three serving paths — **Dynamo + TensorRT-LLM**, **vLLM + Semantic
Router**, and **managed cloud** (Azure/Bedrock). There is no deterministic
canned-response backend: the old `mock` backend was **removed** in the LiteLLM gateway
swap (2026-07-30), when LiteLLM became the single egress path for every provider. Tests
that need determinism fake the LiteLLM SDK call itself (`tests/_litellm_fake.py`), not a
mock backend class.

The `inference_backend: vllm | dynamo | none` axis was **removed** in the pure-router <!-- purged-ok -->
pivot (2026-08-03). routeForge no longer picks an on-prem backend in YAML — on-prem models
are **onboarded at runtime** (each with its own OpenAI-compatible `endpoint`) and live in
the `ModelRegistry`; see `docs/guides/onboarding-models.md`. The on-prem pool boots empty. The
managed-cloud path's availability is still `providers.{azure,bedrock}.enabled`, and there
is **no** separate `serving_paths` config axis. <!-- purged-ok -->
See [architecture-review.md](../archive/2026-07-25-architecture-review.md) for the pre-pivot config shape
this replaced.

So the honest "what's real vs simulated?" answer is: onboarded vLLM/Dynamo endpoints =
real on-prem paths; `managed_cloud` = real cloud path; **nothing here is a demo-safe
simulation.** If no on-prem model is enabled, `onprem_only` requests are rejected
(derived — `has_healthy_onprem()` is false), never a silent cloud fallback. See the
`inference-backends` skill.

## Redaction map return path — placeholders, and nothing else

The gate (step 2) replaces entities with placeholders (`<SSN_1>`) and keeps the map
internal. **There is no detokenization.** The map is built during `anonymize()` and then
discarded; nothing re-inserts an original value, there is no broker, and there is no
config switch that turns one on. Placeholders are the deliverable, deliberately — they
show redaction working — and `model_input: "tokenize"` /
`client_output: "authorized_rehydrate"` are rejected with a 422
(`AXIS_VALUE_NOT_IMPLEMENTED`) rather than faked.

The consequence worth stating: **a redacted value cannot be a lookup key.** An app whose
tool must find a record by SSN needs `model_input: "raw"` — the axis exists for exactly
that — not a placeholder. See the `privacy-gate` and `output-controls` skills, and
`docs/reference/per-app-config.md` for the axis.
