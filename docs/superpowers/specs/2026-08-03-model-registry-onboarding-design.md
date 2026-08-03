# Model Registry & Onboarding — routeForge as a pure router

**Date:** 2026-08-03
**Status:** Design — awaiting review
**Supersedes (in part):** `2026-08-01-infra-management-config-design.md` (removes the
Cluster card, the engine selector, and the replicas/TP/GPU fleet controls it introduced).

## Problem

Customers run different on-prem models and routeForge must route to them. Today a model
is onboarded only by hand-editing `routing.yaml` + `pricing.yaml`, matching a
`--served-model-name` on the single shared vLLM/Dynamo endpoint, and restarting. There
is no runtime path, and the admin surface is full of inference-infrastructure controls
(engine choice, replicas, tensor-parallel, GPU type, Run:ai licensing) that imply
routeForge deploys and manages inference — which it does not and must not (CLAUDE.md
Rule 4; `docs/architecture/inference-layer-boundary.md`).

## Decision: routeForge routes, it does not run inference

routeForge is a **pure router** over externally-deployed, OpenAI-compatible endpoints.
The operator deploys and scales each model independently (vLLM, Dynamo+TensorRT-LLM,
a NIM, anything OpenAI-compatible) and **onboards** it into routeForge by registering
where it is and how to route/price/scope it. routeForge never deploys, scales, load-
balances, or chooses an inference engine. Those are inference-layer concerns owned by
the operator and by Dynamo/vLLM behind the endpoint.

An on-prem model, from routeForge's point of view, is exactly:

```
{ slug (id), display_name, provider=onprem, tier, quality?,
  price_input, price_output, endpoint, api_key_env, context_window?,
  enabled, allowed_app_ids, probe_status }
```

Nothing about engine, replicas, TP, GPU, KV, scaling, or Run:ai. Those are removed.

### Locked decisions (from brainstorming)

1. **DB-backed overlay + hot reload.** Onboarded models live in the DB and merge over
   the static YAML pool at request time; the router re-reads on change — no restart.
2. **Per-model endpoint.** Each onboarded model carries its own `endpoint` + `api_key_env`.
3. **Probe-gated activation.** On onboard, routeForge probes the endpoint; pass → enabled
   and routable; fail → saved disabled with the error surfaced. Never routable-but-broken.
4. **Full purge.** Remove the global `inference_backend: vllm|dynamo|none` axis and the
   `vllm:`/`dynamo:` blocks from `serving.yaml`. Update CLAUDE.md and the
   `inference-backends` skill. Purge the engine/replicas/TP/GPU/Run:ai admin surface.
5. **Remove the built-in Qwen2.5-7B-Instruct** (`local-8b`/`local-32b`) from the YAML
   pool + pricing. routeForge ships with no on-prem model; the operator onboards.
6. **App-scoped onboarding.** Onboarding captures which application(s) may use the model,
   wired into the existing per-app `allowed_models` machinery.
7. **Seed one sample onboarded model on first boot** (demo mode only), pointing at a
   compose-provided stub endpoint, so `docker compose up` is a working demo with zero
   manual steps. No real model or weights ship inside routeForge — the seed is a fixture
   registering a fake endpoint, honestly labeled as a stub, and trivially deletable.

### Non-negotiables preserved

- **Privacy-reject becomes derived, not declared.** The old `inference_backend: none` →
  "reject `onprem_only`" hard rule is preserved as: *if a request's allowed providers
  reduce to on-prem and the effective on-prem pool has no `enabled` + healthy member,
  the request is rejected (fail-closed).* Same guarantee, no engine switch behind it.
- **`onprem_only` / residency / risk-floor ordering** is unchanged. Onboarded models are
  `provider=onprem`, so they flow through the existing residency filter with no special
  case (`router.py` residency filter; `context.py` allowed-provider computation).
- **One owner per concern / config lint** stays; the "every tier has ≥1 member" invariant
  moves from the static YAML to the merged runtime pool (see §Config).

## Architecture

```
                 ┌────────────────── effective pool ──────────────────┐
 YAML built-ins  │  routing.yaml model_pool (cloud tiers only now)     │
   (cloud)  ─────┤                    ∪                                │──▶ Router (step 6)
 DB onboarded    │  onboarded_models rows where enabled=true           │    prices via price_for()
   (on-prem) ────┘                                                     │    routes via endpoint_for()
                 └─────────────────────────────────────────────────────┘
                              produced by ModelRegistry (extends InfraRoster)
                              reload() re-reads DB on onboard/edit/delete → no restart
```

Every reader of the pool/pricing goes through `ModelRegistry`, never through
`settings.routing.model_pool` / `settings.pricing.models` directly.

### Components

#### 1. `onboarded_models` table (new) — the single model table
Self-sufficient rows. Replaces `infra_models` (which was an operational overlay for the
now-removed built-ins) and its `InfraSettings` sibling — both are **removed**.

| column | notes |
|---|---|
| `slug` (PK) | model id; the `--served-model-name` sent to the endpoint |
| `display_name` | UI label |
| `provider` | always `onprem` in v1 |
| `tier` | `cheap \| mid \| frontier` (canonical) |
| `quality` | optional [0,1]; falls back to tier default |
| `price_input`, `price_output` | USD / 1M tokens |
| `endpoint` | OpenAI-compatible base URL (e.g. `.../v1`) |
| `api_key_env` | env var holding the key for this endpoint |
| `context_window` | optional metadata |
| `enabled` | operator on/off; set by probe on create |
| `allowed_app_ids` | JSON list of app ids; **empty = usable by all apps** |
| `probe_status`, `probe_error` | last probe result + message |
| `notes`, `created_at` | |

**Migration:** `create_all` never `ALTER`s (see memory *No DB migrations*). Fresh DBs get
the table from `create_all`; deployed DBs need an explicit `CREATE TABLE IF NOT EXISTS`
step + a documented drop of the orphaned `infra_models`/`infra_settings` tables. No
existing table is altered, so nothing else breaks.

#### 2. `ModelRegistry` (extend `InfraRoster`, `app/routing/infra.py`)
- `effective_pool() -> list[PoolCandidate]` — YAML pool ∪ enabled onboarded models.
- `price_for(slug) -> PriceCfg` — merged pricing; raises a typed, caught error (not a raw
  `KeyError`) if missing.
- `endpoint_for(slug) -> (base_url, api_key_env)` — for onboarded models; None for cloud
  built-ins (they read provider env as today).
- `is_allowed_for_app(slug, app_id) -> bool` — model `allowed_app_ids` check.
- `has_healthy_onprem() -> bool` — drives the derived privacy-reject.
- `reload()` — re-read DB; called after every onboard/edit/delete.

#### 3. Router (`app/routing/router.py`)
- Read candidates from `registry.effective_pool()`, price from `registry.price_for()`.
- Residency filter unchanged (drops `provider not in ctx.allowed_providers`).
- Per-app filter also drops onboarded models not allowed for the request's app
  (`registry.is_allowed_for_app`). This composes with the app's own `allowed_models`
  (both constraints apply — intersection).
- **Derived reject:** if after residency filtering the only allowed provider is on-prem
  and `effective_pool()` yields no enabled on-prem candidate, raise the fail-closed
  rejection (preserves the ex-`none` guarantee).

#### 4. Gateway backend (`app/gateway/litellm_backend.py`, `factory.py`)
- `_map`: on-prem candidate → `registry.endpoint_for(slug)` (its own base_url + key);
  cloud candidate → provider env (`api_base_env`/`api_key_env`/`api_version_env`) as today.
- Remove the `serving.vllm.base_url` / `serving.dynamo.endpoint` branches (those settings
  are gone).
- `factory.py`: `make_backend` constructs one `LiteLLMBackend` with the registry injected.
  **Remove `resolve_backend`'s boot health-check-that-exits-on-onprem-unreachable** — model
  reachability is now established per-model at onboard time via the probe, not at boot.

#### 5. Config purge
- `serving.yaml`: delete `inference_backend`, the `vllm:` and `dynamo:` blocks. Keep
  `providers` (onprem/azure/bedrock availability + `litellm_prefix`); `providers.onprem`
  keeps `enabled` + `litellm_prefix: openai` and a default `api_key_env` (fallback when an
  onboarded model names none).
- `routing.yaml`: remove `local-8b`/`local-32b` from `model_pool`; keep tiers, weights,
  thresholds, escalation, cloud pool entries.
- `pricing.yaml`: remove the `local-8b`/`local-32b` price rows; keep `baseline_model`.
- `app/config/schema.py`: drop `inference_backend`/`vllm`/`dynamo` fields from `ServingCfg`.
- `app/config/loader.py`: stop loading the removed fields.
- `app/config/lint.py`: move `TIER_HAS_NO_MEMBER` to evaluate the **merged** pool (empty
  YAML tiers are legal); keep provider-name consistency (`onprem/azure/bedrock` across
  serving/routing/pricing) and the pool-slug ↔ pricing-slug name cross-check (closes the
  current KeyError gap for cloud built-ins). Onboarded-model validation lives at onboard
  time, not in the boot lint.

#### 6. Admin API (`app/api/admin_routes.py`)
- `POST /admin/infra/models` — onboard. Body = the writable columns above. Flow:
  1. **Static validate:** slug unique (vs YAML built-ins + onboarded); tier canonical;
     `price_input/output` present; `endpoint` well-formed URL; `api_key_env` set; each
     `allowed_app_ids` refers to a real application.
  2. **Merged-lint** — re-run cross-file rules against the merged pool (no drift, no
     provider-name conflict).
  3. **Probe:** `GET {endpoint}/models` (fallback: 1-token `POST /chat/completions`),
     assert the slug is served. Pass → `enabled=true`; fail → `enabled=false` +
     `probe_error`.
  4. Persist → `registry.reload()` → return the row with probe result.
- `PUT /admin/infra/models/{slug}` — edit onboarded fields; re-probe; reload.
- `DELETE /admin/infra/models/{slug}` — remove an onboarded model; reload.
- `GET /admin/infra/models` — list onboarded models with tier, endpoint, app scope,
  `enabled`, and live health.
- **Remove** `GET/PUT /admin/infra` (the `InfraSettings`: `default_engine`, `runai_licensed`,
  `runai_project`) — those concepts are purged.

#### 7. UI (`ui/src/admin/InfrastructurePanel.tsx`, `ui/src/api.ts`)
- **Remove** `ClusterCard`, `EngineSelect`, and the Engine/Replicas/TP/GPU columns.
- Fleet table columns become: **Model** (name + id) · **Tier** · **Endpoint** ·
  **Apps** (allowed) · **Enabled** · **Health** · actions (edit / delete).
- Add an **"Onboard model"** form/modal: display name, id/slug, tier dropdown, endpoint,
  `api_key_env`, price in/out per 1M, optional quality/context-window, and an
  **applications multi-select** (empty = all apps). Submit shows the probe result inline
  (green routable / red with error). Onboarded rows get edit + delete; nothing is
  read-only-because-built-in anymore.
- `api.ts`: replace `InfraModel`/`InfraSettings` types + `infraApi` calls with the new
  onboard/list/edit/delete shapes; drop `infraApi.get/put`.

#### 8. Stub inference endpoint (new) — GPU-less demo fixture
The removed in-process mock backend is re-homed as a **standalone OpenAI-compatible stub
service** (`/v1/models`, `/v1/chat/completions`) with deterministic seeded
latency/confidence by prompt hash (reuse the old mock logic). Added to `docker-compose`.
It is not a routeForge backend mode — it is *an endpoint routeForge points at*, exactly
like a real one.

#### 9. First-boot seed (demo mode only)
No real model or weights are bundled in routeForge. The seed is a **fixture**: on first
boot it inserts one enabled `cheap`-tier `onboarded_models` row pointing at the stub
endpoint, scoped to all apps.

- **Honest identity.** `display_name = "Sample Local Model (stub)"`, `slug =
  sample-local-stub`. It must **not** impersonate a real vendor model (no "Qwen2.5-7B"
  label) — a canned echo server presented as a real model is exactly the quiet lie the
  project avoids (cf. the panel's "declared, not verified" convention).
- **Demo-only gating.** Inserted only when the stub endpoint is present (demo mode) — a
  real DGX/GreenLake deploy has no stub and gets no fake model. Idempotent: skip if any
  onboarded model already exists.
- **Deletable / meant to be replaced.** The demo opens by deleting the sample and
  onboarding the operator's real endpoint — the "onboard your real model" story stays
  front and centre rather than hiding behind a canned model.

### Data flow — onboarding a Nemotron (the acceptance walkthrough)
0. Demo opens with the pre-seeded `sample-local-stub` (pointing at the stub endpoint);
   the admin deletes it to onboard a real model.
1. Operator deploys Nemotron on their cluster → an OpenAI-compatible URL + key env.
2. Admin opens Infrastructure → **Onboard model**: name "Nemotron Super 49B", id
   `nemotron-super-49b`, tier `mid`, endpoint, `api_key_env`, prices, apps.
3. Save → static validate → merged-lint → probe. Green → enabled, `registry.reload()`.
4. The model is immediately a routing candidate (no restart). A request from an allowed
   app whose tier/risk lands on `mid` can now be routed to it; its cost uses the onboarded
   price; residency/`onprem_only` treat it as on-prem.
5. Disable or delete it from the fleet table at any time; reload is immediate.

## Error handling
- Probe failure → model saved **disabled** with `probe_error`; never routable.
- Missing price / bad tier / malformed endpoint / unknown app id → 4xx, nothing persisted.
- Duplicate slug → 4xx.
- Request needs on-prem but no healthy on-prem model → fail-closed reject (derived
  privacy guarantee), with a clear DecisionRecord reason.
- Registry read of a missing price → typed error surfaced in the DecisionRecord, not a
  raw crash.

## Testing
- Onboard happy path: validate → probe pass → enabled → routable after reload.
- Probe fail → saved disabled, not routable, error surfaced.
- Static validation rejects: dup slug, missing price, bad tier, malformed endpoint,
  unknown app id.
- App scoping: model with `allowed_app_ids=[A]` is a candidate for A, excluded for B;
  empty list = all apps; composes with the app's own `allowed_models`.
- Derived privacy-reject: `onprem_only` request with no healthy on-prem model → rejected,
  never routed to cloud.
- Merged lint: empty YAML tier is legal; cloud pool-slug without a price still fails lint.
- Delete/disable removes the candidate on next request without restart.
- Regression: existing residency / risk-floor / escalation / per-app tests still pass
  against the merged pool.

## Docs & skill updates (in scope)
- **New** `docs/` onboarding guide: deploy-externally → onboard via UI/API → field
  reference → probe semantics → app scoping → privacy behavior → migration/seed notes.
- **CLAUDE.md:** remove the `inference_backend` axis, the `mock|vllm|dynamo|none` backend
  rules, and `serving_paths`; reframe on-prem as onboarded endpoints; update the step-8
  module row, the `serving.yaml` ownership row, the demo narrative, build order §9, and
  pitfalls.
- **`inference-backends` skill:** reframe as "routeForge points at OpenAI-compatible
  endpoints and deploys nothing; onboarding registers them." Keep Dynamo/vLLM knowledge
  as *what the operator runs behind the endpoint*, explicitly outside routeForge.
- **`routing-and-gateway` skill:** update the serving-paths section (no `inference_backend`
  axis; endpoints come from the registry).

## Scope boundaries (YAGNI)
- **On-prem onboarding only** in v1. Cloud models stay YAML built-ins gated by
  `providers.{azure,bedrock}.enabled`; no cloud onboarding UI.
- **Declares, never deploys.** No deploy/scale/Run:ai calls. The onboarding guide points
  to external deployment; routeForge only registers + probes.
- No per-model recompression, no engine-aware routing, no auto-migration framework beyond
  the documented `CREATE TABLE IF NOT EXISTS` + orphan-drop.

## Open follow-ups (not this spec)
- Cloud-model onboarding through the same flow.
- Moving cloud built-ins out of YAML into the registry for full symmetry.
