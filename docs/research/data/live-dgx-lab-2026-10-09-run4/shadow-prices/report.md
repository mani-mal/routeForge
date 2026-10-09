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
| max_cost_usd_per_request | app:paper-claims | 275 | 0 | 7 | 0 | 0 (both-routed) | 0.001496 | 0 | 0.682836→0.690909 |
| max_cost_usd_per_request | app:paper-dev | 309 | 0 | 6 | 0 | 0 (both-routed) | 0.001218 | 0 | 0.693069→0.699029 |
| max_cost_usd_per_request | app:paper-clinic | 316 | 15 | 9 | 0 | 0.760635 (both-routed) | 0.013978 | 0 | 1→1 |

### Cost of protection

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) | label |
|---|---|---|---|---|---|---|---|---|---|---|
| residency | app:paper-dev | 309 | 84 | 6 | 0 | -1.47455 (both-routed) | 0.074033 | 2074.78 | 0.693069→0.407767 | price of a protection — not a recommendation |
| residency | app:paper-claims | 275 | 76 | 7 | 0 | -1.25736 (both-routed) | 0.07695 | 2074.78 | 0.682836→0.389091 | price of a protection — not a recommendation |
| risk_floor | app:paper-clinic | 316 | 92 | 9 | 0 | -0.665451 (both-routed) | -0.077794 | -425.961 | 1→1 | price of a protection — not a recommendation |
| risk_floor | app:paper-dev | 309 | 44 | 6 | 0 | -0.407079 (both-routed) | -0.039235 | -425.961 | 0.693069→0.699029 | price of a protection — not a recommendation |
| risk_floor | app:paper-claims | 275 | 37 | 7 | 0 | -0.305307 (both-routed) | -0.038504 | -425.961 | 0.682836→0.690909 | price of a protection — not a recommendation |

Relaxing a constraint can newly reject a row: a wider allowed set can lift the target to the risk floor, and the budget fallback never goes below the floor.

### Ranges

`[lo, hi]` is the 90% range of the figure before it: sampling variability only (see "Uncertainty ranges").

| requirement | scope | rows | cost saved/day (or Δcost both-routed) | rejections avoided | newly rejected | route changed | label |
|---|---|---|---|---|---|---|---|
| residency | app:paper-dev | 309 | -1.47455 (both-routed) [-1.76944, -1.20092] | 6 [2, 10] | 0 [0, 0] | 84 [71, 96] | price of a protection — not a recommendation |
| residency | app:paper-claims | 275 | -1.25736 (both-routed) [-1.5083, -0.99361] | 7 [3, 12] | 0 [0, 0] | 76 [64, 88] | price of a protection — not a recommendation |
| risk_floor | app:paper-clinic | 316 | -0.665451 (both-routed) [-0.785412, -0.552888] | 9 [4, 14] | 0 [0, 0] | 92 [79, 105] | price of a protection — not a recommendation |
| risk_floor | app:paper-dev | 309 | -0.407079 (both-routed) [-0.506556, -0.313713] | 6 [2, 10] | 0 [0, 0] | 44 [35, 54] | price of a protection — not a recommendation |
| risk_floor | app:paper-claims | 275 | -0.305307 (both-routed) [-0.391248, -0.227808] | 7 [3, 12] | 0 [0, 0] | 37 [28, 47] | price of a protection — not a recommendation |
| max_cost_usd_per_request | app:paper-claims | 275 | 0 (both-routed) [0, 0] | 7 [3, 12] | 0 [0, 0] | 0 [0, 0] |  |
| max_cost_usd_per_request | app:paper-dev | 309 | 0 (both-routed) [0, 0] | 6 [2, 10] | 0 [0, 0] | 0 [0, 0] |  |
| max_cost_usd_per_request | app:paper-clinic | 316 | 0.760635 (both-routed) [0.463374, 1.14966] | 9 [4, 14] | 0 [0, 0] | 15 [9, 22] |  |

A count of 0 shows `[0, 0]` at any sample size; that is not zero risk.

A relaxation scoped to one application is resampled as that application's rows; an endpoint-scoped one over every row, with small applications resampled together (`totals_strata` on each entry of `shadow_prices.uncertainty.constraints`). Resampling within strata leaves a total about the `variance_factor` share of its variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

Non-binding: 9 · skipped by cap: 0
