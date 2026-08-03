---
name: inference-backends
description: Use when implementing or switching the inference backend - vLLM, NVIDIA Dynamo with TensorRT-LLM, or none - all egressing through the single LiteLLM SDK path. Trigger on mentions of Dynamo, vLLM, TensorRT, inference backend, load distribution, GPU serving, or backend fallback.
---

# Inference Backends — vllm | dynamo | none

Selected at startup by `inference_backend` in `config/serving.yaml`. **One egress
implementation** (`LiteLLMBackend`, `app/gateway/backends/litellm_backend.py`) serves
every value of this axis — LiteLLM (the SDK, not `litellm.Router`) is called directly
with a per-request model/provider mapping, never a hand-rolled per-backend adapter.
`inference_backend` only changes which on-prem endpoint that mapping points at (or
disables on-prem entirely). No code changes to switch.

**`mock` is removed** (superseded by the LiteLLM gateway swap, 2026-07-30). There is
no more deterministic canned-response path for a live demo run — `vllm` is the
default, and tests that need determinism fake the LiteLLM SDK call itself
(`tests/_litellm_fake.py`), not a `MockBackend` class.

## The two serving paths — one axis, not two

There is exactly **one** serving-path axis. Don't reintroduce a parallel
`serving_paths` block; a second knob makes states like "backend vllm, path disabled"
representable and meaningless.

- **On-prem path** = `inference_backend`: `vllm | dynamo | none`. `vllm` → vLLM +
  Semantic Router; `dynamo` → Dynamo + TensorRT-LLM; `none` → on-prem disabled.
- **Cloud path** (managed_cloud, Azure/Bedrock) is **not** selected by
  `inference_backend`. Its availability is `providers.azure.enabled` /
  `providers.bedrock.enabled`, and the per-request router (step 6) picks it only for
  cloud-permitted requests. That's why `inference_backend` enumerates on-prem options
  only.

All providers — onprem, azure, bedrock — egress through the **same** `LiteLLMBackend`
as an SDK call, always **behind the policy gate** (step 2 clears the request first;
CLAUDE.md hard rule 1). What differs per provider is only the `litellm_prefix` /
model-slug mapping in `config/serving.yaml` + `config/routing.yaml`'s pool, not the
code path. "What's real vs simulated?" → `vllm`/`dynamo` are real on-prem;
managed_cloud is real cloud; nothing here is a demo-safe simulation any more —
if you need determinism for a test, fake the LiteLLM SDK call, don't reach for a mock
backend class.

```python
class InferenceBackend(Protocol):
    async def generate(self, model: str, messages: list[Message],
                       **kw) -> GenerationResult: ...
    async def health(self) -> BackendHealth: ...
    def available_models(self) -> list[ModelInfo]: ...
```

`LiteLLMBackend` is the sole implementer of this Protocol.

## Ownership boundary (routeForge vs Dynamo vs engine vs Run:ai)

NVIDIA Dynamo is a strong choice for the inference-serving and GPU load-balancing
layer. Dynamo manages the inference request; Run:ai (where available) manages the
GPU cluster resources Dynamo runs on. Responsibilities split like this:

| Decision | Recommended owner |
|---|---|
| Is this coding, RAG, reasoning, tool use or a simple question? | routeForge |
| Does data contain PII and need to remain on-premises? | routeForge |
| Which model offers the required quality at the lowest cost? | routeForge |
| Should a failed request fall back to another model? | routeForge |
| Which replica has the most useful KV cache? | Dynamo |
| Which replica has available capacity? | Dynamo |
| Should prefill and decode run on separate GPU pools? | Dynamo |
| How many model-worker replicas should run? | Dynamo Planner |
| How are requests scheduled and continuously batched inside a worker? | vLLM or TensorRT-LLM |
| How many physical GPU nodes are available? | Kubernetes node autoscaler or fixed on-prem hardware |

**What Dynamo already provides** — you do not need to implement these yourself:

- OpenAI-compatible inference frontend
- Worker discovery and registration
- Round-robin, random and KV-cache-aware routing
- Routing based on cache overlap and active worker load
- Multiple vLLM, TensorRT-LLM or SGLang workers
- Aggregated inference, where one worker handles the whole request
- Disaggregated prefill and decode pools
- Multi-node and multi-GPU inference
- Multi-tier KV-cache management
- Request cancellation and streaming
- Kubernetes Operator, CRDs, Helm charts and containers
- Prometheus metrics
- Planner-driven worker autoscaling

The KV router tracks which workers hold reusable prompt blocks and selects a worker
using cache overlap and predicted processing cost, falling back to ordinary load
balancing when KV information is unavailable. The Planner adjusts prefill/decode
replica counts using throughput, latency, load or SLA-oriented policies.

**You still need to configure:**

- Model-to-GPU assignments
- Tensor parallelism
- Minimum and maximum replicas
- Routing mode
- TTFT and inter-token latency targets
- Queue and admission-control limits
- Model storage and caching
- Kubernetes GPU placement
- Monitoring and alerting

**What Dynamo does not replace** — Dynamo does not automatically know that:

- DeepSeek or Qwen is better for a particular coding request
- A small model is sufficient for a simple question
- A prompt contains sensitive financial information
- One model produced better answers in your evaluation dataset
- A request should use local inference instead of a cloud API
- A model exceeded the permitted cost per request

Those decisions are the core value of routeForge. Dynamo's Global Router can select
between pools for the same model, or between pools optimized for characteristics such
as short vs. long prompts and different latency requirements — it is not a complete
semantic, quality and governance router across unrelated models.

**Consumed facts, not new axes.** The per-model `engine` attribute and the cluster
`runai_licensed` fact (surfaced by the infra-management config, spec 2026-08-01) are
*consumed* facts the router/UI read — they are not new serving-path axes. The single
`inference_backend` axis (`vllm | dynamo | none`) above still stands.

## vllm (default)

vLLM exposes an OpenAI-compatible server. `LiteLLMBackend` addresses it through
LiteLLM's `openai/` prefix pointed at `vllm.base_url` (see `providers.onprem` in
`config/serving.yaml`) — there is no separate `AsyncOpenAI`-based adapter anymore.

## dynamo

NVIDIA Dynamo (`ai-dynamo/dynamo`) handles load distribution across TensorRT-LLM
workers — use it when the customer has multi-GPU on-prem inference. It provides
router modes and disaggregated serving; the frontend is OpenAI-compatible, so our
adapter differs from vLLM mainly in endpoint and deployment shape.

**Do not hand-roll Dynamo deployment.** NVIDIA publishes verified skills:

```bash
npx skills add nvidia/skills --skill dynamo-router-starter --agent claude-code --yes
npx skills add nvidia/skills --skill dynamo-recipe-runner --agent claude-code --yes
npx skills add nvidia/skills --skill dynamo-troubleshoot --agent claude-code --yes
npx skills add nvidia/skills --skill dynamo-interconnect-check --agent claude-code --yes
```

Use `dynamo-router-starter` for router modes, `dynamo-recipe-runner` for K8s deploy,
`dynamo-troubleshoot` for day-2, `dynamo-interconnect-check` to validate NIXL/UCX/NCCL.
Dynamo needs GPUs and K8s — it will not run on a demo laptop. Expect to exercise this
path only in GreenLake.

## Choosing vllm-only vs dynamo — the customer decision

Customers ask "Dynamo is open source and does more — why would I run plain vLLM?"
Open source removes the *license* cost, not the *operational* cost, and operational
cost is the whole decision. Dynamo isn't a library you flip on; it's a distributed
system (K8s operator, CRDs, frontend + router + worker pods + Planner, worker
discovery, and — for disaggregated/multi-node serving — a validated NIXL/UCX/NCCL
interconnect). Plain vLLM is one container exposing an OpenAI endpoint.

Most of Dynamo's value only materializes **at scale**, so match the complexity to the
deployment:

**Plain `vllm` is the right call when:**
- One or few replicas on one node — nothing to load-balance; KV-cache-aware routing
  across replicas is meaningless with one replica.
- Modest/bursty traffic — Planner autoscaling and admission control solve problems you
  don't have.
- A single model — no heterogeneous fleet, so no per-engine routing to do.
- Pilot / early on-prem — prove the privacy + cost story before standing up an operator.
- Small ops team — bare vLLM is `docker run`; Dynamo is real day-2 ownership.
- You don't want TensorRT-LLM's per-model engine *build* pipeline (compiled per
  GPU/batch/seqlen); vLLM just loads a HF checkpoint.

**`dynamo` earns its complexity when:**
- Many replicas, especially with shared prefixes (RAG, long system prompts) — KV-cache-
  aware routing gives large real wins.
- High/spiky traffic needing Planner autoscaling of prefill/decode pools.
- Multi-node / multi-GPU, disaggregated prefill + decode.
- A heterogeneous fleet — several models, some best on vLLM, some on TensorRT-LLM,
  behind one endpoint. **This is the only mode where the per-model `engine` field means
  anything;** under `vllm`/`none` engine is a cluster fact, not a per-model choice.

**routeForge positioning:** routeForge's value (privacy/governance/cost/semantic
routing) lives *above* the serving layer and behaves identically on either backend.
That's why `inference_backend` is a swappable axis — a customer **starts on vLLM** and
**graduates to Dynamo** when traffic and fleet size justify it, with zero change to
routeForge's routing behavior. The choice isn't "worse vs. better"; it's "the
operational complexity that matches my scale."

There is **no standalone `tensorrt` backend** — TensorRT-LLM is reached only *through*
Dynamo. So the real deployment choice is **vLLM (direct) vs. Dynamo (fronting vLLM
and/or TensorRT-LLM) vs. none**. `inference_backend` is a boot fact set at deploy time
(the admin UI surfaces it read-only) — switching runtimes is a redeploy, not a live
toggle. The per-model ENGINE control should therefore be read-only under `vllm`/`none`
and editable only under `dynamo`.

## none

No local inference. `LiteLLMBackend` still constructs (its `health()` reports
`ok=True` with no network probe — there's no onprem server to check), but on-prem is
dropped from the enabled provider set structurally in `app/policy/context.py`, so an
`onprem_only` request's `allowed_providers` is empty and the router rejects it
downstream. Never silently route it to cloud — that inverts the entire privacy claim.
Test this explicitly.

## Startup validation — fail-closed, no fallback

`allow_backend_fallback` **no longer exists.** An unreachable onprem endpoint
(`vllm`/`dynamo`) at boot is always a hard failure:

```python
health = await backend.health()
if not health.ok:
    log.error("backend_unavailable_no_fallback", backend=cfg.inference_backend)
    sys.exit(1)
```

There is no mock to fall back to. Validate before serving traffic. Print the active
backend and model list in the banner — when a demo misbehaves, the first question is
always "which backend is it actually on?"

## Tests

- `LiteLLMBackend` satisfies the Protocol for every `inference_backend` value
- `none` + `onprem_only` → `allowed_providers` empty, rejected, no cloud call
  (`tests/test_none_backend.py`)
- Unreachable onprem endpoint at boot → non-zero exit, no fallback of any kind
- The config lint rejects `mock` as a value of `inference_backend` outright
