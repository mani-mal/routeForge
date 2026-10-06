> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

## Shadow prices

> **What each constraint costs.** Each row relaxes ONE constraint for one app (or endpoint) and
> replays the same recorded traffic. Nothing is applied. Security constraints are shown as the
> cost of a protection — they are never a recommendation to relax them.

### Operational constraints

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) |
|---|---|---|---|---|---|---|---|---|---|
| allowed_tiers | app:big | 60 | 20 | 0 | 0 | 0.00056/day | -0.083333 | 0 | 0→0.333333 |
| allowed_tiers | app:few | 6 | 2 | 0 | 0 | 4.9e-05/day | -0.083333 | 0 | 0→0.333333 |

### Cost of protection

| requirement | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day (or Δcost both-routed) | Δquality | Δp50 ms | on-prem share (baseline→relaxed) | label |
|---|---|---|---|---|---|---|---|---|---|---|
| residency | app:safe | 45 | 30 | 0 | 0 | 0.01197/day | 0 | 0 | 1→0.333333 | price of a protection — not a recommendation |

Relaxing a constraint can newly reject a row: a wider allowed set can lift the target to the risk floor, and the budget fallback never goes below the floor.

Non-binding: 3 · skipped by cap: 0
