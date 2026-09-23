# Routing outcome records (P2b-2)

RouteForge writes one content-free row per routed request to `routing_outcomes`. The row
records what was eligible, what was tried, what was served, what it cost, and how step 9
verified it. Feedback scores go to a second table, `outcome_feedback`. An admin can export
both as NDJSON, and `scripts/outcome_report.py` summarises an export offline.

These records are a dataset for later analysis (P2b-3: learning bounded by the feasible
set). **Nothing reads them back into routing.** The router's decisions are the same with
the feature on or off.

- Design: `docs/superpowers/specs/2026-09-23-routing-outcome-records-design.md` (amended
  2026-09-23 where the build differs).
- Research notes (the dataset's model, invariants and limits):
  [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §13.
- The offline report: [`docs/guides/feasible-route-bench.md`](../guides/feasible-route-bench.md) §8.

## 1. Config

`config/serving.yaml` owns the switch. It is infrastructure, like the other observability
switches.

```yaml
outcomes:
  enabled: true
  retention_days: 90
```

| Key | Default | Effect |
|---|---|---|
| `enabled` | `true` | `false` writes no outcome row and no local feedback row (`recorded` is always `false`), skips the retention prune, and changes nothing else. The response and the usage row are the same either way. |
| `retention_days` | `90` | Rows older than this are deleted hourly (§8). Must be `> 0`; otherwise the config lint fails the boot with `OUTCOMES_RETENTION_NOT_POSITIVE`. |

A mounted `serving.yaml` written before this feature has no `outcomes:` block. It loads with
the defaults above.

**Deploy.** Both tables are new, so `create_all` creates them and no `ALTER` is needed. The
`tier_target` field added to the router's `DecisionRecord` lives in JSON (trace and
snapshot), not in a column.

## 2. Content-free by contract

Both tables hold **ids, enum values, numbers, entity types and version hashes only.** They
never hold prompt or completion text, tool-call arguments, `reason` strings, upstream error
text, feedback comments or corrected output.

This is the same rule the trace's `sp.candidates` and `sp.scoring` attributes follow. It is
what makes an admin-wide export acceptable: the export carries routing metadata, not
request content.

Enforced by:

| What | Test |
|---|---|
| No column on either table has a name that suggests text (`prompt_text`, `completion`, `content`, `message`, `comment`, `corrected`, `reason`, `error`); token counts are the only exemptions | `tests/test_outcome_store.py::test_schema_is_content_free` |
| A served request that contains an SSN and an e-mail address writes a row with neither value in any column; the entity **types** remain | `tests/test_outcome_hooks.py::test_outcome_row_carries_no_request_content` |
| The builder copies no text from the response or the trace | `tests/test_outcome_builder.py::test_build_ok_outcome_is_content_free` |
| `rejected` keeps `name` and `code` only, never `reason` | `tests/test_outcome_builder.py::test_build_failure_outcome_no_eligible_stores_only_name_and_code` |
| The gate's outcome context carries no prompt text, even when the prompt has an SSN | `tests/test_outcome_ctx_pipeline.py::test_outcome_ctx_has_no_prompt_text_even_with_ssn` |
| Feedback `comment` and `corrected_output` text is never stored locally | `tests/test_outcome_feedback.py::test_comment_and_corrected_output_text_never_stored_locally` |
| `workload` and categorical feedback values are regex-validated slugs of at most 64 characters | `tests/test_outcome_builder.py::test_parse_workload_*`, `tests/test_outcome_feedback.py::test_bad_categorical_value_is_refused` |

The SSN/e-mail test covers a served (`ok`) row. Failure rows are built from the same
outcome context and from candidate names and codes only, and the builder tests cover them,
but no end-to-end test sends PII through a failing request.

The one field a client controls is `workload`. The slug format limits what fits there, but
it cannot stop a client from encoding personal data in a slug. Clients must not do that
(§4).

## 3. `routing_outcomes`: one row per request that reached routing

Source: `app/apps/models.py::RoutingOutcome`, built by `app/apps/outcomes.py`
(`build_ok_outcome`, `build_failure_outcome`).

### 3.1 Statuses: when a row is written

| `status` | HTTP | Written when |
|---|---|---|
| `ok` | 200 | The request was served. Covers the plain response and every stream mode (`verified_then_stream`, `stream_unverified`, `incremental_verify`). |
| `backend_failed` | 502 | Every attempt failed upstream (`BackendUnavailable`), or a real-token stream's backend failed. The same request also gets a usage row with `status="error"`. |
| `verify_failed` | 422 | Step 9 failed closed (`response failed verification`). A model **was** served before step 9 refused it, so the row is attributed like an `ok` row (§3.5). |
| `no_eligible` | 422 | The candidate set was empty: after the eligibility filters, or after the router's per-request budget filter (`no_eligible_model`). |
| `context_overflow` | 400 | Every attempt refused on context length (`context_length_exceeded`). |

**No row is written** for requests that never reached a routing decision, or whose failure
says nothing about one:

- `401` (bad or missing token), `429` rate limit, `429` daily budget;
- `503` policy-gate error (`PolicyGateError`);
- `400` `streaming_not_supported`, `422` body validation errors;
- `422` pinned-model rejection (`model_not_found`: the client pinned a model the app may not
  use);
- `499` client cancellation (non-stream, and the keepalive stream). A cancellation is not an
  outcome of the routing decision;
- `500` unhandled errors;
- a real-token stream whose backend finished but produced no final result ("stream ended
  without a final result"). This is rare, fits no status, and writes no usage row either.
  It is a known gap.

A real-token stream (`stream_unverified`, `incremental_verify`) whose client disconnects
mid-stream is different: RouteForge drains the backend to recover the accounting, then
writes the `ok` row as usual.

### 3.2 Columns

"Null on failure rows" means `build_failure_outcome` leaves the column null, empty or zero,
because no response exists to read it from. **Exception:** a `verify_failed` row is built
from the served attempt's trace (§3.5), so the routing, verify, cost and token columns are
filled on it exactly as on an `ok` row.

| Column | Type | What it is |
|---|---|---|
| `request_id` | str, PK | The response `id`: `req-` followed by the 32 hex digits of a random UUID4 (36 characters, 128 random bits), so ids do not collide within the retention window. |
| `application_id` | str, indexed | The tenant. Foreign key to `applications`, `ON DELETE CASCADE`, like `usage_records`. |
| `ts` | datetime, indexed | When the row was written (UTC). |
| `trace_id` | str, nullable, indexed | The Langfuse trace id, present only when telemetry exported one. Always null on failure rows. A join key for `/v1/feedback`. |
| `status` | enum | §3.1. |
| `app_policy_version` | str | The app's compiled policy version, from the gate. |
| `endpoint_registry_version` | str, nullable | The endpoint registry version. `ok` rows read it from the response's `policy` block; failure rows read the live registry, and it is null if that read fails. |
| `routing_policy_version` | str | The routing+pricing content hash (`sp.routing_policy_version`). |
| `workload` | str, nullable | The validated client tag (§4). |
| `workload_source` | enum | `client` (a valid tag was supplied), `none` (nothing supplied) or `invalid` (supplied but rejected; `workload` is then null). |
| `tier_target` | str, nullable | The **first routing decision's** target tier (§3.5); when that decision is a failed gateway hop that carries none, the first router hop's. The target is the tier after the complexity rule, risk floor, escalation and the clamp to the best residency-allowed tier, and **before** the budget fallback. The served tier can be lower when the budget fallback stepped down. Null on failure rows other than `verify_failed`. |
| `complexity` | float, nullable | The router's complexity input, `tokens_after / 2000` (post-compression tokens). Written on failure rows too when compression ran before the failure. Null on a semantic-cache hit. |
| `prompt_tokens_bucket` | enum, nullable | `<1k`, `1k-4k`, `4k-16k`, `16k-64k` or `>64k`, over the response's `usage.prompt_tokens`. Lower bound inclusive: exactly 1000 tokens is `1k-4k`. Null on failure rows other than `verify_failed`. |
| `sensitivity` | enum | `public`, `internal`, `confidential` or `restricted`, from the gate. |
| `risk_class` | str | From the gate. |
| `onprem_only` | bool | From the gate. |
| `detected_entity_types` | JSON list[str] | Entity **types** the gate detected (`US_SSN`, `EMAIL_ADDRESS`), sorted and de-duplicated. Never values. |
| `eligible` | JSON list[str] | The names the **first routing decision** (§3.5) **scored** (`scoring.candidates`): the candidates left after the eligibility filters and the router's own tier, budget and circuit filters. Falls back to `[chosen]` when the hop carries no scoring record. `[]` on failure rows other than `verify_failed`. |
| `rejected` | JSON list[{name, code}] | Candidates excluded, `{name, code}` only. On `ok` and `verify_failed` rows: the first routing decision's (§3.5) rejected list. An eligibility-stage rejection carries its stable code (`residency`, `region_not_allowed`, `circuit_open`, …); the router's own later filters (tier, budget, lower score) have `code: null`. A scored candidate that lost appears in both `eligible` and `rejected` (code null). On `no_eligible` rows, see §3.3. |
| `attempts` | JSON list[str] | Models tried, in hop order, including failed hops. `ok`: each failed gateway hop's first-scored candidate and each router hop's chosen model. `verify_failed`: read from the served attempt's trace, as for `ok`. `backend_failed` and `context_overflow`: the models the gateway attempted; a real-token stream has exactly one. `[]` on `no_eligible`. |
| `chosen` | str, nullable | The served model: the last router hop that chose one. Null when nothing was served, and also on a semantic-cache hit (§3.4). |
| `scoring` | JSON, nullable | The first routing decision's (§3.5) scoring record (`RankResult.to_record()`, the same content as `sp.scoring`): weights, and per candidate its score and four terms with their sources. Ids and numbers only. Null on failure rows other than `verify_failed`. |
| `circuit_bypass` | bool | True if any routing decision bypassed the circuit breaker (every candidate in the target tier was open). False on failure rows. |
| `cache_hit` | bool | True when a semantic-cache entry served the request (§3.4). False otherwise, and on every failure row. |
| `cost_usd` | float | The total cost across hops (`cost.total_usd`). On `verify_failed` rows the served attempt's cost, which was incurred. `0` on other failure rows. |
| `latency_ms` | float | **Two definitions.** `ok` rows: the sum of the trace records' `latency_ms` (the same figure the usage row carries). Failure rows: wall-clock time from the start of the request handler to the failure, which includes authentication and gate time. Do not pool the two. |
| `prompt_tokens`, `completion_tokens` | int | From the response `usage` (on `verify_failed`, the served attempt's). `0` on other failure rows. |
| `verify_required`, `verify_performed`, `verify_passed` | bool, nullable | From the trace's `verify` record. On `verify_failed` rows `true`, `true`, `false`. Null on other failure rows. |
| `verify_mode` | enum, nullable | See below. `failed` on `verify_failed` rows; null on other failure rows. |

**`verify_mode`** is derived from the `verify` record, checking the reason first:

1. reason starts with `waived` → `waived_stream` (`stream_unverified`);
2. reason is `incremental` → `incremental` (`incremental_verify`);
3. `required` is false → `not_required`;
4. `passed` is false → `failed`;
5. otherwise → `verified`.

The reason is checked first because real `stream_unverified` and `incremental_verify` rows
always carry `required: false`. Checking `required` first would record them all as
`not_required`. A `verified_then_stream` request runs the full pipeline, so it records
`verified` or `not_required` like a plain request.

### 3.3 `no_eligible` rows: telling the cases apart

A `no_eligible` row has no trace, so `eligible` is `[]` and `rejected` comes from the
exception (`NoEligibleCandidate.rejected`):

| Case | `rejected` |
|---|---|
| Eligibility filters removed every candidate (residency, app scope, region, retention, …) | every candidate, each with its stable `code` |
| Per-request budget filter (`max_cost_usd_per_request`) removed every candidate in reach | the over-budget candidates, each with `code: null` |
| The pool is empty (no model onboarded, none in YAML) | `[]` |

So the budget case and the empty-pool case are distinguishable only by `rejected` being
non-empty with null codes.

### 3.4 Semantic-cache hits

A request served from the semantic cache is recorded as `ok`, but its trace has no router
hop (only the `verify` and `cache` records). So a cache-hit row has `cache_hit: true`, and
`chosen`, `tier_target`, `scoring` and `complexity` are null, and `eligible` and `attempts`
are `[]`. The model that produced the cached answer is not recorded. The report puts these
rows in their own chosen bucket, `(cache)`: never in an endpoint's bucket (that endpoint
did not answer), and never in `(none)` (the request was served).

### 3.5 Which hop each column describes

A request can take several hops: a **failover** (the chosen model's call failed, the
gateway re-selects without it) or a **confidence escalation** (the served answer scored
below the threshold, the gateway re-routes to a higher tier). The row describes two
different hops, on purpose:

- `eligible`, `scoring`, `tier_target` and `rejected` describe the request's **first
  routing decision**: the first trace entry that is either a router hop or a failed gateway
  hop carrying `scoring`. That is the decision that picked the first attempt. After a
  failover it is the failed hop (its scoring and rejected set are kept on the gateway
  record); after an escalation it is the first, unserved router hop. `tier_target` comes
  from that record, or from the first router hop when the failed hop carries none.
- `chosen` is the **served** model: the last router hop that chose one.
- `attempts` lists every model tried, in order, so the path between the two is visible.

So the scoring record ranks the first attempt, not necessarily `chosen`. It is enough to
recompute the router's **first** ranking offline; it is not the record of the hop that
served. Learning studies the router's first choice and its outcome, and every later hop
is recoverable from `attempts`.

A `verify_failed` row follows the same rules. `Pipeline.run` stashes the trace of the
served attempt, with every free-text `reason` removed, in the route's `internals`
out-parameter before it raises (`app/apps/outcomes.py::outcome_trace`); the route builds
the row from it through the same reader as an `ok` row (`trace_features`).

## 4. Workload tagging

A client may label a request with a workload, such as `claims.summary`. The label groups
outcomes in the export and the report. It never affects routing.

| Source | Precedence |
|---|---|
| `x-routeforge-workload` request header | wins whenever present, valid or not |
| `metadata.workload` in the request body (the OpenAI `metadata` object) | used only when no header was sent |

- **Validation.** The value is lower-cased, then must match `^[a-z0-9][a-z0-9._-]{0,63}$`
  (at most 64 characters).
- **Invalid tags are dropped, not rejected.** The request is served normally (HTTP 200) and
  the row records `workload: null, workload_source: "invalid"`. A header that fails
  validation is not replaced by `metadata.workload`. A `metadata` that is not an object, or
  a `workload` that is not a string, is also `invalid`. No header and no `metadata.workload`
  key gives `workload_source: "none"`.
- **Not forwarded upstream.** `metadata` is not in `FORWARDED_PARAMS`, so the backend never
  sees it (`tests/test_outcome_hooks.py::test_metadata_workload_is_stored_and_not_forwarded_upstream`).
- **No personal data.** Treat the tag like `x-routeforge-session`: a label for a kind of
  work, not an identifier for a person or a record. Clients must not put personal data in
  it. The slug format limits what fits, but it cannot enforce this, and the tag is stored
  as given.

## 5. `outcome_feedback`: zero or more rows per request

Source: `app/apps/models.py::OutcomeFeedback`.

| Column | Type | What it is |
|---|---|---|
| `id` | int, PK, autoincrement | |
| `request_id` | str, indexed | A **logical** link to `routing_outcomes.request_id`, not a foreign key. |
| `application_id` | str, indexed | Always the application that owns the outcome row: the authenticated caller for `/v1/feedback`, the snapshot's tenant for admin feedback. |
| `ts` | datetime, indexed | When the score was recorded. |
| `score_name` | str | One of `route_correct`, `preferred_model`, `failure_type`, `quality`, `corrected_output`. |
| `value_num` | float, nullable | A numeric score. NaN and ±infinity are refused (no row). |
| `value_cat` | str, nullable | A categorical score. Must match `[A-Za-z0-9._:/-]{1,100}` (`/` so a `preferred_model` score can name a slashed model slug such as `nvidia/llama-3.1-nemotron-nano-8b-v1`); a value that does not is refused (no row), not truncated. |
| `source` | enum | `app` (`POST /v1/feedback`) or `admin` (the console). |

**Never stored:** `comment` and `corrected_output` text. Both are free text that can carry
PII. They go only to the app's own Langfuse project, as before this feature. A
`corrected_output` submission writes a second row, `score_name: "corrected_output"`,
`value_num: 1.0`, so "a correction was submitted" can still be counted.

## 6. Feedback linkage

### 6.1 `POST /v1/feedback` (app token)

The body names the request by `request_id`, `trace_id`, or both. At least one is required;
neither gives `422 one of trace_id or request_id is required`.

1. **Lookup.** RouteForge looks for the outcome row by `request_id` when given, otherwise by
   `trace_id`, **scoped to the authenticated caller's application**. Another application's
   request is indistinguishable from one that does not exist: both give `recorded: false`
   and no local row.
2. **Local record.** If the row exists, the score is written to `outcome_feedback` with
   `source: "app"`.
3. **Langfuse.** The score is posted to the app's own Langfuse project, as before, when a
   trace id is known: the one in the body, or else the outcome row's `trace_id`.

The response is `{"stored": <bool>, "recorded": <bool>}`, always HTTP 200:

| Field | Meaning |
|---|---|
| `stored` | Unchanged: whether the score reached the app's Langfuse project. `false` when the app has no provisioned project, no trace id is known, or the post failed. |
| `recorded` | Whether the **primary** score was written locally. `false` when outcomes are disabled, the request was not found for this app, a categorical value failed validation, or the local store failed. The extra `corrected_output` row does not affect it. |

Local store failures are logged (`serving.obs` warning) and swallowed. They never change
the HTTP status or `stored`, and never block the Langfuse post.

Feedback can be recorded for any status, including failures, by `request_id`. A failure
row has no `trace_id`, so feedback on it is local only.

### 6.2 Admin feedback (`POST /api/requests/{request_id}/feedback`, admin session)

The console's feedback form records with `source: "admin"`, under the request's own
application. It records locally **even when the request has no trace**. That case
previously did nothing; it now returns `{"stored": false, "reason": "no trace",
"recorded": <bool>}`. The request must still be in the console's `request_snapshots`
cache (404 otherwise), and that cache keeps only the most recent requests.

## 7. Export: `GET /admin/outcomes/export`

Admin-only (the admin session cookie; `401` without it). Returns `application/x-ndjson`,
streamed.

| Parameter | Default | Notes |
|---|---|---|
| `since` | `until` minus 7 days | ISO-8601. A value with no offset is taken as UTC. A bad value is `422`. |
| `until` | now | ISO-8601, as above. The range is inclusive at both ends. |
| `application_id` | all applications | Filter to one application. |
| `limit` | `100000` | Maximum lines. Values above 100 000 are capped at 100 000; a value below 1 is `422`. |

Rows are ordered by `(ts, request_id)` and read in keyset pages of 1000, each in its own
database session, so a large export is never held in memory. Each row's feedback is joined
per page by `(application_id, request_id)`, never by `request_id` alone.

**Line format** (`routeforge.outcome/v1`), one JSON object per line:

```json
{"schema": "routeforge.outcome/v1",
 "outcome": {"request_id": "req-3f9c1a2b0d4e4c6f8a1b2c3d4e5f6a7b", "application_id": "app-claims",
             "ts": "2026-09-23T10:15:02.118000+00:00", "trace_id": "8d2e…",
             "status": "ok", "app_policy_version": "39260c5ee1ad",
             "endpoint_registry_version": "a41c…", "routing_policy_version": "5be0…",
             "workload": "claims.summary", "workload_source": "client",
             "tier_target": "cheap", "complexity": 0.41, "prompt_tokens_bucket": "<1k",
             "sensitivity": "restricted", "risk_class": "high", "onprem_only": true,
             "detected_entity_types": ["US_SSN"],
             "eligible": ["qwen3.8-27b", "llama-8b"],
             "rejected": [{"name": "gpt-cloud", "code": "residency"},
                          {"name": "llama-8b", "code": null}],
             "attempts": ["qwen3.8-27b"], "chosen": "qwen3.8-27b",
             "scoring": {"weights": {…}, "candidates": [{"name": "qwen3.8-27b", "score": …, …}, …], …},
             "circuit_bypass": false, "cache_hit": false, "cost_usd": 0.00021, "latency_ms": 3620.4,
             "prompt_tokens": 812, "completion_tokens": 140,
             "verify_required": true, "verify_performed": true, "verify_passed": true,
             "verify_mode": "verified"},
 "feedback": [{"score_name": "quality", "value": 0.8, "source": "app",
               "ts": "2026-09-23T10:20:44.002000+00:00"}]}
```

(Wrapped here for reading; on the wire each record is one line. Values are illustrative.)

- `outcome` carries every `routing_outcomes` column. `ts` is ISO-8601 with an explicit
  offset.
- `feedback` lists that request's `outcome_feedback` rows. `value` is `value_num` when set,
  otherwise `value_cat`.
- The schema tag's major version (`v1`) changes only on an incompatible change. The loader
  refuses any other major.

```bash
curl -s -b "rf_admin_session=<session>" \
  "http://<host>:8080/admin/outcomes/export?since=2026-09-16T00:00:00Z&application_id=app-claims" \
  > outcomes.ndjson
```

`rf_admin_session` is the cookie the console sets at admin login.

Tested by `tests/test_outcome_export.py` (shape, feedback join, filters, `limit`, paging
across a page boundary, 401 without admin, 422 on bad dates).

## 8. Retention

The hourly beat of the observation flusher (`app/main.py::_observation_loop`, which also
runs once at startup) calls `prune_outcomes(older_than_days=retention_days)`:

- it deletes `routing_outcomes` rows by their `ts`, and `outcome_feedback` rows by **their
  own** `ts`. Feedback given late can outlive its outcome row by up to that delay;
- it runs in its own session and `try` block, independent of the observation-log prune. A
  failure in one never stops the other or the flusher;
- it is skipped when `outcomes.enabled` is `false`. Rows written before the switch was turned
  off stay until it is turned back on.

## 9. Write path and failure handling

- **Where.** `ok` rows are written in `_finalize` (`app/api/routes.py`), right after the usage
  row, for every response shape. Failure rows are written from the same error handlers that
  map the exception to its HTTP status (`_record_exception_outcome`), on the plain path and
  in the keepalive stream's error frame. A real-token stream's backend failure goes through
  `_record_failed_stream`.
- **Best effort.** A failed build or write is logged as a `serving.obs` warning
  (`outcome record failed request=<id>`) and swallowed. It never fails the request or
  changes the response, and it never touches the usage row. Metering stays the billing
  record.
- **Cost.** The write is inline, in its own database session: **one extra commit per
  request**, in the same place as the usage row. It does add that latency.
- **No metric.** There is no write-failure counter; `/metrics` still has five families. A
  failed write is an operator log concern, not a routing signal. Alert on the log line if
  you need to.
- **Identity.** `tests/test_outcome_hooks.py::test_outcomes_disabled_writes_no_row_and_same_response`
  checks that the response is the same with the feature on and off.

## 10. Store functions

`app/apps/store.py`: `write_outcome`, `get_outcome`, `find_outcome`, `write_feedback`,
`feedback_for`, `prune_outcomes`, `iter_outcomes`. `feedback_for` takes an optional
`application_id`; the export always passes it. Each takes an `AsyncSession`, and the
caller commits. `get_outcome` and `find_outcome` are app-scoped: a row owned by another app
returns `None`. `find_outcome` with neither id returns `None`, never an unscoped query.
`iter_outcomes` pages by the keyset `(ts, request_id)` rather than `OFFSET`, so paging stays
stable under concurrent writes.

## 11. What these records cannot tell you

**Production observes only the served endpoint for each request.** There is no record of
what another eligible endpoint would have returned, so oracle regret cannot be computed
from outcomes, and the report never prints it. Comparing endpoints across requests is
confounded by the router's own choices: the router sends different kinds of request to
different endpoints. The router is deterministic, so there are no propensities to correct
for that either. Treat any per-endpoint figure as descriptive, not causal.
FeasibleRouteBench (the offline sweep of every eligible endpoint) is the tool for
counterfactual comparisons; exploration within the feasible set is P2b-3.
