> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

Baseline agreement: model 13.5% over 111 compared rows; outcome 100.0% over 111 simulated rows.

> **Warning:** baseline agreement is only 13.5% across 111 compared rows (below the 90% threshold). Treat the headline diff as noisier than usual -- see spec §7 for causes.

## Headline

requests: 111 &middot; route_changed: 30 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.056764 | 0.027814 | -0.02895 |
| cost_usd (both routed) | 0.056764 | 0.027814 | -0.02895 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.716216 | 0.648649 | -0.067568 |
| onprem_share | 0.405405 | 0.405405 | n/a |
| overflow_hops | 0 | 0 | 0 |

## New rejections

None.

## Moves

| from | to | count |
|---|---|---|
| local-mid | local-cheap | 30 |

## By app

### big

requests: 60 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.018128 | 0.018128 | 0 |
| cost_usd (both routed) | 0.018128 | 0.018128 | 0 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 0 | 0 | n/a |
| overflow_hops | 0 | 0 | 0 |

### few

requests: 6 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.001691 | 0.001691 | 0 |
| cost_usd (both routed) | 0.001691 | 0.001691 | 0 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 0 | 0 | n/a |
| overflow_hops | 0 | 0 | 0 |

### safe

requests: 45 &middot; route_changed: 30 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.036945 | 0.007995 | -0.02895 |
| cost_usd (both routed) | 0.036945 | 0.007995 | -0.02895 |
| latency_ms p50 | 500 | 120 | -380 |
| latency_ms p95 | 500 | 120 | -380 |
| quality_mean | 0.666667 | 0.5 | -0.166667 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

## By workload

### (untagged)

requests: 111 &middot; route_changed: 30 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.056764 | 0.027814 | -0.02895 |
| cost_usd (both routed) | 0.056764 | 0.027814 | -0.02895 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.716216 | 0.648649 | -0.067568 |
| onprem_share | 0.405405 | 0.405405 | n/a |
| overflow_hops | 0 | 0 | 0 |

## Latency and quality sources

### current
- latency: {'tier_default': 96, 'observed': 15}
- quality: {'tier_default': 111}
### proposed
- latency: {'tier_default': 66, 'observed': 45}
- quality: {'tier_default': 111}

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
