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

Figures judged: 38 (2 of 8 metrics have a tolerance; the rest are shown, not judged).

- **baseline arm**: `router_online` (the routing config as loaded)
- **candidate arm**: `router_online:candidate` (id `a0e93576a3ae`, name `no-cheap-tier`)
- **candidate overlay**: `{"complexity_thresholds": {"cheap_max": 0.0}}`
- **cases whose attempt chain changed**: 30 of 108
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
| overall | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| overall | quality_regret | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |
| overall | cost_regret | 0.00504214 | 0.00518204 | 0.000139903 | [8.50078e-05, 0.00020005] | worse | n/a | not_checked |
| overall | latency_regret | 372.932 | 160.951 | -211.981 | [-289.542, -147.969] | better | n/a | not_checked |
| overall | floor_miss_rate | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |
| overall | mean_chosen_cost | 0.00592476 | 0.00606467 | 0.000139903 | [8.50078e-05, 0.00020005] | worse | 0.0005 | within_tolerance |
| overall | wasted_attempts | 0.0277778 | 0.0185185 | -0.00925926 | [-0.0277778, 0] | not_detectable | n/a | not_checked |
| overall | exhausted_rate | 0 | 0 | 0 | [0, 0] | none | n/a | not_checked |

## Groups

Only the metrics a tolerance was named for; `check.json` holds every metric for every group.

| scope | metric | baseline | candidate | worsening | range | change | tolerance | state |
|---|---|---|---|---|---|---|---|---|
| placement=any | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=any | mean_chosen_cost | 0.00367132 | 0.00372789 | 5.65692e-05 | [-2.99131e-05, 0.000154183] | not_detectable | 0.0005 | within_tolerance |
| placement=escalated | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=escalated | mean_chosen_cost | 0.00739167 | 0.00739167 | 0 | [0, 0] | none | 0.0005 | within_tolerance |
| placement=no_retention | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=no_retention | mean_chosen_cost | 0.00692661 | 0.00717111 | 0.0002445 | [0.000101056, 0.000400833] | worse | 0.0005 | within_tolerance |
| placement=onprem_only | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=onprem_only | mean_chosen_cost | 0.00690461 | 0.00717667 | 0.000272056 | [0.000104944, 0.000476944] | worse | 0.0005 | within_tolerance |
| placement=us_only | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| placement=us_only | mean_chosen_cost | 0.00698306 | 0.00719278 | 0.000209722 | [8.04444e-05, 0.000370556] | worse | 0.0005 | within_tolerance |
| protection=enforced | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| protection=enforced | mean_chosen_cost | 0.00571538 | 0.00583927 | 0.000123883 | [6.47061e-05, 0.000184715] | worse | 0.0005 | within_tolerance |
| protection=passthrough | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| protection=passthrough | mean_chosen_cost | 0.00697167 | 0.00719167 | 0.00022 | [7.98333e-05, 0.000379889] | worse | 0.0005 | within_tolerance |
| application=policy-57810d29 | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-57810d29 | mean_chosen_cost | 0.00388132 | 0.00382789 | -5.34308e-05 | [-9.48394e-05, -1.67431e-05] | better | 0.0005 | within_tolerance |
| application=policy-8ec16158 | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-8ec16158 | mean_chosen_cost | 0.00697167 | 0.00719167 | 0.00022 | [9e-05, 0.000363667] | worse | 0.0005 | within_tolerance |
| application=policy-8fb5374a | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-8fb5374a | mean_chosen_cost | 0.00690461 | 0.00717667 | 0.000272056 | [0.000104111, 0.000469722] | worse | 0.0005 | within_tolerance |
| application=policy-a66919ec | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-a66919ec | mean_chosen_cost | 0.00692661 | 0.00717111 | 0.0002445 | [9.71667e-05, 0.000403389] | worse | 0.0005 | within_tolerance |
| application=policy-dc5269b2 | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| application=policy-dc5269b2 | mean_chosen_cost | 0.00698306 | 0.00719278 | 0.000209722 | [7.91111e-05, 0.0003535] | worse | 0.0005 | within_tolerance |
| workload=arithmetic | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=arithmetic | mean_chosen_cost | 0.00563736 | 0.00564954 | 1.21833e-05 | [-3.55778e-05, 6.01667e-05] | not_detectable | 0.0005 | within_tolerance |
| workload=classification | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=classification | mean_chosen_cost | 0.00580177 | 0.005912 | 0.000110222 | [3.33889e-05, 0.000201056] | worse | 0.0005 | within_tolerance |
| workload=code | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=code | mean_chosen_cost | 0.00578955 | 0.00595797 | 0.000168422 | [2.66667e-05, 0.000342944] | worse | 0.0005 | within_tolerance |
| workload=extraction | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=extraction | mean_chosen_cost | 0.00624674 | 0.00650486 | 0.000258127 | [5.5905e-05, 0.000476611] | worse | 0.0005 | within_tolerance |
| workload=json | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=json | mean_chosen_cost | 0.00608661 | 0.00620346 | 0.000116847 | [2.69444e-06, 0.000254833] | worse | 0.0005 | within_tolerance |
| workload=reading | pass_rate | 1 | 1 | 0 | [0, 0] | none | 0.05 | within_tolerance |
| workload=reading | mean_chosen_cost | 0.00598655 | 0.00616017 | 0.000173614 | [3.44472e-05, 0.0003255] | worse | 0.0005 | within_tolerance |
