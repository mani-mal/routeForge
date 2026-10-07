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

## Shadow prices

> **What each constraint costs.** Each row relaxes ONE constraint for one app (or endpoint) and
> replays the same recorded traffic. Nothing is applied. Security constraints are shown as the
> cost of a protection — they are never a recommendation to relax them.

### Operational constraints

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) |
|---|---|---|---|---|---|---|---|---|---|
| allowed_tiers | app:big | 60 | 20 | 0 | 0 | 0.00056/day | -0.083333 | 0 | 0→0.333333 |
| allowed_tiers | app:few | 6 | 2 | 0 | 0 | 4.9e-05/day | -0.083333 | 0 | 0→0.333333 |

### Cost of protection

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) | label |
|---|---|---|---|---|---|---|---|---|---|---|
| residency | app:safe | 45 | 30 | 0 | 0 | -0.002214/day | 0.166667 | 380 | 1→0.333333 | price of a protection — not a recommendation |

Relaxing a constraint can newly reject a row: a wider allowed set can lift the target to the risk floor, and the budget fallback never goes below the floor.

Non-binding: 3 · skipped by cap: 0
