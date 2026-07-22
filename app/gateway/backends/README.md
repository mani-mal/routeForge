# Inference Backends

One `InferenceBackend` Protocol (`base.py`), four implementations, selected at
startup by `inference_backend` in `config/serving.yaml`. No code changes to
switch — see `.claude/skills/inference-backends/SKILL.md` for the full design.

- `mock.py` — deterministic, no GPU, no network. The demo default.
- `vllm.py` / `dynamo.py` — thin `httpx.AsyncClient` adapters over an
  OpenAI-compatible `/v1/chat/completions` + `/v1/models` surface (shared
  mapping logic in `openai_compat.py`). Offline-tested via
  `httpx.MockTransport` — no GPU or real network required for the test suite.
- `factory.py` — `make_backend(settings)` selects the implementation;
  `resolve_backend(settings)` health-checks it and applies the startup
  fallback/exit rule below. `app/main.py` calls `resolve_backend` once at
  `create_app()` time.

## Startup validation

`resolve_backend` calls `backend.health()` once. If unhealthy:
- `allow_backend_fallback: true` → log a warning, fall back to `MockBackend`.
- `allow_backend_fallback: false` → `sys.exit(1)` (never silently degrade a
  customer demo).

`mock` and `none` health checks never touch the network (`none` maps to a
working `MockBackend` — see `factory.py`'s docstring for why: the `none`
*rejection* of `onprem_only` requests is enforced in `app/policy/context.py`,
not here), so they always report healthy and never trigger fallback/exit.
Only `vllm`/`dynamo` can fail health. The resolved backend + its model list
are logged as a startup banner (`serving.backend active backend=... models=...`)
so a misbehaving demo can always be traced back to "which backend is it
actually on?".

## Running real backends on GPU/DGX

To exercise `vllm` or `dynamo` for real (not offline-mocked), point config at
a live server — no code changes required:

- **vLLM**: set `SP_INFERENCE_BACKEND=vllm` and `vllm.base_url` to a live
  vLLM OpenAI-compatible server, e.g. `http://<host>:8000/v1`. Set
  `allow_backend_fallback: false` in `config/serving.yaml` if you want the
  process to fail hard (`sys.exit(1)`) instead of silently falling back to
  `mock` when that server is unreachable — the right setting for a GPU
  smoke test, where a silent fallback would hide the failure.
- **Dynamo**: set `SP_INFERENCE_BACKEND=dynamo` and `dynamo.endpoint` to a
  live Dynamo frontend, e.g. `http://dynamo-frontend:8080` (no `/v1` suffix —
  see `dynamo.py`). Deploying Dynamo itself (K8s, multi-GPU router modes,
  disaggregated serving) is **not** hand-rolled in this repo — use NVIDIA's
  verified skills instead (`dynamo-router-starter`, `dynamo-recipe-runner`,
  `dynamo-troubleshoot`, `dynamo-interconnect-check`; see the
  `inference-backends` skill for the full list). Dynamo needs GPUs and K8s —
  it will not run on a demo laptop, so this path is only exercised on
  GreenLake.
