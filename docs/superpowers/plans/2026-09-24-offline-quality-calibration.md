# P2b-3 Offline Quality Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn P2b-2 outcome exports into per-(model, workload) quality recommendations. An
admin reviews them through a replay diff and applies or reverts them, and the router then
ranks with them. The router never changes itself.

**Architecture:**
- **Serving:** `model_quality` rows finally reach the router, through one shared
  `QualityResolver` whose override snapshot is loaded at boot, refreshed on a timer, and
  refreshed immediately on apply or revert. The resolver is keyed by `(model, task_class)`,
  where `task_class` is the request's validated workload. It never affects eligibility.
- **Offline:** a file-based estimator and replay in `app/eval/calibration.py` produce a
  hash-identified `recommendation.json`.
- **Admin:** an admin-only API validates a recommendation, dry-runs it, applies it and
  reverts it.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, pytest. No new
dependencies. Beta quantiles come from `scipy` only if it is already a dependency (check
`pyproject.toml`); otherwise use the pure-Python fallback in Task 3.

**Spec:** `docs/superpowers/specs/2026-09-24-offline-quality-calibration-design.md`

## Global Constraints

- **Tests:** run them with `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest <tests> -p no:cacheprovider -W ignore`, using that absolute venv. Do not use `-qq`. The full suite takes about 8 minutes: `2>&1 | /usr/bin/grep -E "passed|failed" | tail -2`. For grep, use `/usr/bin/grep`.
- **No schema change.** `model_quality` already has `task_class` and `eval_run_id`. No ALTER.
- **No rows means no change.** With no `model_quality` rows, every routing decision and
  scoring record must be **byte-identical** to today.
- **Quality only reorders the eligible set.** It must never touch eligibility, placement,
  residency or the breaker. Workload is never read by `feasible_set`.
- **Precedence** for a `(model, task_class)`:
  - `customer` beats `calibrated`;
  - among calibrated rows, the newest `calibrated_at` wins;
  - resolution order is `(model, workload)` → `(model, None)` → published → tier_default.
- **The recommendation file:**
  - its schema string is exactly `routeforge.quality-recommendation/v1`;
  - `set_id` is the first 16 hex characters of the sha256 of the canonical JSON (Task 3);
  - apply refuses a file whose recomputed hash does not match (422).
- **Estimator defaults:** `k=10`, `min_labels=20`, `max_ci_width=0.25`, `min_shift=0.02`,
  and a 90% equal-tailed interval.
- **Evidence:**
  - feedback `quality` in [0,1] adds that fraction as successes, plus 1 trial;
  - `route_correct` of 0 or 1 adds that value as successes, plus 1 trial;
  - a `verify_failed` row adds 0 successes and 1 trial;
  - rows are excluded when `cache_hit`, when `chosen` is null, or when the status is
    `no_eligible`, `backend_failed` or `context_overflow`.
- **Grouping:** never pool across `endpoint_registry_version`. By default use the newest
  group.
- **Refresh config:** `routing.quality_refresh_s` defaults to 60 and is owned by
  routing.yaml. The lint rejects values `<= 0`.
- **Isolation:** `app/eval/calibration.py` is file-based. Serving never imports `app/eval`,
  and `tests/eval/test_isolation.py` must pass. The admin apply API validates the
  recommendation with a small **serving-side** validator in `app/apps/calibration_sets.py`,
  not by importing `app/eval`.
- **Replay equivalence:** replaying with no changes must reproduce the logged choice for
  every record, or the report aborts.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
  Match the surrounding comment density.

---

### Task 1: Store — keyed overrides, calibration-set CRUD, refresh config

**Files:**
- Modify: `app/apps/store.py`:
  - `load_quality_overrides` (~line 650);
  - new `write_calibration_set`, `list_calibration_sets`, `delete_calibration_set`.
- Modify: `app/config/schema.py`: `RoutingCfg.quality_refresh_s: float = 60.0`.
- Modify: `config/routing.yaml`: the `quality_refresh_s: 60` key, with a comment.
- Modify: `app/config/lint.py`: `QUALITY_REFRESH_NOT_POSITIVE`.
- Test: `tests/test_quality_overrides_store.py` (new), `tests/test_config_lint.py`.

**Interfaces:**
- **Produces:**
  - `load_quality_overrides(session) -> dict[tuple[str, str | None], tuple[float, str, str | None]]`
    maps `(model, task_class)` to `(score, source, set_id)`, where `set_id` is
    `eval_run_id` for calibrated rows and None for customer rows. Customer beats
    calibrated; among calibrated rows the newest `calibrated_at` wins.
  - `write_calibration_set(session, *, set_id: str, units: list[dict]) -> int` writes one
    row per unit with keys `model`, `workload` (None for `*`) and `score`, using
    `source="calibrated"`, `eval_run_id=set_id` and `notes="P2b-3 set"`. It returns the
    count and raises `ValueError` if the set already exists.
  - `list_calibration_sets(session) -> list[dict]` returns `{set_id, applied_at, units}`,
    newest first.
  - `delete_calibration_set(session, set_id) -> int` returns the number of rows deleted.
  - `Settings.routing.quality_refresh_s`.

- [ ] **Step 1: Write the failing tests.** Use an in-memory DB, following
  `tests/test_outcome_store.py`.
  1. With no rows, the result is `{}`.
  2. A scalar customer row and a scalar calibrated row for the same model: customer wins.
  3. Per-workload rows are keyed `(model, "claims")` and are separate from `(model, None)`.
  4. Two calibrated sets for the same `(model, task_class)`: the one with the newer
     `calibrated_at` wins, and its `set_id` is returned.
  5. `write_calibration_set` twice with the same `set_id` raises `ValueError`.
  6. `list_calibration_sets` groups by `eval_run_id`.
  7. `delete_calibration_set` removes only that set and leaves customer rows alone.
  8. Lint: `quality_refresh_s=0` produces `QUALITY_REFRESH_NOT_POSITIVE`, and the shipped
     config lints clean.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Keep `_QUALITY_SOURCE_RANK`. Selecting over all rows (drop
  the `task_class IS NULL` filter) is fine, because the table is small. The row id is
  `uuid4().hex`.
  - First, grep for callers of the old `load_quality_overrides` signature
    (`/usr/bin/grep -rn load_quality_overrides app tests scripts`) and update them.
  - The docstring must say that nothing in serving loaded this function before P2b-3.
- [ ] **Step 4: Run the tests and see them pass, then the full suite.**
- [ ] **Step 5: Commit** `feat(quality): keyed quality overrides + calibration-set store (P2b-3)`

---

### Task 2: Serving — shared resolver, workload-aware resolution, boot load + refresh

**Files:**
- Modify: `app/routing/quality.py`:
  - `QualityScore` gains `set_id: str | None = None`;
  - `QualityResolver` gains overrides keyed `(model, task_class)` and
    `set_overrides(new: dict)`, which does an atomic reference swap;
  - `resolve(model, task_class)` falls back in order.
- Modify: `app/models.py`: `ExecutionPolicyContext.workload: str | None = None`, with a
  comment that eligibility never reads it.
- Modify: `app/policy/context.py` `build_context`, `app/policy/gate.py`, and
  `app/pipeline.py` `run`/`run_stream`: thread `workload` from the route through to the
  context.
- Modify: `app/api/routes.py`: pass the parsed workload into `pipeline.run(...)` and
  `pipeline.run_stream(...)`.
- Modify: `app/routing/strategy.py` (~line 157): call
  `quality_resolver.resolve(cand.name, ctx.workload)`. In `to_record`, the quality becomes
  `{"v", "src"}`, plus `"set": set_id` only when `set_id` is present, so records stay
  byte-identical when there is no calibration.
- Modify: `app/routing/router.py`: `chosen_q = self._quality.resolve(chosen.name, ctx.workload)`.
- Modify: `app/main.py`:
  - build ONE `QualityResolver(settings, registry=registry)` and store it on
    `app.state.quality`;
  - pass it to `Router(..., quality_resolver=...)`;
  - in the startup event, load the overrides with `load_quality_overrides` and call
    `set_overrides`, guarded;
  - refresh every `routing.quality_refresh_s`. Either add a small background loop, or add
    a timed refresh to the existing `_observation_loop` using its elapsed-time counter the
    way the hourly prune does. On a failed refresh, keep the last snapshot and log a
    warning.
  - also add `async def refresh_quality(app) -> bool` for apply and revert to call.
- **Registry refresh:** `QualityResolver` computes tier and published quality from the
  registry pool at construction. Onboarding changes the pool, so `resolve` must read
  tier and published values through the registry live, not from a snapshot taken at boot.
  Check how the registry refresh works (`ModelRegistry.refresh`) and make the resolver
  read `registry.effective_pool()` lazily, or rebuild those maps on refresh. Keep the
  existing behaviour for callers that pass no registry.
- Test: `tests/test_quality_serving.py` (new).

**Interfaces:**
- **Consumes:** Task 1 `load_quality_overrides`.
- **Produces:**
  - `QualityResolver.set_overrides(mapping: dict[tuple[str, str | None], QualityScore])`
  - `QualityResolver.resolve(model, task_class=None) -> QualityScore` (with `.set_id`)
  - `app.state.quality`
  - `app.main.refresh_quality(app) -> bool`
  - `ExecutionPolicyContext.workload`
  - a scoring-record quality entry `{"v","src"[,"set"]}`

- [ ] **Step 1: Write the failing tests.**
  1. **Resolver order:** a `(m, "claims")` override beats `(m, None)`, which beats
     published, which beats tier_default. An unknown workload falls back to `(m, None)`.
     `set_id` is carried through.
  2. **Identity:** with an empty override map, a routed request's decision and
     `scoring.to_record()` are equal to the same request on a fresh app. Compare the full
     record dict.
  3. **Scoped change:** at the HTTP boundary, with two same-price cheap models A and B
     (A ranks first by tie-break), apply an override where `(B, "claims")` is well above A.
     A request with `x-routeforge-workload: claims` is served by B. A request with no
     workload is still served by A. The scoring record carries `"set": <id>` only on the
     claims request.
  4. **Eligibility unaffected:** an `onprem_only` request whose best-calibrated model is
     cloud still routes on-prem. Use a calibrated cloud model with score 1.0.
  5. **Refresh:** `refresh_quality` picks up a newly written row. When the load raises,
     the previous snapshot stays and it returns False.
  6. **Registry refresh:** a model onboarded after boot resolves to its tier default, not
     to the `_TIER_DEFAULT_FLOOR`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and see them pass.** Then run `tests/test_router*.py`,
  `tests/test_routing_strategy.py`, `tests/test_decision_certificate.py`, `tests/eval`,
  `tests/test_outcome_*.py` and the full suite.
- [ ] **Step 5: Commit** `feat(quality): model_quality reaches the router, per workload, with boot load + refresh (P2b-3)`

---

### Task 3: Offline estimator and recommendation file

**Files:**
- Create: `app/eval/calibration.py` (the estimator and the recommendation builder; replay
  comes in Task 4, in the same file).
- Test: `tests/eval/test_calibration_estimator.py` (new), with fixtures under
  `tests/eval/fixtures/` built in the test.

**Interfaces:**
- **Consumes:** `app/eval/outcomes.load_export(path) -> (records, warnings)`.
- **Produces:**
  - `estimate(records, *, k=10.0, min_labels=20, max_ci_width=0.25, min_shift=0.02, priors: dict[str, float] | None = None, registry_version: str | None = None) -> dict`
    returns
    `{"registry_version": ..., "options": {...}, "units": [ {model, workload, q0, q0_source, alpha, beta, mean, ci_low, ci_high, n_labels, n_rows, label_coverage, status, reason} ]}`.
    - `workload` is None for the `*` unit.
    - `status` is one of `recommended`, `insufficient` or `no_change`.
    - Units are sorted by `(model, workload is None, workload)`.
  - `build_recommendation(estimate_dict, *, export_sha256: str) -> dict` returns
    `{"schema": "routeforge.quality-recommendation/v1", "set_id": ..., "export_sha256": ..., "registry_version": ..., "options": ..., "units": [...recommended and others...]}`.
    - `set_id` is the first 16 hex characters of the sha256 of
      `json.dumps({k: v for k, v in rec.items() if k != "set_id"}, sort_keys=True, separators=(",", ":"))`.
  - `recommendation_set_id(rec: dict) -> str`, the same hash function, reused by Task 5's
    serving-side validator through a copy (see Task 5).

**Details:**
- **The prior `q0`:** for each model, take the quality term (`quality.v` plus its `src`)
  from any record whose `scoring.candidates` contains the model. Among those, prefer a
  record with `src` in (`published`, `tier_default`); `priors[model]` overrides it. If
  there is no source at all, the model's units are `insufficient` with reason `no prior`.
  Report `q0_source`.
- **Beta quantiles:** use `scipy.stats.beta.ppf` if scipy is importable. Otherwise use a
  pure-Python inverse through bisection on the regularized incomplete beta
  (`math.lgamma` plus a continued fraction, Numerical Recipes `betacf`), with a
  tolerance of 1e-10. Put it in a small private helper and test it against known values:
  - `Beta(2,2)` has its median at 0.5;
  - `Beta(1,1)` has its 0.05 quantile at 0.05;
  - `Beta(10,30)` has its 0.95 quantile at 0.3683, to 3 decimal places. Compute this
    value once with the helper and cross-check it with another method in the test
    comment.
- **Rounding:** round every float to 6 decimal places.

- [ ] **Step 1: Write the failing tests.**
  - **Hand-computed posteriors:** one model with prior `q0=0.5` and `k=10`
    (α0=β0=5), with 18 route_correct=1, 2 route_correct=0 and 2 verify_failed rows. So
    s=18 and t=22, giving `Beta(23, 9)`, a mean of 0.71875, and `recommended`. Check the
    interval against the helper.
  - **Exclusions:** cache hits, a null `chosen`, and the three failure statuses are
    excluded.
  - **Status rules:** `min_labels` not met gives `insufficient` (with reason); a shift
    below `min_shift` gives `no_change`.
  - **Grouping:** two registry versions are never pooled, the newest is chosen by default,
    and passing `registry_version` explicitly selects the other.
  - **Units:** a per-workload unit and the `*` unit both appear.
  - **Determinism:** the same input gives the same `set_id` and a byte-identical JSON.
  - **Hash:** `set_id` changes when any unit score changes.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and see them pass, including `tests/eval/test_isolation.py`.**
- [ ] **Step 5: Commit** `feat(eval): offline quality estimator + recommendation file (P2b-3)`

---

### Task 4: Replay diff, report and CLI

**Files:**
- Modify: `app/eval/calibration.py` (add `replay` and `render_report`).
- Create: `scripts/quality_recommend.py`:
  - CLI: `--export FILE --out DIR [--registry-version V] [--k] [--min-labels] [--max-ci-width] [--min-shift] [--priors FILE]`;
  - writes `recommendation.json` and `report.md`;
  - exits 0 on success, 1 on load error, and 2 on a replay equivalence failure.
- Test: `tests/eval/test_calibration_replay.py` (new).

**Interfaces:**
- **Consumes:** Task 3 `estimate` and `build_recommendation`.
- **Produces:**
  - `replay(records, recommendation) -> dict` returns
    `{"equivalence_ok": bool, "mismatches": [...], "per_workload": {wl: {decisions_total, decisions_changed, moves: {"A->B": n}, new_pick_evidence: {model: {served, quality_mean, verify_failed, cost_mean} | "no evidence"}}}}`
  - `render_report(estimate_dict, replay_dict) -> str`

**Replay algorithm.** For each record with `scoring`, do the following.
1. Take `weights` and `candidates` from `scoring`. Each candidate's score is
   `w.quality*q − w.cost*cost.norm − w.latency*min(latency.ms/latency_scale_ms, max_latency_norm) + w.availability*availability.v`,
   read the same way `app/routing/strategy.py` does. Read that file and mirror its
   formula exactly, including how it treats a disabled or missing term.
2. **Unchanged replay:** with `q` taken from the record, sort by `(-score, est_usd)`. The
   winner must equal `scoring.candidates[0].name`; otherwise it is a mismatch.
3. **Proposed replay:** with `q` replaced by the recommended unit for
   `(name, record.workload)`, falling back to `(name, None)`, recompute and re-sort. A
   decision is `changed` if the winner differs.
4. **Evidence:** for each new pick, collect evidence from records where
   `chosen == new_pick` and the workload matches: served count, mean feedback quality,
   verify_failed count, and mean cost over ok rows. If there are none, record
   `"no evidence"`.
5. **Report:** `report.md` opens with the caveat verbatim: "This replay shows which logged
   decisions would change. It cannot show what the changed decisions would have produced:
   production observes only the served model." Then it lists:
   - the recommended units;
   - the insufficient units, each with its reason;
   - the per-workload diff table.

- [ ] **Step 1: Write the failing tests.**
  - **Equivalence on real records:** build real scoring records by calling
    `DefaultRoutingStrategy().rank(...)` for a few small candidate sets (see
    `tests/test_routing_strategy.py` for how to call it). Wrap each in an outcome record
    and check that replay with an empty recommendation gives `equivalence_ok=True` and
    `decisions_changed=0`.
  - **Tamper test:** a record whose `candidates[0]` was swapped gives
    `equivalence_ok=False`, and the CLI exits 2.
  - **Proposed flip:** a hand-built flip is counted under the correct workload, and the
    move matrix is correct.
  - **No evidence:** a new pick that was never served is flagged `no evidence`.
  - **CLI:** end to end on a fixture export, it writes both files; run twice, the files
    are byte-identical; the caveat is present.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `tests/eval` in full.**
- [ ] **Step 5: Commit** `feat(eval): replay diff, report and quality_recommend CLI (P2b-3)`

---

### Task 5: Admin API — validate, dry-run, apply, list, revert

**Files:**
- Create: `app/apps/calibration_sets.py`, the **serving-side** validator. It must not
  import `app/eval`.
  - `validate_recommendation(rec: dict, *, registry, current_registry_version: str, force_registry_mismatch: bool) -> list[dict]`
    returns the recommended units and raises `CalibrationError(message)` on failure.
  - It also holds its own `recommendation_set_id(rec)`, the same canonical-JSON hash as
    Task 3. A test asserts the two implementations agree on a fixture.
- Modify: `app/api/admin_routes.py`:
  - `POST /admin/quality/calibrations` (with the `dry_run` and `force_registry_mismatch`
    query parameters, both bool, default false);
  - `GET /admin/quality/calibrations`;
  - `DELETE /admin/quality/calibrations/{set_id}`;
  - all three use `Depends(require_admin)`.
- Test: `tests/test_quality_calibrations_api.py` (new).

**Interfaces:**
- **Consumes:**
  - Task 1 `write_calibration_set`, `list_calibration_sets`, `delete_calibration_set` and
    `load_quality_overrides`;
  - Task 2 `refresh_quality(app)` and `app.state.quality`;
  - the Task 3 file format.
- **Produces:** the endpoints.

**Behaviour:**
- **Validation.** Any of these is a 422 with a message naming the problem:
  - the schema is not `routeforge.quality-recommendation/v1`;
  - `set_id` does not equal the recomputed hash;
  - a unit's `model` is not in `registry.effective_pool()` names;
  - a score is outside [0,1];
  - the registry version does not match and `force_registry_mismatch` is not set.
- **Dry run.** Returns 200 with
  `{"set_id", "registry_version_match": bool, "units": [{model, workload, current: {score, source, set_id}, proposed, customer_wins: bool}]}`.
  `current` comes from `app.state.quality.resolve(model, workload)`.
- **Apply.**
  - It validates, then calls `write_calibration_set` in one transaction.
  - A duplicate `set_id` gives **409**.
  - It then calls `refresh_quality(app)` and returns `{"set_id", "applied": n, "refreshed": bool}`.
- **Revert.** A 404 when the set is unknown; otherwise it deletes the set, refreshes, and
  returns `{"set_id", "deleted": n}`.

- [ ] **Step 1: Write the failing tests.** Use the admin-login pattern from
  `tests/test_admin_api.py::_login`, and a registry with two onboarded models.
  1. A dry run shows the proposed and current values and writes nothing.
  2. Apply writes the rows, and a routed request's scoring record now carries
     `src=calibrated` and the matching `set`.
  3. Re-applying the same set gives 409.
  4. Revert restores the previous resolution.
  5. A hash mismatch gives 422 and writes nothing.
  6. An unknown model gives 422.
  7. A registry mismatch gives 422, and 200 with force.
  8. With a customer row for the same model, the dry run shows `customer_wins: true`, and
     after apply the resolver still returns customer.
  9. With no admin session: 401.
  10. The two `recommendation_set_id` implementations agree.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and see them pass, then the full suite.**
- [ ] **Step 5: Commit** `feat(quality): admin calibration apply/dry-run/revert API (P2b-3)`

---

### Task 6: Docs

**Files:**
- Create: `docs/reference/quality-calibration.md`. It covers:
  - the workflow: export → `scripts/quality_recommend.py` → review `report.md` → dry run
    → apply → revert;
  - the estimator (the evidence rules, the prior, the options and their defaults);
  - the recommendation format;
  - the API, with examples;
  - precedence;
  - the refresh behaviour, which is per replica, with the refresh period;
  - the caveats: served-arm only, a sparse label density, and no exploration.
- Modify: `docs/research/feasible-route-bench.md`, adding §14. It follows the §13 style:
  - the model;
  - the invariants with the tests that pin them;
  - what it can and cannot support;
  - what a later exploration slice would need.
- Modify:
  - `docs/reference/README.md` (an index entry);
  - `docs/guides/feasible-route-bench.md` (a pointer);
  - `docs/backlog/bugs.md`, wherever P2b-3 is tracked (`/usr/bin/grep -rn "P2b-3" docs/backlog docs/superpowers/specs | head`).
- Modify: the spec, adding a §12 amendments section dated 2026-09-24, covering anything
  the build changed.
- Modify: `docs/reference/api-reference.md` (the admin endpoints, if that doc lists admin
  routes).
- [ ] **Step 1: Write the docs.** Check every claim against the code and cite file:line
  in the report.
- [ ] **Step 2: Run `tests/test_docs_links.py`, then the full suite once.**
- [ ] **Step 3: Commit** `docs: quality calibration reference, research §14 (P2b-3)`
