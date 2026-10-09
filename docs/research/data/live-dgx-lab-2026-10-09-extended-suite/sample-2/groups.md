# FeasibleRouteBench group report

> This run called the endpoints in the endpoint file, once per case and endpoint, and graded each answer with a deterministic grader. One call is one sample.
> The router comparison is mostly the tier rule: the target tier is chosen from the request's length, so on short cases the router's regret against the oracle is set by which tier it may use, not by the four-term scorer.
> Cases are synthetic and single-turn.
> A group is the cases of this suite that carry one label. A figure over fewer cases than the minimum prints its case count and no rate. A group above the minimum is still small: read its case count with every figure.
> The worst group is the extreme of many small-sample figures, so it is worse than a typical group partly by selection. The same cases can carry several labels, so one set of cases can appear under more than one dimension; a tie is listed, not broken.
> Placement, protection and workload labels are the case author's tags. They are not read from the compiled policy and can differ from it. An application with no tag is a fingerprint of the case's policy as written.

Minimum cases behind a printed rate: **5** (`--min-group-size`). Cases in this run: 108.

## Group sizes

| dimension | group | cases |
|---|---|---|
| placement | any | 36 |
| placement | escalated | 18 |
| placement | no_retention | 18 |
| placement | onprem_only | 18 |
| placement | us_only | 18 |
| protection | enforced | 90 |
| protection | passthrough | 18 |
| application | policy-57810d29 | 36 |
| application | policy-8ec16158 | 18 |
| application | policy-8fb5374a | 18 |
| application | policy-a66919ec | 18 |
| application | policy-dc5269b2 | 18 |
| workload | arithmetic | 18 |
| workload | classification | 18 |
| workload | code | 18 |
| workload | extraction | 18 |
| workload | json | 18 |
| workload | reading | 18 |

## Counts per strategy

Counts over every case. They are never withheld for a small group.

| strategy | cases | violations | empty-set mismatches |
|---|---|---|---|
| best_published_quality | 108 | 0 | 0 |
| cheapest | 108 | 0 | 0 |
| router_cold | 108 | 0 | 0 |
| router_online | 108 | 0 | 0 |
| router_online:breaker_off | 108 | 0 | 0 |
| router_warm_prior | 108 | 0 | 0 |

## Worst group per metric

The worst printed figure across every group of every dimension. It names a set of cases, not a model or an endpoint. `groups` is how many groups print the metric, of how many exist, and how many share the worst value.

| strategy | metric | dimension | group | figure | group cases | groups |
|---|---|---|---|---|---|---|
| best_published_quality | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| best_published_quality | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| best_published_quality | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| best_published_quality | cost_regret | placement | escalated | 0.00639222 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| best_published_quality | latency_regret | tied | placement=any, application=policy-57810d29 | 1674.74 | n/a | 18 of 18 printed; 2 at this value |
| best_published_quality | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| best_published_quality | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | cost_regret | tied | protection=passthrough, application=policy-8ec16158, workload=arithmetic | 2.11111e-06 | n/a | 18 of 18 printed; 3 at this value |
| cheapest | latency_regret | workload | json | 1537.06 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| cheapest | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | cost_regret | placement | escalated | 0.00639222 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_cold | latency_regret | placement | any | 1080.97 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_cold | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | cost_regret | placement | escalated | 0.00639222 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_online | latency_regret | placement | any | 1080.97 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_online | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | cost_regret | placement | escalated | 0.00639222 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_online:breaker_off | latency_regret | placement | any | 1080.97 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_online:breaker_off | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | cost_regret | placement | escalated | 0.00639222 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_warm_prior | latency_regret | placement | any | 1080.97 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_warm_prior | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |

## Every group: `router_online`

`groups.json` holds the same table for every strategy.

| dimension | group | cases | violations | empty-set mismatches | violation_rate | pass_rate | quality_regret | cost_regret | latency_regret | floor_miss_rate | empty_set_mismatch_rate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| overall | all cases | 108 | 0 | 0 | 0 (n = 108) | 1 (n = 108) | 0 (n = 108) | 0.00508439 (n = 108) | 523.062 (n = 108) | 0 (n = 108) | 0 (n = 108) |
| placement | any | 36 | 0 | 0 | 0 (n = 36) | 1 (n = 36) | 0 (n = 36) | 0.00315465 (n = 36) | 1080.97 (n = 36) | 0 (n = 36) | 0 (n = 36) |
| placement | escalated | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00639222 (n = 18) | 0 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| placement | no_retention | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00585178 (n = 18) | 345.603 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| placement | onprem_only | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00592083 (n = 18) | 326.743 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| placement | us_only | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00603222 (n = 18) | 304.087 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| protection | enforced | 90 | 0 | 0 | 0 (n = 90) | 1 (n = 90) | 0 (n = 90) | 0.0049184 (n = 90) | 552.997 (n = 90) | 0 (n = 90) | 0 (n = 90) |
| protection | passthrough | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00591433 (n = 18) | 373.39 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-57810d29 | 36 | 0 | 0 | 0 (n = 36) | 1 (n = 36) | 0 (n = 36) | 0.00339359 (n = 36) | 894.275 (n = 36) | 0 (n = 36) | 0 (n = 36) |
| application | policy-8ec16158 | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00591433 (n = 18) | 373.39 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-8fb5374a | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00592083 (n = 18) | 326.743 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-a66919ec | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00585178 (n = 18) | 345.603 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-dc5269b2 | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00603222 (n = 18) | 304.087 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | arithmetic | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00482027 (n = 18) | 458.421 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | classification | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00502877 (n = 18) | 355.674 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | code | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00503667 (n = 18) | 533.933 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | extraction | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00541053 (n = 18) | 512.864 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | json | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00504719 (n = 18) | 848.803 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | reading | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00516292 (n = 18) | 428.679 (n = 18) | 0 (n = 18) | 0 (n = 18) |
