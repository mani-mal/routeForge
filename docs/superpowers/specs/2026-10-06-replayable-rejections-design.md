# Replayable rejections — record the routing token count; compare outcomes, not only models

**Date:** 2026-10-06 · **Status:** approved design, not built
**Fixes:** B60 (`docs/backlog/bugs.md`), found by the first live P3c-2 run on the DGX lab
(`docs/research/feasible-route-bench.md` §18.4.1).
**Priority:** 3, replay fidelity. The first piece of the replay-fidelity slice
(`docs/research/arxiv.md` §4.4); it comes before P3c-3 because every later simulator slice
rests on the baseline it repairs.
**Follows:** P2b-2 outcome records (`app/apps/outcomes.py`), B55 (pin and output cap on the
record), P3a simulator (`app/eval/simulate.py`), P3c-1 shadow prices, P3c-2 scenarios.
**Paper:** feeds `docs/research/arxiv.md` §4.4 and experiment E7b.

## 1. The problem

A request that RouteForge rejects — no eligible model, every attempt over its context window,
every attempt failed upstream — still writes an outcome row. That row records
`prompt_tokens: 0`, because `prompt_tokens` is the backend's billed count and no backend
answered (`app/apps/outcomes.py::build_failure_outcome`).

The policy simulator sizes and prices a row from `prompt_tokens`. A zero-token prompt fits any
per-request budget and any context window, so the simulator's `current` arm **routes** a
request the record says was rejected. Two things follow, both observed live:

- A scenario that raises a per-request budget reports **0 newly served**. On the DGX lab it
  would have served 7 requests.
- Baseline agreement stays **1.0**, because `_baseline` compares the chosen model over `ok`
  rows only. Nothing counts a recorded rejection that replay routes.

The second point is the more serious one: the fidelity check that exists to catch a wrong
replay did not catch this one.

## 2. Goals and non-goals

**Goals.**

1. Every outcome row records the token count the router was given, served or not.
2. Replay routes each row with that count, so a rejection that depended on request size is
   reproduced.
3. A row that has no usable size is excluded and counted, never replayed as an empty prompt.
4. The baseline check compares outcomes (routed or rejected) as well as models, reports both,
   and `--min-agreement` gates on both.

**Non-goals — each a later slice, named here so nobody reads them into this one.**

- General failover and confidence-escalation replay. Replay still simulates the first routing
  decision plus the context-window re-selection P3c-2 added.
- Circuit-breaker state. Both arms still run with no circuit open.
- Daily-spend and RPM admission. Those requests get a 429 and write no outcome row.
- Re-running PII detection. Recorded entity types are replayed as they are.
- Any console (UI) change.
- The onboarding-form fix (B61).
- Recording the pre-compression token count, cached prompt tokens, or the router's cost
  estimate. Only the one number the router reads is added.
- Changing `prompt_tokens`. It stays the backend's billed count, zero on a row no backend
  answered.

## 3. Serving change: `routing_tokens`

### 3.1 What is recorded

One new nullable integer column on `routing_outcomes`:

| Column | Type | Meaning |
|---|---|---|
| `routing_tokens` | int, nullable | The token count the router was given for this request: `tokens_after`, RouteForge's own count of the model-facing messages after the gate and after step-5 compression. It is the `tokens_after` argument of `Gateway.run` / `Gateway.run_stream`, the same number `complexity` is derived from (`min(1, tokens_after / 2000)`). Written on every status. Null when the request never reached compression. |

It is **not** the backend's count. `prompt_tokens` is the backend's tokenisation of what it was
sent, and is what was billed. `routing_tokens` is RouteForge's own estimate
(`app/gateway/backends/base.py::count_tokens`), made before any backend is called, and is what
the per-request budget check and the cost term of the score were computed from. The two differ
on almost every request. Both are kept because they answer different questions: what the
router decided on, and what was billed.

### 3.2 Where it is written

`Pipeline.run` and `Pipeline.run_stream` already publish the content-free outcome context
(`internals["outcome_ctx"]`) and add `complexity` to it right after compression. They add
`routing_tokens` on the next line, from the same `comp.tokens_after`:

```python
complexity = min(1.0, comp.tokens_after / 2000)
if internals is not None:
    internals["outcome_ctx"]["complexity"] = complexity
    internals["outcome_ctx"]["routing_tokens"] = comp.tokens_after
```

`app/apps/outcomes.py::_policy_features` copies `outcome_ctx.get("routing_tokens")` into the
row. Both `build_ok_outcome` and `build_failure_outcome` use `_policy_features`, so every
status gets the column from one place and no route handler changes.

### 3.3 When it is null

| Case | Why |
|---|---|
| A semantic-cache hit (`ok`, `cache_hit: true`) | The hit returns before compression. No router ran. `complexity` is null on the same row for the same reason. |
| A row written before the column existed | Nothing to backfill from. |
| A failure whose outcome context was never published | Defensive only. See below. |

Today every failure status is raised at or after the gateway call, which is after
compression: `NoEligibleCandidate` comes from `Router.select`, `ContextWindowExceeded` and
`BackendUnavailable` from the gateway, `verify_failed` from step 9. So a row written by this
build with any of the five statuses carries `routing_tokens`, except a cache hit. Requests
rejected earlier write no outcome row at all (a policy-gate error is a 503, a pinned model the
app may not use is a `model_not_found` 422; `docs/reference/routing-outcomes.md` §3.1). The
null case is kept because the column must be nullable for old rows anyway, and because replay
must behave correctly if a later change adds an earlier rejection.

### 3.4 Content-free argument

`routing_outcomes` is content-free by contract: ids, enum values, numbers, entity types and
version hashes only (`docs/reference/routing-outcomes.md` §2). The new column is an integer.

- **It carries no text.** It is computed as a length (`count_tokens` is `len(text) // 4`,
  floored at 1), summed over the model-facing messages, the replayed tool-call arguments and
  the tool schemas. No substring, hash or position of the request survives in it.
- **It is the same class of value already recorded.** `prompt_tokens`, `completion_tokens` and
  `prompt_tokens_bucket` are token counts and are the schema test's named exemptions.
  `complexity` is already `routing_tokens / 2000` for every request under 2000 tokens, on every
  status including rejected rows. So the only information that is new is the exact count for
  requests of 2000 tokens or more, and the count (rather than the capped complexity) on
  rejected rows.
- **It is measured after the gate.** It counts the model-facing view: the redacted text under
  `model_input: redact`, the raw text under `raw`. Either way it is a count, and rule 1
  (nothing external before the gate) is not involved: nothing new is sent anywhere, and the
  value does not exist until the gate and compression have run.
- **No new egress.** The value goes where the row already goes: the `routing_outcomes` table
  and the admin-only NDJSON export. It is not added to the response, the trace, Langfuse or
  `/metrics`.
- **Residual: a count is a side channel on length**, exactly as `prompt_tokens` is. An admin
  who can read the export can already see the size of every served request. This change
  extends that to rejected requests. That is the purpose of the change, and it is stated in
  the reference doc rather than hidden.

Existing tests that must keep passing unchanged in meaning:
`tests/test_outcome_store.py::test_schema_is_content_free`,
`tests/test_outcome_hooks.py::test_outcome_row_carries_no_request_content`,
`::test_no_eligible_row_carries_no_request_content`,
`::test_verify_failed_row_carries_no_request_content`,
`tests/test_outcome_builder.py::test_build_ok_outcome_is_content_free`,
`tests/test_outcome_ctx_pipeline.py::test_outcome_ctx_has_no_prompt_text_even_with_ssn`.
The schema test gains one assertion: `routing_tokens` exists, is an `Integer` and is nullable.

### 3.5 The wire is unchanged

This is a serving-path change (`app/pipeline.py`), so the consumer contract applies
(`.claude/skills/consumer-contract-tests`). Nothing is added to a success body, an error
envelope or an SSE frame. A contract test drives the full `/v1/chat/completions` stack with a
realistic payload and asserts: the success body validates against the `openai` SDK's
`ChatCompletion` model; a budget rejection is still the B49 envelope with exactly its existing
keys; the string `routing_tokens` appears in neither; and the messages that reached the
backend are the ones the client sent.

## 4. Deployed databases

`Database.ensure_ready` calls `create_all`, which creates missing tables and never adds a
column to a table that exists. A database that already has `routing_outcomes` needs the
column added by hand.

**PostgreSQL**

```sql
ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS routing_tokens INTEGER;
```

**SQLite** (no `IF NOT EXISTS` for `ADD COLUMN`; running it twice fails with
`duplicate column name`, which is harmless)

```sql
ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER;
```

**This is a hard pre-deploy step, as it was for B55 and for `conflicting_core`.** Run it before
rolling the image. The decision follows the B55 precedent exactly
(`docs/reference/routing-outcomes.md` §1):

- The application **does boot** against a database that lacks the column. Nothing at startup
  reads `routing_outcomes`.
- **Serving continues.** The ORM names every mapped column in each `INSERT`, so every outcome
  write fails; `_record_outcome` logs `outcome record failed request=<id>` and swallows it.
  Responses, the usage row and metering are unaffected.
- **No outcome rows are recorded** until the ALTER runs, and reads of the table fail. The
  admin export returns an empty stream: the error is raised after the `200` header has been
  sent, so it is in the server log and not in the status code. The `/v1/feedback` outcome
  lookup errors.

The code is deliberately **not** made to tolerate a missing column (no reflection, no
conditional insert). Reasons: B55 and P3b set the precedent and operators already follow it;
tolerating it would mean silently dropping the one field this slice exists to record; and a
second code path that runs only on an unmigrated database is a path nobody tests in
production. One test pins the boot, serve and no-row claims so they stay true. It uses a
SQLite file and three real boots: the table is created, the column is dropped, and the
application is started again against it. It boots, a request returns 200 and no row is
written. After the documented ALTER, the next boot's request writes a row with
`routing_tokens` set. The empty export was observed by hand in the same setup and is not
pinned by a test. The PostgreSQL form is not executed by any test; it is the same statement
form as the B55 and P3b upgrades.

Rows written before the ALTER keep the column null.

## 5. Export and loader

`GET /admin/outcomes/export` dumps every `routing_outcomes` column
(`app/api/admin_routes.py::_export_outcome_dict`), so each line gains `"routing_tokens"`
with no code change. The schema tag stays `routeforge.outcome/v1`: the change is additive.

`app/eval/outcomes.py::load_export` copies the `outcome` object as it is, so an export
without the key (taken before this change, or from an older deployment) still loads. Replay
treats an absent key and a null value the same way: "not recorded" (§6.1, §6.3).

## 6. Replay

### 6.1 Two token counts, used for two things

Today `simulate_row` uses one number, `row["prompt_tokens"]`, for routing, for the
context-window check and for pricing. It now uses two.

| Quantity | Used for | Value |
|---|---|---|
| **routing tokens** | `Router.select(tokens_after=…)`: the per-request budget check and the cost term of the score | `routing_tokens` when the row has it, else `prompt_tokens` (as before) |
| **prompt tokens** | pricing the routed call (`price_call`) and the context-window check (`prompt + output > context_window`) | `prompt_tokens` when it is greater than zero (the backend's billed count), else the routing tokens |

Both are multiplied by `workload.token_scale` with the existing rule
(`max(1, round(tokens × scale))`, zero stays zero, scale 1 is the identity). `complexity` is
replayed from the recorded `complexity` exactly as before.

Why the context-window check stays on the backend's count for a served row: live, overflow is
the backend's refusal on its own tokenisation, so the backend's count is the better predictor,
and keeping it means P3c-2's overflow behaviour for served rows does not move. For a row no
backend answered there is no such count, and the routing tokens are the best available.

### 6.2 Rows with no backend usage are priced at the admission estimate

A row is **estimated** when it has `routing_tokens` and `prompt_tokens` is zero: a request
that was rejected, or routed and then failed before any response. No backend billed it, so
replay has no billed prompt count and no completion count.

For an estimated row, when an arm routes it:

- prompt tokens are the routing tokens (§6.1);
- completion tokens are the router's own output estimate for the row: the recorded
  `max_tokens`, else `budgets.default_estimated_output_tokens`
  (`expected_output_tokens`, already computed in `simulate_row`).

That is the same pair of numbers the live per-request budget check priced the request at. So a
request that was rejected at an estimated $0.053 against a $0.05 cap, and that a proposal
rescues, is costed at $0.053 in the proposed arm. Pricing it at its prompt alone would
understate what rescuing it costs; the completion it would have produced is unknown, and the
client's own cap is the figure the operator's budget is written against.

**It is an estimate and is labelled as one.** `rows.estimated_tokens` in the output counts the
simulated rows sized this way, and both reports print the count with one sentence saying what
it means.

Where the estimate lands in the cost figures:

- A rejected row that a proposal rescues is `newly_served`. Its estimate is in the proposed
  side of `cost_usd`. It is outside `cost_usd_both_routed`, which covers only rows routed in
  both arms, so the like-for-like cost delta is not moved by it.
- A `backend_failed` row is routed in both arms (its first decision was a route). Its
  replayed cost is what the call would have cost had it succeeded, on both sides, so it
  cancels in every delta unless the proposal moves its route. Replay does not model the
  failure; that is the failover non-goal.

### 6.3 Rows with no usable size are excluded — a behaviour change

A row is excluded under a new reason, **`missing_routing_tokens`**, when all of these hold:

- its `status` is not `ok`;
- it has no `routing_tokens` (absent or null);
- its `prompt_tokens` is zero.

That is exactly a pre-fix `no_eligible`, `context_overflow` or `backend_failed` row (and a
`verify_failed` row whose trace stash failed): a row with no record of how large the request
was. A `verify_failed` row with its served attempt's token counts is kept and replayed as
before. An `ok` row is never excluded by this rule.

`EXCLUSION_REASONS` becomes

```python
("before_since", "cache_hit", "unknown_status", "unknown_app", "missing_features",
 "missing_routing_tokens")
```

`missing_routing_tokens` is **last**: a row is first cut by `--since`, then by the checks that
say it is not a simulatable row at all, then by malformed fields, and only a well-formed row
of a known app can be "well-formed but unsized". A `routing_tokens` value that is present but
mistyped (a string, a bool, a float, a negative number) is `missing_features`, like any other
mistyped field.

> **Behaviour change for existing exports.** Before this change those rows were simulated, as
> empty prompts. After it they are excluded. For an export that contains pre-fix rejected or
> failed rows, `rows.simulated` falls, `rows.excluded.missing_routing_tokens` rises by the same
> number, and totals, by-app figures and shadow prices move. The old figures for those rows
> were wrong (they were the B60 defect); the new output does not contain them. Outputs from
> before and after this change are not comparable row for row on such an export, and
> `simulation_id` (a hash of the inputs) does not distinguish them. To simulate rejected
> requests, export rows recorded after the upgrade; `--since` cuts at the first of them.

### 6.4 What the simulator still gets wrong about rejected rows

- **A recorded `context_overflow` is reproduced only when the endpoint declares a window.**
  Replay predicts overflow from the declared `context_window`; an undeclared window never
  overflows. Such a row replays as routed and is counted as an outcome disagreement (§7).
  That is correct reporting of a real gap, not a defect of this slice.
- **`routing_tokens` is RouteForge's count, not the backend's.** For a rejected row the
  context-window check uses it, so a request the backend refused by a small margin can replay
  as fitting.
- **Drift.** A request rejected because an endpoint was disabled or unhealthy at the time
  replays against the snapshot's endpoint state.

## 7. Outcome-level agreement

### 7.1 Recorded outcome class

Each simulated status maps to the outcome its **first routing decision** had:

| `status` | Recorded class | Why |
|---|---|---|
| `ok` | routed | served |
| `verify_failed` | routed | a model was chosen and answered; step 9 refused the answer |
| `backend_failed` | routed | a model was chosen; every attempt failed upstream |
| `no_eligible` | rejected | `Router.select` found no candidate at the first decision |
| `context_overflow` | rejected | every attempt was refused for size; replay models this as a `context_window` rejection (P3c-2) |

The table is checked against `app/apps/outcomes.py::STATUSES` by a test, so a sixth status
cannot be added without deciding its class.

`no_eligible` is always a first-decision rejection: when the router finds nothing on a later
hop, the gateway reports `BackendUnavailable`/`ContextWindowExceeded` (failures already
happened) or keeps the answer already served (a skipped escalation).

### 7.2 The measure

Over **every simulated row**, the `current` arm's outcome (`routed` or `rejected`) is compared
with the recorded class. `_baseline` gains:

| Field | Meaning |
|---|---|
| `outcome_rows_compared` | number of simulated rows |
| `outcome_agreement` | share whose `current` outcome equals the recorded class; `null` when there is no row |
| `outcome_warning` | `true` when `outcome_agreement` is below 0.90 |
| `recorded_rejected` | rows whose recorded class is rejected |
| `recorded_rejected_but_routed` | of those, rows the `current` arm routes — the B60 signature |
| `recorded_routed_but_rejected` | rows recorded routed that the `current` arm rejects |
| `outcome_by_app` | per app: `rows`, `agreement`, `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected` |

The existing fields (`rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`,
`overflow_contradicted`) keep their names and meanings. `agreement` is still the model
agreement: the first pick against the recorded first attempt, over `ok` rows.

### 7.3 Reports

`report.md` (single proposal) and the comparison report's "Baseline agreement" banner both
show outcome agreement next to model agreement, with the rate and the number of rows.

- The single-proposal report states both rates on one line, on every run and not only when
  one is low. A report that says nothing about agreement when nothing looks wrong is how B60
  went unnoticed. A rate below 90% adds a warning.
- The comparison banner adds an "Outcome agreement" paragraph under the model one, with its
  own warning below 90% and its own per-app list, as model agreement has.

Independently of the 90% threshold, **any non-zero `recorded_rejected_but_routed` or
`recorded_routed_but_rejected` prints a note** giving the count out of its denominator
("3 of 7 recorded rejections replay as routed"). The reason is dilution: rejected requests
are usually a small share of traffic, so every one of them can replay wrong while
`outcome_agreement` stays above 0.90. The count out of the rejected rows is the number that
says whether a "newly served" figure can be trusted.

### 7.4 `--min-agreement` gates on the lower of the two

`agreement_ok(baseline, minimum)` is true only when **both** `agreement` and
`outcome_agreement` are computable and the **minimum** of the two is at least `minimum`. Exit
code stays 4, and nothing is written.

- **Why the minimum and not a pooled rate.** The two measures catch different defects. Model
  agreement caught B55 (replay picked a different model). Outcome agreement catches B60
  (replay served a rejected request). A replay is fit to answer "what would change" only if
  it reproduces both; a high score on one says nothing about the other. A pooled or averaged
  rate would let the larger population hide the smaller.
- **Why both must be computable.** With no served row, nothing vouches for which model replay
  picks. That case already exits 4 and still does. Outcome agreement is computable whenever
  any row is simulated, so it is never the missing one on its own.
- **Known limit.** Outcome agreement is taken over all simulated rows, so it is diluted in the
  way §7.3 describes. `--min-agreement 1.0` refuses any disagreement; for a lower threshold
  the note in §7.3 is the safeguard. A per-class threshold is not added in this slice.

The message printed on exit 4 names both rates and both counts.

## 8. What else moves

- **Shadow prices (P3c-1)** go through `select_rows` and `simulate_row`, so they gain the fix
  with no change of their own: relaxing `max_cost_usd_per_request` can now show
  `rejections_avoided`, and pre-fix unsized rows leave the baseline.
- **`--shadow-prices` without `--proposal`** gates through `baseline_check`, which returns the
  same `_baseline` block, so the new gate applies there too.
- **The comparison (`--scenarios`)** lifts `baseline` and `rows` from the shared current arm;
  both carry the new fields.
- **`saturated_complexity_estimated`** is unchanged. With `routing_tokens` on the row a
  saturated row's scaled complexity could be computed rather than estimated; that refinement
  is left out to keep this slice to the defect.

## 9. Invariants (each pinned by a named test)

- **R1 content-free.** `routing_outcomes` still holds no column whose name suggests text, and
  `routing_tokens` is a nullable integer. A rejected request that contains a marker string, an
  SSN and an e-mail address writes a row that contains none of them.
- **R2 served rows are unchanged when the two counts coincide.** For a row with
  `prompt_tokens > 0`, adding `routing_tokens == prompt_tokens` to the row yields an
  `ArmResult` equal, field for field, to the one produced without the key — in both arms and
  under any `token_scale`. ("Unchanged" is this and only this: when the counts differ, the
  route may legitimately differ, because the router is now given the number it was given
  live. Pricing and the context-window check still use `prompt_tokens`.)
- **R3 a budget rejection is reproduced.** A request rejected live for
  `max_cost_usd_per_request`, exported and replayed under the snapshot taken from the same
  deployment, is `rejected` in the `current` arm.
- **R4 raising the cap serves it.** The same row under a proposal that raises the app's cap
  above the estimate is `routed` in the `proposed` arm and counted `newly_served`; its cost is
  the admission estimate.
- **R5 outcome agreement is perfect on a live round trip with rejections.** Served and
  rejected requests sent to a real app, exported with its snapshot, replay with
  `agreement == 1.0`, `outcome_agreement == 1.0` and both disagreement counts zero, and pass
  `--min-agreement 1.0`.
- **R6 unsized rows are never routed.** A non-`ok` row with no `routing_tokens` and zero
  `prompt_tokens` is excluded under `missing_routing_tokens`, counted, and reaches no arm.
- **R7 isolation.** No serving module imports `app.eval`
  (`tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval`).
- **R8 the wire is unchanged.** Success bodies validate against the `openai` SDK model; error
  envelopes keep exactly their keys; `routing_tokens` appears in no response.
- **R9 old exports still load.** An export line without `routing_tokens` loads, and its served
  rows replay exactly as before this change.
- **R10 the recorded value is the router's input.** On every status, `routing_tokens` equals
  the `tokens_after` the router was called with; on a semantic-cache hit it is null.
- **R11 every status has a class.** The recorded-class table covers exactly
  `app/apps/outcomes.py::STATUSES`.
- **R12 the gate takes the minimum.** An export whose model agreement is 1.0 and whose outcome
  agreement is below the threshold exits 4 and writes nothing, and the reverse.
- **R13 an unmigrated table does not break serving, and the documented ALTER repairs it.**

## 10. Testing

The live round trip is the acceptance test for B60. It has to reject requests *because of
their size*: a long prompt, a small `max_tokens`, and a cap between the estimate with the
prompt and the estimate without it. A request made over budget by a large `max_tokens` alone
is over budget with a zero-token prompt too, so a test built on it passes before the fix.

TDD per task. Serving: `tests/test_outcome_builder.py`, `tests/test_outcome_ctx_pipeline.py`,
`tests/test_outcome_store.py`, a new `tests/test_outcome_routing_tokens.py` (HTTP boundary:
one test per status, the unmigrated-table test), `tests/test_openai_compat.py` (the contract
test). Replay: a new `tests/eval/test_replay_rejections.py` (R2, R3, R4, R6, R9, R11, R12),
`tests/eval/test_simulate.py` and `tests/eval/test_proposal_workload.py` (expectations that
legitimately change), `tests/eval/test_scenarios_compare.py` (banner),
`tests/eval/test_policy_simulate_cli.py` (the gate, and the live round trip extended with
rejections: R3, R4, R5 and R6 end to end).

The live round trip follows the existing pattern
(`test_live_round_trip_baseline_is_perfect`): a real `create_app()` under `TestClient`, real
requests, `GET /admin/config-snapshot` and `GET /admin/outcomes/export`, then the CLI as a
subprocess.

## 11. Docs

- `docs/reference/routing-outcomes.md`: the column (§3.2), the upgrade note with both ALTERs
  (§1), the content-free table (§2), the export example (§7).
- `docs/reference/policy-simulator.md`: §1.1 (the third per-row field), §3
  (`--min-agreement`, exit 4), §4.1 (`rows`, `baseline`), §4.2 (the report order), §5
  (outcome agreement and the gate), §6 (remove the B60 limit; state what remains), §9.4 and
  §9.5 (the comparison document and its banner).
- `docs/backlog/bugs.md`: B60 resolved, with the evidence.
- `docs/research/feasible-route-bench.md`: new §19 — the model, invariants → tests, results
  **TO MEASURE** (the DGX lab re-run of the 2026-10-06 scenarios is the experiment). No number
  is written before it is measured.
- `docs/research/arxiv.md`: §4.4 and the experiments table — the claim, the invariants, and
  the lesson: *every input the admission and routing decision read must be on the record for
  rejected requests too; agreement must compare outcomes, not only the chosen model.*
- `docs/roadmap/priorities-1-3-status.md`: the slice moved to Built; the remaining fidelity
  gaps restated.

## 12. Where this design departs from the brief

The brief fixed seven decisions. The code agrees with most of them as written. These are the
places where it does not, or where the brief was silent and a choice had to be made. The plan
lists the same items under "Deviations from spec".

1. **The exclusion covers every unsized row, not only rejected ones (§6.3).** The brief said
   rows "recorded as rejected without `routing_tokens`". `build_failure_outcome` writes
   `prompt_tokens: 0` on every failure row built without a trace, so a pre-fix
   `backend_failed` row has the same empty-prompt defect. The rule is stated on the data: not
   `ok`, no `routing_tokens`, zero `prompt_tokens`.
2. **A gate rejection writes no outcome row (§3.3).** The brief's example of a null column
   was "the gate itself rejects". A policy-gate error is a 503 and a pinned-model rejection a
   422, and neither writes a row. Every status that does write a row is raised after
   compression. So a row written by this build is null only on a semantic-cache hit; the
   counted exclusion is reached by rows from before the column existed.
3. **The context-window check stays on the backend's count for a served row (§6.1).** The
   brief assigned routing to `routing_tokens` and pricing to `prompt_tokens` and did not
   assign the window check. Live overflow is the backend's own refusal, so the window check
   goes with the backend's count, and P3c-2's overflow results for served rows do not move.
4. **A row no backend answered is priced with an output estimate too (§6.2).** The brief
   gave it a prompt count. It has no completion count either, and pricing it at its prompt
   alone would understate the cost of rescuing it. It is priced at the pair the live budget
   check used.
5. **`context_overflow` is classed rejected although its first decision was a route
   (§7.1).** The status vocabulary in the brief matches `app/apps/outcomes.py::STATUSES`
   exactly. Live, a `context_overflow` request was routed and then refused by the backend on
   every attempt. Replay models that as a `context_window` rejection, so the classes agree
   where it matters: on what the arm reports.
6. **The gate needs both rates to be computable (§7.4).** The brief said "the minimum of the
   two". With no served row, model agreement is not computable; that case already exits 4
   and is pinned by an existing test, so it still does.
7. **Additions the brief did not ask for.** `recorded_rejected` as the denominator, and a
   note printed for any non-zero disagreement count, because a pooled outcome rate dilutes
   rejections (§7.3). `rows.estimated_tokens` (§6.2). A line stating both agreements on every
   single-proposal report, not only when one is low. Invariants R9–R13 (§9). A test for the
   unmigrated-table claims, which B55 made without one (§4).
8. **`saturated_complexity_estimated` is left as it is (§8).**
