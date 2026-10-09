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

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0.794989→0.794989 | 0 | none |
| all-apps-onprem-only | n/a | 3.12205 [2.68873, 3.54237] | 180 [163, 199] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | -15205.8 [-15205.8, -15205.8] | -0.030182 [-0.035511, -0.025371] | 0.794989→1 | 0 | paper-claims, paper-dev |
| all-apps-no-retention | n/a | 3.12205 [2.68873, 3.54237] | 180 [163, 199] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | -15205.8 [-15205.8, -15205.8] | -0.030182 [-0.035511, -0.025371] | 0.794989→1 | 0 | paper-claims, paper-dev |
| clinic-may-use-cloud | n/a | -0.761067 [-0.949771, -0.585306] | 59 [48, 70] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0.010251 [0.007151, 0.013636] | 0.794989→0.72779 | 0 | paper-clinic |
| raise-request-budget | n/a | 0.221706 [0.074646, 0.376803] | 6 [2, 10] | 0 [0, 0] | 21 [14, 28] | 0 [0, 0] | 0 [0, 0] | 0.003005 [0.00184, 0.004314] | 0.794989→0.799778 | 0 | paper-claims, paper-clinic, paper-dev |

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

21 of 899 simulated rows have no backend token count (`estimated_token_rows`): replay sizes and prices them from RouteForge's own estimate, the same in every resample.

Totals strata: 3 application(s) resampled on their own and 0 with fewer than 30 rows resampled together (0 rows); the smallest stratum has 273 rows. Resampling within strata leaves the totals about 0.996663 of their variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking: overlapping ranges do not make two scenarios equal, and separate ranges do not make one of them the one to choose.

- **no-change** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **all-apps-onprem-only** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **all-apps-no-retention** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **clinic-may-use-cloud** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **raise-request-budget** -- Quality uncertainty covers 0 of 878 routed rows in the current arm and 0 of 899 in the proposed arm; the rest use a point prior.

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd proposed | 7.25879 | 6.73916 to 7.8144 |
| cost_usd delta | 0 | 0 to 0 |
| cost_usd (both routed) current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd (both routed) proposed | 7.25879 | 6.73916 to 7.8144 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 proposed | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.692768 | 0.684537 to 0.701695 |
| quality_mean proposed | 0.692768 | 0.684537 to 0.701695 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 0.794989 | 0.773455 to 0.814773 |
| onprem_share proposed | 0.794989 | 0.773455 to 0.814773 |

newly_rejected is 0 in all 899 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

Per-app ranges are in `comparison.json`.

### Moves

None.

### New rejections

None.

Affected apps: none

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd proposed | 10.3808 | 9.72726 to 11.0457 |
| cost_usd delta | 3.12205 | 2.68873 to 3.54237 |
| cost_usd (both routed) current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd (both routed) proposed | 10.3808 | 9.72726 to 11.0457 |
| cost_usd (both routed) delta | 3.12205 | 2.68873 to 3.54237 |
| route_changed | 180 | 163 to 199 |
| route_changed_share | 0.200222 | 0.181313 to 0.221357 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p95 delta | -15205.8 | -15205.8 to -15205.8 |
| quality_mean current | 0.692768 | 0.684537 to 0.701695 |
| quality_mean proposed | 0.662585 | 0.656002 to 0.669134 |
| quality_mean delta | -0.030182 | -0.035511 to -0.025371 |
| onprem_share current | 0.794989 | 0.773455 to 0.814773 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 899 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 89 |
| openrouter-frontier | glm-5.3-flash | 76 |
| openrouter-frontier | qwen3.6-35b | 15 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd proposed | 10.3808 | 9.72726 to 11.0457 |
| cost_usd delta | 3.12205 | 2.68873 to 3.54237 |
| cost_usd (both routed) current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd (both routed) proposed | 10.3808 | 9.72726 to 11.0457 |
| cost_usd (both routed) delta | 3.12205 | 2.68873 to 3.54237 |
| route_changed | 180 | 163 to 199 |
| route_changed_share | 0.200222 | 0.181313 to 0.221357 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p95 delta | -15205.8 | -15205.8 to -15205.8 |
| quality_mean current | 0.692768 | 0.684537 to 0.701695 |
| quality_mean proposed | 0.662585 | 0.656002 to 0.669134 |
| quality_mean delta | -0.030182 | -0.035511 to -0.025371 |
| onprem_share current | 0.794989 | 0.773455 to 0.814773 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 899 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| openrouter-mid | glm-5.3-flash | 89 |
| openrouter-frontier | glm-5.3-flash | 76 |
| openrouter-frontier | qwen3.6-35b | 15 |

### New rejections

None.

Affected apps: paper-claims, paper-dev

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd proposed | 6.49772 | 5.97694 to 7.05007 |
| cost_usd delta | -0.761067 | -0.949771 to -0.585306 |
| cost_usd (both routed) current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd (both routed) proposed | 6.49772 | 5.97694 to 7.05007 |
| cost_usd (both routed) delta | -0.761067 | -0.949771 to -0.585306 |
| route_changed | 59 | 48 to 70 |
| route_changed_share | 0.065628 | 0.053393 to 0.077864 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 proposed | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.692768 | 0.684537 to 0.701695 |
| quality_mean proposed | 0.703018 | 0.694034 to 0.712868 |
| quality_mean delta | 0.010251 | 0.007151 to 0.013636 |
| onprem_share current | 0.794989 | 0.773455 to 0.814773 |
| onprem_share proposed | 0.72779 | 0.702857 to 0.751147 |

newly_rejected is 0 in all 899 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | openrouter-mid | 29 |
| glm-5.3-flash | openrouter-frontier | 24 |
| qwen3.6-35b | openrouter-frontier | 6 |

### New rejections

None.

Affected apps: paper-clinic

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

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd proposed | 9.2772 | 8.49914 to 10.093 |
| cost_usd delta | 2.01841 | 1.36284 to 2.70525 |
| cost_usd (both routed) current | 7.25879 | 6.73916 to 7.8144 |
| cost_usd (both routed) proposed | 7.4805 | 6.92141 to 8.04623 |
| cost_usd (both routed) delta | 0.221706 | 0.074646 to 0.376803 |
| route_changed | 6 | 2 to 10 |
| route_changed_share | 0.006674 | 0.002225 to 0.011123 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 21 | 14 to 28 |
| newly_served_share | 0.023359 | 0.015573 to 0.031146 |
| latency_ms p50 current | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 proposed | 2683.83 | 2683.83 to 2683.83 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 proposed | 17889.6 | 17889.6 to 17889.6 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.692768 | 0.684537 to 0.701695 |
| quality_mean proposed | 0.695773 | 0.687709 to 0.704672 |
| quality_mean delta | 0.003005 | 0.00184 to 0.004314 |
| onprem_share current | 0.794989 | 0.773455 to 0.814773 |
| onprem_share proposed | 0.799778 | 0.778643 to 0.818687 |

newly_rejected is 0 in all 899 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 21 |
| qwen3.6-35b | glm-5.3-flash | 6 |

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
