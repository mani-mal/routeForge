# RouteForge — Logical & Request/Response Architecture

Companion notes for the draw.io diagrams in
[`diagrams/`](diagrams/):

- `diagrams/routeforge-logical-architecture.drawio` — the ten Serving-Plane steps as
  layers, each labelled with the library that implements it.
- `diagrams/routeforge-request-flow.drawio` — the numbered request/response flow.
- `diagrams/routeforge-physical-architecture.drawio` — the Kubernetes pods (covered in
  [`api-load-balancing.md`](api-load-balancing.md) / the Helm chart, not repeated here).

> **This file is verified against the code at api:0.24.0 (2026-09-10 re-verification; every
> row below was re-checked against HEAD).** Every component below carries a
> status marker. **Read the [status legend](#status-legend) first.** Application-scope
> concerns that live outside RouteForge — prompt-optimization (DSPy) and NeMo Guardrails —
> are **not** part of the router and are intentionally absent from the diagrams and the
> tables below. Persistent memory (mem0-style) is **not** one of these — see the Step 5
> table and the Scope note below: it shipped as an in-house step-5 extension, inside the
> router.

---

## Status legend

| Marker | Meaning |
|--------|---------|
| ✅ **Used** | Core dependency, on the default request path. |
| 🔶 **Optional** | Real, but behind an opt-in `uv` extra and/or a **default-OFF** feature flag. Works when enabled; the default path does not use it. |
| ⚠️ **Name ≠ reality** | Implemented, but the diagram label overstates it — the real implementation is simpler than the name suggests. |

---

## Logical architecture — the ten steps and their libraries

### Step 1 / 10 · Incoming request & governed response — `app/api/`
| Component | Library | Status | Notes |
|-----------|---------|--------|-------|
| HTTP API | **FastAPI + Uvicorn** | ✅ Used | OpenAI-compatible `/v1/chat/completions`. |
| Models / validation | **Pydantic v2**, Starlette, **httpx** | ✅ Used | Core deps. |

### Step 2 / 3 · Identity, privacy & policy gate — `app/policy/` (runs first, fails closed)
| Component | Library | Status | Notes |
|-----------|---------|--------|-------|
| PII/PHI detection | **Presidio** (+ spaCy NER) | 🔶 Optional | `uv sync --extra presidio`, lazy-imported in `detectors.py::PresidioDetector`. **The configured default is `presidio`** (`config/serving.yaml` `policy.pii_detection`; `make_detector` takes no default and raises on an unknown mode). The bundled Compose and Spark deploys override this to `regex` (offline-safe — no spaCy model download); per-app `pii_detection` = `off \| regex \| presidio`. |
| Custom recognizers | **`regex`** (ReDoS-safe) | ✅ Used | Core dep; powers the per-app custom recognizers. |
| Auth + API tokens | **argon2-cffi** | ✅ Used | Admin password + API-token hashing. |
| App / usage store | **SQLAlchemy** (async) | ✅ Used | `aiosqlite` (dev) core; `asyncpg` (prod Postgres) via `--extra postgres`. |

### Step 4 · Task-mode router + agent runtime — `app/routing/`, `app/agent/`
| Component | Library | Status | Notes |
|-----------|---------|--------|-------|
| Task-mode router | rules-first (in-house) | ✅ Used | direct vs multi-step; classifier **off** by default. |
| Optional LLM classifier | in-house routed call | 🔶 Optional | `app/routing/task_mode_llm.py`, default-off (`PromptOptCfg.enabled`). A routed LLM call using a managed prompt. |
| Agent runtime | **NeMo Agent Toolkit** (`nvidia-nat[langchain]`) | 🔶 Optional | `uv sync --extra agent`, lazy-imported in `agent/runtime.py`. **Required for the multi-step path**; each hop re-enters steps 5–8. Brings LangChain transitively. |

### Step 5 · Conditional context optimization — `app/compression/`, `app/cache/`, `app/memory/` (per call)
| Component | Library | Status | Notes |
|-----------|---------|--------|-------|
| Compression (default) | `DeterministicCompressor` (in-house) | 🔶 Optional | Whole feature is **default-OFF** (`context_optimization.enabled: false`). |
| Compression (real ML) | **Headroom** (`headroom-ai`) | 🔶 Optional | `uv sync --extra compression`, lazy-imported in `compression/headroom.py`; only selected when the extra is installed. |
| CCR (cache of originals) | in-house `CCRStore` | 🔶 Optional | Local, TTL-bounded, residency-fenced; only active when compression is on. |
| Semantic cache | in-house `DeterministicSemanticCache` | ⚠️ Name ≠ reality | **Default-OFF** (`semantic_cache.enabled: false`). Despite the name it keys on a **normalized query string** (case/whitespace) — **no embeddings, no similarity** ("no embedding library is imported"; fuzzy/embedding backend is a deferred seam, C8). It is an exact-match cache, not semantic retrieval. |
| Persistent memory | in-house `DeterministicMemoryStore` | 🔶 Optional | **Default-OFF** (`memory.enabled: false`, `SP_MEMORY` override) but **wired into the pipeline**, not just substrate — `app/pipeline.py` calls `recall_memory`/`write_memory` on both the direct and streaming paths. Session-scoped, most-recent-first recall (`max_recall: 3`); sanitized-input + post-verify-output only, never raw text; CCR-class residency (`local_only`, TTL-enforced, `export_to_telemetry: false`). Ships as an in-house step-5 extension, not mem0 itself — see [`self-improving-agent-feature-fit.md`](./decisions/self-improving-agent-feature-fit.md) (✅ **SHIPPED**, 2026-08-17 scope decision) and [`persistent-memory-residency-spec.md`](./specs/persistent-memory-residency-spec.md). |

### Step 6 / 7 / 8 · Adaptive router + model gateway — `app/routing/`, `app/gateway/`
| Component | Library | Status | Notes |
|-----------|---------|--------|-------|
| Adaptive router | in-house `RoutingStrategy` | ✅ Used | Scores candidates post-compression; residency clamp; pluggable strategy seam. |
| Model gateway | **LiteLLM** | ✅ Used | **Core dep** (not optional). Single multi-provider egress path behind the gate (`gateway/backends/litellm_backend.py`). |

### Step 8 · Approved inference backends (external — operator-run, **not** a RouteForge library)
| Path | Status | Notes |
|------|--------|-------|
| On-prem endpoints (vLLM / Dynamo / NIM) | external | Operator-run OpenAI-compatible endpoints, onboarded at runtime. RouteForge routes to them; it hosts none. |
| Managed cloud (Azure / Bedrock) | 🔶 Optional | Reached via LiteLLM; **default-OFF** (`providers.azure.enabled` / `.bedrock.enabled` = `false`; only `onprem` enabled by default). Blocked entirely for `onprem_only` requests. |

### Step 9 · Output controls & verification — `app/output/`
| Component | Status | Notes |
|-----------|--------|-------|
| Leakage checks / `require_verification` / escalation | ✅ Used | In-house; runs on egress before the response is returned. |

### Cross-cutting · Observability & config — `app/obs/`, `app/config/`
| Component | Library | Status | Notes |
|-----------|---------|--------|-------|
| Tracing | **OpenTelemetry SDK + OTLP exporter** | 🔶 Optional | `uv sync --extra observability`, lazy-imported. Spans posted to Langfuse's OTLP ingestion endpoint. |
| Trace/cost UI | **Langfuse** | 🔶 Optional | Same `observability` extra; self-hosted (see physical diagram). Telemetry is **redacted on egress** by default. |
| Metrics | **prometheus-client** (`GET /metrics`) | 🔶 Optional | Same extra; `/metrics` returns 503 without it. In-process exporter, no bundled server. |
| Config + cross-file lint | **PyYAML + Pydantic** | ✅ Used | Four YAMLs, boot-time cross-validation. |

### Console (browser) — `ui/`
| Component | Library | Status |
|-----------|---------|--------|
| Admin console | **React + Recharts**, Vite, Tailwind, TypeScript | ✅ Used |

---

## Request / response flow (numbered)

Matches `diagrams/routeforge-request-flow.drawio`. Steps 1–13 are the request/response
spine; 14–15 are the telemetry tap. **Every step emits a `DecisionRecord`.**

| # | Hop | What happens |
|---|-----|--------------|
| 1 | app → API | `POST /v1/chat/completions` with the app's API token. |
| 2 | API → policy gate | Authenticate; **PII scan runs first** (Presidio if enabled, else regex). Nothing external sees the raw request before this completes. |
| 3 | gate → policy context | Build the immutable `ExecutionPolicyContext`: residency (`onprem_only`), `allowed_providers`, budget. Gate errors → **reject** (fail closed). |
| 4 | context → task-mode router | Classify **direct** vs **multi-step** (rules-first). |
| 5 | → agent runtime (NAT) | *Multi-step only.* NeMo Agent Toolkit drives hops; **each hop re-enters steps 5–8**. |
| 6 | → context optimization | Direct path (or per hop): memory recall (prepend) → semantic-cache lookup → Headroom compression. *All default-OFF.* See [`sample-request-flows.md`](./sample-request-flows.md) for the exact per-call order, including the memory-write step on the success path. |
| 7 | optimization → adaptive router | Score candidates on **post-compression** token cost; apply residency clamp. |
| 8 | router → model gateway | Select model + backend (LiteLLM); quotas, breakers, fallback. |
| 9 | gateway → backend | Dispatch to an on-prem endpoint, **or** managed cloud *only if allowed* (never for `onprem_only`). |
| 10 | backend → verify | Completion returns to step-9 output controls. |
| 11 | verify → governed response | Leakage checks; assemble the response + full `DecisionRecord`. |
| 12–13 | response → API → app | Return `200` (or `422`/`502`/`503` on reject — see `dev-references/api-reference.md`). |
| 14 | (response) → OTel exporter | Emit the trace, **redacted on egress**. |
| 15 | OTel → Langfuse | Store trace, cost, latency (no raw PII, no CCR/cache contents). |

---

## Scope note — what is deliberately not here

RouteForge is the **router / policy gateway**. Two things that belong to the *application*
built on top of it — not to the router — are intentionally excluded from these diagrams and
tables:

- **Prompt-optimization (DSPy)** — an offline concern of the application, not the serving path.
- **NeMo Guardrails** — not a RouteForge dependency; the gate is Presidio/regex + rules only.

**Persistent memory (mem0-style) is NOT one of these** — it is an in-house **step-5
extension inside the router** (`app/memory/`, `DeterministicMemoryStore`), wired into
`app/pipeline.py`'s recall/write calls on both the direct and streaming paths (default-OFF,
`memory.enabled: false`), governed by the same CCR-class residency rules as compression
(local-only, TTL-enforced, never exported to telemetry). See the Step 5 table above and
[`self-improving-agent-feature-fit.md`](./decisions/self-improving-agent-feature-fit.md) for the
2026-08-17 scope decision that shipped it as an in-scope extension rather than an
application-side concern.

One naming caveat remains inside the router itself:

- **"Semantic cache — CCR-class"** → **not semantic** (no embeddings); it's a deterministic
  normalized-query exact-match cache (default-OFF). ⚠️

Everything else in the diagrams is accurate, with the optional/default-OFF caveats captured
in the tables above. The genuinely **core, always-on** libraries are: FastAPI, Uvicorn,
Pydantic, httpx, `regex`, argon2-cffi, SQLAlchemy, **LiteLLM**, PyYAML, and React/Recharts.
Everything else is an opt-in extra or a default-OFF feature.
