# FeasibleRouteBench: evaluating an eligibility-constrained LLM router against the feasible oracle

**Status:** research notes, written as the code landed. 2026-09-21.
**Branch:** `feat/feasible-route-bench` (base `0f9e823`). Design:
`docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md` (binding; §6 amended in
`aa50dc2`). Plan: `docs/superpowers/plans/2026-09-21-feasible-route-bench.md`.
**Predecessors:** P1a ([`policy-compiler-noninterference.md`](policy-compiler-noninterference.md)),
P1b (`app/policy/feasible.py`), P2a ([`routing-signal-provenance.md`](routing-signal-provenance.md)),
signal hardening (`SignalStore.replay`).
**Slice:** P2b-1, the offline harness. P2b-2 (production outcome records) and P2b-3
(learning bounded by the feasible set) are not built.
**Audience:** whoever writes the paper.

This is the evidence file, not the paper. It records what was built, what the first runs
show, and what they do not show. **Every number in §5 comes from a simulated run.** Its
quality inputs were written by hand into an endpoint file. Read §6 before you write any
sentence that claims a result.

The honest one-line summary: **this slice builds the instrument and checks that it is
calibrated. It does not yet produce a finding about routing quality.** The first B45
result is null, and that null is predicted by the scoring formula (§5.5). The router
comparison mostly measures a tier-targeting rule, not the scorer (§5.3).

---

## 0. File before you publish

The harness is the evaluation half of **R1**, "Eligibility-Constrained LLM Routing: When
the Candidate Pool Is a Per-Request Variable", in
[`docs/roadmap/ip-and-research-opportunities.md`](../roadmap/ip-and-research-opportunities.md).
The *feasible oracle* (best **allowed** endpoint, not best registry endpoint) is R1's
evaluation claim. The same caution as P1a and P2a applies. Public disclosure starts
clocks. The landscape scan behind that document is a floor on prior art, not a ceiling.
Nothing here is legal advice.

---

## 1. The problem

Router benchmarks score a choice against the best model in a **fixed** candidate set.
RouteForge's candidate set is not fixed. Per request, the policy gate and
`feasible_set()` remove endpoints by residency, region, retention and per-app allow-lists
before anything is scored. Scoring RouteForge against the registry-best endpoint penalises
it for obeying policy. In the handoff's §14 example the registry-best endpoint is a
European model. A US-only claims workload may never send data to it.

The correct comparator is the **feasible oracle**: the best outcome among the endpoints
the request was allowed to use. Production traffic cannot compute it, because production
observes only the endpoint it chose. So P2b-1 is an **offline sweep**. For every case,
every eligible endpoint is called through the real pipeline. The oracle is then computed
from the full matrix, and each strategy is scored by looking its choice up in that
matrix.

### 1.1 Boundary

- `app/eval/` plus `scripts/feasible_bench.py`. Operator-run and outside the serving
  process. Nothing on the serving path imports `app.eval` (asserted, §4).
- The harness runs the **real** gate, `compile_policy`, `feasible_set`, redaction,
  compression and `Pipeline.run`, in process. It does not re-implement them and does not
  call over HTTP.
- It builds its own `Settings`, an in-memory SQLite database and a scratch
  `ModelRegistry`. It **never touches a deployment's database**.
- **No eligibility bypass.** Every endpoint swept is one that the post-gate
  `feasible_set()` returned as eligible for that case. A sweep call that served anything
  else is a violation (§3, metric 1).
- `harness_settings` **empties the stock YAML `model_pool`**, so the endpoint file is the
  whole endpoint universe. This was found by a failing test in Task 6 (`fb29403`). Before
  the fix, enabling `azure` for a file endpoint also pulled the deployment's own
  `azure-gpt-mid` into the pool. Under `--live` it would have been **called for real
  although the operator never listed it**. Pinned by
  `tests/eval/test_endpoints.py::TestHarnessSettings::test_static_yaml_pool_is_excluded`.

---

## 2. The run, per case

1. **Pre-gate set.** `feasible_set(settings, registry, app_policy)` over the case's
   declared policy, before any content is inspected.
2. **Gate.** `Gate.evaluate` runs with the detector, rules and compiled effective policy
   that `Pipeline._gate_inputs` builds. That is the same composition point every pinned
   `pipeline.run` uses, so the harness sees the same context the pipeline does.
3. **Post-gate set** `E(c)`. This set is authoritative. The gate may only **tighten**
   placement. If `E(c) ⊄ pre-gate set`, the case counts as `post_gate_widened`, and the run
   exits 2.
4. **Sweep.** For each `e ∈ E(c)` in slug order, the harness makes one pinned
   `ChatRequest(model=e)` through `Pipeline.run`. That makes the model see exactly the
   model-facing view production would show it. The harness records: grader quality, pass,
   `cost.total_usd`, the backend latency from the last `router` trace record, prompt and
   cached tokens, `served`, and `error`. Completion text is dropped unless you pass
   `--keep-text`.
5. **Strategies.** Each strategy maps `(case, E(c)) -> slug | None`. Its outcome is looked
   up in the matrix. Strategies make no extra model calls.

| Strategy name (as it appears in outputs) | Rule |
|---|---|
| `cheapest` | lowest **estimated** cost (the router's own `price_call` estimate), ties by slug |
| `best_published_quality` | highest registry quality through the router's `QualityResolver` (tier default when unset), ties by estimated cost then slug |
| `fixed:<slug>` | that slug when eligible, else `cheapest` (`fallback_used` in `choices.jsonl`) |
| `router_cold` | real `Router.select` over a **fresh, empty** `SignalStore` |
| `router_warm` | real `Router.select` over a `SignalStore` that has **replayed the sweep's own rows** (§6.4) |
| `router_cold:<variant>` / `router_warm:<variant>` | the same, with a weights mapping from `--variants` (the B45 instrument) |

(The spec calls the variant strategies `router_weights:<name>`. The shipped names follow
the plan's brief and are the ones to cite.)

**Simulated backend** (`app/eval/simulated.py`, `8b3c403`). For `(case, endpoint)` it
draws `u = Random(f"{seed}:{case.id}:{slug}")`. It returns the case's passing answer if
`u < quality[workload or default]`, otherwise the literal `SIMULATED-WRONG`. Latency is
`mean + jitter·(2u₂−1)` from a second seeded draw, floored at 1 ms, with no sleep. Cost is
`price_call` over the pipeline's token counts. Confidence is fixed at 1.0 so gateway
confidence escalation never fires. It has **no failure-injection mode**. This matters for
B45 (§5.5).

---

## 3. Formal definitions (spec §6, as amended in `aa50dc2`)

For case `c` with post-gate eligible set `E(c)`, sweep quality `q(c,e) ∈ [0,1]`, cost and
latency, and floor `f(c)` (default 1.0):

- `q*(c) = max_{e∈E(c)} q(c,e)`
- `P(c) = { e ∈ E(c) : q(c,e) ≥ f(c) }`, the passing set
- `c*(c) = min_{e∈P(c)} cost(c,e)`, `l*(c) = min_{e∈P(c)} latency(c,e)`; undefined when
  `P(c) = ∅` (**infeasible-at-floor**)

The oracle is computed over `E(c)` only. Rows for any other endpoint are filtered out
before `q*`, `P`, `c*` and `l*` are formed (`820bec2`).

For strategy `s` choosing `s(c)`:

| # | Metric | Definition |
|---|---|---|
| 1 | Policy-violation rate | share of cases with `s(c) ∉ E(c)`. A **missing choice record**, or a choice with no matrix row, also counts. Separately, any sweep call that **served** a non-eligible slug counts. |
| 2 | Quality regret, pass rate | mean `q*(c) − q(c,s(c))` over non-empty cases; pass rate = share with `s(c) ∈ P(c)` |
| 3 | Cost regret, floor-miss rate | mean `cost(c,s(c)) − c*(c)` **over cases where `s(c) ∈ P(c)` only**. Floor-miss rate = among cases with `P(c) ≠ ∅`, the share where `s(c) ∉ P(c)`. **Denominator: cases with `P ≠ ∅` only.** |
| 4 | Latency regret | same shape as 3; error rows (latency undefined) are excluded from latency aggregates |
| 5 | Empty-set handling | per case: `expect_empty` must equal `E(c) = ∅`, and an empty case must be rejected with ≥ 1 exclusion reason and no model call |
| 6 | Worst group | metrics 2–4 (quality/cost/latency regret, pass rate, floor-miss rate) per value of the tags `workload`, `placement`, `protection`. For each metric the worst group is named (highest regret or floor-miss, lowest pass rate). No other tag, `synthetic` included, is ever a grouping dimension. |

**Cost and latency regret are never averaged over floor misses.** A cheap wrong answer
does not count as a saving. The cost of that rule is that a strategy can have zero cost
regret and a poor pass rate at the same time, and `cheapest` does on some seeds (§5.6).
Always read metric 3 together with its floor-miss rate. An aggregate with no contributing
cases is `null` (rendered `n/a`), never `0.0`.

A `None` choice on a non-empty case is not a violation, but it scores quality 0 and counts
as a floor miss when `P(c) ≠ ∅`. A `None` choice on an empty case is the correct answer.

**Headline (handoff §20).** The headline is `router_warm` mean chosen cost vs
`best_published_quality` mean chosen cost, as a saving percentage. It is printed only
when `round(|pass_rate_router − pass_rate_ref|, 12) ≤ parity_tol` (default 0.02) and the
reference cost is defined and `> 0`. Otherwise the report states both pass rates and
makes no claim. The 12-decimal rounding exists because `|0.90 − 0.92|` evaluates to
`0.020000000000000018` in IEEE-754 (`820bec2`).

**Exit codes.** `0` means ok. `1` means a load or usage error, including a gate exception
on any case (treated as a configuration error; `matrix.jsonl` is left partial and no
`metrics.json` is written). `2` means a policy violation or an empty-set mismatch: any
strategy `violation_rate > 0`, any ineligible sweep-served model, any post-gate eligible
set that is not a subset of the pre-gate set, or any empty-set mismatch.

**Deferred, and named in every report:** calibration (the router emits no confidence),
policy/endpoint churn robustness, and the noninterference failure rate (owned by P1a's
harness).

---

## 4. Invariants, and the test that pins each

All tests live under `tests/eval/`: 153 test functions, 155 collected cases, about 14 s.

| Invariant | Pinned by | Falsified? |
|---|---|---|
| **Violation guard.** An ineligible choice makes metric 1 > 0 and the run exits 2 | `test_runner.py::test_injected_ineligible_choice_exits_2` monkeypatches `cheapest` to return a slug outside `E(c)`. The run exits 2 with `violation_rate > 0` for that strategy and 0 for the others. `test_cloud_choice_on_onprem_case_exits_2` is the realistic form: always choosing cloud violates on the `onprem_only` case. | **Yes.** Both tests inject the fault the guard exists to catch. `test_oracle.py::TestSpec14Fixture` also checks the sweep-call half of `violations()` with a served-european row. |
| **A missing choice is a violation, not a silent pass** | `test_oracle.py::TestNoneChoiceOnNonEmptyCase::test_missing_choice_record_entirely_is_a_violation` | Yes (the assertion was flipped in `820bec2` when the rule changed) |
| **The oracle ignores non-eligible rows** | `test_oracle.py::TestOracleIgnoresNonEligibleRows` (a contaminating row must not turn a real floor miss into a pass, but `violations()` still flags it) | Yes |
| **Floor misses never produce negative cost regret** | `test_oracle.py::TestFloorMiss` | Yes. A cheap failer chosen gives `floor_miss_rate = 1.0` and `cost_regret = None`, not a negative number |
| **No spurious `-0.0` regret** | `test_oracle.py::TestNoNegativeZeroRegret` (quality = cost = latency = 2/3 checks the sign bit and the JSON text) | Yes. The first implementation produced `-0.0` (`db25203`, fixed `820bec2`) |
| **Determinism.** Same seed gives a byte-identical `metrics.json` | `test_runner.py::test_same_seed_gives_byte_identical_metrics` (also `matrix.jsonl`). **Across processes** as well: the starter run at seed 0 with variants, under `PYTHONHASHSEED` = 1, 2 and 3, gives md5 `6ab2ee35fdfdbf8044a940114caa1363` each time, identical to an unset-hash run (re-verified for this document at `82bc968` and again at `e8e8682`) | Partially. `test_simulated.py::TestDeterminism::test_different_seed_changes_at_least_one_outcome_over_50_draws` shows the seed is not ignored |
| **The warm store reads against a frozen clock** | `test_strategies.py::test_frozen_read_clock_is_stable_across_real_elapsed_time`, `…_when_the_live_clock_advances` | Yes. An earlier draft read the live clock, which broke determinism (`e7f7bee`) |
| **Model-facing view.** The pinned call sees what production would show | `test_runner.py::test_model_facing_view_redacted_for_enforced_raw_for_passthrough`. The simulated backend never receives `078-05-1120` for an `enforced` case, and does receive it for a `passthrough` case | Yes. The passthrough arm shows the check would see the SSN if it arrived |
| **Pinned sweep serves the pinned endpoint** | `test_runner.py::test_every_row_served_its_pinned_endpoint` | n/a (all rows must satisfy it) |
| **`onprem_only` is absolute in the sweep** | `test_runner.py::test_onprem_only_case_never_sweeps_cloud`; `test_endpoints.py` (`azure` spec excluded under `residency="onprem_only"`) | n/a |
| **Empty set.** An expected-empty case is rejected with reasons and no sweep; a mismatch exits 2 | `test_runner.py::test_empty_set_expected_is_rejected_without_calls`, `test_empty_set_unexpected_exits_2` | Yes (flipping `expect_empty` gives exit 2) |
| **Serving isolation.** Nothing under `app/` except `app/eval/` imports `app.eval` | `test_isolation.py::test_no_serving_module_imports_app_eval` | Yes. `test_scanner_detects_an_import` feeds the scanner a module that does import it |
| **Live safety.** No endpoint the operator did not list, and no call without consent | `test_endpoints.py::TestHarnessSettings::test_static_yaml_pool_is_excluded`; `test_runner.py::test_cli_live_endpoint_without_live_flag_exits_1`, `test_cli_live_declined_at_prompt_makes_no_calls` (target `127.0.0.1:9`, never contacted) | Yes (the pool leak in §1.1 was the RED) |
| **Loader rejections** (each §4 load error, including untagged PII-shaped content and a key-looking `api_key_env`) | `test_cases.py::TestLoadSuiteErrors`, `test_endpoints.py` load-error tests | per error |
| **Variants reach the real router** | `test_strategies.py::test_availability_weight_flips_choice_between_slow_available_and_fast_flaky`. With two same-tier candidates, same price and same quality, availability weight 0 picks `fast-flaky` and weight 5 flips the choice to `slow-available` | Yes. This is the proof that the B45 instrument *can* move a ranking. §5.5 explains why it does not on the starter suite |

**Known weak spot.** The "no model call" half of an empty case's `rejected_ok` is read
from a per-case call counter. That counter only attributes calls after a sweep call has
set the case, so on an empty case it reads 0 by construction. The load-bearing guarantee
is structural: the sweep loop iterates over `E(c)`, which is empty. The counter adds no
independent evidence. Recorded as a deferred review minor in the SDD ledger.

---

## 5. Results (simulated)

### 5.1 Run stamp

```
suite            eval/suites/starter.jsonl      22 cases
suite_hash       3339c7735b5d22df84dbd3ec470ee91513c06c0d50ea033e2a99ac79a95d8199
endpoints        eval/endpoints/healthcare-sim.yaml   (local-llama, azure-us, european, general-us)
endpoints_hash   04eccd8f75060e554eac7584ee3238667cb0d61773f42551837c738c855c4d96
variants         eval/variants/b45.yaml         (default, availability_0/1/2)
seed             0
git_sha          e8e8682787e4423d6ffb35193641ca7e5d2f1f6e
routing_policy_version  a4bf5708d2db      registry_version  86294e62adea
mode             simulated                exit 0; every policy counter 0
```

`routing_policy_version` hashes the **stock** routing and pricing config the harness
loaded, before the harness empties `model_pool` and without any variant weights. It
identifies the deployment configuration, not the endpoint universe. The endpoint universe
is `endpoints_hash` + `registry_version`.

The worktree venv has no Presidio, so every run here used `SP_PII_DETECTION=regex`. The
cases set `pii_detection: regex` per app, so this sets the deployment default and not the
per-case detector.

The suite has 8 public FAQ cases (`residency: any`), 10 patient-claim cases and 4
`onprem_only` cases. The claim cases carry synthetic PII only: reserved SSNs
`078-05-1120` and `219-09-9999`, and `@example.com` emails.

### 5.2 The §14 worked example, as computed by the harness

The handoff's §14 table gives four endpoints. The European model has the highest quality
(94) and is ineligible for a US-only workload. The general US model retains prompts.
Local Llama accepts raw data at $0.08. Azure US needs redaction at $0.03.
`healthcare-sim.yaml` encodes that table.

**Two claim-case configurations are in the suite, deliberately.**

- **8 cases (`claim-001`–`008`, tag `placement: us_only`)** set
  `escalate_residency_on_detection: false`, plus `allowed_regions: [us]`,
  `require_no_retention: true` and `policy_mode: enforced`. This reproduces §14's
  `US_ONLY` + redaction placement. The override is a **declared per-app setting**
  (CLAUDE.md rule 2a: `sensitivity_rules` may tighten residency on a detected entity,
  *gated per app* by this field). It is not a harness bypass. It says "this app wants
  US-only placement with redaction, not automatic escalation to on-prem". Computed:
  `E(c) = {azure-us, local-llama}`. `european` is rejected `region_not_allowed` and
  `general-us` is rejected `prompt_retention_not_allowed`. Pre-gate and post-gate sets
  are equal.
- **2 cases (`claim-009`, `010`, tag `placement: escalated`)** leave the field at the
  production default. `config/policy.yaml`'s sensitivity rules escalate a detected
  `US_SSN` to `onprem_only`. Computed: pre-gate `{azure-us, local-llama}`, post-gate
  `E(c) = {local-llama}`, with `azure-us`, `european` and `general-us` each rejected
  `residency`. This is the gate tightening placement in the recorded data: post ⊂ pre,
  strictly.

Over the 8 `us_only` cases at seed 0 (8/8 pass on both endpoints):

| endpoint | mean cost (USD) | mean latency (ms) | pass | chosen by |
|---|---|---|---|---|
| `azure-us` | 0.001470 | 607.3 | 8/8 | `cheapest`, `router_cold`, `router_warm` (all variants) |
| `local-llama` | 0.003920 | 908.6 | 8/8 | `best_published_quality` (published quality 0.92 > 0.89) |

`c*` is `azure-us` on every one of the 8 cases. `best_published_quality` has cost regret
**0.00245 USD** and latency regret **301.3 ms** per case on the `us_only` group. That is
its worst group for both metrics. The router has zero regret there. A registry-best
comparator would have scored every strategy against `european`, which none of them may
use. That is the error the feasible oracle exists to prevent.

**Scale caveat.** The YAML header calibrates prices to about 1,000 tokens per claim
exchange. The measured claim prompts are about 44 tokens (mean prompt_tokens 43.9). So
absolute costs are roughly 1/20 of §14's per-request figures. The **ratio** is preserved
(80:30 per token, so local-llama/azure-us = 2.67 on every case). The header comment is
left uncorrected on purpose. Any edit to the file changes `endpoints_hash`, and a price
recalibration would change every cost figure here.

### 5.3 Strategy results, seed 0

| strategy | pass rate | quality regret | cost regret (USD) | latency regret (ms) | floor-miss rate | mean chosen cost |
|---|---|---|---|---|---|---|
| `cheapest` | 1.000 | 0 | 0 | 0 | 0 | 0.001441 |
| `best_published_quality` | 1.000 | 0 | 0.000891 | 109.6 | 0 | 0.002332 |
| `router_cold` | 0.909 | 0.0909 | 0.000032 | 30.7 | 0.0909 | 0.001490 |
| `router_warm` | 0.909 | 0.0909 | 0.000032 | 30.7 | 0.0909 | 0.001490 |

Violation rate is 0 for all 12 strategies. No case is infeasible-at-floor at seed 0.
**Headline: not comparable.** `router_warm` 0.909 vs `best_published_quality` 1.000
exceeds the 0.02 tolerance, so no saving is claimed.

**What the router's numbers actually measure.** `Router.select` filters to a single
**target tier** before scoring. The target comes from `complexity = min(1, tokens/2000)`
(`app/pipeline.py:905`). Every starter case is short, so the target is `cheap`. On FAQ
cases, where all four endpoints are eligible, the router therefore never considers
`european` or `general-us` (tier `mid`), though both are cheaper and better than `azure-us` here. It picks
`azure-us`, the cheaper of the two `cheap` endpoints. Its two floor misses (`faq-005`,
`faq-006`) are seed-0 draws where `azure-us` returned the wrong answer. Its FAQ cost
regret, 0.000107 on the FAQ group, is the price of the tier filter under a price table
where `mid` undercuts `cheap`. That inversion comes from §14's illustrative prices. Tier
labels are operator declarations, and nothing forces `mid` to cost more. This is a
structural finding about the tier rule, and it holds whatever the seed. It is not a
finding about the scorer.

### 5.4 Cold vs warm

**`router_cold` and `router_warm` chose the same endpoint on every case, at every one of
seeds 0–9.** This is expected here, not evidence that warm state is useless.

- Inside the target tier, the contested cases are FAQ and `us_only` claims. There,
  `azure-us` already beats `local-llama` on estimated cost. The measured latencies
  (about 600 ms vs about 900 ms) push the same way, so a warm latency term can only
  widen a lead that already exists.
- No sweep call failed, so every endpoint's measured availability is the same (≈ 1.0),
  and the availability term cannot separate candidates.
- The P2a signals are latency and availability only. **Quality is never measured into
  the store**, so a warm router cannot learn that `azure-us` fails FAQ 7% of the time.

A suite that separates the two arms needs endpoints whose measured latency or
availability disagrees with their price and quality ordering inside one tier.
`test_strategies.py`'s fast-flaky/slow-available pair is the minimal example. The
starter suite does not contain one.

### 5.5 First B45 result: null, and predicted by the formula

Backlog B45 asks whether `weights.availability: 0.2` is large enough for a dead endpoint
to lose. `b45.yaml` holds quality/cost/latency at the shipped 1.0/0.6/0.3 and varies only
availability: 0.2 (`default`), 0.0, 1.0 and 2.0.

**Result (seed 0, suite hash `3339c773…d8199`):** all eight variant strategies
(`router_cold:{default,availability_0,availability_1,availability_2}` and the four
`router_warm:` forms) have pass rate **0.909091**, cost regret **3.2e-05**, floor-miss
rate **0.090909**, latency regret **30.748 ms** and mean chosen cost **0.00149045**. That
is identical to each other and to the unvaried `router_cold` / `router_warm`.

**Why this is null by construction.** Availability enters the score as
`+ w.availability · availability`. A weight can change a ranking only when availability
**differs between candidates**. In the cold arm every candidate reads the neutral prior.
In the warm arm every candidate measured ≈ 1.0, because the simulated backend has no
failure mode and no call failed. The term is a constant across candidates in both arms,
so its magnitude cannot matter. **The run does not answer B45 in either direction.** It
shows neither that 0.2 is too small (no dead endpoint was present to keep winning) nor
that a larger weight overrides healthy cost and quality differences (no availability
difference was present to override them). It does show the instrument is wired
end-to-end: the variants are parsed, deep-copied into their own `Settings`, run through
the real `Router.select`, and reported per strategy. The unit test in §4 shows the same
path can flip a choice when availability differs.

**What would answer it.** A failure-injected sweep. Either (a) a simulated
`error_rate` per endpoint, which is not built (the simulated backend never raises), or
(b) a `--live` run with a deliberately failing endpoint in the file. Either way the result
is reported as the regret and floor-miss curve over the availability weight. B45 stays
open.

### 5.6 Seed sensitivity (seeds 0–9, no variants)

Seeds 1–9 were run at `82bc968`. The only change between `82bc968` and `e8e8682` is the header
comment of `healthcare-sim.yaml`. Seed 0's `metrics.json` and `matrix.jsonl` are
byte-identical at both commits.

| strategy | pass rate mean | min–max | expected from the declared quality |
|---|---|---|---|
| `cheapest` | 0.936 | 0.773–1.000 | 0.920 |
| `best_published_quality` | 0.955 | 0.818–1.000 | 0.931 |
| `router_cold` = `router_warm` | 0.900 | 0.773–1.000 | 0.909 |

The "expected" column needs no model calls. It is each strategy's fixed choice per case,
weighted by that endpoint's declared `simulated.quality`. For the router:
`(8·0.93 + 8·0.89 + 2·0.92 + 4·0.90)/22 = 0.9091`. The observed means sit within sampling
noise of it (220 Bernoulli draws per strategy). **So in simulated mode the pass rate
recovers the input. It measures nothing** (§6.1).

The headline was **comparable at 4 of 10 seeds** (3, 5, 6, 8), with a saving of
35.7–36.7%, and not comparable at the other 6. Parity there is a coincidence of draws.
The expected pass rates differ by 0.022, just outside the 0.02 tolerance. The ~36% figure
is almost entirely the `us_only` claims, where the router picks `azure-us` and the
reference picks `local-llama`, partly offset on FAQ where the router pays more than the
reference. **No single-seed headline from this suite should be quoted.**

`cheapest`'s cost regret is 0 at every seed. Cost regret is conditioned on passing, and
`cheapest` is by definition the cheapest when it passes. Its failures show up only in the
floor-miss rate (0.15 at seed 3, 3 of 20 feasible cases). This is the conditioning rule in
§3 working as designed. Some case was infeasible-at-floor (every eligible endpoint drew
wrong) at 4 of 10 seeds (1, 3, 6, 7), up to 2 cases. Those cases lower every strategy's
pass rate equally and are excluded from the floor-miss denominator.

### 5.7 Empty-set run

```
suite eval/suites/empty-set.jsonl   hash acb862336e156a5a5cf2e5f59c471c4ad1140cd02d4ff4ec59568e7d6ec157af
endpoints eval/endpoints/cloud-only-sim.yaml   hash 0c00a3c02b6b676e8cc1cedd69617cc9c5dd3e04d600de4a04b441f1e0d3e806
seed 0, git e8e8682, registry_version 2f228020d13d, exit 0
```

All four `onprem_only` cases resolve to `E(c) = ∅`, with `cloud-a` and `cloud-b` each
rejected `residency`, `rejected_ok: yes` and 0 calls. Every strategy returns `None` with
violation rate 0. Every regret aggregate is `n/a`, because there are no non-empty cases.
This is the "no on-prem model onboarded ⇒ derived reject" guarantee, observed through the
real `feasible_set`. The two suites are kept in separate files so that the main run's exit
code keeps its meaning.

---

## 6. What is NOT demonstrated

### 6.1 Simulated quality is an input, not a measurement

Every pass/fail in §5 is a seeded Bernoulli draw against a probability written by hand in
`healthcare-sim.yaml`. §5.6 shows the pass rate recovering that input. **No statement of
the form "the router achieves X% quality" follows from any simulated run.** The simulated
numbers can support only two kinds of claim. (a) Structural: which endpoints are
eligible, which strategy chose what, and that no choice left `E(c)`. (b) Conditional: "if
endpoint quality were as declared, strategy s would have regret r". Cost and latency are
simulated too: prices from the file over real token counts, and latency drawn from a
declared mean ± jitter.

### 6.2 Deterministic graders only

`exact`, `contains`, `regex`, `json_schema` and `numeric` each return 0 or 1. There is no
LLM judge, no partial credit, and no free-text summary quality. The regex and schema cases
check a required *format*, not the content of a summary. With binary quality and
floor 1.0, quality regret equals the floor-miss rate whenever no case is infeasible-at-floor.
The two differ only in denominator: all non-empty cases vs cases with `P ≠ ∅`.

### 6.3 Synthetic, small, single-turn cases

22 + 4 hand-written cases, 3 workloads, all short (one tier ever targeted), single-turn,
and English. They contain no tool calls and no streaming (the sweep does not stream),
and compression is effectively inert at these sizes. The PII is synthetic and reserved.
Group sizes are 2–10 cases, so the worst-group numbers are illustrative only.

### 6.4 `router_warm` is optimistic by construction

The warm store is built immediately before scoring each case. It replays **every row swept
so far, up to and including the case being scored**. So the router has seen this case's
own outcomes on every eligible endpoint. It is never deployable, and it is an upper bound
on what the current signal set could learn, not an estimate. Two further artefacts:

- **Frozen read clock.** `Router.select` has no `now` parameter. The harness's store reads
  against a clock frozen at build time (`e7f7bee`), which determinism requires. A real
  deployment's store ages between reads.
- **Synthetic 1 s spacing.** Replayed rows are placed 1 s apart and end at the frozen
  "now". So the first row of a long sweep is `len(rows) − 1` synthetic seconds old, and
  availability decay (half-life 900 s) depends on **sweep size and sweep position**, not
  on elapsed time. With the starter suite's 54 rows this is immaterial. In a sweep of thousands of rows an
  early error would partially recover purely because of where it sits.

A non-optimistic warm arm would replay a **disjoint** prior sweep (train/test split by
case). That is not built.

### 6.5 No calibration, no churn, no noninterference rate

The router emits no confidence, so calibration is undefined. Policy or endpoint churn
(endpoints joining, leaving or degrading mid-run) is not modelled: the registry is static
for a run. Noninterference is P1a's harness. All three are named as deferred in every
`report.md`.

### 6.6 No live result

No `--live` run has been made. `spark-live.example.yaml` is loaded by a test with
`live=True` and is never run. Every claim about real endpoints waits for one.

### 6.7 The router comparison is mostly the tier rule

See §5.3. With every case targeting `cheap`, the router's regret against the oracle is
dominated by the length-based tier filter excluding cheaper `mid` endpoints. The
four-term scorer only ever decided between `azure-us` and `local-llama`, and cost
settled that.

---

## 7. Reproducing it

```bash
git checkout feat/feasible-route-bench        # code at e8e8682
uv sync --group dev

# the harness suite: 153 test functions, 155 collected, ~14 s
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run pytest tests/eval -o addopts="" -q

# the §5 run (writes results/; results/ is gitignored)
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run python scripts/feasible_bench.py \
    --suite eval/suites/starter.jsonl \
    --endpoints eval/endpoints/healthcare-sim.yaml \
    --variants eval/variants/b45.yaml --seed 0 --out results/fb-starter
md5sum results/fb-starter/metrics.json       # 6ab2ee35fdfdbf8044a940114caa1363 at e8e8682

# the §5.7 run
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run python scripts/feasible_bench.py \
    --suite eval/suites/empty-set.jsonl \
    --endpoints eval/endpoints/cloud-only-sim.yaml --seed 0 --out results/fb-empty

# §5.6: repeat the first run with --seed 1 … 9 (drop --variants)
```

`SP_PII_DETECTION=regex` is needed only when Presidio is not installed. Without it the
CLI exits 1 with a message saying so. `LITELLM_LOCAL_MODEL_COST_MAP=True` stops LiteLLM
fetching its cost map over the network at import. The `md5sum` holds only for the same
suite, endpoint file, variants, seed and code. Any change to them changes the stamp, and
that is the point of the stamp.

| File | Contents |
|---|---|
| `app/eval/cases.py`, `graders.py` | suite loader, load-time validation, the five graders |
| `app/eval/endpoints.py` | endpoint loader, `harness_settings`, scratch `build_registry` |
| `app/eval/simulated.py` | the seeded simulated backend |
| `app/eval/oracle.py` | pure oracle and metrics (imports only `dataclasses`) |
| `app/eval/strategies.py` | the strategies, `router_variant`, `warm_signals` |
| `app/eval/runner.py` | `run_bench`: gate, sweep, strategies, outputs, exit code |
| `scripts/feasible_bench.py` | the CLI |
| `eval/` | the starter suite, empty-set suite, endpoint profiles and B45 variants |

---

## 8. Provenance

| | |
|---|---|
| Design (binding) | `docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md` |
| Plan | `docs/superpowers/plans/2026-09-21-feasible-route-bench.md` |
| Per-task briefs, reports, rulings | `.superpowers/sdd/2026-09-21-feasible-route-bench/` |
| Operator guide | [`docs/guides/feasible-route-bench.md`](../guides/feasible-route-bench.md) |
| The question B45 asks | [`docs/backlog/bugs.md`](../backlog/bugs.md) B45; [`routing-signal-provenance.md`](routing-signal-provenance.md) §2.5 |
| Source of the §14 example | `docs/new/RouteForge-Priorities-1-3-LLM-Handoff.md` §12–21 (not tracked in this branch) |

Commits, oldest first: `c213763` (spec) · `bfeb8ab` (plan) · `f8e41a5` (graders and
case-suite loader) · `55e3990` (endpoint files, harness settings, scratch registry) ·
`8b3c403` (deterministic simulated backend) · `db25203` (feasible oracle and regret
metrics) · `aa50dc2` (spec: floor-miss denominator, worst-group metrics and tags, exit
rule) · `820bec2` (oracle review fixes: parity rounding, `-0.0`, oracle over `E(c)`,
missing-choice violation) · `92a9468` (strategies) · `e7f7bee` (frozen warm read clock,
variant weight validation) · `87019e9` (weight-key check moved out of the serving schema)
· `fb29403` (runner and CLI; YAML pool emptied) · `79be0a4` (starter suite, healthcare
simulation, B45 variants) · `82bc968` (the production-default escalation cases alongside
the override) · `e8e8682` (endpoint-file header: drop an untracked docs path that failed
the docs link gate; comment only).

### Related work — TO VERIFY BEFORE CITING

⚠️ Nothing below has been verified. It is a list of threads, not citations.

1. **Router benchmarks** (RouterBench, RouterEval, RouteLLM's evaluation). *Establish:*
   that they score against a fixed candidate set, and that none makes the candidate set a
   per-request variable, so none can state a feasible oracle.
2. **Off-policy and counterfactual evaluation** (logged-bandit estimators, inverse
   propensity scoring). *Establish:* why the full sweep is used here instead. The sweep
   removes the need for propensities offline, at the cost of `|E(c)|` calls per case.
   P2b-2's production records will need these methods.
3. **Constrained and safe bandits.** *Establish:* the framing for P2b-3, exploration
   restricted to `E(c)`, for which this harness is the measurement.
