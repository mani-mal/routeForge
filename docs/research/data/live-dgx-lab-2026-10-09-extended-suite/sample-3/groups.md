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
| best_published_quality | cost_regret | placement | escalated | 0.00636056 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| best_published_quality | latency_regret | application | policy-57810d29 | 1081.4 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| best_published_quality | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| best_published_quality | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | cost_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | latency_regret | workload | json | 1200.06 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| cheapest | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| cheapest | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | cost_regret | placement | escalated | 0.00636056 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_cold | latency_regret | placement | any | 724.637 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_cold | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_cold | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | cost_regret | placement | escalated | 0.00636056 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_online | latency_regret | placement | any | 724.637 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_online | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | cost_regret | placement | escalated | 0.00636056 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_online:breaker_off | latency_regret | placement | any | 724.637 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_online:breaker_off | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_online:breaker_off | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | violation_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | pass_rate | all | all | 1 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | quality_regret | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | cost_regret | placement | escalated | 0.00636056 (n = 18) | 18 | 18 of 18 printed; 1 at this value |
| router_warm_prior | latency_regret | placement | any | 724.637 (n = 36) | 36 | 18 of 18 printed; 1 at this value |
| router_warm_prior | floor_miss_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |
| router_warm_prior | empty_set_mismatch_rate | all | all | 0 in every printed group | n/a | 18 of 18 printed; 18 at this value |

## Every group: `router_online`

`groups.json` holds the same table for every strategy.

| dimension | group | cases | violations | empty-set mismatches | violation_rate | pass_rate | quality_regret | cost_regret | latency_regret | floor_miss_rate | empty_set_mismatch_rate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| overall | all cases | 108 | 0 | 0 | 0 (n = 108) | 1 (n = 108) | 0 (n = 108) | 0.00504617 (n = 108) | 398.608 (n = 108) | 0 (n = 108) | 0 (n = 108) |
| placement | any | 36 | 0 | 0 | 0 (n = 36) | 1 (n = 36) | 0 (n = 36) | 0.00311123 (n = 36) | 724.637 (n = 36) | 0 (n = 36) | 0 (n = 36) |
| placement | escalated | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00636056 (n = 18) | 2.31333 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| placement | no_retention | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00589433 (n = 18) | 234.061 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| placement | onprem_only | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00592728 (n = 18) | 306.172 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| placement | us_only | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00587239 (n = 18) | 399.83 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| protection | enforced | 90 | 0 | 0 | 0 (n = 90) | 1 (n = 90) | 0 (n = 90) | 0.00487118 (n = 90) | 415.272 (n = 90) | 0 (n = 90) | 0 (n = 90) |
| protection | passthrough | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00592111 (n = 18) | 315.292 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-57810d29 | 36 | 0 | 0 | 0 (n = 36) | 1 (n = 36) | 0 (n = 36) | 0.00333095 (n = 36) | 568.148 (n = 36) | 0 (n = 36) | 0 (n = 36) |
| application | policy-8ec16158 | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00592111 (n = 18) | 315.292 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-8fb5374a | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00592728 (n = 18) | 306.172 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-a66919ec | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00589433 (n = 18) | 234.061 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| application | policy-dc5269b2 | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00587239 (n = 18) | 399.83 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | arithmetic | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00477436 (n = 18) | 333.008 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | classification | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00496872 (n = 18) | 346.194 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | code | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00492053 (n = 18) | 320.98 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | extraction | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00532989 (n = 18) | 454.473 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | json | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00513484 (n = 18) | 689.153 (n = 18) | 0 (n = 18) | 0 (n = 18) |
| workload | reading | 18 | 0 | 0 | 0 (n = 18) | 1 (n = 18) | 0 (n = 18) | 0.00514868 (n = 18) | 247.842 (n = 18) | 0 (n = 18) | 0 (n = 18) |
