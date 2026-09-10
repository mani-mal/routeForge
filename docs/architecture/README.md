# Architecture diagram — reading notes

`llm orchestration loop.drawio` is the canonical picture of the **Serving Plane**.
Two things the current diagram does not yet draw explicitly; treat these notes as
authoritative until the `.drawio` is redrawn to match.

> **Where routeForge stops and the serving layer begins** — for the recurring
> "does routeForge do llm-d / DRA / Gateway API / prefill-decode?" question and the
> "why routeForge does not deploy Dynamo" decision, see
> [`inference-layer-boundary.md`](./inference-layer-boundary.md).

> **NeMo Switchyard vs. our step-6 router** — what to borrow from NVIDIA NeMo
> Switchyard, what to refuse on scope grounds, and the (deferred, not rejected)
> "should we replace our custom router with it?" decision, see
> [`nemo-switchyard-comparison.md`](./nemo-switchyard-comparison.md). The action-oriented
> per-feature incorporation plan is
> [`switchyard-features-to-incorporate.md`](./switchyard-features-to-incorporate.md).

> **How a request actually gets a tier (and why "easy question → cheap" is really
> "short prompt → cheap"), plus the text-only / no-modality boundary** — for the
> recurring "why did this land on cheap/frontier?" and "can it route text vs image
> models?" questions, see [`routing-tier-selection.md`](./routing-tier-selection.md).

> **Why context compression (Headroom, step 5) lives in RouteForge and not the
> calling app** — for the recurring "the app owns its own harness, so shouldn't
> compression move out like RLHF did?" question, see
> [`compression-in-scope.md`](./compression-in-scope.md).

> **What belongs in the calling application vs. RouteForge** — the general placement
> test ("you cannot move a boundary-enforcing component outside the boundary it
> enforces"), the three-bucket sort (chokepoint / in-boundary lever / the app's own
> business), and the worked cases: client-side compressors like rtk, Headroom, the
> semantic cache, and **the two families of "prompt caching"** (app-side caches are
> invisible and fine; provider-side `cache_control` / `prompt_cache_key` markers pass
> *through* RouteForge and are currently stripped). See
> [`app-vs-routeforge-boundary.md`](./app-vs-routeforge-boundary.md); the cost backlog
> that fixes the lossy half is [`caching-cost-feature-fit.md`](./caching-cost-feature-fit.md).

> **Which Serving-Plane settings can safely become per-application overrides** —
> for "should `pii_detection` / streaming be per-app?" and the rule for classifying
> any new per-app knob (monotonic-tightening vs free-but-floored vs floor-itself),
> see [`per-app-config-flexibility.md`](./per-app-config-flexibility.md).

> **How the `api` deployment scales horizontally** — the L4/L7 balancing layers, why
> per-pod in-memory state was a bug, and how to scale replicas, see
> [`api-load-balancing.md`](./api-load-balancing.md).

> **Where the self-improving-agent research lands vs. RouteForge's scope** — the
> 2026-08-17 control-plane-vs-compute boundary decision (compression, prompt-opt,
> memory, distillation-and-onboard all orchestrated, not hosted), see
> [`self-improving-agent-feature-fit.md`](./self-improving-agent-feature-fit.md).

> **Residency + correctness contract for the semantic cache (feature #6)** — why a
> cached answer is a second copy that must stay CCR-class and residency-partitioned,
> see [`semantic-cache-residency-spec.md`](./semantic-cache-residency-spec.md).

> **Residency contract for persistent memory (mem0-style, step-5 extension)** — the
> CCR-class guarantees a cross-request memory store must honor, see
> [`persistent-memory-residency-spec.md`](./persistent-memory-residency-spec.md).

> **Which features are IP-worthy, and what the prior art forecloses** — the ranked
> menu (IP-1…12, R1…R9) is [`ip-and-research-opportunities.md`](./ip-and-research-opportunities.md).
> **Briefing a new reviewer on it?** Give them
> [`ip-review-handoff.md`](./ip-review-handoff.md) — reading order, what is already
> settled, the gaps that are the actual assignment, and the disclosure-before-filing
> constraint.

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
the `ModelRegistry`; see `docs/onboarding-models.md`. The on-prem pool boots empty. The
managed-cloud path's availability is still `providers.{azure,bedrock}.enabled`, and there
is **no** separate `serving_paths` config axis. <!-- purged-ok -->
See [architecture-review.md](../architecture-review.md) for the pre-pivot config shape
this replaced.

So the honest "what's real vs simulated?" answer is: onboarded vLLM/Dynamo endpoints =
real on-prem paths; `managed_cloud` = real cloud path; **nothing here is a demo-safe
simulation.** If no on-prem model is enabled, `onprem_only` requests are rejected
(derived — `has_healthy_onprem()` is false), never a silent cloud fallback. See the
`inference-backends` skill.

## Redaction map return path

The gate (step 2) replaces entities with placeholders (`<SSN_1>`) and keeps the map
internal. Whether step 10 detokenizes is a **config decision**, not an omission:
placeholder mode returns placeholders deliberately (it shows redaction working);
detokenize mode re-inserts values inside the gate's trust boundary, after step 9. See the
`privacy-gate` and `output-controls` skills.
