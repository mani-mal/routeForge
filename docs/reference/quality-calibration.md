# Quality calibration (P2b-3)

Every onboarded model starts at its tier's default quality (`cheap` 0.5, `mid` 0.75,
`frontier` 1.0), or a published score set at onboarding. That number never changes on its
own. Quality calibration is an **offline, human-approved** path that turns P2b-2 outcome
records into a proposed quality score per `(model, workload)`, lets an admin review exactly
which past routing decisions would have changed, and — only on explicit apply — writes the
result into `model_quality` so the router starts using it.

**Nothing here learns online.** There is no exploration, no bandit, and no in-process
promotion path. An admin runs a script, reads a report, and calls an API. See
`docs/research/feasible-route-bench.md` §14 for the model, its invariants and what a later
online-learning slice would still need.

- Design: the offline-quality-calibration design spec
  (§12 records where the build departed from the design).
- Prerequisite dataset: `docs/reference/routing-outcomes.md` (P2b-2) — this feature reads
  its export and nothing else.
- Prior slice: `docs/guides/feasible-route-bench.md` §9 is the operator pointer into this
  workflow.

## 1. Workflow

```
GET /admin/outcomes/export                          (P2b-2; admin, or own-app service credential)
  -> outcomes.ndjson
scripts/quality_recommend.py --export outcomes.ndjson --out results/quality
  -> results/quality/recommendation.json, results/quality/report.md
review report.md                                     (a human reads the replay diff)
POST /admin/quality/calibrations?dry_run=true         (preview; writes nothing)
POST /admin/quality/calibrations                      (apply)
GET  /admin/quality/calibrations                      (list applied sets)
DELETE /admin/quality/calibrations/{set_id}           (revert one set)
```

Every step is file-based or admin-API-only. Nothing on this path touches the request
serving path except the very last one: an applied `model_quality` row changes what the
router's `QualityResolver` returns on the *next* refresh (§7).

## 2. The estimator (`app/eval/calibration.py`)

File-based, offline, no DB and no `app.api` import — it reads only the NDJSON export
`load_export` (`app/eval/outcomes.py`) already parses. `tests/eval/test_isolation.py`
pins that serving never imports `app.eval`.

### 2.1 Grouping

Records are grouped by `endpoint_registry_version`; a recommendation is computed from
**exactly one group, never pooled across groups** — a registry change can change which
endpoints exist. The default group is the **newest** (the group holding the record with
the latest `ts`; a tie goes to the lexicographically greatest version label). Pass
`--registry-version` to pick a group explicitly.

### 2.2 Unit, credit, and exclusions

The unit is `(model, workload)` for every non-null workload the model served, plus
`(model, *)` over all of that model's included rows (tagged and untagged). Credit goes to
the **served** model (`chosen`). A row is excluded — contributes no evidence at all — when:

- `cache_hit` is true (no model produced the cached answer);
- `chosen` is null;
- `status` is `no_eligible`, `backend_failed`, `context_overflow` or `cancelled` (none of
  these say anything about answer quality; a `cancelled` row has no `chosen` anyway).

### 2.3 Evidence per included row

- its **latest** `quality` feedback value (by the feedback entry's own `ts`; a tie goes to
  the last entry in file order), when numeric and in `[0, 1]`, adds that fraction as a
  success and 1 as a trial;
- its latest `route_correct` feedback value, when exactly `0` or `1`, adds that value as a
  success and 1 as a trial — a separate signal from `quality`, so a row can contribute both;
- a `verify_failed` row adds 0 successes and 1 trial, in addition to any label.

Only the latest label **per score name** counts. A client that posts the same `quality`
score for one request five times contributes one trial, not five — otherwise a client
could narrow its own interval just by re-posting. A row with no evidence at all still
counts toward `n_rows` and `label_coverage` (rows with ≥1 piece of evidence ÷ `n_rows`),
just not toward `n_labels`.

### 2.4 Prior

`Beta(α₀, β₀)` with `α₀ = k·q0` and `β₀ = k·(1 − q0)`. `q0` is the model's quality **today**
before any override — read from the group's own `scoring.candidates[*].quality` terms. A
term whose `src` is `published` or `tier_default` is used as-is. A `calibrated` or
`customer` term is itself a previous recommendation or a declaration; using its `v` as a
prior would compound one calibration on top of another, so it is never eligible as `q0`
itself. Instead, whenever an override supplied the quality the router also records the
value underneath it as a content-free `base: {"v", "src"}` (`src` is `published` or
`tier_default`), and the estimator takes `q0` from that `base`. That is what lets a
**second round** work: once a set is applied, every quality term in later exports reads
`src: calibrated`, and without `base` every unit would be `insufficient`. Among qualifying
terms in the group, the newest record wins. A `--priors` JSON file (`{"model": q0, ...}`)
overrides the export's value for named models; a prior must lie strictly inside `(0, 1)`.
With no qualifying term and no `--priors` entry, the unit is `insufficient` with reason
`no prior` — or, when the model had only `calibrated`/`customer` terms with no `base`
(records written before `base` existed), `no prior: only calibrated/customer terms
without a base; pass --priors`. It is never silently scored against a default.

The posterior is `Beta(α₀ + s, β₀ + (t − s))`, its mean is the point estimate, and its
90% equal-tailed interval comes from the Beta quantile function — computed by a
**pure-Python** implementation (`math.lgamma` plus the Numerical Recipes `betacf`
continued fraction, inverted by bisection), never `scipy`, so the same export and options
give byte-identical output on any machine, with or without `scipy` installed.

### 2.5 Options and their defaults

| Option | Default | Meaning |
|---|---|---|
| `k` | `10` | prior strength, in pseudo-labels |
| `min_labels` | `20` | minimum trials (`n_labels`) for a unit to be recommendable |
| `max_ci_width` | `0.25` | widest 90% interval a recommendation may have |
| `min_shift` | `0.02` | smallest `|mean − q0|` worth recommending |

A unit is `recommended` only when **all three** gates pass: `n_labels ≥ min_labels`,
interval width `≤ max_ci_width`, and `|shift| ≥ min_shift`. Otherwise it is
`insufficient` (with a reason naming which gate failed) or, when only the shift gate
fails, `no_change`.

**How many labels that takes in practice.** The gates are stricter than they look at a
glance. A hand-worked case pinned by `tests/eval/test_calibration_estimator.py::test_hand_example_ci_too_wide_at_default`
(18 `route_correct: 1`, 2 `route_correct: 0`, 2 `verify_failed` — `s=18`, `t=22`, prior
`q0=0.5`) gives posterior `Beta(23, 9)`, whose 90% interval is 0.257049 wide: **over** the
0.25 default, so at only 22 labels the unit is `insufficient`, not `recommended`, even
though its shift (0.219) comfortably clears `min_shift`. Extending the same evidence mix
(computed with the module's own quantile function, so this is indicative, not a rule) the
interval first narrows under 0.25 at around **24–25 labels**. The exact count moves with
`q0` and the observed rate — a prior further from 0.5, or evidence closer to unanimous,
narrows faster — but "the low twenties are usually still insufficient" is a reasonable
rule of thumb at the shipped defaults.

### 2.6 Output shape and determinism

Every unit carries: `model`, `workload` (`null` for the `(model, *)` unit), `q0`,
`q0_source`, `alpha`, `beta`, `mean`, `ci_low`, `ci_high`, `shift`, `n_labels`, `n_rows`,
`label_coverage`, `status`, `reason`. Every float is rounded to 6 decimal places (and a
signed `-0.0` is normalized to `0.0`), units are sorted by `(model, workload is None,
workload)`, and the whole `recommendation.json` is byte-identical for the same input and
options.

## 3. Replay: the evidence an admin reviews

Replay never touches the live router or config — it recomputes each logged decision from
its own recorded `scoring` term (`RankResult.to_record()`), the way an offline audit would.

- **Re-ranking.** For every outcome record in the recommendation's registry group that
  carries a `scoring` record, every candidate's score is recomputed with the **recorded**
  weights and terms, substituting the **proposed** quality for a candidate whose
  `(model, workload)` unit is `recommended` (falling back to the `(model, *)` unit, else
  keeping the recorded quality). The formula and the `(-score, est_cost)` tie-break mirror
  `DefaultRoutingStrategy.rank` exactly. Replay only **reorders** the logged eligible set —
  it can never add a candidate, so it can never change eligibility or placement.
- **Equivalence invariant.** Before trusting any diff, replay first re-ranks every record
  with **no** quality changes and checks that this reproduces the **full recorded
  candidate order** (not just the winner). A record whose scoring is missing a term (for
  example a pre-P2a export that predates per-candidate scoring) cannot be recomputed at
  all: it is skipped and counted in `not_recomputable`, not treated as a failure. A record
  whose scoring is fully specified but whose unchanged replay still disagrees with the
  logged order is a genuine mismatch — that means the replay does not mirror the router,
  so nothing about the diff can be trusted, and the CLI aborts with exit code `2` and
  writes no files.
- **Per-workload report.** `decisions_changed / decisions_total`, a from→to move matrix,
  and for each new pick under a changed workload, its own **logged** evidence in this
  export (served count, mean feedback quality, verify-failure count, mean cost) — or an
  explicit `no evidence` flag when that pick was never served for that workload at all.
- **The caveat, printed first in `report.md`, verbatim:**

  > This replay shows which logged decisions would change. It cannot show what the
  > changed decisions would have produced: production observes only the served model.

## 4. `scripts/quality_recommend.py`

```bash
uv run python scripts/quality_recommend.py \
  --export outcomes.ndjson --out results/quality \
  [--registry-version REG] [--k 10] [--min-labels 20] \
  [--max-ci-width 0.25] [--min-shift 0.02] [--priors priors.json]
```

Writes `recommendation.json` and `report.md` into `--out`. Exit codes:

| Code | Meaning |
|---|---|
| `0` | wrote both files |
| `1` | load, option or numeric error — bad export path, unrecognized export schema, unknown `--registry-version`, an invalid option, a bad `--priors` file, or a Beta quantile that failed to converge (reported, never a traceback) |
| `2` | replay equivalence failure — a fully-specified scoring record's unchanged replay disagreed with its logged order; nothing is written |

A `not_recomputable` count (records skipped because their scoring was missing a term) is
a warning on stderr, not a failure — the CLI still exits `0` and writes both files.

## 5. Recommendation file format

`recommendation.json`:

```json
{
  "schema": "routeforge.quality-recommendation/v1",
  "export_sha256": "<sha256 of the export file>",
  "registry_version": "<the replayed group's endpoint_registry_version>",
  "options": {"k": 10.0, "min_labels": 20, "max_ci_width": 0.25,
              "min_shift": 0.02, "ci_level": 0.9, "priors": null},
  "units": [
    {"model": "qwen3-8b", "workload": "claims.summary",
     "q0": 0.75, "q0_source": "tier_default",
     "alpha": 36.0, "beta": 4.0, "mean": 0.9, "ci_low": 0.81302, "ci_high": 0.964201,
     "shift": 0.15, "n_labels": 30, "n_rows": 30, "label_coverage": 1.0,
     "status": "recommended", "reason": null}
  ],
  "set_id": "<16 hex chars>"
}
```

The numbers are consistent: with `k = 10` and `q0 = 0.75` the prior is `Beta(7.5, 2.5)`;
30 trials with `s = 28.5` successes (fractional `quality` labels count as fractions) give
the posterior `Beta(7.5 + 28.5, 2.5 + 1.5) = Beta(36, 4)`, mean `0.9`, 90% interval
`[0.81302, 0.964201]` (width 0.151 — under the 0.25 default).

`units` carries **every** unit the estimator computed, whatever its status — the report
needs the `insufficient` and `no_change` ones too. Apply writes a **score** only for units
with `status: "recommended"`. It also writes a **lower bound** (`ci_low`) for every
`recommended` **and** `no_change` unit that carries one, which the quality-floor router
reads (§11). An `insufficient` unit contributes neither.

`set_id` is the first 16 hex characters of the sha256 hash of the file's own canonical
JSON (sorted keys, compact separators) **with the `set_id` key itself removed** — so it is
a content hash the server can always recompute and check. Two independent implementations
compute it identically: `app/eval/calibration.py::recommendation_set_id` (offline) and
`app/apps/calibration_sets.py::recommendation_set_id` (serving) — serving never imports
`app.eval`, so it carries its own copy, and a parity test
(`tests/test_quality_calibrations_api.py::test_serving_and_eval_set_id_implementations_agree`)
asserts the two agree.

## 6. API (`app/api/admin_routes.py`, `require_admin`)

Every route below needs the admin session cookie (`401` without it, same as the rest of
`/admin/*`); errors use FastAPI's `{"detail": ...}` shape.

### `POST /admin/quality/calibrations?dry_run=true|false&force_registry_mismatch=true|false`

Body: the `recommendation.json` payload, as-is.

**Validation (in order), any failure is `422` and writes nothing:**

1. `schema`'s major version must be a supported one (currently `1`);
2. `set_id` must equal the value recomputed from the payload's own content;
3. every `status: "recommended"` unit's `model` must exist in the live registry
   (`ModelRegistry.effective_pool()`);
4. every such unit's `workload` must be `null` (the `(model, *)` unit) or a workload slug
   exactly as serving stores one — lower-case, `^[a-z0-9][a-z0-9._-]{0,63}$`, the P2b-2
   tag shape. `"*"`, upper-case, and non-strings are refused, not normalized;
5. every such unit's `mean` must be a finite number in `[0, 1]`;
6. no two recommended units may share a `(model, workload)`;
7. at least one unit must be `recommended`, or carry a lower bound (`"no recommended
   units"` otherwise — an empty set would be a no-op that could be neither `409`-blocked
   nor reverted). A file of only `no_change` units with `ci_low` is a valid **bounds-only**
   set;
8. the payload's `registry_version` must equal the live `registry.registry_version()`,
   unless the request carries `force_registry_mismatch=true`.

9. every `recommended` or `no_change` unit that has a `ci_low` (B80): `model` must exist
   in the live registry, `workload` must be `null` or a workload slug as in 4, `ci_low`
   must be a finite number in `[0, 1]` and no greater than the unit's `mean`, and no two
   such units may share a `(model, workload)`. A unit without `ci_low` carries no bound
   and is skipped, not refused.

**Dry run (`dry_run=true`, default `false`) — writes nothing:**

```json
{"set_id": "...", "registry_version_match": true,
 "units": [{"model": "qwen3-8b", "workload": null,
            "current": {"score": 0.5, "source": "tier_default", "set_id": null},
            "proposed": 0.9, "customer_wins": false, "shadowed_by": []}],
 "bounds": [{"model": "qwen3-8b", "workload": null, "lower": 0.81302, "ci_level": 0.9}]}
```

`bounds` lists the lower bounds an apply would store (B80), one per `recommended` or
`no_change` unit with a `ci_low`; `ci_level` is the file's `options.ci_level`, or `0.9`
when it states none. It is `[]` for a file without `ci_low` values. A bounds-only set has
an empty `units` and a non-empty `bounds`.

`customer_wins` is `true` when a `customer` declaration will beat this unit after apply —
computed on exactly §7's order: a `customer (model, workload)` row, or a model-wide
`customer (model, *)` row (which beats even a per-workload calibrated unit). Applying
still writes the `calibrated` row, but it will not change what the router resolves, so the
admin sees that up front instead of being surprised after apply.

`shadowed_by` lists existing rows **more specific** than a proposed `(model, *)` unit that
will keep winning for their own workload after apply:
`[{"workload": "claims", "source": "calibrated", "set_id": "..."}, ...]` (customer rows
carry `"set_id": null`). A per-workload unit has nothing more specific, so its list is
always empty.

**Apply (`dry_run=false`):** one DB transaction writes a `model_quality` row per
recommended unit (`source="calibrated"`, `task_class=<workload>` or `NULL` for `*`,
`score=mean`, `eval_run_id=set_id`, `notes="P2b-3 set"`) and a `model_quality_bounds` row
per bound (`model_name`, `task_class`, `lower`, `ci_level`, `set_id`; B80), then refreshes this replica's
quality snapshot at once. Response:

```json
{"set_id": "...", "applied": 1, "refreshed": true,
 "registry_version_match": true, "forced": false}
```

`applied` counts score rows only (0 for a bounds-only set); the bound rows are counted in
the audit line's `bounds` and in `GET`. `forced` is `true` only when `force_registry_mismatch=true` was actually needed (the
versions differed). Apply and revert each write **one** audit line on the `serving.admin`
logger — a compact JSON object of ids, counts and flags only:
`{"admin_id": "...", "event": "quality_calibration_applied", "forced": false,
"registry_version_match": true, "set_id": "...", "units": 1}` (revert:
`"event": "quality_calibration_reverted"`, `"forced": false`, `units` = rows deleted).

Re-applying the same `set_id` is **not** idempotent: it returns `409` (a `model_quality`
row with that `eval_run_id`, or a `model_quality_bounds` row with that `set_id`, already
exists) and writes nothing further.

### `GET /admin/quality/calibrations`

Lists applied sets, newest first, each `{"set_id": "...", "applied_at": "<ISO-8601>",
"units": <n>, "bounds": <n>}`: `units` counts the set's score rows, `bounds` its lower-bound
rows. A bounds-only set is listed with `units: 0`. A set applied before B80 shows
`bounds: 0` (see §11).

### `DELETE /admin/quality/calibrations/{set_id}`

Deletes every `calibrated` score row **and every lower-bound row** for that set and
refreshes this replica's snapshot at once. `{"set_id": "...", "deleted": <n>}`, where
`deleted` is score rows plus bound rows; `404` if the set has neither. Bounds-only sets
revert the same way. Other
applied sets and every `customer` row are untouched.

## 7. Precedence and serving

`model_quality` rows now reach the router — **before P2b-3, nothing in serving loaded
this table at all**, so any `customer` or `calibrated` row already present in a deployed
DB had zero effect. That changes with this feature; see the deploy note below.

`QualityResolver.resolve(model, task_class)` checks, in order:

1. `customer (model, task_class)` — a workload-scoped declaration;
2. `customer (model, None)` — a model-wide declaration;
3. `calibrated (model, task_class)` — a workload-scoped calibration;
4. `calibrated (model, None)` — the `(model, *)` calibration;
5. the model's published quality (`onboarded_models.quality`, or `quality` on a YAML pool
   entry);
6. the tier default.

An operator's `customer` declaration is never overridden by a calibration — not even by a
**more specific** per-workload one: a model-wide customer row beats a per-workload
calibrated row. The snapshot keeps customer and calibrated candidates separate per key so
this cross-key order is decidable. Among several applied `calibrated` sets touching the
same key, the row with the newest `calibrated_at` wins. Only the router reads the resolver
(the gateway reads the router's decision, never the resolver).

The scoring record's quality term is `{"v", "src"}` plus, only when an override supplied
the score:

- `set` — the calibration's `set_id`, when the source is `calibrated`;
- `base: {"v", "src"}` — the `published`/`tier_default` value the ladder would have used
  without the override (for `calibrated` **and** `customer` terms). It is what the next
  calibration round uses as its prior (§2.4).

A term with no override has exactly its pre-P2b-3 shape (no `set`, no `base`).

**B54, fixed in this slice.** `QualityResolver` used to build its tier/published-quality
maps once, at `create_app` time, before the `ModelRegistry` had loaded any onboarded row —
so **every** onboarded model silently fell through to the flat 0.5 floor, ignoring both
its real tier **and** its published `onboarded_models.quality`. The resolver now rebuilds
those maps whenever the registry's `generation` counter moves (i.e., on every successful
refresh), so an onboarded model scores its published quality when it has one — **any
tier, `cheap` included** — else its own tier default (`cheap` 0.5, `mid` 0.75, `frontier`
1.0), like a YAML pool model would. This is the one intentional change to pre-P2b-3
behaviour: with zero `model_quality` rows, YAML-pool decisions are byte-identical to
before, and an onboarded model's quality changes only if it is non-`cheap` or has a
non-null published `quality`. See `docs/backlog/bugs.md` B54.

## 8. Refresh behaviour

The shared `QualityResolver` (`app.state.quality`, one per process, read only by the
router — the gateway never reads it) holds its override snapshot as a plain
`(customer, calibrated)` pair of dicts (plus a separate lower-bound map, §11), swapped by reference in one assignment — a
concurrent `resolve()` always sees either the old snapshot or the new one, never a mix,
and `resolve()` itself has no awaits. Refreshes are serialized by a lock around the DB
read *and* the swap, so snapshots install in the order they were read: a slow timed
refresh that read before an apply committed can never finish after the apply's own
refresh and put the pre-apply snapshot back.

That snapshot is (re)loaded:

- **at boot**, before the timed refresher starts;
- **every `routing.quality_refresh_s` seconds** (default `60`; single-owned in
  `config/routing.yaml`, config-linted to be `> 0`) by a background loop, on every replica
  independently;
- **immediately**, on whichever replica handled a `POST` apply or a `DELETE` revert.

So the replica an admin's apply or revert request landed on picks up the change at once;
every *other* replica picks it up within one refresh period (up to `quality_refresh_s`
seconds later) via its own timed loop — there is no cross-replica push. A failed refresh
(a DB error, for example) keeps the last good snapshot in place and logs a warning; it
never raises and never blocks serving, because quality only ever reorders the eligible
set, so serving on a stale-but-valid snapshot is always safe.

## 9. Caveats — what this cannot do

- **Served-arm-only evidence.** Every row in the export describes what the *served* model
  did. What any other eligible model would have produced for the same request is not
  observed, so this cannot correct for the fact that the router's own past choices decide
  which model gets credit for which requests. A model's estimate reflects the requests it
  was actually sent, not a controlled comparison.
- **Sparse, non-random label density.** `quality` and `route_correct` feedback is
  voluntary. A workload with active grading will calibrate quickly; one with none never
  clears `min_labels` and stays `insufficient` indefinitely — that is by design (§2.5), not
  a bug to route around by lowering the defaults ad hoc.
- **No exploration.** Every request in the export was routed by a deterministic policy
  that had already decided; nothing here randomizes routing to go gather more labels
  faster. If a use case needs that, it needs a different, out-of-scope slice (propensity
  logging, bandit or online learning — see `docs/research/feasible-route-bench.md` §14.4).
- **Only the served (final) hop gets credit.** A request that failed over to another
  model is credited to the model that finally served it (`chosen`); the earlier hops'
  models get neither credit nor blame from that request's labels.
- **The workload namespace is global.** A workload slug is a free-form, client-declared
  tag (`x-routeforge-workload` / `metadata.workload`), and a calibration keyed
  `(model, "claims")` applies to every app that sends `claims` — not just the app whose
  feedback produced it. Apps that mean different things by the same slug share one
  calibration; pick slugs deliberately (e.g. prefix them per app) if that matters.
- **No automatic gate.** Applying a recommendation is a human action. There is no
  FeasibleRouteBench gate wired into apply; running the bench against a candidate set
  before applying it is a manual, optional step an operator can still choose to do.

## 10. Deploy note

No schema change — `model_quality` already has every column this feature needs. One new
config key, `routing.quality_refresh_s` (config-linted `> 0`). Two pre-deploy checks,
because two stored values start affecting routing the moment this ships:

1. Nothing in serving read `model_quality` before this slice, so **any `customer` or
   `calibrated` row already present in a deployed database starts affecting routing** —
   check `SELECT model_name, task_class, source, score FROM model_quality;` and call any
   rows out in the release notes.
2. B54: every onboarded model used to score the 0.5 floor. Now any onboarded model with a
   non-null published `quality`, or a non-`cheap` tier, scores that value instead —
   check with

   ```sql
   SELECT slug, tier, quality FROM onboarded_models WHERE tier <> 'cheap' OR quality IS NOT NULL;
   ```

   Every row returned will be scored differently after deploy.

## 11. Lower bounds and quality floors (B80)

The estimator's `ci_low` (the 5th percentile of the Beta posterior at the default
`ci_level` 0.90) is the **calibrated lower bound** on a model's success rate for a
workload. The quality-floor router
([`per-app-config.md`](./per-app-config.md) `quality_floors`) keeps a model only if this
bound clears the workload's floor. The point score (`mean`) still feeds the ranking score
as before; the bound never does.

**Where bounds are stored and read.**

- Apply writes one `model_quality_bounds` row per `(model, workload)` for every
  `recommended` **and** `no_change` unit with a `ci_low`. A `no_change` unit changes no
  score, but a model whose quality the data confirmed still gets a bound. A new table, so
  `create_all` makes it; no `ALTER` is needed for it.
- The resolver keeps bounds in their own map, refreshed with the score overrides (§8). For
  a model and workload it tries `(model, workload)`, then `(model, *)`, from the newest
  applied set, and reports the source `calibrated`.
- **Nothing else supplies a bound.** Published quality, the tier default and `customer`
  rows do not, and there is no write path for customer bounds. A model with no bound
  never clears a floor.
- Revert deletes a set's bound rows together with its score rows.
- `GET /admin/config-snapshot` carries the applied bounds under `quality.bounds`
  (`{model, workload, lower, set_id}`), so the policy simulator can replay a floor.

**Operator steps.**

1. Collect outcomes and run `scripts/quality_recommend.py` as in §1. Check that the report
   shows enough labelled rows per workload; a unit below the estimator's gates is
   `insufficient` and gets no bound.
2. `POST /admin/quality/calibrations?dry_run=true` and read the `bounds` list: it is
   what will be stored.
3. Apply, then `GET /admin/quality/calibrations` and confirm the set shows `bounds > 0`.
4. Set `routing.quality_floors.workloads` in `config/routing.yaml` and start with `mode:
   shadow`. The trace and response then record what the floor would have chosen, without
   acting. Forecast the change first with `scripts/policy_simulate.py`
   ([`policy-simulator.md`](./policy-simulator.md)).
5. Move to `enforce` per app (`quality_floor_mode`) or for the deployment.

**Sets applied before this feature have no bounds.** They stored scores only. To give
them bounds, **revert** the set (`DELETE`) and **re-apply** the same file; the file must
still carry `ci_low` (every file `quality_recommend.py` writes does). Re-applying without
reverting is a `409`. Until then, a workload calibrated only by an old set has no bound
and its floor falls back to the length-tier path with `unmet_no_qualifier`.

**Fitting bounds offline for the benchmark.** `scripts/floor_fit.py` produces a
`routeforge.floor-bounds/v1` file from a FeasibleRouteBench sweep, using this same
estimator with its default gates on the calibration half of a seeded 50/50 split
(stratified by workload). `scripts/feasible_bench.py --floor-bounds` then scores the
`router_floor` arm on the held-out half. See
[`feasible-route-bench.md`](../guides/feasible-route-bench.md) §10. Those bounds are never
applied to a deployment.

**Caveats.** A bound is only as good as its labels: it says the data is consistent with at
least that success rate, for the traffic the labels came from. It does not cover a
workload shift, and a floor does not verify any single answer.
