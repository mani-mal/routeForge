> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: DGX lab live run 2, 2026-10-08 (three paper apps, three endpoint names over one server, 900 rows)

900 rows simulated, 5 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 92.6%** over 880 compared rows.

Per app: paper-claims 87.2% ⚠ · paper-clinic 100.0% · paper-dev 89.1% ⚠

**Outcome agreement: 100.0%** over 900 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Note:** 20 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 0 | none |
| all-apps-no-retention | n/a | 0.46795 [0.41865, 0.521085] | 248 [230, 268] | 4 [1, 7] | 0 [0, 0] | 83.5711 [83.5711, 83.5711] | 0 [0, 0] | 0.071429 [0.066327, 0.076901] | 1→1 | 0 | paper-claims, paper-dev |
| all-apps-us-lab-only | n/a | 1.40954 [1.13213, 1.67234] | 296 [277, 314] | 5 [2, 9] | 0 [0, 0] | 0 [0, 0] | -83.5711 [-83.5711, -83.5711] | -0.003258 [-0.004782, -0.001812] | 1→1 | 0 | paper-claims, paper-dev |
| clinic-may-use-eu-lab | n/a | -1.03733 [-1.15547, -0.925896] | 329 [324, 334] | 0 [0, 0] | 10 [5, 15] | 83.5711 [83.5711, 83.5711] | 0 [0, 0] | 0.000801 [0.000413, 0.00123] | 1→1 | 0 | paper-clinic |
| raise-request-budget | n/a | 0.149515 [0.04081, 0.28036] | 4 [1, 7] | 0 [0, 0] | 18 [12, 25] | 0 [0, 0] | 0 [0, 0] | 0.00254 [0.001575, 0.00365] | 1→1 | 0 | paper-claims, paper-clinic, paper-dev |

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

20 of 900 simulated rows have no backend token count (`estimated_token_rows`): replay sizes and prices them from RouteForge's own estimate, the same in every resample.

Totals strata: 3 application(s) resampled on their own and 0 with fewer than 30 rows resampled together (0 rows); the smallest stratum has 269 rows. Resampling within strata leaves the totals about 0.996667 of their variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking: overlapping ranges do not make two scenarios equal, and separate ranges do not make one of them the one to choose.

- **no-change** -- Quality uncertainty covers 0 of 882 routed rows in the current arm and 0 of 882 in the proposed arm; the rest use a point prior.
- **all-apps-no-retention** -- Quality uncertainty covers 0 of 882 routed rows in the current arm and 0 of 878 in the proposed arm; the rest use a point prior.
- **all-apps-us-lab-only** -- Quality uncertainty covers 0 of 882 routed rows in the current arm and 0 of 877 in the proposed arm; the rest use a point prior.
- **clinic-may-use-eu-lab** -- Quality uncertainty covers 0 of 882 routed rows in the current arm and 0 of 892 in the proposed arm; the rest use a point prior.
- **raise-request-budget** -- Quality uncertainty covers 0 of 882 routed rows in the current arm and 0 of 900 in the proposed arm; the rest use a point prior.

## Scenario: no-change

requests: 900 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 6.00133 | 0 |
| cost_usd (both routed) | 6.00133 | 6.00133 | 0 |
| latency_ms p50 | 485.741 | 485.741 | 0 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.678571 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd proposed | 6.00133 | 5.52547 to 6.44355 |
| cost_usd delta | 0 | 0 to 0 |
| cost_usd (both routed) current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd (both routed) proposed | 6.00133 | 5.52547 to 6.44355 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 485.741 | 485.741 to 485.741 |
| latency_ms p50 proposed | 485.741 | 485.741 to 485.741 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 569.312 | 569.312 to 569.312 |
| latency_ms p95 proposed | 569.312 | 569.312 to 569.312 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.678571 | 0.673099 to 0.683673 |
| quality_mean proposed | 0.678571 | 0.673099 to 0.683673 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

None.

### New rejections

None.

Affected apps: none

## Scenario: all-apps-no-retention

requests: 900 &middot; route_changed: 248 &middot; newly_rejected: 4 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 6.43938 | 0.438047 |
| cost_usd (both routed) | 5.97143 | 6.43938 | 0.46795 |
| latency_ms p50 | 485.741 | 569.312 | 83.5711 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.75 | 0.071429 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd proposed | 6.43938 | 5.97936 to 6.86487 |
| cost_usd delta | 0.438047 | 0.380968 to 0.495151 |
| cost_usd (both routed) current | 5.97143 | 5.4918 to 6.41079 |
| cost_usd (both routed) proposed | 6.43938 | 5.97936 to 6.86487 |
| cost_usd (both routed) delta | 0.46795 | 0.41865 to 0.521085 |
| route_changed | 248 | 230 to 268 |
| route_changed_share | 0.275556 | 0.255556 to 0.297778 |
| newly_rejected | 4 | 1 to 7 |
| newly_rejected_share | 0.004444 | 0.001111 to 0.007778 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 485.741 | 485.741 to 485.741 |
| latency_ms p50 proposed | 569.312 | 569.312 to 569.312 |
| latency_ms p50 delta | 83.5711 | 83.5711 to 83.5711 |
| latency_ms p95 current | 569.312 | 569.312 to 569.312 |
| latency_ms p95 proposed | 569.312 | 569.312 to 569.312 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.678571 | 0.673099 to 0.683673 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0.071429 | 0.066327 to 0.076901 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-flash-econ | glm-flash-east | 248 |
| glm-flash-econ | (rejected) | 4 |

### New rejections

| app | count |
|---|---|
| paper-claims | 4 |

| code | count |
|---|---|
| over_budget | 4 |

**By conflicting core**

| core | count |
|---|---|
| max_cost_usd_per_request | 4 |

Affected apps: paper-claims, paper-dev

## Scenario: all-apps-us-lab-only

requests: 900 &middot; route_changed: 296 &middot; newly_rejected: 5 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 7.17363 | 1.1723 |
| cost_usd (both routed) | 5.76409 | 7.17363 | 1.40954 |
| latency_ms p50 | 485.741 | 485.741 | 0 |
| latency_ms p95 | 569.312 | 485.741 | -83.5711 |
| quality_mean | 0.678571 | 0.675314 | -0.003258 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd proposed | 7.17363 | 6.6386 to 7.68388 |
| cost_usd delta | 1.1723 | 0.839082 to 1.50257 |
| cost_usd (both routed) current | 5.76409 | 5.31689 to 6.2001 |
| cost_usd (both routed) proposed | 7.17363 | 6.6386 to 7.68388 |
| cost_usd (both routed) delta | 1.40954 | 1.13213 to 1.67234 |
| route_changed | 296 | 277 to 314 |
| route_changed_share | 0.328889 | 0.307778 to 0.348889 |
| newly_rejected | 5 | 2 to 9 |
| newly_rejected_share | 0.005556 | 0.002222 to 0.01 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 485.741 | 485.741 to 485.741 |
| latency_ms p50 proposed | 485.741 | 485.741 to 485.741 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 569.312 | 569.312 to 569.312 |
| latency_ms p95 proposed | 485.741 | 485.741 to 485.741 |
| latency_ms p95 delta | -83.5711 | -83.5711 to -83.5711 |
| quality_mean current | 0.678571 | 0.673099 to 0.683673 |
| quality_mean proposed | 0.675314 | 0.669817 to 0.680524 |
| quality_mean delta | -0.003258 | -0.004782 to -0.001812 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-flash-east | glm-5.3-flash | 286 |
| glm-flash-east | glm-flash-econ | 10 |
| glm-flash-east | (rejected) | 5 |

### New rejections

| app | count |
|---|---|
| paper-claims | 1 |
| paper-dev | 4 |

| code | count |
|---|---|
| over_budget | 5 |

**By conflicting core**

| core | count |
|---|---|
| max_cost_usd_per_request+risk_floor | 5 |

Affected apps: paper-claims, paper-dev

## Scenario: clinic-may-use-eu-lab

requests: 900 &middot; route_changed: 329 &middot; newly_rejected: 0 &middot; newly_served: 10

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 5.37566 | -0.625674 |
| cost_usd (both routed) | 6.00133 | 4.96401 | -1.03733 |
| latency_ms p50 | 485.741 | 569.312 | 83.5711 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.679372 | 0.000801 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd proposed | 5.37566 | 4.91549 to 5.8089 |
| cost_usd delta | -0.625674 | -0.858088 to -0.35748 |
| cost_usd (both routed) current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd (both routed) proposed | 4.96401 | 4.54431 to 5.35359 |
| cost_usd (both routed) delta | -1.03733 | -1.15547 to -0.925896 |
| route_changed | 329 | 324 to 334 |
| route_changed_share | 0.365556 | 0.36 to 0.371111 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 10 | 5 to 15 |
| newly_served_share | 0.011111 | 0.005556 to 0.016667 |
| latency_ms p50 current | 485.741 | 485.741 to 485.741 |
| latency_ms p50 proposed | 569.312 | 569.312 to 569.312 |
| latency_ms p50 delta | 83.5711 | 83.5711 to 83.5711 |
| latency_ms p95 current | 569.312 | 569.312 to 569.312 |
| latency_ms p95 proposed | 569.312 | 569.312 to 569.312 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.678571 | 0.673099 to 0.683673 |
| quality_mean proposed | 0.679372 | 0.673852 to 0.68449 |
| quality_mean delta | 0.000801 | 0.000413 to 0.00123 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | glm-flash-east | 329 |
| (rejected) | glm-flash-east | 10 |

### New rejections

None.

Affected apps: paper-clinic

## Scenario: raise-request-budget

requests: 900 &middot; route_changed: 4 &middot; newly_rejected: 0 &middot; newly_served: 18

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 6.00133 | 7.45609 | 1.45476 |
| cost_usd (both routed) | 6.00133 | 6.15085 | 0.149515 |
| latency_ms p50 | 485.741 | 485.741 | 0 |
| latency_ms p95 | 569.312 | 569.312 | 0 |
| quality_mean | 0.678571 | 0.681111 | 0.00254 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd proposed | 7.45609 | 6.72378 to 8.14686 |
| cost_usd delta | 1.45476 | 0.924678 to 2.00507 |
| cost_usd (both routed) current | 6.00133 | 5.52547 to 6.44355 |
| cost_usd (both routed) proposed | 6.15085 | 5.64935 to 6.63865 |
| cost_usd (both routed) delta | 0.149515 | 0.04081 to 0.28036 |
| route_changed | 4 | 1 to 7 |
| route_changed_share | 0.004444 | 0.001111 to 0.007778 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 18 | 12 to 25 |
| newly_served_share | 0.02 | 0.013333 to 0.027778 |
| latency_ms p50 current | 485.741 | 485.741 to 485.741 |
| latency_ms p50 proposed | 485.741 | 485.741 to 485.741 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 569.312 | 569.312 to 569.312 |
| latency_ms p95 proposed | 569.312 | 569.312 to 569.312 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.678571 | 0.673099 to 0.683673 |
| quality_mean proposed | 0.681111 | 0.675556 to 0.686111 |
| quality_mean delta | 0.00254 | 0.001575 to 0.00365 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 900 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.256% (about 1 request in 391), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 12 |
| (rejected) | glm-flash-east | 6 |
| glm-flash-econ | glm-flash-east | 4 |

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
