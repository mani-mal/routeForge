## How to read this

- **B45 (is the availability weight large enough?) is read from `router_online:<variant>`** -- never from `router_warm_prior`, which is a full-information reference (not an upper bound), nor `router_cold`.
- router_cold scores every case with an empty signal store: the no-measurement baseline.
- router_online is the reported arm: its store holds only the attempts this arm itself made on earlier cases (failovers included), applied after each case is scored; an error counts only when production's counts_as_endpoint_unhealth would count it. B45 is read from router_online:<variant>.
- router_warm_prior replays every matrix row of every earlier case (all eligible endpoints) before scoring a case: a full-information reference (not an upper bound), never the reported number.
- router_online:breaker_off is the control: the same online arm with the step-7 circuit breaker disabled.
- router_online and router_warm_prior depend on the suite's case order: a case is scored with what earlier cases taught the arm, and outages are indexed by case position.
- Both non-cold arms use a synthetic case clock: case k happens at t_k = k x seconds_per_case (1 ms sub-steps within a case) and is scored reading at t_k, so availability decay (half_life_s) and latency staleness depend on seconds_per_case -- quote it with any number. The step-7 breaker (router_online, unless breaker_off) runs on the SAME case clock, so seconds_per_case also sets how much of an open cooldown a case gap actually covers: at seconds_per_case == cooldown_s (30 by default) the FIRST open period ends exactly at the next case (its read lands at t >= open_until, i.e. already half-open), so that first cooldown is invisible in the chosen chains -- only a later, doubled cooldown can span a full case gap. At seconds_per_case >= max_cooldown_s no cooldown, however many times it has doubled, can ever outlast one case gap, so the breaker never excludes anything: router_online and router_online:breaker_off produce identical chains (a documented no-op, not a bug -- see TestBreakerCaseClockInteraction).
- A failed attempt with unknown failure latency (e.g. a live failure) counts as 0 ms in latency figures.
- policy.violations_total counts ATTEMPTS (every attempt outside the case's eligible set, plus sweep calls); each strategy's violation_rate is per CASE (at most one per case).
- This run used **seconds_per_case = 30**; the online and warm-prior arms' numbers change with it and with the case order.

## Headline

Not comparable -- no cost claim is made. `router_online` pass rate n/a vs `best_published_quality` n/a (parity tolerance 0.02); a claim also needs a defined, non-zero reference cost.

## Strategies

| strategy | violation_rate | pass_rate | quality_regret | cost_regret | latency_regret | floor_miss_rate | mean_chosen_cost | wasted_attempts | exhausted_rate |
|---|---|---|---|---|---|---|---|---|---|
| best_published_quality | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| cheapest | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| router_cold | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| router_online | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| router_online:breaker_off | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| router_warm_prior | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## Worst groups

| strategy | metric | tag | value | metric value |
|---|---|---|---|---|
| best_published_quality | cost_regret | n/a | n/a | n/a |
| best_published_quality | floor_miss_rate | n/a | n/a | n/a |
| best_published_quality | latency_regret | n/a | n/a | n/a |
| best_published_quality | pass_rate | n/a | n/a | n/a |
| best_published_quality | quality_regret | n/a | n/a | n/a |
| cheapest | cost_regret | n/a | n/a | n/a |
| cheapest | floor_miss_rate | n/a | n/a | n/a |
| cheapest | latency_regret | n/a | n/a | n/a |
| cheapest | pass_rate | n/a | n/a | n/a |
| cheapest | quality_regret | n/a | n/a | n/a |
| router_cold | cost_regret | n/a | n/a | n/a |
| router_cold | floor_miss_rate | n/a | n/a | n/a |
| router_cold | latency_regret | n/a | n/a | n/a |
| router_cold | pass_rate | n/a | n/a | n/a |
| router_cold | quality_regret | n/a | n/a | n/a |
| router_online | cost_regret | n/a | n/a | n/a |
| router_online | floor_miss_rate | n/a | n/a | n/a |
| router_online | latency_regret | n/a | n/a | n/a |
| router_online | pass_rate | n/a | n/a | n/a |
| router_online | quality_regret | n/a | n/a | n/a |
| router_online:breaker_off | cost_regret | n/a | n/a | n/a |
| router_online:breaker_off | floor_miss_rate | n/a | n/a | n/a |
| router_online:breaker_off | latency_regret | n/a | n/a | n/a |
| router_online:breaker_off | pass_rate | n/a | n/a | n/a |
| router_online:breaker_off | quality_regret | n/a | n/a | n/a |
| router_warm_prior | cost_regret | n/a | n/a | n/a |
| router_warm_prior | floor_miss_rate | n/a | n/a | n/a |
| router_warm_prior | latency_regret | n/a | n/a | n/a |
| router_warm_prior | pass_rate | n/a | n/a | n/a |
| router_warm_prior | quality_regret | n/a | n/a | n/a |

## Policy

`violations_total` counts attempts; `violation_rate` (above) is per case.

- **violations_total**: 0
- **sweep_violations**: 0
- **empty_set_mismatches**: 0
- **post_gate_widened**: 0
- **pinned_mismatches**: 0
- **sweep_generate_mismatches**: 0

## Empty-set cases

| case | expect_empty | eligible | rejected_ok | calls | exclusions |
|---|---|---|---|---|---|
| onprem-empty-001 | yes | 0 | yes | 0 | cloud-a (residency), cloud-b (residency) |
| onprem-empty-002 | yes | 0 | yes | 0 | cloud-a (residency), cloud-b (residency) |
| onprem-empty-003 | yes | 0 | yes | 0 | cloud-a (residency), cloud-b (residency) |
| onprem-empty-004 | yes | 0 | yes | 0 | cloud-a (residency), cloud-b (residency) |

## Deferred metrics

- calibration (the router emits no confidence)
- policy/endpoint churn robustness
- noninterference failure rate (owned by P1a's policy-compiler harness)
