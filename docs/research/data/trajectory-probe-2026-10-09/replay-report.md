> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

Baseline agreement: model 100.0% over 54 compared rows; outcome 100.0% over 54 simulated rows.

## Headline

requests: 54 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.56293 | 0.56293 | 0 |
| cost_usd (both routed) | 0.56293 | 0.56293 | 0 |
| latency_ms p50 | 3186.15 | 3186.15 | 0 |
| latency_ms p95 | 10895.8 | 10895.8 | 0 |
| quality_mean | 0.847222 | 0.847222 | 0 |
| onprem_share | 0.611111 | 0.611111 | n/a |
| overflow_hops | 0 | 0 | 0 |

## New rejections

None.

## Moves

None.

## By app

### paper-dev

requests: 54 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.56293 | 0.56293 | 0 |
| cost_usd (both routed) | 0.56293 | 0.56293 | 0 |
| latency_ms p50 | 3186.15 | 3186.15 | 0 |
| latency_ms p95 | 10895.8 | 10895.8 | 0 |
| quality_mean | 0.847222 | 0.847222 | 0 |
| onprem_share | 0.611111 | 0.611111 | n/a |
| overflow_hops | 0 | 0 | 0 |

## By workload

### trajectory_probe

requests: 54 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.56293 | 0.56293 | 0 |
| cost_usd (both routed) | 0.56293 | 0.56293 | 0 |
| latency_ms p50 | 3186.15 | 3186.15 | 0 |
| latency_ms p95 | 10895.8 | 10895.8 | 0 |
| quality_mean | 0.847222 | 0.847222 | 0 |
| onprem_share | 0.611111 | 0.611111 | n/a |
| overflow_hops | 0 | 0 | 0 |

## Latency and quality sources

### current
- latency: {'observed': 54}
- quality: {'tier_default': 54}
### proposed
- latency: {'observed': 54}
- quality: {'tier_default': 54}

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
