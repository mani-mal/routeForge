# Model Quality Scoring — Design Spec

**Date:** 2026-08-01
**Status:** approved for planning
**Supersedes/relates:** `docs/dashboard-metrics.txt` (metric catalog this draws from),
`docs/bugs-backlog.md`, memory `routeforge-gateway-router-decisions` (LLMRouter deferral),
`routeforge-product-pivot`.

---

## 1. Context

routeForge is moving from **demo-grade prototype to a real product deployed on
customer infrastructure.** The traffic generator is no longer "the demo" — it becomes
**product tooling** (an onboarding/calibration harness). The `local-8b`/`local-32b` =
Qwen2.5-7B/32B mapping was a demo convenience and carries no design weight here.

**The problem this solves:** routeForge has **no quality label** in the pipeline. The
router approximates quality purely by tier (`_Q = {cheap:0.5, mid:0.75, frontier:1.0}`
in `app/routing/router.py:9`), and the whole "Outcome/Efficiency" half of the metric
catalog — Token Intelligence Density, Cost Intelligence Density, cost-per-successful-
task, "savings at equivalent quality" — is uncomputable without a per-model quality
number. This spec introduces one.

## 2. Goal

Give the router and the dashboard a **per-model quality score** with transparent
provenance, resolved so it is **never empty** and **never blocks a model** — including a
customer's custom-trained model with no published benchmark.

## 3. The three-stage strategy (documented; this build = Stages 1–2)

Quality-score provenance matures over the product's life. All three stages feed the
**same** `quality_score(model, task_class)` abstraction; only the source changes.

| Stage | Source | Solves | In this build? |
|-------|--------|--------|----------------|
| **1. Published** | Artificial Analysis Intelligence Index / provider benchmarks, snapshotted into config | Day-1 cold-start for known models | ✅ |
| **2. Calibrated** | Traffic-generator eval set run through the model in-boundary, scored by an LLM judge; customer-declared prior as the fast path | Custom/unknown models, and generic-benchmark ≠ customer-domain | ✅ |
| **3. Production posterior** | Blend prior toward observed outcomes (acceptance, verification pass, escalation, task success) as real traffic accrues | Long-run accuracy to the customer's actual workload | ❌ **deferred** |

**Why not Stage 3 now:** it reopens the Improvement Plane's feedback loop (see §10) and
needs production success signals we are not wiring yet. The abstraction is built so
Stage 3 slots in later without touching the router.

## 4. Architecture — the quality resolver

A single function the router and metrics consume:

```
resolve_quality(model, task_class) -> (score: float in [0,1], source: QualitySource)
```

**Resolution ladder (first hit wins; last rung always succeeds):**

```
1. customer-declared   (DB: model_quality, source=customer)      highest authority
2. calibrated          (DB: model_quality, source=calibrated)    Stage-2 output
3. published           (config: routing.yaml model_pool[].quality, source=published)
4. tier default        (routing.yaml tier_quality — the current _Q, source=tier_default)
                                                                  never empty
```

`QualitySource = customer | calibrated | published | tier_default`. The resolved
`(score, source)` is attached to the routing **DecisionRecord**, so every trace shows
*both* the number and where it came from (e.g. *"quality 0.74 (calibrated on your
data)"* vs *"quality 0.50 (tier default — no published benchmark)"*).

**Task-class dimension (design call):** ship a **single scalar quality per model now.**
The resolver signature already takes `task_class`, and the config/DB schema shape a
`quality_by_class` map for later, but no domain classifier is built in this scope
(YAGNI). AA's domain indices (Agentic/Coding/Finance/Legal/Healthcare) map onto
`quality_by_class` when it's wanted. Until then `task_class` is ignored and the scalar
is returned.

## 5. Data model & config ownership

**One owner per concern (config lint enforced).** Quality is a **routing** input →
lives in `config/routing.yaml`, never `pricing.yaml` (USD only).

- **`routing.yaml`** gains:
  - `tier_quality: {cheap: 0.5, mid: 0.75, frontier: 1.0}` — the current hard-coded
    `_Q` promoted to config (single source; the tier-default rung reads it).
  - optional `quality:` (float 0–1) on each `model_pool` entry — the published snapshot.
- **`app/config/schema.py`** — `PoolCandidate` gains `quality: float | None = None`
  (validated 0–1). `RoutingCfg` gains `tier_quality: dict[str,float]`.
- **Config lint** (new rule): every enabled `model_pool` model must **resolve** to a
  quality via the ladder — which always succeeds because `tier_quality` covers every
  tier. The lint's real job: reject a `tier_quality` missing a canonical tier, and
  reject a per-model `quality` outside [0,1]. No model can boot unscored.
- **DB — new table `model_quality`** (customer-deployment scoped): `model_name,
  task_class (nullable), score, source (customer|calibrated), calibrated_at,
  eval_run_id (nullable), notes`. A **new table**, so `create_all` handles it — **no
  manual migration** (cf. `routeforge-schema-no-migrations`; only new *columns* need
  ALTERs). This is model metadata, **not** a promotion/registry system (§10).

## 6. Router integration

`app/routing/router.py` `score()` replaces `_Q[c.tier]` with
`resolve_quality(c.name, task_class).score`. `_Q` becomes the `tier_quality` config
read used by the tier-default rung. `_L` (latency) is untouched. Weights
(`weights.quality`) unchanged. The resolver is injected (constructed from Settings +
a DB accessor) so the router stays unit-testable without a DB.

**"Savings at equivalent quality"** becomes computable: the all-frontier baseline is
re-expressed as "cheapest route achieving the *same quality band* as the frontier
model," so the savings headline reflects value delivered at held quality, not just raw
price delta.

## 7. Stage 2 — the calibration harness

`tests/traffic/generator.py` graduates to `app/` as a **model-onboarding calibration
tool** (it stops being test-only; the burst demo path keeps using it):

1. Operator points the harness at a new/custom model (already in the pool or being added).
2. Harness runs a **representative eval set** through the model **in-boundary**.
3. Each output is scored by an **LLM judge** (NeMo Agent Toolkit Eval / Ragas), producing
   a 0–1 quality score.
4. Score is written to `model_quality` with `source=calibrated`, `eval_run_id`,
   `calibrated_at`. The router picks it up on the next request (cache-invalidated like
   the existing profile cache).

**Fast path:** a **customer-declared** score (map the custom model to a known reference,
or assign a number) writes `source=customer` and skips the eval run — available day-1.

**Residency (Rule 3/6):** the eval set and judge run **inside the customer boundary**;
no customer content and no eval I/O leaves. If the judge is itself an LLM call, it goes
**through the gateway** (steps 5–7), respects `allowed_providers`, and emits a
DecisionRecord — no unrouted side channel (matches the routing-and-gateway skill rule).

## 8. Dashboard metrics unlocked

**In scope now** (computable from static quality + already-measured tokens/cost):
- **Token Intelligence Density** = Σquality ÷ Σworkflow-tokens × 1000
- **Cost Intelligence Density** = Σquality ÷ Σcost
- **Savings at equivalent quality** (§6)

**Still deferred (need a production success signal → Stage 3):** cost-per-successful-
task, route-success-rate, regression delta. Logged, not built.

## 9. Artificial Analysis as a source

- **What we take:** the per-model **Intelligence Index** (composite 0–100 → normalize to
  0–1) as the `published` rung; domain indices reserved for `quality_by_class` later;
  their USD/1M and tokens/sec as a **cross-check** on our `pricing.yaml` (not a
  replacement — pricing stays our owned value).
- **Constraint:** **no public API** (premium tier). Published values are a **periodic
  manual snapshot** committed to `routing.yaml`, with a dated `notes:`/comment recording
  the snapshot date. Published is a **seed, never the system of record** — Stages 2–3 are
  what make the number true for the customer.

## 10. Governance — narrow CLAUDE.md amendment (needs sign-off)

`CLAUDE.md` currently: *"The Improvement Plane (training, evaluation gates, promotion,
registry) is NOT built here. Do not add it."* This spec adds **evaluation** (Stage 2's
judge-scored calibration). Amendment, kept deliberately narrow:

> **Model quality calibration (Stage 2) is in scope: an in-boundary eval harness that
> scores a model's quality for routing.** It does **not** add training, promotion gates,
> auto-deploy, or a model registry. Rule 4 ("No auto-deploy of anything") stands. Stage 3
> (production-feedback quality) remains out of scope until separately approved.

All other hard rules (fail-closed, `onprem_only` absolute, redact-on-egress, nothing
external before the gate, CCR residency) are unaffected.

## 11. Honesty & privacy invariants (unchanged)

- DecisionRecord shows entity **types** only; quality provenance is metadata, never PII.
- Calibration eval I/O and the `model_quality` table stay in-boundary; never exported to
  telemetry.
- The judge call (if LLM) is routed and priced like any other call.

## 12. Testing (design-level; plan expands)

- Resolver ladder: customer > calibrated > published > tier_default; each rung hit.
- Unknown model with only a tier → returns tier default, `source=tier_default`, never raises.
- Config lint: missing `tier_quality` tier → boot failure; per-model `quality` >1 → boot failure.
- Router: given two same-tier models with different `quality`, higher-quality wins when
  cost/latency equal; tier-default path reproduces today's behaviour (regression).
- Calibration writes `source=calibrated`; next route reflects it (cache invalidation).
- Residency: calibration produces no telemetry export; judge call respects `allowed_providers`.
- Metrics: TID/CID computed correctly over a mixed run; savings-at-equal-quality baseline.

## 13. Deferred / open

- **Stage 3** production-posterior blending (separate spec + governance approval).
- **`quality_by_class`** + a domain/task classifier (schema ready; not built).
- **AA snapshot automation** — manual for now; revisit if they expose an API.
- Curating the **representative eval set** per customer domain (harness accepts a set;
  sourcing it is an onboarding activity, not code).
- **Stage-1 seam (carry into Stage 2):** `store.load_quality_overrides` returns
  `dict[str, tuple[float, str]]`, but `QualityResolver` consumes
  `dict[str, QualityScore]`. Wiring the store output straight into the resolver would
  `AttributeError` on `.score`. Harmless in Stage 1 (nothing wires them — the router
  always builds `QualityResolver(settings)` with empty overrides); Stage 2's DB→router
  refresh MUST adapt the `(score, source)` tuples into `QualityScore` objects when
  building the override snapshot.
