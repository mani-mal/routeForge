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
