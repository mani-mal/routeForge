# P2 evaluation metrics — group report, churn robustness, confidence calibration, and an offline router-version check

**Date:** 2026-10-07 · **Status:** design. Two plans build it (§15): the first —
`docs/superpowers/plans/2026-10-07-p2-eval-metrics.md` — covers the per-case scores, the
group report and the router-version check; the second (churn robustness and confidence
calibration) is written when the first has shipped.
**Priority:** 2 (`docs/roadmap/priorities-1-3-status.md` §2.2: "Handoff §18 metrics not yet
reported" and "Router-version promotion gates"; handoff §18 metrics 6–9, §19 "alternative
router versions", §36 Phase 2 "safe router-promotion gates").
**Follows:** P2b-1 FeasibleRouteBench (`app/eval/runner.py`, `app/eval/oracle.py`), B48
failure injection, the circuit breaker, P3c-3's paired bootstrap (`app/eval/uncertainty.py`).
**Paper:** feeds `docs/research/arxiv.md` §3.3 and §4.6, and new experiments E11a–E11f.
**Scope:** offline only — `app/eval/`, `scripts/feasible_bench.py`, `tests/eval/`, docs. No
file under `app/` outside `app/eval/` changes. No database change, no admin route, no
setting, no new dependency.

## 1. The problem

FeasibleRouteBench reports six of the ten metrics the handoff asks of a feasible-set
evaluation (§18). The four it does not report are named, in every `report.md`, as deferred
(`runner.DEFERRED_METRICS`) or are only half there:

| Handoff §18 | Today |
|---|---|
| 6. Calibration — does routing confidence correspond to observed success? | "calibration (the router emits no confidence)" — deferred |
| 7. Policy-churn robustness | deferred: the policy is static for a run |
| 8. Endpoint-churn robustness | deferred: the registry is static for a run |
| 9. Worst-group performance by placement, protection, tenant and workload | partly: `metrics.json` carries `groups` and `worst_groups` for three author tags, with no group size, no minimum size, no violation or empty-set figure, and no tenant or application |

The handoff also asks for "safe router-promotion gates" (§36 Phase 2) and for the
evaluation plane to compare "alternative router versions" (§19). The bench can score a
weight variant beside the shipped router (`--variants`), but nothing compares two router
configurations and says, with an exit code, whether the second is worse.

Two product rules decide what an answer may look like:

- **The Improvement Plane is orchestrated, not hosted, and RouteForge has no in-process
  promotion path** (CLAUDE.md rule 4). A "promotion gate" here cannot promote. It can only
  be a check an operator runs before **they** change configuration.
- **The eligible set is derived from policy before scoring** (rules 3 and 8). A router
  version can change which eligible endpoint is chosen; it cannot change which endpoints
  are eligible. So the one thing a router check must never tolerate is a choice outside
  the eligible set, and everything else is a trade-off the operator sets a tolerance for.

## 2. Goals and non-goals

**Goals.**

1. **Group report.** Every bench metric, per strategy, by placement, protection,
   application and workload, with each figure's denominator beside it; the worst group
   named per metric with its size; a figure over too few cases printed as a count and no
   rate.
2. **Churn robustness.** A harness mode that changes the policy or the endpoint registry
   one declared change at a time, re-runs the suite, and reports per change: violations
   (which must stay 0), newly empty feasible sets and how they were handled, how many
   choices moved, and how regret moved.
3. **Confidence calibration.** A measurement of the one confidence RouteForge has — the
   backend-reported answer confidence the gateway escalates on — against graded outcomes:
   reliability by bin, expected calibration error, and the escalation threshold's operating
   point. Where the confidence carries no information (simulation), a measured statement
   of that, not a number.
4. **Router-version check.** A CLI mode that scores a candidate routing configuration
   beside the current one over the same sweep and exits non-zero when the candidate makes
   any ineligible choice (no tolerance) or worsens a named metric beyond an operator-set
   tolerance, overall or in any group with enough cases.
5. With none of the new flags, every bench output is byte-identical to today's, and that is
   pinned by a golden test this slice adds (there is none today).
6. Every new report opens with fixed text saying what its figures are not (§8).

**Non-goals.**

- **Any promotion, apply or activation path.** Nothing in this slice writes a config file,
  calls an admin route, or is read by serving code.
- **An LLM judge.** Paused by owner decision (`docs/roadmap/priorities-1-3-status.md` §2.2).
- **Online learning or exploration.** Paused; research §14.4 lists what it would need.
- **A confidence signal invented in serving code.** The calibration metric measures what
  exists. Recording that confidence in production is a backlog item (§14), not this slice.
- **A ranking of models, endpoints or vendors**, and any recommendation to relax a
  security requirement. A change that tightens security and raises cost is reported as a
  cost.
- **A UI.**
- **Rebuilding the policy simulator.** §6.6 says how churn differs from it.

## 3. What the code has today

Read before the design was written; each point decided something.

### 3.1 Groups

`oracle.grouped(cases, rows, choices, strategy, tag)` runs `strategy_metrics` over the cases
sharing each value of one tag, and `oracle.worst_groups` names the worst value per metric
over the tags `workload`, `placement`, `protection`. The runner writes both into
`metrics.json` (`groups`, `worst_groups`) for every strategy and prints a "Worst groups"
table in `report.md`.

What that leaves out:

- **No size.** A worst group is `(tag, value, metric value)`. The starter suite's groups are
  2 to 10 cases (research §6.3: "the worst-group numbers are illustrative only"), and a
  two-case group can be "worst" with nothing saying so.
- **No denominator.** Cost regret is over the cases whose served answer passed. A group of
  eight cases can have a cost regret that is over one.
- **A case missing a tag is in no group**, so the groups of a dimension do not sum to the
  suite.
- **No violation rate and no empty-set figure** among the worst-group metrics.
- **No tenant, no application.** Every case runs under one tenant id
  (`runner.TENANT_ID = "feasible-bench"`); a case carries an `app_policy`, not an
  application identity.

`strategy_metrics` computes each aggregate inside one loop and returns only the aggregates.
A per-group or per-resample figure has to re-run that loop over a subset of cases.

### 3.2 "Routing confidence"

There is exactly one confidence in RouteForge, and it is not the router's:

- `GenerationResult.confidence` (`app/models.py`) is **reported by the backend for one
  answer**. `LiteLLMBackend.generate` computes it as `min(1, exp(mean token logprob / T))`
  with `T = routing.confidence_logprob_temperature` (4.0 as shipped), or the constant
  `DEFAULT_CONFIDENCE = 0.9` when the provider returned no logprobs
  (`app/gateway/backends/litellm_backend.py`). It is `None` on the streaming path.
- Its only reader is `Gateway.run` (`app/gateway/gateway.py`): when
  `result.confidence < routing.confidence_escalation_threshold` (0.60 as shipped), fewer
  than `max_escalations` escalations have happened and a higher tier exists, the gateway
  raises `min_tier` one tier and re-selects. That is where the threshold "gets its number":
  it is compared against a temperature-softened geometric-mean token probability of the
  answer just served.
- **It is recorded nowhere.** `DecisionRecord` has no confidence field; `sp.scoring` and the
  response certificate carry the four scoring terms and nothing about confidence
  (`docs/reference/routing-observability.md`); `routing_outcomes` records that more than
  one attempt happened, not why. Whether an escalation fired is visible only as a second
  router hop.
- The step-6 router emits no confidence at all. `sp.scoring` holds every eligible
  candidate's score, so a margin between the top two could be derived; nothing derives it.
- **In the bench it is inert twice over.** `harness_settings` sets `max_escalations = 0` so
  that a pinned sweep call is one backend call, and `SimulatedBackend` returns
  `confidence=1.0` for every answer ("simulation has no logprobs to derive a real
  confidence from").

So "calibration of routing confidence" can only mean: **is the answer confidence the
escalation decision reads related to whether the answer was right?** The harness can see
that confidence without any serving change: `runner._CountingBackend.generate` already
wraps every backend call of the sweep and returns the `GenerationResult`.

### 3.3 "Router version"

A router version is the content of `config/routing.yaml` (with `pricing.yaml`):
`weights`, `tier_quality`, `complexity_thresholds`, `risk_floors`, `strategy` (a name in
`app/routing/strategy.py::_STRATEGIES`), `signals`, `breaker`, and the escalation and
failover bounds. `Settings.routing_policy_version` is the first 12 hex of a sha256 over the
raw routing and pricing YAML (`app/config/loader.py::_policy_version`), stamped on every
trace.

The bench loads the deployment's config with `load_settings()` (the `config/` directory of
the working directory), then builds its own settings from it
(`endpoints.harness_settings`): every provider an endpoint file names is enabled, the
static `model_pool` is emptied, prices come from the endpoint file, and `max_escalations`
and `max_failover_attempts` are zeroed for the sweep (the attempt cap is read from the
deployment's value first).

It already scores alternative routers in the same run: `--variants` maps a name to a
weights mapping, `strategies.variant_settings` deep-copies the harness settings with those
weights through the real `RoutingCfg` validation, and each variant gets three arms
(`router_cold:<name>`, `router_online:<name>`, `router_warm_prior:<name>`) scored **over
the same sweep matrix** as every other strategy.

Two facts make that the right base for a comparison:

- **Eligibility does not depend on routing config.** `feasible_set`
  (`app/policy/feasible.py`) reads the policy context, the registry and
  `serving.providers`; of `settings.routing` it reads only the pool. The runner computes
  each case's eligible set once and hands it to every strategy.
- **The sweep does not depend on routing config either.** A matrix row is one pinned call
  to one endpoint; its quality, cost and latency come from the backend.

So two router configurations scored as two arms of one run share the cases, the eligible
sets, the seed and every per-endpoint outcome. A second run from a second config directory
would re-do the sweep and could differ in policy or providers as well — a different
question.

### 3.4 What pins the bench's output

`test_runner.py::test_same_seed_gives_byte_identical_metrics` shows two runs agree with each
other. Nothing pins the output against a stored copy: research §4 records an md5 by hand.
A refactor that changed every figure identically in both runs would pass.

### 3.5 The paired bootstrap

`app/eval/uncertainty.py` (P3c-3) resamples simulator rows. Its replicate loop is built
around `RowResult` and application strata and is not reusable. Three functions are, as
they are: `draw_indices(seed, replicate, label, n)` (the positions one replicate draws — a
function of its arguments alone, which is the pairing), `interval_of(values, level)` (the
equal-tailed percentile interval) and `MAX_REPLICATES`.

## 4. The group report

A new pure module, `app/eval/groups.py`, and a flag, `--group-report`.

### 4.1 Dimensions

Four, in this order: `placement`, `protection`, `application`, `workload`.

- `placement`, `protection`, `workload` are the case's tags of those names — the same three
  `oracle.grouped` reads, so the report extends the existing block and cannot disagree with
  it (a test compares the two).
- `application` is `tags.app` when the author set a non-empty string, else `policy-` plus
  the first 8 hex of sha256 over the case's `app_policy` (sorted keys). Cases with the same
  policy are one application. The starter suite has four.
- **There is no tenant dimension.** The bench runs every case under one tenant id;
  splitting by it would produce one group. The handoff's "tenant" is served by
  `application`; the departure is listed in §16.
- A case with no value for a dimension is in that dimension's `(untagged)` group. Every
  case is therefore in exactly one group per dimension, and a dimension's groups sum to
  the suite (invariant E3).

The tags are the case author's labels, not values derived from the compiled policy: a case
tagged `placement: any` whose policy says `onprem_only` is grouped as `any`. Deriving the
two policy dimensions from the gate's own output is a follow-up (§14).

### 4.2 Figures and their denominators

Seven metrics per group, per strategy:

| Metric | Over | Worse is |
|---|---|---|
| `violation_rate` | every case in the group | higher |
| `pass_rate` | non-empty cases | **lower** |
| `quality_regret` | non-empty cases | higher |
| `cost_regret` | non-empty cases whose served answer passed | higher |
| `latency_regret` | the same, with both latencies known | higher |
| `floor_miss_rate` | non-empty cases with a passing endpoint (`P(c) ≠ ∅`) | higher |
| `empty_set_mismatch_rate` | every case in the group | higher |

The definitions are `strategy_metrics`' (research §3); `empty_set_mismatch_rate` is its
`empty_set_mismatches` count over the group's case count.

Every figure is printed with its own `n`: `{"value": 0.75, "n": 8}`. Each group also carries
four counts that are always printed: `n_cases`, `empty_cases`, `violations`,
`empty_set_mismatches`.

**The overall figure is the combination of a dimension's groups weighted by each figure's
own denominator**: `overall = Σ n_g · value_g / Σ n_g` (invariant E4). Weighting by the
group's **case count** is right for `violation_rate` and `empty_set_mismatch_rate` only.
For the other five it is wrong whenever groups differ in how many of their cases are empty
or passed; §12.1 has a worked counter-example (0.31875 against the true 0.24).

### 4.3 Small groups

A figure over fewer than `--min-group-size` cases (default **5**, inclusive: five is
enough) is printed as `{"state": "insufficient_cases", "n": k}` — in the report,
`insufficient cases (n = k)` — and carries no value.

- The rule is applied to **the figure's denominator**, not the group's case count. A
  ten-case group with four passing cases prints a pass rate and no cost regret.
- **Counts are never suppressed.** A violation in a one-case group prints
  `violations: 1` beside `insufficient cases (n = 1)`.
- Five is a floor against reading a rate off one or two cases. It is not evidence that a
  group above it is large enough for anything; the report's fixed text says so (§8).

### 4.4 The worst group

Per metric, the worst printed figure across every group of every dimension, with its
dimension, label, value, `n`, and the group's case count. A tie keeps the first in
dimension order, then label order.

Two additions to what `oracle.worst_groups` reports:

- `groups_printed` and `groups_at_value`. When every printed group has the same value (all
  zero violations, say), no group is worse than another, and the report prints "0 in every
  printed group (7)" instead of naming one.
- A metric no group prints has no worst group (`null`; "no group has enough cases").

"Worst group" names a set of cases. It orders no model, endpoint or vendor.

### 4.5 Output

`--group-report` writes two files beside the existing five:

- `groups.json` — `schema: routeforge.bench-group-report/v1`, `min_group_size`,
  `dimensions`, `metrics`, `n_cases`, `group_sizes`, `strategies.<name>.{overall,
  dimensions, worst}`, `caveats`. Every strategy of the run is in it.
- `groups.md` — the caveats, the group sizes, the worst group per metric for every
  strategy, and the full table for `router_online` (the JSON holds the rest).

`--min-group-size N` (integer ≥ 1) needs `--group-report` or `--candidate`; alone it is a
usage error (exit 1), because a flag that silently does nothing reads as "applied".

`metrics.json`'s existing `groups` and `worst_groups` blocks are **not** changed: they are
part of the pinned default output.

## 5. Per-case scores

Both the group report and the router-version check need what one case contributes to each
aggregate. `oracle.py` gains:

- `CaseScore` — one `(case, strategy)` outcome, unrounded: `case_id`, `empty`, `violation`,
  `empty_mismatch`, `infeasible_at_floor`, and `passed`, `quality_regret`, `floor_miss`,
  `cost_regret`, `latency_regret`, `chosen_cost`, `wasted_attempts`, `exhausted`, each
  `None` when the case is outside that metric's denominator.
- `case_scores(cases, rows, choices, strategy) -> list[CaseScore]` — the loop that is
  inside `strategy_metrics` today, moved, not rewritten.
- `aggregate_scores(scores) -> dict` — the unrounded `strategy_metrics` dict.
- `metric_support(scores) -> dict[str, int]` — each figure's denominator.

`strategy_metrics` becomes `_round_value(aggregate_scores(case_scores(...)))`. Its output
is byte-identical: sums run over the same cases in the same order. The golden pin (§10) is
written before this refactor and must still pass after it.

## 6. Churn robustness (second plan)

A new module, `app/eval/churn.py`, and a flag, `--churn`.

### 6.1 What a perturbation is

One declared change to one input, applied for a whole run:

```
Perturbation(id, kind, target, direction, expect)
```

`direction` is `tightens`, `loosens`, `neutral` or `registry`. `expect` is what the change
may do to a case's eligible set, checked on every case (§6.4).

### 6.2 The built-in set

Generated from the suite and the endpoint file, in a fixed order, with stable ids. One
perturbation per row and target; a perturbation that would change nothing (the value is
already set) is skipped and listed as skipped.

**Policy** — applied to the `app_policy` of every case of one application (§4.1), one
application at a time:

| kind | change | direction | `expect` |
|---|---|---|---|
| `residency_onprem_only` | `residency: onprem_only` | tightens | subset |
| `require_no_retention` | `require_no_retention: true` | tightens | subset |
| `allowed_regions:<r>` | `allowed_regions: [r]`, for each region the endpoint file declares | tightens | subset |
| `allowed_providers:<p>` | `allowed_providers: [p]`, for each provider in the endpoint file | tightens | subset |
| `allowed_tiers:<t>` | `allowed_tiers: [t]`, for each tier in the endpoint file | tightens | subset |
| `model_input:raw` / `:redact` | flip the `model_input` axis | loosens / tightens | any / any |
| `client_output:raw` / `:redact` | flip the `client_output` axis | loosens / tightens | **unchanged** |
| `policy_mode:passthrough` / `:enforced` | flip the preset | loosens / tightens | any |

`model_input: raw` is a protection change that can shrink the eligible set — an endpoint
declaring `accepts_raw_sensitive: false` is excluded for raw sensitive content (P1b). That
is the one declared cross-effect, and it only narrows; the report shows it as it is.
`client_output` decides what the client receives and must not move eligibility at all:
`expect: unchanged` makes the bench a second witness of that P1a rule.

**Endpoint registry** — one endpoint at a time:

| kind | change | `expect` |
|---|---|---|
| `remove:<slug>` | the entry is deleted | subset, and `slug` in no eligible set |
| `disable:<slug>` | the entry is onboarded with `enabled: false` | subset, and `slug` in no eligible set |
| `unhealthy:<slug>` | a simulated outage covering every case | unchanged (health is not eligibility; the gateway fails over) |
| `reprice:<slug>:x10`, `:x0.1` | both prices × 10, ÷ 10 | unchanged (the budget filter is the router's, inside the eligible set) |
| `region:<slug>:unknown`, `:<other>` | the region declaration withdrawn, or changed to another declared region | any |
| `retention:<slug>:retained`, `:unknown` | the retention declaration changed | any |
| `raw_sensitive:<slug>:flip` | `accepts_raw_sensitive` flipped | any |
| `add_cheaper_twin:<slug>` | a copy of the first endpoint (by slug) under a new slug at half the price | superset by at most the new slug |
| `add_undeclared:<provider>` | a new endpoint of each provider with no region or retention declared | superset by at most the new slug; never eligible for an application with a region or retention requirement |

`disable` needs one harness change: `EndpointSpec` gains `enabled: bool = True`, passed to
`upsert_onboarded` (today `build_registry` onboards every entry enabled, and asserts every
entry reached the effective pool — that assertion must then skip a disabled one). Whether
the registry drops a disabled row from the pool or keeps it as a candidate rejected with
`disabled by admin` has **not** been established for this design; the second plan reads
`app/apps/registry.py` and `feasible.py`'s stage 6 first. Either way the perturbation's
`expect` is the same, and the report shows what the product does: where a disabled
endpoint is visible to the conflicting core it belongs under endpoint **state**, not under
a requirement, and that is the difference from `remove` worth showing.

The `expect` column is this design's claim about the product, read from
`app/policy/feasible.py` and not yet run except for the probe in §6.3. Invariant E21 is
the test. A row that fails it on the starter suite is a finding to report, not a cell to
correct.

An operator can pass `--churn FILE` to declare a set instead (the same fields, a
`routeforge.bench-perturbations/v1` document). The built-in set is `--churn builtin`.

### 6.3 How a perturbation runs

The bench is not changed to take a perturbation. For each one, churn **writes the
perturbed suite and endpoint file** to `<out>/churn/<nn>-<id>/inputs/` and calls
`run_bench` on them, into `<out>/churn/<nn>-<id>/`. `<out>/churn/00-baseline/` is the
unperturbed run.

- The perturbed inputs are files an operator can open and diff: the audit trail of "what
  exactly was changed" is the inputs themselves.
- Nothing about the gate, `feasible_set`, the pipeline or the router is bypassed or
  re-implemented: a perturbed run is an ordinary run.
- A feasibility probe on the base tree confirms the approach (2026-10-07, starter suite ×
  healthcare-sim, seed 0, not a result): with `local-llama` removed, 6 of 22 cases have an
  empty feasible set, all 6 are rejected by the real pipeline with no model call and a
  conflicting core `(residency)`, and no strategy makes an ineligible attempt.

Churn does **not** use a perturbed run's exit code: the suite's `expect_empty` flags
describe the unperturbed world, so a correctly rejected newly-empty case would read as an
"empty-set mismatch". Churn applies its own checks (§6.4) to `metrics.json`.

One runner addition, in memory only: `RunResult.empty_cores` — for each empty case, the
`ConflictingCore.to_record()` of the `NoEligibleCandidate` the empty-set probe raised, or
`None`. It is written to `churn.json`, never to `metrics.json`.

### 6.4 Per perturbation

| Figure | Definition |
|---|---|
| `violations` | `policy.violations_total`, and the number of strategies with `violation_rate > 0`. **Must be 0.** |
| `eligibility` | cases whose eligible set changed; of those, narrowed, widened, both |
| `expectation_met` | whether every case's change is allowed by `expect` |
| `newly_empty` | cases non-empty in the baseline and empty here; of those, `rejected_with_core` (the real pipeline refused the unpinned probe, made no model call, listed ≥ 1 exclusion, and the exception carried a core) and `served` (**must be 0**) |
| `newly_non_empty` | the reverse |
| `choice_changed` | the share of cases whose `router_online` attempt chain changed |
| `deltas` | `router_online`'s pass rate, quality, cost and latency regret, and mean chosen cost: baseline, perturbed, difference, each with its `n` |

Deltas compare two aggregates that can be over different cases (a newly empty case leaves
the perturbed denominator); both `n` are printed, and a figure below `--min-group-size` in
either run is `insufficient_cases`.

Exit code with `--churn`: `0` clean; `1` load error; `2` when any perturbation has a
violation, a served empty set, a post-gate widening, a pinned mismatch, or an unmet
`expect`.

### 6.5 What a churn result is

It is about **the declared set**: single changes, one at a time, each held for a whole
run. It says nothing about two changes at once, about a change arriving mid-traffic, or
about a change not in the set. That sentence is fixed text in every churn report (§8).

A tightening perturbation that raises cost is reported as a cost delta. No output names a
perturbation to undo.

### 6.6 How it differs from the policy simulator

| | Policy simulator (`policy_simulate.py --scenarios`) | Churn (`feasible_bench.py --churn`) |
|---|---|---|
| Input | recorded, content-free outcome rows from a deployment | labelled cases with graders |
| Question | "what would **this** change do to **my** traffic?" | "does the router stay inside the feasible set, and how far does regret move, when **something** changes?" |
| Change | a proposal the operator writes | a fixed, generic set of single changes |
| Runs | a replay of the first routing decision per row | the real gate, pipeline and router, with a graded answer per eligible endpoint |
| Quality | the router's quality term for the picked model | a grade against the feasible oracle |
| Weighting | by traffic | by case |
| Output | counts and costs for a decision | a robustness table, and a non-zero exit when an invariant breaks |

Churn reuses none of the simulator's code and parses no proposal. Where the vocabularies
overlap (`endpoints.remove`, an app's `residency`), the docs map one to the other.

## 7. The offline router-version check

Two new modules — `app/eval/candidate.py`, `app/eval/version_check.py` — and a flag,
`--candidate FILE`.

**The name.** The handoff calls this a promotion gate. In the code, the CLI and the docs it
is the **router-version check**: `--candidate`, `check.json`, `check.md`,
`routeforge.router-version-check/v1`. "Promote" appears in exactly one sentence of a
report — the one saying this does not do it (§8) — and in no key.

### 7.1 The candidate

A YAML file stating the `routing.yaml` keys an operator is thinking of changing:

```yaml
schema: routeforge.router-candidate/v1
name: latency-heavy            # optional, a label for the report
routing:
  weights: {latency: 0.6}      # merged over the base weights
  complexity_thresholds: {cheap_max: 0.25}
```

- **Baseline** is the deployment's routing config as the bench loads it. **Candidate** is
  that config with the overlay laid over it, in memory, for one run.
- A mapping-valued key is merged **one level** over the base value (`weights: {latency:
  0.6}` keeps the other three weights); a scalar replaces.
- The result goes through the real `RoutingCfg` validation, the required-weights check the
  variants use, the strategy registry, and the config lint. A candidate the deployment
  could not boot with is a load error.
- **Allowed keys**: `weights`, `tier_quality`, `complexity_thresholds`, `risk_floors`,
  `strategy`, `signals`, `breaker` — what the bench's router arms read when they choose.
- **Refused keys, each with its reason** (`candidate.NOT_EVALUABLE`):
  `confidence_escalation_threshold`, `confidence_logprob_temperature`, `max_escalations`
  (the sweep runs with escalation off, so a change moves no bench figure),
  `max_failover_attempts` (the attempt cap is the deployment's, for every arm), `tiers`,
  `model_pool`, `score_on`, `fallback`, `prompt_opt`, `quality_refresh_s`. Refusing is the
  point: a candidate that silently changed nothing would read as "no regression"
  (invariant E15). A test asserts the two lists cover `RoutingCfg` exactly, so a new
  routing key must be put in one of them.
- `Candidate.id` is the first 12 hex of sha256 over the overlay (sorted keys).

Why an overlay and not a second config directory: §3.3. A second directory would re-run
the sweep under possibly different policy and providers, and the comparison would no
longer be of the router alone.

### 7.2 The arms

The candidate is scored exactly like a weight variant: its own `Settings`, and three arms
named `router_cold:candidate`, `router_online:candidate`, `router_warm_prior:candidate`,
over the shared sweep matrix. The variant name `candidate` is reserved while `--candidate`
is given.

**The check compares `router_online` with `router_online:candidate`** — the reported arm
(research §9.3). Both learn only from their own earlier attempts, on the same case clock.

Adding the candidate changes nothing else in the run: `matrix.jsonl` is byte-identical,
and every other strategy's metrics and choices are unchanged (invariant E16).

### 7.3 Figures

For a scope — `overall`, or `<dimension>=<group>` for a group of §4.1 — and each of eight
metrics (`pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`, `floor_miss_rate`,
`mean_chosen_cost`, `wasted_attempts`, `exhausted_rate`):

- `baseline`, `candidate` — each arm's aggregate over the scope's cases, over its own
  denominator (`support`).
- `worsening` — the candidate's change **in the direction that is worse**:
  `candidate − baseline`, or `baseline − candidate` for `pass_rate`. Positive is worse.
- `lo`, `hi` — a percentile interval of the worsening under a **paired case bootstrap**:
  replicate `b` draws `n` case positions with replacement,
  `uncertainty.draw_indices(seed, b, scope, n)`, and both arms' aggregates are recomputed on
  that one draw. Defaults: 1000 replicates, level 0.90, seed 0
  (`--check-replicates`, `--check-interval`, `--check-seed`). Cases are sorted by id before
  drawing, so the result does not depend on the order they were scored in (invariant E9).
  A replicate in which either aggregate is undefined leaves the figure with no range.
- `change` — what the range says, independent of any tolerance: `none` (every case has the
  same value in both arms), `worse` (`lo > 0`), `better` (`hi < 0`), `not_detectable` (the
  range includes 0), `undefined` (no range).

A figure whose denominator is below `--min-group-size` in either arm is
`insufficient_cases`: it keeps its `support` and carries no value, worsening or range.

What the range is: the spread of the paired difference when the suite's cases are
resampled. It says how much the difference depends on which cases happen to be in the
suite. It says nothing about whether the suite resembles real traffic, and it does not
cover the simulation seed or the case order — one run is one seed and one order.

### 7.4 The decision rule

`--tolerance METRIC=VALUE` (repeatable) names a metric and the most it may worsen, in the
metric's own units: `pass_rate=0.02` is two points, `mean_chosen_cost=0.0001` is a
hundredth of a cent per request, `latency_regret=50` is 50 ms. A tolerance is ≥ 0.

For a figure with worsening `w`, range upper end `hi` and tolerance `tol`:

| # | Condition | State |
|---|---|---|
| 1 | `w` is undefined | `undefined` |
| 2 | `w > tol` | `regressed` |
| 3 | there is no range, or `hi > tol` | `inconclusive` |
| 4 | otherwise | `within_tolerance` |

Read in order; the first that holds decides. Comparisons round to 12 decimals first, as
`oracle.headline`'s parity check does (`0.92 − 0.90` is `0.020000000000000018`).

Three properties, each deliberate:

- **The point estimate fails a figure on its own** (row 2). A worsening beyond the
  tolerance is `regressed` even when its range includes 0. The range can keep a figure from
  passing; it can never excuse one.
- **A figure passes only when its whole range is inside the tolerance** (rows 3–4). A
  small suite therefore produces `inconclusive` readily. That is the conservative
  direction for a check, and it is the honest reading of 22 cases.
- **A range that includes 0 decides nothing by itself.** `decide` does not take `lo`;
  `change` does not take a tolerance. A figure can be `regressed` and `not_detectable` at
  once (§12.3), and the report prints both.

A metric with no tolerance is `not_checked`: shown, never judged.

### 7.5 Hard checks, the verdict and the exit code

**Hard checks — no tolerance applies.**

- `candidate_violations`: cases where the candidate's attempt chain names an endpoint
  outside the case's eligible set, or any endpoint at all on a case whose feasible set is
  empty (`candidate_served_on_empty` counts the latter). The rule is `oracle.case_scores`'
  `violation`, unchanged.
- `run_exit_code`: the bench run's own integrity code (`runner._exit_code`). A run that
  exits 2 for any reason — any strategy's violation, an empty-set mismatch, a post-gate
  widening, a pinned mismatch — is a hard failure of the check, whichever arm caused it:
  a comparison made on a run that failed its integrity checks is not a comparison.

**Which figures are judged.**

- `overall`: every metric with a tolerance. One that cannot be judged (`undefined`,
  `insufficient_cases`) counts as inconclusive — a named metric that could not be checked
  cannot pass.
- a group with at least `--min-group-size` cases: every metric with a tolerance whose
  figure is not `insufficient_cases`. A smaller group, and a thin figure inside a large
  one, are listed and not judged ("any sufficiently large group").

No correction is made for the number of figures judged. More groups mean more chances of a
`regressed` or `inconclusive` figure; for a check that is the safe direction, and it is
stated in the docs.

**Verdict — the worst state among the judged figures.**

| Verdict | When | Exit |
|---|---|---|
| `hard_fail` | a hard check failed | **2** |
| `regressed` | no hard failure; at least one judged figure is `regressed` | **3** |
| `inconclusive` | neither of the above; at least one judged figure is `inconclusive`, `undefined` or `insufficient_cases` | **4** |
| `pass` | at least one tolerance was named and every judged figure is `within_tolerance` | 0 |
| `no_hard_failure` | no tolerance was named: only the hard checks ran | 0 |

Exit `1` stays the load/usage error. `2` keeps its existing meaning — a policy violation
or integrity failure — so an injected violating candidate exits 2 through the bench's own
rule whatever the tolerances are (invariant E7). `3` and `4` are new and occur only with
`--candidate`.

`metrics.json`'s `exit_code` remains the bench run's own code (0 or 2), because that file
is otherwise unchanged by the check. The process exit code is in `check.json` and
`run.json`.

### 7.6 Output

- `check.json` — `schema: routeforge.router-version-check/v1`, `verdict`, `exit_code`,
  `effect: "none"`, `arms`, `candidate` (`id`, `name`, `routing`), `plan` (method
  `paired_case_bootstrap`, tolerances, replicates, interval, seed, minimum),
  `hard`, `changed` (cases whose attempt chain differs), `overall`, `groups`, `regressed`,
  `inconclusive`, `caveats`.
- `check.md` — the fixed text, the verdict, the hard checks, every overall figure, and
  each group's figures for the metrics a tolerance was named for. Rows are in dimension,
  label and metric order; nothing is sorted by size.
- `run.json` gains `candidate: {id, name, verdict}` and its `exit_code` is the process's.
- stdout: `feasible_bench: router-version check: <verdict> (nothing was applied)`.

`--tolerance`, `--check-replicates`, `--check-interval` and `--check-seed` without
`--candidate` are usage errors.

**It applies nothing.** There is no flag that writes the candidate anywhere. The new
modules import no admin route, no application store and no HTTP client, and contain no
call that writes a file; the only writes are the runner's, under `--out`. A test pins each
of those statements (invariant E11).

## 8. Fixed text

Constants, pinned word for word by tests (invariant E14). Every new report opens with the
ones that apply, as a block quote, before any figure.

| Constant | Text | In |
|---|---|---|
| `CAVEAT_SIMULATED` | On the simulated backend, quality is an input: each pass or fail is a seeded draw against a probability written in the endpoint file. Pass rate and quality regret measure the harness, not the models. | every report of a simulated run |
| `CAVEAT_LIVE` | This run called the endpoints in the endpoint file, once per case and endpoint, and graded each answer with a deterministic grader. One call is one sample. | every report of a `--live` run, in place of the above |
| `CAVEAT_TIER_RULE` | The router comparison is mostly the tier rule: the target tier is chosen from the request's length, so on short cases the router's regret against the oracle is set by which tier it may use, not by the four-term scorer. | every report |
| `CAVEAT_SYNTHETIC` | Cases are synthetic and single-turn. | every report |
| `CAVEAT_TO_MEASURE` | Live-backend results: TO MEASURE. | every report of a simulated run |
| `CAVEAT_GROUPS` | A group is the cases of this suite that carry one label. A figure over fewer cases than the minimum prints its case count and no rate. A group above the minimum is still small: read its case count with every figure. | `groups.md` |
| `CHECK_NO_EFFECT` | This check applies nothing and promotes nothing. It reads a candidate routing config, scores it offline beside the current one, and writes this report. A routing change is still made by an operator editing config/routing.yaml. | `check.md`, first |
| `CHECK_CONDITIONAL` | The verdict is conditional on this suite, this endpoint file, this simulation seed and this case order. It is not a statement about live traffic. | `check.md` |
| `CHECK_RANGE` | A range is the spread of the paired difference when the suite's cases are resampled. It says how much the difference depends on which cases are in the suite, and nothing about whether the suite resembles real traffic. | `check.md` |
| `CHECK_NO_TOLERANCE` | No tolerance was named, so only the hard checks were applied: the differences below are shown, not judged. | `check.md`, when no tolerance was named |
| `CHURN_SCOPE` (second plan) | A churn result is about the declared perturbation set: single changes, one at a time, each held for a whole run. It says nothing about two changes at once, a change arriving mid-traffic, or a change that is not in the set. | `churn.md` |
| `CHURN_NO_ADVICE` (second plan) | A change that tightens a requirement and raises cost is reported as a cost. Nothing here names a requirement to loosen. | `churn.md` |
| `CALIBRATION_WHAT` (second plan) | The confidence measured here is the backend's own figure for one answer, the one the gateway's escalation threshold reads. RouteForge's router emits no confidence, and RouteForge has no calibrated routing confidence. | `calibration.md` |
| `CALIBRATION_SIMULATED` (second plan) | The simulated backend reports a confidence of 1.0 for every answer, so nothing about calibration can be measured in simulation. The figures below state that; they are not a result. | `calibration.md` of a simulated run |

**Vocabulary** (invariant E13). No key and no fixed sentence of the new outputs contains
`rank`, `recommend`, `best`, `winner`, `prefer`, `should`, `relax` or `vendor`; no key
contains `promot`, `apply` or `applied`. Strategy names are data and are exempt
(`best_published_quality` predates this slice).

## 9. Confidence calibration (second plan)

A new module, `app/eval/calibration_bench.py` (the name `calibration.py` is taken by
P2b-3's quality calibration, which is unrelated), and a flag, `--calibration`.

### 9.1 What is measured

The confidence of §3.2 — the backend's figure for one answer — against that answer's
grade. One observation per matrix row that produced an answer: `(confidence, passed)`.

**Capturing it changes no serving code.** `runner._CountingBackend.generate` already wraps
every sweep call; it additionally records `result.confidence` per `(case, endpoint)`. The
value is written to `calibration.json` only. `matrix.jsonl` is unchanged: it is pinned.

Two populations, reported side by side:

- **every eligible endpoint** — all sweep rows with an answer;
- **as routed** — the rows `router_online` served, which is the population the escalation
  decision actually sees.

### 9.2 Figures

- **Reliability.** Ten equal-width bins on `[0, 1]` (`[0, 0.1)`, …, `[0.9, 1.0]`, the last
  closed). Per bin: `n`, mean confidence, pass rate. The binning is in the output.
- **Expected calibration error.** `ECE = Σ_b (n_b / N) · |pass_rate_b − mean_confidence_b|`.
- **The escalation threshold's operating point**, at the deployment's
  `routing.confidence_escalation_threshold` (escalation fires when `confidence <
  threshold`):
  - `catch_rate` — of the failing answers, the share below the threshold;
  - `needless_rate` — of the passing answers, the share below the threshold;
  - `escalated_share` — of all answers, the share below the threshold;
  - `failing_among_escalated` — of the answers below the threshold, the share that failed.
- **The same four figures at each tenth from 0.1 to 0.9**, in that order. No row is marked;
  no threshold is recommended.
- `default_confidence_share` — the share of answers whose confidence is exactly `0.9`,
  `DEFAULT_CONFIDENCE`: a backend that returned no logprobs. A real 0.9 cannot be told from
  the default in the record; the share is printed so a reader can.
- `temperature` — `routing.confidence_logprob_temperature`, on which the whole mapping
  rests.
- By **tier** of the answering endpoint, and by the four dimensions of §4.1 — never by
  endpoint or model: that would be a ranking of models (§2).

A figure over fewer than `--min-group-size` answers is `insufficient_cases`.

### 9.3 When there is nothing to calibrate

If every confidence in a population is the same value, the state is
`constant_confidence`: the reliability table has one row, the ECE is `|pass_rate − c|`
and is labelled as a restatement of the pass rate, and the operating point is trivial
(nothing below the threshold, or everything). **That is every simulated run**, where the
value is 1.0. The report then opens with `CALIBRATION_SIMULATED`, and the research notes
record the negative statement with its seed: in simulation, calibration is not measurable,
by construction.

A simulated confidence model is deliberately **not** added to the endpoint file. It would
be a second hand-written input, and "calibration" of one hand-written number against
another measures nothing.

Live figures are **TO MEASURE**.

### 9.4 What would make it measurable in production

The smallest record change, filed as backlog (§14), not built here: put the answer
confidence and its source (`logprob` / `default` / `none`) on the `step="router"`
`DecisionRecord` and on the `routing_outcomes` row. Both are content-free numbers. With
voluntary feedback as the label, the figures of §9.2 could then be computed from recorded
traffic by `scripts/outcome_report.py`.

A score-margin analysis (does the gap between the top two candidates' scores predict
regret?) was the fallback had no confidence existed. One does, so it is not built; it is
noted in the same backlog entry.

## 10. Determinism and the default-output pin

- **Same suite, seed, endpoint file, config and flags give byte-identical files**:
  `groups.json`, `groups.md`, `check.json`, `check.md`, as `metrics.json` and `matrix.jsonl`
  already are. No clock, no global random state; every draw is seeded by its arguments;
  floats are rounded to 12 decimals by `oracle._round_value`; JSON is written with sorted
  keys. Renderers iterate the dimension and metric constants, never a dict's order.
- **The default output is pinned for the first time.** `tests/eval/test_bench_golden.py`
  runs four simulated cases — starter × healthcare-sim × the b45 variants at seed 0, the
  same suite and endpoints at seed 1, outage × outage-sim, empty-set × cloud-only-sim — and
  compares `metrics.json`, `matrix.jsonl`, `choices.jsonl` and the body of `report.md`
  (from "## How to read this"; the "## Run" block above it holds timestamps and the git
  sha) with stored copies.
  - The seed-1 case is there for a reason found while the pin was being built: at seed 0 no
    starter case is infeasible at floor (no eligible endpoint passes), and a deliberate
    mutation of that branch of the refactored loop (§5) passed the other three cases. At
    seed 1 some cases are infeasible at floor, and the pin fails on the mutation.
  - `fixtures/bench_golden/inputs/` holds a copy of every input **and of the four config
    YAMLs**, and the test points `runner.load_settings` at that copy and forces
    `SP_PII_DETECTION=regex`. The pin is of the harness code, not of today's `config/`.
  - The expected files are written from the tree **before any product code of this slice
    exists**. They are never regenerated to make a test pass.
- `run.json` is not byte-stable (timestamps) and is not pinned; an unflagged run's `args`
  keeps exactly its current keys.

## 11. Invariants (each pinned by a named test)

First plan:

| # | Invariant |
|---|---|
| E1 | With no new flag, `metrics.json`, `matrix.jsonl`, `choices.jsonl` and the report body are byte-identical to the pre-slice tree; a default run writes exactly five files |
| E2 | Same inputs and flags give byte-identical `groups.*` and `check.*` |
| E3 | Every case is in exactly one group per dimension; a dimension's group sizes sum to the suite |
| E4 | The overall figure equals the combination of a dimension's groups weighted by each figure's own denominator; weighting by case count is wrong for a conditional metric (counter-example pinned) |
| E5 | A figure below the minimum prints a count and no rate; the rule is on the figure's denominator; counts are never suppressed; the minimum is inclusive |
| E6 | Group figures and the worst group match a hand-computed example; a tie and an all-equal metric are handled as §4.4 says |
| E7 | A candidate with any ineligible choice, or an attempt on an empty feasible set, is `hard_fail`, exit 2, whatever the tolerances |
| E8 | A candidate identical to the baseline has worsening 0 and range `[0, 0]` on every figure and exits 0 with every tolerance at 0 |
| E9 | The check document does not depend on the order of the cases |
| E10 | Worsening, the decision rule and the tolerance boundary match hand-computed examples |
| E11 | Nothing is applied: a check run writes only under `--out`, changes no config or input file; the new modules import no serving surface that can change a deployment and call nothing that writes; no file outside `app/eval/` mentions the candidate or the check |
| E12 | No serving module imports `app.eval` |
| E13 | No ranking, recommendation or promotion vocabulary in any key or fixed sentence |
| E14 | The fixed text is the spec's, and opens every new report |
| E15 | Nothing stated is silently ignored: a candidate key the bench cannot evaluate, an unknown key, a flag without its parent flag, a value out of range — each is a load error naming it |
| E16 | The candidate adds arms and changes nothing else: the sweep matrix and every other strategy's metrics and choices are identical with and without it |
| E17 | A range that includes 0 decides nothing by itself: the lower end is not an input of the decision, and a figure can be `regressed` and `not_detectable` at once |

Second plan:

| # | Invariant |
|---|---|
| E18 | No perturbation of the built-in set produces a violation on the starter suite |
| E19 | A strategy injected to choose an ineligible endpoint is caught under every perturbation: each perturbed run reports the violation and churn exits 2 |
| E20 | A perturbation that empties a feasible set yields a rejection by the real pipeline, with no model call and a conflicting core; `served` is 0 |
| E21 | Every perturbation's `expect` holds on the starter suite: a tightening never widens an eligible set; a reprice, an outage and a `client_output` flip leave every eligible set unchanged; a removed or disabled endpoint is in no eligible set |
| E22 | The built-in set is deterministic, its ids are stable, and each perturbed input differs from the original in exactly the declared field |
| E23 | The reliability bins, the ECE and the operating point match a hand-computed example (§12.4) |
| E24 | A constant confidence is a state, not a calibration; a simulated run opens with the pinned negative statement |
| E25 | Capturing the confidence changes no default output and no serving code |

E1, E2, E12, E13, E14 and E15 are extended to the second plan's outputs by that plan.

## 12. Hand-computed examples

### 12.1 Group figures (E4, E6)

Eight cases, one strategy. `-` means the case is outside that figure's denominator.

| case | workload | placement | passed | quality regret | floor miss | cost regret |
|---|---|---|---|---|---|---|
| c1 | faq | any | 1 | 0 | 0 | 0.2 |
| c2 | faq | any | 1 | 0 | 0 | 0.0 |
| c3 | faq | any | 0 | 1 | 1 | - |
| c4 | faq | any | 1 | 0 | 0 | 0.4 |
| c5 | faq | onprem_only | 1 | 0 | 0 | 0.0 |
| c6 | claim | onprem_only | 1 | 0 | 0 | 0.6 |
| c7 | claim | onprem_only | 0 | 1 | 1 | - |
| c8 | claim | onprem_only | 0 | 1 | - (no endpoint passes) | - |

- `pass_rate`: faq 4/5 = 0.8; claim 1/3; overall 5/8 = 0.625.
  `(5 × 0.8 + 3 × 1/3) / 8 = 5/8`. ✓
- `cost_regret`: faq 0.6/4 = 0.15 (n = 4); claim 0.6/1 = 0.6 (n = 1); overall 1.2/5 = 0.24.
  By denominator: `(4 × 0.15 + 1 × 0.6) / 5 = 0.24`. ✓
  By case count: `(5 × 0.15 + 3 × 0.6) / 8 = 0.31875`. ✗ — the counter-example of E4.
- At a minimum of 3: claim prints a pass rate (n = 3), **no** cost regret (n = 1) and
  **no** floor-miss rate (n = 2, c8 has no passing endpoint).
- Worst `pass_rate` among the printed groups: `workload = claim`, 1/3, n = 3.

### 12.2 A constant shift (E10)

Ten cases; the candidate costs exactly 0.25 more on every case. Whatever a resample draws,
the paired difference of the two means is 0.25: worsening 0.25, range `[0.25, 0.25]`,
`change: worse`. Against a tolerance of 0.25: `within_tolerance` (the boundary is
inclusive). Against 0.2: `regressed`.

### 12.3 Regressed and not detectable at once (E17)

Six cases; the baseline costs 1 on each; the candidate costs 10 on one and 0 on the other
five. Worsening of the mean cost: `10/6 − 1 = 4/6 = 0.667`. A resample of six misses the
dear case with probability `(5/6)^6 = 0.335`, and then the difference is −1: the 90% range
starts at −1 and includes 0. Against a tolerance of 0.5 the figure is `regressed` (row 2)
and its `change` is `not_detectable`. The verdict is `regressed`.

### 12.4 Calibration (E23, second plan)

Ten answers, confidence and grade:

`0.95 P · 0.92 P · 0.91 F · 0.85 P · 0.75 P · 0.72 F · 0.55 F · 0.52 P · 0.35 F · 0.15 F`

| bin | n | mean confidence | pass rate | gap |
|---|---|---|---|---|
| [0.9, 1.0] | 3 | 0.926667 | 0.666667 | 0.26 |
| [0.8, 0.9) | 1 | 0.85 | 1.0 | 0.15 |
| [0.7, 0.8) | 2 | 0.735 | 0.5 | 0.235 |
| [0.5, 0.6) | 2 | 0.535 | 0.5 | 0.035 |
| [0.3, 0.4) | 1 | 0.35 | 0.0 | 0.35 |
| [0.1, 0.2) | 1 | 0.15 | 0.0 | 0.15 |

`ECE = (3×0.26 + 1×0.15 + 2×0.235 + 2×0.035 + 1×0.35 + 1×0.15) / 10 = 1.97 / 10 = 0.197`.

At the threshold 0.60, four answers are below it (0.55 F, 0.52 P, 0.35 F, 0.15 F). Five
answers fail, three of them below: `catch_rate = 0.6`. Five pass, one of them below:
`needless_rate = 0.2`. `escalated_share = 0.4`; `failing_among_escalated = 0.75`.

## 13. Docs

The first plan updates:

- `docs/guides/feasible-route-bench.md` — the new flags in §5; a new section on the group
  report and one on the router-version check (the candidate file, tolerances, the decision
  rule, how to read `check.md`, the fixed text); the two new exit codes in §7.
- `docs/research/feasible-route-bench.md` — a new section: the definitions, each invariant
  and its test, the hand-computed examples, results on the simulated starter suite
  labelled as simulated with their seeds, and live results **TO MEASURE**; §3's metric 6
  and §6.5 are updated to say what is now reported and what still is not.
- `docs/research/arxiv.md` — §3.3 and §4.6: what the group report and the check let the
  paper claim and what they do not; experiments E11a–E11f; a threat to validity.
- `docs/roadmap/priorities-1-3-status.md` — the slice in Built; §2.2 keeps the LLM judge
  and online learning as paused, and keeps churn and calibration as the next plan.
- `docs/backlog/bugs.md` — B67, the follow-ups of §14.

The second plan adds the churn and calibration sections to the same files, changes
`runner.DEFERRED_METRICS` and research §6.5 (a deliberate change to the default report,
made with a reviewed regeneration of the golden report body), and files the record change
of §9.4.

## 14. Follow-ups (not built)

Filed as **B67**:

1. **Confidence on the record** (§9.4): the answer confidence and its source on the router
   `DecisionRecord` and the `routing_outcomes` row, so calibration can be computed on
   recorded traffic. A score-margin analysis from `sp.scoring` belongs with it.
2. **Policy dimensions derived from the gate**, not from tags (§4.1): group by the compiled
   residency and protection axes the run already computes.
3. **More than one seed in a check.** A verdict is for one simulation seed and one case
   order. Running the check over several seeds and reporting the spread of verdicts is the
   obvious next step.
4. **A multiplicity note or correction** for the number of figures a check judges.
5. **Candidate keys the bench cannot evaluate** — the escalation threshold above all. It
   needs an escalation-aware sweep (a second call per case at the next tier), which the
   bench deliberately does not make.
6. **A baseline overlay.** The baseline is always the config as loaded. Comparing two
   candidates needs the first applied to a scratch config directory today.

## 15. Two plans

Four deliverables with their tests and docs are about eighteen tasks: too many for one
reviewable plan.

**First plan — `2026-10-07-p2-eval-metrics.md` (nine tasks):** the golden pin, the per-case
scores, the group report, the candidate overlay, the router-version check, docs.

**Second plan — `p2-eval-churn-calibration` (to be written):** churn (§6) and calibration
(§9), with E18–E25.

Why this cut:

- The per-case scores (§5) are the one change to existing code that everything else rests
  on, and the group machinery is used by the check ("in any sufficiently large group"),
  by churn's deltas and by calibration's breakdowns. They come first whatever follows.
- The group report and the check are both **read-outs of one ordinary run**. Churn runs
  the bench many times over rewritten inputs and adds a harness field; calibration adds a
  capture to the sweep. Those are a different shape of work.
- The check is the roadmap's named open item with a product rule riding on it (rule 4), so
  it is the one to get reviewed first and alone.
- The second plan changes the default report (`DEFERRED_METRICS`); the first changes no
  default output at all, which keeps the golden pin's first use clean.

## 16. Where this design departs from the brief

1. **"Tenant" is `application`.** The bench has one tenant id; a case carries a policy,
   not an identity. An application is `tags.app`, else the policy's fingerprint (§4.1).
2. **The group report is behind `--group-report`, not on by default.** The brief asks for
   a "standing output" and for default output to be unchanged; both cannot hold for the
   same file. The report is standing in the sense that matters: one flag, every strategy,
   every run, and the check and churn build on it. `metrics.json`'s older `groups` block is
   untouched.
3. **The minimum applies to each figure's denominator, not to the group's case count.**
   The brief says "a group below a minimum size". A group can be large and one of its
   figures thin (cost regret is over passing cases); the stricter reading is the honest
   one.
4. **Counts are printed for a group below the minimum.** A violation must not disappear
   into "insufficient cases".
5. **The candidate is an overlay on the loaded config, not a second config directory**
   (§7.1, §3.3). The brief offered both and asked for the choice to follow the code.
6. **Some routing keys are refused** (§7.1), the escalation threshold among them. The
   bench cannot evaluate them, and accepting them would produce a false "no regression".
7. **Four verdict exit codes, not one "non-zero".** 2 hard fail, 3 regressed, 4
   inconclusive. The brief asks for non-zero on (i) and (ii); `inconclusive` is the
   conservative reading of "a delta whose range includes 0 is … not a pass … by itself"
   once the tolerance is taken into account, and CI needs to tell the three apart.
8. **A regression is decided by the point estimate; the range can only withhold a pass**
   (§7.4). The brief asks for the rule to be explicit and conservative. The alternative —
   failing only when the whole range is beyond the tolerance — would let a small suite
   pass almost anything.
9. **With no tolerance named the verdict is `no_hard_failure`, not `pass`.**
10. **The check fails on the run's integrity code, not only on the candidate's own
    violations** (§7.5).
11. **"Routing confidence" is the backend's answer confidence** (§3.2, §9). The router has
    none. The calibration deliverable measures that one, says in fixed text that RouteForge
    has no calibrated routing confidence, and reports a measured negative for simulation.
12. **No simulated confidence model** (§9.3), so the simulated calibration result is the
    negative statement and nothing else.
13. **Churn perturbs inputs on disk and runs the unmodified bench** (§6.3), instead of a
    perturbation parameter inside the runner. It does not use the perturbed run's exit
    code, because the suite's `expect_empty` flags describe the unperturbed world.
14. **`disable` needs an `enabled` field on the harness's endpoint spec**, and churn needs
    the conflicting core of an empty case in memory (`RunResult.empty_cores`). Both are
    harness-only.
15. **Each perturbation carries an `expect`** and an unmet one fails churn. The brief asks
    for violations to stay 0; "a tightening never widens an eligible set" is the same
    guarantee one level up, and the bench can check it for free.
16. **The work is split into two plans** (§15).
