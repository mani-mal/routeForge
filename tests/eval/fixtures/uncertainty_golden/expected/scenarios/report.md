> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: Golden options

111 rows simulated, 3 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 13.5%** over 111 compared rows.

⚠ **Warning:** below the 90% threshold. Every scenario row below rests on a replay that disagrees with the record this often.

Per app: big 0.0% ⚠ · few 0.0% ⚠ · safe 33.3% ⚠

**Outcome agreement: 100.0%** over 111 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): big 100.0% · few 100.0% · safe 100.0%

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.405405→0.405405 | 0 | none |
| drop-mid | -0.014184 | -0.02895 | 30 | 0 | 0 | 0 | 0 | -0.067568 | 0.405405→0.405405 | 0 | safe |
| half-tokens | -0.014641 | -0.029882 | 15 | 0 | 0 | 0 | 0 | -0.033784 | 0.405405→0.405405 | 0 | safe |

Listed in file order. RouteForge does not rank or recommend scenarios.

Cost columns compare only the rows routed in both arms. A row a scenario newly rejects is counted under "newly rejected", never as a saving.

## Assumptions

- **half-tokens** -- token_scale: *=0.5; rows whose saturated complexity was estimated from scaled prompt tokens: 37

## Scenario: no-change

requests: 111 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.056764 | 0.056764 | 0 |
| cost_usd (both routed) | 0.056764 | 0.056764 | 0 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.716216 | 0.716216 | 0 |
| onprem_share | 0.405405 | 0.405405 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

## Scenario: drop-mid

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

### Moves

| from | to | count |
|---|---|---|
| local-mid | local-cheap | 30 |

### New rejections

None.

Affected apps: safe

## Scenario: half-tokens

requests: 111 &middot; route_changed: 15 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.056764 | 0.026882 | -0.029882 |
| cost_usd (both routed) | 0.056764 | 0.026882 | -0.029882 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.716216 | 0.682432 | -0.033784 |
| onprem_share | 0.405405 | 0.405405 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| local-mid | local-cheap | 15 |

### New rejections

None.

Affected apps: safe

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
