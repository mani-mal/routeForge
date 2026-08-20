# Inference-layer boundary — routeForge routes, your platform serves

**Audience:** solution engineers, customer platform teams, and anyone who asks
"does routeForge do llm-d / DRA / Gateway API / prefill-decode?"

**One sentence:** routeForge is a **semantic, privacy, and cost router** that sits
*above* the inference-serving layer; it **points at** an OpenAI-compatible endpoint,
it does **not** deploy or operate the GPU-serving stack. That boundary is what keeps
routeForge thin enough to drop into many different customer on-prems without turning
every install into a serving-infra project.

This doc exists because the question keeps coming up (usually after someone watches an
llm-d / inference-optimization talk): *"the video covered prefill/decode splitting,
Dynamic Resource Allocation, Kubernetes Gateway API endpoint-pickers, models-as-volumes,
llm-d… does routeForge do those?"* The honest answer is **"those live one layer below
routeForge, and are owned by Dynamo / the inference engine / Kubernetes — by design."**
See `docs/llm-d.txt` for the source write-up those questions come from.

---

## The layers

```
┌─────────────────────────────────────────────────────────────┐
│  routeForge  (the control plane — steps 1–10)                │
│  • Is this coding / RAG / reasoning / a simple question?     │
│  • Does the request contain PII/PHI? must it stay on-prem?   │
│  • Which model gives the needed quality at the lowest cost?  │
│  • Fall back? verify the output? record cost + a trace?      │
│  → emits an OpenAI-compatible call to ONE chosen endpoint    │
└───────────────────────────┬─────────────────────────────────┘
                            │  OpenAI-compatible HTTP (via LiteLLM)
┌───────────────────────────▼─────────────────────────────────┐
│  Inference serving  (owned by the customer's platform)       │
│  • Dynamo   → KV-cache-aware routing, prefill/decode split,  │
│               worker discovery, Planner autoscaling          │
│  • vLLM / TensorRT-LLM → continuous batching inside a worker │
│  • Kubernetes / Run:ai → GPU placement, scheduling, DRA      │
│  • Storage → model weights: PVC / image-volume / dist. cache │
└─────────────────────────────────────────────────────────────┘
```

routeForge makes the **decisions that require knowing the request** (semantics,
sensitivity, quality, cost, governance). Everything below the line is about **making a
chosen model run fast on scarce GPUs** — a job the serving layer already does better
than any router could, and one that differs at every customer site.

---

## The backend options routeForge offers — and who owns serving

On-prem models are **onboarded at runtime** (admin UI → Infrastructure → Onboard, or
`POST /admin/infra/models`), not selected in YAML — see `docs/onboarding-models.md`. Each
onboarded model carries its own OpenAI-compatible `endpoint`. Every path egresses through
the **same** LiteLLM path, always behind the policy gate.

| Path | On-prem serving | Who stands it up | routeForge's job |
|---|---|---|---|
| Onboarded vLLM | vLLM OpenAI server | Customer (single/few GPU) | Point the onboarded model's `endpoint` at it |
| Onboarded Dynamo | NVIDIA Dynamo + TensorRT-LLM/vLLM workers | Customer, via **NVIDIA's Dynamo Operator / recipes** | Point the onboarded model's `endpoint` at the frontend |
| No on-prem model enabled | *(empty pool)* | — | `onprem_only` requests are **rejected**, never sent to cloud (derived) |
| Managed cloud | Azure / Bedrock managed | The provider | `providers.{azure,bedrock}.enabled` + per-request routing |

**Onboarding only changes which endpoint the router's chosen model maps to.** It does not
make routeForge deploy anything. The customer (or their MLOps platform / NVIDIA's operator)
runs the serving stack; routeForge consumes its OpenAI-compatible frontend. If no on-prem
model is currently enabled, `ModelRegistry.has_healthy_onprem()` is false and `onprem_only`
requests are rejected — the same guarantee the old `none` backend gave, now derived from
onboarding state.

---

## Concept map — the llm-d.txt items, and where each actually lives

| Concept (from the talk / `docs/llm-d.txt`) | Layer that owns it | routeForge status |
|---|---|---|
| **Prefill/decode disaggregation** | Dynamo (disaggregated serving) | Delegated — not built. Correct. |
| **KV-cache-aware routing** | Dynamo KV router (`router_mode: kv`) | Delegated. `config/serving.yaml` already points at it. |
| **Model-aware / body-based routing** (the "model ID is in the POST body" problem) | routeForge **and/or** a Gateway API endpoint-picker | Solved **in-app** (`app/routing/router.py`) — richer than an endpoint-picker (routes on sensitivity/cost/quality, not just model ID). No K8s Gateway API resources, and none needed for a single service. |
| **Dynamic Resource Allocation (DRA)** | Kubernetes (≥1.35) + device drivers | Not adopted. Manifests use static `nodeSelector: nvidia.com/gpu.product`. A deploy-manifest concern, independent of app code. |
| **Models-as-containers / image-volumes; distributed layer cache (Spigel)** | Kubernetes storage layer | Not adopted. Weights are HF-downloaded into a `local-path` PVC. Deploy-only concern. |
| **llm-d framework** (Envoy inference gateway + vLLM) | Serving substrate | Absent — and it **overlaps/competes with the Dynamo choice** already made. Adopting it means re-choosing the serving substrate, not adding to routeForge. |
| **Continuous batching** | vLLM / TensorRT-LLM (inside a worker) | Delegated. |

One app-layer nod exists: `context_optimization.cache_alignment` (Headroom's
CacheAligner) stabilizes prompt prefixes for **provider prefix-cache** hits. It is
complementary to serving-side KV routing, not a reimplementation of it.

**Takeaway:** none of the "missing" items are gaps in routeForge's own responsibility.
The genuinely-absent ones (DRA, image-volumes, distributed cache) are Kubernetes /
deployment-manifest optimizations that belong to the customer's packaging layer.

---

## Why routeForge does NOT deploy Dynamo ("enactment") — decision, 2026-08-03

The infra-management feature (`feat/infra-management-config`) lets an admin **declare**
per-model serving facts — `engine` (vllm|trtllm), `tensor_parallel`,
`min_replicas`/`max_replicas`, `gpu_type`, `runai_scheduled` — and the cluster fact
`runai_licensed`. These are **describe-and-consume**: the router reads them to gate/
annotate decisions and the UI displays them. routeForge **renders and deploys nothing**
from them (CLAUDE.md hard rule 4; infra-management design §10).

We considered an "enactment" track: have routeForge turn those declared facts into a
real Dynamo/TensorRT-LLM deployment. **Rejected.** Reasons, in the context of shipping
routeForge to many customer on-prems without over-complicating setup or implementation:

1. **It inverts what makes routeForge portable.** A thin pointer (`endpoint` + one
   config value) drops into any cluster. An in-cluster deployer inherits every
   customer's infra heterogeneity — plain K8s vs OpenShift vs Run:ai, GPU types,
   air-gapped registries, storage classes, K8s + CRD version skew. Each install becomes
   a serving-infra project. That is the over-complication we're trying to avoid.
2. **It requires cluster-admin at every site.** Deploying Dynamo means holding RBAC to
   create GPU workloads/CRDs and owning their lifecycle — a far harder sell and a far
   bigger blast radius than "give me a namespace and an endpoint URL." Platform teams
   will not want a routing app mutating their GPU fleet.
3. **It duplicates what NVIDIA maintains.** Dynamo ships an Operator, CRDs, Helm charts,
   and recipes. Re-implementing deployment is a worse copy to maintain against upstream
   drift. (Skill guidance: *do not hand-roll Dynamo deployment; use NVIDIA's verified
   skills.*)
4. **It breaks the boundary we deliberately built.** "Declared fact, not enacted" is
   exactly what keeps the infra-management feature simple and safe.

The single real upside of enactment — turning `tensor_parallel` / `min_replicas` into a
live Planner config — is genuine value, but it belongs to **Dynamo's Planner and
NVIDIA's operator** the moment the customer runs Dynamo. It is not routeForge's to own.

**Rule of thumb:** *Dynamo's operator enacts; routeForge points at the result.*

### If we ever want to smooth the Dynamo path

The low-risk move is a **scaffold, not a deployer**: ship reference deploy artifacts
(a `deploy/dynamo/` example + docs), and optionally let the infra-management panel
*export* a starter Dynamo/Helm values file **derived from** the declared facts — a
"hand this to your platform team" artifact. routeForge generates; the customer applies.
This keeps rule 4 intact, needs no RBAC, and still gives customers a running start.
(Deferred — not currently planned.)

---

## FAQ (SE / customer-facing)

**Q: Does routeForge do prefill/decode splitting or KV-cache-aware routing?**
Not itself — it delegates to Dynamo, which owns those. routeForge already targets
Dynamo's KV router (`router_mode: kv`). This is the recommended division of labor.

**Q: Does routeForge use the Kubernetes Gateway API / endpoint-pickers?**
No. The "route on the model ID buried in the request body" problem is solved *in-app*
by routeForge's router, and it routes on more than the model ID (sensitivity, cost,
quality, complexity). A Gateway API endpoint-picker would be a strict subset.

**Q: Does routeForge support Dynamic Resource Allocation (DRA)?**
DRA is a Kubernetes accelerator-scheduling feature, owned by the cluster, not the app.
routeForge is unaffected by whether the cluster uses DRA or static GPU pinning — it just
needs an endpoint. Adopting DRA is a deploy-manifest change on the customer side.

**Q: Is llm-d supported / should we use it instead of Dynamo?**
llm-d is a serving substrate that overlaps Dynamo. routeForge would sit above either the
same way. Choosing between them is a serving-layer decision for the platform team; it
does not change routeForge.

**Q: So what does routeForge actually add over just calling vLLM/Dynamo directly?**
The decisions that require understanding the request: privacy/PHI detection + redaction,
residency enforcement (`onprem_only`), per-call model/tier selection for cost, output
verification, and a full decision trace + cost accounting. Dynamo makes a *chosen* model
fast; routeForge decides *which* model, *whether* it may leave on-prem, and *proves* what
it did. See the ownership table in the `inference-backends` skill for the full split.
