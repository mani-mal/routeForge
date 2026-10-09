> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: DGX lab live run 2, 2026-10-08 (three paper apps, three endpoint names over one server, 900 rows)

900 rows simulated, 5 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 92.6%** over 880 compared rows.

Per app: paper-claims 87.2% ⚠ · paper-clinic 100.0% · paper-dev 89.1% ⚠

**Outcome agreement: 100.0%** over 900 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Note:** 20 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps | peak / avg load (proposed) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1→1 | 0 | none | peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |
| all-apps-no-retention | n/a | 0.46795 | 248 | 4 | 0 | 83.5711 | 0 | 0.071429 | 1→1 | 0 | paper-claims, paper-dev | peak 115.0% / avg 80.7%, over 100% in 17.6% of 17 buckets (glm-flash-east, requests_per_minute, 60 s buckets) |
| all-apps-us-lab-only | n/a | 1.40954 | 296 | 5 | 0 | 0 | -83.5711 | -0.003258 | 1→1 | 0 | paper-claims, paper-dev | peak 127.5% / avg 90.4%, over 100% in 47.1% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |
| clinic-may-use-eu-lab | n/a | -1.03733 | 329 | 0 | 10 | 83.5711 | 0 | 0.000801 | 1→1 | 0 | paper-clinic | peak 132.5% / avg 94.1%, over 100% in 52.9% of 17 buckets (glm-flash-east, requests_per_minute, 60 s buckets) |
| raise-request-budget | n/a | 0.149515 | 4 | 0 | 18 | 0 | 0 | 0.00254 | 1→1 | 0 | paper-claims, paper-clinic, paper-dev | peak 77.5% / avg 50.1%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |

Listed in file order. RouteForge does not rank or recommend scenarios.

Cost columns compare only the rows routed in both arms. A row a scenario newly rejects is counted under "newly rejected", never as a saving.

Peak load is the proposed arm's most loaded stated limit and the endpoint it is on: its largest bucket, then its average over the whole window, then the share of the window's buckets in which it is above 100%. A peak is a maximum over buckets, endpoints and limits, so read it with the average and the share. It is listed in file order like every other column: a lower figure does not make a scenario the one to choose.

## Capacity

> **Stated capacity against replayed demand.** Every limit below is a figure the operator
> stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed
> route of each recorded request. A request arrives at its estimated arrival time (the time
> the record was written minus its recorded latency) and is in service from then for its
> service time: requests and tokens are counted in the bucket of arrival, service time in
> every bucket it runs through. Service time is the request's own recorded latency where
> replay serves it on the endpoint that served it alone; otherwise that endpoint's mean
> recorded latency; otherwise replay's latency for the endpoint, unless that is only a tier
> default, which is not a service time: no concurrency figure is then computed. A latency the
> proposal asserts replaces all three. A recorded latency is what RouteForge recorded for the
> whole request, not the backend's own time: it includes the time RouteForge spent before and
> after the backend call and any time the request waited in the endpoint's own queue, and for
> a request that failed over it cannot be attributed to one endpoint, so it is not used. So
> concurrency from recorded requests is the number in flight as recorded, waiting included,
> not the number an idle endpoint would have had in service. The window ends when the last
> recorded request ended; service time that runs past it is in no bucket and is reported as a
> share. A peak is a maximum over buckets: it grows with a longer window and with a shorter
> bucket even for a steady stream, so every peak is printed with the average over the whole
> window and the share of buckets above the limit. Nothing here models queueing, batching,
> autoscaling, the serving engine's real concurrency behaviour, load that reaches an endpoint
> without passing through RouteForge, bursts shorter than a bucket, or the load of failover
> attempts. RouteForge does not know what an endpoint does above a stated limit (queue, shed
> or slow down), so no rejection and no delay is predicted. An export of a few buckets says
> little.

This section reports load against stated limits and proposes no remedy: not more capacity, and not a change to any policy or protection.

Buckets of 60 s. The window is 17 bucket(s), from the first estimated arrival to the end of the last recorded request (2026-10-08T23:33:00+00:00 to 2026-10-08T23:49:00+00:00); a request arrived in 17 of them; 900 simulated row(s).

`avg (window)` is over every bucket of the window, empty ones included: the time-average. `avg (active)` is over the buckets in which the endpoint had an arrival or a request in service; that count is printed with it.

Current arm peak load (the same for every scenario): peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

## Scenario: no-change

requests: 900 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 6.00133 | 0 |
| cost_usd (both routed) | 6.00133 | 6.00133 | 0 |
| latency_ms p50 | 485.741 | 485.741 | 0 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.678571 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

### Capacity

Peak load: current peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 329, proposed 329. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded); proposed mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| max_concurrency | proposed | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | current | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | proposed | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| tokens_per_minute | current | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |
| tokens_per_minute | proposed | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.28065 | 0.168295 | 0.168295 |
| concurrency | proposed | 0.28065 | 0.168295 | 0.168295 |
| requests_per_minute | current | 31 | 19.3529 | 19.3529 |
| requests_per_minute | proposed | 31 | 19.3529 | 19.3529 |
| tokens_per_minute | current | 32058 | 15254.8 | 15254.8 |
| tokens_per_minute | proposed | 32058 | 15254.8 | 15254.8 |
| output_tokens_per_minute | current | 2208 | 1407.47 | 1407.47 |
| output_tokens_per_minute | proposed | 2208 | 1407.47 | 1407.47 |

#### glm-flash-east

Requests: current 301, proposed 301. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 602.096 ms, longest 1377 ms over 301 row(s) (228 recorded, 73 endpoint_mean); proposed mean 602.096 ms, longest 1377 ms over 301 row(s) (228 recorded, 73 endpoint_mean).

**Note:** 2 row(s) routed here in the current arm have no backend token count; their token demand is RouteForge's own estimate.

**Note:** 2 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 13.8% | 8.9% | 8.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| max_concurrency | proposed | 2 | 13.8% | 8.9% | 8.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | current | 40 | 67.5% | 44.3% | 44.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | proposed | 40 | 67.5% | 44.3% | 44.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | current | 80000 | 72.3% | 40.3% | 40.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | proposed | 80000 | 72.3% | 40.3% | 40.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.2754 | 0.177677 | 0.177677 |
| concurrency | proposed | 0.2754 | 0.177677 | 0.177677 |
| requests_per_minute | current | 27 | 17.7059 | 17.7059 |
| requests_per_minute | proposed | 27 | 17.7059 | 17.7059 |
| tokens_per_minute | current | 57814 | 32201.2 | 32201.2 |
| tokens_per_minute | proposed | 57814 | 32201.2 | 32201.2 |
| output_tokens_per_minute | current | 1968 | 1297.88 | 1297.88 |
| output_tokens_per_minute | proposed | 1968 | 1297.88 | 1297.88 |

#### glm-flash-econ

Requests: current 252, proposed 252. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded); proposed mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| max_concurrency | proposed | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | proposed | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| tokens_per_minute | current | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |
| tokens_per_minute | proposed | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.185717 | 0.115285 | 0.115285 |
| concurrency | proposed | 0.185717 | 0.115285 | 0.115285 |
| requests_per_minute | current | 24 | 14.8235 | 14.8235 |
| requests_per_minute | proposed | 24 | 14.8235 | 14.8235 |
| tokens_per_minute | current | 21262 | 7264.29 | 7264.29 |
| tokens_per_minute | proposed | 21262 | 7264.29 | 7264.29 |
| output_tokens_per_minute | current | 1536 | 1039.12 | 1039.12 |
| output_tokens_per_minute | proposed | 1536 | 1039.12 | 1039.12 |

## Scenario: all-apps-no-retention

requests: 900 &middot; route_changed: 248 &middot; newly_rejected: 4 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 6.43938 | 0.438047 |
| cost_usd (both routed) | 5.97143 | 6.43938 | 0.46795 |
| latency_ms p50 | 485.741 | 569.312 | 83.5711 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.75 | 0.071429 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| glm-flash-econ | glm-flash-east | 248 |
| glm-flash-econ | (rejected) | 4 |

### New rejections

| app | count |
|---|---|
| paper-claims | 4 |

| code | count |
|---|---|
| over_budget | 4 |

**By conflicting core**

| core | count |
|---|---|
| max_cost_usd_per_request | 4 |

Affected apps: paper-claims, paper-dev

### Capacity

Peak load: current peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 115.0% / avg 80.7%, over 100% in 17.6% of 17 buckets (glm-flash-east, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 329, proposed 329. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded); proposed mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| max_concurrency | proposed | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | current | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | proposed | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| tokens_per_minute | current | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |
| tokens_per_minute | proposed | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.28065 | 0.168295 | 0.168295 |
| concurrency | proposed | 0.28065 | 0.168295 | 0.168295 |
| requests_per_minute | current | 31 | 19.3529 | 19.3529 |
| requests_per_minute | proposed | 31 | 19.3529 | 19.3529 |
| tokens_per_minute | current | 32058 | 15254.8 | 15254.8 |
| tokens_per_minute | proposed | 32058 | 15254.8 | 15254.8 |
| output_tokens_per_minute | current | 2208 | 1407.47 | 1407.47 |
| output_tokens_per_minute | proposed | 2208 | 1407.47 | 1407.47 |

#### glm-flash-east

Requests: current 301, proposed 549. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 602.096 ms, longest 1377 ms over 301 row(s) (228 recorded, 73 endpoint_mean); proposed mean 602.053 ms, longest 1377 ms over 549 row(s) (228 recorded, 321 endpoint_mean).

**Note:** 2 row(s) routed here in the current arm have no backend token count; their token demand is RouteForge's own estimate.

**Note:** 2 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 13.8% | 8.9% | 8.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| max_concurrency | proposed | 2 | 22.9% | 16.2% | 16.2% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | current | 40 | 67.5% | 44.3% | 44.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | proposed | 40 | 115.0% | 80.7% | 80.7% | 3 of 17 | 24.0% | 2026-10-08T23:48:00+00:00 |
| tokens_per_minute | current | 80000 | 72.3% | 40.3% | 40.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | proposed | 80000 | 80.3% | 47.1% | 47.1% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.2754 | 0.177677 | 0.177677 |
| concurrency | proposed | 0.457233 | 0.324046 | 0.324046 |
| requests_per_minute | current | 27 | 17.7059 | 17.7059 |
| requests_per_minute | proposed | 46 | 32.2941 | 32.2941 |
| tokens_per_minute | current | 57814 | 32201.2 | 32201.2 |
| tokens_per_minute | proposed | 64216 | 37706.5 | 37706.5 |
| output_tokens_per_minute | current | 1968 | 1297.88 | 1297.88 |
| output_tokens_per_minute | proposed | 3101 | 2322.88 | 2322.88 |

#### glm-flash-econ

Requests: current 252, proposed 0. Active buckets (an arrival or a request in service): current 17 of 17, proposed 0 of 17.

Service time: current mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| max_concurrency | proposed | 2 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |
| requests_per_minute | current | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | proposed | 40 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |
| tokens_per_minute | current | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |
| tokens_per_minute | proposed | 80000 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.185717 | 0.115285 | 0.115285 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 24 | 14.8235 | 14.8235 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 21262 | 7264.29 | 7264.29 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 1536 | 1039.12 | 1039.12 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

## Scenario: all-apps-us-lab-only

requests: 900 &middot; route_changed: 296 &middot; newly_rejected: 5 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 7.17363 | 1.1723 |
| cost_usd (both routed) | 5.76409 | 7.17363 | 1.40954 |
| latency_ms p50 | 485.741 | 485.741 | 0 |
| latency_ms p95 | 569.312 | 485.741 | -83.5711 |
| quality_mean | 0.678571 | 0.675314 | -0.003258 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| glm-flash-east | glm-5.3-flash | 286 |
| glm-flash-east | glm-flash-econ | 10 |
| glm-flash-east | (rejected) | 5 |

### New rejections

| app | count |
|---|---|
| paper-claims | 1 |
| paper-dev | 4 |

| code | count |
|---|---|
| over_budget | 5 |

**By conflicting core**

| core | count |
|---|---|
| max_cost_usd_per_request+risk_floor | 5 |

Affected apps: paper-claims, paper-dev

### Capacity

Peak load: current peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 127.5% / avg 90.4%, over 100% in 47.1% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 329, proposed 615. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded); proposed mean 528.067 ms, longest 1541 ms over 615 row(s) (394 recorded, 221 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| max_concurrency | proposed | 2 | 23.0% | 15.9% | 15.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | current | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | proposed | 40 | 127.5% | 90.4% | 90.4% | 8 of 17 | 59.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | current | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |
| tokens_per_minute | proposed | 80000 | 82.1% | 51.3% | 51.3% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.28065 | 0.168295 | 0.168295 |
| concurrency | proposed | 0.459433 | 0.318393 | 0.318393 |
| requests_per_minute | current | 31 | 19.3529 | 19.3529 |
| requests_per_minute | proposed | 51 | 36.1765 | 36.1765 |
| tokens_per_minute | current | 32058 | 15254.8 | 15254.8 |
| tokens_per_minute | proposed | 65649 | 41064.9 | 41064.9 |
| output_tokens_per_minute | current | 2208 | 1407.47 | 1407.47 |
| output_tokens_per_minute | proposed | 3824 | 2651.71 | 2651.71 |

#### glm-flash-east

Requests: current 301, proposed 0. Active buckets (an arrival or a request in service): current 17 of 17, proposed 0 of 17.

Service time: current mean 602.096 ms, longest 1377 ms over 301 row(s) (228 recorded, 73 endpoint_mean); proposed none (no row routed there).

**Note:** 2 row(s) routed here in the current arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 13.8% | 8.9% | 8.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| max_concurrency | proposed | 2 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |
| requests_per_minute | current | 40 | 67.5% | 44.3% | 44.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | proposed | 40 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |
| tokens_per_minute | current | 80000 | 72.3% | 40.3% | 40.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | proposed | 80000 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.2754 | 0.177677 | 0.177677 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 27 | 17.7059 | 17.7059 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 57814 | 32201.2 | 32201.2 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 1968 | 1297.88 | 1297.88 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### glm-flash-econ

Requests: current 252, proposed 262. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded); proposed mean 466.645 ms, longest 1375 ms over 262 row(s) (252 recorded, 10 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| max_concurrency | proposed | 2 | 9.7% | 6.0% | 6.0% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | proposed | 40 | 62.5% | 38.5% | 38.5% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| tokens_per_minute | current | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |
| tokens_per_minute | proposed | 80000 | 26.6% | 14.2% | 14.2% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.185717 | 0.115285 | 0.115285 |
| concurrency | proposed | 0.1935 | 0.119864 | 0.119864 |
| requests_per_minute | current | 24 | 14.8235 | 14.8235 |
| requests_per_minute | proposed | 25 | 15.4118 | 15.4118 |
| tokens_per_minute | current | 21262 | 7264.29 | 7264.29 |
| tokens_per_minute | proposed | 21262 | 11329.5 | 11329.5 |
| output_tokens_per_minute | current | 1536 | 1039.12 | 1039.12 |
| output_tokens_per_minute | proposed | 1632 | 1073 | 1073 |

## Scenario: clinic-may-use-eu-lab

requests: 900 &middot; route_changed: 329 &middot; newly_rejected: 0 &middot; newly_served: 10

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 5.37566 | -0.625674 |
| cost_usd (both routed) | 6.00133 | 4.96401 | -1.03733 |
| latency_ms p50 | 485.741 | 569.312 | 83.5711 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.679372 | 0.000801 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | glm-flash-east | 329 |
| (rejected) | glm-flash-east | 10 |

### New rejections

None.

Affected apps: paper-clinic

### Capacity

Peak load: current peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 132.5% / avg 94.1%, over 100% in 52.9% of 17 buckets (glm-flash-east, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 329, proposed 0. Active buckets (an arrival or a request in service): current 17 of 17, proposed 0 of 17.

Service time: current mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| max_concurrency | proposed | 2 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |
| requests_per_minute | current | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | proposed | 40 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |
| tokens_per_minute | current | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |
| tokens_per_minute | proposed | 80000 | 0.0% | 0.0% | 0.0% | 0 of 17 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.28065 | 0.168295 | 0.168295 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 31 | 19.3529 | 19.3529 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 32058 | 15254.8 | 15254.8 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 2208 | 1407.47 | 1407.47 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### glm-flash-east

Requests: current 301, proposed 640. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 602.096 ms, longest 1377 ms over 301 row(s) (228 recorded, 73 endpoint_mean); proposed mean 602.045 ms, longest 1377 ms over 640 row(s) (228 recorded, 412 endpoint_mean).

**Note:** 2 row(s) routed here in the current arm have no backend token count; their token demand is RouteForge's own estimate.

**Note:** 12 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 13.8% | 8.9% | 8.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| max_concurrency | proposed | 2 | 26.8% | 18.9% | 18.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | current | 40 | 67.5% | 44.3% | 44.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | proposed | 40 | 132.5% | 94.1% | 94.1% | 9 of 17 | 65.2% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | current | 80000 | 72.3% | 40.3% | 40.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | proposed | 80000 | 101.0% | 64.4% | 64.4% | 1 of 17 | 7.0% | 2026-10-08T23:43:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.2754 | 0.177677 | 0.177677 |
| concurrency | proposed | 0.536267 | 0.377754 | 0.377754 |
| requests_per_minute | current | 27 | 17.7059 | 17.7059 |
| requests_per_minute | proposed | 53 | 37.6471 | 37.6471 |
| tokens_per_minute | current | 57814 | 32201.2 | 32201.2 |
| tokens_per_minute | proposed | 80767 | 51491.8 | 51491.8 |
| output_tokens_per_minute | current | 1968 | 1297.88 | 1297.88 |
| output_tokens_per_minute | proposed | 3904 | 2750.53 | 2750.53 |

#### glm-flash-econ

Requests: current 252, proposed 252. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded); proposed mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| max_concurrency | proposed | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | proposed | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| tokens_per_minute | current | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |
| tokens_per_minute | proposed | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.185717 | 0.115285 | 0.115285 |
| concurrency | proposed | 0.185717 | 0.115285 | 0.115285 |
| requests_per_minute | current | 24 | 14.8235 | 14.8235 |
| requests_per_minute | proposed | 24 | 14.8235 | 14.8235 |
| tokens_per_minute | current | 21262 | 7264.29 | 7264.29 |
| tokens_per_minute | proposed | 21262 | 7264.29 | 7264.29 |
| output_tokens_per_minute | current | 1536 | 1039.12 | 1039.12 |
| output_tokens_per_minute | proposed | 1536 | 1039.12 | 1039.12 |

## Scenario: raise-request-budget

requests: 900 &middot; route_changed: 4 &middot; newly_rejected: 0 &middot; newly_served: 18

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 7.45609 | 1.45476 |
| cost_usd (both routed) | 6.00133 | 6.15085 | 0.149515 |
| latency_ms p50 | 485.741 | 485.741 | 0 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.681111 | 0.00254 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 12 |
| (rejected) | glm-flash-east | 6 |
| glm-flash-econ | glm-flash-east | 4 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

### Capacity

Peak load: current peak 77.5% / avg 48.4%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 77.5% / avg 50.1%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 329, proposed 341. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 521.766 ms, longest 1541 ms over 329 row(s) (329 recorded); proposed mean 521.985 ms, longest 1541 ms over 341 row(s) (329 recorded, 12 endpoint_mean).

**Note:** 12 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 14.0% | 8.4% | 8.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| max_concurrency | proposed | 2 | 14.0% | 8.7% | 8.7% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | current | 40 | 77.5% | 48.4% | 48.4% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| requests_per_minute | proposed | 40 | 77.5% | 50.1% | 50.1% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |
| tokens_per_minute | current | 80000 | 40.1% | 19.1% | 19.1% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |
| tokens_per_minute | proposed | 80000 | 49.9% | 26.1% | 26.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.28065 | 0.168295 | 0.168295 |
| concurrency | proposed | 0.28065 | 0.174507 | 0.174507 |
| requests_per_minute | current | 31 | 19.3529 | 19.3529 |
| requests_per_minute | proposed | 31 | 20.0588 | 20.0588 |
| tokens_per_minute | current | 32058 | 15254.8 | 15254.8 |
| tokens_per_minute | proposed | 39885 | 20854.5 | 20854.5 |
| output_tokens_per_minute | current | 2208 | 1407.47 | 1407.47 |
| output_tokens_per_minute | proposed | 2208 | 1458.29 | 1458.29 |

#### glm-flash-east

Requests: current 301, proposed 311. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 602.096 ms, longest 1377 ms over 301 row(s) (228 recorded, 73 endpoint_mean); proposed mean 602.093 ms, longest 1377 ms over 311 row(s) (228 recorded, 83 endpoint_mean).

**Note:** 2 row(s) routed here in the current arm have no backend token count; their token demand is RouteForge's own estimate.

**Note:** 8 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 13.8% | 8.9% | 8.9% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| max_concurrency | proposed | 2 | 13.8% | 9.2% | 9.2% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | current | 40 | 67.5% | 44.3% | 44.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| requests_per_minute | proposed | 40 | 67.5% | 45.7% | 45.7% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | current | 80000 | 72.3% | 40.3% | 40.3% | 0 of 17 | 0.0% | 2026-10-08T23:47:00+00:00 |
| tokens_per_minute | proposed | 80000 | 76.6% | 46.8% | 46.8% | 0 of 17 | 0.0% | 2026-10-08T23:43:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.2754 | 0.177677 | 0.177677 |
| concurrency | proposed | 0.2754 | 0.183579 | 0.183579 |
| requests_per_minute | current | 27 | 17.7059 | 17.7059 |
| requests_per_minute | proposed | 27 | 18.2941 | 18.2941 |
| tokens_per_minute | current | 57814 | 32201.2 | 32201.2 |
| tokens_per_minute | proposed | 61245 | 37423.8 | 37423.8 |
| output_tokens_per_minute | current | 1968 | 1297.88 | 1297.88 |
| output_tokens_per_minute | proposed | 1984 | 1343.06 | 1343.06 |

#### glm-flash-econ

Requests: current 252, proposed 248. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 466.631 ms, longest 1375 ms over 252 row(s) (252 recorded); proposed mean 462.29 ms, longest 1375 ms over 248 row(s) (248 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 9.3% | 5.8% | 5.8% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| max_concurrency | proposed | 2 | 9.3% | 5.6% | 5.6% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 37.1% | 37.1% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| requests_per_minute | proposed | 40 | 60.0% | 36.5% | 36.5% | 0 of 17 | 0.0% | 2026-10-08T23:48:00+00:00 |
| tokens_per_minute | current | 80000 | 26.6% | 9.1% | 9.1% | 0 of 17 | 0.0% | 2026-10-08T23:34:00+00:00 |
| tokens_per_minute | proposed | 80000 | 12.7% | 6.9% | 6.9% | 0 of 17 | 0.0% | 2026-10-08T23:38:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.185717 | 0.115285 | 0.115285 |
| concurrency | proposed | 0.185717 | 0.1124 | 0.1124 |
| requests_per_minute | current | 24 | 14.8235 | 14.8235 |
| requests_per_minute | proposed | 24 | 14.5882 | 14.5882 |
| tokens_per_minute | current | 21262 | 7264.29 | 7264.29 |
| tokens_per_minute | proposed | 10128 | 5505.29 | 5505.29 |
| output_tokens_per_minute | current | 1536 | 1039.12 | 1039.12 |
| output_tokens_per_minute | proposed | 1536 | 1025 | 1025 |

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
