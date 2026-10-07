> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

Baseline agreement: model 37.4% over 147 compared rows; outcome 100.0% over 160 simulated rows.

> **Warning:** baseline agreement is only 37.4% across 147 compared rows (below the 90% threshold). Treat the headline diff as noisier than usual -- see spec §7 for causes.

> **Note:** 13 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Headline

requests: 160 &middot; route_changed: 39 &middot; newly_rejected: 40 &middot; newly_served: 13

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.079981 | 0.037647 | -0.042334 |
| cost_usd (both routed) | 0.045331 | 0.021759 | -0.023572 |
| latency_ms p50 | 500 | 120 | -380 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.656463 | 0.608333 | -0.048129 |
| onprem_share | 0.823129 | 0.566667 | n/a |
| overflow_hops | 0 | 0 | 0 |

## New rejections

| app | count |
|---|---|
| pinned | 40 |

| code | count |
|---|---|
| app_scope | 40 |

**By conflicting core**

| core | count |
|---|---|
| allowed_models | 40 |

## Moves

| from | to | count |
|---|---|---|
| local-mid | (rejected) | 40 |
| local-mid | local-cheap | 26 |
| (rejected) | azure-gpt-mid | 13 |
| local-cheap | azure-gpt-mid | 13 |

## By app

### capped

requests: 40 &middot; route_changed: 13 &middot; newly_rejected: 0 &middot; newly_served: 13

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.00264 | 0.01989 | 0.01725 |
| cost_usd (both routed) | 0.00264 | 0.004002 | 0.001362 |
| latency_ms p50 | 120 | 500 | 380 |
| latency_ms p95 | 120 | 500 | 380 |
| quality_mean | 0.5 | 0.6625 | 0.1625 |
| onprem_share | 1 | 0.35 | n/a |
| overflow_hops | 0 | 0 | 0 |

### pinned

requests: 40 &middot; route_changed: 0 &middot; newly_rejected: 40 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.03465 | 0 | -0.03465 |
| cost_usd (both routed) | 0 | 0 | 0 |
| latency_ms p50 | 500 | n/a | n/a |
| latency_ms p95 | 500 | n/a | n/a |
| quality_mean | 0.75 | n/a | n/a |
| onprem_share | 1 | n/a | n/a |
| overflow_hops | 0 | 0 | 0 |

### safe

requests: 40 &middot; route_changed: 26 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.031864 | 0.00693 | -0.024934 |
| cost_usd (both routed) | 0.031864 | 0.00693 | -0.024934 |
| latency_ms p50 | 500 | 120 | -380 |
| latency_ms p95 | 500 | 120 | -380 |
| quality_mean | 0.6625 | 0.5 | -0.1625 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### steady

requests: 40 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.010827 | 0.010827 | 0 |
| cost_usd (both routed) | 0.010827 | 0.010827 | 0 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.6625 | 0.6625 | 0 |
| onprem_share | 0.35 | 0.35 | n/a |
| overflow_hops | 0 | 0 | 0 |

## By workload

### (untagged)

requests: 160 &middot; route_changed: 39 &middot; newly_rejected: 40 &middot; newly_served: 13

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.079981 | 0.037647 | -0.042334 |
| cost_usd (both routed) | 0.045331 | 0.021759 | -0.023572 |
| latency_ms p50 | 500 | 120 | -380 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.656463 | 0.608333 | -0.048129 |
| onprem_share | 0.823129 | 0.566667 | n/a |
| overflow_hops | 0 | 0 | 0 |

## Latency and quality sources

### current
- latency: {'observed': 55, 'tier_default': 92}
- quality: {'tier_default': 147}
### proposed
- latency: {'observed': 68, 'tier_default': 52}
- quality: {'tier_default': 120}

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
| allowed_models | app:pinned | 40 | 0 | 30 | 0 | 0/day | n/a | n/a | n/a→0.333333 |

### Cost of protection

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) | label |
|---|---|---|---|---|---|---|---|---|---|---|
| residency | app:safe | 40 | 26 | 0 | 0 | -0.001923/day | 0.1625 | 380 | 1→0.35 | price of a protection — not a recommendation |

Relaxing a constraint can newly reject a row: a wider allowed set can lift the target to the risk floor, and the budget fallback never goes below the floor.

Non-binding: 5 · skipped by cap: 0
