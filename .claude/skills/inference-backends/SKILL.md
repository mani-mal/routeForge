---
name: inference-backends
description: Use when an operator is standing up the on-prem server behind an onboarded routeForge model - vLLM, NVIDIA Dynamo with TensorRT-LLM, or NIM - or deciding which one to run. Trigger on mentions of Dynamo, vLLM, TensorRT, inference backend, load distribution, GPU serving, or backend fallback.
---

# Inference Backends — what the operator runs behind an onboarded endpoint

routeForge is a pure router: it points at externally-deployed OpenAI-compatible
endpoints and deploys, scales, or load-balances nothing itself. There is no
`inference_backend` config axis any more — on-prem models are onboarded at runtime
(admin UI or `POST /admin/infra/models`), not selected by a startup config value. See
`docs/guides/onboarding-models.md` for the onboarding flow and field reference.

This skill covers the *other* side of that boundary: what an operator stands up on
their own infrastructure — vLLM, NVIDIA Dynamo + TensorRT-LLM, NIM, or any other
OpenAI-compatible server — before pointing routeForge at it. None of this is
routeForge's job to build, deploy, or operate; it's context for advising a customer
or standing up a demo/dev endpoint to onboard.

**One egress implementation, regardless of what's behind the endpoint.**
`LiteLLMBackend` (`app/gateway/backends/litellm_backend.py`) is the single egress path
for every provider — onprem, azure, bedrock alike — via the LiteLLM SDK (not
`litellm.Router`), called directly with a per-request model/provider mapping. It never
knows or cares whether the on-prem endpoint it's hitting is bare vLLM, Dynamo, or NIM;
that distinction lives entirely on the operator's side of the wire. Onboarded models'
endpoint + auth env come from the `ModelRegistry` (onboarded DB rows), never a static
`base_url` in config — see `app/gateway/backends/litellm_backend.py::_map`.

```python
class InferenceBackend(Protocol):
    async def generate(self, model: str, messages: list[Message],
                       **kw) -> GenerationResult: ...
    async def health(self) -> BackendHealth: ...
    def available_models(self) -> list[ModelInfo]: ...
```

`LiteLLMBackend` is the sole implementer of this Protocol. Tests that need
determinism fake the LiteLLM SDK call itself (`tests/_litellm_fake.py`), not a mock
backend class — there is no `mock` backend.

## What the operator runs behind the endpoint — outside routeForge

routeForge's value (privacy/governance/cost/semantic routing) lives *above* the
serving layer, whatever serves the actual tokens. The table below is the ownership
boundary customers ask about, and the two options they're choosing between when they
stand up an on-prem endpoint to onboard.

### Ownership boundary (routeForge vs Dynamo vs engine vs Run:ai)

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

**Consumed facts, not axes routeForge selects.** The per-model `engine` attribute and
the cluster `runai_licensed` fact (surfaced by the infra-management config, spec
2026-08-01) are *consumed* facts the router/UI read from an onboarded model's
metadata — routeForge doesn't choose or switch between them; the operator's choice of
server behind the endpoint determines them.

## vllm

vLLM exposes an OpenAI-compatible server. Onboard it by pointing `endpoint` at that
server and `--served-model-name <slug>` at the `slug` you onboard with — see
`docs/guides/onboarding-models.md`. `LiteLLMBackend` addresses any onboarded on-prem endpoint
through LiteLLM's `openai/` prefix; there is no separate `AsyncOpenAI`-based adapter.

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
  anything;** behind a plain vLLM endpoint (or no onboarded on-prem model at all)
  `engine` is a cluster fact, not a per-model choice.

**routeForge positioning:** routeForge's value (privacy/governance/cost/semantic
routing) lives *above* the serving layer and behaves identically no matter what's
behind an onboarded endpoint. A customer can **start by onboarding a plain vLLM
endpoint** and **graduate to a Dynamo-fronted one** when traffic and fleet size
justify it — they onboard the new endpoint (same or new slug) and routeForge's
routing behavior doesn't change. The choice isn't "worse vs. better"; it's "the
operational complexity that matches my scale," and it's entirely the operator's to
make outside routeForge.

There is **no standalone `tensorrt` backend** — TensorRT-LLM is reached only *through*
Dynamo. So the real deployment choice behind any one endpoint is **vLLM (direct) vs.
Dynamo (fronting vLLM and/or TensorRT-LLM)**. It's a fact of that endpoint, surfaced to
routeForge (if at all) as the per-model `engine` attribute — never a routeForge config
value, and never something routeForge switches at runtime.

## No on-prem model onboarded

There is no `inference_backend: none` value to set — routeForge has no local-inference
mode of its own. Instead this is a **derived** state: if no onboarded on-prem model is
currently `enabled`, `ModelRegistry.has_healthy_onprem()` is `false`, which drops
`onprem` from `allowed_providers` in `app/policy/context.py`. An `onprem_only`
request's `allowed_providers` is then empty and the router rejects it downstream.
Never silently route it to cloud — that inverts the entire privacy claim. See
`docs/guides/onboarding-models.md` §(e) and CLAUDE.md's "No on-prem model onboarded ⇒ derived
reject, not a backend value" note. Test this explicitly.

## No boot-time backend health check

There is no `inference_backend` to validate at startup and no `allow_backend_fallback`
— both are gone. `LiteLLMBackend.health()` always reports `ok=True` with no network
probe (`app/gateway/backends/factory.py::resolve_backend`); per-model reachability is
probed **at onboard time** instead (`probe_endpoint`, `docs/guides/onboarding-models.md`
§(c)), and re-probed on every edit. An unreachable onboarded endpoint is saved
disabled, never a boot failure — the server keeps running with whatever else is
onboarded and enabled.

## Tests

- `LiteLLMBackend` satisfies the Protocol regardless of what's behind the endpoint
  (`tests/test_backends.py`, `tests/test_litellm_backend.py`)
- No onboarded on-prem model + `onprem_only` → `allowed_providers` empty, rejected,
  no cloud call (`tests/test_model_registry.py::test_empty_onboarded_has_no_healthy_onprem`)
- `make_backend`/`resolve_backend` never raise and need no registry at construction
  time — no boot-time health probe (`tests/test_backend_selection.py`)
- Onboard/edit probes the endpoint and only ever persists `enabled=True` when the
  probe passed (`tests/test_onboard_api.py`)
