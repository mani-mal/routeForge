---
name: inference-backends
description: Use when implementing or switching the inference backend - mock, vLLM, NVIDIA Dynamo with TensorRT-LLM, or none. Trigger on mentions of Dynamo, vLLM, TensorRT, inference backend, load distribution, GPU serving, or backend fallback.
---

# Inference Backends — mock | vllm | dynamo | none

Selected at startup by `inference_backend` in `config/serving.yaml`. **One interface,
four implementations.** No code changes to switch.

## The two serving paths — one axis, not two

There is exactly **one** serving-path axis. Don't reintroduce a parallel
`serving_paths` block; a second knob makes states like "backend vllm, path disabled"
representable and meaningless.

- **On-prem path** = `inference_backend`: `mock | vllm | dynamo | none`. `mock` is a
  first-class path (the demo/dev path), not an afterthought — it is what every live
  customer demo actually runs on. `vllm` → vLLM + Semantic Router; `dynamo` → Dynamo
  + TensorRT-LLM; `none` → on-prem disabled.
- **Cloud path** (managed_cloud, Azure/Bedrock) is **not** selected by
  `inference_backend`. Its availability is `providers.azure.enabled` /
  `providers.bedrock.enabled`, and the per-request router (step 6) picks it only for
  cloud-permitted requests. That's why `inference_backend` enumerates on-prem options
  only.

"What's real vs simulated?" → `mock` is simulated (seeded); `vllm`/`dynamo` are real
on-prem; managed_cloud is real cloud. Say so plainly.

```python
class InferenceBackend(Protocol):
    async def generate(self, model: str, messages: list[Message],
                       **kw) -> GenerationResult: ...
    async def health(self) -> BackendHealth: ...
    def available_models(self) -> list[ModelInfo]: ...
```

## mock (default)

Demo-safe: deterministic, no GPU, no network, never fails live. Seed responses by
prompt hash; simulate per-tier latency and token counts so cost math stays realistic.
Cheap tier ~200ms, frontier ~2s — the UI needs visible spread.

**Use this for the customer demo.** Real backends are for proving the integration,
not for the live run.

## vllm

vLLM exposes an OpenAI-compatible server, so the adapter is thin — point an
`AsyncOpenAI` client at `vllm.base_url`. Read `/v1/models` at startup to populate the
on-prem tier.

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

## none

No local inference. On-prem tier is **disabled**, and `onprem_only` requests must be
**rejected with a clear error**. Never silently route them to cloud — that inverts
the entire privacy claim. Test this explicitly.

## Startup validation

```python
health = await backend.health()
if not health.ok:
    if cfg.allow_backend_fallback:
        log.warning("backend_unavailable_falling_back", backend=cfg.inference_backend)
        backend = MockBackend()
    else:
        sys.exit(1)
```

Validate before serving traffic. Print the active backend and model list in the
banner — when a demo misbehaves, the first question is always "which backend is it
actually on?"

## Tests

- Each backend satisfies the Protocol
- `none` + `onprem_only` → rejected, no cloud call
- Unreachable backend + `allow_backend_fallback: false` → non-zero exit
- Unreachable backend + fallback true → mock, warning logged
