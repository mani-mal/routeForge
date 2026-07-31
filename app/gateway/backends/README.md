# Inference Backends

One `InferenceBackend` Protocol (`base.py`), one implementation
(`LiteLLMBackend`, `litellm_backend.py`) fronting the on-prem serving path
selected at startup by `inference_backend` in `config/serving.yaml`
(`vllm` | `dynamo` | `none`). No code changes to switch — see
`.claude/skills/inference-backends/SKILL.md` and
`.claude/skills/routing-and-gateway/SKILL.md` for the full design.

- `litellm_backend.py` — `LiteLLMBackend.generate` calls `litellm.acompletion`
  pinned to the model the router already chose (`num_retries=0`, no
  `litellm.Router`), mapping `onprem` to `vllm.base_url` / `dynamo.endpoint`
  per `inference_backend` and cloud providers to their configured
  `litellm_prefix` (see `config/serving.yaml`'s `providers` block). Cost is
  computed from `pricing.yaml`, never `litellm.completion_cost`. Offline-tested
  via a monkeypatched `acompletion` (see `tests/_litellm_fake.py`) — no GPU or
  real network required for the test suite.
- `factory.py` — `make_backend(settings)` builds the `LiteLLMBackend`;
  `resolve_backend(settings)` health-checks it and applies the startup
  fail-closed rule below. `app/main.py` calls `resolve_backend` once at
  `create_app()` time.

## Startup validation

`resolve_backend` calls `backend.health()` once. There is no mock fallback:
an unreachable onprem endpoint (`vllm`/`dynamo`) is always `sys.exit(1)` —
never silently degrade a customer demo.

`inference_backend: none` skips the network probe entirely (`health()`
returns `ok=True` with no request) — the `none` *rejection* of `onprem_only`
requests is enforced in `app/policy/context.py`, not here: with `none`,
`onprem` is dropped from the enabled provider set, so an onprem_only
request's `allowed_providers` is empty and the router rejects it downstream.
The backend object itself just needs to exist so public/cloud-eligible
requests still get served.

The resolved backend + its model list are logged as a startup banner
(`serving.backend active backend=litellm inference=... models=...`) so a
misbehaving demo can always be traced back to "which backend is it actually
on?".

`SP_SKIP_BACKEND_HEALTHCHECK=1` is a test-only escape hatch (see
`factory.py`'s `resolve_backend` docstring and `tests/conftest.py`): it skips
the probe so `app/main.py`'s module-level `app = create_app()` doesn't hang
or exit at test-collection time against an unreachable real backend. Never
set it in production.

## Running against a real backend on GPU/DGX

To exercise `vllm` or `dynamo` for real (not offline-mocked), point config at
a live server — no code changes required:

- **vLLM**: set `SP_INFERENCE_BACKEND=vllm` and `vllm.base_url` to a live
  vLLM OpenAI-compatible server, e.g. `http://<host>:8000/v1`. An unreachable
  server makes the process exit non-zero at boot — the right behaviour for a
  GPU smoke test, where a silent fallback would hide the failure.
- **Dynamo**: set `SP_INFERENCE_BACKEND=dynamo` and `dynamo.endpoint` to a
  live Dynamo frontend, e.g. `http://dynamo-frontend:8080` (no `/v1` suffix).
  Deploying Dynamo itself (K8s, multi-GPU router modes, disaggregated
  serving) is **not** hand-rolled in this repo — use NVIDIA's verified
  skills instead (`dynamo-router-starter`, `dynamo-recipe-runner`,
  `dynamo-troubleshoot`, `dynamo-interconnect-check`; see the
  `inference-backends` skill for the full list). Dynamo needs GPUs and K8s —
  it will not run on a demo laptop, so this path is only exercised on
  GreenLake.
