# P3c-3 — uncertainty ranges for the policy simulator

**Date:** 2026-10-06 · **Status:** approved design; amended 2026-10-06 by the final review
(§16 lists every amendment)
**Priority:** 3, slice P3c-3 (`docs/roadmap/priorities-1-3-status.md` §2.1; handoff §36
"uncertainty ranges", open question 11 "How will uncertainty intervals be communicated to
administrators?").
**Follows:** P3a simulator (`app/eval/simulate.py`), P3c-1 shadow prices (`app/eval/shadow.py`),
P3c-2 scenarios (`app/eval/scenarios.py`), B60 replayable rejections, P2b-3 quality calibration
(`app/eval/calibration.py`).
**Paper:** feeds `docs/research/arxiv.md` §4.2 and experiments E9a–E9d.
**Scope:** offline only — `app/eval/`, `scripts/policy_simulate.py`, `tests/eval/`, docs. No
serving code changes, no database change, no new dependency.

## 1. The problem

Every figure the simulator prints is a point estimate from one replay of one export. The
2026-10-06 DGX lab run reported `token_scale: 0.6` → cost −39.0% on 24 rows
(`docs/research/feasible-route-bench.md` §19.4.1). The same line would be printed for 2
million rows. A reader cannot tell the two apart, and cannot tell whether a quality delta of
−0.03 is larger than the noise in the quality labels it was computed from.

Two things are missing:

- a range beside each figure that narrows as the export grows;
- a statement of what that range covers, so a narrow range is not read as an accurate replay.

## 2. Goals and non-goals

**Goals.**

1. `--uncertainty` adds a range to every headline figure of all three simulator modes
   (`--proposal`, `--scenarios`, `--shadow-prices`).
2. Without the flag, every output is byte-identical to what it is today.
3. With the flag, output is deterministic for a given (inputs, replicates, level, seed, minimum
   rows, posterior files), and those values are part of the document's id.
4. A delta's range is the range of the paired difference between the two arms over the same
   resampled rows.
5. Where a quality score has a stated posterior, the quality range includes it. Where it has
   none, no uncertainty is invented and the report says how many rows that is.
6. Too few rows is a first-class answer: "insufficient data (n = k)" instead of a range.
7. Every report that shows a range says what the range does not include.

**Non-goals.**

- **Replay model error.** The range does not measure how wrong the replay is. §8.
- **A day-block bootstrap.** Decided against for this slice; §4.5 gives the reasons.
- **Per-workload ranges.** A workload is not a stratum, so a workload bucket can be empty in a
  replicate, and its cost needs another per-row pass per workload. `by_workload` keeps its
  point figures and gets no range.
- **Ranges on `overflow_hops`, `moves`, `new_rejections.by_code`, `by_core` and the baseline
  agreement block.** Counts of rare, clustered events and a fidelity check, not headline
  figures. Not asked for; not built.
- **Ranges on non-binding shadow-price relaxations.** They are counted, not listed, today; that
  does not change.
- **Ranking.** A range is not a way to order scenarios or relaxations. §10.2.
- **The capacity model** (P3c-4), further replay fidelity, any console surface, any serving-side
  change.

## 3. Flags and inputs

All flags are on `scripts/policy_simulate.py` and apply to every mode.

| Flag | Default | Valid | Meaning |
|---|---|---|---|
| `--uncertainty` | off | — | compute and write ranges |
| `--bootstrap N` | `1000` | integer, `1 ≤ N ≤ 20000` | bootstrap replicates |
| `--interval P` | `0.90` | `0 < P < 1` (NaN refused) | level of the percentile interval |
| `--seed S` | `0` | integer `≥ 0` | seed |
| `--min-rows K` | `30` | integer `≥ 1` | a bucket with fewer rows gets no range (§7.1) |
| `--quality-posteriors FILE` | none | repeatable | a P2b-3 `recommendation.json` that was applied (§6) |

Rules:

- `--bootstrap`, `--interval`, `--seed`, `--min-rows` or `--quality-posteriors` without
  `--uncertainty` is exit 2, naming the flags (`--seed, --min-rows given without
  --uncertainty`). A tuning flag that silently does nothing is a way to believe a run was
  seeded when it was not.
- A value out of range, or a posterior file that is missing, is not JSON, has the wrong
  schema, fails its own content hash or has a malformed `recommended` unit, is exit 2 naming
  the file and the location. Nothing is written.
- A posterior file whose `set_id` is not a calibration set in the snapshot is loaded and
  ignored, with a warning on stderr. It is probably the wrong file; the report's coverage line
  (§6.4) will also say so.
- Exit codes do not change: `0`, `2`, `3`, `4`.
- `--min-agreement` still runs first. A refused run starts no bootstrap. In `--scenarios`
  mode the gate normally runs after `compare` returns (it needs the shared baseline), which
  with `--uncertainty` would be after every scenario's bootstrap; so with both flags the CLI
  first calls the existing `baseline_check` (one replay of the snapshot) and gates on that.

**`inputs`.** With the flag, each document's `inputs` gains one key:

```json
"uncertainty": {"replicates": 1000, "interval": 0.9, "seed": 0, "min_rows": 30,
                "quality_posteriors_sha256": []}
```

`simulation_id` and `comparison_id` are hashes of `inputs`, so two runs that differ in any of
these values have different ids, and a flagged run never shares an id with an unflagged one.
Without the flag the key is **absent** (not null), so `inputs` and the id are exactly what
they are today.

## 4. What is resampled

### 4.1 Rows, once

Replay is a deterministic function of a row and a scenario. Each row's two `ArmResult`s are
computed once, exactly as today. A bootstrap replicate draws row positions with replacement
and recomputes the figures over the drawn rows. Nothing is simulated a second time.

### 4.2 Stratified by application

Rows are grouped by `application_id`, and the groups become **strata**:

- An application with at least `--min-rows` rows is **its own stratum**. In each replicate it
  is resampled to exactly its own row count, from its own rows. That one draw feeds both the
  application's bucket and the totals.
- All rows of the applications with fewer than `--min-rows` rows form **one pooled stratum**:
  concatenated in (`application_id` ascending, then the order `select_rows` returns), and
  resampled together to the pooled row count. The pool feeds the **totals only**. Those
  applications are not drawn individually; each already has no range of its own (§7.1).

The totals of a replicate are the union of the strata's resamples, so every replicate has
exactly the recorded number of rows.

**Why small applications are pooled** (amended by the final review). A bootstrap of a
stratum of `n_s` rows reproduces `(n_s − 1)/n_s` of that stratum's unbiased variance, and a
one-row stratum is a constant. Drawn one application at a time, an export of 240 rows in 240
one-row applications has a totals range of width zero, and 80 applications of 3 rows give
`√(2/3) ≈ 0.82` of the width the same rows give as one application. Pooling the small
applications removes most of that. What is left is disclosed, not corrected: every block
carries

```json
"totals_strata": {"own": 2, "pooled_apps": 4, "pooled_rows": 45, "smallest": 30,
                  "variance_factor": 0.975}
```

- `own`: applications that are their own stratum; `pooled_apps`, `pooled_rows`: what the
  pool holds (both `0` when nothing is pooled); `smallest`: the smallest stratum's row count
  (the pool counts as one stratum).
- `variance_factor = (n − S)/n` for `n` rows in `S` strata: the share of the totals'
  variance that resampling within strata keeps **if every stratum has the same variance**.
  It is analytic, not measured, and the ranges are **not** corrected for it. The report
  prints the numbers and those words.
- With `--min-rows 1` nothing is pooled. A 240 × 1 export then has `variance_factor: 0.0`
  and totals ranges of width zero; the disclosure is what stops that reading as precision.
- When the totals themselves are below `--min-rows` nothing is drawn and `totals_strata` is
  `null`.

Pooling treats the small applications' rows as exchangeable with each other. When those
applications differ in mean, the pooled range is wider than a fixed-mix reading would give.
And no rule here covers variation **between** applications: if the applications are
themselves a sample of users or tenants, the range is much too narrow (§8, research §20.7).

Why stratify at all:

- **A bucket that has a range never empties.** An unstratified resample of 300 rows leaves a
  30-row app with a different row count in every replicate, and a one-row app out of about a
  third of them ((1 − 1/300)^300 ≈ 0.37).
- **An application is the unit policy is written for.** Proposals, shadow-price relaxations
  and budgets are per app. The per-app figures are the ones acted on.
- **It is what pairs a shadow price with the main run.** A relaxation re-simulates one app's
  rows; drawing that app the same way in both places needs the app's draw to depend on the app
  alone (§4.3).

What it assumes, and what it holds fixed:

- **Rows are exchangeable within a stratum over the export window** — within an application
  that is its own stratum, and across the pooled applications. That is false when
  traffic shifts inside the window — a release on day 3, a batch job at night, a client that
  retries in bursts. The range then describes resampling a mixture and understates how
  different a new window would be.
- **Rows are independent.** Every call of one agent loop is its own row, and the calls of a
  loop share a context. Correlated rows carry less information than their count suggests, so
  the range is too narrow by an amount this design does not estimate.
- **The mix of traffic between applications is fixed** at the recorded counts for every
  application that is its own stratum; within the pool it varies from replicate to replicate.
  Variation in how much each large app sends is not in the range.
- **The export window's length is fixed.** Per-day figures divide by it; it is not resampled.

### 4.3 Paired by construction

The row positions replicate `b` draws for application `a` with `n` rows are

```
random.Random(f"{seed}:{b}:app:{a}").choices(range(n), k=n)
```

a function of `(seed, b, a, n)` and nothing else. Positions index the app's rows in the order
`select_rows` returns them (`ts`, then `request_id`). The pooled stratum of `n_pool` rows
(§4.2) draws

```
random.Random(f"{seed}:{b}:pool").choices(range(n_pool), k=n_pool)
```

a function of `(seed, b, n_pool)` alone, indexing the pooled rows in the order §4.2 gives.
Consequences:

- **Both arms.** A `RowResult` carries both arms, so a drawn row contributes its `current` and
  its `proposed` result together. A delta in a replicate is a difference over the same rows.
  A proposal that changes nothing has a delta of exactly `0` in every replicate, so its range
  is exactly `[0, 0]` — not a small range around zero, which two independent draws would give.
- **Every scenario.** Every scenario of a comparison is replayed over the same rows in the
  same order, so every scenario sees the same draws. A scenario's block does not depend on
  which other scenarios are in the file, or on their order.
- **Every relaxation.** A shadow-price relaxation scoped to one app bootstraps that app's rows
  with the same draws the main run used for that app. The range of "relax `allowed_tiers` for
  app X" equals app X's bucket under the proposal "set X's `allowed_tiers` to null". The one
  endpoint-scoped relaxation re-simulates every row, so it is resampled in the export's own
  strata, pool included, and its range equals the **totals** of the proposal that makes the
  same edit.
- **An app alone and inside the whole export.** An application that is its own stratum has
  the same bucket whether its rows are bootstrapped alone or with every other application's.
  (A pooled application has no bucket to compare.)

No shared state is needed for this. Each scenario and each relaxation is bootstrapped where
its rows already are, one at a time, and regenerates the draws from the seed. That costs the
draws again per scenario and keeps memory flat (§11).

Seeding a `random.Random` with a string hashes it with SHA-512, so the stream does not depend
on the platform or on `PYTHONHASHSEED`. The global `random` state is neither read nor moved.

### 4.4 The interval

A percentile interval. For `N` replicate values sorted ascending and level `P`, with
`k = floor((1 − P)/2 · N)`: `lo` is the `(k+1)`-th smallest and `hi` the `(k+1)`-th largest.
At the defaults (`N = 1000`, `P = 0.90`) those are the 51st smallest and the 51st largest.
One replicate gives `lo = hi`.

It is not bias-corrected (no BCa), and it is not studentized. For sums, means and shares over
many rows the three agree closely. For percentiles of a discrete distribution they do not, and
the percentile interval is the one that needs no further assumption. §7.3 says what happens
when the point is not inside it.

### 4.5 A day-block bootstrap: not in this slice

A block bootstrap that resamples whole (application, UTC day) blocks would address the
within-day dependence §4.2 names. It is left out, for three reasons:

1. **Too few blocks.** The export this was designed against is 24 rows recorded in one pass
   on the DGX lab (research §19.4.1); an operator's first exports will span hours to a few
   days. A percentile interval from fewer than roughly ten blocks is badly anti-conservative,
   so the option would mostly print "insufficient data" or, worse, a confident-looking range
   from three days.
2. **Replicates stop having a fixed row count.** Days differ in size, so every share's
   denominator and the insufficient-data rule would vary by replicate, and a per-app bucket
   could not be compared with its point.
3. **`ts` is optional on a row.** Rows without it cannot be placed in a block.

It is recorded as a follow-up (§15, item 2). The caveat (§8) names traffic shift as not
covered.

## 5. Which figures get a range

### 5.1 Buckets

Two kinds of bucket get ranges: **totals** (all simulated rows) and **each application**. Each
is a tree of leaves (§9.1) mirroring `simulation._bucket_stats`:

| Figure | Leaves |
|---|---|
| `cost_usd` (all routed rows of each arm) | `current`, `proposed`, `delta` |
| `cost_usd_both_routed` (rows routed in both arms) | `current`, `proposed`, `delta` |
| `route_changed`, `newly_rejected`, `newly_served` | one leaf each (a count) |
| `route_changed_share`, `newly_rejected_share`, `newly_served_share` | one leaf each: the count ÷ the bucket's rows |
| `latency_ms.p50`, `latency_ms.p95` | `current`, `proposed`, `delta` |
| `quality_mean` | `current`, `proposed`, `delta` |
| `onprem_share` | `current`, `proposed` |

The three shares are new figures: today's documents carry only the counts. Because a
stratified replicate has a fixed row count, a share's range is its count's range divided by
`n`; it is written out because it is the form that compares across exports.

**Per day** (totals only, and only when the export window is at least one day, the existing
rule): `per_day.cost_usd.{current, proposed, delta}` — the figure `simulation.json` already
carries — and `per_day.cost_usd_both_routed.delta`, the comparison table's "Δcost/day" cell.

### 5.2 Latency: exact per replicate, and what it means

Replay gives a routed row the latency of the endpoint it picked: the proposal's asserted
value, else the observed median for that endpoint, else the snapshot signal, else the tier
default. So a row's latency is a property of its endpoint, and there are only as many distinct
latencies as endpoints. The nearest-rank p50 and p95 are computed **exactly** in every
replicate, from the replicate's count per distinct latency. No approximation is needed.

The consequence has to be stated: a latency range reflects only which endpoints the resampled
rows used. It contains no per-request latency variation, because the replay has none. Where
every routed row of an arm picks one endpoint, the range is a single value. The caveat says so
(§8).

### 5.3 Shadow prices

Each **binding** relaxation gets five leaves, over the rows it re-simulated:

| Leaf | Is the entry's |
|---|---|
| `route_changed` | `route_changed` |
| `rejections_avoided` | `rejections_avoided` |
| `newly_rejected` | `newly_rejected` |
| `cost_usd_both_routed_delta` | `cost_usd_both_routed.delta` |
| `cost_saved_per_day` | `cost_saved_per_day`; `null` when the window is under a day |

A relaxation with fewer than `--min-rows` rows in scope gets `insufficient_data`. A security
requirement's ranges keep the `price of a protection — not a recommendation` label wherever
they are printed.

### 5.4 The comparison table

Each scenario's row gets a range for `cost_per_day_delta_usd`, `cost_delta_usd`,
`route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`,
`latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current` and
`onprem_share_proposed`. They are the scenario's own totals leaves under the table's column
keys. `overflow_hops` and `affected_apps` have none.

## 6. Quality-label uncertainty

### 6.1 What the snapshot carries

`GET /admin/config-snapshot` exports each calibrated quality as
`{"model", "workload", "score", "set_id"}` (`app/apps/snapshot.py::_quality_entries`). The
score is the posterior mean. The Beta parameters are **not** in the snapshot, and not in the
database either: `model_quality` has no column for them (`app/apps/models.py::ModelQuality`).

The parameters do exist, offline. P2b-3's `recommendation.json` carries `alpha`, `beta`,
`mean` and `status` for every unit, and its `set_id` is the first 16 hex characters of the
SHA-256 of the file's own canonical content — the value the admin API recomputed and checked
before it wrote the set (`docs/reference/quality-calibration.md` §5, §6).

### 6.2 The decision

The operator passes the recommendation file with `--quality-posteriors`. No serving change.

- The file is accepted only if `set_id` equals `calibration.recommendation_set_id` of its
  content. A file that passes, and whose `set_id` appears in the snapshot, is the set the
  snapshot's score came from: the same hash that authorised the write.
- Only units with `status: "recommended"` are read. The others were never applied.
- A unit's posterior is used for a routed arm only when all of these hold: the arm's
  `quality_source` is `calibrated`; the arm's set id (new field `ArmResult.quality_set_id`,
  from `QualityScore.set_id`) is the file's; the unit is `(model, workload)` for the row's
  workload, else `(model, *)` — the resolver's own order
  (`app/routing/quality.py::QualityResolver.resolve`); and the unit's `mean` equals the arm's
  quality to within `1e-6`. The last condition means a file can never attach to a score it did
  not produce (a hand-edited snapshot, a later set for the same model).

What making this automatic would need, for the record: `model_quality` would have to store
`alpha` and `beta` (two nullable columns, an `ALTER` on deployed databases), the apply path
would have to write them, and the snapshot would have to export them under a schema bump. That
is a serving-side change for a convenience; it is deferred (§15, item 3).

### 6.3 The draw

In replicate `b`, each calibrated unit in use gets one quality value:

```
random.Random(f"{seed}:{b}:quality:{set_id}:{model}:{workload or '*'}").betavariate(alpha, beta)
```

- **One draw per unit per replicate**, used by every row that resolves to that unit. The
  unknown is the unit's quality, not each row's.
- **The same draw in both arms and every scenario.** A row that stays on the same model
  contributes exactly `0` to the quality delta in every replicate. A no-op proposal has a
  quality delta range of `[0, 0]` although each arm's quality has a wide range.
- **Its own random stream.** Loading a posterior file changes quality ranges and no other
  figure.

The point estimate is unchanged: it uses the score, which is the posterior mean.

A published or tier-default quality has no stated uncertainty. It stays a point in every
replicate. Its contribution to the quality range is row sampling only — which model the
resampled rows used.

### 6.4 Coverage is reported

`uncertainty.quality_posterior` gives, per arm, `routed` (routed rows that carry a quality),
`calibrated` (of those, rows whose quality is a calibrated score) and `posterior` (of those,
rows with a loaded posterior). The report prints:

> Quality uncertainty covers 40 of 111 routed rows in the current arm and 0 of 111 in the
> proposed arm; the rest use a point prior.

and, when `calibrated > posterior`, that some calibrated rows have no posterior loaded and
which flag supplies it.

## 7. Insufficient data, zero counts, undefined figures

### 7.1 Insufficient data

A bucket with fewer than `--min-rows` rows (default 30) gets no range:

```json
{"insufficient_data": true, "n": 6}
```

and the report prints `insufficient data (n = 6)` in place of a table. This applies to an
application's bucket, to the totals, to a scenario (its totals), and to a shadow-price
relaxation (its rows in scope). The threshold is inclusive: 30 rows get a range at the
default.

An application below the threshold is still resampled, with the other small applications:
its rows are part of the totals through the pooled stratum (§4.2). When the totals themselves
are below the threshold nothing is drawn at all.

Thirty is a convention, not a derivation. It is the usual floor below which a percentile
bootstrap of a mean is not trusted, and it is a flag because the right number depends on how
skewed the app's costs are.

### 7.2 A zero count

If no row in a bucket is newly rejected, no resample of those rows contains one. The range is
exactly `[0, 0]`, for 24 rows and for 2 million. Printed alone that reads as "no risk", which
it is not.

For a share whose count is `0` in all `n` rows, the leaf also carries

```
zero_bound = 1 − (1 − P)^(1/n)
```

the largest per-row rate at which `n` independent rows would still show no such row with
probability `1 − P`. At the default level it is about `2.3/n`; at `P = 0.95` it is the "rule
of three", `3/n`. It is 9.2% for 24 rows and 0.00012% for 2 million.

**Why it is valid for this sampling scheme.** With stratum sizes `n_a` and per-app rates
`p_a`, the chance of seeing no event is `∏(1 − p_a)^(n_a)`. `ln(1 − x)` is concave, so by
Jensen's inequality with weights `n_a/n` that chance is at most `(1 − p̄)^n`, where
`p̄ = Σ (n_a/n) p_a` is the rate weighted by the recorded mix. So if `p̄` exceeds
`1 − (1 − P)^(1/n)`, the chance of an all-zero sample is below `1 − P`: the bound holds for
the totals of a stratified sample exactly as it does for one app. It is an analytic statement
beside the bootstrap, not a product of it.

**What it assumes.** Independent rows from the same traffic — the assumption §4.2 already
makes, and for the same reason too optimistic when rows are correlated. The report says both:

> newly_rejected is 0 in all 80 rows. That is not zero risk: at the 90% level these rows are
> consistent with a newly-rejected rate of up to 2.84% (about 1 request in 35), if the rows
> are independent draws from the same traffic. Correlated rows, such as the calls of one agent
> loop, make the true bound larger.

`zero_bound` is attached to all three shares when their count is zero; the sentence is printed
for `newly_rejected` only, the count a reader would otherwise take as a safety claim.

### 7.3 A point outside its own range

A percentile interval does not promise to contain the point estimate. It can fail for a
skewed statistic (the mean of a very skewed Beta lies above its 95th percentile), for a
percentile of a discrete distribution, and trivially with one replicate.

When it happens the leaf carries `"point_outside": true`, the block counts such leaves in
`points_outside_interval`, and the report marks the row and says how many there are. **The
range is not widened to include the point.** A widened range would no longer be the interval
§4.4 defines.

### 7.4 A figure undefined in some replicate

An arm with very few routed rows can have none in a replicate, and then it has no latency, no
quality and no on-prem share there. An interval over the remaining replicates would be
conditional on the figure existing. So a figure that is undefined in **any** replicate has
`lo` and `hi` null and `"undefined_replicates": k`; the report prints
`n/a (undefined in k resamples)`.

## 8. What the range is, and is not

The range is **sampling variability of the recorded rows under this replay model**: how much
the figure would move if the same traffic had happened to produce a different sample of the
same size per stratum (§4.2). It says nothing about a different set of applications.

It does not include:

| id in `not_covered` | What |
|---|---|
| `replay_model_error` | Replay predicts the first routing decision with frozen signals and a predicted context-window overflow. It does not replay failover, escalation, breaker state, spend or RPM admission (`docs/reference/policy-simulator.md` §6). |
| `admission_estimate_error` | A row with no backend token count — rejected, failed before a response, or served by a backend that reported no usage — is sized and priced from RouteForge's own token count (`simulate.replay_tokens`). On the 2026-10-06 DGX lab run that count was 1.54× the backend's (research §19.4.1). Every replicate inherits the same error. |
| `unrecorded_inputs` | Anything the decision read that the row does not record. B55 and B60 were both of this kind. |
| `traffic_shift` | A change in the traffic after, or inside, the export window. |
| `per_request_latency` | Replay gives each endpoint one latency (§5.2). |

A narrow range on a biased replay is still biased. The baseline agreement block is the check
on bias; the range is not.

**The caveat.** Every report that shows a range prints this, verbatim, as a blockquote:

> **Ranges show sampling variability only.** Each range is a percentile interval from
> resampling the recorded rows with replacement, within each application (applications with
> fewer than `--min-rows` rows are resampled together for the totals), under this replay
> model. It does not include replay model error (first-decision replay, frozen signals,
> predicted overflow), the error of the admission estimate for rows with no backend token
> count, inputs that were not recorded, or future traffic shift. Replay gives each endpoint
> one latency, so a latency range reflects only which endpoints the resampled rows used. A
> narrow range on a biased replay is still biased.

It is the constant `app/eval/uncertainty.py::UNCERTAINTY_CAVEAT`, pinned word for word by a
test (U9).

## 9. Output shape

**The rule.** `--uncertainty` adds keys and changes no existing key or value. The additions
are exactly: `uncertainty` (top level), `inputs.uncertainty`, and — with shadow prices —
`shadow_prices.uncertainty`. The schema strings do not change: a v1 reader that ignores
unknown keys reads a flagged document as before.

### 9.1 The leaf

Wherever a figure `x` exists, its leaf sits at the same path under the bucket:

```json
{"point": -0.02895, "lo": -0.034024, "hi": -0.023268}
```

- `point` is the figure the document already carries, value for value (computed by the same
  code, `_bucket_stats`, and rounded the same way).
- `lo`, `hi`: §4.4. Floats are rounded to 6 decimal places; counts are integers.
- Optional keys, present only when they apply: `point_outside: true` (§7.3),
  `undefined_replicates: k` with `lo`/`hi` null (§7.4), `zero_bound: u` (§7.2).

A bucket is `{"n": <rows>, <the figure tree of §5.1>}` or `{"insufficient_data": true, "n":
<rows>}`. With the block's `replicates`, `seed` and `interval`, `n` is what is needed to
reproduce a leaf.

### 9.2 `simulation.json` (`--proposal`)

```json
"uncertainty": {
  "method": "stratified_paired_row_bootstrap",
  "strata": "application_id",
  "replicates": 1000, "interval": 0.9, "seed": 0, "min_rows": 30,
  "quality_posterior_sets": [],
  "not_covered": ["replay_model_error", "admission_estimate_error", "unrecorded_inputs",
                  "traffic_shift", "per_request_latency"],
  "totals": {
    "n": 111,
    "cost_usd": {"current": {"point": 0.069573, "lo": 0.061, "hi": 0.0779},
                 "proposed": {"point": 0.040623, "lo": 0.0361, "hi": 0.0452},
                 "delta": {"point": -0.02895, "lo": -0.034024, "hi": -0.023268}},
    "cost_usd_both_routed": {"current": {}, "proposed": {}, "delta": {}},
    "route_changed": {"point": 30, "lo": 25, "hi": 34},
    "route_changed_share": {"point": 0.27027, "lo": 0.225225, "hi": 0.306306},
    "newly_rejected": {"point": 0, "lo": 0, "hi": 0},
    "newly_rejected_share": {"point": 0.0, "lo": 0.0, "hi": 0.0, "zero_bound": 0.02053},
    "newly_served": {}, "newly_served_share": {},
    "latency_ms": {"p50": {"current": {}, "proposed": {}, "delta": {}},
                   "p95": {"current": {}, "proposed": {}, "delta": {}}},
    "quality_mean": {"current": {}, "proposed": {}, "delta": {}},
    "onprem_share": {"current": {}, "proposed": {}}
  },
  "by_app": {
    "big": {"n": 60, "cost_usd": {}},
    "few": {"insufficient_data": true, "n": 6}
  },
  "per_day": {"cost_usd": {"current": {}, "proposed": {}, "delta": {}},
              "cost_usd_both_routed": {"delta": {}}},
  "quality_posterior": {"current": {"routed": 111, "calibrated": 0, "posterior": 0},
                        "proposed": {"routed": 111, "calibrated": 0, "posterior": 0}},
  "points_outside_interval": 0,
  "totals_strata": {"own": 2, "pooled_apps": 1, "pooled_rows": 6, "smallest": 6,
                    "variance_factor": 0.972973}
}
```

(`{}` stands for a leaf; the numbers shown are illustrative of the shape.) `per_day` is `null`
when the window is under a day. `by_app` is keyed like the document's own `by_app`.
`totals_strata` is §4.2's disclosure, `null` when nothing was drawn.

With `--shadow-prices` and no `--proposal` there are no totals: the block holds only its head
(`method` … `not_covered`).

### 9.3 `shadow_prices.uncertainty`

```json
"uncertainty": {
  "interval": 0.9,
  "constraints": [
    {"requirement": "allowed_tiers", "scope": {"app": "big"}, "security": false, "n": 60,
     "route_changed": {"point": 20, "lo": 14, "hi": 25},
     "rejections_avoided": {"point": 0, "lo": 0, "hi": 0},
     "newly_rejected": {"point": 0, "lo": 0, "hi": 0},
     "cost_usd_both_routed_delta": {"point": -0.001143, "lo": -0.001433, "hi": -0.000842},
     "cost_saved_per_day": {"point": 0.00056, "lo": 0.000396, "hi": 0.000707}},
    {"requirement": "allowed_tiers", "scope": {"app": "few"}, "security": false, "n": 6,
     "insufficient_data": true}
  ]
}
```

`constraints` is parallel to `shadow_prices.constraints`: same length, same order, and each
entry repeats `requirement`, `scope` and `security` so it can be read alone. The order is the
block's existing one; the ranges do not enter it.

### 9.4 `comparison.json` (`--scenarios`)

```json
"uncertainty": {
  "method": "stratified_paired_row_bootstrap", "strata": "application_id",
  "replicates": 1000, "interval": 0.9, "seed": 0, "min_rows": 30,
  "quality_posterior_sets": [], "not_covered": ["..."],
  "scenarios": [
    {"name": "drop-mid",
     "totals": {}, "by_app": {}, "per_day": {}, "quality_posterior": {},
     "points_outside_interval": 0,
     "table": {"cost_per_day_delta_usd": {"point": -0.014184, "lo": -0.01667, "hi": -0.0114},
               "cost_delta_usd": {}, "route_changed": {}, "newly_rejected": {},
               "newly_served": {}, "latency_p50_delta_ms": {}, "latency_p95_delta_ms": {},
               "quality_mean_delta": {}, "onprem_share_current": {},
               "onprem_share_proposed": {}}}
  ]
}
```

`scenarios` is in file order, like `table` and the document's own `scenarios`. Each entry is
the §9.2 block for that scenario's rows plus `table`, whose keys are the comparison table's
column keys; `table.<key>.point` is `table[i].<key>`. With too few rows `table` is
`{"insufficient_data": true, "n": k}`.

One detail the code forces: the table's "Δcost/day" cell is computed from the like-for-like
delta **after** it has been rounded to 6 decimal places (`scenarios._table_row`). The leaf for
that cell uses the same arithmetic in every replicate, so its point is that cell exactly.

## 10. Reports

### 10.1 Single proposal

`report.md` is today's report, byte for byte, followed by a new section, followed by the
shadow-prices section when there is one:

```
## Uncertainty ranges

> <the caveat, §8>

90% percentile intervals from 1000 bootstrap resamples of the simulated rows, drawn with
replacement within each application (applications with fewer than 30 rows are resampled
together for the totals) (seed 0). Both arms, and every scenario or relaxation, use the same
resampled rows. A bucket with fewer than 30 rows gets no range.

Totals strata: 2 application(s) resampled on their own and 1 with fewer than 30 rows
resampled together (6 rows); the smallest stratum has 6 rows. Resampling within strata leaves
the totals about 0.972973 of their variance (analytic, equal variance assumed, not measured);
ranges are not corrected for it.

Quality uncertainty covers … (§6.4)

### Totals (n = 111)

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 0.069573 | 0.061 to 0.0779 |
…

### Per day
…
### By app
#### big (n = 60)
…
#### few (n = 6)

insufficient data (n = 6)
```

A table has one row per leaf of §5.1 (23 rows). A range cell is `lo to hi`, or
`n/a (undefined in k resamples)`, with ` (point outside range)` appended when §7.3 applies.
Under a bucket whose `newly_rejected` is 0, the zero-count sentence (§7.2).

### 10.2 Scenario comparison

- In the comparison table, each of the eight figures of §5.4 that the table prints as one
  number is followed by its range: `-0.02895 [-0.034024, -0.023268]`. The on-prem share cell
  (`current→proposed`) is left as it is; its two ranges are in each scenario's section.
- After the table and its two existing notes: an "Uncertainty ranges" section with the
  caveat, the method sentence, this sentence, verbatim

  > In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking:
  > overlapping ranges do not make two scenarios equal, and separate ranges do not make one of
  > them the one to choose.

  and one line per scenario, in file order, with its quality coverage — or
  `insufficient data (n = k)`.
- In each scenario's own section, after its metrics table: `### Ranges`, the totals table of
  §10.1, and a line saying per-app ranges are in `comparison.json`, naming any app with
  insufficient data.

With too few rows, no cell has brackets and every scenario's line and `### Ranges` say
`insufficient data (n = k)`.

RouteForge still does not rank or recommend: the table stays in file order, nothing is sorted
by a range, and no output key says which scenario is better.

### 10.3 Shadow prices

The existing two tables are unchanged. Before the non-binding footer, a `### Ranges` table: one
row per binding relaxation, in the block's order, with `requirement`, `scope`, `rows`, the
cost cell (`cost saved/day`, else the like-for-like delta) and the three counts, each as
`point [lo, hi]`, and a `label` column that carries `price of a protection — not a
recommendation` on every security row. A relaxation with too few rows shows `insufficient data
(n = k)`. The section points at "Uncertainty ranges" for the caveat, which is always printed
above it.

## 11. Performance

`numpy` is not a dependency of the offline tooling (`pyproject.toml`), and none is added. The
engine is pure Python, `O(replicates × rows)` with small constants:

- **Classes, not rows, for everything but cost.** A row's contribution to every figure except
  cost is determined by its two arms' `(routed, on-prem, latency, quality, posterior)` and
  whether the route changed. Rows share a small number of these classes (bounded by
  apps × workloads × endpoints²). A replicate counts drawn rows per class
  (`Counter(map(cls.__getitem__, idx))`) and computes counts, shares, both percentiles and
  both quality means from the class counts.
- **Cost is the one per-row value.** Four columns per app (each arm; each arm restricted to
  rows routed in both), summed over the drawn positions with `sum(map(col.__getitem__, idx))`.
  Equal columns are stored once: an app a proposal does not touch has one distinct column.
- **Nothing is held across scenarios.** §4.3.

**Measured** (synthetic rows built in memory, three apps, one untouched; Python 3.12.3, one
core of an Intel Core Ultra 7 165U):

| rows | replicates | seconds |
|---|---|---|
| 100,000 | 1000 | 25.2 (27.5 on a second run) |
| 1,000,000 | 50 | 36.0 |

So about 25 ms per replicate at 100,000 rows and about 0.7 s per replicate at 1,000,000 (the
second figure includes the one-off setup, and the per-row cost is higher at that size). A
1,000,000-row export at the default 1000 replicates is therefore about 12 minutes per
proposal, per scenario, or per relaxation of an app that large. That is feasible for an
offline tool and is the reason `--bootstrap` exists; the reference will say so.

## 12. Invariants (each pinned by a named test)

| # | Invariant |
|---|---|
| U1 | Without `--uncertainty`, every output file of every mode is byte-identical to what the tree produced before this slice. With it, the document is that document plus the three keys of §9, and no existing value moves. |
| U2 | With the flag, the same inputs and plan give byte-identical files. The global `random` state is neither read nor moved. |
| U3 | Every leaf has `lo ≤ point ≤ hi`, or `point_outside: true`, or null `lo`/`hi` with `undefined_replicates`. The range is never widened. |
| U4 | Paired. A no-op proposal has every delta, count and share range exactly `[0, 0]`, with pooled applications present too. An app that is its own stratum is drawn the same way alone and inside the whole export; a scenario gets the same block alone and among others; an app-scoped relaxation gets the range of the equivalent proposal's app bucket and an endpoint-scoped one the range of its totals; one calibrated unit in both arms cancels in the quality delta. |
| U5 | Ranges narrow as the rows are replicated k-fold: a sum's relative width and a share's or mean's width strictly decrease from 1× to 4× to 16×. |
| U6 | Every replicate draws exactly `n` positions for every app with its own stratum (`n ≥ --min-rows`) and exactly `n_pool` for the pooled stratum; an app with its own stratum is present in every replicate, a one-row app included when `--min-rows` is 1. Every replicate value equals `_bucket_stats` over the rows the two seed formulas draw. |
| U7 | A bucket, scenario or relaxation below `--min-rows` carries `insufficient_data` and `n`, no range; the reports print `insufficient data (n = k)`. |
| U8 | Quality-label uncertainty exists only where a posterior does: one calibrated unit serving every row reproduces the Beta interval; without the file, or for a tier-default quality, the quality range has no label width; a file never attaches to a score it did not produce. |
| U9 | The caveat is the spec's text, and every report that shows a range prints it; `not_covered` lists the five ids. |
| U10 | `replicates`, `interval`, `seed`, `min_rows` and the posterior files' hashes are in `inputs`, and ids differ whenever any of them does. |
| U11 | No output key contains a ranking word; scenarios stay in file order and are not sorted by a range; a security relaxation's ranges keep the protection label. |
| U12 | No serving module imports `app.eval`. |
| U13 | The engine's figures are the document's: drawing every row once reproduces `_bucket_stats`, and every leaf's `point` is the figure at the same path. |

## 13. Testing

Tests are in `tests/eval/`. The engine is tested on hand-built `RowResult`s (fast; every
figure controllable) and against real replays, so its figure definitions cannot drift from
`_bucket_stats`. The CLI is tested in-process over committed input files.

U1 is pinned by golden files: the four input files and the eight output files of the four
ways to run the CLI (`--proposal`, `--proposal --shadow-prices`, `--shadow-prices`,
`--scenarios`), written from the tree **before** any product code of this slice exists, and
compared byte for byte ever after.

Input classes that are likely to bite and that an obvious test would miss:

1. **Thin and lopsided data.** A one-row app; an app just under and exactly at `--min-rows`;
   fewer than `--min-rows` rows in all; no rows; an arm with one routed row.
2. **Degenerate figures.** A zero count; a figure with no variance (one endpoint, one
   quality); a count that is the whole bucket; one replicate.
3. **Pairing across scopes.** The same app alone and in the whole export; a relaxation against
   the proposal that makes the same edit; one scenario alone and in a file of three, in both
   orders.
4. **Posterior files.** Edited after writing; for a set the snapshot does not hold; for a
   score the snapshot was hand-edited away from; with a workload-scoped and a model-wide unit
   in one set; given twice; not JSON; a unit that was not recommended.
5. **Flags.** Each tuning flag alone without `--uncertainty`; each bound and one step past it;
   NaN.
6. **The gate.** `--min-agreement` failing with `--uncertainty` in every mode: exit 4, nothing
   written, no bootstrap started.

## 14. Docs

- `docs/reference/policy-simulator.md`: a new §10 "Uncertainty ranges (P3c-3)" — what it
  answers, the flags, the method, the output shape for each mode, quality posteriors,
  insufficient data and zero counts, the caveat verbatim, the cost; plus the new flags in §3,
  the new keys in §4.1, the new section in §4.2, and "no uncertainty ranges" removed from the
  not-simulated list in favour of a pointer.
- `docs/research/feasible-route-bench.md`: a new §20 — the model, the pairing argument, the
  zero-count bound, invariants → tests, results. The only numbers are the synthetic timing of
  §11 and synthetic checks from the test suite, labelled synthetic; every measurement on real
  records is **TO MEASURE**.
- `docs/research/arxiv.md`: §4.2 filled in — claim, method, what the interval does and does
  not cover, findings from the build, experiments E9a–E9d for the DGX lab — and the
  experiments checklist and threats to validity updated.
- `docs/roadmap/priorities-1-3-status.md`: the slice in Built (PR `pending`), §2.1 renumbered
  with P3c-4 next, the day-block bootstrap and the automatic posterior export listed as
  follow-ups.

## 15. Where this design departs from the brief

Each item follows the code as it is and keeps the brief's intent.

1. **Quality posteriors come from a file, not from the snapshot.** The brief said to check
   what the snapshot carries and, if the parameters are not there, either scope a serving
   change or defer. The snapshot carries the mean and the `set_id` only (§6.1). A third option
   needs neither: the P2b-3 recommendation file has the parameters and is self-authenticating
   through the same content hash the admin API checked. So quality-label uncertainty is in
   this slice, with no serving change, behind `--quality-posteriors`. The automatic export is
   deferred.
2. **No `--block day`.** The brief asked for it only if cheap and testable. §4.5.
3. **Automatic posterior export is deferred**, with what it would need written down (§6.2).
4. **`--min-rows` applies to a relaxation's rows in scope and to a scenario's totals**, not
   only to per-app buckets. The brief said "a bucket"; these are the buckets those modes have.
5. **Shares are new figures.** The brief asked for the three counts "as counts and as shares".
   Today's documents have no share, so the shares exist only inside `uncertainty`, each with
   its own `point`.
6. **Every leaf carries `point`.** The brief asked for `lo`, `hi` and enough to reproduce. A
   copy of the point makes the block readable alone and makes U3 checkable from the block; the
   row count is on the bucket (`n`), not on every leaf.
7. **A zero count gets a stated bound, for all three counts.** The brief allowed a
   rule-of-three statement only if it could be justified for this sampling scheme. §7.2 gives
   the derivation and its assumption; the bound uses the run's own level `P`, so at the
   default it is `2.3/n`, not `3/n`.
8. **A point outside its range is flagged, not prevented.** The brief asked for "within its
   interval, or the exception explained and tested". §7.3; U3.
9. **A figure undefined in any replicate has no range.** Not in the brief. §7.4.
10. **Latency is exact per replicate**, and the caveat gains a sentence the brief did not
    list, because the code makes latency a per-endpoint constant (§5.2). `not_covered` has a
    fifth id for it.
11. **Shadow ranges live at `shadow_prices.uncertainty`**, not under the top-level
    `uncertainty`. The shadow block is built and rendered on its own (`render_shadow_section`
    takes the block alone), so its ranges travel with it. U1 names all three added keys.
12. **`--scenarios` with `--min-agreement` and `--uncertainty` replays the snapshot once more**
    to gate before the bootstraps (§3). Not in the brief; it keeps "the gate runs first" true.
13. **The comparison table's on-prem share cell shows no range.** It is two numbers in one
    cell. Both ranges are in the JSON and in each scenario's section.
14. **`ArmResult` gains `quality_set_id`.** An offline dataclass, defaulted, serialised
    nowhere. It is how an arm's calibrated score is tied to its set.
15. **Additional invariants** beyond the brief's list: U8, U9, U13, and the gate test.

## 16. Amendments from the final review (2026-10-06)

The whole-branch review found the points below. Each changed the code and this document
together; the sections above describe the design as amended.

1. **Small applications are pooled for the totals draw** (§4.2, §4.3, §7.1, §8, U4, U6). The
   first build drew every application on its own. A stratum of `n_s` rows contributes
   `(n_s − 1)/n_s` of its variance and a one-row stratum none, so the totals range was too
   narrow on an export with many small applications: on a synthetic 240-row export the mean
   width against normal theory was 0.82 for 80 × 3 rows, 0.70 for 120 × 2 and 0 for 240 × 1
   (research §20.6.1 has the measurement and its method). Applications below `--min-rows` are
   now one pooled stratum for the totals, the block discloses the strata and the analytic
   `variance_factor`, and the caveat and the method sentence say so.
