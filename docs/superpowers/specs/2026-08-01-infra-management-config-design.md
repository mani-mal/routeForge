# Infra Management Config — Design Spec

**Date:** 2026-08-01
**Status:** approved for planning
**Relates:** `docs/dynamo.txt` (ownership boundary this encodes), the
`inference-backends` skill (serving-path axis), `routeforge-product-pivot` (demo →
product), `routeforge-gateway-router-decisions` (LLMRouter / vLLM-semantic-router
deferrals), `routeforge-schema-no-migrations` (create_all discipline).

---

## 1. Context

routeForge is a real product deployed on customer infrastructure. Today the on-prem
serving fleet is fixed at boot: `inference_backend` (`vllm | dynamo | none`) in
`serving.yaml` is a single global switch, and the on-prem model roster (`local-8b`,
`local-32b`) is baked into `routing.yaml`. There is **no way for an operator to declare
what infrastructure routeForge is fronting** — which models are actually served
on-prem, on which engine (vLLM vs TensorRT-LLM), whether the cluster's GPUs are
scheduled by Run:ai (a licensed product) or plain Kubernetes — and **no way to take a
model out of rotation without a redeploy.**

`docs/dynamo.txt` draws the responsibility boundary that makes this tractable: routeForge
owns the *intelligent* decisions (task class, PII/residency, quality-at-cost model
choice, fallback); **NVIDIA Dynamo** owns GPU-worker routing (KV-aware balancing,
capacity, prefill/decode separation, Planner-driven replica autoscaling); the **engine**
(vLLM / TensorRT-LLM) owns in-worker batching; and **Run:ai *or* the Kubernetes node
autoscaler** owns physical GPU nodes. routeForge does **not** reimplement or deploy any
of Dynamo's capabilities — it *describes and consumes* the fleet.

## 2. Goal

Give an admin a **runtime, DB-backed, console-editable declaration of the on-prem
serving fleet** — cluster-level (Run:ai licensed? default engine?) and per-model (engine,
serving topology, enabled/available?) — that routeForge **consumes** to gate its
candidate pool, annotate its decision traces, and cross-check against live backend
health. No redeploy to enable/disable a model or change its engine. **routeForge deploys
nothing** (CLAUDE.md rule 4); Dynamo/Run:ai deployment stays external.

## 3. The ownership boundary (from `docs/dynamo.txt`)

This spec encodes exactly this split; it is the reference for what routeForge does and
does **not** build.

| Decision | Owner | This project |
|---|---|---|
| Coding / RAG / reasoning / simple question? | **routeForge** (step 4) | built |
| PII present → must stay on-prem? | **routeForge** (step 2 gate) | built |
| Which model = required quality at lowest cost? | **routeForge** (step 6) | built (+ quality scoring) |
| Should a failed request fall back to another model? | **routeForge** (step 7) | built |
| Which replica has the most useful KV cache / capacity? | **Dynamo KV router** | delegated — not built |
| Prefill and decode on separate GPU pools? | **Dynamo** | delegated — not built |
| How many worker replicas should run? | **Dynamo Planner** | delegated — not built |
| Request scheduling / continuous batching inside a worker? | **vLLM / TensorRT-LLM** | the per-model `engine` attribute |
| How many physical GPU nodes exist? | **Run:ai** *or* k8s node autoscaler | the `runai_licensed` flag |

**What routeForge folds in from the doc:** the per-model **engine** attribute (vLLM vs
TensorRT-LLM workers under Dynamo), **descriptive serving topology** (replicas, tensor
parallelism, GPU type, endpoint — the same values the admin gives Dynamo, so routeForge
*knows* them for display and cross-check), and the cluster **Run:ai license** fact
(which scheduler owns GPU placement).

**What it explicitly does NOT fold in** (owned by Dynamo / deferred elsewhere): KV-aware
routing, capacity/admission control, prefill/decode separation, Planner autoscaling,
Dynamo deployment (see `inference-backends` skill: *"Do not hand-roll Dynamo
deployment"*). vLLM Semantic Router and SIE stay deferred per
`routeforge-gateway-router-decisions`.

## 4. Architecture

Three pieces: **new DB tables**, an **admin console surface** to edit them, and a
**consume path** that reads a cached snapshot into routing + the decision trace.

```
Admin console (/admin/infra*)  --PUT-->  infra_settings / infra_models  (Postgres)
                                                  |
                                    InfraRoster snapshot (in-memory, invalidated on write)
                                                  |
        router candidate assembly (step 6) <------+------> DecisionRecord annotation
        (gate disabled/unmanaged on-prem)                  (serving_engine on chosen)
                                                  |
                                     admin console cross-check <---- live backend health
```

The **InfraRoster** is an in-memory snapshot loaded at startup and invalidated on every
admin write — the same cache-invalidation pattern already used for the quality overrides
and per-app privacy profiles. The DB is never read on the request hot path.

## 5. Data model & config ownership

**New tables — `Base.metadata.create_all` handles them, NO manual migration** (contrast
the `usage_records` column ALTER: that was a *new column on an existing table*; these are
*new tables*, cf. `routeforge-schema-no-migrations`).

### `infra_settings` — cluster singleton (row `id = "cluster"`)
| Column | Type | Notes |
|---|---|---|
| `id` | str PK | always `"cluster"` (singleton) |
| `runai_licensed` | bool | Run:ai present & licensed for GPU scheduling |
| `runai_project` | str? | Run:ai project / namespace (informational, e.g. `runai-routeforge`) |
| `default_engine` | str | `vllm` \| `trtllm` — default for a model with no explicit engine |
| `updated_at` | datetime | |

Distinct from `serving.yaml`'s `inference_backend` (`vllm | dynamo | none`), which stays
the **boot-time serving-LAYER selector**. `infra_settings` is the **runtime fleet
facts** (who schedules GPUs, default engine). No concern is owned twice: the serving
layer axis is boot infra (YAML); Run:ai license + fleet defaults are runtime ops (DB).

### `infra_models` — one row per on-prem managed model
| Column | Type | Notes |
|---|---|---|
| `model_name` | str PK | the **routing slug** (matches a `routing.yaml` on-prem `model_pool` entry) |
| `engine` | str | `vllm` \| `trtllm`; only meaningful under `inference_backend: dynamo` (heterogeneous workers). Under global `vllm` it is `vllm` by construction. |
| `enabled` | bool | admin availability toggle — **gates routing** (§6) |
| `replicas` | int? | current replica count (descriptive; Dynamo Planner actually controls) |
| `min_replicas` | int? | descriptive |
| `max_replicas` | int? | descriptive |
| `tensor_parallel` | int? | descriptive |
| `gpu_type` | str? | e.g. `NVIDIA-H200` (descriptive) |
| `endpoint` | str? | per-model endpoint override; null → the global `vllm`/`dynamo` endpoint |
| `runai_scheduled` | bool | whether this model's workers are placed via Run:ai; defaults to `infra_settings.runai_licensed` |
| `notes` | str? | |
| `updated_at` | datetime | |

**Reconciliation with `routing.yaml` (one owner per concern, preserved):**
- `routing.yaml` **keeps** owning routing identity — `model_pool` (`name`, `provider`,
  `tier`, `display_name`, `quality`); `pricing.yaml` keeps per-model USD. **Unchanged.**
- `infra_models` is **additive serving metadata** keyed by the same slug, owning **only**
  serving topology + the runtime availability toggle for **on-prem** models. It **never**
  restates tier, provider, quality, or cost.
- Runtime "managing models" = enable/disable + set engine/topology **live**. Introducing
  a brand-new *routable* model still requires the coordinated `routing.yaml` +
  `pricing.yaml` change (tier, quality, price) — a deploy, by design.
- **Cloud models (`azure`, `bedrock`) are external APIs, not fleet infra** → they are
  **not** `infra_models` rows. Their availability stays `providers.{azure,bedrock}.enabled`
  in `serving.yaml`. `infra_models` is on-prem only.

**Validation (runtime, not the boot YAML lint):**
- `engine` and `default_engine` ∈ `{vllm, trtllm}` (rejected at the admin API boundary).
- An `infra_models.model_name` with no matching on-prem `routing.yaml` pool member is a
  **surfaced warning** in the console + a structured log line — not a hard failure (the
  pool can legitimately change independently).
- The boot config lint (the four YAMLs) is **untouched**; this layer is DB/runtime.

## 6. Consume behavior — gate + annotate

**Gating.** When the router (step 6) assembles candidates, each **on-prem** pool member
is checked against the InfraRoster:
- **no `infra_models` row → treated as enabled** (a model in `routing.yaml` is assumed
  available unless an admin explicitly disables it — fail toward serving, consistent with
  the fail-safe below). It routes normally; the console shows it as "unmanaged /
  defaulted" so the admin can add an explicit row.
- **`enabled = false`** → the candidate is **excluded from the pool** and recorded as a
  `RejectedCandidate` with a human reason (`"disabled by admin"`), visible in the UI
  decision inspector exactly like a policy or capacity exclusion.
- The InfraRoster is **injected into the Router** (the same seam the `QualityResolver`
  uses), so the router stays unit-testable without a DB.

**onprem_only stays absolute (CLAUDE.md rule 3).** Cloud providers are already removed by
the gate for an `onprem_only` request. If every on-prem model is also disabled, the
candidate pool is **empty → the request is REJECTED** through the existing empty-pool
path. It is **never** lifted to cloud. This is a required test.

**Fail-safe on DB trouble.** The InfraRoster is a cached snapshot. On a read/refresh
error, routeForge uses **last-known-good**; with no cache yet, availability falls back to
the `routing.yaml` on-prem pool (fail toward *serving*, not toward outage). This is safe
because the **privacy guarantee is independent** — the gate enforces onprem_only →
never-cloud regardless of the infra layer, so a transient DB blip degrades ops
visibility, never residency.

**Annotation.** The chosen model's `engine` is attached to the routing `DecisionRecord`
as `serving_engine: str | None`, surfaced in the decision inspector ("served by:
TensorRT-LLM"). Full topology is not copied onto every record — it lives in the console.

**Cross-check (describe + consume).** The admin console compares each declared model's
`engine`/`endpoint` against live backend health (the existing `backend.health()` /
model-list probe) and shows a **match / mismatch badge** ("declared trtllm; endpoint
reports vllm"). Purely informational — routeForge never rewrites the declaration or the
backend.

## 7. Admin console + UI

**API (`/admin`, `require_admin`, matching existing route/serializer patterns):**
- `GET /admin/infra` → cluster settings (`runai_licensed`, `runai_project`,
  `default_engine`) + the boot `inference_backend` read-back (from Settings).
- `PUT /admin/infra` → upsert cluster settings.
- `GET /admin/infra/models` → the on-prem roster: the `routing.yaml` on-prem pool joined
  with `infra_models` (pool members with no row show as "unmanaged / defaulted"), each
  with a live-health cross-check badge.
- `PUT /admin/infra/models/{slug}` → upsert engine / enabled / topology for one model.

**UI — a new "Infrastructure" admin section:**
- **Cluster card:** Run:ai-licensed toggle, Run:ai project field, default-engine select,
  and a read-only **serving-layer** readout (`inference_backend`, backend health).
- **On-prem model table:** one row per on-prem model — engine dropdown (`vllm` /
  `trtllm`), enabled toggle, replicas / min-max / tensor-parallel / gpu_type / endpoint
  fields, and a live-health badge. Cloud providers are shown read-only elsewhere (they
  are not fleet infra).

Follows `demo-ui` conventions (dark, legible, entity-types-only — though no PII appears
here at all).

## 8. Run:ai semantics

`runai_licensed` is a **cluster GPU-scheduling fact**, not a routing input:
- **Licensed** → GPU placement is delegated to Run:ai (per-model `runai_scheduled`
  defaults true); the console notes workers schedule via the Run:ai project.
- **Not licensed** → plain Kubernetes / node autoscaler owns placement; `runai_scheduled`
  defaults false.

routeForge **consumes it for display and defaulting only** — it does not schedule GPUs or
call Run:ai. (Matches `dgx-runai-ops`: Run:ai's admission webhook handles scheduling; we
never set scheduler fields.) It does **not** change model scoring or the privacy path.

## 9. `docs/dynamo.txt` disposition

Promote the ownership content into the **`inference-backends` skill** (the single source
for backend/Dynamo constraints) as an "ownership boundary" section, and delete the loose
`docs/dynamo.txt` (or leave a one-line pointer). The `dynamo`/`vllm`/`none` axis
documentation there is unchanged; this spec adds the per-model `engine` attribute and the
Run:ai fact as *consumed* facts, not new serving axes — the "one serving-path axis" rule
in that skill stands.

## 10. Hard rules & invariants preserved

- **Rule 4 — no auto-deploy.** Describe + consume only; routeForge renders and applies
  nothing. (Manifest authoring/apply stays external — `deploy/dgx_lab`, NVIDIA Dynamo
  skills.)
- **Rule 3 — onprem_only absolute.** Disabling on-prem models never routes an
  onprem_only request to cloud; empty pool → reject (tested).
- **Rule 5 — fail closed.** The gate's fail-closed behavior is untouched. The InfraRoster
  fail-*safe* (last-known-good → pool) governs only ops availability, which cannot breach
  residency (§6).
- **One owner per concern.** `infra_models` never duplicates tier/provider/quality/cost;
  the boot YAML lint is untouched (§5).
- **No migration for new tables** (`create_all`); document that any later *column* on
  these tables needs the manual ALTER (cf. `routeforge-schema-no-migrations`).
- **Honesty/privacy.** No prompt/answer/PII is stored or shown in this layer — it is pure
  infra metadata. Nothing here is exported to telemetry beyond the existing
  `serving_engine` annotation on the decision trace (a model name, never content).

## 11. Testing (design-level; the plan expands)

- **Schema:** `infra_settings` singleton + `infra_models` created by `create_all`; store
  upsert/read round-trips.
- **Gating:** a disabled on-prem model is excluded with a reason; an enabled one is
  scored normally; an unmanaged pool member (no row) defaults to **enabled** and routes
  normally.
- **onprem_only + all on-prem disabled → REJECT, never cloud** (the load-bearing test).
- **Injection:** the Router scores identically with an all-enabled InfraRoster vs none
  (regression — no behavior change when nothing is disabled).
- **Annotation:** the chosen model's `serving_engine` reaches the DecisionRecord and the
  response trace.
- **Cross-check:** declared engine ≠ live health → mismatch badge; no rewrite of either.
- **Fail-safe:** InfraRoster refresh error → last-known-good used; cold cache → routing
  falls back to the on-prem pool; residency still enforced.
- **API:** `PUT`s validate `engine ∈ {vllm, trtllm}`; admin-gated; invalidate the cache.

## 12. Deferred / open

- **Manifest rendering / reconciliation** (the "render" and "fully manage" options not
  chosen) — a later spec if the customer wants routeForge to author Dynamo CRDs.
- **Per-model live capacity / KV-hit signals from Dynamo** into scoring — Dynamo owns
  these; surfacing them as a routing input is future work (Stage-3-adjacent).
- **vLLM Semantic Router** as the in-pool path selector and **SIE** for non-generative
  serving — deferred per `routeforge-gateway-router-decisions`.
- **Making the routable roster itself DB-managed** (moving `model_pool` out of
  `routing.yaml`) — deliberately *not* done here; tier/quality/price stay YAML-owned.
