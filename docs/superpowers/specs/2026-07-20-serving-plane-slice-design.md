# Serving Plane — Core Vertical Slice Design

**Date:** 2026-07-20
**Status:** Approved (design), pending spec review
**Scope:** BUILD_PLAN phases 1, 2, 3, 3b — the privacy + cost core, testable end-to-end.

## 1. Goal & scope

Build the smallest vertical slice of the Serving Plane that proves **claim 1
(privacy-first)** and **claim 2 (cost reduction)** in running, tested code:

```
Request → policy gate → task mode → [compression stub] → router → mock gateway
        → output verification → governed response + full DecisionRecord trace
```

The slice is an OpenAI-compatible API (`/v1/chat/completions`) running on the **mock**
backend — no GPU, no network, no API keys. It is exercised by a pytest suite and a
live `uvicorn` smoke run.

**In scope (fully built):** `app/` pipeline (steps 1–4 rules-only, 6, 7, 8-mock, 9,
10), the cross-file config lint, `tests/`, and `scripts/`.

**Scaffolded as typed stubs this pass** (later phases fill them, same interface):
`app/agent/` (NAT → "direct"), `app/compression/` (Headroom pass-through), `app/obs/`
(Langfuse export → no-op), `gateway/backends/{vllm,dynamo}.py`, `ui/` (Vite skeleton),
`deploy/compose` + `deploy/helm` (skeletons), `tests/traffic/` (stub generator).

**Every tool is specified in §10** (role, integration point, config surface, interface,
phase) even when its build is a later phase — the design is complete; only the build is
sliced. The one thing genuinely **out of this repo entirely** is the Improvement Plane.

Authoritative *rules* live in `CLAUDE.md` and the eight skills under `.claude/skills/`;
this spec does not restate them — §10 pins the *seams*, the skills own the *constraints*.
Config **values** live in `config/*.yaml` (single-sourced, lint-enforced).

## 2. Directory layout

`█` = fully built this pass · `░` = typed stub this pass.

```
routeForge/
├── pyproject.toml                █ uv project: fastapi, uvicorn, pydantic v2,
│                                   presidio-analyzer/anonymizer, pyyaml, httpx, pytest
├── app/
│   ├── main.py                   █ app factory; startup: load+lint config, warm detector
│   ├── config/
│   │   ├── loader.py             █ 4 YAMLs + env override → typed Settings (pydantic-settings)
│   │   └── lint.py               █ cross-file invariants → exit(1) w/ named violation
│   ├── models.py                 █ shared Pydantic models + enums (§4)
│   ├── api/routes.py             █ steps 1+10: /v1/chat/completions, /health
│   ├── policy/
│   │   ├── detectors.py          █ Detector protocol: PresidioDetector | RegexDetector | NullDetector
│   │   ├── gate.py               █ step 2: detect → classify → redact → residency
│   │   └── context.py            █ step 3: ExecutionPolicyContext (frozen) + allowed_providers
│   ├── routing/
│   │   ├── task_mode.py          █ step 4: rules-first direct vs multi-step (classifier OFF → "direct")
│   │   └── router.py             █ step 6: residency filter → risk floor → score → escalate
│   ├── gateway/
│   │   ├── gateway.py            █ step 7: adapter dispatch; fallback re-checks allowed_providers
│   │   └── backends/
│   │       ├── base.py           █ InferenceBackend Protocol
│   │       ├── mock.py           █ step 8: seeded text/latency/cost/confidence
│   │       ├── vllm.py           ░ stub → InferenceBackend (Phase 9; inference-backends skill)
│   │       └── dynamo.py         ░ stub → InferenceBackend (Phase 9; NVIDIA skills)
│   ├── output/verify.py          █ step 9: leakage re-scan, require_verification, stream reject
│   ├── compression/__init__.py   ░ Headroom pass-through: tokens_after == tokens_before (Phase 5b)
│   ├── agent/__init__.py         ░ NAT runtime: returns "direct"; typed interface only (Phase 5)
│   └── obs/export.py             ░ NAT→OTel→Langfuse redacting exporter; no-op now (Phase 4)
├── tests/
│   ├── conftest.py               █ fixtures: settings w/ pii_detection=regex, mock backend, seed
│   ├── test_config_lint.py       █
│   ├── test_policy_gate.py       █
│   ├── test_router.py            █
│   ├── test_backends.py          █
│   ├── test_output_controls.py   █
│   ├── test_e2e_slice.py         █ each in-scope persona through the real pipeline
│   └── traffic/
│       ├── README.md             ░ → traffic-and-tests skill
│       └── __main__.py           ░ stub generator (prints "not implemented yet")
├── scripts/
│   ├── README.md                 █ lists all scripts
│   ├── dev-backend.sh            █ uv run uvicorn app.main:app --reload --port 8080
│   ├── dev-frontend.sh           █ cd ui && npm run dev   (starts Vite skeleton)
│   ├── test.sh                   █ uv run pytest -q
│   ├── smoke.sh                  █ boot backend + httpx checks (§7 live smoke)
│   ├── lint-config.sh            █ uv run python -m app.config.lint
│   └── traffic.sh                █ uv run python -m tests.traffic  (wraps the stub)
├── ui/
│   ├── README.md                 ░ → demo-ui skill
│   ├── package.json              ░ Vite + React + Tailwind skeleton
│   └── index.html + src/main.tsx ░ renders "Serving Plane UI — coming in Phase 6"
├── deploy/
│   ├── compose/docker-compose.yml ░ skeleton (api service only, commented services)
│   └── helm/serving-plane/        ░ Chart.yaml + values.yaml + stub templates + README
├── config/                       (exists, lint-clean)
└── docs/                         (exists)
```

**Boundary rule:** every module has one purpose and a typed interface; a stub exposes
the same interface its real version will, so wiring never changes when it's filled in.

## 3. Request flow

```
POST /v1/chat/completions  (api/routes.py)
 1. If body.stream is true → 400 "streaming not supported in demo" (no model call).
 2. gate.evaluate(request) → ExecutionPolicyContext
       · authenticate: `Authorization: Bearer <key>` → tenant via a static
         api-key→tenant map (in Settings; keys map to the `policy.yaml` tenant ids
         acme/hospital/default). Missing/unknown key → `default` tenant in demo mode
         (documented convenience; this is auth, not the fail-closed policy rule —
         a *gate error* still rejects). Tests pass the header explicitly.
       · detect entities (detector), classify sensitivity (policy.yaml rules),
         redact to placeholders, determine residency, compute allowed_providers
       · ANY exception → HTTP 503, zero model calls (fail closed)
 3. task_mode(ctx, request) → "direct"   (multi_step deferred to agent phase)
 4. compression stub → post_compression_tokens = raw token count (pass-through)
 5. router.select(ctx, pool, task) → RoutingDecision
       · filter pool to allowed_providers (residency FIRST)
       · apply risk floor to the *remaining* tiers (residency beats risk floor)
       · score survivors; pick best; record rejected[] with reasons
 6. gateway.generate(chosen, messages) → GenerationResult
       · mock backend: seeded by prompt hash → text, latency_ms, tokens, cost, confidence
       · confidence < confidence_escalation_threshold → escalate one tier, re-run (cap = max_escalations)
       · on backend failure → fallback candidate, re-checking allowed_providers
 7. verify(ctx, completion) → VerificationRecord
       · re-scan completion for PII; redact on egress
       · if ctx.require_verification and unverifiable after escalation cap → fail closed (safe error)
       · onprem_only escalation stays within on-prem tiers, never cloud
 8. Response: OpenAI-shaped body (placeholders retained — demo mode, no detokenization)
       + trace: list[DecisionRecord] (one per step) + cost totals + baseline_cost_usd
```

Cost accumulates per hop; `baseline_cost_usd` = same tokens priced at
`pricing.yaml.baseline_model`. Demo mode returns placeholders (per privacy-gate
decision); detokenization is a config flag not exercised here.

## 4. Data models (`app/models.py`)

Pydantic v2. Enums are built from `policy.yaml`'s canonical scales at load time (not
hard-coded), so the config lint is the single source.

- `Sensitivity` = `public | internal | confidential | restricted`
- `RiskClass` = `low | medium | high`
- `Residency` = `any | private_cloud_ok | onprem_only`
- `Message`, `ChatRequest`, `ChatResponse` — OpenAI-compatible subset (model, messages,
  stream, usage, choices).
- `ExecutionPolicyContext` — `model_config = ConfigDict(frozen=True)`; fields per the
  privacy-gate skill (`request_id, tenant_id, sensitivity, onprem_only,
  allowed_providers: frozenset[str], allowed_models: frozenset[str], max_cost_usd,
  max_latency_ms, risk_class, require_verification: bool, detected_entities:
  tuple[str,...]` (TYPES only), `redaction_map_id: str | None`).
- `RejectedCandidate` — `name, provider, tier, reason`.
- `DecisionRecord` — `step, chosen, reason, rejected: list[RejectedCandidate],
  est_cost_usd, actual_cost_usd: float | None, latency_ms, hop_index`.
- `VerificationRecord` — `step="verify", required, performed, passed, entities_found:
  tuple[str,...], escalated, escalations_used, reason, latency_ms`.
- `CompressionRecord` (stub emits it with applied=False) — per headroom skill, incl.
  `ccr_retrievals_to_cloud`.

The redaction map (placeholder → original value) is held in-process, keyed by
`redaction_map_id`, per-request scope, never in the trace, never returned. Treated as a
second PII store (privacy-gate skill).

## 5. Config lint (`app/config/lint.py`)

Runs at startup (and via `scripts/lint-config.sh`). Loads all four YAMLs and asserts,
exiting non-zero with a **named** violation on failure:

1. Provider names identical across `serving.providers`, `routing.model_pool[].provider`,
   `pricing.models[].provider` (all `onprem`/`azure`/`bedrock`).
2. Every tier in `routing.risk_floors` and every `model_pool[].tier` ∈ `routing.tiers`,
   and every tier in `routing.tiers` has ≥1 pool member. Pricing tiers ⊆ `routing.tiers`.
3. No concern owned by two files (no duplicated `max_spend_usd`, thresholds, agent caps
   across serving/routing/policy).
4. One sensitivity enum: `policy.sensitivity_scale` is the only scale; `default_sensitivity`
   and every rule's `sensitivity` ∈ it; residency values spelled `onprem_only`.

This is the exact check already validated against the current YAMLs. Verified: the
current configs pass.

## 6. Detectors (`app/policy/detectors.py`)

`Detector` protocol: `analyze(text) -> list[Entity]`, `anonymize(text, entities) ->
(redacted_text, redaction_map)`. Three implementations selected by
`serving.policy.pii_detection`:

- `PresidioDetector` — `presidio` (demo default). Adds a custom recognizer for
  `EMPLOYEE_ID`. May require a spaCy model download.
- `RegexDetector` — `regex`. Deterministic patterns for SSN, email, phone, credit card,
  bank number. No network. **The pytest suite uses this** so tests are offline-safe on
  any machine.
- `NullDetector` — `off`. No detection (never used in tests; present for completeness).

Startup warms the selected detector once (avoids first-request latency).

## 7. Test plan

**pytest** — in-scope critical assertions (numbers cross-ref traffic-and-tests skill):

| File | Asserts |
|---|---|
| `test_config_lint.py` | drifted config (`on_prem` in one file / missing tier) → exit non-zero, named violation; current config passes |
| `test_policy_gate.py` | SSN/email/phone → detected, redacted, `onprem_only=True`; clean → `public`, full pool; gate exception → 503, no model call; context frozen; redaction map absent from trace |
| `test_router.py` | clean → cheap; `onprem_only` → cloud excluded w/ reason; high-risk+cloud → frontier; **high-risk+onprem_only → best on-prem + require_verification, never cloud**; low-confidence → exactly one escalation |
| `test_backends.py` | mock satisfies Protocol; `inference_backend=none` + `onprem_only` → rejected, zero cloud calls |
| `test_output_controls.py` | PII in completion → redacted on egress; `require_verification` unverifiable → fail closed (unverified completion never returned); `stream=true` → 400, no model call |
| `test_e2e_slice.py` | simple_qa → cheap; pii_bearing → onprem_only + cloud in rejected[]; cost accumulates and matches sum of parts; baseline_cost_usd computed |

Fixtures fix time, seed randomness, force `pii_detection=regex` and the mock backend. No
GPU or network. Runs on a laptop.

**Live smoke** (`scripts/smoke.sh`, the `/verify` discipline): boot `uvicorn`, then
httpx:
- clean request → 200, trace shows cheap tier chosen;
- request with an SSN → 200, `onprem_only`, cloud providers in `rejected[]` with reason;
- `stream: true` → 400 with a clear message;
- `/health` → active backend + model list.

## 8. Success criteria (done = all true)

1. `scripts/lint-config.sh` passes on current config; a deliberately drifted config
   fails the boot with a named violation.
2. `scripts/test.sh` → full in-scope pytest suite green on a laptop, offline.
3. `scripts/smoke.sh` → the four live checks above pass against a booted server.
4. Every response carries a `DecisionRecord[]` trace with per-step reasons and rejected
   candidates (the "why" is the demo).
5. No raw PII in any trace/response beyond the deliberate placeholder; redaction map
   never leaves the gate.
6. All stub areas (`ui/`, `deploy/`, `tests/traffic`, `app/compression`, `app/agent`)
   exist with typed interfaces / skeletons and READMEs pointing at their skills.
7. `git` history on `main`; work done on a feature branch, spec committed.

## 9. Open questions / deferred decisions

- Detokenization on egress: implemented as a config flag but **off** (demo returns
  placeholders). Revisit when the UI can show the before/after.
- Real confidence signal: mock emits a seeded scalar; logprob-based signal lands with
  real backends (Phase 9).
- Streaming: rejected in the demo; production incremental verification is a separate
  design.

## 10. Tool & technology integration map

Every third-party tool in the architecture, where it plugs in, and when it's built.
Tool-specific rules are **not restated here** — they live in the named skill; this
section pins down the *seam* so a stub exposes the exact interface its real version
will fill. Status: `BUILT` this pass · `STUB` typed placeholder this pass.

### 10.1 Presidio — PII/PHI detection · `BUILT`
- **Role:** step 2 detection + anonymization (the privacy claim).
- **Integration:** `app/policy/detectors.py` → `PresidioDetector` behind the `Detector`
  protocol; `gate.py` calls `analyze()`/`anonymize()`. Selected by
  `serving.policy.pii_detection: presidio`.
- **Config surface:** `pii_detection` (`presidio | regex | off`); custom recognizers
  (e.g. `EMPLOYEE_ID`) registered at startup.
- **Interface:** `analyze(text) -> list[Entity]`, `anonymize(text, entities) ->
  (redacted, redaction_map)`.
- **This pass:** real, but tests use the `regex` sibling for offline determinism.
- **Skill:** `privacy-gate`.

### 10.2 NeMo Guardrails + OPA-style policy rules — gate reasoning · `STUB` (rules-only now)
- **Role:** step 2 input/output guardrails and tenant policy decisions beyond entity
  detection.
- **Integration:** `app/policy/gate.py`. This pass uses **plain rules** from
  `policy.yaml` (`sensitivity_rules`, residency, `require_verification`). NeMo
  Guardrails rails and an OPA-style evaluator are the later, richer implementation
  behind the same `gate.evaluate()` interface — no pipeline change when added.
- **Config surface:** `policy.yaml` (rules) now; a `guardrails:` block later.
- **Interface:** `gate.evaluate(request) -> ExecutionPolicyContext` (stable).
- **Skill:** `privacy-gate`.

### 10.3 NeMo Agent Toolkit (NAT, `nvidia-nat`) — agent runtime + telemetry source · `STUB` (Phase 5)
- **Role:** multi-step workflows (step 4 → runtime); also the **origin of telemetry**
  (it emits OpenTelemetry spans that Langfuse consumes).
- **Integration:** `app/agent/__init__.py`. The key design fact (from the skill): the
  **gateway is registered as NAT's model endpoint**, so every LLM call the agent makes
  re-enters compression (5) and the router (6) — per-call, not per-request. This pass
  the stub returns `"direct"` and no agent loop runs; `task_mode.py` never selects
  multi-step yet.
- **Config surface:** `serving.agent.runtime: nat`; caps in `policy.agent_budgets`
  (`max_steps`, `max_spend_usd`, `per_call_timeout_s`).
- **Interface:** `run(ctx, request, gateway) -> AgentResult` (hops → DecisionRecords).
- **Skill:** `nemo-agent-toolkit`.

### 10.4 Langfuse — observability sink · `STUB` (Phase 4)
- **Role:** stores every trace with cost/latency and **redacted** PII (the observable
  claim).
- **Integration:** `app/obs/export.py` — a redacting span processor on the OTel export
  path NAT feeds. Redaction runs at the **exporter** (egress), not only intake, and
  CCR contents/redaction-map values are never exported. Self-hosted via compose
  (Postgres + ClickHouse). This pass: a no-op exporter that satisfies the interface;
  DecisionRecords are returned in the response trace instead.
- **Config surface:** `serving.observability.langfuse` (`enabled`, `host`,
  `redact_on_export: true` — mandatory).
- **Interface:** `export(trace: list[DecisionRecord | VerificationRecord]) -> None`
  with a redaction hook.
- **Skill:** `nemo-agent-toolkit`.

### 10.5 Headroom (`headroom-ai`) — conditional context compression · `STUB` (Phase 5b)
- **Role:** step 5, the **second, independent savings lever** — compresses tool
  outputs/history so the router prices fewer tokens.
- **Integration:** `app/compression/__init__.py`, sitting **after** the gate (only sees
  sanitized content) and **before** the router (which prices `tokens_after`). This pass
  it is a **pass-through**: emits a `CompressionRecord` with `applied=False` and
  `tokens_after == tokens_before`, so the router's post-compression pricing path is
  wired and testable now. CCR (local-only, TTL, never in telemetry) and CacheAligner
  are part of the real Phase-5b build. The canonical-tokenizer approximation (compress
  once vs cheap-tier tokenizer) resolves the 5↔6 cycle.
- **Config surface:** `serving.context_optimization` (`enabled`, `mode`, `min_tokens`,
  `bypass_on`, `ccr`, `cache_alignment`).
- **Interface:** `compress(messages, ctx) -> CompressionResult(messages, tokens_before,
  tokens_after, record)`.
- **Skill:** `headroom-compression`.

### 10.6 vLLM — on-prem serving path · `STUB` (Phase 9)
- **Role:** real on-prem inference (OpenAI-compatible), + Semantic Router model-path
  selection within a vLLM pool.
- **Integration:** `app/gateway/backends/vllm.py` behind the `InferenceBackend`
  Protocol; an `AsyncOpenAI` client at `serving.vllm.base_url`. Selected by
  `inference_backend: vllm`. Populates the on-prem tier from `/v1/models` at startup.
- **This pass:** stub raising `NotImplementedError`; mock covers the on-prem tier.
- **Skill:** `inference-backends`.

### 10.7 NVIDIA Dynamo + TensorRT-LLM — multi-GPU serving path · `STUB` (Phase 9)
- **Role:** load distribution / KV-aware routing / disaggregated prefill-decode across
  TensorRT-LLM workers. GPU + K8s only; exercised in GreenLake, not on a laptop.
- **Integration:** `app/gateway/backends/dynamo.py` behind `InferenceBackend`;
  OpenAI-compatible frontend at `serving.dynamo.endpoint`. Selected by
  `inference_backend: dynamo`. Deployment via NVIDIA's verified skills — not hand-rolled.
- **Config surface:** `serving.dynamo` (`endpoint`, `router_mode`, `timeout_s`).
- **This pass:** stub; the full `npx skills add nvidia/skills …` list lives in the
  `inference-backends` skill.
- **Skill:** `inference-backends` (+ NVIDIA `dynamo-*` skills).

### 10.8 FastAPI · Pydantic v2 · uvicorn · uv — framework · `BUILT`
- **Role:** the API surface (steps 1/10), typed boundaries, ASGI server, dependency mgmt.
- **Integration:** `app/api/routes.py`, `app/models.py`, `app/main.py`; `pyproject.toml`.
- **This pass:** real.

### 10.9 React + Vite + Tailwind + Recharts — demo UI · `STUB` (Phase 6)
- **Role:** flow visualizer, decision inspector, cost dashboard (routing vs compression
  split), control panel. Consumes the SSE decision stream + DecisionRecords.
- **Integration:** `ui/` — Vite skeleton renders a placeholder now; components land in
  Phase 6.
- **Config surface:** `serving.server.stream_decisions` (SSE/WebSocket).
- **Skill:** `demo-ui`.

### 10.10 Docker Compose + Helm — packaging · `STUB` (Phase 8)
- **Role:** local demo (`docker compose up` < 5 min, no GPU) and HPE GreenLake/K8s deploy.
- **Integration:** `deploy/compose/docker-compose.yml` (api + commented langfuse/vllm),
  `deploy/helm/serving-plane/` (Chart, values, stub templates). Everything in
  `serving.yaml` settable via Helm values.
- **This pass:** skeletons only; healthchecks, images, GreenLake values in Phase 8.
- **Skill:** `greenlake-deploy`.

**Summary — built vs stubbed this pass:**

| Tool | Module | This pass | Phase |
|---|---|---|---|
| Presidio | `policy/detectors.py` | **BUILT** (regex sibling for tests) | 2 |
| Rules gate (Guardrails/OPA later) | `policy/gate.py` | **BUILT** (rules-only) | 2 |
| FastAPI/Pydantic/uvicorn | `api`, `models`, `main` | **BUILT** | 1 |
| Mock backend | `gateway/backends/mock.py` | **BUILT** | 3 |
| NAT agent runtime | `agent/` | STUB → "direct" | 5 |
| Langfuse export | `obs/export.py` | STUB → no-op | 4 |
| Headroom | `compression/` | STUB → pass-through | 5b |
| vLLM | `gateway/backends/vllm.py` | STUB | 9 |
| Dynamo + TensorRT-LLM | `gateway/backends/dynamo.py` | STUB | 9 |
| React UI | `ui/` | STUB → placeholder | 6 |
| Compose + Helm | `deploy/` | STUB → skeleton | 8 |
