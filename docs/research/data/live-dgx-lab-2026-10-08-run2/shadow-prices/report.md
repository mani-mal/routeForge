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
| max_cost_usd_per_request | app:paper-clinic | 341 | 0 | 12 | 0 | 0 (both-routed) | 0 | 0 | 1→1 |
| max_cost_usd_per_request | app:paper-dev | 290 | 0 | 3 | 0 | 0 (both-routed) | 0.001289 | 0 | 1→1 |
| max_cost_usd_per_request | app:paper-claims | 269 | 4 | 3 | 0 | 0.149515 (both-routed) | 0.00486 | 0 | 1→1 |

### Cost of protection

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) | label |
|---|---|---|---|---|---|---|---|---|---|---|
| allowed_regions | app:paper-clinic | 341 | 329 | 10 | 0 | -1.03733 (both-routed) | 0 | 83.5711 | 1→1 | price of a protection — not a recommendation |
| require_no_retention | app:paper-clinic | 341 | 78 | 8 | 0 | -0.163224 (both-routed) | -0.063798 | 0 | 1→1 | price of a protection — not a recommendation |
| risk_floor | app:paper-dev | 290 | 36 | 3 | 0 | -0.102005 (both-routed) | -0.032332 | -148.679 | 1→1 | price of a protection — not a recommendation |
| risk_floor | app:paper-claims | 269 | 39 | 3 | 0 | -0.09216 (both-routed) | -0.037891 | -148.679 | 1→1 | price of a protection — not a recommendation |

Relaxing a constraint can newly reject a row: a wider allowed set can lift the target to the risk floor, and the budget fallback never goes below the floor.

### Ranges

`[lo, hi]` is the 90% range of the figure before it: sampling variability only (see "Uncertainty ranges").

| requirement | scope | rows | cost saved/day (or Δcost both-routed) | rejections avoided | newly rejected | route changed | label |
|---|---|---|---|---|---|---|---|
| allowed_regions | app:paper-clinic | 341 | -1.03733 (both-routed) [-1.15547, -0.925896] | 10 [5, 15] | 0 [0, 0] | 329 [324, 334] | price of a protection — not a recommendation |
| require_no_retention | app:paper-clinic | 341 | -0.163224 (both-routed) [-0.198099, -0.128889] | 8 [4, 13] | 0 [0, 0] | 78 [65, 90] | price of a protection — not a recommendation |
| risk_floor | app:paper-dev | 290 | -0.102005 (both-routed) [-0.13477, -0.07256] | 3 [1, 6] | 0 [0, 0] | 36 [27, 46] | price of a protection — not a recommendation |
| risk_floor | app:paper-claims | 269 | -0.09216 (both-routed) [-0.11841, -0.066785] | 3 [1, 6] | 0 [0, 0] | 39 [30, 49] | price of a protection — not a recommendation |
| max_cost_usd_per_request | app:paper-clinic | 341 | 0 (both-routed) [0, 0] | 12 [7, 17] | 0 [0, 0] | 0 [0, 0] |  |
| max_cost_usd_per_request | app:paper-dev | 290 | 0 (both-routed) [0, 0] | 3 [1, 6] | 0 [0, 0] | 0 [0, 0] |  |
| max_cost_usd_per_request | app:paper-claims | 269 | 0.149515 (both-routed) [0.04081, 0.28036] | 3 [1, 6] | 0 [0, 0] | 4 [1, 7] |  |

A count of 0 shows `[0, 0]` at any sample size; that is not zero risk.

A relaxation scoped to one application is resampled as that application's rows; an endpoint-scoped one over every row, with small applications resampled together (`totals_strata` on each entry of `shadow_prices.uncertainty.constraints`). Resampling within strata leaves a total about the `variance_factor` share of its variance (analytic, equal variance assumed, not measured); ranges are not corrected for it.

Non-binding: 9 · skipped by cap: 0
