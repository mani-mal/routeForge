> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

# Scenario comparison: DGX lab live run 2026-10-08 (three paper apps, glm-5.3-flash, 912 rows)

912 rows simulated, 6 scenario(s), one shared current arm.

## Baseline agreement

**Baseline agreement: 100.0%** over 876 compared rows.

Per app: paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Outcome agreement: 100.0%** over 912 simulated rows (routed or rejected, against each row's recorded status).

Per app (outcome): paper-claims 100.0% · paper-clinic 100.0% · paper-dev 100.0%

**Note:** 36 row(s) had no backend token count (rejected, failed before a response, or served by a backend that reported no usage). Replay sizes and prices them from RouteForge's own routing token count and output-token estimate -- an estimate, not a bill.

## Comparison

| scenario | Δcost/day USD | Δcost USD | route changed | newly rejected | newly served | Δp50 ms | Δp95 ms | Δquality | on-prem share (current→proposed) | overflow hops | affected apps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no-change | n/a | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 0 | none |
| raise-request-budget | n/a | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 36 [27, 46] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 0 | paper-claims, paper-clinic, paper-dev |
| tighten-claims-budget-0.02 | n/a | 0 [0, 0] | 0 [0, 0] | 29 [21, 38] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 0 | paper-claims |
| smaller-prompts-0.6 | n/a | -3.12994 [-3.34447, -2.91226] | 0 [0, 0] | 0 [0, 0] | 18 [12, 25] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 0 | paper-claims, paper-clinic, paper-dev |
| window-4000 | n/a | 0 [0, 0] | 0 [0, 0] | 26 [18, 35] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 26 | paper-claims, paper-clinic, paper-dev |
| clinic-eu-only | n/a | 0 [0, 0] | 0 [0, 0] | 298 [292, 303] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 1→1 | 0 | paper-clinic |

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

36 of 912 simulated rows have no backend token count (`estimated_token_rows`): replay sizes and prices them from RouteForge's own estimate, the same in every resample.

Totals strata: 3 application(s) resampled on their own and 0 with fewer than 30 rows resampled together (0 rows); the smallest stratum has 295 rows. Resampling within strata leaves the totals about 0.996711 of their variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking: overlapping ranges do not make two scenarios equal, and separate ranges do not make one of them the one to choose.

- **no-change** -- Quality uncertainty covers 0 of 876 routed rows in the current arm and 0 of 876 in the proposed arm; the rest use a point prior.
- **raise-request-budget** -- Quality uncertainty covers 0 of 876 routed rows in the current arm and 0 of 912 in the proposed arm; the rest use a point prior.
- **tighten-claims-budget-0.02** -- Quality uncertainty covers 0 of 876 routed rows in the current arm and 0 of 847 in the proposed arm; the rest use a point prior.
- **smaller-prompts-0.6** -- Quality uncertainty covers 0 of 876 routed rows in the current arm and 0 of 894 in the proposed arm; the rest use a point prior.
- **window-4000** -- Quality uncertainty covers 0 of 876 routed rows in the current arm and 0 of 850 in the proposed arm; the rest use a point prior.
- **clinic-eu-only** -- Quality uncertainty covers 0 of 876 routed rows in the current arm and 0 of 578 in the proposed arm; the rest use a point prior.

## Assumptions

- **smaller-prompts-0.6** -- token_scale: paper-claims=0.6, paper-clinic=0.6, paper-dev=0.6; rows whose saturated complexity was estimated from scaled prompt tokens: 125

## Scenario: no-change

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 8.4686 | 0 |
| cost_usd (both routed) | 8.4686 | 8.4686 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd proposed | 8.4686 | 7.93007 to 9.00707 |
| cost_usd delta | 0 | 0 to 0 |
| cost_usd (both routed) current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd (both routed) proposed | 8.4686 | 7.93007 to 9.00707 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 497.349 | 497.349 to 497.349 |
| latency_ms p50 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 497.349 | 497.349 to 497.349 |
| latency_ms p95 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.75 | 0.75 to 0.75 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 912 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.252% (about 1 request in 397), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

None.

### New rejections

None.

Affected apps: none

## Scenario: raise-request-budget

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 36

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 11.624 | 3.15544 |
| cost_usd (both routed) | 8.4686 | 8.4686 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd proposed | 11.624 | 10.7133 to 12.6376 |
| cost_usd delta | 3.15544 | 2.33638 to 4.10164 |
| cost_usd (both routed) current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd (both routed) proposed | 8.4686 | 7.93007 to 9.00707 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 36 | 27 to 46 |
| newly_served_share | 0.039474 | 0.029605 to 0.050439 |
| latency_ms p50 current | 497.349 | 497.349 to 497.349 |
| latency_ms p50 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 497.349 | 497.349 to 497.349 |
| latency_ms p95 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.75 | 0.75 to 0.75 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 912 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.252% (about 1 request in 397), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 36 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

## Scenario: tighten-claims-budget-0.02

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 29 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 7.78665 | -0.68195 |
| cost_usd (both routed) | 7.78665 | 7.78665 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd proposed | 7.78665 | 7.30609 to 8.29026 |
| cost_usd delta | -0.68195 | -0.93602 to -0.46281 |
| cost_usd (both routed) current | 7.78665 | 7.30609 to 8.29026 |
| cost_usd (both routed) proposed | 7.78665 | 7.30609 to 8.29026 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 29 | 21 to 38 |
| newly_rejected_share | 0.031798 | 0.023026 to 0.041667 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 497.349 | 497.349 to 497.349 |
| latency_ms p50 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 497.349 | 497.349 to 497.349 |
| latency_ms p95 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.75 | 0.75 to 0.75 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | (rejected) | 29 |

### New rejections

| app | count |
|---|---|
| paper-claims | 29 |

| code | count |
|---|---|
| over_budget | 29 |

**By conflicting core**

| core | count |
|---|---|
| max_cost_usd_per_request | 29 |

Affected apps: paper-claims

## Scenario: smaller-prompts-0.6

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 0 &middot; newly_served: 18

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 6.03094 | -2.43766 |
| cost_usd (both routed) | 8.4686 | 5.33866 | -3.12994 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd proposed | 6.03094 | 5.65382 to 6.45598 |
| cost_usd delta | -2.43766 | -2.77179 to -2.06946 |
| cost_usd (both routed) current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd (both routed) proposed | 5.33866 | 5.01781 to 5.66433 |
| cost_usd (both routed) delta | -3.12994 | -3.34447 to -2.91226 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 0 | 0 to 0 |
| newly_rejected_share | 0 | 0 to 0 |
| newly_served | 18 | 12 to 25 |
| newly_served_share | 0.019737 | 0.013158 to 0.027412 |
| latency_ms p50 current | 497.349 | 497.349 to 497.349 |
| latency_ms p50 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 497.349 | 497.349 to 497.349 |
| latency_ms p95 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.75 | 0.75 to 0.75 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

newly_rejected is 0 in all 912 rows. That is not zero risk: at the 90% level these rows are consistent with a replayed newly-rejected rate of up to 0.252% (about 1 request in 397), if the rows are independent draws from the same traffic. The bound is one-sided, and it is a bound of the rate under this replay, not of the live rate. Correlated rows, such as the calls of one agent loop, make the true bound larger.

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| (rejected) | glm-5.3-flash | 18 |

### New rejections

None.

Affected apps: paper-claims, paper-clinic, paper-dev

## Scenario: window-4000

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 26 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 7.09042 | -1.37818 |
| cost_usd (both routed) | 7.09042 | 7.09042 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 26 | 26 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd proposed | 7.09042 | 6.7166 to 7.47506 |
| cost_usd delta | -1.37818 | -1.83639 to -0.94356 |
| cost_usd (both routed) current | 7.09042 | 6.7166 to 7.47506 |
| cost_usd (both routed) proposed | 7.09042 | 6.7166 to 7.47506 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 26 | 18 to 35 |
| newly_rejected_share | 0.028509 | 0.019737 to 0.038377 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 497.349 | 497.349 to 497.349 |
| latency_ms p50 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 497.349 | 497.349 to 497.349 |
| latency_ms p95 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.75 | 0.75 to 0.75 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | (rejected) | 26 |

### New rejections

| app | count |
|---|---|
| paper-claims | 3 |
| paper-clinic | 9 |
| paper-dev | 14 |

| code | count |
|---|---|
| context_window | 26 |

**By conflicting core**

| core | count |
|---|---|
| (context_window) | 26 |

Affected apps: paper-claims, paper-clinic, paper-dev

## Scenario: clinic-eu-only

requests: 912 &middot; route_changed: 0 &middot; newly_rejected: 298 &middot; newly_served: 0

| metric | current | proposed | delta |
|---|---|---|---|
| cost_usd | 8.4686 | 5.70204 | -2.76656 |
| cost_usd (both routed) | 5.70204 | 5.70204 | 0 |
| latency_ms p50 | 497.349 | 497.349 | 0 |
| latency_ms p95 | 497.349 | 497.349 | 0 |
| quality_mean | 0.75 | 0.75 | 0 |
| onprem_share | 1 | 1 | n/a |
| overflow_hops | 0 | 0 | 0 |

### Ranges

| figure | point | 90% range |
|---|---|---|
| cost_usd current | 8.4686 | 7.93007 to 9.00707 |
| cost_usd proposed | 5.70204 | 5.31141 to 6.13754 |
| cost_usd delta | -2.76656 | -3.11127 to -2.44843 |
| cost_usd (both routed) current | 5.70204 | 5.31141 to 6.13754 |
| cost_usd (both routed) proposed | 5.70204 | 5.31141 to 6.13754 |
| cost_usd (both routed) delta | 0 | 0 to 0 |
| route_changed | 0 | 0 to 0 |
| route_changed_share | 0 | 0 to 0 |
| newly_rejected | 298 | 292 to 303 |
| newly_rejected_share | 0.326754 | 0.320175 to 0.332237 |
| newly_served | 0 | 0 to 0 |
| newly_served_share | 0 | 0 to 0 |
| latency_ms p50 current | 497.349 | 497.349 to 497.349 |
| latency_ms p50 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p50 delta | 0 | 0 to 0 |
| latency_ms p95 current | 497.349 | 497.349 to 497.349 |
| latency_ms p95 proposed | 497.349 | 497.349 to 497.349 |
| latency_ms p95 delta | 0 | 0 to 0 |
| quality_mean current | 0.75 | 0.75 to 0.75 |
| quality_mean proposed | 0.75 | 0.75 to 0.75 |
| quality_mean delta | 0 | 0 to 0 |
| onprem_share current | 1 | 1 to 1 |
| onprem_share proposed | 1 | 1 to 1 |

A share of exactly 1 shows `1 to 1` at any sample size; that is not certainty: `one_bound` in the JSON is the one-sided lower limit these rows are consistent with if they are independent draws, and it says nothing about a share a policy fixes by construction.

Per-app ranges are in `comparison.json`.

### Moves

| from | to | count |
|---|---|---|
| glm-5.3-flash | (rejected) | 298 |

### New rejections

| app | count |
|---|---|
| paper-clinic | 298 |

| code | count |
|---|---|
| region_not_allowed | 298 |

**By conflicting core**

| core | count |
|---|---|
| allowed_regions | 298 |

Affected apps: paper-clinic

## Excluded rows

| reason | count |
|---|---|
| before_since | 0 |
| cache_hit | 0 |
| missing_features | 0 |
| missing_routing_tokens | 0 |
| unknown_app | 0 |
| unknown_status | 0 |
