# FeasibleRouteBench group report

> This run called the endpoints in the endpoint file, once per case and endpoint, and graded each answer with a deterministic grader. One call is one sample.
> The router comparison is mostly the tier rule: the target tier is chosen from the request's length, so on short cases the router's regret against the oracle is set by which tier it may use, not by the four-term scorer.
> Cases are synthetic and single-turn.
> A group is the cases of this suite that carry one label. A figure over fewer cases than the minimum prints its case count and no rate. A group above the minimum is still small: read its case count with every figure.
> The worst group is the extreme of many small-sample figures, so it is worse than a typical group partly by selection. The same cases can carry several labels, so one set of cases can appear under more than one dimension; a tie is listed, not broken.
> Placement, protection and workload labels are the case author's tags. They are not read from the compiled policy and can differ from it. An application with no tag is a fingerprint of the case's policy as written.

Minimum cases behind a printed rate: **5** (`--min-group-size`). Cases in this run: 22.

## Group sizes

| dimension | group | cases |
|---|---|---|
| placement | any | 8 |
| placement | escalated | 2 |
| placement | onprem_only | 4 |
| placement | us_only | 8 |
| protection | enforced | 22 |
| application | policy-57810d29 | 8 |
| application | policy-7b90407f | 8 |
| application | policy-8fb5374a | 4 |
| application | policy-d30395cb | 2 |
| workload | claim_summary | 10 |
| workload | faq | 8 |
| workload | onprem_required | 4 |

## Counts per strategy

Counts over every case. They are never withheld for a small group.

| strategy | cases | violations | empty-set mismatches |
|---|---|---|---|
| best_published_quality | 22 | 0 | 0 |
| cheapest | 22 | 0 | 0 |
| router_cold | 22 | 0 | 0 |
| router_cold:candidate | 22 | 0 | 0 |
| router_online | 22 | 0 | 0 |
| router_online:breaker_off | 22 | 0 | 0 |
| router_online:candidate | 22 | 0 | 0 |
| router_warm_prior | 22 | 0 | 0 |
| router_warm_prior:candidate | 22 | 0 | 0 |

## Worst group per metric

The worst printed figure across every group of every dimension. It names a set of cases, not a model or an endpoint. `groups` is how many groups print the metric, of how many exist, and how many share the worst value.

| strategy | metric | dimension | group | figure | group cases | groups |
|---|---|---|---|---|---|---|
| best_published_quality | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| best_published_quality | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| best_published_quality | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| best_published_quality | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| best_published_quality | latency_regret | tied | placement=any, application=policy-57810d29, workload=faq | 5375.64 | n/a | 5 of 12 printed; 3 at this value |
| best_published_quality | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| best_published_quality | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| cheapest | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| cheapest | pass_rate | tied | placement=us_only, application=policy-7b90407f | 0.75 | n/a | 7 of 12 printed; 2 at this value |
| cheapest | quality_regret | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| cheapest | cost_regret | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| cheapest | latency_regret | tied | placement=any, application=policy-57810d29, workload=faq | 1411.68 | n/a | 7 of 12 printed; 3 at this value |
| cheapest | floor_miss_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| cheapest | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_cold | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_cold | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_cold | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_cold | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_cold | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_cold | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_cold | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_cold:candidate | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_cold:candidate | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_cold:candidate | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_cold:candidate | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_cold:candidate | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_cold:candidate | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_cold:candidate | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_online | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_online | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_online | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_online | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_online | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_online | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_online | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_online:breaker_off | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_online:breaker_off | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_online:breaker_off | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_online:breaker_off | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_online:breaker_off | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_online:breaker_off | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_online:breaker_off | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_online:candidate | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_online:candidate | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_online:candidate | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_online:candidate | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_online:candidate | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_online:candidate | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_online:candidate | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_warm_prior | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_warm_prior | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_warm_prior | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_warm_prior | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_warm_prior | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_warm_prior | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_warm_prior | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_warm_prior:candidate | violation_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |
| router_warm_prior:candidate | pass_rate | tied | placement=us_only, application=policy-7b90407f, workload=claim_summary | 0.5 | n/a | 7 of 12 printed; 3 at this value |
| router_warm_prior:candidate | quality_regret | workload | claim_summary | 0.3 (n = 10) | 10 | 7 of 12 printed; 1 at this value |
| router_warm_prior:candidate | cost_regret | workload | claim_summary | 0.0093696 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_warm_prior:candidate | latency_regret | workload | claim_summary | 3552.56 (n = 5) | 10 | 5 of 12 printed; 1 at this value |
| router_warm_prior:candidate | floor_miss_rate | workload | claim_summary | 0.375 (n = 8) | 10 | 7 of 12 printed; 1 at this value |
| router_warm_prior:candidate | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 7 of 12 printed; 7 at this value |

## Every group: `router_online`

`groups.json` holds the same table for every strategy.

| dimension | group | cases | violations | empty-set mismatches | violation_rate | pass_rate | quality_regret | cost_regret | latency_regret | floor_miss_rate | empty_set_mismatch_rate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| overall | all cases | 22 | 0 | 0 | 0 (n = 22) | 0.727273 (n = 22) | 0.136364 (n = 22) | 0.00300265 (n = 16) | 1276.09 (n = 16) | 0.157895 (n = 19) | 0 (n = 22) |
| placement | any | 8 | 0 | 0 | 0 (n = 8) | 1 (n = 8) | 0 (n = 8) | 0.000149301 (n = 8) | 253.769 (n = 8) | 0 (n = 8) | 0 (n = 8) |
| placement | escalated | 2 | 0 | 0 | insufficient cases (n = 2) | insufficient cases (n = 2) | insufficient cases (n = 2) | insufficient cases (n = 1) | insufficient cases (n = 1) | insufficient cases (n = 2) | insufficient cases (n = 2) |
| placement | onprem_only | 4 | 0 | 0 | insufficient cases (n = 4) | insufficient cases (n = 4) | insufficient cases (n = 4) | insufficient cases (n = 3) | insufficient cases (n = 3) | insufficient cases (n = 3) | insufficient cases (n = 4) |
| placement | us_only | 8 | 0 | 0 | 0 (n = 8) | 0.5 (n = 8) | 0.25 (n = 8) | insufficient cases (n = 4) | insufficient cases (n = 4) | 0.333333 (n = 6) | 0 (n = 8) |
| protection | enforced | 22 | 0 | 0 | 0 (n = 22) | 0.727273 (n = 22) | 0.136364 (n = 22) | 0.00300265 (n = 16) | 1276.09 (n = 16) | 0.157895 (n = 19) | 0 (n = 22) |
| application | policy-57810d29 | 8 | 0 | 0 | 0 (n = 8) | 1 (n = 8) | 0 (n = 8) | 0.000149301 (n = 8) | 253.769 (n = 8) | 0 (n = 8) | 0 (n = 8) |
| application | policy-7b90407f | 8 | 0 | 0 | 0 (n = 8) | 0.5 (n = 8) | 0.25 (n = 8) | insufficient cases (n = 4) | insufficient cases (n = 4) | 0.333333 (n = 6) | 0 (n = 8) |
| application | policy-8fb5374a | 4 | 0 | 0 | insufficient cases (n = 4) | insufficient cases (n = 4) | insufficient cases (n = 4) | insufficient cases (n = 3) | insufficient cases (n = 3) | insufficient cases (n = 3) | insufficient cases (n = 4) |
| application | policy-d30395cb | 2 | 0 | 0 | insufficient cases (n = 2) | insufficient cases (n = 2) | insufficient cases (n = 2) | insufficient cases (n = 1) | insufficient cases (n = 1) | insufficient cases (n = 2) | insufficient cases (n = 2) |
| workload | claim_summary | 10 | 0 | 0 | 0 (n = 10) | 0.5 (n = 10) | 0.3 (n = 10) | 0.0093696 (n = 5) | 3552.56 (n = 5) | 0.375 (n = 8) | 0 (n = 10) |
| workload | faq | 8 | 0 | 0 | 0 (n = 8) | 1 (n = 8) | 0 (n = 8) | 0.000149301 (n = 8) | 253.769 (n = 8) | 0 (n = 8) | 0 (n = 8) |
| workload | onprem_required | 4 | 0 | 0 | insufficient cases (n = 4) | insufficient cases (n = 4) | insufficient cases (n = 4) | insufficient cases (n = 3) | insufficient cases (n = 3) | insufficient cases (n = 3) | insufficient cases (n = 4) |
