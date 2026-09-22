# FeasibleRouteBench: evaluating an eligibility-constrained LLM router against the feasible oracle

**Status:** research notes, written as the code landed. 2026-09-21. **Extended 2026-09-22**
with B48 (failure injection, failover-aware scoring, the online router arm) and the first
non-null B45 answer: §9. Branch `feat/b48-failure-injection`, code at `8344b21`.
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
result is null, and that null is predicted by the scoring formula (§5.5; superseded by §9). The router
comparison mostly measures a tier-targeting rule, not the scorer (§5.3).

**Update 2026-09-22 (§9).** With failure injection built (B48), a designed outage scenario
gives B45 its first real answer, still simulated: at the shipped `availability: 0.2`, the
online router keeps sending first attempts to a dead endpoint that is 4× cheaper than its
peer, exactly as if the weight were 0, at every seed and time scale tried. The formula
predicts this too (§9.5). The starter-suite numbers below are unchanged at `8344b21`; only
arm names and output stamps moved (§5.1).

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
   `--keep-text`. **The sweep disables confidence escalation** (`harness_settings` sets
   `routing.max_escalations = 0`; no strategy reads it): with a pin, escalation clamps
   back to the pinned tier and calls the *same* endpoint again, which would double a row's
   spend, report the last hop's latency and grade the second answer. The runner enforces
   one backend `generate` call per successful sweep call independently; any other count
   is an error row (`GenerateCountMismatch`, counted as `sweep_generate_mismatches`).
   Found by the final review's probe: with simulated confidence forced to 0.1, the
   starter run (seed 0, b45 variants) makes 54 matrix rows and **108** generate calls at
   `e8e8682`, and 54 rows and 54 calls at `0a92a73`, exit 0 at both.
   Pinned by `test_runner.py::test_low_confidence_does_not_reissue_pinned_sweep_calls`.
4a. **Empty case.** When `E(c) = ∅` nothing is swept. Instead the harness sends **one
   unpinned probe**, `ChatRequest(model="auto")`, through the real `Pipeline.run`. The case
   is `rejected_ok` only if the probe **raised**, the case made **0** backend calls, and
   there is at least one exclusion reason. A probe that is served is appended to the
   sweep's served list, which makes it a violation (exit 2).
5. **Strategies.** Each strategy maps `(case, E(c)) -> slug | None`. Its outcome is looked
   up in the matrix. Strategies make no extra model calls. *Since `8344b21` (B48) a strategy
   yields an **attempt order** instead and is scored failover-aware (§9.2); with no failures
   the order has one entry and every figure is the same.*

| Strategy name (as it appears in outputs) | Rule |
|---|---|
| `cheapest` | lowest **estimated** cost (the router's own `price_call` estimate), ties by slug |
| `best_published_quality` | highest registry quality through the router's `QualityResolver` (tier default when unset), ties by estimated cost then slug |
| `fixed:<slug>` | that slug when eligible, else `cheapest` (`fallback_used` in `choices.jsonl`) |
| `router_cold` | real `Router.select` over a **fresh, empty** `SignalStore` |
| `router_warm` | real `Router.select` over a `SignalStore` that has **replayed the sweep's own rows** (§6.4). **Removed at `8344b21`**: it leaks the scored case's outcome (§9.3) |
| `router_online` | *(since `8344b21`)* real `Router.select` over a store holding only the attempts this arm itself made on earlier cases, the reported arm (§9.3) |
| `router_warm_prior` | *(since `8344b21`)* real `Router.select` over a store that replayed every matrix row of **earlier** cases only, a full-information reference (§9.3) |
| `router_cold:<variant>` / `router_online:<variant>` / `router_warm_prior:<variant>` | the same, with a weights mapping from `--variants` (the B45 instrument). Before `8344b21`: `router_cold:<variant>` / `router_warm:<variant>` |

(The spec calls the variant strategies `router_weights:<name>`. The shipped names follow
the plan's brief and are the ones to cite.)

**Simulated backend** (`app/eval/simulated.py`, `8b3c403`). For `(case, endpoint)` it
draws `u = Random(f"{seed}:{case.id}:{slug}")`. It returns the case's passing answer if
`u < quality[workload or default]`, otherwise the literal `SIMULATED-WRONG`. Latency is
`mean + jitter·(2u₂−1)` from a second seeded draw, floored at 1 ms, with no sleep. Cost is
`price_call` over the pipeline's token counts. Confidence is fixed at 1.0, and the
harness also turns escalation off (step 4), so gateway confidence escalation never fires. It had **no failure-injection mode** until `290b555`.
This matters for B45 (§5.5). Since `290b555` an endpoint profile can declare outages and a
flake rate (§9.1); the starter and empty-set endpoint files declare neither, so their
endpoints still never fail.

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

**Headline (handoff §20).** The headline is `router_warm` (since `8344b21`: `router_online`) mean chosen cost vs
`best_published_quality` mean chosen cost, as a saving percentage. It is printed only
when `round(|pass_rate_router − pass_rate_ref|, 12) ≤ parity_tol` (default 0.02) and the
reference cost is defined and `> 0`. Otherwise the report states both pass rates and
makes no claim. The 12-decimal rounding exists because `|0.90 − 0.92|` evaluates to
`0.020000000000000018` in IEEE-754 (`820bec2`).

**Exit codes.** `0` means ok. `1` means a load or usage error, including a gate exception
on any case (treated as a configuration error; `matrix.jsonl` is left partial and no
`metrics.json` is written). `2` means a policy violation or an integrity failure: any
strategy `violation_rate > 0`, any ineligible sweep-served (or empty-probe-served) model,
any post-gate eligible set that is not a subset of the pre-gate set, any empty-set
mismatch, or any `pinned_mismatches` (a pinned call served by another endpoint, which is
impossible by construction and so an integrity bug). The rule is the pure
`runner._exit_code`, table-tested per trigger.

**Deferred, and named in every report:** calibration (the router emits no confidence),
policy/endpoint churn robustness, and the noninterference failure rate (owned by P1a's
harness).

---

## 4. Invariants, and the test that pins each

All tests live under `tests/eval/`: 164 test functions, 166 collected cases, about 20 s
(at `0a92a73`; at `8344b21`, 265 functions and 270 collected cases, about 29 s). The
invariants B48 added are in §9.7.

| Invariant | Pinned by | Falsified? |
|---|---|---|
| **Violation guard.** An ineligible choice makes metric 1 > 0 and the run exits 2 | `test_runner.py::test_injected_ineligible_choice_exits_2` monkeypatches `cheapest` to return a slug outside `E(c)`. The run exits 2 with `violation_rate > 0` for that strategy and 0 for the others. `test_cloud_choice_on_onprem_case_exits_2` is the realistic form: always choosing cloud violates on the `onprem_only` case. | **Yes.** Both tests inject the fault the guard exists to catch. `test_oracle.py::TestSpec14Fixture` also checks the sweep-call half of `violations()` with a served-european row. |
| **A missing choice is a violation, not a silent pass** | `test_oracle.py::TestNoneChoiceOnNonEmptyCase::test_missing_choice_record_entirely_is_a_violation` | Yes (the assertion was flipped in `820bec2` when the rule changed) |
| **The oracle ignores non-eligible rows** | `test_oracle.py::TestOracleIgnoresNonEligibleRows` (a contaminating row must not turn a real floor miss into a pass, but `violations()` still flags it) | Yes |
| **Floor misses never produce negative cost regret** | `test_oracle.py::TestFloorMiss` | Yes. A cheap failer chosen gives `floor_miss_rate = 1.0` and `cost_regret = None`, not a negative number |
| **No spurious `-0.0` regret** | `test_oracle.py::TestNoNegativeZeroRegret` (quality = cost = latency = 2/3 checks the sign bit and the JSON text) | Yes. The first implementation produced `-0.0` (`db25203`, fixed `820bec2`) |
| **Determinism.** Same seed gives a byte-identical `metrics.json` | `test_runner.py::test_same_seed_gives_byte_identical_metrics` (also `matrix.jsonl`). **Across processes** as well: the starter run at seed 0 with variants, under `PYTHONHASHSEED` = 1, 2 and 3, gives md5 `85cae106fa1ff5723c910d83a722b562` each time at `0a92a73`, identical to an unset-hash run (it was `6ab2ee35fdfdbf8044a940114caa1363` at `82bc968` and `e8e8682`, before two keys were added to `metrics.json`; that md5 still reproduces from `e8e8682`'s tree) | Partially. `test_simulated.py::TestDeterminism::test_different_seed_changes_at_least_one_outcome_over_50_draws` shows the seed is not ignored |
| **The warm store reads against a frozen clock** | `test_strategies.py::test_frozen_read_clock_is_stable_across_real_elapsed_time`, `…_when_the_live_clock_advances` | Yes. An earlier draft read the live clock, which broke determinism (`e7f7bee`) |
| **Model-facing view.** The pinned call sees what production would show | `test_runner.py::test_model_facing_view_redacted_for_enforced_raw_for_passthrough`. The simulated backend never receives `078-05-1120` for an `enforced` case, and does receive it for a `passthrough` case | Yes. The passthrough arm shows the check would see the SSN if it arrived |
| **Pinned sweep serves the pinned endpoint** | `test_runner.py::test_every_row_served_its_pinned_endpoint`; a mismatch exits 2 (`test_exit_code_each_trigger_alone_is_2`) | n/a (all rows must satisfy it) |
| **One backend call per sweep call** | `test_runner.py::test_low_confidence_does_not_reissue_pinned_sweep_calls` (confidence forced to 0.1: generate calls = matrix rows, exit 0); `test_sweep_call_with_extra_generate_becomes_error_row` (escalation re-enabled behind the runner's back: every double call is a `GenerateCountMismatch` error row, and counted); `test_endpoints.py::test_confidence_escalation_is_off_and_base_untouched` | **Yes.** Before the fix the first test read 22 generate calls for 12 rows |
| **Endpoint-file prices win** | `test_endpoints.py::test_slug_colliding_with_pricing_yaml_is_priced_from_the_file` (`azure-gpt-mid` at input 99 is priced 99, not pricing.yaml's 0.15); `test_baseline_model_slug_still_resolves` | Yes (fails on the unfixed `harness_settings`) |
| **`onprem_only` is absolute in the sweep** | `test_runner.py::test_onprem_only_case_never_sweeps_cloud`; `test_endpoints.py` (`azure` spec excluded under `residency="onprem_only"`) | n/a |
| **Empty set.** An expected-empty case is refused by the real pipeline with reasons and no model call; a mismatch exits 2 | `test_runner.py::test_empty_set_expected_is_rejected_without_calls`, `test_empty_set_unexpected_exits_2`, `test_empty_set_seen_by_runner_but_served_by_pipeline_exits_2` (only the runner's `feasible_set` is blinded, the pipeline serves the probe: `rejected_ok` false, violation, exit 2) | **Yes.** Flipping `expect_empty` gives exit 2; before the probe existed, the blinded test read `rejected_ok: true` |
| **Serving isolation.** Nothing under `app/` except `app/eval/` imports `app.eval` | `test_isolation.py::test_no_serving_module_imports_app_eval` | Yes. `test_scanner_detects_an_import` feeds the scanner a module that does import it |
| **Live safety.** No endpoint the operator did not list, and no call without consent | `test_endpoints.py::TestHarnessSettings::test_static_yaml_pool_is_excluded`; `test_runner.py::test_cli_live_endpoint_without_live_flag_exits_1`, `test_cli_live_declined_at_prompt_makes_no_calls` (target `127.0.0.1:9`, never contacted) | Yes (the pool leak in §1.1 was the RED) |
| **Loader rejections** (each §4 load error, including untagged PII-shaped content and a key-looking `api_key_env`) | `test_cases.py::TestLoadSuiteErrors`, `test_endpoints.py` load-error tests | per error |
| **Variants reach the real router** | `test_strategies.py::test_availability_weight_flips_choice_between_slow_available_and_fast_flaky`. With two same-tier candidates, same price and same quality, availability weight 0 picks `fast-flaky` and weight 5 flips the choice to `slow-available` | Yes. This is the proof that the B45 instrument *can* move a ranking. §5.5 explains why it does not on the starter suite |

**Formerly a weak spot, now evidence.** Until the final review, the "no model call" half
of an empty case's `rejected_ok` was read from a per-case call counter that could only
read 0 on an empty case (nothing set the case when nothing was swept), so it added no
evidence. The empty-case probe (§2 step 4a) replaces that with an observation: the real
pipeline is handed the case unpinned and must **refuse** it, with the counter now armed
for that case. In the shipped empty-set run all four `onprem_only` cases raise
`NoEligibleCandidate` with 0 calls (§5.7). That is the real serving path failing closed
for `onprem_only` with no on-prem model onboarded (CLAUDE.md rules 3 and 5), observed end
to end rather than inferred from the harness's own `feasible_set` call.

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
git_sha          0a92a73d48ecf676c65d85ed82e612433464f742
base_routing_policy_version  a4bf5708d2db      registry_version  86294e62adea
mode             simulated                exit 0; every policy counter 0
```

`base_routing_policy_version` (called `routing_policy_version` in `run.json` before
`0a92a73`) hashes the **stock** routing and pricing config the harness loaded, before the
harness empties `model_pool`, applies endpoint-file prices and `max_escalations = 0`, and
without any variant weights. `run.json`'s `notes` says so. It
identifies the deployment configuration, not the endpoint universe. The endpoint universe
is `endpoints_hash` + `registry_version`.

The §5 numbers were first produced at `e8e8682` and re-run at `0a92a73`, after the final
review's fixes. `matrix.jsonl` is byte-identical at both commits (md5
`dda387af76f7cf2adac5333bf989e546`), and `metrics.json` differs only by two added keys
(`policy.sweep_generate_mismatches`, per-case `empty_probe_error`), so every figure below
holds at both. The simulated backend reports confidence 1.0, so escalation never fired
before the fix either.

**Re-cited at `8344b21` (B48, 2026-09-22).** The same run (same suite, endpoint file,
variants and seed; `run.json` now also records `seconds_per_case` 30, the default, and
`attempt_cap` 3) exits 0 with every policy counter 0. B48 changed the arm set
(`router_warm` removed; `router_online` and `router_warm_prior` added, each with the four
variants) and the output schema, so the stamps moved: `metrics.json` md5
`1df6bac3bfc27380abfc29da76d8ae86` (was `85cae106…`), `matrix.jsonl` md5
`75ef848e632fd3f01613022042e9f651` (was `dda387af…`). The matrix change is only the three
keys B48 added to every row (`counts_unhealth`, `failure_latency_ms`, `error_class`, all
`null` here): with those three keys removed and each row re-serialised the runner's way
(`json.dumps(row, sort_keys=True)`), the file's md5 is `dda387af76f7cf2adac5333bf989e546`
again. **Every figure in §5.2–§5.6 was re-read at `8344b21` and is unchanged**; where a
figure was attributed to `router_warm`, it now holds for `router_online` and
`router_warm_prior` (identical values, because no call fails, §5.4).

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
| `azure-us` | 0.001470 | 607.3 | 8/8 | `cheapest`, `router_cold`, `router_warm` (all variants); at `8344b21` `router_online` and `router_warm_prior` (all variants) |
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
| `router_warm` (to `0a92a73`) | 0.909 | 0.0909 | 0.000032 | 30.7 | 0.0909 | 0.001490 |
| `router_online` (`8344b21`) | 0.909 | 0.0909 | 0.000032 | 30.7 | 0.0909 | 0.001490 |
| `router_warm_prior` (`8344b21`) | 0.909 | 0.0909 | 0.000032 | 30.7 | 0.0909 | 0.001490 |

Violation rate is 0 for all 12 strategies (17 at `8344b21`, where `wasted_attempts` and
`exhausted_rate` are 0 for every one). No case is infeasible-at-floor at seed 0.
**Headline: not comparable.** `router_warm` (at `8344b21`, `router_online`) 0.909 vs
`best_published_quality` 1.000 exceeds the 0.02 tolerance, so no saving is claimed.

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
seeds 0–9.** This is expected here, not evidence that warm state is useless. At `8344b21`
the same holds for the new arms: `router_cold`, `router_online` and `router_warm_prior`
produced identical attempt chains (one attempt each) on every case at every one of seeds
0–9, and no matrix row at any seed is an error row.

- Inside the target tier, the contested cases are FAQ and `us_only` claims. There,
  `azure-us` already beats `local-llama` on estimated cost. The measured latencies
  (about 600 ms vs about 900 ms) push the same way, so a warm latency term can only
  widen a lead that already exists.
- No sweep call failed, so every endpoint's measured availability is the same, **exactly
  1.0** (the store replays successes only, from its start state, and decay pulls toward
  1.0; read back at `0a92a73`: `value=1.0, source=measured` for all four endpoints), and
  the availability term cannot separate candidates.
- The P2a signals are latency and availability only. **Quality is never measured into
  the store**, so a warm router cannot learn that `azure-us` fails FAQ 7% of the time.

A suite that separates the two arms needs endpoints whose measured latency or
availability disagrees with their price and quality ordering inside one tier.
`test_strategies.py`'s fast-flaky/slow-available pair is the minimal example. The
starter suite does not contain one. (The §9 outage scenario does, by design.)

### 5.5 First B45 result: null, and predicted by the formula

Backlog B45 asks whether `weights.availability: 0.2` is large enough for a dead endpoint
to lose. `b45.yaml` holds quality/cost/latency at the shipped 1.0/0.6/0.3 and varies only
availability: 0.2 (`default`), 0.0, 1.0 and 2.0.

**Result (seed 0, suite hash `3339c773…d8199`):** all eight variant strategies
(`router_cold:{default,availability_0,availability_1,availability_2}` and the four
`router_warm:` forms; at `8344b21` all twelve, with the `router_online:` and
`router_warm_prior:` forms) have pass rate **0.909091**, cost regret **3.2e-05**, floor-miss
rate **0.090909**, latency regret **30.748 ms** and mean chosen cost **0.00149045**. That
is identical to each other and to the unvaried `router_cold` / `router_warm`.

**Why this is null by construction.** Availability enters the score as
`+ w.availability · availability`. A weight can change a ranking only when availability
**differs between candidates**. In the cold arm every candidate reads the neutral prior.
In the warm arm every candidate measured **exactly 1.0**, because the simulated backend
has no failure mode and no call failed (successes only from the store's start state, and
decay pulls toward 1.0, so there is nothing for it to decay from). The term is a constant across candidates in both arms,
so its magnitude cannot matter. **The run does not answer B45 in either direction.** It
shows neither that 0.2 is too small (no dead endpoint was present to keep winning) nor
that a larger weight overrides healthy cost and quality differences (no availability
difference was present to override them). It does show the instrument is wired
end-to-end: the variants are parsed, deep-copied into their own `Settings`, run through
the real `Router.select`, and reported per strategy. The unit test in §4 shows the same
path can flip a choice when availability differs.

**What would answer it.** A failure-injected sweep. *(Built by B48; the answer is §9. The
paragraph below is kept as written.)* Either (a) a simulated
`error_rate` per endpoint, which is not built (the simulated backend never raises), or
(b) a `--live` run with a deliberately failing endpoint in the file. Either way the result
is reported as the regret and floor-miss curve over the availability weight. B45 stays
open. What such an injection must get right before its result can be trusted (a
prior-cases-only warm arm, error-row cost, production-aligned failure types, and the
single-shot vs failover gap) is backlog **B48**.

### 5.6 Seed sensitivity (seeds 0–9, no variants)

Seeds 1–9 were run at `82bc968`. The only change between `82bc968` and `e8e8682` is the header
comment of `healthcare-sim.yaml`. Seed 0's `metrics.json` and `matrix.jsonl` are
byte-identical at both commits. Seeds 1–9 were re-run at `0a92a73`: each `metrics.json`
equals the `e8e8682` one once the two added keys are removed, and every run exits 0.

| strategy | pass rate mean | min–max | expected from the declared quality |
|---|---|---|---|
| `cheapest` | 0.936 | 0.773–1.000 | 0.920 |
| `best_published_quality` | 0.955 | 0.818–1.000 | 0.931 |
| `router_cold` = `router_warm` (at `8344b21`: = `router_online` = `router_warm_prior`) | 0.900 | 0.773–1.000 | 0.909 |

The "expected" column needs no model calls. It is each strategy's fixed choice per case,
weighted by that endpoint's declared `simulated.quality`. For the router:
`(8·0.93 + 8·0.89 + 2·0.92 + 4·0.90)/22 = 0.9091`. The observed means sit within sampling
noise of it (220 Bernoulli draws per strategy). **So in simulated mode the pass rate
recovers the input. It measures nothing** (§6.1).

The headline was **comparable at 4 of 10 seeds** (3, 5, 6, 8), with a saving of
35.7–36.7%, and not comparable at the other 6 (re-run at `8344b21` with `router_online` as
the headline router: the same four seeds, 35.73–36.70%, and every run exits 0). Parity there is a coincidence of draws.
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
seed 0, git 0a92a73, registry_version 2f228020d13d, exit 0
metrics.json md5 288b58287dd864105ef37155741c1e89
at 8344b21: exit 0, same hashes and registry_version, metrics.json md5 ad91885241c8776e4a1daafc417f86c0
```

(The `8344b21` md5 differs because the strategy set changed and every strategy block gained
`wasted_attempts` and `exhausted_rate`. The four probes still raise `NoEligibleCandidate`
with 0 calls.)

All four `onprem_only` cases resolve to `E(c) = ∅`, with `cloud-a` and `cloud-b` each
rejected `residency`. Each case's unpinned probe through the real pipeline raised
`NoEligibleCandidate` (`empty_probe_error`) with 0 calls, so `rejected_ok: yes`. Every strategy returns `None` with
violation rate 0. Every regret aggregate is `n/a`, because there are no non-empty cases.
This is the "no on-prem model onboarded ⇒ derived reject" guarantee, observed through the
real `feasible_set` **and** the real `Pipeline.run` failing closed. The two suites are kept in separate files so that the main run's exit
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

*Historical since `8344b21`: `router_warm` was removed for exactly this reason, and the
1 s spacing below was replaced by the case clock. See §9.3–§9.4. Kept as the rationale.*

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
git checkout feat/feasible-route-bench        # code at 0a92a73
uv sync --group dev

# the harness suite: 164 test functions, 166 collected, ~20 s
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run pytest tests/eval -o addopts="" -q

# the §5 run (writes results/; results/ is gitignored)
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run python scripts/feasible_bench.py \
    --suite eval/suites/starter.jsonl \
    --endpoints eval/endpoints/healthcare-sim.yaml \
    --variants eval/variants/b45.yaml --seed 0 --out results/fb-starter
md5sum results/fb-starter/metrics.json       # 85cae106fa1ff5723c910d83a722b562 at 0a92a73
                                             # 1df6bac3bfc27380abfc29da76d8ae86 at 8344b21 (§5.1)

# the §5.7 run
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run python scripts/feasible_bench.py \
    --suite eval/suites/empty-set.jsonl \
    --endpoints eval/endpoints/cloud-only-sim.yaml --seed 0 --out results/fb-empty
md5sum results/fb-empty/metrics.json         # 288b58287dd864105ef37155741c1e89 at 0a92a73
                                             # ad91885241c8776e4a1daafc417f86c0 at 8344b21

# the §9 outage runs: see §9.8

# §5.6: repeat the first run with --seed 1 … 9 (drop --variants)
```

`SP_PII_DETECTION=regex` is needed only when Presidio is not installed. Without it the
CLI exits 1 with a message saying so. `LITELLM_LOCAL_MODEL_COST_MAP=True` stops LiteLLM
fetching its cost map over the network at import; the CLI sets it itself when unset, so
it matters only for the `pytest` line. The `md5sum` holds only for the same
suite, endpoint file, variants, seed and code. Any change to them changes the stamp, and
that is the point of the stamp.

| File | Contents |
|---|---|
| `app/eval/cases.py`, `graders.py` | suite loader, load-time validation, the five graders |
| `app/eval/endpoints.py` | endpoint loader, `harness_settings`, scratch `build_registry` |
| `app/eval/simulated.py` | the seeded simulated backend |
| `app/eval/oracle.py` | pure oracle and metrics (imports only `dataclasses`) |
| `app/eval/strategies.py` | the strategies, `router_variant`, `warm_signals`; since `8344b21` the attempt orders, `OnlineArm`, `warm_prior_signals` and the case clock |
| `app/eval/runner.py` | `run_bench`: gate, sweep, strategies, outputs, exit code |
| `scripts/feasible_bench.py` | the CLI |
| `eval/` | the starter suite, empty-set suite, endpoint profiles and B45 variants; since `8344b21` the outage suite and `outage-sim.yaml` (§9) |

---

## 8. Provenance

| | |
|---|---|
| Design (binding) | `docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md` |
| Plan | `docs/superpowers/plans/2026-09-21-feasible-route-bench.md` |
| Per-task briefs, reports, rulings | `.superpowers/sdd/2026-09-21-feasible-route-bench/` (local working files, not in the repository) |
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
the docs link gate; comment only) · `8dbfc96` (these notes and the guide) · final-review
fixes: `847bce8` (one generate call per pinned sweep call) · `d60bb30` (pinned-served
mismatch exits 2; `_exit_code`) · `f42e73d` (empty-case probe through the real pipeline)
· `e3ef0f5` (file-priced colliding slugs, scratch-DB dispose on error, list-only set
fields) · `0a92a73` (`base_routing_policy_version`, air-gap-safe CLI, tracked docstring
path).

B48 (§9), oldest first: `73b7af6` (spec) · `4df40c3` (plan) · `290b555` (simulated outages
and flaky failures) · `f446c91` (matrix rows record endpoint unhealth; warm signals count
only unhealth failures) · `b32abb1` (the sweep gateway makes one attempt;
`deployment_failover_cap`) · `3a3364e` (classify only the gateway's own exhaustion
exception, one hop) · `a543abb` (failover-aware strategy scoring) · `1a6712b` (attempt
orders, online arm, prior-only warm arm) · `153a0d4` (spec §6 amended: the case clock and
`seconds_per_case`) · `8344b21` (runner wiring, failover scoring of every arm,
`router_warm` removed, outage scenario). Design:
`docs/superpowers/specs/2026-09-22-feasible-bench-failure-injection-design.md`; plan:
`docs/superpowers/plans/2026-09-22-feasible-bench-failure-injection.md`; per-task reports
and review rulings: `.superpowers/sdd/2026-09-22-feasible-bench-failure-injection/` (local
working files, not in the repository). The score probe that §9.5 quotes is committed:
`scripts/b45_score_probe.py`.

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

---

## 9. B48: failure injection, failover-aware scoring, and the first B45 answer (2026-09-22)

**Design (binding):** `docs/superpowers/specs/2026-09-22-feasible-bench-failure-injection-design.md`,
§6 amended in `153a0d4`. **Code:** `290b555` … `8344b21` (commits in §8). **Backlog:**
B48 (resolved by this work) and B45 (answered for the simulation, still open) in
[`docs/backlog/bugs.md`](../backlog/bugs.md).

§5.5 showed that the first B45 run was null by construction: no simulated call could
fail, so every candidate's availability was the same and the weight could not matter.
B48 listed four things a failure-injected sweep had to get right before any number was
quoted: (a) no outcome leakage in the warm arm, (b) a dead endpoint must not make a
strategy look cheaper, (c) injected failures and warm-signal counting must match
production's `counts_as_endpoint_unhealth`, (d) the single-shot vs failover modelling
gap. §9.1–§9.4 show how each was handled. §9.5 is the result and §9.6 its limits.

### 9.1 Failure model (simulated backend only)

`SimProfile` (`app/eval/endpoints.py`) gains three optional fields (`290b555`):

| Field | Meaning |
|---|---|
| `outages: [{from: int, to: int}]` | inclusive **case-index** ranges (0-based, suite file order) in which every call to this endpoint fails |
| `error_rate: float = 0.0` | independent failure probability per `(case, endpoint)`, drawn from `Random(f"{seed}:{case.id}:{slug}:err")` |
| `failure_latency_ms: float \| None` | latency charged for a failed attempt; unset means the profile's `latency_ms.mean` |

- An outage failure raises `SimulatedEndpointFailure` with **no** `status_code`, the
  shape of a refused connection. A flake raises it with `status_code = 503`. The real
  `app/gateway/gateway.py::counts_as_endpoint_unhealth` returns `True` for both
  (`test_simulated.py::TestCountsAsEndpointUnhealth`). The outage check comes before the
  flake draw, so an outage always fails.
- Validation happens at load: `0 <= from <= to`, `0 <= error_rate <= 1`, and
  `failure_latency_ms > 0` when set. A bad file exits 1.
- **The sweep stays a single-shot measurement.** `harness_settings` now also sets
  `routing.max_failover_attempts = 0` (`b32abb1`), next to `max_escalations = 0`. This
  was found in Task 2 with a probe through the real gateway. With the deployment's
  failover of 2, a pinned sweep call that failed got retried with its only candidate
  excluded. That raised a fresh `NoEligibleCandidate`, which hid the real exception: the
  row lost its failure latency, and a 4xx would have been recorded as unhealth. With one
  attempt, the gateway's exhaustion exception (`BackendUnavailable` /
  `ContextWindowExceeded`) chains directly from the backend's exception.
- **Classifying a failure** (`3a3364e`). The runner goes exactly **one hop** below the
  gateway's own exhaustion exception (`__cause__`, never `__context__`). It records
  `counts_unhealth = counts_as_endpoint_unhealth(that exception)`, plus its
  `failure_latency_ms` and `error_class`. A failure that never reached gateway exhaustion
  (a gate rejection, a bare `NoEligibleCandidate`) gets `counts_unhealth = false`, which
  means no endpoint verdict. The first fix walked the exception chain to its deepest
  link. The Task 2 review's probe showed that walk goes past an OpenAI-shaped
  `BadRequestError` (status 400) into its status-less `__context__`, so a live 400 would
  be read as unhealth. `TestBackendExceptionUnitLevel` pins the one-hop rule.
- Error rows keep `cost_usd = 0.0`, `quality = 0` and `passed = false`. The oracle
  (§3) is unchanged: it is the best **successful** single call among `E(c)`.

### 9.2 Failover-aware definitions (B48 (b) and (d))

Production fails over, so a strategy now yields an **attempt order**, not a single choice
(`a543abb`, `1a6712b`).

- **Router arms:** the real mechanism. `Router.select(ctx, …, exclude=frozenset(failed so
  far))` is called repeatedly, for at most `1 + max_failover_attempts` attempts. That cap
  is read from the **deployment's** settings before the harness zeroes it
  (`deployment_failover_cap`): `config/routing.yaml` has 2, so the cap is 3 attempts
  (`attempt_cap` in `run.json`). The loop stops at the first success, and a
  `NoEligibleCandidate` ends the chain. This mirrors `Gateway.run`'s failover loop. For
  0, 1, 2 and 4 dead endpoints, each chain equals a hand-written loop of direct
  `Router.select` calls (`test_strategies.py::TestRouterAttemptChains`).
- **Baselines:** each strategy's own ranking, capped the same way. `cheapest` goes in
  estimated-cost order and `best_published_quality` in quality order. `fixed:<slug>`
  tries the pin first, then `cheapest`'s order without the pin.
- **Outcome of a chain on case `c`** (`oracle.attempt_outcome`), with each attempt looked
  up in the matrix:
  - an attempt **fails** if its row is an error row, and **serves** otherwise. The chain
    stops at the first attempt that serves. It fails over on an error only, never on
    quality: a below-floor answer is served, as in production;
  - `quality` is the served row's quality, or 0 if the chain is exhausted;
  - `cost` is the sum of attempt costs, and failed attempts cost 0;
  - `latency` is the sum of the failed attempts' `failure_latency_ms` plus the served
    row's latency. An unknown failure latency (possible only in a live run) counts as
    0 ms, and the report says so;
  - `wasted_attempts` is the number of failed attempts;
  - **`exhausted`** means at least one attempt was made and none served. That is the
    gateway's `BackendUnavailable`, **HTTP 502** (`app/api/routes.py`).
  - An **empty chain is "no choice", not exhausted.** A router whose very first select
    raises `NoEligibleCandidate` is production's **HTTP 422** ("no model is available for
    this request's policy"), a policy outcome and not an endpoint failure. It scores as
    §3's `None` choice.
- **Per-strategy metrics.** Pass rate, floor-miss rate and quality regret use the chain's
  outcome. Cost regret is chain cost − `c*` and latency regret is chain latency − `l*`,
  both counted over served-and-passing cases only, as in §3. `mean_chosen_cost` uses the
  chain cost. New: `wasted_attempts`, the mean number of failed attempts per non-empty
  case, and `exhausted_rate`, the share of non-empty cases whose chain was exhausted.
  **B48 (b) is closed:** a strategy that keeps choosing a dead endpoint pays for the
  failover it forces and can no longer look cheaper
  (`test_oracle.py::test_dead_first_is_not_cheaper_than_live_first_b48b`). **B48 (d) is
  closed** by modelling failover instead of stating the gap. Picking a failing endpoint
  now costs a wasted attempt and its failure latency, and quality is lost only when every
  attempt fails.
- **Violations are checked on every attempt**, including attempts recorded after the
  served one. On top of that, a chain that repeats an endpoint, records entries after the
  one that served, or whose `Choice.endpoint` is not the served endpoint is a violation.
  **Units:** `violation_rate` is per case (at most one per case), while
  `policy.violations_total` counts attempts. The report states both.

### 9.3 The three router arms, and why `router_warm` was removed (B48 (a) and (c))

| Arm | Store contents before scoring case *k* | Role |
|---|---|---|
| `router_cold` | empty | the no-measurement baseline |
| `router_online` | only the attempts **this arm made** on cases `0…k−1`, failovers included. They are applied in attempt order through the real `observe_success` / `observe_error`, and an error counts only when `counts_unhealth` is true | **the reported arm.** It knows what production would know, and nothing more |
| `router_warm_prior` | every matrix row of cases `0…k−1` (every eligible endpoint, attempted or not), via `SignalStore.replay`. An error counts only when `counts_unhealth` is true | a full-information reference, never the reported number |

Each arm also runs as `<arm>:<variant>` for every entry in `--variants`. **B45 is read
from `router_online:<variant>`**, and the report says so at the top.

**Why the old arm had to go.** `router_warm` replayed every row swept so far, **including
the rows of the case being scored** (§6.4). With failures injected, that is outcome
leakage in exactly the quantity B45 measures: the arm learns an endpoint is down from the
very call it is being scored on. The falsification is in `test_strategies.py::TestOnlineArm`:

- `test_no_leakage_when_scoring_each_case` checks two things before each case `k` is
  scored: that the arm's applied-observation log holds nothing from case `k` or later,
  and that the store's own latency and availability timestamps are all `< k`.
- `test_injected_leak_is_caught` runs the same loop with a leak injected (case `k` is
  observed before it is scored). It trips that same check (`AssertionError: case 0 scored
  with observations from [0]`), which shows the check is not vacuous. The Task 4 mutation
  M2 did the same by patching the leak into the passing test: it failed with
  `AssertionError: case 0 scored with observations from [0]` (mutation run at `1a6712b`,
  then reverted).
- `test_the_leak_changes_outcomes` uses two endpoints, one dead, with a high availability
  weight. The honest arm fails over `(dead, live)` on cases 0–4 and avoids the dead
  endpoint from case 5, once it has 5 counted failures (`min_samples`). The leaking arm
  avoids it **from case 4**. That is one wasted attempt fewer in six cases, credited to
  information production would not have had.

The guard is built into the arms, not just asserted in tests.
`OnlineArm.choose(…, case_index=k)` requires the index and raises `LeakageError` when
case `k` or a later case has already been observed. `warm_prior_signals(…,
scoring_case=k)` raises `LeakageError` if it is handed a row from case `k` or later. The
runner tests trip both guards (`test_runner.py::test_runner_out_of_order_online_call_trips_the_leak_guard`,
`test_runner_passing_the_scored_cases_rows_to_warm_prior_is_refused`).

**B48 (c).** Both warm arms count an error row only when `counts_unhealth` is true. With
`counts_unhealth = false` the dead endpoint never enters the store, and the arm never
learns to avoid it: `test_availability_drops_after_unhealth_failures_only` and
`test_strategies.py::test_non_unhealth_errors_are_skipped_without_consuming_a_slot`. The
Task 4 mutation M3, which counted every error (`elif True`), fails the first of these: the
dead endpoint then appears in the snapshot, with availability 0.0015 (mutation run at
`1a6712b`, then reverted).

**One difference from production.** The gateway calls `observe_error` in the middle of a
request, so the next hop is selected after the failed model's signal has moved. Here a
case's observations are applied after its whole chain is chosen. This cannot change the
chain, because the failed model is in `exclude` for every later hop either way.

**`router_warm_prior` is not an upper bound on outcomes.** It sees the flakes of an
endpoint the online arm is not using, which lowers that endpoint's availability as well.
In the outage runs it wasted **more** attempts than `router_online` in some runs:
availability 1.0 at seed 2 (1 s/case) and seeds 2 and 6 (30 s/case), and availability
2.0 at seed 6 (1 s/case) and seeds 2, 3 and 6 (30 s/case). Call it a full-information
reference, never an upper bound. The spec was amended to say so (`92be40c`, 2026-09-22),
and the arm notes in `run.json` / `report.md` and the code docstrings now read
"full-information reference (not an upper bound)".

### 9.4 The case clock, and why `seconds_per_case` matters

Neither non-cold arm reads a live clock. Case `k` happens at `t_k = k × seconds_per_case`,
and each observation applied within that case is 1 ms after the previous one. When the
arm scores case `k` it **reads at `t_k`**, so every observation it has applied is older
than `t_k`. The value is a run parameter (`--seconds-per-case`, default 30), recorded in
`run.json` and `report.md` (`153a0d4`, implemented in `8344b21`).

**Why this matters.** In production, time passes between requests even when nothing is
observed. Availability decays back toward 1.0 **on clock time**, with a 900 s half-life
(`SignalStore._decayed_availability`). That decay lets a down-weighted endpoint earn a
retry even though failover stops it receiving traffic. The first draft of the online arm
spaced cases 1 s apart and read at the last observation. At that spacing, three
half-lives (a deficit shrinking to 1/8) take **2,700 cases**, far longer than any suite.
Recovery would essentially never be exercised, which biases B45 against high
availability weights for a reason production does not share. The Task 4 review found
this, and the spec was amended to add the case clock and to report B45 across
`seconds_per_case` ∈ {1, 30, 300}.

A closed form shows the scale (derived from `signals.py`, not measured). Once an endpoint
has 9 or more samples its EWMA step is `alpha = 0.1`. If it fails once per case, its
availability deficit converges to

`D* = 0.1·r / (1 − 0.9·r)`, with `r = 0.5^(Δ/900)` and `Δ = seconds_per_case`.

That gives `D*` = **0.992** at 1 s, **0.811** at 30 s and **0.278** at 300 s. The most
availability can ever subtract from an always-failing endpoint's score is `w·D*`. At
300 s even `w = 2` cannot exceed about 0.56, whatever the outage length.
`latency.staleness_s` (3600 s) also runs on this clock: a latency not refreshed for
3600 s falls back to the tier default. That is 12 cases at 300 s/case, and it does not
bind in these runs.

### 9.5 The B45 result (simulated, designed scenario)

**Scenario** (`eval/suites/outage.jsonl`, `eval/endpoints/outage-sim.yaml`, Task 5). The
suite has 30 public FAQ cases (`residency: any`). Both endpoints are `cheap`-tier on-prem
with equal published quality 0.90, so both are always eligible and in the target tier.

- `fast-cheap` costs $0.10/1M at about 300 ms. It is down for case indices **10–19**
  (connection-refused shape), and a failed call costs **1000 ms**.
- `steady` costs $0.40/1M at about 600 ms, with `error_rate` 0.05 (HTTP 503).

The prices, outage span and failure latency were fixed before the first run (the YAML
header says so). Nothing was re-tuned after seeing results.

**Stamp.**

```
suite            eval/suites/outage.jsonl        30 cases
suite_hash       78e1be2bc1b234ba8ea418f83eeb890fc700dae75179425facc194f6a4dd9969
endpoints        eval/endpoints/outage-sim.yaml  (fast-cheap, steady)
endpoints_hash   bdb74978978dbe2ef956e7e7a447bec80fbcc9d59525bd5b74f9f6bfe465f7d6
variants         eval/variants/b45.yaml          (default 0.2, availability_0/1/2 = 0.0/1.0/2.0;
                                                  quality/cost/latency fixed at 1.0/0.6/0.3)
seeds            0–9        seconds_per_case  1, 30, 300      attempt_cap 3
git_sha          8344b21fb287042b434b0d5dd9b4a639ba9768c2
base_routing_policy_version  a4bf5708d2db      registry_version  abeacbc653b5
mode             simulated  SP_PII_DETECTION=regex
result           30/30 runs exit 0; violations_total 0 in every run
```

`metrics.json` md5, first 8 hex digits, for seeds 0–9 in order:

| `seconds_per_case` | seed 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `54094729` | `764510e7` | `bb1158ec` | `dd2300a2` | `6355f3af` | `b22432d2` | `da4d5db3` | `b4301fc3` | `431045a5` | `108f6bbe` |
| 30 | `53cc513f` | `6295503d` | `f1b5f591` | `0dec3b7b` | `887409b9` | `374be0da` | `d6a5daef` | `acd443bf` | `aeadc349` | `8acccfb1` |
| 300 | `b08300c2` | `4f8bb1ec` | `35c686f2` | `bcd35a00` | `6e2401ae` | `0af4b790` | `82629247` | `65b1c123` | `64c3a1a5` | `dd9a2869` |

Seed 0 at 30 s re-run under `PYTHONHASHSEED=1` gives the same md5
(`53cc513f24785d505b52359aa8a3bce1`).

**The matrix** is the same at every spacing, as it should be: the sweep does not depend
on the clock.

- `fast-cheap` fails on exactly cases 10–19 at every seed, all with `counts_unhealth`
  true.
- `steady` flaked **0 times at seed 0**, and 1–4 times at each of seeds 1–9: 14 flakes in
  270 draws, 5.2%.
- 5 of those 14 flakes fell inside the outage, one each at seeds 4, 5, 7, 8 and 9. On
  those cases both endpoints fail, and **every** strategy's chain is exhausted. That is
  the whole of `exhausted_rate` (1/30 at those seeds, 0 elsewhere). No weight can change
  it.

**Result: `router_online:<variant>`, mean over seeds 0–9 [min–max].** "Wasted per run"
is the number of failed attempts in the 30-case run (`wasted_attempts` × 30). The baseline
row is `availability_0` (weight 0).

| `seconds_per_case` | variant (weight) | wasted per run | mean chosen cost (USD) | latency regret (ms) | cost regret (USD) |
|---|---|---|---|---|---|
| 1, 30, 300 | `availability_0` (0.0) | 10.5 [10–11] | 2.251e-06 | 326.1 [285.7–344.8] | 0 |
| 1, 30, 300 | **`default` (0.2)** | **10.5 [10–11]** | **2.251e-06** | **326.1** | **0** |
| 1 | `availability_1` (1.0) | 9.9 [8–12] | 3.247e-06 (+44%) | 381.2 [318.8–435.7] | 9.6e-07 |
| 1 | `availability_2` (2.0) | 5.5 [3–8] | 3.135e-06 (+39%) | 217.0 [100.0–300.2] | 8.5e-07 |
| 30 | `availability_1` (1.0) | 10.0 [9–11] | 2.286e-06 (+2%) | 313.5 [285.7–333.3] | 2.5e-08 |
| 30 | `availability_2` (2.0) | 5.7 [4–8] | 3.043e-06 (+35%) | 221.2 [136.1–292.8] | 8.0e-07 |
| 300 | `availability_1` (1.0) | 10.5 [10–11] | 2.251e-06 | 326.1 | 0 |
| 300 | `availability_2` (2.0) | 10.5 [10–11] | 2.270e-06 (+1%) | 328.6 [285.7–357.1] | 8.3e-09 |

`cheapest`, `best_published_quality` and every `router_cold*` arm equal the weight-0 row
at every spacing. With no measurements, the cold router's order is the price order.

**B45, answered for this simulation: no.** At the shipped `availability: 0.2`,
`router_online` sent the first attempt of **every** outage case to the dead endpoint, at
every seed and every spacing. Its attempt chains are **identical, case by case, to weight
0** in all 30 runs, and the same holds for `router_warm_prior` and `router_cold`
(checked chain by chain and metric by metric). Quality was not lost, because failover
served every outage case unless `steady` also flaked. What 0.2 costs here is **one wasted
attempt and the dead endpoint's failure latency (1000 ms, a designed value) on every
request during the outage**.

**Why w = 0.2 behaves like w = 0 here.** The step-6 score is
`1.0·q − 0.6·cost_norm − 0.3·lat_norm + w·availability`, where
`cost_norm = est_cost / max est_cost in the set` and
`lat_norm = latency_ms / 1000`. Latency is measured once an endpoint has 5 samples;
before that it is the cheap-tier default of 200 ms. **Where the score values come from.**
Every per-case score, lead, availability and margin quoted in this section was printed by
`scripts/b45_score_probe.py` (the router's own `ScoredCandidate` terms at each
`router_online` first hop; §9.8 gives the command). Each value is labelled with its run:
seed 0 at 1, 30 or 300 s/case, or seed 4 at 30 s/case. The run's `metrics.json` is
byte-identical to the unprobed run's (§9.8). The bullets below are **seed 0, 1 s/case**
(cases 10 and 15 read the same at every spacing):

- Before the outage, `fast-cheap` has `cost_norm` 0.25 and a measured latency of about
  298 ms, so its non-availability score is 0.9 − 0.15 − 0.0895 = **0.6605**. `steady`
  is unmeasured: `cost_norm` 1.0 and the 200 ms default give 0.9 − 0.6 − 0.06 =
  **0.2400**. The lead is **0.4205** (case 10).
- Once `steady` has 5 samples (its failover attempts at cases 10–14), its latency flips
  from the 200 ms default to a measured 581 ms, and the lead grows to **0.535**
  (case 15).
- The availability term can take back at most `w`. At `w = 0.2`, even a completely dead
  `fast-cheap` (availability 0) would keep a lead of 0.2205 (0.335 once `steady` is
  measured). The smallest lead observed at `w = 0.2` was +0.352, at case 14, where
  `fast-cheap`'s availability was 0.657 after 4 failures. For the dead endpoint to lose,
  the weight must satisfy `w > lead / (1 − availability)`. That is more than 0.42 even at
  availability 0, and after `m` failures availability is about `0.9^m`, not 0.

**The general statement.** Take two candidates with equal quality and equal latency
terms, where the dead one costs a fraction `p` of its peer's estimated cost (the peer is
the set maximum) and the peer's availability reads 1.0. The dead one's lead is `0.6·(1 − p)`. It loses at weight `w` only if
`w·(1 − availability) > 0.6·(1 − p)`. Even when it is perfectly dead, that requires
`p > 1 − w/0.6`. **At `w = 0.2`, a dead endpoint loses only if its price is more than
about 67% (2/3) of its peer's**, a cost-norm gap under 1/3. In this scenario `p = 0.25`.
A latency difference adds `0.3·Δlat_norm` to either side. This matches, and quantifies,
the backlog's original B45 demonstration (a dead endpoint at +0.38 ranked above a healthy
peer at +0.04).

**Switch points (seed 0, `router_online`, first hop).** "Switch" means the first attempt
moves from `fast-cheap` to `steady`. Each column's scores come from the probe run at seed 0
and that column's spacing.

| weight | 1 s/case | 30 s/case | 300 s/case |
|---|---|---|---|
| 0.2 | never | never | never |
| 1.0 | switches at case 18 (availability 0.432 after 8 failures: penalty 0.568 against a lead of 0.535); **does not return** in the 10 remaining cases | switches at case 19, returns at case 21 | never (availability never falls below about 0.73) |
| 2.0 | switches at case 13 (availability 0.729 after 3 failures: penalty 0.541 against a lead of 0.4205); returns at case 20 | switches at case 13, **retries** `fast-cheap` at case 15, switches again at 16, returns at 23 | never at seed 0 (at case 20 it misses by 0.0018) |

Each cell reads from the traces as follows.

- **Stickiness is a clock-time constant, not a case count** (seed 0, 1 s/case). Once the router stops
  sending traffic to `fast-cheap`, it can only come back when decay lifts
  `fast-cheap`'s availability. At `w = 1` that means rising from 0.432 to above about
  0.459, the point where it overtakes `steady` (score 1.119). That takes about
  `900·log2(0.568/0.5415)` ≈ **62 s of clock after its last failure**. At 1 s/case that
  is about 62 cases, longer than the rest of the suite. At 30 s/case it is 2 cases. In
  production terms, `w = 1` keeps a recovered endpoint out of rotation for about a
  minute of wall time, whatever the request rate.
- **The return at case 20 (`w = 2`, seed 0, 1 s) is a coincidence, not a recognition of
  recovery.** The margin is **7×10⁻⁶** score units (2.1222154 vs 2.1222083). It came
  from `steady`'s latency EWMA creeping from 586.5 to 592.6 ms plus 1 s of decay. Case 20
  just happens to be the first healthy case.
- **The latency flip** (seed 0, 30 s/case). At `w = 2`, `steady`'s latency flips to measured on its
  5th sample. That raises the non-availability lead from about 0.42 to 0.535, and
  `fast-cheap` wins case 15 (2.1655 vs 2.1256) **while still down**: a wasted attempt
  caused by the latency flip, not by availability. This is production's `min_samples`
  behaviour. An unmeasured candidate reads its tier default (200 ms here), which is
  optimistic for a 600 ms endpoint. Once measured, the healthy peer looks worse, and the
  dead endpoint looks relatively better.
- **At 300 s** (seed 0, 300 s/case), decay between cases (factor 0.794) holds `fast-cheap`'s deficit to 0.268
  after 10 failures (the limit from §9.4 is 0.278). At `w = 2` the penalty is 0.536,
  against a lead of 0.538: the score misses by 0.0018 at case 20 at seed 0. At seeds 1
  and 6 it crosses once, **at case 20, after the outage ended**, which is a switch that
  only costs.

**Across seeds.**

- **For weight 0 and 0.2**, every seed and every spacing shows the same picture as
  seed 0: the two rows are identical, and every outage case's first attempt goes to the
  dead endpoint. For weights 1.0 and 2.0, switch points and returns vary by seed (below).
- `w = 2` roughly halves the wasted attempts at 1 s and 30 s per case, and pays for it
  with 35–39% higher mean chosen cost.
- `w = 1` is the worst of the variants at 1 s/case. It switches late in the outage and
  sticks after recovery. At seeds 4, 5, 7 and 9 it first switches at case 20, the first
  healthy case, and at seed 7 it wastes 12 attempts against weight 0's 11. Its latency
  regret (381 ms) is worse than weight 0 (326 ms).
- At 30 s/case, `w = 1` saves at most one attempt per run. At 5 of 10 seeds (4, 5, 7, 8,
  9) it never switches at all. At those seeds `steady` flaked inside the outage, so its
  own measured availability fell too: at seed 4 (30 s/case, probe run), one flake in its
  first 5 samples puts it at 0.81 at case 15, where `fast-cheap` still leads by 0.220. The availability term is **relative**, and a healthy peer's small-sample
  availability is noisy.
- Pass-rate differences between variants (0.950 vs 0.943–0.947) come from the declared
  simulated qualities (`fast-cheap` FAQ 0.97, `steady` 0.95). They are an input, not a
  finding (§6.1).

**Both sides of a larger weight** (mean over seeds 0–9, `router_online`, against weight 0;
from `choices.jsonl`, per-case chain cost and latency differences):

- **The benefit is latency.** Every attempt a larger weight keeps off the dead endpoint
  saves that attempt's failure latency. At `w = 2`, latency regret is **217 ms** at 1 s and
  **221 ms** at 30 s per case, against **326 ms** at weight 0. Inside the outage it saves
  about 5,400 ms (1 s) and 5,000 ms (30 s) per 30-case run. Quality does not change: failover
  served those cases anyway.
- **The cost is all after recovery.** Inside the outage the extra cost is **exactly 0** for
  every weight: a failed attempt costs 0, and failover already served those cases on
  `steady`. The whole mean-chosen-cost increase (for example +35–39% at `w = 2`) comes
  from cases after the outage ends (cases 0–9 are identical for every weight, at every
seed and spacing), where the router sticks to
  `steady`, the endpoint that costs 4× as much. Those cases also give back latency: about
  +2,000 to +2,400 ms per run at `w = 2`, about 300 ms per case served on `steady`, which
  the net 217/221 ms already includes. At `w = 1` and 1 s/case, the stick costs more than
  the outage saves: +2,700 ms outside, −1,000 ms inside, so latency regret is 381 ms.
- **Both sides scale with designed values.** The saving scales with the failure latency
  (1000 ms, declared). The cost scales with the price ratio (0.25, declared) and with how
  long the router sticks, which is set by clock time. A cheaper failure (a fast connection
  refusal) or a smaller price gap moves the balance. These runs measure one point.

**Recommendation to the owner (a recommendation, not a config change; `config/` is
untouched on this branch).** The simulated evidence says:

1. `weights.availability: 0.2` cannot move traffic off a dead endpoint that undercuts its
   peer by more than a third. Operators should not expect it to.
2. No single weight tried is good across time scales. The weights large enough to act
   (1.0 and 2.0 here) buy a latency saving during the outage (up to about 1 s per avoided
   attempt, as designed here), and pay for it after recovery by sticking to the pricier
   endpoint: +35–44% mean chosen cost in the runs where they moved the most traffic
   (`w = 2` at 1 s and 30 s, and `w = 1` at 1 s). They help only when requests arrive
   seconds to tens of seconds apart. Their effect lags the outage by several failures,
   and they keep a recovered endpoint out for roughly a minute. The weight needed at all
   depends on the price ratio of the specific pair (`w > 0.6·(1 − p)` at availability 0).
3. So the lever for a dead endpoint is failover, which already absorbs the quality loss
   here, plus the step-7 breaker that is not built yet, or disabling the endpoint. It is
   not a larger availability weight. That is a judgement on this evidence. An owner who
   values the outage-time latency saving above the post-recovery cost could reasonably
   choose otherwise.

**Recommended:** keep 0.2 for now, as a ranking preference. Decide the breaker question
with B45 in view (B45 "Related"). Revisit the weight only after a live run. Changing
`weights.availability` is the owner's decision.

### 9.6 Limits: what this does NOT show

- **A designed scenario.** There are two endpoints, one price ratio (0.25), one outage
  span (cases 10–19), one failure latency (1000 ms) and one suite order. It shows what
  the scoring formula and signal store do in this configuration. It does not show how
  often such configurations occur, or what the regret would be on real traffic. The
  general statement in §9.5 comes from the formula, not from the runs. The runs confirm
  it at one point.
- **Simulated failures.** Failures are exceptions shaped like production's, and they are
  classified by production's own predicate. There are no timeouts with real waiting, no
  partial or streamed failures, no 429 with retry-after, and no correlated failure other
  than the one designed outage. Failure latency is declared, not measured.
- **Outages are indexed by case, not wall time.** The case clock maps index to time
  uniformly. Real traffic is bursty, and an outage's length in seconds here is
  `10 × seconds_per_case`. That is why B45 is reported at three spacings, not one.
  Results also depend on the suite's case order, because the arms learn in that order.
- **Mostly a one-sided outage.** At seed 0 `steady` drew **no** flakes, so there was no
  failover outside the outage, no exhausted chain, and `steady`'s availability stayed
  1.0 for the online arm. At seeds 1–9 it flaked 14 times in total. 5 of those flakes
  coincided with the outage and exhausted that case for every strategy. The ones outside
  it matter only to an arm that is sending `steady` traffic (the online arm after it
  switches, and the warm-prior arm always). So the scenario mainly measures the response
  to a one-sided outage, with a light, incidental two-sided component. No designed
  two-sided scenario (both endpoints degraded, or the expensive one failing) has been
  run. The seed and rates were not changed to create one.
- **Small.** The suite has 30 cases, and the matrix is run at 10 seeds. The per-seed
  ranges in §9.5 are wide (for example, `w = 2` at 1 s wastes 3–8 attempts).
- **`router_warm_prior` is a full-information reference, not an outcome bound** (§9.3).
- **No live run.** Every §9 number is simulated. A `--live` run with a deliberately
  failing endpoint is still pending, and B45 stays open until one exists.
- **B45's answer is a recommendation.** Nothing in `config/routing.yaml` was changed, and
  changing it is the owner's decision.

### 9.7 Invariants B48 added, and the test that pins each

| Invariant | Pinned by | Falsified? |
|---|---|---|
| Failure injection is deterministic per seed, and both failure kinds pass `counts_as_endpoint_unhealth` | `test_simulated.py::TestFailurePatternDeterminism`, `TestCountsAsEndpointUnhealth`, `TestOutages`, `TestErrorRate` | Yes: a different seed gives a different failure pattern |
| A matrix row carries the gateway's own verdict, one hop below exhaustion. A 400 is not unhealth, and a non-gateway failure gets no verdict | `test_runner.py::test_outage_row_records_endpoint_unhealth_and_failure_latency`, `test_flake_row_records_endpoint_unhealth`, `test_400_status_backend_failure_does_not_count_as_unhealth`, `TestBackendExceptionUnitLevel`, `test_non_gateway_pipeline_failure_is_not_an_endpoint_verdict` | Yes: the review probe showed the deep walk misreading a live 400 (fixed in `3a3364e`) |
| The sweep makes one attempt, and the scoring cap is the deployment's | `test_endpoints.py::test_gateway_failover_is_off_and_base_untouched`, `TestDeploymentFailoverCap`; `test_strategies.py::TestAttemptCap` | Yes: `attempt_cap(harness settings)` is 1, which is pinned to show what the wrong settings object would give |
| Failover scoring matches hand-computed chains, including an exhausted chain and the cap | `test_oracle.py::TestAttemptOutcome`, `TestFailoverAwareStrategyMetrics`; `test_strategies.py::test_cap_three_failing_candidates_is_exhausted_after_three`, `test_all_failing_router_chain_is_capped` | n/a (hand-computed) |
| A dead endpoint never makes a strategy look cheaper (B48 (b)) | `test_oracle.py::test_dead_first_is_not_cheaper_than_live_first_b48b` | n/a |
| Router attempt order = repeated real `Router.select(…, exclude=…)` | `test_strategies.py::TestRouterAttemptChains` | Yes: mutation M4 (`exclude=frozenset()`) failed the test, with the chain repeating `b-model` (run at `1a6712b`, then reverted) |
| No leakage in `router_online` or `router_warm_prior` | `TestOnlineArm::test_no_leakage_when_scoring_each_case`; `TestWarmPriorSignals::test_rows_from_the_scored_case_raise`; runner-level guard tests | **Yes**: `test_injected_leak_is_caught`, `test_the_leak_changes_outcomes`, mutation M2 (`AssertionError: case 0 scored with observations from [0]`) |
| Warm arms count only unhealth errors (B48 (c)) | `test_availability_drops_after_unhealth_failures_only`, `test_warm_signals_skips_error_rows_that_do_not_count_as_unhealth`, `test_non_unhealth_errors_are_skipped_without_consuming_a_slot` | Yes: mutation M3 (every error counted: the dead endpoint appears in the snapshot, availability 0.0015) |
| Violations are checked on every attempt; repeats, trailing entries and endpoint ≠ served are violations | `test_oracle.py` (ineligible second attempt, violation after a success, the ruling-D tests); `test_runner.py` (entries after the served attempt exit 2) | Yes: each test injects the defect |
| **No-failure invariant.** With no failures, availability is exactly 1.0 in every arm and every weight variant gives identical outcomes | `test_strategies.py::TestNoFailureInvariant`; `test_starter_suite.py::test_starter_run_against_healthcare_sim_is_clean` | n/a. §5.1 re-reads it at `8344b21`: starter figures unchanged |
| The outage scenario is deterministic, and its online availability variants differ | `test_outage_scenario.py::test_outage_run_is_deterministic_clean_and_moves_the_online_variants` | The test asserts only **that** the variants differ, never in which direction (Task 5 ruling) |

At `8344b21`: `tests/eval` has 265 test functions and 270 collected cases, all passing in
about 29 s. The full suite, excluding the Presidio-dependent test this venv cannot import,
gave 2981 passed and 15 skipped (Task 5 report).

### 9.8 Reproducing §9

```bash
git checkout feat/b48-failure-injection        # code at 8344b21
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run pytest tests/eval -o addopts="" -q     # 270 passed

# one cell of §9.5; repeat for --seed 0..9 and --seconds-per-case 1, 30, 300
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run python scripts/feasible_bench.py \
    --suite eval/suites/outage.jsonl \
    --endpoints eval/endpoints/outage-sim.yaml \
    --variants eval/variants/b45.yaml \
    --seed 0 --seconds-per-case 30 --out /tmp/fb-outage-s0-30
md5sum /tmp/fb-outage-s0-30/metrics.json      # 53cc513f24785d505b52359aa8a3bce1 at 8344b21

# the per-case score values quoted in §9.5 (seed 0 at 1, 30 and 300 s/case; seed 4 at 30)
SP_PII_DETECTION=regex uv run python scripts/b45_score_probe.py \
    --seed 0 --seconds-per-case 1 --out /tmp/b45-probe-s0-1
# ... --seed 0 --seconds-per-case 30, --seed 0 --seconds-per-case 300, --seed 4 --seconds-per-case 30
md5sum /tmp/b45-probe-s0-1/metrics.json        # 5409472979c3ed0f08288ed063fe69da: same as the unprobed run
```

Write the output outside the repository, or under the gitignored `results/`. Run output
is not committed. The first attempt of each chain is in `choices.jsonl` (`attempts[0]`
per `strategy`, `case_id`), and switch points are read from there. `choices.jsonl` records
chains, not scores, so the per-case score terms quoted in §9.5 come from
`scripts/b45_score_probe.py`. It wraps `OnlineArm.choose` and
`DefaultRoutingStrategy.rank` to log the router's own `ScoredCandidate` terms at each
`router_online` first hop, and changes nothing else. Its `metrics.json` md5s at seed 0
(1/30/300 s: `54094729…`, `53cc513f…`, `b08300c2…`) and seed 4 at 30 s (`887409b9…`)
equal the unprobed runs in §9.5.
