> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: Golden rejections

160 rows simulated, 4 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 37.4%** over 147 compared rows.

⚠ **Warning:** below the 90% threshold. Every scenario row below rests on a replay that disagrees with the record this often.

Per app: capped 100.0% · pinned 0.0% ⚠ · safe 35.0% ⚠ · steady 35.0% ⚠

**Outcome agreement: 100.0%** over 160 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): capped 100.0% · pinned 100.0% · safe 100.0% · steady 100.0%

**Note:** 13 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.823129→0.823129 | 0 | none |
| lift-cap | 0.000672 | 0.001362 | 13 | 0 | 13 | 0 | 0 | 0.027912 | 0.823129→0.675 | 0 | capped |
| drop-mid | -0.012305 | -0.024934 | 26 | 40 | 0 | -380 | 0 | -0.095715 | 0.823129→0.757009 | 0 | pinned, safe |
| lift-cap+drop-mid | -0.011633 | -0.023572 | 39 | 40 | 13 | -380 | 0 | -0.048129 | 0.823129→0.566667 | 0 | capped, pinned, safe |

Listed in file order. RouteForge does not rank or recommend scenarios.

Cost columns compare only the rows routed in both arms. A row a scenario newly rejects is counted under "newly rejected", never as a saving.

## Scenario: no-change

requests: 160 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.079981 | 0.079981 | 0 |
| cost_usd (both routed) | 0.079981 | 0.079981 | 0 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.656463 | 0.656463 | 0 |
| onprem_share | 0.823129 | 0.823129 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

None.

### New rejections

None.

Affected apps: none

## Scenario: lift-cap

requests: 160 &middot; route_changed: 13 &middot; newly_rejected: 0 &middot; newly_served: 13

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.079981 | 0.097231 | 0.01725 |
| cost_usd (both routed) | 0.079981 | 0.081342 | 0.001362 |
| latency_ms p50 | 500 | 500 | 0 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.656463 | 0.684375 | 0.027912 |
| onprem_share | 0.823129 | 0.675 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| (rejected) | azure-gpt-mid | 13 |
| local-cheap | azure-gpt-mid | 13 |

### New rejections

None.

Affected apps: capped

## Scenario: drop-mid

requests: 160 &middot; route_changed: 26 &middot; newly_rejected: 40 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 0.079981 | 0.020397 | -0.059584 |
| cost_usd (both routed) | 0.045331 | 0.020397 | -0.024934 |
| latency_ms p50 | 500 | 120 | -380 |
| latency_ms p95 | 500 | 500 | 0 |
| quality_mean | 0.656463 | 0.560748 | -0.095715 |
| onprem_share | 0.823129 | 0.757009 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Moves

| from | to | count |
|---|---|---|
| local-mid | (rejected) | 40 |
| local-mid | local-cheap | 26 |

### New rejections

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

Affected apps: pinned, safe

## Scenario: lift-cap+drop-mid

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

### Moves

| from | to | count |
|---|---|---|
| local-mid | (rejected) | 40 |
| local-mid | local-cheap | 26 |
| (rejected) | azure-gpt-mid | 13 |
| local-cheap | azure-gpt-mid | 13 |

### New rejections

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

Affected apps: capped, pinned, safe

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
