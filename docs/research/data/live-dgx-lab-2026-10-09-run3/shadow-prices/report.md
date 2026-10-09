> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

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

## Shadow prices

> **What each constraint costs.** Each row relaxes ONE constraint for one app (or endpoint) and
> replays the same recorded traffic. Nothing is applied. Security constraints are shown as the
> cost of a protection — they are never a recommendation to relax them.

### Operational constraints

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) |
|---|---|---|---|---|---|---|---|---|---|
| max_cost_usd_per_request | app:paper-claims | 301 | 0 | 5 | 0 | 0 (both-routed) | 0.000547 | 0 | 0.648649→0.654485 |
| max_cost_usd_per_request | app:paper-dev | 273 | 0 | 4 | 0 | 0 (both-routed) | 0.001239 | 0 | 0.717472→0.721612 |
| max_cost_usd_per_request | app:paper-clinic | 325 | 6 | 12 | 0 | 0.221706 (both-routed) | 0.006709 | 0 | 1→1 |

### Cost of protection

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) | label |
|---|---|---|---|---|---|---|---|---|---|---|
| residency | app:paper-claims | 301 | 96 | 5 | 0 | -1.53287 (both-routed) | 0.084434 | 15205.8 | 0.648649→0.318937 | price of a protection — not a recommendation |
| residency | app:paper-dev | 273 | 68 | 4 | 0 | -1.18793 (both-routed) | 0.067173 | 3312.27 | 0.717472→0.457875 | price of a protection — not a recommendation |
| risk_floor | app:paper-clinic | 325 | 100 | 12 | 0 | -0.696519 (both-routed) | -0.08406 | -472.75 | 1→1 | price of a protection — not a recommendation |
| risk_floor | app:paper-claims | 301 | 51 | 5 | 0 | -0.381303 (both-routed) | -0.045964 | -472.75 | 0.648649→0.654485 | price of a protection — not a recommendation |
| risk_floor | app:paper-dev | 273 | 36 | 4 | 0 | -0.319806 (both-routed) | -0.035391 | -472.75 | 0.717472→0.721612 | price of a protection — not a recommendation |

Relaxing a constraint can newly reject a row: a wider allowed set can lift the target to the risk floor, and the budget fallback never goes below the floor.

### Ranges

`[lo, hi]` is the 90% range of the figure before it: sampling variability only (see "Uncertainty ranges").

| requirement | scope | rows | cost saved/day (or Δcost both-routed) | rejections avoided | newly rejected | route changed | label |
|---|---|---|---|---|---|---|---|
| residency | app:paper-claims | 301 | -1.53287 (both-routed) [-1.83535, -1.25975] | 5 [2, 9] | 0 [0, 0] | 96 [83, 110] | price of a protection — not a recommendation |
| residency | app:paper-dev | 273 | -1.18793 (both-routed) [-1.48017, -0.916086] | 4 [1, 7] | 0 [0, 0] | 68 [56, 79] | price of a protection — not a recommendation |
| risk_floor | app:paper-clinic | 325 | -0.696519 (both-routed) [-0.814392, -0.58401] | 12 [7, 17] | 0 [0, 0] | 100 [85, 115] | price of a protection — not a recommendation |
| risk_floor | app:paper-claims | 301 | -0.381303 (both-routed) [-0.477288, -0.294147] | 5 [2, 9] | 0 [0, 0] | 51 [41, 62] | price of a protection — not a recommendation |
| risk_floor | app:paper-dev | 273 | -0.319806 (both-routed) [-0.406917, -0.237924] | 4 [1, 7] | 0 [0, 0] | 36 [27, 45] | price of a protection — not a recommendation |
| max_cost_usd_per_request | app:paper-claims | 301 | 0 (both-routed) [0, 0] | 5 [2, 9] | 0 [0, 0] | 0 [0, 0] |  |
| max_cost_usd_per_request | app:paper-dev | 273 | 0 (both-routed) [0, 0] | 4 [1, 7] | 0 [0, 0] | 0 [0, 0] |  |
| max_cost_usd_per_request | app:paper-clinic | 325 | 0.221706 (both-routed) [0.074646, 0.376803] | 12 [7, 17] | 0 [0, 0] | 6 [2, 10] |  |

A count of 0 shows `[0, 0]` at any sample size; that is not zero risk.

A relaxation scoped to one application is resampled as that application's rows; an endpoint-scoped one over every row, with small applications resampled together (`totals_strata` on each entry of `shadow_prices.uncertainty.constraints`). Resampling within strata leaves a total about the `variance_factor` share of its variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

Non-binding: 9 · skipped by cap: 0
