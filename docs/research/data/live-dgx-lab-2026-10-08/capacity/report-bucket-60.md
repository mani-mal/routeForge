> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: DGX lab capacity check 2026-10-08 (three paper apps, glm-5.3-flash)

912 rows simulated, 3 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 100.0%** over 876 compared rows.

Per app: paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Outcome agreement: 100.0%** over 912 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Note:** 36 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps | peak / avg load (proposed) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1→1 | 0 | none | peak 87.8% / avg 57.3%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |
| volume-x3 | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1→1 | 0 | none | peak 263.3% / avg 171.8%, over 100% in 88.2% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |
| raise-request-budget | n/a | 0 | 0 | 0 | 36 | 0 | 0 | 0 | 1→1 | 0 | paper-claims, paper-clinic, paper-dev | peak 90.0% / avg 59.6%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets) |

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

Buckets of 60 s. The window is 17 bucket(s), from the first estimated arrival to the end of the last recorded request (2026-10-08T22:07:00+00:00 to 2026-10-08T22:23:00+00:00); a request arrived in 17 of them; 912 simulated row(s).

`avg (window)` is over every bucket of the window, empty ones included: the time-average. `avg (active)` is over the buckets in which the endpoint had an arrival or a request in service; that count is printed with it.

A volume scale counts each recorded request that many times, at its recorded time; it does not model how more traffic would arrive.

Current arm peak load (the same for every scenario): peak 87.8% / avg 57.3%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

## Scenario: no-change

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 8.4686 | 0 |
| cost_usd (both routed) | 8.4686 | 8.4686 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

### Capacity

Peak load: current peak 87.8% / avg 57.3%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 87.8% / avg 57.3%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 876, proposed 876. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded); proposed mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 37.9% | 25.2% | 25.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| max_concurrency | proposed | 2 | 37.9% | 25.2% | 25.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| requests_per_minute | current | 90 | 87.8% | 57.3% | 57.3% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| requests_per_minute | proposed | 90 | 87.8% | 57.3% | 57.3% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| tokens_per_minute | current | 150000 | 53.9% | 33.2% | 33.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| tokens_per_minute | proposed | 150000 | 53.9% | 33.2% | 33.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.757483 | 0.503834 | 0.503834 |
| concurrency | proposed | 0.757483 | 0.503834 | 0.503834 |
| requests_per_minute | current | 79 | 51.5294 | 51.5294 |
| requests_per_minute | proposed | 79 | 51.5294 | 51.5294 |
| tokens_per_minute | current | 80881 | 49815.3 | 49815.3 |
| tokens_per_minute | proposed | 80881 | 49815.3 | 49815.3 |
| output_tokens_per_minute | current | 5834 | 3783.41 | 3783.41 |
| output_tokens_per_minute | proposed | 5834 | 3783.41 | 3783.41 |

## Scenario: volume-x3

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 8.4686 | 0 |
| cost_usd (both routed) | 8.4686 | 8.4686 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

### Capacity

Volume scale assumed by the proposal (proposed arm only, multiplying any stated for both arms): *=3.

Peak load: current peak 87.8% / avg 57.3%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 263.3% / avg 171.8%, over 100% in 88.2% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 876, proposed 2628. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded); proposed mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 37.9% | 25.2% | 25.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| max_concurrency | proposed | 2 | 113.6% | 75.6% | 75.6% | 2 of 17 | 10.3% | 2026-10-08T22:15:00+00:00 |
| requests_per_minute | current | 90 | 87.8% | 57.3% | 57.3% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| requests_per_minute | proposed | 90 | 263.3% | 171.8% | 171.8% | 15 of 17 | 96.7% | 2026-10-08T22:15:00+00:00 |
| tokens_per_minute | current | 150000 | 53.9% | 33.2% | 33.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| tokens_per_minute | proposed | 150000 | 161.8% | 99.6% | 99.6% | 10 of 17 | 67.1% | 2026-10-08T22:15:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.757483 | 0.503834 | 0.503834 |
| concurrency | proposed | 2.27245 | 1.5115 | 1.5115 |
| requests_per_minute | current | 79 | 51.5294 | 51.5294 |
| requests_per_minute | proposed | 237 | 154.588 | 154.588 |
| tokens_per_minute | current | 80881 | 49815.3 | 49815.3 |
| tokens_per_minute | proposed | 242643 | 149446 | 149446 |
| output_tokens_per_minute | current | 5834 | 3783.41 | 3783.41 |
| output_tokens_per_minute | proposed | 17502 | 11350.2 | 11350.2 |

## Scenario: raise-request-budget

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 36

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 11.624 | 3.15544 |
| cost_usd (both routed) | 8.4686 | 8.4686 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 36 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

### Capacity

Peak load: current peak 87.8% / avg 57.3%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets); proposed peak 90.0% / avg 59.6%, over 100% in 0.0% of 17 buckets (glm-5.3-flash, requests_per_minute, 60 s buckets).

#### glm-5.3-flash

Requests: current 876, proposed 912. Active buckets (an arrival or a request in service): current 17 of 17, proposed 17 of 17.

Service time: current mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded); proposed mean 586.67 ms, longest 8686 ms over 912 row(s) (876 recorded, 36 endpoint_mean).

**Note:** 36 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 37.9% | 25.2% | 25.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| max_concurrency | proposed | 2 | 38.9% | 26.2% | 26.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| requests_per_minute | current | 90 | 87.8% | 57.3% | 57.3% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| requests_per_minute | proposed | 90 | 90.0% | 59.6% | 59.6% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| tokens_per_minute | current | 150000 | 53.9% | 33.2% | 33.2% | 0 of 17 | 0.0% | 2026-10-08T22:15:00+00:00 |
| tokens_per_minute | proposed | 150000 | 71.3% | 45.6% | 45.6% | 0 of 17 | 0.0% | 2026-10-08T22:14:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.757483 | 0.503834 | 0.503834 |
| concurrency | proposed | 0.77705 | 0.524552 | 0.524552 |
| requests_per_minute | current | 79 | 51.5294 | 51.5294 |
| requests_per_minute | proposed | 81 | 53.6471 | 53.6471 |
| tokens_per_minute | current | 80881 | 49815.3 | 49815.3 |
| tokens_per_minute | proposed | 106884 | 68376.7 | 68376.7 |
| output_tokens_per_minute | current | 5834 | 3783.41 | 3783.41 |
| output_tokens_per_minute | proposed | 5930 | 3937.76 | 3937.76 |

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
