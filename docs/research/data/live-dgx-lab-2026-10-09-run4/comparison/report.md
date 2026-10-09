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

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0.797267→0.797267 | 0 | none |
| all-apps-onprem-only | n/a | 3.7391 [3.20027, 4.29716] | 178 [160, 197] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | -9394.52 [-9394.52, -9394.52] | -0.033599 [-0.039278, -0.028253] | 0.797267→1 | 0 | paper-claims, paper-dev |
| all-apps-no-retention | n/a | 3.7391 [3.20027, 4.29716] | 178 [160, 197] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | -9394.52 [-9394.52, -9394.52] | -0.033599 [-0.039278, -0.028253] | 0.797267→1 | 0 | paper-claims, paper-dev |
| clinic-may-use-cloud | n/a | -0.890274 [-1.1905, -0.6521] | 63 [52, 75] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0.01623 [0.012328, 0.020286] | 0.797267→0.725513 | 0 | paper-clinic |
| raise-request-budget | n/a | 0.760635 [0.463374, 1.14966] | 15 [9, 22] | 0 [0, 0] | 22 [15, 30] | 0 [0, 0] | 0 [0, 0] | 0.005768 [0.004031, 0.007576] | 0.797267→0.802222 | 0 | paper-claims, paper-clinic, paper-dev |

Listed in file order. RouteForge does not rank or recommend scenarios.

Cost columns compare only the rows routed in both arms. A row a scenario newly rejects is counted under "newly rejected", never as a saving.

A count of 0 shows `[0, 0]` at any sample size; that is not zero risk.

## Uncertainty ranges

> **Ranges show sampling variability only.** Each range is a percentile interval from
> resampling the recorded rows with replacement, within each application (applications with
> fewer than `--min-rows` rows are resampled together for the totals), under this replay
> model. It does not include replay model error (first-decision replay, frozen signals,
> predicted overflow), the error of the admission estimate for rows with no backend token
> count, inputs that were not recorded, or future traffic shift. Replay gives each endpoint
> one latency, so a latency range reflects only which endpoints the resampled rows used. A
> narrow range on a biased replay is still biased.

90% percentile intervals from 1000 bootstrap resamples of the simulated rows, drawn with replacement within each application (applications with fewer than 30 rows are resampled together for the totals) (seed 0). Both arms, and every scenario or relaxation, use the same resampled rows. A bucket with fewer than 30 rows gets no range.

22 of 900 simulated rows have no backend token count (`estimated_token_rows`): replay sizes and prices them from RouteForge's own estimate, the same in every resample.

Totals strata: 3 application(s) resampled on their own and 0 with fewer than 30 rows resampled together (0 rows); the smallest stratum has 275 rows. Resampling within strata leaves the totals about 0.996667 of their variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking: overlapping ranges do not make two scenarios equal, and separate ranges do not make one of them the one to choose.

- **no-change** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **all-apps-onprem-only** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **all-apps-no-retention** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **clinic-may-use-cloud** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **raise-request-budget** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 900 in the proposed arm; the rest use a point prior.

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd proposed | 7.01583 | 6.50374 to 7.57461 |
| cost_usd delta | 0 | 0 to 0 |
| cost_usd (both routed) current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd (both routed) proposed | 7.01583 | 6.50374 to 7.57461 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 proposed | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.68451 | 0.675768 to 0.693182 |
| quality_mean proposed | 0.68451 | 0.675768 to 0.693182 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 0.797267 | 0.776 to 0.817253 |
| onprem_share proposed | 0.797267 | 0.776 to 0.817253 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

Per-app ranges are in `comparison.json`.

### Moves

None.

### New rejections

None.

Affected apps: none

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd proposed | 10.7549 | 10.0833 to 11.4738 |
| cost_usd delta | 3.7391 | 3.20027 to 4.29716 |
| cost_usd (both routed) current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd (both routed) proposed | 10.7549 | 10.0833 to 11.4738 |
| cost_usd (both routed) delta | 3.7391 | 3.20027 to 4.29716 |
| route_changed | 178 | 160 to 197 |
| route_changed_share | 0.197778 | 0.177778 to 0.218889 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p95 delta | -9394.52 | -9394.52 to -9394.52 |
| quality_mean current | 0.68451 | 0.675768 to 0.693182 |
| quality_mean proposed | 0.650911 | 0.644165 to 0.657565 |
| quality_mean delta | -0.033599 | -0.039278 to -0.028253 |
| onprem_share current | 0.797267 | 0.776 to 0.817253 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 84 |
| openrouter-frontier | glm-5.3-flash | 70 |
| openrouter-frontier | qwen3.6-35b | 24 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd proposed | 10.7549 | 10.0833 to 11.4738 |
| cost_usd delta | 3.7391 | 3.20027 to 4.29716 |
| cost_usd (both routed) current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd (both routed) proposed | 10.7549 | 10.0833 to 11.4738 |
| cost_usd (both routed) delta | 3.7391 | 3.20027 to 4.29716 |
| route_changed | 178 | 160 to 197 |
| route_changed_share | 0.197778 | 0.177778 to 0.218889 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p95 delta | -9394.52 | -9394.52 to -9394.52 |
| quality_mean current | 0.68451 | 0.675768 to 0.693182 |
| quality_mean proposed | 0.650911 | 0.644165 to 0.657565 |
| quality_mean delta | -0.033599 | -0.039278 to -0.028253 |
| onprem_share current | 0.797267 | 0.776 to 0.817253 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 84 |
| openrouter-frontier | glm-5.3-flash | 70 |
| openrouter-frontier | qwen3.6-35b | 24 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd proposed | 6.12556 | 5.64957 to 6.65221 |
| cost_usd delta | -0.890274 | -1.1905 to -0.6521 |
| cost_usd (both routed) current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd (both routed) proposed | 6.12556 | 5.64957 to 6.65221 |
| cost_usd (both routed) delta | -0.890274 | -1.1905 to -0.6521 |
| route_changed | 63 | 52 to 75 |
| route_changed_share | 0.07 | 0.057778 to 0.083333 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 proposed | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.68451 | 0.675768 to 0.693182 |
| quality_mean proposed | 0.70074 | 0.691761 to 0.709816 |
| quality_mean delta | 0.01623 | 0.012328 to 0.020286 |
| onprem_share current | 0.797267 | 0.776 to 0.817253 |
| onprem_share proposed | 0.725513 | 0.701714 to 0.748276 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | openrouter-frontier | 27 |
| glm-5.3-flash | openrouter-mid | 21 |
| qwen3.6-35b | openrouter-frontier | 15 |

### New rejections

None.

Affected apps: paper-clinic

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd proposed | 9.72416 | 8.80666 to 10.6804 |
| cost_usd delta | 2.70832 | 1.91594 to 3.56593 |
| cost_usd (both routed) current | 7.01583 | 6.50374 to 7.57461 |
| cost_usd (both routed) proposed | 7.77647 | 7.17032 to 8.44397 |
| cost_usd (both routed) delta | 0.760635 | 0.463374 to 1.14966 |
| route_changed | 15 | 9 to 22 |
| route_changed_share | 0.016667 | 0.01 to 0.024444 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 22 | 15 to 30 |
| newly_served_share | 0.024444 | 0.016667 to 0.033333 |
| latency_ms p50 current | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 proposed | 2681.28 | 2681.28 to 2681.28 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 proposed | 12075.8 | 12075.8 to 12075.8 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.68451 | 0.675768 to 0.693182 |
| quality_mean proposed | 0.690278 | 0.681944 to 0.698333 |
| quality_mean delta | 0.005768 | 0.004031 to 0.007576 |
| onprem_share current | 0.797267 | 0.776 to 0.817253 |
| onprem_share proposed | 0.802222 | 0.781111 to 0.822222 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 22 |
| qwen3.6-35b | glm-5.3-flash | 15 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
