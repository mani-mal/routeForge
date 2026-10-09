> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: DGX lab live run 3, 2026-10-09 (three paper apps; two on-prem models, two cloud models; 899 rows)

899 rows simulated, 5 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 91.9%** over 878 compared rows.

Per app: paper-claims 87.8% ⚠ · paper-clinic 100.0% · paper-dev 87.0% ⚠

**Outcome agreement: 100.0%** over 899 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Note:** 21 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps | peak / avg load (proposed) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.794989→0.794989 | 0 | none | peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets) |
| all-apps-onprem-only | n/a | 3.12205 | 180 | 0 | 0 | 0 | -15205.8 | -0.030182 | 0.794989→1 | 0 | paper-claims, paper-dev | peak 82.6% / avg 45.9%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets) |
| all-apps-no-retention | n/a | 3.12205 | 180 | 0 | 0 | 0 | -15205.8 | -0.030182 | 0.794989→1 | 0 | paper-claims, paper-dev | peak 82.6% / avg 45.9%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets) |
| clinic-may-use-cloud | n/a | -0.761067 | 59 | 0 | 0 | 0 | 0 | 0.010251 | 0.794989→0.72779 | 0 | paper-clinic | peak 58.8% / avg 17.6%, over 100% in 0.0% of 31 buckets (qwen3.6-35b, max_concurrency, 60 s buckets) |
| raise-request-budget | n/a | 0.221706 | 6 | 0 | 21 | 0 | 0 | 0.003005 | 0.794989→0.799778 | 0 | paper-claims, paper-clinic, paper-dev | peak 63.1% / avg 35.4%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets) |

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

Buckets of 60 s. The window is 31 bucket(s), from the first estimated arrival to the end of the last recorded request (2026-10-09T13:41:00+00:00 to 2026-10-09T14:11:00+00:00); a request arrived in 31 of them; 899 simulated row(s).

`avg (window)` is over every bucket of the window, empty ones included: the time-average. `avg (active)` is over the buckets in which the endpoint had an arrival or a request in service; that count is printed with it.

Current arm peak load (the same for every scenario): peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets).

## Scenario: no-change

requests: 899 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.25879 | 7.25879 | 0 |
| cost_usd (both routed) | 7.25879 | 7.25879 | 0 |
| latency_ms p50 | 2683.83 | 2683.83 | 0 |
| latency_ms p95 | 17889.6 | 17889.6 | 0 |
| quality_mean | 0.692768 | 0.692768 | 0 |
| onprem_share | 0.794989 | 0.794989 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

### Capacity

Peak load: current peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets); proposed peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets).

#### glm-5.3-flash

Requests: current 406, proposed 406. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 3040.85 ms, longest 9145 ms over 406 row(s) (406 recorded); proposed mean 3040.85 ms, longest 9145 ms over 406 row(s) (406 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 63.1% | 33.2% | 33.2% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| max_concurrency | proposed | 2 | 63.1% | 33.2% | 33.2% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 32.7% | 32.7% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 40 | 60.0% | 32.7% | 32.7% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| tokens_per_minute | current | 80000 | 48.2% | 26.8% | 26.8% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |
| tokens_per_minute | proposed | 80000 | 48.2% | 26.8% | 26.8% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.26253 | 0.663756 | 0.663756 |
| concurrency | proposed | 1.26253 | 0.663756 | 0.663756 |
| requests_per_minute | current | 24 | 13.0968 | 13.0968 |
| requests_per_minute | proposed | 24 | 13.0968 | 13.0968 |
| tokens_per_minute | current | 38549 | 21460 | 21460 |
| tokens_per_minute | proposed | 38549 | 21460 | 21460 |
| output_tokens_per_minute | current | 12726 | 7466.58 | 7466.58 |
| output_tokens_per_minute | proposed | 12726 | 7466.58 | 7466.58 |

#### openrouter-frontier

Requests: current 91, proposed 91. Active buckets (an arrival or a request in service): current 30 of 31, proposed 30 of 31.

Service time: current mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean); proposed mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |
| requests_per_minute | proposed | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.7652 | 0.83754 | 0.865458 |
| concurrency | proposed | 1.7652 | 0.83754 | 0.865458 |
| requests_per_minute | current | 6 | 2.93548 | 3.03333 |
| requests_per_minute | proposed | 6 | 2.93548 | 3.03333 |
| tokens_per_minute | current | 24765 | 9651.94 | 9973.67 |
| tokens_per_minute | proposed | 24765 | 9651.94 | 9973.67 |
| output_tokens_per_minute | current | 3796 | 1638.45 | 1693.07 |
| output_tokens_per_minute | proposed | 3796 | 1638.45 | 1693.07 |

#### openrouter-mid

Requests: current 89, proposed 89. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean); proposed mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.6543 | 0.313078 | 0.313078 |
| concurrency | proposed | 0.6543 | 0.313078 | 0.313078 |
| requests_per_minute | current | 6 | 2.87097 | 2.87097 |
| requests_per_minute | proposed | 6 | 2.87097 | 2.87097 |
| tokens_per_minute | current | 9445 | 4350.19 | 4350.19 |
| tokens_per_minute | proposed | 9445 | 4350.19 | 4350.19 |
| output_tokens_per_minute | current | 3195 | 1431.23 | 1431.23 |
| output_tokens_per_minute | proposed | 3195 | 1431.23 | 1431.23 |

#### qwen3.6-35b

Requests: current 292, proposed 292. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2292.3 ms, longest 5261 ms over 292 row(s) (291 recorded, 1 endpoint_mean); proposed mean 2292.3 ms, longest 5261 ms over 292 row(s) (291 recorded, 1 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 60.3% | 18.0% | 18.0% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| max_concurrency | proposed | 2 | 60.3% | 18.0% | 18.0% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | current | 40 | 57.5% | 23.5% | 23.5% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | proposed | 40 | 57.5% | 23.5% | 23.5% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | current | 80000 | 37.1% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | proposed | 80000 | 37.1% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.20657 | 0.359867 | 0.359867 |
| concurrency | proposed | 1.20657 | 0.359867 | 0.359867 |
| requests_per_minute | current | 23 | 9.41935 | 9.41935 |
| requests_per_minute | proposed | 23 | 9.41935 | 9.41935 |
| tokens_per_minute | current | 29709 | 11065.6 | 11065.6 |
| tokens_per_minute | proposed | 29709 | 11065.6 | 11065.6 |
| output_tokens_per_minute | current | 20945 | 7516.71 | 7516.71 |
| output_tokens_per_minute | proposed | 20945 | 7516.71 | 7516.71 |

## Scenario: all-apps-onprem-only

requests: 899 &middot; route_changed: 180 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.25879 | 10.3808 | 3.12205 |
| cost_usd (both routed) | 7.25879 | 10.3808 | 3.12205 |
| latency_ms p50 | 2683.83 | 2683.83 | 0 |
| latency_ms p95 | 17889.6 | 2683.83 | -15205.8 |
| quality_mean | 0.692768 | 0.662585 | -0.030182 |
| onprem_share | 0.794989 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 89 |
| openrouter-frontier | glm-5.3-flash | 76 |
| openrouter-frontier | qwen3.6-35b | 15 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

### Capacity

Peak load: current peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets); proposed peak 82.6% / avg 45.9%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets).

#### glm-5.3-flash

Requests: current 406, proposed 571. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 3040.85 ms, longest 9145 ms over 406 row(s) (406 recorded); proposed mean 2990.85 ms, longest 9145 ms over 571 row(s) (477 recorded, 94 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 63.1% | 33.2% | 33.2% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| max_concurrency | proposed | 2 | 82.6% | 45.9% | 45.9% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 32.7% | 32.7% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 40 | 82.5% | 46.0% | 46.0% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| tokens_per_minute | current | 80000 | 48.2% | 26.8% | 26.8% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |
| tokens_per_minute | proposed | 80000 | 63.7% | 40.0% | 40.0% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.26253 | 0.663756 | 0.663756 |
| concurrency | proposed | 1.65237 | 0.918158 | 0.918158 |
| requests_per_minute | current | 24 | 13.0968 | 13.0968 |
| requests_per_minute | proposed | 33 | 18.4194 | 18.4194 |
| tokens_per_minute | current | 38549 | 21460 | 21460 |
| tokens_per_minute | proposed | 50964 | 32037.5 | 32037.5 |
| output_tokens_per_minute | current | 12726 | 7466.58 | 7466.58 |
| output_tokens_per_minute | proposed | 15738 | 10040 | 10040 |

#### openrouter-frontier

Requests: current 91, proposed 0. Active buckets (an arrival or a request in service): current 30 of 31, proposed 0 of 31.

Service time: current mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.7652 | 0.83754 | 0.865458 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 6 | 2.93548 | 3.03333 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 24765 | 9651.94 | 9973.67 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3796 | 1638.45 | 1693.07 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### openrouter-mid

Requests: current 89, proposed 0. Active buckets (an arrival or a request in service): current 31 of 31, proposed 0 of 31.

Service time: current mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.6543 | 0.313078 | 0.313078 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 6 | 2.87097 | 2.87097 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 9445 | 4350.19 | 4350.19 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3195 | 1431.23 | 1431.23 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### qwen3.6-35b

Requests: current 292, proposed 307. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2292.3 ms, longest 5261 ms over 292 row(s) (291 recorded, 1 endpoint_mean); proposed mean 2292.29 ms, longest 5261 ms over 307 row(s) (291 recorded, 16 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 60.3% | 18.0% | 18.0% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| max_concurrency | proposed | 2 | 60.3% | 18.9% | 18.9% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | current | 40 | 57.5% | 23.5% | 23.5% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | proposed | 40 | 57.5% | 24.8% | 24.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | current | 80000 | 37.1% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | proposed | 80000 | 40.5% | 18.1% | 18.1% | 0 of 31 | 0.0% | 2026-10-09T13:47:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.20657 | 0.359867 | 0.359867 |
| concurrency | proposed | 1.20657 | 0.378351 | 0.378351 |
| requests_per_minute | current | 23 | 9.41935 | 9.41935 |
| requests_per_minute | proposed | 23 | 9.90323 | 9.90323 |
| tokens_per_minute | current | 29709 | 11065.6 | 11065.6 |
| tokens_per_minute | proposed | 32415 | 14490.2 | 14490.2 |
| output_tokens_per_minute | current | 20945 | 7516.71 | 7516.71 |
| output_tokens_per_minute | proposed | 20945 | 8012.97 | 8012.97 |

## Scenario: all-apps-no-retention

requests: 899 &middot; route_changed: 180 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.25879 | 10.3808 | 3.12205 |
| cost_usd (both routed) | 7.25879 | 10.3808 | 3.12205 |
| latency_ms p50 | 2683.83 | 2683.83 | 0 |
| latency_ms p95 | 17889.6 | 2683.83 | -15205.8 |
| quality_mean | 0.692768 | 0.662585 | -0.030182 |
| onprem_share | 0.794989 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 89 |
| openrouter-frontier | glm-5.3-flash | 76 |
| openrouter-frontier | qwen3.6-35b | 15 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

### Capacity

Peak load: current peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets); proposed peak 82.6% / avg 45.9%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets).

#### glm-5.3-flash

Requests: current 406, proposed 571. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 3040.85 ms, longest 9145 ms over 406 row(s) (406 recorded); proposed mean 2990.85 ms, longest 9145 ms over 571 row(s) (477 recorded, 94 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 63.1% | 33.2% | 33.2% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| max_concurrency | proposed | 2 | 82.6% | 45.9% | 45.9% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 32.7% | 32.7% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 40 | 82.5% | 46.0% | 46.0% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| tokens_per_minute | current | 80000 | 48.2% | 26.8% | 26.8% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |
| tokens_per_minute | proposed | 80000 | 63.7% | 40.0% | 40.0% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.26253 | 0.663756 | 0.663756 |
| concurrency | proposed | 1.65237 | 0.918158 | 0.918158 |
| requests_per_minute | current | 24 | 13.0968 | 13.0968 |
| requests_per_minute | proposed | 33 | 18.4194 | 18.4194 |
| tokens_per_minute | current | 38549 | 21460 | 21460 |
| tokens_per_minute | proposed | 50964 | 32037.5 | 32037.5 |
| output_tokens_per_minute | current | 12726 | 7466.58 | 7466.58 |
| output_tokens_per_minute | proposed | 15738 | 10040 | 10040 |

#### openrouter-frontier

Requests: current 91, proposed 0. Active buckets (an arrival or a request in service): current 30 of 31, proposed 0 of 31.

Service time: current mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.7652 | 0.83754 | 0.865458 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 6 | 2.93548 | 3.03333 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 24765 | 9651.94 | 9973.67 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3796 | 1638.45 | 1693.07 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### openrouter-mid

Requests: current 89, proposed 0. Active buckets (an arrival or a request in service): current 31 of 31, proposed 0 of 31.

Service time: current mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean); proposed none (no row routed there).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 20 | 0.0% | 0.0% | 0.0% | 0 of 31 | n/a | n/a |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.6543 | 0.313078 | 0.313078 |
| concurrency | proposed | 0 | 0 | 0 |
| requests_per_minute | current | 6 | 2.87097 | 2.87097 |
| requests_per_minute | proposed | 0 | 0 | 0 |
| tokens_per_minute | current | 9445 | 4350.19 | 4350.19 |
| tokens_per_minute | proposed | 0 | 0 | 0 |
| output_tokens_per_minute | current | 3195 | 1431.23 | 1431.23 |
| output_tokens_per_minute | proposed | 0 | 0 | 0 |

#### qwen3.6-35b

Requests: current 292, proposed 307. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2292.3 ms, longest 5261 ms over 292 row(s) (291 recorded, 1 endpoint_mean); proposed mean 2292.29 ms, longest 5261 ms over 307 row(s) (291 recorded, 16 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 60.3% | 18.0% | 18.0% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| max_concurrency | proposed | 2 | 60.3% | 18.9% | 18.9% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | current | 40 | 57.5% | 23.5% | 23.5% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | proposed | 40 | 57.5% | 24.8% | 24.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | current | 80000 | 37.1% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | proposed | 80000 | 40.5% | 18.1% | 18.1% | 0 of 31 | 0.0% | 2026-10-09T13:47:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.20657 | 0.359867 | 0.359867 |
| concurrency | proposed | 1.20657 | 0.378351 | 0.378351 |
| requests_per_minute | current | 23 | 9.41935 | 9.41935 |
| requests_per_minute | proposed | 23 | 9.90323 | 9.90323 |
| tokens_per_minute | current | 29709 | 11065.6 | 11065.6 |
| tokens_per_minute | proposed | 32415 | 14490.2 | 14490.2 |
| output_tokens_per_minute | current | 20945 | 7516.71 | 7516.71 |
| output_tokens_per_minute | proposed | 20945 | 8012.97 | 8012.97 |

## Scenario: clinic-may-use-cloud

requests: 899 &middot; route_changed: 59 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.25879 | 6.49772 | -0.761067 |
| cost_usd (both routed) | 7.25879 | 6.49772 | -0.761067 |
| latency_ms p50 | 2683.83 | 2683.83 | 0 |
| latency_ms p95 | 17889.6 | 17889.6 | 0 |
| quality_mean | 0.692768 | 0.703018 | 0.010251 |
| onprem_share | 0.794989 | 0.72779 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | openrouter-mid | 29 |
| glm-5.3-flash | openrouter-frontier | 24 |
| qwen3.6-35b | openrouter-frontier | 6 |

### New rejections

None.

Affected apps: paper-clinic

### Capacity

Peak load: current peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets); proposed peak 58.8% / avg 17.6%, over 100% in 0.0% of 31 buckets (qwen3.6-35b, max_concurrency, 60 s buckets).

#### glm-5.3-flash

Requests: current 406, proposed 353. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 3040.85 ms, longest 9145 ms over 406 row(s) (406 recorded); proposed mean 3149.99 ms, longest 9145 ms over 353 row(s) (353 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 63.1% | 33.2% | 33.2% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| max_concurrency | proposed | 2 | 57.6% | 29.9% | 29.9% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 32.7% | 32.7% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 40 | 52.5% | 28.5% | 28.5% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| tokens_per_minute | current | 80000 | 48.2% | 26.8% | 26.8% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |
| tokens_per_minute | proposed | 80000 | 45.3% | 23.6% | 23.6% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.26253 | 0.663756 | 0.663756 |
| concurrency | proposed | 1.15225 | 0.597822 | 0.597822 |
| requests_per_minute | current | 24 | 13.0968 | 13.0968 |
| requests_per_minute | proposed | 21 | 11.3871 | 11.3871 |
| tokens_per_minute | current | 38549 | 21460 | 21460 |
| tokens_per_minute | proposed | 36270 | 18867 | 18867 |
| output_tokens_per_minute | current | 12726 | 7466.58 | 7466.58 |
| output_tokens_per_minute | proposed | 12313 | 6801.06 | 6801.06 |

#### openrouter-frontier

Requests: current 91, proposed 121. Active buckets (an arrival or a request in service): current 30 of 31, proposed 31 of 31.

Service time: current mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean); proposed mean 17119 ms, longest 32492 ms over 121 row(s) (12 recorded, 109 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |
| requests_per_minute | proposed | 20 | 45.0% | 19.5% | 19.5% | 0 of 31 | 0.0% | 2026-10-09T14:06:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.7652 | 0.83754 | 0.865458 |
| concurrency | proposed | 2.08322 | 1.11365 | 1.11365 |
| requests_per_minute | current | 6 | 2.93548 | 3.03333 |
| requests_per_minute | proposed | 9 | 3.90323 | 3.90323 |
| tokens_per_minute | current | 24765 | 9651.94 | 9973.67 |
| tokens_per_minute | proposed | 24913 | 11975.7 | 11975.7 |
| output_tokens_per_minute | current | 3796 | 1638.45 | 1693.07 |
| output_tokens_per_minute | proposed | 5073 | 2105 | 2105 |

#### openrouter-mid

Requests: current 89, proposed 118. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean); proposed mean 6542.99 ms, longest 13323 ms over 118 row(s) (12 recorded, 106 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 20 | 45.0% | 19.0% | 19.0% | 0 of 31 | 0.0% | 2026-10-09T14:10:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.6543 | 0.313078 | 0.313078 |
| concurrency | proposed | 0.98145 | 0.415093 | 0.415093 |
| requests_per_minute | current | 6 | 2.87097 | 2.87097 |
| requests_per_minute | proposed | 9 | 3.80645 | 3.80645 |
| tokens_per_minute | current | 9445 | 4350.19 | 4350.19 |
| tokens_per_minute | proposed | 14019 | 5414 | 5414 |
| output_tokens_per_minute | current | 3195 | 1431.23 | 1431.23 |
| output_tokens_per_minute | proposed | 5641 | 1792.39 | 1792.39 |

#### qwen3.6-35b

Requests: current 292, proposed 286. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2292.3 ms, longest 5261 ms over 292 row(s) (291 recorded, 1 endpoint_mean); proposed mean 2288.12 ms, longest 5261 ms over 286 row(s) (285 recorded, 1 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 60.3% | 18.0% | 18.0% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| max_concurrency | proposed | 2 | 58.8% | 17.6% | 17.6% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | current | 40 | 57.5% | 23.5% | 23.5% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | proposed | 40 | 57.5% | 23.1% | 23.1% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | current | 80000 | 37.1% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | proposed | 80000 | 37.1% | 12.8% | 12.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.20657 | 0.359867 | 0.359867 |
| concurrency | proposed | 1.17683 | 0.351829 | 0.351829 |
| requests_per_minute | current | 23 | 9.41935 | 9.41935 |
| requests_per_minute | proposed | 23 | 9.22581 | 9.22581 |
| tokens_per_minute | current | 29709 | 11065.6 | 11065.6 |
| tokens_per_minute | proposed | 29709 | 10271 | 10271 |
| output_tokens_per_minute | current | 20945 | 7516.71 | 7516.71 |
| output_tokens_per_minute | proposed | 20945 | 7354.52 | 7354.52 |

## Scenario: raise-request-budget

requests: 899 &middot; route_changed: 6 &middot; newly_rejected: 0 &middot; newly_served: 21

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 7.25879 | 9.2772 | 2.01841 |
| cost_usd (both routed) | 7.25879 | 7.4805 | 0.221706 |
| latency_ms p50 | 2683.83 | 2683.83 | 0 |
| latency_ms p95 | 17889.6 | 17889.6 | 0 |
| quality_mean | 0.692768 | 0.695773 | 0.003005 |
| onprem_share | 0.794989 | 0.799778 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 21 |
| qwen3.6-35b | glm-5.3-flash | 6 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

### Capacity

Peak load: current peak 63.1% / avg 33.2%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets); proposed peak 63.1% / avg 35.4%, over 100% in 0.0% of 31 buckets (glm-5.3-flash, max_concurrency, 60 s buckets).

#### glm-5.3-flash

Requests: current 406, proposed 433. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 3040.85 ms, longest 9145 ms over 406 row(s) (406 recorded); proposed mean 3037.74 ms, longest 9145 ms over 433 row(s) (406 recorded, 27 endpoint_mean).

**Note:** 21 row(s) routed here in the proposed arm have no backend token count; their token demand is RouteForge's own estimate.

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 63.1% | 33.2% | 33.2% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| max_concurrency | proposed | 2 | 63.1% | 35.4% | 35.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | current | 40 | 60.0% | 32.7% | 32.7% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 40 | 60.0% | 34.9% | 34.9% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| tokens_per_minute | current | 80000 | 48.2% | 26.8% | 26.8% | 0 of 31 | 0.0% | 2026-10-09T13:56:00+00:00 |
| tokens_per_minute | proposed | 80000 | 60.7% | 35.1% | 35.1% | 0 of 31 | 0.0% | 2026-10-09T14:03:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.26253 | 0.663756 | 0.663756 |
| concurrency | proposed | 1.26253 | 0.707174 | 0.707174 |
| requests_per_minute | current | 24 | 13.0968 | 13.0968 |
| requests_per_minute | proposed | 24 | 13.9677 | 13.9677 |
| tokens_per_minute | current | 38549 | 21460 | 21460 |
| tokens_per_minute | proposed | 48528 | 28050.5 | 28050.5 |
| output_tokens_per_minute | current | 12726 | 7466.58 | 7466.58 |
| output_tokens_per_minute | proposed | 14262 | 8462.84 | 8462.84 |

#### openrouter-frontier

Requests: current 91, proposed 91. Active buckets (an arrival or a request in service): current 30 of 31, proposed 30 of 31.

Service time: current mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean); proposed mean 17118.9 ms, longest 32492 ms over 91 row(s) (12 recorded, 79 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |
| requests_per_minute | proposed | 20 | 30.0% | 14.7% | 15.2% | 0 of 31 | 0.0% | 2026-10-09T13:46:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.7652 | 0.83754 | 0.865458 |
| concurrency | proposed | 1.7652 | 0.83754 | 0.865458 |
| requests_per_minute | current | 6 | 2.93548 | 3.03333 |
| requests_per_minute | proposed | 6 | 2.93548 | 3.03333 |
| tokens_per_minute | current | 24765 | 9651.94 | 9973.67 |
| tokens_per_minute | proposed | 24765 | 9651.94 | 9973.67 |
| output_tokens_per_minute | current | 3796 | 1638.45 | 1693.07 |
| output_tokens_per_minute | proposed | 3796 | 1638.45 | 1693.07 |

#### openrouter-mid

Requests: current 89, proposed 89. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean); proposed mean 6542.99 ms, longest 13323 ms over 89 row(s) (12 recorded, 77 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| requests_per_minute | current | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |
| requests_per_minute | proposed | 20 | 30.0% | 14.4% | 14.4% | 0 of 31 | 0.0% | 2026-10-09T13:48:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 0.6543 | 0.313078 | 0.313078 |
| concurrency | proposed | 0.6543 | 0.313078 | 0.313078 |
| requests_per_minute | current | 6 | 2.87097 | 2.87097 |
| requests_per_minute | proposed | 6 | 2.87097 | 2.87097 |
| tokens_per_minute | current | 9445 | 4350.19 | 4350.19 |
| tokens_per_minute | proposed | 9445 | 4350.19 | 4350.19 |
| output_tokens_per_minute | current | 3195 | 1431.23 | 1431.23 |
| output_tokens_per_minute | proposed | 3195 | 1431.23 | 1431.23 |

#### qwen3.6-35b

Requests: current 292, proposed 286. Active buckets (an arrival or a request in service): current 31 of 31, proposed 31 of 31.

Service time: current mean 2292.3 ms, longest 5261 ms over 292 row(s) (291 recorded, 1 endpoint_mean); proposed mean 2288.12 ms, longest 5261 ms over 286 row(s) (285 recorded, 1 endpoint_mean).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 2 | 60.3% | 18.0% | 18.0% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| max_concurrency | proposed | 2 | 58.8% | 17.6% | 17.6% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | current | 40 | 57.5% | 23.5% | 23.5% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| requests_per_minute | proposed | 40 | 57.5% | 23.1% | 23.1% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | current | 80000 | 37.1% | 13.8% | 13.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |
| tokens_per_minute | proposed | 80000 | 37.1% | 12.8% | 12.8% | 0 of 31 | 0.0% | 2026-10-09T14:01:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.20657 | 0.359867 | 0.359867 |
| concurrency | proposed | 1.17683 | 0.351829 | 0.351829 |
| requests_per_minute | current | 23 | 9.41935 | 9.41935 |
| requests_per_minute | proposed | 23 | 9.22581 | 9.22581 |
| tokens_per_minute | current | 29709 | 11065.6 | 11065.6 |
| tokens_per_minute | proposed | 29709 | 10271 | 10271 |
| output_tokens_per_minute | current | 20945 | 7516.71 | 7516.71 |
| output_tokens_per_minute | proposed | 20945 | 7354.52 | 7354.52 |

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
