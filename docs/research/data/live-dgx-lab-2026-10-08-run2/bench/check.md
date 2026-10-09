# Router-version check (offline)

> This check applies nothing and promotes nothing. It reads a candidate routing config, scores it offline beside the current one, and writes this report. A routing change is still made by an operator editing config/routing.yaml.
> This run called the endpoints in the endpoint file, once per case and endpoint, and graded each answer with a deterministic grader. One call is one sample.
> The router comparison is mostly the tier rule: the target tier is chosen from the request's length, so on short cases the router's regret against the oracle is set by which tier it may use, not by the four-term scorer.
> Cases are synthetic and single-turn.
> The verdict is conditional on this suite, this endpoint file, the run's seed, the resampling seed and replicate count, and this case order. It is not a statement about live traffic.
> A range is the spread of the paired difference when the suite's cases are resampled. It says how much the difference depends on which cases are in the suite, and nothing about whether the suite resembles real traffic. A figure is judged on its whole range: its upper end is a one-sided bound at half the interval's miss rate (95% for a 90% interval).
> No correction is made for the number of figures judged. Every named metric is judged overall and in every group with enough cases, each at the same level, so the more figures are judged the more likely one is inconclusive by chance alone.
> Cost regret and latency regret are means over the cases whose served answer passed, and that set can differ between the two arms. A candidate that fails more cases can show a lower regret for that reason alone: read them beside pass_rate.

## Verdict

**PASS** (exit 0). No hard failure, and every named metric is within its tolerance, range included, overall and in every group with enough cases.

Figures judged: 16 (2 of 8 metrics have a tolerance; the rest are shown, not judged).

- **baseline arm**: `router_online` (the routing config as loaded)
- **candidate arm**: `router_online:candidate` (id `6af4e16f72b8`, name `latency-only`)
- **candidate overlay**: `{"weights": {"cost": 0, "latency": 2}}`
- **cases whose attempt chain changed**: 0 of 22
- **tolerances**: mean_chosen_cost 0.0005, pass_rate 0.05
- **range**: 90% percentile interval of the paired difference, 1000 resamples of the cases, seed 0
- **minimum cases behind a judged figure**: 5

## Hard checks (no tolerance)

- **candidate attempts outside the eligible set (cases)**: 0
- **of those, on a case whose feasible set is empty**: 0
- **bench run exit code**: 0

## Overall

`worsening` is candidate minus baseline in the direction that is worse for that metric (for `pass_rate`, baseline minus candidate). Positive is worse.

| scope | metric | baseline | candidate | worsening | range | change | tolerance | state |
|---|---|---|---|---|---|---|---|---|
| overall | pass_rate | 0.727273 | 0.727273 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| overall | quality_regret | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |
| overall | cost_regret | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |
| overall | latency_regret | 190.952 | 190.952 | 0 | [0, 0] | none | n/a | not_checked |
| overall | floor_miss_rate | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |
| overall | mean_chosen_cost | 0.00670659 | 0.00670659 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| overall | wasted_attempts | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |
| overall | exhausted_rate | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |

## Groups

Only the metrics a tolerance was named for; `check.json` holds every metric for every group.

| scope | metric | baseline | candidate | worsening | range | change | tolerance | state |
|---|---|---|---|---|---|---|---|---|
| placement=any | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=any | mean_chosen_cost | 0.000103125 | 0.000103125 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| placement=escalated | all | n/a | n/a | insufficient cases (n = 2) | n/a | n/a | n/a | insufficient_cases |
| placement=onprem_only | all | n/a | n/a | insufficient cases (n = 4) | n/a | n/a | n/a | insufficient_cases |
| placement=us_only | pass_rate | 0.375 | 0.375 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=us_only | mean_chosen_cost | 0.0145588 | 0.0145588 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| protection=enforced | pass_rate | 0.727273 | 0.727273 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| protection=enforced | mean_chosen_cost | 0.00670659 | 0.00670659 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| application=policy-57810d29 | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-57810d29 | mean_chosen_cost | 0.000103125 | 0.000103125 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| application=policy-7b90407f | pass_rate | 0.375 | 0.375 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-7b90407f | mean_chosen_cost | 0.0145588 | 0.0145588 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| application=policy-8fb5374a | all | n/a | n/a | insufficient cases (n = 4) | n/a | n/a | n/a | insufficient_cases |
| application=policy-d30395cb | all | n/a | n/a | insufficient cases (n = 2) | n/a | n/a | n/a | insufficient_cases |
| workload=claim_summary | pass_rate | 0.5 | 0.5 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=claim_summary | mean_chosen_cost | 0.014551 | 0.014551 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| workload=faq | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=faq | mean_chosen_cost | 0.000103125 | 0.000103125 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| workload=onprem_required | all | n/a | n/a | insufficient cases (n = 4) | n/a | n/a | n/a | insufficient_cases |
