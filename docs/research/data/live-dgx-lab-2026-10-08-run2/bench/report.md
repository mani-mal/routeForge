# FeasibleRouteBench report

## Run

- **mode**: live
- **seed**: 0
- **seconds_per_case**: 30
- **attempt_cap**: 3
- **suite_hash**: 3339c7735b5d22df84dbd3ec470ee91513c06c0d50ea033e2a99ac79a95d8199
- **endpoints_hash**: c6699fd63cee5482c5b1dc58ba6388b2c5568b529d3d738c0164f48ed4d8b2b8
- **base_routing_policy_version**: 6e6fe2027862
- **registry_version**: 68d0064b86ca
- **git_sha**: 7233174e34ef3c19b1b82287bb823b7eb4e20917
- **n_cases**: 22
- **started_at**: 2026-10-09T01:15:34Z
- **finished_at**: 2026-10-09T01:17:36Z
- **endpoints**: glm-5.3-flash, glm-flash-east, glm-flash-econ
- **completion text kept (--keep-text)**: no
- **exit code**: 0

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

`router_online` costs 8.08081% less than `best_published_quality` at comparable pass rate (0.727273 vs 0.727273, tolerance 0.02).

## Strategies

| strategy | violation_rate | pass_rate | quality_regret | cost_regret | latency_regret | floor_miss_rate | mean_chosen_cost | wasted_attempts | exhausted_rate |
|---|---|---|---|---|---|---|---|---|---|
| best_published_quality | 0 | 0.727273 | 0 | 0.000720313 | 295.611 | 0 | 0.00729618 | 0 | 0 |
| cheapest | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_cold | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_cold:candidate | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_online | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_online:breaker_off | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_online:candidate | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_warm_prior | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |
| router_warm_prior:candidate | 0 | 0.727273 | 0 | 0 | 190.952 | 0 | 0.00670659 | 0 | 0 |

## Worst groups

| strategy | metric | tag | value | metric value |
|---|---|---|---|---|
| best_published_quality | cost_regret | workload | onprem_required | 0.00241267 |
| best_published_quality | floor_miss_rate | workload | claim_summary | 0 |
| best_published_quality | latency_regret | workload | onprem_required | 1376.2 |
| best_published_quality | pass_rate | placement | us_only | 0.375 |
| best_published_quality | quality_regret | workload | claim_summary | 0 |
| cheapest | cost_regret | workload | claim_summary | 0 |
| cheapest | floor_miss_rate | workload | claim_summary | 0 |
| cheapest | latency_regret | workload | onprem_required | 679.112 |
| cheapest | pass_rate | placement | us_only | 0.375 |
| cheapest | quality_regret | workload | claim_summary | 0 |
| router_cold | cost_regret | workload | claim_summary | 0 |
| router_cold | floor_miss_rate | workload | claim_summary | 0 |
| router_cold | latency_regret | workload | onprem_required | 679.112 |
| router_cold | pass_rate | placement | us_only | 0.375 |
| router_cold | quality_regret | workload | claim_summary | 0 |
| router_cold:candidate | cost_regret | workload | claim_summary | 0 |
| router_cold:candidate | floor_miss_rate | workload | claim_summary | 0 |
| router_cold:candidate | latency_regret | workload | onprem_required | 679.112 |
| router_cold:candidate | pass_rate | placement | us_only | 0.375 |
| router_cold:candidate | quality_regret | workload | claim_summary | 0 |
| router_online | cost_regret | workload | claim_summary | 0 |
| router_online | floor_miss_rate | workload | claim_summary | 0 |
| router_online | latency_regret | workload | onprem_required | 679.112 |
| router_online | pass_rate | placement | us_only | 0.375 |
| router_online | quality_regret | workload | claim_summary | 0 |
| router_online:breaker_off | cost_regret | workload | claim_summary | 0 |
| router_online:breaker_off | floor_miss_rate | workload | claim_summary | 0 |
| router_online:breaker_off | latency_regret | workload | onprem_required | 679.112 |
| router_online:breaker_off | pass_rate | placement | us_only | 0.375 |
| router_online:breaker_off | quality_regret | workload | claim_summary | 0 |
| router_online:candidate | cost_regret | workload | claim_summary | 0 |
| router_online:candidate | floor_miss_rate | workload | claim_summary | 0 |
| router_online:candidate | latency_regret | workload | onprem_required | 679.112 |
| router_online:candidate | pass_rate | placement | us_only | 0.375 |
| router_online:candidate | quality_regret | workload | claim_summary | 0 |
| router_warm_prior | cost_regret | workload | claim_summary | 0 |
| router_warm_prior | floor_miss_rate | workload | claim_summary | 0 |
| router_warm_prior | latency_regret | workload | onprem_required | 679.112 |
| router_warm_prior | pass_rate | placement | us_only | 0.375 |
| router_warm_prior | quality_regret | workload | claim_summary | 0 |
| router_warm_prior:candidate | cost_regret | workload | claim_summary | 0 |
| router_warm_prior:candidate | floor_miss_rate | workload | claim_summary | 0 |
| router_warm_prior:candidate | latency_regret | workload | onprem_required | 679.112 |
| router_warm_prior:candidate | pass_rate | placement | us_only | 0.375 |
| router_warm_prior:candidate | quality_regret | workload | claim_summary | 0 |

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
| n/a | n/a | n/a | n/a | n/a | no expected or actual empty-set case |

## Deferred metrics

- calibration (the router emits no confidence)
- policy/endpoint churn robustness
- noninterference failure rate (owned by P1a's policy-compiler harness)
