# P2b-3: offline, human-approved quality calibration — design

**Date:** 2026-09-24 · **Status:** design approved by owner; spec awaiting review.
**Programme:** P2b-1 FeasibleRouteBench → P2b-2 outcome records (PR #68) → **P2b-3 (this spec)**.

## 1. Why

Every onboarded model scores at its tier-default quality. When several eligible models tie
on price and tier, which one serves is decided by a tie-break, not a judgement. This was
observed live twice (research §10, §11.1): Llama fails every claim summary and the router
cannot know that.

P2b-2 now records what happened per request and per workload, together with the
feedback scores. P2b-3 turns those records into quality estimates the router can use,
**without the router ever changing itself.**

## 2. Owner decisions (2026-09-23/24)

| Question | Decision |
|---|---|
| How learning reaches routing | **Offline and human-approved.** An offline job recommends; an admin reviews and applies it. Routing stays deterministic and auditable, and nothing self-modifies at runtime. This is the 2026-08 scope decision: self-improvement is offline, human-gated and recommend-only. |
| What counts as quality evidence | **Feedback plus verify failures.** The explicit `quality` score (0–1) and `route_correct` are evidence. Step-9 `verify_failed` rows are negatives. Unlabelled rows count only toward coverage. |
| Promotion gate | **A replay diff, then a human applies.** The logged decisions are re-ranked with the proposed qualities and the admin reviews the diff. A dry run precedes apply. Every applied set is versioned and can be reverted. |

**Out of scope:**
- online or bandit learning;
- exploration, randomised routing and propensity logging;
- LLM-as-judge labels;
- a FeasibleRouteBench gate on apply (optional, manual);
- any change to eligibility.

## 3. Existing substrate (verified 2026-09-24)

- **The quality ladder** (`app/routing/quality.py`) is customer > calibrated > published >
  tier_default. `QualityResolver.resolve(model, task_class=None)` accepts `task_class` but
  ignores it.
- **The `model_quality` table** (`app/apps/models.py`) has these columns: `model_name`,
  `task_class` (reserved, nullable), `score`, `source` (`customer | calibrated`),
  `calibrated_at`, `eval_run_id`, `notes`.
- **`store.load_quality_overrides`** returns one winning scalar row per model, with
  customer beating calibrated.
- **Gap: nothing in serving calls it.** `Router` builds `QualityResolver(settings,
  registry=…)` with no overrides (`router.py:98`). So a `customer` or `calibrated` row
  written today has **no effect on routing**. This slice must wire it (§7).
- **Outcome rows** (`routing_outcomes`) carry `scoring`: the first decision's
  `RankResult.to_record()` with weights, per-candidate terms and their provenance. That
  record is enough to recompute that decision's ranking offline (research §7.3), and the
  replay (§5) relies on it.

## 4. Estimator (`app/eval/calibration.py`, file-based)

- **Input:** an outcome export in NDJSON (`routeforge.outcome/v1`), loaded by the existing
  `app/eval/outcomes.load_export`. Records are grouped by `endpoint_registry_version`. A
  recommendation is computed per registry-version group and **never pooled across groups**.
  The recommendation names the group it came from, and by default the job uses the newest
  group.
- **Unit:** `(model, workload)` for each non-null `workload`, plus `(model, *)` over all of
  that model's rows, including untagged ones.
- **Credit** goes to the served model (`chosen`). Rows are excluded when:
  - `cache_hit` is true;
  - `chosen` is null;
  - the status is `no_eligible`, `backend_failed` or `context_overflow`, because those say
    nothing about answer quality.
- **Evidence per row:**
  - each feedback `quality` value in [0,1] adds that fraction as a success and 1 as a
    trial;
  - each `route_correct` value of 0 or 1 adds that success and 1 trial;
  - a `verify_failed` row adds 0 successes and 1 trial.
  - **A row with no label adds nothing**, but still counts toward `n_rows` and
    `label_coverage`.
- **Prior.** `Beta(α0, β0)` with `α0 = k·q0` and `β0 = k·(1−q0)`. `q0` is the model's
  quality today, from the published or tier default in the export's `scoring` quality
  terms, or from a `--prior` file when absent. `k` is the pseudo-count (default 10). The
  posterior is `Beta(α0 + s, β0 + (t − s))`.
- **Output per unit:**
  - the posterior mean `q̂` and a 90% equal-tailed interval (from the Beta quantiles);
  - `n_labels` (trials), `n_rows` and `label_coverage`;
  - `q0` and the shift `q̂ − q0`.
- **Recommendation rule.** A unit is `recommended` only when all of these hold:
  - `n_labels ≥ min_labels` (default 20);
  - the interval width `≤ max_ci_width` (default 0.25);
  - `|q̂ − q0| ≥ min_shift` (default 0.02).
  Otherwise the unit is `insufficient` (with the reason) or `no_change`.
- **Determinism:** the same input and options give a byte-identical `recommendation.json`.
  Floats are rounded to 6 decimal places and keys are sorted.

## 5. Replay diff: the evidence an admin reviews

- **Which records:** for each outcome record with a `scoring` record (the request's first
  decision), recompute every candidate's score with the recorded weights and terms. The
  quality term is replaced by the **proposed** quality when a recommended unit applies:
  the per-workload unit if the record's workload has one, else the `(model, *)` unit.
  Otherwise the recorded quality is kept.
- **Re-ranking** uses the same tie-break as `DefaultRoutingStrategy`.
- **Replay only reorders the logged eligible set**, so it can never change eligibility or
  placement.
- **Equivalence invariant:** replaying with **no** changes reproduces the logged choice for
  every record (`scoring.candidates[0]`). The report and a test both assert this. A
  mismatch aborts the report, because it means the scoring record is not recomputable.
- **Per workload, the report shows:**
  - `decisions_changed / decisions_total`;
  - a from→to move matrix;
  - for each new pick, its **logged** evidence in this export: its served count, mean
    feedback quality, verify failures and mean cost;
  - a clear `no evidence` flag when the new pick was never served for that workload.
- **Honest caveat,** printed first in the report: the replay says which decisions would
  change. It does not say what those decisions would have produced, because production
  observes only the served arm.
- **Output:** `recommendation.json` and `report.md`.
  - `recommendation.json` carries `schema: routeforge.quality-recommendation/v1`, a
    `set_id` (a content hash), the export's hash and registry version, the options used,
    and the unit list (recommended units are the ones apply acts on).
  - `report.md` holds the estimates, the insufficient units with their reasons, and the
    replay diff.

## 6. Apply and revert (admin API, `app/api/admin_routes.py`, `require_admin`)

- **`POST /admin/quality/calibrations?dry_run=true|false`**
  - **Body:** a `recommendation.json`.
  - **Validation:**
    - the schema major must be supported;
    - `set_id` must equal the recomputed content hash;
    - every model must exist in the current registry;
    - every score must be in [0,1];
    - the registry version in the recommendation must equal the current
      `registry_version()`, or the request must carry `force_registry_mismatch=true`,
      which is reported.
    - Any failure returns 422, and nothing is written.
  - **Dry run:** returns, per recommended unit, the current effective quality and source,
    the proposed score, and whether a `customer` row would still win.
  - **Apply:**
    - in one transaction, it writes one `model_quality` row per recommended unit:
      `source="calibrated"`, `task_class=<workload>` (or NULL for `*`), `score=q̂`,
      `eval_run_id=set_id` and `notes="P2b-3 set"`;
    - a set already applied is idempotent (a 409, or a no-op; see the plan);
    - it then refreshes this replica's quality overrides at once (§7).
- **`GET /admin/quality/calibrations`** lists the applied sets (set_id, applied_at, unit
  count).
- **`DELETE /admin/quality/calibrations/{set_id}`** deletes that set's rows and refreshes.
  Other sets and customer rows are untouched.
- **Precedence:** a `customer` row always beats `calibrated` for the same
  `(model, task_class)`.
- **Several applied sets:** the newest calibrated row per `(model, task_class)` wins, by
  `calibrated_at`.

## 7. Router use (serving)

- **The request context carries the workload.** The route already parses and validates the
  workload tag (P2b-2). Add `workload: str | None = None` to `ExecutionPolicyContext` and
  thread it in through `build_context`. It is content-free, validated, and **never read by
  eligibility**.
- **Loading the overrides:**
  - `store.load_quality_overrides` returns a mapping keyed by `(model, task_class | None)`,
    with customer beating calibrated and the newest calibrated row winning (per §6);
  - its result is loaded at boot and refreshed every `routing.quality_refresh_s` (default
    60, and single-owned in routing.yaml) by the existing background loop;
  - apply and revert also refresh at once;
  - a refresh failure keeps the last good snapshot and logs a warning.
- **Sharing the snapshot:** the router and the gateway share **one** `QualityResolver`
  whose override snapshot is swapped atomically. It is a plain dict replaced by reference;
  there are no awaits in `resolve`.
- **Resolution:** `QualityResolver.resolve(model, task_class)` checks, in order:
  1. the `(model, task_class)` override;
  2. the `(model, None)` override;
  3. published;
  4. tier default.
  The source label stays `customer` or `calibrated`, and the override also carries its
  `set_id` (null for customer).
- **Call site:** the strategy calls `resolve(cand.name, ctx.workload)`.
- **Traceability:** the scoring record's quality provenance gains `set` (the calibration
  `set_id`) when the source is `calibrated`. It is content-free, it stays in the `scoring`
  JSON, and it needs no ALTER.
- **Identity:** with no `model_quality` rows, every decision is byte-identical to today.
  A test pins this.
  *Amended 2026-09-24:* the identity holds with one exception.
  Onboarded mid and frontier models now resolve to their tier-default quality (0.75
  and 1.0) instead of the 0.5 floor. The old resolver was built before the registry
  loaded, so every onboarded model fell to the floor. That was a bug, recorded as B54
  in `docs/backlog/bugs.md`, and is fixed here. Cheap-tier and static YAML pool
  scores are unchanged. A golden test pins the YAML-pool scoring record verbatim.

## 8. Privacy and safety

- Everything is content-free: the inputs are the content-free export, and the outputs are
  numbers, model names and workload slugs.
- The apply API is admin-only. The recommendation file is validated and hash-checked.
- Nothing affects eligibility, placement, residency or the circuit breaker; quality only
  reorders the eligible set.
- A calibrated score is bounded to [0,1], so it can move the quality term by at most
  `weights.quality` (1.0).

## 9. Testing

- **Estimator:** golden posteriors, intervals and statuses, hand-computed on a small
  fixture export. Per-registry-version grouping. Exclusions (cache hits, failed
  statuses). Determinism.
- **Replay:**
  - the no-change equivalence over a fixture whose `scoring` records come from the real
    `DefaultRoutingStrategy` (generated in the test, not hand-written);
  - hand-checked flips;
  - the `no evidence` flag.
- **Apply API:** dry run, apply, idempotency, revert, a hash mismatch (422), an unknown
  model (422), a registry mismatch (422, and allowed with force), customer precedence, and
  admin-only access (401).
- **Serving:**
  - the resolver falls back in order;
  - a per-workload calibration changes the pick only for that workload's requests;
  - no rows means identical decisions;
  - a refresh failure keeps the last snapshot;
  - the scoring record carries `set`.
- **Isolation:** `app/eval/calibration.py` stays file-based; serving never imports
  `app/eval`.

## 10. Docs

- A new `docs/reference/quality-calibration.md` covering the workflow (export →
  recommend → dry run → apply → revert), the estimator, the options and the caveats.
- Research §14: the model, the invariants and the tests that pin them, and what it can
  and cannot support.
- `docs/guides/feasible-route-bench.md`: a pointer to the new workflow.
- Backlog: P2b-3 marked shipped.

## 11. Deploy

- **No schema change.** `model_quality` already exists with `task_class` and
  `eval_run_id`.
- A new config key, `routing.quality_refresh_s`, which is linted.
- **Behaviour change:** existing `customer` or `calibrated` rows in `model_quality`
  (if any) **start affecting routing**, because nothing loaded them before. Before
  deploying, check the Spark DB for existing rows and name them in the release notes.
