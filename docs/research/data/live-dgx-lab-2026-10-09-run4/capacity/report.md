> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: DGX lab live run 4, 2026-10-09 (three paper apps; two on-prem models, two cloud models, cloud path working)

900 rows simulated, 5 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 100.0%** over 878 compared rows.

Per app: paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Outcome agreement: 100.0%** over 900 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Note:** 22 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps | peak / avg load (proposed) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.797267→0.797267 | 0 | none | peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |
| all-apps-onprem-only | n/a | 3.7391 | 178 | 0 | 0 | 0 | -9394.52 | -0.033599 | 0.797267→1 | 0 | paper-claims, paper-dev | peak 90.9% / avg 41.1%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, tokens_per_minute, 60 s buckets) |
| all-apps-no-retention | n/a | 3.7391 | 178 | 0 | 0 | 0 | -9394.52 | -0.033599 | 0.797267→1 | 0 | paper-claims, paper-dev | peak 90.9% / avg 41.1%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, tokens_per_minute, 60 s buckets) |
| clinic-may-use-cloud | n/a | -0.890274 | 63 | 0 | 0 | 0 | 0 | 0.01623 | 0.797267→0.725513 | 0 | paper-clinic | peak 55.0% / avg 21.9%, over 100% in 0.0% of 31 buckets (openrouter-frontier, requests_per_minute, 60 s buckets) |
| raise-request-budget | n/a | 0.760635 | 15 | 0 | 22 | 0 | 0 | 0.005768 | 0.797267→0.802222 | 0 | paper-claims, paper-clinic, paper-dev | peak 77.4% / avg 36.7%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, tokens_per_minute, 60 s buckets) |

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

Buckets of 60 s. The window is 31 bucket(s), from the first estimated arrival to the end of the last recorded request (2026-10-09T14:44:00+00:00 to 2026-10-09T15:14:00+00:00); a request arrived in 31 of them; 900 simulated row(s).

`avg (window)` is over every bucket of the window, empty ones included: the time-average. `avg (active)` is over the buckets in which the endpoint had an arrival or a request in service; that count is printed with it.

Current arm peak load (the same for every scenario): peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

## Scenario: no-change

requests: 900 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.01583 | 7.01583 | 0 |
| cost_usd (both routed) | 7.01583 | 7.01583 | 0 |
| latency_ms p50 | 2681.28 | 2681.28 | 0 |
| latency_ms p95 | 12075.8 | 12075.8 | 0 |
| quality_mean | 0.68451 | 0.68451 | 0 |
| onprem_share | 0.797267 | 0.797267 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

### Capacity

Peak load: current peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 376, proposed 376. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2880.53 ms, longest 7147 ms over 376 row(s) (376 recorded); proposed mean 2880.53 ms, longest 7147 ms over 376 row(s) (376 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 49.1% | 29.1% | 29.1% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| max_concurrency | proposed | 2 | 49.1% | 29.1% | 29.1% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| requests_per_minute | current | 40 | 50.0% | 30.3% | 30.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 40 | 50.0% | 30.3% | 30.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| tokens_per_minute | current | 80000 | 49.0% | 25.4% | 25.4% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | proposed | 80000 | 49.0% | 25.4% | 25.4% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.9818 | 0.582301 | 0.582301 |
| concurrency | proposed | 0.9818 | 0.582301 | 0.582301 |
| requests_per_minute | current | 20 | 12.129 | 12.129 |
| requests_per_minute | proposed | 20 | 12.129 | 12.129 |
| tokens_per_minute | current | 39207 | 20328.9 | 20328.9 |
| tokens_per_minute | proposed | 39207 | 20328.9 | 20328.9 |
| output_tokens_per_minute | current | 11364 | 7063.61 | 7063.61 |
| output_tokens_per_minute | proposed | 11364 | 7063.61 | 7063.61 |

#### openrouter-frontier

Requests: current 94, proposed 94. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean); proposed mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |
| requests_per_minute | proposed | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 2.87308 | 0.944918 | 0.944918 |
| concurrency | proposed | 2.87308 | 0.944918 | 0.944918 |
| requests_per_minute | current | 9 | 3.03226 | 3.03226 |
| requests_per_minute | proposed | 9 | 3.03226 | 3.03226 |
| tokens_per_minute | current | 31597 | 12164.6 | 12164.6 |
| tokens_per_minute | proposed | 31597 | 12164.6 | 12164.6 |
| output_tokens_per_minute | current | 3710 | 1360.48 | 1360.48 |
| output_tokens_per_minute | proposed | 3710 | 1360.48 | 1360.48 |

#### openrouter-mid

Requests: current 84, proposed 84. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded); proposed mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.751267 | 0.261644 | 0.261644 |
| concurrency | proposed | 0.751267 | 0.261644 | 0.261644 |
| requests_per_minute | current | 6 | 2.70968 | 2.70968 |
| requests_per_minute | proposed | 6 | 2.70968 | 2.70968 |
| tokens_per_minute | current | 15590 | 4897.03 | 4897.03 |
| tokens_per_minute | proposed | 15590 | 4897.03 | 4897.03 |
| output_tokens_per_minute | current | 3685 | 1300.16 | 1300.16 |
| output_tokens_per_minute | proposed | 3685 | 1300.16 | 1300.16 |

#### qwen3.6-35b

Requests: current 324, proposed 324. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2288.52 ms, longest 5304 ms over 324 row(s) (324 recorded); proposed mean 2288.52 ms, longest 5304 ms over 324 row(s) (324 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 32.2% | 19.9% | 19.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| max_concurrency | proposed | 2 | 32.2% | 19.9% | 19.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| requests_per_minute | current | 40 | 45.0% | 26.1% | 26.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| requests_per_minute | proposed | 40 | 45.0% | 26.1% | 26.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | current | 80000 | 36.4% | 17.2% | 17.2% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | proposed | 80000 | 36.4% | 17.2% | 17.2% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.644717 | 0.398646 | 0.398646 |
| concurrency | proposed | 0.644717 | 0.398646 | 0.398646 |
| requests_per_minute | current | 18 | 10.4516 | 10.4516 |
| requests_per_minute | proposed | 18 | 10.4516 | 10.4516 |
| tokens_per_minute | current | 29097 | 13759.9 | 13759.9 |
| tokens_per_minute | proposed | 29097 | 13759.9 | 13759.9 |
| output_tokens_per_minute | current | 13592 | 8293.81 | 8293.81 |
| output_tokens_per_minute | proposed | 13592 | 8293.81 | 8293.81 |

## Scenario: all-apps-onprem-only

requests: 900 &middot; route_changed: 178 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.01583 | 10.7549 | 3.7391 |
| cost_usd (both routed) | 7.01583 | 10.7549 | 3.7391 |
| latency_ms p50 | 2681.28 | 2681.28 | 0 |
| latency_ms p95 | 12075.8 | 2681.28 | -9394.52 |
| quality_mean | 0.68451 | 0.650911 | -0.033599 |
| onprem_share | 0.797267 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 84 |
| openrouter-frontier | glm-5.3-flash | 70 |
| openrouter-frontier | qwen3.6-35b | 24 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

### Capacity

Peak load: current peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 90.9% / avg 41.1%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, tokens_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 376, proposed 530. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2880.53 ms, longest 7147 ms over 376 row(s) (376 recorded); proposed mean 2880.67 ms, longest 7147 ms over 530 row(s) (376 recorded, 154 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 49.1% | 29.1% | 29.1% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| max_concurrency | proposed | 2 | 70.7% | 41.0% | 41.0% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| requests_per_minute | current | 40 | 50.0% | 30.3% | 30.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 40 | 72.5% | 42.7% | 42.7% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | current | 80000 | 49.0% | 25.4% | 25.4% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | proposed | 80000 | 90.9% | 41.1% | 41.1% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.9818 | 0.582301 | 0.582301 |
| concurrency | proposed | 1.41497 | 0.820835 | 0.820835 |
| requests_per_minute | current | 20 | 12.129 | 12.129 |
| requests_per_minute | proposed | 29 | 17.0968 | 17.0968 |
| tokens_per_minute | current | 39207 | 20328.9 | 20328.9 |
| tokens_per_minute | proposed | 72715 | 32864.8 | 32864.8 |
| output_tokens_per_minute | current | 11364 | 7063.61 | 7063.61 |
| output_tokens_per_minute | proposed | 17475 | 9458.61 | 9458.61 |

#### openrouter-frontier

Requests: current 94, proposed 0. Active buckets (an arrival or a request in service): current 31 of 31, proposed 0 of 31.

Service time: current mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 2.87308 | 0.944918 | 0.944918 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 9 | 3.03226 | 3.03226 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 31597 | 12164.6 | 12164.6 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3710 | 1360.48 | 1360.48 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### openrouter-mid

Requests: current 84, proposed 0. Active buckets (an arrival or a request in service): current 31 of 31, proposed 0 of 31.

Service time: current mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.751267 | 0.261644 | 0.261644 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 6 | 2.70968 | 2.70968 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 15590 | 4897.03 | 4897.03 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3685 | 1300.16 | 1300.16 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### qwen3.6-35b

Requests: current 324, proposed 348. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2288.52 ms, longest 5304 ms over 324 row(s) (324 recorded); proposed mean 2288.56 ms, longest 5304 ms over 348 row(s) (324 recorded, 24 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 32.2% | 19.9% | 19.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| max_concurrency | proposed | 2 | 34.0% | 21.4% | 21.4% | 0 of 31 | 0.0% | 2026-10-09T14:48:00+00:00 |
| requests_per_minute | current | 40 | 45.0% | 26.1% | 26.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| requests_per_minute | proposed | 40 | 45.0% | 28.1% | 28.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | current | 80000 | 36.4% | 17.2% | 17.2% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | proposed | 80000 | 51.4% | 22.9% | 22.9% | 0 of 31 | 0.0% | 2026-10-09T14:45:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.644717 | 0.398646 | 0.398646 |
| concurrency | proposed | 0.679517 | 0.428182 | 0.428182 |
| requests_per_minute | current | 18 | 10.4516 | 10.4516 |
| requests_per_minute | proposed | 18 | 11.2258 | 11.2258 |
| tokens_per_minute | current | 29097 | 13759.9 | 13759.9 |
| tokens_per_minute | proposed | 41105 | 18285.7 | 18285.7 |
| output_tokens_per_minute | current | 13592 | 8293.81 | 8293.81 |
| output_tokens_per_minute | proposed | 13804 | 8559.45 | 8559.45 |

## Scenario: all-apps-no-retention

requests: 900 &middot; route_changed: 178 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.01583 | 10.7549 | 3.7391 |
| cost_usd (both routed) | 7.01583 | 10.7549 | 3.7391 |
| latency_ms p50 | 2681.28 | 2681.28 | 0 |
| latency_ms p95 | 12075.8 | 2681.28 | -9394.52 |
| quality_mean | 0.68451 | 0.650911 | -0.033599 |
| onprem_share | 0.797267 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 84 |
| openrouter-frontier | glm-5.3-flash | 70 |
| openrouter-frontier | qwen3.6-35b | 24 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

### Capacity

Peak load: current peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 90.9% / avg 41.1%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, tokens_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 376, proposed 530. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2880.53 ms, longest 7147 ms over 376 row(s) (376 recorded); proposed mean 2880.67 ms, longest 7147 ms over 530 row(s) (376 recorded, 154 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 49.1% | 29.1% | 29.1% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| max_concurrency | proposed | 2 | 70.7% | 41.0% | 41.0% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| requests_per_minute | current | 40 | 50.0% | 30.3% | 30.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 40 | 72.5% | 42.7% | 42.7% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | current | 80000 | 49.0% | 25.4% | 25.4% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | proposed | 80000 | 90.9% | 41.1% | 41.1% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.9818 | 0.582301 | 0.582301 |
| concurrency | proposed | 1.41497 | 0.820835 | 0.820835 |
| requests_per_minute | current | 20 | 12.129 | 12.129 |
| requests_per_minute | proposed | 29 | 17.0968 | 17.0968 |
| tokens_per_minute | current | 39207 | 20328.9 | 20328.9 |
| tokens_per_minute | proposed | 72715 | 32864.8 | 32864.8 |
| output_tokens_per_minute | current | 11364 | 7063.61 | 7063.61 |
| output_tokens_per_minute | proposed | 17475 | 9458.61 | 9458.61 |

#### openrouter-frontier

Requests: current 94, proposed 0. Active buckets (an arrival or a request in service): current 31 of 31, proposed 0 of 31.

Service time: current mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 2.87308 | 0.944918 | 0.944918 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 9 | 3.03226 | 3.03226 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 31597 | 12164.6 | 12164.6 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3710 | 1360.48 | 1360.48 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### openrouter-mid

Requests: current 84, proposed 0. Active buckets (an arrival or a request in service): current 31 of 31, proposed 0 of 31.

Service time: current mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.751267 | 0.261644 | 0.261644 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 6 | 2.70968 | 2.70968 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 15590 | 4897.03 | 4897.03 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3685 | 1300.16 | 1300.16 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### qwen3.6-35b

Requests: current 324, proposed 348. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2288.52 ms, longest 5304 ms over 324 row(s) (324 recorded); proposed mean 2288.56 ms, longest 5304 ms over 348 row(s) (324 recorded, 24 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 32.2% | 19.9% | 19.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| max_concurrency | proposed | 2 | 34.0% | 21.4% | 21.4% | 0 of 31 | 0.0% | 2026-10-09T14:48:00+00:00 |
| requests_per_minute | current | 40 | 45.0% | 26.1% | 26.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| requests_per_minute | proposed | 40 | 45.0% | 28.1% | 28.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | current | 80000 | 36.4% | 17.2% | 17.2% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | proposed | 80000 | 51.4% | 22.9% | 22.9% | 0 of 31 | 0.0% | 2026-10-09T14:45:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.644717 | 0.398646 | 0.398646 |
| concurrency | proposed | 0.679517 | 0.428182 | 0.428182 |
| requests_per_minute | current | 18 | 10.4516 | 10.4516 |
| requests_per_minute | proposed | 18 | 11.2258 | 11.2258 |
| tokens_per_minute | current | 29097 | 13759.9 | 13759.9 |
| tokens_per_minute | proposed | 41105 | 18285.7 | 18285.7 |
| output_tokens_per_minute | current | 13592 | 8293.81 | 8293.81 |
| output_tokens_per_minute | proposed | 13804 | 8559.45 | 8559.45 |

## Scenario: clinic-may-use-cloud

requests: 900 &middot; route_changed: 63 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.01583 | 6.12556 | -0.890274 |
| cost_usd (both routed) | 7.01583 | 6.12556 | -0.890274 |
| latency_ms p50 | 2681.28 | 2681.28 | 0 |
| latency_ms p95 | 12075.8 | 12075.8 | 0 |
| quality_mean | 0.68451 | 0.70074 | 0.01623 |
| onprem_share | 0.797267 | 0.725513 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | openrouter-frontier | 27 |
| glm-5.3-flash | openrouter-mid | 21 |
| qwen3.6-35b | openrouter-frontier | 15 |

### New rejections

None.

Affected apps: paper-clinic

### Capacity

Peak load: current peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 55.0% / avg 21.9%, over 100% in 0.0% of 31 buckets (openrouter-frontier, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 376, proposed 328. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2880.53 ms, longest 7147 ms over 376 row(s) (376 recorded); proposed mean 2980.82 ms, longest 7147 ms over 328 row(s) (328 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 49.1% | 29.1% | 29.1% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| max_concurrency | proposed | 2 | 46.4% | 26.3% | 26.3% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| requests_per_minute | current | 40 | 50.0% | 30.3% | 30.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 40 | 47.5% | 26.5% | 26.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| tokens_per_minute | current | 80000 | 49.0% | 25.4% | 25.4% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | proposed | 80000 | 43.7% | 21.7% | 21.7% | 0 of 31 | 0.0% | 2026-10-09T14:51:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.9818 | 0.582301 | 0.582301 |
| concurrency | proposed | 0.927833 | 0.52565 | 0.52565 |
| requests_per_minute | current | 20 | 12.129 | 12.129 |
| requests_per_minute | proposed | 19 | 10.5806 | 10.5806 |
| tokens_per_minute | current | 39207 | 20328.9 | 20328.9 |
| tokens_per_minute | proposed | 34981 | 17320.4 | 17320.4 |
| output_tokens_per_minute | current | 11364 | 7063.61 | 7063.61 |
| output_tokens_per_minute | proposed | 10987 | 6415.13 | 6415.13 |

#### openrouter-frontier

Requests: current 94, proposed 136. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean); proposed mean 18697.2 ms, longest 59856 ms over 136 row(s) (59 recorded, 77 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |
| requests_per_minute | proposed | 20 | 55.0% | 21.9% | 21.9% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 2.87308 | 0.944918 | 0.944918 |
| concurrency | proposed | 3.5914 | 1.36711 | 1.36711 |
| requests_per_minute | current | 9 | 3.03226 | 3.03226 |
| requests_per_minute | proposed | 11 | 4.3871 | 4.3871 |
| tokens_per_minute | current | 31597 | 12164.6 | 12164.6 |
| tokens_per_minute | proposed | 40418 | 17125.7 | 17125.7 |
| output_tokens_per_minute | current | 3710 | 1360.48 | 1360.48 |
| output_tokens_per_minute | proposed | 6373 | 2215.81 | 2215.81 |

#### openrouter-mid

Requests: current 84, proposed 105. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded); proposed mean 5793.63 ms, longest 17160 ms over 105 row(s) (84 recorded, 21 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 20 | 35.0% | 16.9% | 16.9% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.751267 | 0.261644 | 0.261644 |
| concurrency | proposed | 0.751267 | 0.32706 | 0.32706 |
| requests_per_minute | current | 6 | 2.70968 | 2.70968 |
| requests_per_minute | proposed | 7 | 3.3871 | 3.3871 |
| tokens_per_minute | current | 15590 | 4897.03 | 4897.03 |
| tokens_per_minute | proposed | 15916 | 5670.81 | 5670.81 |
| output_tokens_per_minute | current | 3685 | 1300.16 | 1300.16 |
| output_tokens_per_minute | proposed | 4860 | 1562.52 | 1562.52 |

#### qwen3.6-35b

Requests: current 324, proposed 309. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2288.52 ms, longest 5304 ms over 324 row(s) (324 recorded); proposed mean 2262.81 ms, longest 5304 ms over 309 row(s) (309 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 32.2% | 19.9% | 19.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| max_concurrency | proposed | 2 | 31.9% | 18.8% | 18.8% | 0 of 31 | 0.0% | 2026-10-09T14:51:00+00:00 |
| requests_per_minute | current | 40 | 45.0% | 26.1% | 26.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| requests_per_minute | proposed | 40 | 42.5% | 24.9% | 24.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | current | 80000 | 36.4% | 17.2% | 17.2% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | proposed | 80000 | 25.0% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.644717 | 0.398646 | 0.398646 |
| concurrency | proposed | 0.637967 | 0.375918 | 0.375918 |
| requests_per_minute | current | 18 | 10.4516 | 10.4516 |
| requests_per_minute | proposed | 17 | 9.96774 | 9.96774 |
| tokens_per_minute | current | 29097 | 13759.9 | 13759.9 |
| tokens_per_minute | proposed | 20008 | 11033.6 | 11033.6 |
| output_tokens_per_minute | current | 13592 | 8293.81 | 8293.81 |
| output_tokens_per_minute | proposed | 12981 | 7824.61 | 7824.61 |

## Scenario: raise-request-budget

requests: 900 &middot; route_changed: 15 &middot; newly_rejected: 0 &middot; newly_served: 22

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.01583 | 9.72416 | 2.70832 |
| cost_usd (both routed) | 7.01583 | 7.77647 | 0.760635 |
| latency_ms p50 | 2681.28 | 2681.28 | 0 |
| latency_ms p95 | 12075.8 | 12075.8 | 0 |
| quality_mean | 0.68451 | 0.690278 | 0.005768 |
| onprem_share | 0.797267 | 0.802222 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 22 |
| qwen3.6-35b | glm-5.3-flash | 15 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

### Capacity

Peak load: current peak 50.0% / avg 30.3%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 77.4% / avg 36.7%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, tokens_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 376, proposed 413. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2880.53 ms, longest 7147 ms over 376 row(s) (376 recorded); proposed mean 2880.57 ms, longest 7147 ms over 413 row(s) (376 recorded, 37 endpoint_mean).

**Note:** 22 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 49.1% | 29.1% | 29.1% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| max_concurrency | proposed | 2 | 53.9% | 32.0% | 32.0% | 0 of 31 | 0.0% | 2026-10-09T15:03:00+00:00 |
| requests_per_minute | current | 40 | 50.0% | 30.3% | 30.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 40 | 55.0% | 33.3% | 33.3% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| tokens_per_minute | current | 80000 | 49.0% | 25.4% | 25.4% | 0 of 31 | 0.0% | 2026-10-09T14:49:00+00:00 |
| tokens_per_minute | proposed | 80000 | 77.4% | 36.7% | 36.7% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.9818 | 0.582301 | 0.582301 |
| concurrency | proposed | 1.07783 | 0.639611 | 0.639611 |
| requests_per_minute | current | 20 | 12.129 | 12.129 |
| requests_per_minute | proposed | 22 | 13.3226 | 13.3226 |
| tokens_per_minute | current | 39207 | 20328.9 | 20328.9 |
| tokens_per_minute | proposed | 61950 | 29338.1 | 29338.1 |
| output_tokens_per_minute | current | 11364 | 7063.61 | 7063.61 |
| output_tokens_per_minute | proposed | 13633 | 8366.87 | 8366.87 |

#### openrouter-frontier

Requests: current 94, proposed 94. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean); proposed mean 18697.3 ms, longest 59856 ms over 94 row(s) (59 recorded, 35 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |
| requests_per_minute | proposed | 20 | 45.0% | 15.2% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T15:07:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 2.87308 | 0.944918 | 0.944918 |
| concurrency | proposed | 2.87308 | 0.944918 | 0.944918 |
| requests_per_minute | current | 9 | 3.03226 | 3.03226 |
| requests_per_minute | proposed | 9 | 3.03226 | 3.03226 |
| tokens_per_minute | current | 31597 | 12164.6 | 12164.6 |
| tokens_per_minute | proposed | 31597 | 12164.6 | 12164.6 |
| output_tokens_per_minute | current | 3710 | 1360.48 | 1360.48 |
| output_tokens_per_minute | proposed | 3710 | 1360.48 | 1360.48 |

#### openrouter-mid

Requests: current 84, proposed 84. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded); proposed mean 5793.54 ms, longest 17160 ms over 84 row(s) (84 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |
| requests_per_minute | proposed | 20 | 30.0% | 13.5% | 13.5% | 0 of 31 | 0.0% | 2026-10-09T14:52:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.751267 | 0.261644 | 0.261644 |
| concurrency | proposed | 0.751267 | 0.261644 | 0.261644 |
| requests_per_minute | current | 6 | 2.70968 | 2.70968 |
| requests_per_minute | proposed | 6 | 2.70968 | 2.70968 |
| tokens_per_minute | current | 15590 | 4897.03 | 4897.03 |
| tokens_per_minute | proposed | 15590 | 4897.03 | 4897.03 |
| output_tokens_per_minute | current | 3685 | 1300.16 | 1300.16 |
| output_tokens_per_minute | proposed | 3685 | 1300.16 | 1300.16 |

#### qwen3.6-35b

Requests: current 324, proposed 309. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2288.52 ms, longest 5304 ms over 324 row(s) (324 recorded); proposed mean 2262.81 ms, longest 5304 ms over 309 row(s) (309 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 32.2% | 19.9% | 19.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| max_concurrency | proposed | 2 | 31.9% | 18.8% | 18.8% | 0 of 31 | 0.0% | 2026-10-09T14:51:00+00:00 |
| requests_per_minute | current | 40 | 45.0% | 26.1% | 26.1% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| requests_per_minute | proposed | 40 | 42.5% | 24.9% | 24.9% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | current | 80000 | 36.4% | 17.2% | 17.2% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |
| tokens_per_minute | proposed | 80000 | 25.0% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T15:02:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.644717 | 0.398646 | 0.398646 |
| concurrency | proposed | 0.637967 | 0.375918 | 0.375918 |
| requests_per_minute | current | 18 | 10.4516 | 10.4516 |
| requests_per_minute | proposed | 17 | 9.96774 | 9.96774 |
| tokens_per_minute | current | 29097 | 13759.9 | 13759.9 |
| tokens_per_minute | proposed | 20008 | 11033.6 | 11033.6 |
| output_tokens_per_minute | current | 13592 | 8293.81 | 8293.81 |
| output_tokens_per_minute | proposed | 12981 | 7824.61 | 7824.61 |

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
