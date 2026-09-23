# P2b-2: routing outcome records — design

**Date:** 2026-09-23 · **Status:** built 2026-09-23 on `feat/routing-outcome-records`;
**amended 2026-09-23** where the build departed from it (§13, and inline notes marked
*Amended*) · **Programme:** P2b-1 FeasibleRouteBench (shipped) → **P2b-2 outcome records** →
P2b-3 learning/exploration · **Source:** Priorities 1–3 handoff §16, and the Phase 2 bullet
"versioned routing-outcome records"

## 1. Why

The router has no measured quality signal. Every onboarded model scores at its tier-default
quality. So when several models tie on price and tier, which one wins is a tie-break, not a
judgement. This was observed live twice (research doc §10, §11.1): Llama fails every claim
summary and the router cannot know it. B51's fix removed the latency fiction that made
this worse. The durable fix is to **record what actually happened on each request, per
workload**, so that P2b-3 can learn from it and FeasibleRouteBench can report on it.

Most of the substrate exists but is scattered or perishable:

- versions and the eligible/rejected/scored sets are on the trace (`resp["trace"]`, `sp.*`);
- cost and latency are in `usage_records`, which is metering and has no row for
  verify-failed or empty-set requests;
- quality feedback goes **only** to the app's Langfuse project (`/v1/feedback`). RouteForge
  never keeps it, and it is keyed by `trace_id`, which exists only when telemetry exported;
- `request_snapshots` is capped at 500 rows, so it is a console cache, not a dataset.

## 2. Owner decisions (2026-09-23)

| Question | Decision |
|---|---|
| Quality signal source | **Feedback + step 9.** Keep `/v1/feedback` scores locally, and record the step-9 verification outcome on every request. Each is labelled by source. LLM-as-judge is a later slice. |
| Workload label | **Client tag + derived.** An optional validated client label, plus features RouteForge derives itself. |
| Dataset export | **Admin JSONL + bench loader.** An admin-only NDJSON export, and a FeasibleRouteBench loader/report over it. Training stays P2b-3. |
| Storage | **New tables** (`create_all`, no ALTER), not wider `usage_records`, and not derived from snapshots. |

## 3. Scope

**In:**
- the `routing_outcomes` and `outcome_feedback` tables;
- the write path on every routed outcome;
- workload tagging;
- local feedback storage, with request-id linkage;
- retention;
- the admin export;
- the eval loader and report;
- docs.

**Out:**
- **Learning or changing routing from outcomes** (P2b-3). This slice writes and reads
  records; the router does not consume them. Routing is byte-identical.
- **LLM-as-judge.**
- **Pushing datasets to Langfuse.**
- **Propensity logging for off-policy evaluation.** The router is deterministic today, and
  P2b-3's exploration adds propensities.

## 4. Records

### 4.1 `routing_outcomes`: one row per request that reached routing

| Column | Type | Source / notes |
|---|---|---|
| `request_id` | str PK | `resp["id"]` / the route's request id |
| `application_id` | str, indexed | the tenant (no FK cascade surprise: `ondelete=CASCADE` like `usage_records`) |
| `ts` | datetime, indexed | |
| `trace_id` | str, nullable, indexed | `resp["trace_id"]` when telemetry exported. The join key for `/v1/feedback`. |
| `status` | str enum | see below |
| `app_policy_version`, `endpoint_registry_version`, `routing_policy_version` | str | from ctx / certificate / `Settings.routing_policy_version` |
| `workload` | str, nullable | the validated client tag (§5) |
| `workload_source` | str enum | `client`, `none` or `invalid` |
| `tier_target` | str, nullable | surfaced from the router (§6) |
| `complexity` | float, nullable | surfaced from the pipeline (§6) |
| `prompt_tokens_bucket` | str | `<1k`, `1k-4k`, `4k-16k`, `16k-64k` or `>64k`, over post-compression prompt tokens |
| `sensitivity`, `risk_class` | str | from ctx |
| `onprem_only` | bool | |
| `detected_entity_types` | JSON list[str] | entity **types** only |
| `eligible` | JSON list[str] | the first router hop's eligible names, from `scoring.candidates` plus chosen |
| `rejected` | JSON list[{name, code}] | codes only, no reason strings |
| `attempts` | JSON list[str] | models tried, in order, including failed hops |
| `chosen` | str, nullable | the served model; null when nothing was served |
| `scoring` | JSON, nullable | the first router hop's `RankResult.to_record()`, which is ids and numbers only |
| `circuit_bypass` | bool | *Amended:* recorded now (the breaker merged in PR #67): true if any router hop bypassed the breaker |
| `cost_usd`, `latency_ms` | float | the total across hops. *Amended:* `latency_ms` on failure rows is wall-clock from the start of the request handler (includes gate time), not a hop sum |
| `prompt_tokens`, `completion_tokens` | int | |
| `verify_required`, `verify_performed`, `verify_passed` | bool, nullable | from the `verify` trace record |
| `verify_mode` | str enum, nullable | `not_required`, `verified`, `failed`, `waived_stream` or `incremental` |

`status` takes exactly one of these values:

| `status` | HTTP | When |
|---|---|---|
| `ok` | 200 | the request was served (every stream mode included) |
| `backend_failed` | 502 | `BackendUnavailable`: all attempts failed |
| `verify_failed` | 422 | step 9 failed closed |
| `no_eligible` | 422 | an empty feasible set, **the empty-set cases the benchmark cares about** |
| `context_overflow` | 400 | every attempt refused on context length |

The following are **not recorded**, because they never reached routing: 401, rate-limit 429,
budget 429, gate errors, and client cancellation (499). A cancellation is not an outcome of
the routing decision.

### 4.2 `outcome_feedback`: zero or more rows per request

| Column | Type | Notes |
|---|---|---|
| `id` | int PK autoincrement | |
| `request_id` | str, indexed | a logical link to `routing_outcomes`, not an FK, so feedback survives the outcome row's pruning order |
| `application_id` | str, indexed | always the **authenticated caller's** app |
| `ts` | datetime | |
| `score_name` | str enum | `SCORE_NAMES`: `route_correct`, `preferred_model`, `failure_type`, `quality` |
| `value_num` | float, nullable | for numeric scores |
| `value_cat` | str, nullable | for categorical scores; validated `[A-Za-z0-9._:-]{1,64}`, and otherwise the row is refused |
| `source` | str enum | `app` for `/v1/feedback`, `admin` for the console |

**Never stored:** `comment` and `corrected_output`. They are free text that can carry PII,
and they keep going to Langfuse exactly as today. `corrected_output`'s own score row
(value 1) is stored, so "a correction was submitted" is still countable.

### 4.3 The privacy invariant

Both tables hold **ids, enums, numbers, entity types and version hashes only**, never prompt,
completion, tool arguments, reason strings, or upstream error text. Four tests enforce it:

- a pipeline test sends an SSN and an e-mail address, then asserts neither appears in any
  column of either row;
- a schema test asserts that neither table has a column whose name suggests content;
- `rejected` stores `code`, never `reason`, since reasons can embed model or detail text;
- `workload` and `value_cat` are regex-validated slugs, capped at 64 characters.

## 5. Workload tagging

- **Sources,** first match wins:
  1. the `x-routeforge-workload` header;
  2. the OpenAI `metadata.workload` body field (the body model already allows extras);
  3. none.
- **Validation.** Lower-cased, then `^[a-z0-9][a-z0-9._-]{0,63}$`. An invalid value is
  **dropped, never rejected**. A bad tag must not break a client, so the row records
  `workload=null, workload_source="invalid"`.
- **Not forwarded upstream.** `metadata` is still not in `FORWARDED_PARAMS`.
- **Content-free by contract.** The workload tag is documented like `x-routeforge-session`:
  clients must not put personal data in it, and the slug format limits what can go there.

## 6. Write path

- **Builder.** One pure builder, `app/apps/outcomes.py::build_outcome(...)`, maps
  `(resp or failure context, ctx, settings, workload, status)` to a row dict. It lives in
  `app/apps`, because `app/eval` must not be imported by serving (the AST isolation test).
- **Hooks,** in `app/api/routes.py`:
  - `_finalize` writes `ok`, in the same place `record_usage` writes the usage row. It
    covers non-stream, keepalive/verified_then_stream, stream_unverified and
    incremental_verify.
  - `_record_failed` / `_record_failed_stream` write `backend_failed`.
  - The error branches of the non-stream `except` block (and the keepalive error branch)
    gain `verify_failed`, `no_eligible` and `context_overflow` writes. Those have no
    `resp`, so the builder takes whatever the context holds and leaves the rest null.
- **Surfacing the missing fields:**
  - `tier_target` is added to `RoutingDecision` and to the router `DecisionRecord`, as a
    new optional field (default None), so no wire break. The router already computes it.
  - `complexity` is placed on the pipeline's result metadata.
  - `routing_policy_version` comes from `Settings`.
  - An empty-set rejection carries no trace, so its `eligible` is `[]` and `rejected`
    comes from the `NoEligibleCandidate`'s feasible set. The exception gains an optional
    `rejected` tuple for this.
- **Best effort.** The outcome write happens in its own `try` block after the usage
  write. If it fails, the error is logged with the request_id (a `serving.obs` warning).
  **It never fails or changes the response.** It is inline, like the usage row, so it
  **does add one DB commit per request**. Metering, which is billing, stays the durable
  record. *Amended 2026-09-23:* there is no `routeforge_outcome_write_failures_total`
  metric (§13), and the earlier "never delays" was wrong.
- **Switch.** `serving.yaml` gets `outcomes: {enabled: true, retention_days: 90}`. This is
  infrastructure: `serving.yaml` owns observability switches, and the config lint gains
  its single-owner entry. With `enabled: false`, nothing is written and nothing else changes.
- **Retention.** The existing hourly `_observation_loop` (`app/main.py`) also calls
  `prune_outcomes(older_than_days)`, which deletes from both tables by `ts`.

## 7. Feedback linkage

- **`FeedbackIn` accepts `request_id`.** It gains an optional `request_id`, and at least
  one of `trace_id` / `request_id` is required, otherwise the call gets a 422.
- **Resolving the request.** The outcome row is looked up by `request_id`, or by
  `trace_id`, **scoped to the authenticated caller's `application_id`**. An app can never
  score another app's request: a mismatch is treated as not found.
- **Storing the score.** When the row exists, the score is stored locally, and **also**
  posted to Langfuse exactly as today when a `trace_id` is known. The response becomes
  `{"stored": <langfuse result>, "recorded": <local result>}`. It stays HTTP 200 in both
  cases, and `stored` keeps its current meaning.
- **Admin feedback.** Admin feedback (`POST /api/requests/{request_id}/feedback`) records
  with `source="admin"`, including when the snapshot has no `trace_id` (today that returns
  "no trace"). The local record no longer needs telemetry.

## 8. Export

`GET /admin/outcomes/export?since=&until=&application_id=&limit=` requires admin auth
(`require_admin`) and returns `application/x-ndjson`.

- **The response is streamed.** Rows are read in keyset pages ordered by `(ts, request_id)`,
  so a large export never loads into memory.
- **Line format.** Each line has the form
  `{"schema": "routeforge.outcome/v1", "outcome": {...row...}, "feedback": [{score_name, value, source, ts}]}`.
- **Parameters.** `since` and `until` are ISO-8601 and default to the last 7 days. `limit`
  is capped at 100 000.
- **Access boundary.** The data is already content-free, and access is limited to admin
  users.

## 9. Bench loader and report (`app/eval/outcomes.py` + `scripts/outcome_report.py`)

- **Input.** The script reads an export file (not the DB). This keeps `app/eval`
  deployment-agnostic and lets an operator hand a file to someone else.
- **Loading.** It validates `schema` and refuses unknown majors. Records carry every
  version stamp, so the report groups by `endpoint_registry_version` when more than one is
  present and never silently pools across registry changes.
- **Per `(workload, chosen)`,** where no workload is shown as `(untagged)`:
  - n, and served-ok rate;
  - verify pass rate, over requests where verification was performed;
  - mean feedback `quality` with its label count;
  - `route_correct` rate with its label count;
  - mean cost, and latency p50/p95;
  - failover rate (attempts > 1).
- **Per workload:** `no_eligible` rate, `backend_failed` rate, and label coverage (the
  share of requests with any feedback).
- **Output.** `report.md` + `summary.json`. The summary is deterministic: stable ordering
  and no timestamps, as the bench's `metrics.json` is.
- **Stated caveat in the report header.** Production observes only the **served** endpoint
  for each request. There is no counterfactual matrix, so **oracle regret is not
  computable** from outcomes alone, and the report never prints it. Comparing endpoints
  across different request mixes is confounded by the router's own choices. That is
  P2b-3's problem, which is why exploration within the feasible set is a later slice.

## 10. Testing (TDD)

- **Builder:** every status; the fields present or null per status; bucket boundaries.
- **Hooks (HTTP boundary, consumer-contract style):**
  - one row per status;
  - streaming modes write `ok` with the right `verify_mode`;
  - cancelled writes nothing;
  - a failure in the outcome write still returns 200 with metering intact.
- **Privacy:** the SSN/e-mail test and the schema column-name test (§4.3).
- **Workload:** header beats metadata; invalid values are dropped and never rejected; the
  tag is not forwarded upstream.
- **Feedback:**
  - request_id and trace_id resolution;
  - a cross-app attempt is not recorded;
  - comment and corrected_output text are never stored;
  - `stored`/`recorded` semantics;
  - admin feedback works with no trace.
- **Retention:** the prune deletes old rows from both tables.
- **Export:** NDJSON shape, keyset paging across a page boundary, filters, admin-only (401).
- **Loader/report:** a golden summary on a fixture export; mixed registry versions are not
  pooled; the caveat text is present.
- **Identity:** routing decisions are unchanged (the existing suite, plus a check that
  `outcomes.enabled` has no effect on `resp`).

## 11. Docs

- New `docs/reference/routing-outcomes.md`: the schema, statuses, workload tagging,
  feedback linkage, export, retention, and privacy invariant.
- `docs/reference/api-reference.md`: `/v1/feedback`'s `request_id` and `recorded`, the
  workload header, and the export endpoint.
- `docs/guides/feasible-route-bench.md`: the outcome report.
- Research-notes section per the research-documentation rule: the dataset's model,
  invariants, the served-arm-only caveat, and what P2b-3 needs from it.
- Backlog: P2b-2 marked shipped, and the B51 durable-fix pointer updated.

## 12. Deploy

- **Two new tables,** created by `create_all`. **No ALTER** is needed for them.
- **The `DecisionRecord.tier_target` field** lives in JSON (trace and snapshot), not a
  column, so it needs no migration either.
- **Earlier ALTERs are still owed.** Deployments must first apply the earlier outstanding
  ALTERs (`context_window_source` from #64) before rolling any image.

## 13. Amendments (2026-09-23, from the build)

The build (commits `e24eb2c`..`e14f8e5`) departed from this spec in these ways. The
reference doc, `docs/reference/routing-outcomes.md`, describes what was built.

**Decided while planning:**

1. **No write-failure metric.** `routeforge_outcome_write_failures_total` was not added. A
   failed write is a `serving.obs` warning naming the request_id. `/metrics` stays at five
   families; a write failure is an operator log concern, not a routing signal.
2. **`circuit_bypass` is recorded now.** The breaker merged in PR #67 before this slice, so
   the column is true when any router hop's `DecisionRecord.circuit_bypass` was true.

**Changed or settled during the build:**

3. **The write adds one commit per request.** §6 said the write "never delays" the response.
   It is inline in `_finalize`, in the same place as the usage row, so it adds one DB commit
   per request. It still never fails or changes the response.
4. **`verify_mode` checks the reason first.** Precedence: reason `waived*` → `waived_stream`;
   reason `incremental` → `incremental`; `required` false → `not_required`; `passed` false →
   `failed`; else `verified`. Real `stream_unverified` and `incremental_verify` records always
   carry `required: false`, so checking `required` first recorded them all as `not_required`.
5. **`latency_ms` has two definitions.** `ok` rows sum the trace hops (as the usage row does);
   failure rows use wall-clock from the start of the request handler, including gate time.
6. **More requests write no row** than §4.1 listed: pinned-model rejections (`422
   model_not_found`), unhandled `500`s, `400 streaming_not_supported`, body validation
   errors, and a real-token stream that ends with "stream ended without a final result"
   (rare; it writes no usage row either; a known gap).
7. **`no_eligible` rejections.** `rejected` comes from `NoEligibleCandidate.rejected` as
   `{name, code}`. Eligibility-filter rejections carry codes; the router's budget filter has
   `code: null`; an empty pool gives `[]`. The budget and empty-pool cases are
   distinguishable only by `rejected` being non-empty with null codes.
8. **`eligible` is the first hop's scored set** (`scoring.candidates`), falling back to
   `[chosen]`, not "scored plus chosen". `tier_target` is the router's target before any
   budget fallback.
9. **Semantic-cache hits** are `ok` rows with no router hop, so `chosen`, `tier_target`,
   `scoring` and `complexity` are null. The cached answer's model is not recorded.
10. **Workload parsing.** A header that is present but invalid is authoritative: `metadata`
    is not consulted. A `metadata` that is not an object, or a non-string `workload`, is
    `invalid`; nothing supplied is `none`.
11. **Feedback.** `SCORE_NAMES` has five names (`corrected_output` too). `recorded` reflects
    the primary score only, not the extra `corrected_output` row. Every local lookup and
    write is guarded: a local store failure is logged, gives `recorded: false`, and never
    changes the HTTP status or `stored`, or blocks the Langfuse post. Admin feedback still
    needs the request in the console's snapshot cache (404 otherwise).
12. **Retention.** Feedback is pruned by its own `ts`, so late feedback can outlive its
    outcome row. The prune is skipped when `outcomes.enabled` is false. The config lint
    requires `retention_days > 0` (`OUTCOMES_RETENTION_NOT_POSITIVE`).
13. **Export.** A naive `since`/`until` is taken as UTC. `limit` below 1 is `422`. Pages are
    1000 rows, each in its own session. Feedback `value` is `value_num`, else `value_cat`.
14. **Report.** Records are always grouped by `endpoint_registry_version`, not only when
    more than one is present; a null version is `(unknown)`, a null chosen is `(none)`.
    Mean cost, like the latency percentiles, is over `ok` rows only, because failed rows
    carry zero cost. A missing `schema` field is refused like an unknown major.

**Final-review fixes (2026-09-23):**

15. **`verify_failed` rows are attributed.** `Pipeline.run` stashes a copy of the trace with
    every free-text `reason` removed (`outcome_trace`), plus the served attempt's tokens and
    cost, in `internals` before raising `PipelineRejected` for a step-9 failure. The route
    builds the row from it through the same reader as `ok` rows (`trace_features`):
    `chosen`, `attempts`, `scoring`, `eligible`, `tier_target`, `rejected`, the `verify_*`
    fields (`verify_mode: failed`), cost and tokens.
16. **Hop semantics.** `eligible`, `scoring`, `tier_target` and `rejected` describe the
    request's **first routing decision**: the first trace entry that is a router hop or a
    failed gateway hop carrying `scoring`. `chosen` is the served model. Amends item 8
    ("the first hop's scored set"), which after a failover pointed at the served hop.
17. **Request ids are 128-bit.** `req-` plus the full UUID4 hex (36 characters), since the
    id is the outcomes primary key kept for 90 days. Feedback joins on
    `(application_id, request_id)` (`feedback_for(..., application_id=)`).
18. **`value_cat`** allows `/` and up to 100 characters (`String(100)`), so a
    `preferred_model` score can name a slashed model slug. A non-finite numeric value is
    refused.
19. **`cache_hit` column.** True on a semantic-cache hit (`chosen` stays null). The report
    gives these rows their own `(cache)` chosen bucket. Amends items 9 and 14.
20. **`context_overflow` rows record attempts**: the gateway's `ContextWindowExceeded`
    carries `attempted_models` like `BackendUnavailable`.
21. **Report:** `failover_rate` is renamed `multi_attempt_rate` (more than one attempt is
    a failover or an escalation; the record does not tell them apart).
