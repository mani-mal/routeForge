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
| no-change | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1→1 | 0 | none | peak 212.1% / avg 27.0%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets) |
| volume-x3 | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1→1 | 0 | none | peak 636.4% / avg 81.1%, over 100% in 27.4% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets) |
| raise-request-budget | n/a | 0 | 0 | 0 | 36 | 0 | 0 | 0 | 1→1 | 0 | paper-claims, paper-clinic, paper-dev | peak 215.1% / avg 28.2%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets) |

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

Buckets of 10 s. The window is 95 bucket(s), from the first estimated arrival to the end of the last recorded request (2026-10-08T22:07:40+00:00 to 2026-10-08T22:23:20+00:00); a request arrived in 95 of them; 912 simulated row(s).

`avg (window)` is over every bucket of the window, empty ones included: the time-average. `avg (active)` is over the buckets in which the endpoint had an arrival or a request in service; that count is printed with it.

A volume scale counts each recorded request that many times, at its recorded time; it does not model how more traffic would arrive.

Current arm peak load (the same for every scenario): peak 212.1% / avg 27.0%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets).

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

Peak load: current peak 212.1% / avg 27.0%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets); proposed peak 212.1% / avg 27.0%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets).

#### glm-5.3-flash

Requests: current 876, proposed 876. Active buckets (an arrival or a request in service): current 95 of 95, proposed 95 of 95.

Service time: current mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded); proposed mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 212.1% | 27.0% | 27.0% | 1 of 95 | 0.8% | 2026-10-08T22:07:50+00:00 |
| max_concurrency | proposed | 2 | 212.1% | 27.0% | 27.0% | 1 of 95 | 0.8% | 2026-10-08T22:07:50+00:00 |
| requests_per_minute | current | 90 | 120.0% | 61.5% | 61.5% | 5 of 95 | 9.5% | 2026-10-08T22:18:50+00:00 |
| requests_per_minute | proposed | 90 | 120.0% | 61.5% | 61.5% | 5 of 95 | 9.5% | 2026-10-08T22:18:50+00:00 |
| tokens_per_minute | current | 150000 | 85.8% | 35.7% | 35.7% | 0 of 95 | 0.0% | 2026-10-08T22:14:00+00:00 |
| tokens_per_minute | proposed | 150000 | 85.8% | 35.7% | 35.7% | 0 of 95 | 0.0% | 2026-10-08T22:14:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 4.2425 | 0.540959 | 0.540959 |
| concurrency | proposed | 4.2425 | 0.540959 | 0.540959 |
| requests_per_minute | current | 108 | 55.3263 | 55.3263 |
| requests_per_minute | proposed | 108 | 55.3263 | 55.3263 |
| tokens_per_minute | current | 128724 | 53485.9 | 53485.9 |
| tokens_per_minute | proposed | 128724 | 53485.9 | 53485.9 |
| output_tokens_per_minute | current | 8064 | 4062.19 | 4062.19 |
| output_tokens_per_minute | proposed | 8064 | 4062.19 | 4062.19 |

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

Peak load: current peak 212.1% / avg 27.0%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets); proposed peak 636.4% / avg 81.1%, over 100% in 27.4% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets).

#### glm-5.3-flash

Requests: current 876, proposed 2628. Active buckets (an arrival or a request in service): current 95 of 95, proposed 95 of 95.

Service time: current mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded); proposed mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 212.1% | 27.0% | 27.0% | 1 of 95 | 0.8% | 2026-10-08T22:07:50+00:00 |
| max_concurrency | proposed | 2 | 636.4% | 81.1% | 81.1% | 26 of 95 | 38.5% | 2026-10-08T22:07:50+00:00 |
| requests_per_minute | current | 90 | 120.0% | 61.5% | 61.5% | 5 of 95 | 9.5% | 2026-10-08T22:18:50+00:00 |
| requests_per_minute | proposed | 90 | 360.0% | 184.4% | 184.4% | 82 of 95 | 93.7% | 2026-10-08T22:18:50+00:00 |
| tokens_per_minute | current | 150000 | 85.8% | 35.7% | 35.7% | 0 of 95 | 0.0% | 2026-10-08T22:14:00+00:00 |
| tokens_per_minute | proposed | 150000 | 257.4% | 107.0% | 107.0% | 46 of 95 | 57.9% | 2026-10-08T22:14:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 4.2425 | 0.540959 | 0.540959 |
| concurrency | proposed | 12.7275 | 1.62288 | 1.62288 |
| requests_per_minute | current | 108 | 55.3263 | 55.3263 |
| requests_per_minute | proposed | 324 | 165.979 | 165.979 |
| tokens_per_minute | current | 128724 | 53485.9 | 53485.9 |
| tokens_per_minute | proposed | 386172 | 160458 | 160458 |
| output_tokens_per_minute | current | 8064 | 4062.19 | 4062.19 |
| output_tokens_per_minute | proposed | 24192 | 12186.6 | 12186.6 |

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

Peak load: current peak 212.1% / avg 27.0%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets); proposed peak 215.1% / avg 28.2%, over 100% in 1.1% of 95 buckets (glm-5.3-flash, max_concurrency, 10 s buckets).

#### glm-5.3-flash

Requests: current 876, proposed 912. Active buckets (an arrival or a request in service): current 95 of 95, proposed 95 of 95.

Service time: current mean 586.656 ms, longest 8686 ms over 876 row(s) (876 recorded); proposed mean 586.67 ms, longest 8686 ms over 912 row(s) (876 recorded, 36 endpoint_mean).

**Note:** 36 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 212.1% | 27.0% | 27.0% | 1 of 95 | 0.8% | 2026-10-08T22:07:50+00:00 |
| max_concurrency | proposed | 2 | 215.1% | 28.2% | 28.2% | 1 of 95 | 0.9% | 2026-10-08T22:07:50+00:00 |
| requests_per_minute | current | 90 | 120.0% | 61.5% | 61.5% | 5 of 95 | 9.5% | 2026-10-08T22:18:50+00:00 |
| requests_per_minute | proposed | 90 | 126.7% | 64.0% | 64.0% | 5 of 95 | 9.3% | 2026-10-08T22:18:50+00:00 |
| tokens_per_minute | current | 150000 | 85.8% | 35.7% | 35.7% | 0 of 95 | 0.0% | 2026-10-08T22:14:00+00:00 |
| tokens_per_minute | proposed | 150000 | 152.8% | 48.9% | 48.9% | 8 of 95 | 11.7% | 2026-10-08T22:12:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 4.2425 | 0.540959 | 0.540959 |
| concurrency | proposed | 4.3012 | 0.563203 | 0.563203 |
| requests_per_minute | current | 108 | 55.3263 | 55.3263 |
| requests_per_minute | proposed | 114 | 57.6 | 57.6 |
| tokens_per_minute | current | 128724 | 53485.9 | 53485.9 |
| tokens_per_minute | proposed | 229212 | 73415 | 73415 |
| output_tokens_per_minute | current | 8064 | 4062.19 | 4062.19 |
| output_tokens_per_minute | proposed | 8160 | 4227.92 | 4227.92 |

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
