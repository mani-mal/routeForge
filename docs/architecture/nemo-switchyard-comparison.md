# NeMo Switchyard vs. routeForge's step-6 router — what to borrow, what to refuse

**Audience:** anyone weighing whether routeForge's custom per-call routing logic
(step 6, `app/routing/router.py`) should adopt ideas from — or be replaced by —
**NVIDIA NeMo Switchyard**.

**One sentence:** NeMo Switchyard and routeForge's step-6 router solve the *same*
problem (per-call model selection to cut cost while holding quality), so Switchyard is
a rich source of routing *signals and algorithm shapes* worth mining — but routeForge's
value lives in the **policy/residency/verification envelope above** model selection,
which a generic router does not enforce, so the recommendation is **mine the ideas, do
not adopt the binary — and revisit the replace question once the NVIDIA solution has
real-world review.**

> Naming note: routeForge's own step-6 router has long carried the internal codename
> "Switchyard" (see CLAUDE.md and the `routing-and-gateway` skill). That predates and is
> **unrelated to** NVIDIA-NeMo/Switchyard — a name collision, now a live confusion risk
> given routeForge already depends on the NeMo stack (NAT, Guardrails). Throughout this
> doc, "Switchyard" means the NVIDIA project unless prefixed "our".

## What NeMo Switchyard is

A Rust proxy + SDK (`switchyard-libsy`) that routes agent LLM traffic across a mix of
open, proprietary, and NVIDIA models. It accepts OpenAI Chat Completions, OpenAI
Responses, and Anthropic Messages inbound; translates to vLLM / NIM / Ollama /
OpenAI-compatible backends; and records each routing decision (rationale, tokens,
latency, outcome) with Prometheus metrics. It ships a **family of routing algorithms**
rather than one scorer. NVIDIA's headline benchmark: routing between Nemotron 3.5
Lightning and a frontier model sent only **7% of calls to frontier for ~74% cost
reduction at a ~6% accuracy tradeoff**. Ecosystem integrations include LangChain,
LiteLLM, and Kong. Status: **pre-alpha, explicitly not for production** (as of the
2026 launch posts).

Sources: NVIDIA developer blog "Route AI Agent Workloads Across Models with NVIDIA NeMo
Switchyard"; NVIDIA blog "Nemotron Lightning + Switchyard on RTX/DGX"; github.com/NVIDIA-NeMo/Switchyard;
internal report `NVIDIA_Nemotron_and_Switchyard_Report.docx`.

> **Companion:** [`switchyard-features-to-incorporate.md`](./switchyard-features-to-incorporate.md)
> is the action-oriented counterpart — assuming we keep our own router, the per-feature
> incorporation plan (metrics endpoint, stage/escalation signals, semantic cache,
> low-confidence fallback) with guardrails and sequencing.

## Concept-by-concept map

| Switchyard concept | What it does | routeForge today | Verdict |
|---|---|---|---|
| **Escalation Router** | Start cheap, watch progress turn-by-turn, escalate on *sustained difficulty* | Escalation is **reactive** — triggered by step-9 verification failure, not in-flight progress | **Incorporate** (highest value) |
| **Stage Router** | Route on *tool-activity signals*: errors/exploration → capable model; steady edits → efficient | Step-6 scores cost/risk/task-mode per hop; does **not** read agent-loop state as a feature | **Incorporate** |
| **Session affinity** | Cache the classification; don't re-decide unchanged work | Router re-enters every hop by design (needed for per-hop cost accounting) | **Partial** — cache the *decision*, still price each hop |
| **Strategy family** (Classifier / Stage / Escalation / Random / Passthrough) | Routing is a **pluggable interface**, picked per priority | Step-6 is a single cost/risk scorer; step-4 classifier off by default | **Incorporate the shape** — make the scorer one strategy among several |
| **Accuracy-tradeoff reporting** | "7% to frontier, 74% cheaper, **6% accuracy cost**" | Dashboard splits routing vs compression savings; no explicit *quality-cost* axis | **Incorporate** — strengthens the cost story |
| **Semantic model naming** decoupled from endpoints | Map names → provider/model IDs | Already done: `ModelRegistry` + tiers + LiteLLM prefixes | **Already have** ✓ |
| **Router as library, hands call back to host** | Routing logic separate from HTTP ownership | Step-6 is already an internal module | **Already have** ✓ |
| **Multi-format inbound** (OpenAI + Anthropic Messages + Responses) | Protocol bridge | OpenAI `/v1/chat/completions` only | **Consider, with guardrails** (must keep no-inbound-stream + gate-first) |
| **Prefill Router** (residual-stream complexity → trained MLP predicts per-model accuracy) | Tunable, requires training | — | **Reject (out of scope)** — Improvement Plane |
| **Prometheus routing-overhead metric** | Ops observability | Langfuse + DecisionRecord | **Minor add** — record the router's own overhead |

## The three worth actually building

1. **Progress-driven escalation in the agent loop.** The multi-step path already
   re-enters the router per hop — plumbing exists. What's missing is Switchyard's
   escalation *signal*: track difficulty across turns (retries, verification
   near-misses, repeated tool errors) and let the per-hop router floor **upward** when
   a task is visibly struggling, rather than only reacting after step 9 fails. Stays
   inside the invariants — escalation only ever moves within `allowed_providers`, so an
   `onprem_only` task escalates cheap→mid→frontier-on-prem and **never** lifts to cloud
   (CLAUDE.md hard rule 8). Also depends on real per-hop signal aggregation, which the
   open multi-step latency=0 bug currently lacks.

2. **Tool-activity as a routing feature (Stage Router).** Feed the NAT runtime's
   tool-call history into the step-6 scorer as a feature: exploration/error phase →
   capable model; steady write/edit phase → efficient model. More defensible than an
   LLM classifier because it's rules-first, matching the "classifier off by default"
   stance.

3. **An accuracy-tradeoff axis on the cost dashboard.** The dashboard already splits
   routing vs compression savings. Switchyard always pairs the cost win with the
   *quality cost*. Step 9 already produces a quality signal (verification pass/fail,
   `require_verification`); surfacing "cost saved vs. estimated quality delta against the
   all-frontier baseline" turns the two-lever story into a harder-to-dispute
   three-number story.

## What to explicitly refuse

- **Prefill Router** — training an MLP on residual streams is the Improvement Plane
  (CLAUDE.md hard rule 4; out of scope, stop).
- **The RTX/DGX on-device-vs-datacenter routing story** — that's inference *placement*
  (Dynamo, step 8). routeForge points at endpoints; on-prem-vs-cloud tiering already
  covers the routing-visible part. See `inference-layer-boundary.md`.

## Should we replace our custom router with NeMo Switchyard?

**Recommendation: not now — mine the algorithms, keep our envelope, and re-decide after
the NVIDIA solution has accumulated real-world review.** It is pre-alpha and
NVIDIA-labelled "not for production"; that alone makes replacement premature. Three
structural reasons hold even if it matures:

- **It's Rust and pre-alpha.** routeForge is Python 3.12, self-hosted, frequently
  air-gapped. A pre-alpha Rust crate in that deployment target is a support liability
  the router doesn't justify yet.
- **A generic router won't enforce our invariants.** `onprem_only` is absolute,
  residency beats the risk floor, gate-first, fail-closed — these are routeForge's
  *product value*, and they live *above* model selection. Switchyard picks the best
  model for the task; it has no concept of "residency filter runs before the risk
  floor." Adopting it wholesale means wrapping it in exactly the policy layer we
  already have.
- **We already delegate the right layer.** Switchyard's gateway/translation role is the
  job we gave **LiteLLM** (steps 7–8). Its *routing-algorithm* role is our step 6. The
  honest positioning: **routeForge = policy/residency/verification envelope; Switchyard's
  algorithms = candidate strategies for the step-6 scorer inside that envelope.** If
  anything, routeForge is the governance layer NeMo Switchyard lacks.

**Decision posture:** treat replacement as **deferred, not rejected.** Trigger a
re-evaluation when (a) Switchyard exits pre-alpha with a production posture, (b) it
gains — or exposes an extension point for — hard policy constraints like absolute
residency filtering, and (c) a Python-consumable path exists (stable server API or
bindings) that fits an air-gapped install. Until then, the pluggable-strategy interface
(above) is the low-regret move: it captures Switchyard's best ideas now *and* makes a
future swap to `switchyard-libsy` a per-strategy adapter rather than a router rewrite.
If we later pursue that interface, it is also the natural moment to rename our step-6
module to end the name collision.
