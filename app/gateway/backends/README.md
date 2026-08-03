# Inference Backends

One `InferenceBackend` Protocol (`base.py`), one implementation
(`LiteLLMBackend`, `litellm_backend.py`). There is no `inference_backend` axis
any more — routeForge deploys nothing; on-prem models are onboarded at
runtime (endpoint + auth env registered per model, see
`docs/onboarding-models.md` and `app/apps/registry.py`'s `ModelRegistry`).
Every provider (onprem/azure/bedrock alike) egresses through this single
LiteLLM SDK path. See `.claude/skills/inference-backends/SKILL.md` and
`.claude/skills/routing-and-gateway/SKILL.md` for the full design.

- `litellm_backend.py` — `LiteLLMBackend.generate` calls `litellm.acompletion`
  pinned to the model the router already chose (`num_retries=0`, no
  `litellm.Router`), mapping `onprem` models to their registered
  `registry.endpoint_for(slug)` and cloud providers to their configured
  `litellm_prefix` (see `config/serving.yaml`'s `providers` block). Cost is
  computed from `pricing.yaml` (cloud) or the onboarded row's own price
  (on-prem), never `litellm.completion_cost`. Offline-tested via a
  monkeypatched `acompletion` (see `tests/_litellm_fake.py`) — no GPU or real
  network required for the test suite.
- `factory.py` — `make_backend(settings, registry=None)` builds the
  `LiteLLMBackend`, injecting the `ModelRegistry` so onboarded on-prem
  endpoints are resolvable; `resolve_backend(settings, registry=None)` just
  constructs it. `app/main.py` calls `resolve_backend` once at `create_app()`
  time.

## No boot-time health probe

Per-model reachability is probed at **onboard time** (the registry, via the
admin onboarding flow — Task 5), not at boot. `resolve_backend` never
health-checks and never exits: the single LiteLLM egress path is always
constructed directly, whether or not any on-prem model has been onboarded
yet.

On-prem availability for `onprem_only` requests is derived in
`app/policy/context.py`: with no healthy onboarded on-prem model
(`registry.has_healthy_onprem()` is False), `onprem` is dropped from the
enabled provider set, so an onprem_only request's `allowed_providers` is
empty and the router rejects it downstream (`ValueError("no candidates after
residency filter")`) — never silently routed to cloud.

`SP_SKIP_BACKEND_HEALTHCHECK` is now a harmless no-op (kept only so any
lingering reference doesn't break) — there is no probe left to skip.

## Onboarding a real on-prem model

Point routeForge at a live OpenAI-compatible endpoint (vLLM, Dynamo+TRT-LLM,
or anything else that speaks the OpenAI chat-completions API) via the
onboarding admin flow — no code changes, no deploy. See
`docs/onboarding-models.md`. Deploying the inference server itself (vLLM,
NVIDIA Dynamo, K8s, GPUs) is out of scope for routeForge — use NVIDIA's
verified skills for that (`dynamo-router-starter`, `dynamo-recipe-runner`,
`dynamo-troubleshoot`, `dynamo-interconnect-check`; see the
`inference-backends` skill for the full list).
