# Models-page metrics API

Admin-authenticated, content-free (model names, app ids, workload slugs, numbers, enums).

## Endpoints

- `GET /admin/metrics/summary` returns `window`, `current`, `previous`, `alerts`, `baseline`, `generated_at`.
- `GET /admin/metrics/models` returns `window`, `models`, `generated_at`.

Query parameters (both): `hours` (default 720, 1-8760, else 422), `application_id`, `workload`, `placement` (`onprem` | `cloud`, else 422). The previous window is the same length immediately before the current one.

## Definitions

- Completed without failure: `routing_outcomes.status` `ok` over `ok`, `backend_failed`, `verify_failed`, `context_overflow`. `no_eligible` is `policy_rejections`; other statuses are `excluded`.
- Task: a `trajectory_key` group; a request without one is its own task (`task_unit`).
- Reported success: latest `task_success` feedback per task. Cost per successful task = cost of labelled tasks / successful labelled tasks.
- Savings: sum of the four `*_saved_usd` columns over rows with `routing_saved_usd` not NULL; `excluded_rows` counts the rest.
- Attribution: a failure outcome (`backend_failed`, `context_overflow`) is stored with `chosen = null` and the failed model in `attempts`; it belongs to `chosen` else the last attempt. Per-model `completed`, `errors`, `failovers` and every placement filter use this. `no_eligible` and `cancelled` have no model.
- Placement: from the registry's effective pool (provider `onprem` is `onprem`, anything else `cloud`), else from the provider on the model's usage rows (ignoring `-` and `?`), else `unknown`. With `placement=` set, outcomes without a model are dropped and `completed.policy_rejections` and `completed.excluded` are `null` (not attributable), never 0.
- Usage rows whose model is `-` or `?` (cancelled, daily-budget 429, unrecognised name) are in `spend` but are not model rows.
- Labels: a `task_success` value of exactly 1 is success, exactly 0 is failure, anything else is ignored; for equal timestamps the higher feedback id wins.
- A task is credited to the model with the most spend; ties go to a model that served (`ok`), then the name.
- `empty` (per window summary): true only when the window has no usage rows and no outcome rows.
- Workload filter: `current.workloads` is the list before the workload filter is applied.

## Per-model row additions

- `successful_reported`: `{value, n, insufficient}`, the model's labelled tasks, minimum 20.
- `chart`: the server-chosen point `{x, x_kind, y, y_kind}` or `null`. `x` is cost per successful task (`per_successful_task`) when that has a value, else cost per task (`per_task`); `y` is `successful_reported` (`reported`) when it has a value, else `completed` (`completed`). `null` when either has no value.
- `status_rank`: 0 (`cost_anomaly`) to 5 (`healthy`), the sort key for severity.
- `cost_per_task.successful` (summary and per model) is `null` below 20 labelled tasks; `successful_insufficient` says so.

## Cost of a request

Each call scans every usage, outcome and feedback row of the current and previous window (selected columns only, no row cap, because a cap would bias the figures). Large windows (90 days on a busy deployment) are slow; concurrent identical calls (the page fires both endpoints) share one scan. Status reasons print money with 2 decimals from one cent up, else 4 significant digits.

## Minimum n

30 requests (rates, per-model figures), 20 labelled tasks (reported success), 30 non-streamed ok attempts (speed), 10 streamed attempts (TTFT). Below minimum: `value` null, `insufficient` true, `n` given.

## Scope of speed and TTFT

`speed_tps` and `ttft_ms` come from `endpoint_observations`, which carry no tenant: they are model-level over all applications and marked `"scope": "all_applications"`, ignoring the `application_id`/`workload` filters.

## Statuses

Worst first: `cost_anomaly`, `performance_degradation`, `cache_regression`, `price_drift`, `insufficient_data`, `healthy`. Non-healthy statuses (except `insufficient_data`) also appear in `alerts`, after the page alerts (`prices_undated`, `prices_stale`, `savings_partial`, `aa_snapshot_missing` info, `aa_snapshot_stale` warning when the newest snapshot is over 30 days old).

## Published metrics (Artificial Analysis)

Display-only; attribution "Published data: Artificial Analysis" (https://artificialanalysis.ai/). Each model row carries `published` (the mapped AA model's fields plus `snapshot_id`, `retrieved_at`, `index_version`; `null` when unmapped or the slug is not in the latest snapshot; any metric may be `null` = not published) and `comparability` `{overall, overall_rank, speed, ttft, price, reasons[]}` (`overall_rank` 0 = comparable .. 3 = not_comparable, server-owned, for sorting). Labels: `comparable`, `partially_comparable`, `not_comparable`, `insufficient_local_data`; `overall` is the worst (`not_comparable` > `insufficient_local_data` > `partially_comparable` > `comparable`). On-prem and unmapped models are `not_comparable`; local n below the speed/TTFT minimum is `insufficient_local_data`; a median prompt outside 500-2000 tokens makes speed/TTFT `partially_comparable`; the provider note is informational. `price_drift` (cloud, mapped, `pricing.yaml` input or output more than 10% from AA's) is a status. `/summary` adds `published` `{snapshot_id, retrieved_at, dated_at_upload, index_version, source, origin}` or `null`; `retrieved_at` is always timezone-aware UTC.
