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
- Placement: `onprem` when the provider is `onprem`, else `cloud`.

## Minimum n

30 requests (rates, per-model figures), 20 labelled tasks (reported success), 30 non-streamed ok attempts (speed), 10 streamed attempts (TTFT). Below minimum: `value` null, `insufficient` true, `n` given.

## Scope of speed and TTFT

`speed_tps` and `ttft_ms` come from `endpoint_observations`, which carry no tenant: they are model-level over all applications and marked `"scope": "all_applications"`, ignoring the `application_id`/`workload` filters.

## Statuses

Worst first: `cost_anomaly`, `performance_degradation`, `cache_regression`, `insufficient_data`, `healthy`. Non-healthy statuses (except `insufficient_data`) also appear in `alerts`, after the page alerts (`prices_undated`, `prices_stale`, `savings_partial`).
