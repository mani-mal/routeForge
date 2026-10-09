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
**Slice:** P2b-1, the offline harness. P2b-2 (production outcome records) was built on
2026-09-23 (§13). P2b-3 — offline, human-approved quality calibration, the first thing
built on top of the P2b-2 dataset — was built on 2026-09-24 (§14). P3a — the offline
policy counterfactual simulator, which replays P2b-2 records through the real eligibility
and routing code under a proposed config edit — was also built on 2026-09-24 (§15).
§13.4's other prerequisites for online exploration (propensity logging, a denser label, a
workload taxonomy) remain not built.
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

**Update 2026-09-23 (§12).** The step-7 circuit breaker is built, and the same outage scenario
was re-run with it on and off (simulated, seeds 0–9). It removes 7 of the 10 dead first
attempts at 1 s per case and 4 of 10 at 30 s per case, and does nothing at 300 s per case,
which is an effect of the bench's case clock, not of production. It pays for this after
recovery, by keeping the cheaper endpoint out for the rest of its cooldown: +51% and +39% mean
chosen cost.

**Update 2026-09-23 (§13).** P2b-2 is built: RouteForge now writes one content-free
outcome record per routed request, keeps feedback scores locally, and exports both as a
dataset. §13 records the dataset's model, its invariants, and what it can and cannot
support. It contains no results: nothing has been deployed, so there is no production
data yet.

**Update 2026-10-07 (§22).** Two of the metrics §6.5 lists as missing are now reported,
both opt-in and both offline: a group report with denominators and a minimum group size,
and an offline router-version check that compares a candidate routing config with the
current one over the same sweep. Calibration and churn robustness are designed and not yet
built. §22's numbers are simulated.

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

*Status 2026-10-07.* Still true of the code, and `report.md` still says so. What changed:

- **Worst-group results** now have sizes, denominators, a minimum group size and an
  application dimension (§22.1). That was §3's metric 6, half built.
- **An offline router-version check** exists (§22.2).
- **Calibration is designed, not built.** The router does emit no confidence. The gateway
  does read one: the backend's own figure for an answer, which the escalation threshold is
  compared with, and which is recorded nowhere. That is the only thing there is to
  calibrate; the design measures it and says in fixed text that RouteForge has no
  calibrated routing confidence. The simulated backend reports 1.0 for every answer, so in
  simulation the result is, by construction, "not measurable".
- **Churn robustness is designed, not built**: one declared change to the policy or the
  registry at a time, the suite re-run, violations required to stay 0.

Both designs are in `docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` §6 and §9.

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

---

## 10. First live run on the DGX Spark (2026-09-22)

**What this is:** the first `--live` FeasibleRouteBench run against real on-prem endpoints: the
starter suite (22 cases) × the three models onboarded on the owner's DGX Spark, one seed. **It is
a signal, not a quotable result** (see §10.4). No endpoint failed, so it **does not answer B45**
live; it did surface a routing defect, filed as backlog **B51**.

### 10.1 Setup and stamp

| Field | Value |
|---|---|
| Source | `d9906f8` (exported to `~/projects/routeForge/feasible-bench/` on the Spark; `run.json` shows `git_sha: unknown` because the export has no `.git` — the commit is recorded in that directory's `SOURCE_COMMIT`) |
| Suite | `eval/suites/starter.jsonl`, `suite_hash` `3339c773…d8199` |
| Endpoints | a local file mirroring the live registry, identical in content to the corrected `eval/endpoints/spark-live.example.yaml` with `<spark>` = `127.0.0.1`; `endpoints_hash` `d96737d8…31d3`; `registry_version` `81da53fced52` |
| Routing config | `base_routing_policy_version` `a4bf5708d2db` |
| Run | `--live --seed 0 --seconds-per-case 30`, `attempt_cap` 3, 66 calls, 2026-09-22 17:11–17:25 UTC, exit 0 |
| Execution | a throwaway container from the deployed API image (for its Python environment only), `--network host`, `PYTHONPATH` pointed at the export; the running API, its database and the deployed checkout were not touched |
| Outputs | on the Spark only: `feasible-bench/results/live-starter-20260922T171117Z/` (completion text not kept) |

All three endpoints are `onprem`, tier `cheap`, priced $0 — exactly as onboarded — so every case's
feasible set is all three models.

### 10.2 Per-endpoint outcomes (the sweep)

| Endpoint | Passed | Errors | Mean latency | Median latency | Failed on |
|---|---|---|---|---|---|
| `qwen3.8-27b` | 21/22 | 0 | 16.2 s | 6.9 s | 1 on-prem case |
| `nemotron-3.5-lightning` | 18/22 | 0 | 18.4 s | 12.3 s | 3 claim summaries, 1 on-prem |
| `llama3.2:latest` | 9/22 | 0 | 3.6 s | 2.4 s | all 10 claim summaries, 3 on-prem |

Every model passed all 8 FAQ cases. The claim summaries separate them: Qwen 10/10, Nemotron 7/10,
Llama 0/10.

### 10.3 Strategies

| Strategy | Pass rate | Endpoint picked (first attempt) |
|---|---|---|
| `router_cold` | 0.955 | Qwen ×22 |
| `router_online` | **0.636** | Qwen ×12, Nemotron **×5**, Llama **×5** |
| `router_warm_prior` | 0.955 | Qwen ×22 |
| `cheapest`, `best_published_quality` | 0.409 | Llama ×22 |

`violations_total` 0, `wasted_attempts` 0 for every arm (no failures), headline **not comparable**
(pass rates differ and the reference cost is $0).

**Reading it correctly:**

- **`router_cold`'s 95% is a tie-break, not a judgement.** All three candidates are the same price
  ($0), the same tier, and have no measured quality (tier default for every onboarded model), and
  an empty store gives them the same latency default. The router has nothing that distinguishes
  them; Qwen wins the tie. The baselines break the same tie toward Llama, hence 41%.
- **`router_online`'s drop is the finding (B51).** Its store measures latency from its own calls. A
  measured Qwen (≈16 s) competes against unmeasured peers scored at the `cheap` tier default of
  200 ms (`config/routing.yaml:23`) — ~80× faster on paper — so the arm moves traffic to them. It
  sent **exactly** `min_samples` (5) requests to each unmeasured model before their real latency
  counted; after that, every model is slower than the `max_latency_norm` cap (3 s), they tie on
  latency, and the arm returns to Qwen. With no quality signal, some of that warm-up traffic landed
  on Llama for claim summaries it fails every time. **Mechanism inferred** from the pick counts
  matching `min_samples` exactly and from the config; not traced call by call.
- **`router_warm_prior` stayed on Qwen** because it replays every earlier row for all three models,
  so all are measured early, all hit the latency cap, and the tie-break holds.

### 10.4 Limits

- One seed, 22 cases, live latencies (possibly affected by concurrent Qwen Code use on the same
  GPU). A signal to investigate, not a result to quote.
- No endpoint failed, so availability and failover were not exercised: **B45 remains unanswered
  live.** Answering it needs a real outage (e.g. stopping one model's server mid-run — only with
  the owner's agreement, since the same servers serve Qwen Code).
- Quality is deterministic-grader pass/fail on synthetic cases; the claim-summary gap reflects
  these graders, not a general capability ranking.

### 10.5 Consequences

- Backlog **B51**: the optimistic unmeasured-latency default and the latency cap make every restart
  or onboarding pay ~5 requests per model of quality-blind exploration. Fix directions are listed
  there; the durable one is a measured quality signal (P2b-2 → P2b-3).
- The example endpoint file shipped in P2b-1 put all three models on `:8000` with three different
  tiers; it is corrected (per-model ports, all `cheap`), and its test now pins the ports.

---

## 11. Live re-runs on the DGX Spark: B51 fix and B45 outage (2026-09-23)

Two `--live` runs at `5da06a4` (the B51 fix, PR #65), both from an export at
`~/projects/routeForge/feasible-bench-5da06a4/` on the Spark, executed as in §10.1 (a throwaway
container from the deployed API image, `--network host`, run as the host user so it can write
`results/`). The running API, its database and the deployed checkout were not touched. Both
runs are one seed, so treat them as **signals, not quotable results**. Outputs are on the Spark
only (completion text not kept).

### 11.1 B51 re-run: the starter suite, same setup as §10

| Field | Value |
|---|---|
| Suite / endpoints | as §10.1: `suite_hash` `3339c773…d8199`, `endpoints_hash` `d96737d8…31d3`, `registry_version` `81da53fced52`, `base_routing_policy_version` `a4bf5708d2db` |
| Run | `--live --seed 0 --seconds-per-case 30`, `attempt_cap` 3, 66 calls, 2026-09-23 13:17–13:34 UTC, exit 0, `violations_total` 0 |
| Output | `results/live-starter-b51-20260923T131738Z/` |

| Strategy | §10 (`d9906f8`) | §11 (`5da06a4`) |
|---|---|---|
| `router_online` | 0.636: Qwen ×12, Nemotron **×5**, Llama **×5** | **0.955: Qwen ×22** |
| `router_cold` | 0.955: Qwen ×22 | 0.955: Qwen ×22 |
| `router_warm_prior` | 0.955: Qwen ×22 | 0.955: Qwen ×22 |
| `cheapest`, `best_published_quality` | 0.409: Llama ×22 | 0.455: Llama ×22 |

The sweep itself moved a little between the two days (Llama 9 → 10/22, Nemotron 18 → 19/22,
Qwen 21/22 both days; mean latency 5.3 / 16.9 / 22.0 s), which is why the baselines changed.
`router_online` is the arm the fix targets: with the peer prior an unmeasured model scores at
its measured tier-mates' median latency instead of 200 ms, so there is no warm-up and the arm
stays with its tie-break winner. This is the live confirmation of the offline check recorded
in backlog B51. What it does **not** show: any model being chosen *for a reason*. All three
still tie on price, tier, quality default and (above the 3 s cap) latency, so every router arm
is a tie-break. That needs a measured quality signal (P2b-2 → P2b-3).

### 11.2 B45 live outage run

**Setup.** The simulated scenario of §9 (`outage-sim.yaml`) with real endpoints
(`eval/endpoints/spark-outage-live.example.yaml`): `llama3.2:latest` as the cheap endpoint
(illustrative price 0.10 per 1M) and `nemotron-3.5-lightning` as the healthy peer (0.40),
equal published quality 0.90, both `cheap`. The outage is **real at the transport level but
injected**. No model server was stopped (the same servers serve an interactive coding
client). Instead `scripts/outage_proxy.py` sat in front of Ollama and, for exactly the
questions of cases 10–19, closed the connection without a response. Everything else went
through to Ollama unchanged. Its log shows 10 drops and 20 passes, one per sweep call, as
designed.

| Field | Value |
|---|---|
| Suite | `eval/suites/outage.jsonl`, `suite_hash` `78e1be2b…9969` |
| Endpoints | `endpoints_hash` `8d44fbca…0d04`, `registry_version` `1d210fb00609` |
| Run | `--live --variants eval/variants/b45.yaml --seed 0 --seconds-per-case 30`, `attempt_cap` 3, 60 calls, 2026-09-23 13:34–13:37 UTC, exit 0, `violations_total` 0 |
| Output | `results/live-outage-b45-20260923T133434Z/` |

**The live failure path works end to end.** Each dropped call surfaced through the real
LiteLLM gateway as `InternalServerError … Connection error`. The runner recorded all 10 rows
with `counts_unhealth: true` (production's `counts_as_endpoint_unhealth` verdict) and
`failure_latency_ms: null`. The connection closed immediately, so no failure latency was
measured and chains charge it as 0 ms (§9.2). There were no other errors. Mean successful
latency was Llama 0.3 s and Nemotron 4.4 s, short FAQ answers. The one non-error failure,
`outage-faq-028` on Llama, is a grader miss, and it gives every strategy 29/30.

**First attempts (L = the dead-during-outage Llama, N = Nemotron), cases 0–29:**

| Arm | Chain firsts | Wasted attempts |
|---|---|---|
| `router_online:default` (w = 0.2) | `L` ×30 | 10 |
| `router_online:availability_0` | `L` ×30 | 10 |
| `router_online:availability_1` | `L` ×30 | 10 |
| `router_online:availability_2` | `L`×13, **`N` at cases 13–14**, `L` ×15 | 8 |
| every `router_cold:*`, `router_warm_prior:*`, baseline | `L` ×30 | 10 |

No arm was exhausted. Failover to Nemotron served every outage case, so the price of each
wasted attempt here is one refused call, near-instant in this run.

**Reading it.**

- **The live answer agrees with §9.** At the shipped `availability: 0.2` the online router
  sent the first attempt of **every** outage case to the dead endpoint, the same chains as
  weight 0. The live run has one seed and one real failure shape (an immediate connection
  drop). §9 has 30 simulated runs with a designed 1000 ms failure. They agree, and the formula
  predicts the result: the dead endpoint's score lead is mostly cost (4× cheaper), and a term
  of weight `w` can take back at most `w`.
- **Even `w = 1` moved nothing here, and `w = 2` moved two cases.** At `w = 2` the router
  switched after three consecutive failures (cases 10–12), then went back to Llama from case
  15 while the outage was still on. The mechanism is **not traced**: once traffic leaves
  Llama it gets no new observations, and its availability is an EWMA that also carries its
  earlier 10 successes and decays back toward 1.0 on wall-clock time. How those interact at
  30 s per case was not instrumented. Either way, no weight tried here kept the dead
  endpoint out for the whole outage.
- **What this does not show:** a slow failure (a connect timeout or a hung server, where each
  wasted attempt costs seconds), several seeds, or a real price difference (both endpoints
  are really $0; the prices are illustrative).

**Consequence.** B45 now has both a simulated and a live answer, and they agree: raising
`weights.availability` does not keep traffic off a dead endpoint without overriding genuine
cost differences. The recommendation of §9 stands unchanged: keep 0.2 as a ranking
preference, and take a dead endpoint out of rotation with a **step-7 circuit breaker** (not
built) or an operator disable. Changing the weight, or building the breaker, is the owner's
decision. `config/routing.yaml` is untouched.

### 11.3 Reproducing §11

```bash
# on the Spark, in an export of 5da06a4 (git archive; the Spark cannot git fetch)
python3 -c "import json; rows=[json.loads(l) for l in open('eval/suites/outage.jsonl')]; \
  json.dump([r['messages'][-1]['content'] for r in rows[10:20]], open('down.json','w'))"
nohup python3 scripts/outage_proxy.py 11500 http://127.0.0.1:11434 down.json proxy.log &
docker run --rm --user $(id -u):$(id -g) -e HOME=/tmp --network host \
  --entrypoint /app/.venv/bin/python -e PYTHONPATH=/bench -e SP_ONPREM_API_KEY \
  -v $PWD:/bench -w /bench routeforge-spark-api scripts/feasible_bench.py \
  --suite eval/suites/outage.jsonl --endpoints eval/endpoints/spark-outage-live.yaml \
  --variants eval/variants/b45.yaml --live --yes --seed 0 --seconds-per-case 30
# §11.1: --suite eval/suites/starter.jsonl --endpoints eval/endpoints/spark-live.yaml, no variants
```

---

## 12. The step-7 circuit breaker, measured (2026-09-23)

§9 and §11 answered B45's weight question: `weights.availability` is a bounded ranking
preference, and no weight tried kept a dead endpoint out of rotation without also
overriding real cost differences. Both recommended a step-7 circuit breaker instead. This
section records the breaker as built on `feat/endpoint-circuit-breaker` (code at `ef9315f`)
and its simulated measurement on the §9 outage scenario. Design:
`docs/superpowers/specs/2026-09-23-endpoint-circuit-breaker-design.md`, amended on
2026-09-23 where the build departed from it (§12.1). **Every number here is simulated.**
There is no live breaker run.

### 12.1 What was built

**State machine** (`app/gateway/breaker.py`, one `CircuitBreaker` per API process, state per
model name):

```
closed    --failure_threshold consecutive unhealth failures--> open       (cooldown_s)
open      --cooldown elapsed-----------------------------------> half_open
half_open --first reported success-----------------------------> closed     (count and cooldown reset)
half_open --first reported failure-----------------------------> open       (cooldown doubled, capped at max_cooldown_s)
```

- **What counts.** Only failures for which `counts_as_endpoint_unhealth(exc)` is true: no-status
  transport errors, 5xx and 429. A 4xx, including context overflow, never trips a circuit (the
  B36 rule the availability signal already uses). *(Final-review fix wave, 2026-09-23.)* Nor
  does a local programming error (`TypeError`, `AttributeError`, `NameError`, `KeyError`,
  `IndexError`, `AssertionError`, `NotImplementedError`, `ValueError` incl. `JSONDecodeError`):
  the breaker is shared by every tenant, and a bug on our side must not open a circuit for
  everyone. The bench's `SimulatedEndpointFailure` is a plain `Exception` and still counts.
  Any success while closed resets the count.
- **Evidence-based half-open.** Before the cooldown elapses, a reported success or failure
  changes nothing: no close, no reopen, no change to the count or the cooldown. Once it has
  elapsed, the model is admitted to **every** request, and the first result anyone reports
  decides: a success closes the circuit, a failure reopens it with the cooldown doubled.
- **No single-trial lease.** The spec's one-request trial lease and `trial_timeout_s` were
  removed. The lease was granted inside `open_set()`, which runs before policy and scoring, so a
  request that could never use the model (out of the app's scope, wrong tier, or simply
  outscored by a cheaper peer) consumed it, and a recovered endpoint stayed excluded for a
  further `trial_timeout_s` per such request (the Task 4 review reproduced three eligible
  requests diverted this way). **Accepted cost:** while a circuit is half-open, concurrent
  in-flight requests may each spend one attempt on a still-dead endpoint. Failover serves them
  (a streamed request has no failover and fails that one call).
- **Where it applies: `feasible_set` stage 7, the last stage** (`circuit_open`, "excluded ·
  circuit open"). The spec placed it right after `prior_failure`. Running it after every policy
  stage (app scope, pinned model, registry gates) means every policy reason wins over it, so
  `circuit_open` only ever labels a candidate that policy fully admitted.
- **The breaker never changes the target tier** *(final-review fix wave, 2026-09-23)*. As first
  built, open circuits were removed before `Router._select` chose the tier, so the clamp to the
  best allowed tier pulled the target down to whatever tier was still closed. The review probe:
  a high-risk request with cloud allowed and `bedrock-frontier` open was served on **mid**
  ("tier mid (target mid)", no bypass); a medium-risk `onprem_only` request with its on-prem mid
  open, and an escalation to mid with mid open, were served on **cheap**. That silently undercut
  the risk floor and the escalation (CLAUDE.md rule 8). Now the targets, the clamp, the tier pick
  and the budget fallback are computed from the **policy**-eligible set, and open circuits are
  dropped only among the chosen tier's final (post-budget) candidates, still labelled by stage 7.
- **Within-tier bypass** (`Router._select`). If every final candidate in the target tier is
  open, the router ranks them all anyway, sets `circuit_bypass: true` and appends
  `"; circuit bypass: every candidate in the target tier is open"` to the reason. It never steps
  to another tier to find a closed model. The bypass can only restore candidates policy admitted,
  in the tier policy chose: an `onprem_only` request whose on-prem models are all open still
  never reaches cloud. So the breaker never makes a request worse off than no breaker: it only
  removes an open model when a closed same-tier peer can take its place.
  **Re-checked on the bench:** the outage scenario's two endpoints share one tier, so the fix
  changes no §12 number; seed 0 at 1 s/case re-run after the fix gives `router_online:default`
  `wasted_attempts` 0.1 (3/30), as in §12.4.
- **Gateway wiring.** `Gateway.run` and `Gateway.run_stream` read `open_set()` fresh on every
  `Router.select`, including each failover and escalation re-select, and report every attempt's
  outcome to the breaker.
- **Per-process, boots closed.** Nothing is shared across replicas and nothing is replayed at
  boot. Production reads `time.monotonic()`.
- **Config** (`config/routing.yaml`, linted): `breaker: {enabled: true, failure_threshold: 3,
  cooldown_s: 30, max_cooldown_s: 300}`. `enabled: false` makes `open_set()` always empty, which
  is exactly the pre-breaker behaviour.

### 12.2 The bench arms, and the case clock

- **`router_online:<variant>`** (and the unvaried `router_online`) now carries its own
  `CircuitBreaker`, because `routing.breaker.enabled` is true. It is fed only by the arm's own
  earlier attempts, the same leak guards apply, and `open_set()` is read fresh on every hop of
  the attempt chain, as in the gateway. This is the reported arm. `router_online` equals
  `router_online:default` in all 30 runs below.
- **`router_online:breaker_off`** is the control: the base weights, the same online arm, the
  breaker disabled. The runner adds it whenever the base config has the breaker on.
- **The breaker runs on the case clock** (§9.4): case `k` happens at `k × seconds_per_case`.
  So `seconds_per_case` decides how many cases a cooldown covers:
  - at **1 s/case**, the first 30 s cooldown covers 30 cases, longer than the rest of the suite;
  - at **30 s/case**, the first cooldown ends exactly at the next case, which is therefore
    already half-open. The first cooldown never excludes anything; only the doubled ones
    (60, 120, 240 s) span whole case gaps;
  - at **300 s/case** (= `max_cooldown_s`), no cooldown can outlast one case gap, so the breaker
    never excludes anything and the two arms are identical by construction
    (`TestBreakerCaseClockInteraction`).

  This is a property of the bench, not of production. In production a cooldown is wall-clock
  time, and it covers however many requests arrive in it.

### 12.3 Stamp

```
suite            eval/suites/outage.jsonl        30 cases
suite_hash       78e1be2bc1b234ba8ea418f83eeb890fc700dae75179425facc194f6a4dd9969
endpoints        eval/endpoints/outage-sim.yaml  (fast-cheap, steady; unchanged from §9.5)
endpoints_hash   bdb74978978dbe2ef956e7e7a447bec80fbcc9d59525bd5b74f9f6bfe465f7d6
variants         eval/variants/b45.yaml
seeds            0–9        seconds_per_case  1, 30, 300      attempt_cap 3
breaker          enabled, failure_threshold 3, cooldown_s 30, max_cooldown_s 300
git_sha          ef9315f9713a3c382c3347df0f6fbe0406c0efd7
base_routing_policy_version  b56a629b0417 (was a4bf5708d2db: routing.yaml gained the breaker block)
registry_version abeacbc653b5
mode             simulated  SP_PII_DETECTION=regex
result           30/30 runs exit 0; violations_total 0 in every run
```

`metrics.json` md5, first 8 hex digits, seeds 0–9 in order (they differ from §9.5 because the
new arm is in the file):

| `seconds_per_case` | seed 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `0f42055d` | `6274ff12` | `4a34aa67` | `2f1c880a` | `aed0246a` | `27b348de` | `8f571d0d` | `e52c1f70` | `6aaef368` | `a438db7e` |
| 30 | `d9ceb535` | `91255545` | `6b7e415c` | `c0e66a52` | `c499d399` | `62519057` | `c8f2000b` | `d077041d` | `188e925f` | `91eb332c` |
| 300 | `3f574feb` | `02d9dc86` | `92ea52a9` | `353d70ff` | `cea99e9e` | `10dc293c` | `820ad1e0` | `4a83a2ea` | `200c544a` | `b4e9a3b6` |

**Control check.** `router_online:breaker_off` reproduces §9.5's weight-0.2 row exactly at every
spacing (10.5 wasted per run, mean chosen cost 2.251e-06, latency regret 326.1 ms, cost regret
0), so the control is the pre-breaker router.

### 12.4 Result

Mean over seeds 0–9 [min–max]. "Wasted per run" is `wasted_attempts` × 30. "Dead firsts" is the
number of outage cases (10–19) whose first attempt went to the dead `fast-cheap`. "Back on
`fast-cheap`" is the first case after the outage whose first attempt is `fast-cheap` again.
Mean chosen cost is against the control at the same spacing.

| s/case | arm | wasted per run | mean chosen cost (USD) | cost regret (USD) | latency regret (ms) | pass rate | dead firsts | back on `fast-cheap` |
|---|---|---|---|---|---|---|---|---|
| 1 | **breaker on** | **4.4 [3–6]** | 3.392e-06 (**+51%**) | 1.07e-06 | **198.7** [147.2–234.5] | 0.943 | **3** [3–3] | **never** (not by case 29) |
| 1 | breaker off | 10.5 [10–11] | 2.251e-06 | 0 | 326.1 [285.7–344.8] | 0.950 | 10 | case 20 |
| 30 | **breaker on** | **7.0 [6–8]** | 3.122e-06 (**+39%**) | 8.4e-07 | **264.9** [220.0–289.7] | 0.950 | **6** [6–6] | case 27 [27–27] |
| 30 | breaker off | 10.5 [10–11] | 2.251e-06 | 0 | 326.1 [285.7–344.8] | 0.950 | 10 | case 20 |
| 300 | breaker on | 10.5 [10–11] | 2.251e-06 | 0 | 326.1 [285.7–344.8] | 0.950 | 10 | case 20 |
| 300 | breaker off | 10.5 [10–11] | 2.251e-06 | 0 | 326.1 [285.7–344.8] | 0.950 | 10 | case 20 |

`exhausted_rate` is the same for both arms at every spacing (mean 0.017): the five cases where
`steady` flaked inside the outage exhaust every strategy, as in §9.5.

**Where the difference falls** (breaker on − off, summed over the run, mean over seeds
[min–max]; from `choices.jsonl`):

| s/case | cases 0–9 | outage, cases 10–19 | after recovery, cases 20–29 |
|---|---|---|---|
| 1 | 0 | cost 0; latency **−6,700 ms** [−7,000 to −6,000] | cost **+3.42e-05 USD**; latency **+3,221 ms** [2,784–3,861] |
| 30 | 0 | cost 0; latency **−3,800 ms** [−4,000 to −3,000] | cost **+2.61e-05 USD**; latency **+2,234 ms** [1,950–2,556] |
| 300 | 0 | 0 (chains identical at 10/10 seeds) | 0 |

**Seed 0, first attempts per case** (`F` = `fast-cheap`, `S` = `steady`, `F+S` = a wasted
attempt on `fast-cheap`, then served by `steady`; cases 0–9 are `F` in every row):

| arm | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20–26 | 27 | 28 | 29 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| on, 30 s/case | F+S | F+S | F+S | F+S | S | F+S | S | S | S | F+S | S | F | F | F |
| on, 1 s/case | F+S | F+S | F+S | S | S | S | S | S | S | S | S | S | S | S |
| off (any spacing), on at 300 s | F+S | F+S | F+S | F+S | F+S | F+S | F+S | F+S | F+S | F+S | F | F | F | F |

At 30 s/case the sequence is the case clock at work. Cases 10–12 trip the circuit (30 s). Case
13 lands exactly at the end of that cooldown, is admitted half-open, fails, and reopens for 60 s.
Case 14 is excluded. Case 15 is the next trial, which fails (120 s). Cases 16–18 are excluded.
Case 19 fails its trial (240 s), which excludes cases 20–26 although `fast-cheap` recovered at
case 20. Case 27 is a successful trial and closes the circuit. The same sequence holds at every
seed.

### 12.5 Reading it

- **At 1 s/case the breaker does what the spec predicted during the outage.** Exactly
  `failure_threshold` (3) outage cases pay a wasted attempt, instead of all 10, at every seed.
  No trial fits inside the suite, so there is no "one per failed trial" term. Latency regret
  falls from 326 to 199 ms (−39%).
- **At 30 s/case the benefit is partial:** 6 dead first attempts instead of 10, 3 to trip it and
  3 failed trials. The first cooldown ends exactly at the next case and so excludes nothing.
  Latency regret falls from 326 to 265 ms (−19%).
- **At 300 s/case there is no effect at all**, at any seed. This is the case clock, not the
  breaker: no cooldown up to `max_cooldown_s` outlasts a 300 s case gap. Production uses
  wall-clock time, so this null says nothing about production at a low request rate beyond
  what it literally is: requests 300 s apart never meet an open circuit.
- **The cost is all after recovery, as it was for the larger weights in §9.5.** Inside the
  outage the extra cost is exactly 0, because a failed attempt costs 0 and failover already
  served those cases on `steady`. After the outage the breaker keeps the recovered, 4× cheaper
  endpoint out for the rest of its current cooldown: for the whole of cases 20–29 at 1 s/case
  (the 30 s cooldown is 30 cases long), and for cases 20–26 at 30 s/case (the 240 s cooldown
  set by the failed trial at case 19). That costs +51% and +39% mean chosen cost, and gives back
  about 300 ms per case served on `steady` (+3.2 s and +2.2 s per run), which the latency-regret
  figures already include. Net latency is still better, because the saved failures (1000 ms
  each, a designed value) outweigh it.
- **A successful bypass does not close a circuit early.** At seed 6, 1 s/case, `steady` flaked
  at cases 25, 28 and 29. `fast-cheap` was open, so the failover re-select found no candidate,
  bypassed, and `fast-cheap` served each case. Those successes arrived before the cooldown had
  elapsed, so under the evidence rule they changed nothing, and the next case went to `steady`
  again. This is the designed behaviour (a result before the cooldown elapses is not trial
  evidence). It is also part of the post-recovery cost. The same flakes are why the breaker-on
  arm has wasted attempts outside the outage (0.6 per run at 1 s/case, 0.3 at 30 s/case): it
  serves more traffic from `steady`, so it meets more of `steady`'s 5% error rate.
- **Against the weights of §9.5.** At 1 s/case the breaker wastes fewer attempts than
  `availability: 2.0` without a breaker (4.4 vs 5.5 per run) with lower latency regret (199 vs
  217 ms) and higher cost (+51% vs +39%). At 30 s/case it is behind `w = 2` on all three (7.0 vs
  5.7 wasted, 265 vs 221 ms, +39% vs +35%). What the breaker has that a weight does not is
  structural and not measured here: it does nothing at all until an endpoint fails three
  times in a row, so it never overrides a cost difference between two healthy endpoints, which
  is what a weight of 2.0 would do on every request. The `router_online:<variant>` arms now
  carry the breaker too, so §9.5's variant numbers are no longer reproduced by a current run;
  they remain the no-breaker reference.
- **Pass-rate differences** (0.943 vs 0.950 at 1 s/case) come from the declared simulated
  qualities (`fast-cheap` FAQ 0.97, `steady` 0.95), as in §9.5. They are an input (§6.1).

### 12.6 Limits: what this does NOT show

- **The §9.6 limits all apply.** One designed scenario: two endpoints, one price ratio (0.25), one
  outage span, one suite order, simulated failures, 30 cases, 10 seeds.
- **The saving scales with a designed value.** Each avoided attempt saves the declared 1000 ms
  failure latency. A fast refusal, like the live connection drop in §11.2, would make the saving
  close to 0 while the post-recovery cost stays. A slow failure (a connect timeout, a hung
  server) would make it larger.
- **Case clock, not wall clock.** The three spacings produce three different stories (full,
  partial, none) for one mechanism. Real traffic is bursty. In production the exclusion lasts a
  wall-clock cooldown (30–300 s), and the post-recovery cost is whatever traffic arrives in the
  remainder of it.
- **One process, serial requests.** The bench models one replica with one request at a time.
  With N API replicas each trips on its own failures, so an outage can cost up to N ×
  `failure_threshold` wasted attempts before every replica has tripped. The accepted cost of
  the no-lease half-open (concurrent in-flight requests each spending one attempt on a
  still-dead endpoint) cannot occur in a serial bench and is unmeasured.
- **No streaming in the bench.** The case the spec names as benefiting most (a streamed request,
  which has no failover) is covered by unit tests (`test_run_stream_*`), not measured.
- **One configuration.** `failure_threshold`, `cooldown_s` and `max_cooldown_s` were not swept.
  The post-recovery cost at 30 s/case is set by the 240 s cooldown the third failed trial
  produced. A shorter cap would shorten it.
- **No live run.** A `--live` re-run with `scripts/outage_proxy.py` (§11.3) would exercise the
  breaker through the real gateway. It has not been made.

### 12.7 Invariants the breaker added, and the test that pins each

| Invariant | Pinned by | Falsified? |
|---|---|---|
| Opens after `failure_threshold` consecutive unhealth failures; any success resets; models are independent | `tests/test_breaker.py::test_opens_after_threshold_consecutive_failures`, `test_success_resets_the_count`, `test_models_are_independent` | n/a |
| A 4xx never trips; 5xx, 429 and transport errors do (and open it, after which the next request makes no wasted attempt); a local programming error neither trips it nor moves availability | `tests/test_gateway_breaker.py::test_gateway_failures_trip_the_breaker[500\|429\|no-status-transport]`, `test_local_programming_errors_do_not_trip_the_breaker_or_lower_availability`, `test_4xx_does_not_trip_the_breaker`; `tests/routing/test_signal_wiring.py::test_local_programming_errors_are_not_endpoint_unhealth`; `tests/eval/test_strategies.py::TestOnlineArmBreaker::test_breaker_true_ignores_non_unhealth_failures` | n/a |
| Before the cooldown elapses, no result changes state, count or cooldown | `test_breaker.py::test_success_mid_cooldown_does_not_close`, `test_failure_mid_cooldown_does_not_change_cooldown_or_count` | **Yes**: the Task 2 review found a mid-cooldown success closing the circuit (fixed in `2d0d029`) |
| After the cooldown, the first success closes and the first failure reopens with the cooldown doubled and capped | `test_breaker.py::test_trial_success_closes`, `test_trial_failure_reopens_with_doubled_cooldown_capped`, `test_success_after_cooldown_closes_without_lease`, `test_failure_after_cooldown_reopens_without_lease` | n/a |
| Half-open admits every caller; an ineligible request cannot starve a recovered endpoint | `test_breaker.py::test_half_open_admits_every_caller_no_lease`, `test_concurrent_callers_all_admitted_while_still_dead`; `test_gateway_breaker.py::test_ineligible_request_does_not_starve_a_recovered_endpoint` | **Yes**: the Task 4 review probe diverted three eligible requests under the lease design (lease removed in `bb97af8`) |
| `half_open` is metered and logged once per open period | `test_breaker.py::test_half_open_metric_emitted_exactly_once_per_open_period`, `test_half_open_metric_also_dedupes_across_record_calls` | n/a |
| An open model is rejected with `circuit_open`; every policy reason wins over it | `tests/test_feasible_set.py::test_open_circuit_candidate_is_rejected_as_circuit_open`, `test_placement_excluded_and_open_circuit_reads_residency_not_circuit_open`; `tests/test_router_breaker.py::test_prior_failure_plus_open_circuits_bypass_keeps_prior_failure` | n/a |
| All open → bypass, marked; the bypass never widens placement; a policy-empty set still raises | `test_router_breaker.py::test_all_open_bypasses_and_marks_it`, `test_bypass_never_widens_placement`, `test_no_eligible_for_policy_reasons_still_raises`; `test_gateway_breaker.py::test_all_open_bypass_is_on_the_record` | n/a |
| The breaker never changes the target tier (risk floor, escalation, budget fallback); the bypass stays within the target tier; an empty open set reproduces today's decisions | `test_router_breaker.py::test_high_risk_open_frontier_keeps_the_frontier_target`, `test_medium_risk_onprem_only_open_mid_is_not_lowered_to_cheap`, `test_medium_risk_open_mid_prefers_a_closed_mid_peer`, `test_escalation_to_open_mid_is_not_lowered_to_cheap`, `test_open_cheap_peer_with_closed_peer_keeps_target_and_labels`, `test_budget_fallback_tier_is_computed_before_circuits`, `test_budget_fallback_prefers_closed_peer_in_fallback_tier`, `test_empty_open_set_matches_no_argument` | **Yes**: the final review's probe served a high-risk request on mid and a medium-risk / escalated one on cheap (fixed in `75e5ae4`) |
| The gateway excludes an open endpoint up front (no wasted attempt), in `run` and `run_stream` | `test_gateway_breaker.py::test_open_circuit_saves_the_wasted_attempt`, `test_run_stream_open_circuit_excludes_the_model`, `test_run_stream_success_records_on_the_breaker`, `test_run_stream_failure_records_on_the_breaker`, `test_run_stream_early_close_records_nothing` | n/a |
| `enabled: false` (or no breaker) is the pre-breaker behaviour | `test_breaker.py::test_disabled_never_opens`; `test_gateway_breaker.py::test_no_breaker_is_todays_behaviour`; `test_feasible_set.py::test_open_circuits_defaults_to_empty_set_no_change`; `TestOnlineArmBreaker::test_breaker_off_kwarg_equals_settings_disabled` | n/a |
| Config lint: positive threshold and cooldown, `max_cooldown_s ≥ cooldown_s` | `tests/test_config_lint.py::test_breaker_config_loads_with_shipped_values`, `test_bad_breaker_values_are_caught` | n/a |
| Bench arm: excludes after the threshold, its chain equals the direct `Router.select` chain, and it is a no-op at `seconds_per_case ≥ max_cooldown_s` | `TestOnlineArmBreaker::test_breaker_true_excludes_a_after_failure_threshold_failures`, `test_choose_equals_router_chain_with_breaker_open_circuits`; `TestBreakerCaseClockInteraction::test_seconds_per_case_at_max_cooldown_is_a_documented_no_op` | n/a |
| Outage scenario: breaker-on wastes no more attempts than `breaker_off` (direction only) | `tests/eval/test_outage_scenario.py::test_outage_run_is_deterministic_clean_and_moves_the_online_variants` | n/a (asserts direction, never magnitude) |

At `ef9315f`, `tests/eval` gives 279 passed; `tests/eval` plus the five breaker-related test
files give 366 passed.

### 12.8 Reproducing §12

```bash
git checkout feat/endpoint-circuit-breaker      # code at ef9315f
for SPC in 1 30 300; do for SEED in 0 1 2 3 4 5 6 7 8 9; do
  LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
    uv run python scripts/feasible_bench.py \
      --suite eval/suites/outage.jsonl \
      --endpoints eval/endpoints/outage-sim.yaml \
      --variants eval/variants/b45.yaml \
      --seed $SEED --seconds-per-case $SPC --out /tmp/claude-breaker/s$SEED-$SPC
done; done
md5sum /tmp/claude-breaker/s0-30/metrics.json   # d9ceb5354738c22db6691e4135704e13 at ef9315f
```

Compare `router_online:default` with `router_online:breaker_off` in each `metrics.json`
(`strategies`), and read first attempts from `choices.jsonl` (`attempts[0]` per `strategy`,
`case_id`). Each run takes about 3 s. Write the output outside the repository; run output is
not committed.

---

## 13. P2b-2: production outcome records as a dataset (2026-09-23)

§1 named the obstacle this programme started from: the feasible oracle needs outcomes
for every eligible endpoint of a request, and production observes only the one it chose.
P2b-1 worked around that offline, by sweeping every eligible endpoint of a synthetic
suite. §10 and §11.1 then showed what a missing quality signal costs live: Llama fails
every claim summary, and the router cannot know it, because every onboarded model scores
at its tier-default quality. B51's peer prior removed the latency fiction that made this
worse; it did not add a quality signal.

P2b-2 is the first half of the durable fix. RouteForge now **records what happened on
each production request**, per workload, so that a later slice can learn from it. This
section records the dataset as built on `feat/routing-outcome-records` (code at
`e14f8e5`, plus the final-review fixes of 2026-09-23). Design: `docs/superpowers/specs/2026-09-23-routing-outcome-records-design.md`,
amended on 2026-09-23 where the build departed from it. Field-level reference:
[`docs/reference/routing-outcomes.md`](../reference/routing-outcomes.md). **This section
contains no results.** Nothing has been deployed, so no production record exists yet.

### 13.1 The record model

One record per request that reached a routing decision. Writing $x$ for what is known
before routing, $E$ for the candidate set, $a$ for the served endpoint and $y$ for what
was observed:

- **Context $x$.** Everything the gate and compression know, content-free:
  `sensitivity`, `risk_class`, `onprem_only`, `detected_entity_types` (types, never
  values), `complexity` (post-compression tokens / 2000), `prompt_tokens_bucket`, and
  `workload` (an optional client-supplied slug; §13.4).
- **Versions $v$.** `app_policy_version`, `endpoint_registry_version`,
  `routing_policy_version`. Together they identify the policy, the endpoint pool and the
  router configuration the decision was made under.
- **Decision.** `tier_target`; `eligible` (the names scored); `rejected` (`{name, code}`
  for everything excluded); `scoring` (a `RankResult.to_record()`); `circuit_bypass`;
  `cache_hit`. All four decision fields describe the request's **first routing
  decision**: the first trace entry that is a router hop or a failed gateway hop carrying
  `scoring`, i.e. the decision that picked the first attempt. After a failover that is the
  failed hop; after a confidence escalation it is the first, unserved router hop. A failed
  hop carries its own `tier_target` and `circuit_bypass` (2026-09-23), so `tier_target` is
  never borrowed from a later hop, and `circuit_bypass` is true if any decision hop bypassed
  an open circuit. The scoring record is enough to recompute that **first** ranking offline. It is not the
  record of the hop that served, which can differ.
- **Action $a$.** `chosen` (the **served** model: the last router hop that chose one), and
  `attempts` (every model tried, in order, including failed hops). So the record pairs
  the router's first choice with the request's final outcome, and `attempts` gives the
  path between them.
- **Outcome $y$.** `status` (`ok`, `backend_failed`, `verify_failed`, `no_eligible`,
  `context_overflow`), `cost_usd`, `latency_ms`, token counts, the step-9 verification
  fields and `verify_mode`, and zero or more **feedback** rows (`score_name`, a numeric or
  categorical value, `source` = `app` or `admin`).

Two quality signals exist, both labelled by source. **Step-9 verification** is recorded on
every served request, but it measures leakage and verifiability, not whether the answer
was good. **Feedback** is the only direct quality label, and it exists only where a client
or an admin chose to send it.

What is *not* in the model, by design (spec §3): no propensity, no LLM-as-judge score,
no counterfactual outcome, and no request content.

**Definitions the analysis must respect** (full list in the reference doc, §3):

- `latency_ms` has two definitions. `ok` records sum the trace hops; failure records
  measure wall-clock from the start of the request handler, including gate time. Never pool
  them.
- `eligible` is the **scored** set of the target tier, not the whole feasible set. The
  policy-feasible set of the first hop is, by inference from how codes are assigned (not
  pinned by a test), `eligible` plus every `rejected` entry whose `code` is null (the
  router's own tier, budget and lower-score filters) or `circuit_open`.
- A semantic-cache hit is `ok` with `cache_hit: true` and `chosen: null`: its trace has no
  router hop, so the model that produced the cached answer is not recorded. The report
  gives these records their own `(cache)` bucket.
- A `verify_failed` record is attributed to the served attempt: step 9 refused a response a
  model had already produced, so `chosen`, `attempts`, `scoring`, the cost, the tokens and
  `verify_mode: failed` are all recorded, read from a reason-free copy of the trace.
- `request_id` is `req-` plus a full UUID4 (128 random bits). Feedback joins on
  `(application_id, request_id)`.
- Some requests write no record at all (reference §3.1): 401, 429, gate errors,
  pinned-model rejections, cancellations, unhandled 500s, and the rare real-token stream
  that ends without a final result. So "records" is not "requests".

### 13.2 Invariants, and the test that pins each

| Invariant | Pinned by | Falsified? |
|---|---|---|
| Content-free: no column name suggests text; a request carrying an SSN and an e-mail address leaves neither in its record; the builder copies no text; `rejected` keeps `{name, code}` only; the gate context holds no prompt text | `tests/test_outcome_store.py::test_schema_is_content_free`; `tests/test_outcome_hooks.py::test_outcome_row_carries_no_request_content`; `tests/test_outcome_builder.py::test_build_ok_outcome_is_content_free`, `test_build_failure_outcome_no_eligible_stores_only_name_and_code`; `tests/test_outcome_ctx_pipeline.py::test_outcome_ctx_has_no_prompt_text_even_with_ssn` | n/a. The end-to-end PII test now covers failure records too (`test_verify_failed_row_carries_no_request_content`, `test_no_eligible_row_carries_no_request_content`) |
| Feedback text (`comment`, `corrected_output`) is never stored locally; a correction is still countable | `tests/test_outcome_feedback.py::test_comment_and_corrected_output_text_never_stored_locally` | n/a |
| Feedback is app-scoped: another app's request is indistinguishable from a missing one, and writes nothing | `tests/test_outcome_feedback.py::test_cross_app_feedback_is_refused_and_writes_no_row`; `tests/test_outcome_store.py::test_get_outcome_is_app_scoped`, `test_find_outcome_by_trace_id_and_request_id_is_app_scoped` | n/a |
| Versions are stamped on every record, served or failed | `tests/test_outcome_hooks.py::test_plain_request_writes_one_ok_row` (all three versions); `test_backend_failure_writes_backend_failed_row` (registry version only; the other two reach failure records by the same builder arguments, untested end to end) | n/a |
| The report never pools across `endpoint_registry_version` | `tests/eval/test_outcomes_report.py::test_mixed_registry_versions_produce_separate_groups` | n/a |
| The report never prints regret, and opens with the served-arm caveat verbatim | `tests/eval/test_outcomes_report.py::test_report_never_mentions_regret`, `test_report_starts_with_the_caveat_verbatim`, `test_report_of_an_empty_export_still_starts_with_the_caveat` | n/a |
| Mean cost and latency percentiles are over `ok` records only | `tests/eval/test_outcomes_report.py::test_summarize_chat_ep_mid_bucket_has_the_failover_and_the_verify_failure`, `test_summarize_no_eligible_row_lands_in_unchosen_bucket_with_no_latency` | **Yes**: the Task 7 review found failed records (cost 0) diluting the mean cost (fixed in `e14f8e5`) |
| A `verify_failed` record names the served model, its attempts and `verify_mode: failed`; the stashed trace carries no `reason` | `tests/test_outcome_hooks.py::test_verify_failed_row_is_attributed_to_the_served_model`, `test_keepalive_verify_failed_row_is_attributed`; `tests/test_outcome_builder.py::test_build_failure_outcome_verify_failed_reads_the_stashed_trace` | **Yes**: the final review found `verify_failed` records carried no attribution at all (`chosen` null, `attempts` empty), because the pipeline raised after the gateway served; the eval fixture's attributed verify_failed line was one the code could not produce (fixed 2026-09-23) |
| Decision fields describe the first routing decision; `chosen` the served model; the same after failover and after escalation | `tests/test_outcome_builder.py::test_build_ok_outcome_failover_first_decision_is_the_failed_hop`, `test_build_ok_outcome_escalation_first_decision_describes_eligible_scoring_tier`; `tests/test_outcome_hooks.py::test_failover_row_first_decision_is_the_failed_attempt` | **Yes**: the final review found the hop differed by path (after escalation the first router hop, after failover the served hop, dropping the failed attempt's scoring) (fixed 2026-09-23) |
| Request ids do not collide; feedback never attaches across apps | `tests/test_outcome_hooks.py::test_request_id_is_full_uuid_hex`; `tests/test_outcome_export.py::test_export_never_attaches_another_apps_feedback`; `tests/test_outcome_store.py::test_feedback_for_is_scoped_by_application_id` | **Yes**: the final review found 32-bit ids as a 90-day primary key and a feedback join on `request_id` alone (fixed 2026-09-23) |
| `verify_mode` distinguishes the real streaming modes | `tests/test_outcome_hooks.py::test_stream_modes_record_verify_mode`; `tests/test_outcome_builder.py::test_build_ok_outcome_verify_waived_stream`, `test_build_ok_outcome_verify_incremental` | **Yes**: the Task 2 review found that checking `required` first recorded every real `stream_unverified` and `incremental_verify` request as `not_required`, because both always carry `required: false` (fixed in `6d3f542`) |
| Recording never changes routing or the response; switched off, nothing is written | `tests/test_outcome_hooks.py::test_outcomes_disabled_writes_no_row_and_same_response`, `test_outcomes_disabled_writes_no_failure_row`; `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` | n/a |
| A failed write never fails the request or its metering | `tests/test_outcome_hooks.py::test_outcome_write_failure_is_swallowed` | n/a |
| A local feedback-store failure never changes the status or `stored`, and never blocks the Langfuse post | `tests/test_outcome_feedback.py::test_feedback_find_outcome_failure_does_not_block_langfuse_post`, `test_feedback_write_feedback_failure_on_corrected_output_does_not_block_second_post`, and the two admin equivalents | **Yes**: the Task 5 review found an unguarded local lookup turning a DB error into a bare 500 and skipping the Langfuse post (fixed in `afd5045`) |
| An invalid workload tag is dropped, never rejected; the header beats `metadata`; `metadata` is not forwarded upstream | `tests/test_outcome_hooks.py::test_invalid_workload_header_still_serves`, `test_metadata_workload_is_stored_and_not_forwarded_upstream`; `tests/test_outcome_builder.py::test_parse_workload_header_beats_metadata` | n/a |
| Retention deletes old records from both tables and runs independently of the observation prune | `tests/test_outcome_store.py::test_prune_outcomes_deletes_old_rows_from_both_tables`; `tests/routing/test_signal_wiring.py::test_the_loop_also_prunes_outcomes_hourly_when_enabled`, `test_an_outcomes_prune_failure_does_not_stop_the_flusher` | n/a |

### 13.3 What the dataset can and cannot support

**Can support:**

- **Descriptive rates per workload**: served-ok, `no_eligible`, `backend_failed`,
  `context_overflow` and verification-failure rates, multi-attempt rate (failover or
  escalation; the record does not tell them apart), cost and latency of
  served requests, and feedback coverage. The empty-set rate is the production
  counterpart of the bench's §5.7 run.
- **Auditing the router against its own record.** The scoring record lets an analyst
  recompute each first-hop ranking offline, and the versions tell them which configuration
  to recompute it under.
- **Change detection across versions.** Records never mix across registry versions in the
  report, so a before/after comparison around an onboarding or a routing change is
  possible. It is still confounded by any change in the traffic mix over the same period.
- **Quality labels for the served endpoint**, where feedback exists. This is the signal
  §10 and §11.1 lacked: "Llama fails claim summaries" becomes visible as a low `quality` or
  `route_correct` mean in the `(claims.summary, llama)` cell, once clients tag and score.

**Cannot support:**

- **Counterfactuals.** Each record has the outcome of the served endpoint only. What any
  other eligible endpoint would have done is not observed, so oracle regret is not
  computable from these records, and the report never prints it. FeasibleRouteBench
  remains the only source of counterfactual outcomes.
- **Unconfounded endpoint comparisons.** The router decides which requests each endpoint
  sees, by tier, price and signal. Two endpoints' per-cell means come from different
  request mixes, even within one `(workload, chosen)` cell, because the router also
  conditions on `complexity`, `risk_class` and residency. Differences are descriptive, not
  causal.
- **Off-policy evaluation.** The router is deterministic: for a given context and state it
  picks one endpoint with probability 1 and every other with probability 0. Inverse
  propensity weighting and doubly robust estimators need every action a candidate policy
  would take to have been taken with non-zero probability. A deterministic logger gives no
  such overlap, so no estimator can score an alternative router from these records alone.
  The records carry no propensities because there are none to record.
- **Quality where nobody labelled.** Feedback is voluntary and sparse, and who labels is
  not random: a client may only score failures. Label coverage is reported per workload so
  this is visible; it is not corrected for. Step-9 verification is present everywhere but
  is not a quality measure.

### 13.4 What P2b-3 needs from it

P2b-3 (learning and exploration bounded by the feasible set) needs, at least:

1. **Propensities, logged at decision time.** Any exploration must record the probability
   with which the served endpoint was chosen and the policy that chose it. That means new
   columns and a schema bump, and it is the prerequisite for off-policy evaluation from
   production data. Exploration must stay inside the feasible set: the eligibility filters
   run before any exploration, so it can never widen placement.
2. **The full feasible set on the record.** `eligible` is the target tier's scored set.
   Exploration across tiers within the feasible set needs the set logged directly, not
   reconstructed from `rejected` codes (§13.1).
3. **A denser quality label.** Voluntary feedback will be sparse and non-randomly missing.
   An LLM-as-judge (out of scope here) or a deterministic per-workload grader is the
   candidate. Whatever it is, it must stay content-free in these tables and be labelled by
   source.
4. **A workload taxonomy clients actually send.** `workload` is optional. Untagged traffic
   can be grouped only by derived features (`prompt_tokens_bucket`, `complexity`,
   `sensitivity`, entity types), which do not say what the task was.
5. **The bench as the check.** Any learned policy should be scored on FeasibleRouteBench
   against the feasible oracle before it serves traffic. The production records say what
   happened; the bench says what would have happened.
6. **The served model on cache hits.** A cache-hit record does not name the model that
   produced the answer (§13.1). A learner that credits endpoints must either exclude cache
   hits or have this added.

### 13.5 Limits: what this does NOT show

- **No data.** The feature is built and tested, not deployed. Every statement in §13.3 is
  about what the records permit, not about anything observed.
- **Deploy prerequisite.** The two new tables need no `ALTER`, but earlier outstanding
  `ALTER`s (`context_window_source` from #64) must be applied first.
- **One extra commit per request.** The outcome write is inline, next to the usage row,
  so it adds one database commit to every routed request. Its latency cost was not
  measured.
- **Failure-record PII** is now covered end to end for `verify_failed` and `no_eligible`
  (§13.2, first row); `backend_failed` and `context_overflow` are covered by the builder
  unit tests and by construction.

## 14. P2b-3: offline, human-approved quality calibration (2026-09-24)

§13.4 named what production learning from the P2b-2 dataset would need, and listed the
online items (propensity logging, the full feasible set on the record, a denser label) as
still missing. P2b-3, as actually built, is narrower and deliberately does not attempt
those: it is a **recommend-then-human-applies** loop over the served-arm dataset §13
already describes, with no exploration and no change to eligibility. Design:
`docs/superpowers/specs/2026-09-24-offline-quality-calibration-design.md` (§12 records
where the build departed from it). Field-level reference:
[`docs/reference/quality-calibration.md`](../reference/quality-calibration.md). Code:
`app/eval/calibration.py` (estimator + replay), `app/apps/calibration_sets.py` (serving-side
validator), `app/routing/quality.py` (resolver), `scripts/quality_recommend.py` (CLI),
`app/api/admin_routes.py` (apply/list/revert). **Not deployed**, so this section — like
§13 — reports what the code does, not a production result.

### 14.1 The model

An outcome record's evidence (§13.1's $y$) is turned into a Beta-Binomial estimate of one
model's quality on one workload, and that estimate is checked against the router's own
past decisions before anything about it can move.

- **Unit and credit.** `(model, workload)` for every non-null workload a model served,
  plus `(model, *)` over all its included rows. Credit goes to the served model
  (`chosen`); cache hits, a null `chosen`, and the three "says nothing about answer
  quality" statuses (`no_eligible`, `backend_failed`, `context_overflow`) are excluded —
  the same exclusion set §13.3 already names as a "cannot support" for endpoint
  comparisons, now enforced in code rather than just noted.
- **Evidence.** At most one `quality` trial and one `route_correct` trial per row (the
  latest by feedback `ts`, so repeated identical feedback cannot narrow a unit's interval
  by volume alone), plus a 0/1 trial from `verify_failed`. A row with no evidence still
  counts toward `n_rows` and `label_coverage`.
- **Prior.** `Beta(k·q0, k·(1−q0))`, `q0` read only from a `published` or `tier_default`
  value in the group's own scoring records — never from a `calibrated` or `customer`
  term's own value, so a calibration can never compound on top of an earlier one. Such a
  term instead carries a content-free `base` (the published/tier_default value underneath
  the override), which is the prior for the next round; without it every unit of a second
  round would be `insufficient`. No qualifying value and no `--priors` entry means the unit
  is `insufficient` by construction, never silently scored against a default.
- **Gate.** A unit is `recommended` only once it has enough trials (`min_labels`, default
  20), a narrow enough 90% interval (`max_ci_width`, default 0.25), and a shift big enough
  to matter (`min_shift`, default 0.02) — all three, not any one.
- **Promotion.** A recommendation is a **content-hashed** file: its `set_id` is a sha256
  over its own canonical content. That hash is an **integrity** check (the server
  recomputes it and refuses a file edited after it was produced), **not** an authenticity
  control — anyone can recompute it for any content. Authorization is the admin session
  that `POST`s the file. An admin reviews a
  **replay** — every logged decision in the recommendation's registry group, re-ranked
  with the recorded weights and terms but the proposed quality substituted in — before
  choosing to apply. Applying writes `model_quality` rows; the router picks them up on its
  next refresh. Nothing in the loop is automatic past that one human action.

### 14.2 Invariants, and the test that pins each

| Invariant | Pinned by |
|---|---|
| A recommendation is deterministic: same export + options → byte-identical `recommendation.json`, sorted keys, 6-dp floats | `tests/eval/test_calibration_estimator.py::test_recommendation_shape_and_determinism` |
| `set_id` is a content hash of the file minus `set_id`, computed identically by the offline and serving implementations | `tests/eval/test_calibration_estimator.py::test_set_id_is_the_specified_hash`, `test_set_id_changes_with_any_unit_score`; `tests/test_quality_calibrations_api.py::test_serving_and_eval_set_id_implementations_agree` |
| Records never pool across `endpoint_registry_version`; the default group is the newest by latest `ts`, not by label | `tests/eval/test_calibration_estimator.py::test_registry_versions_never_pooled`, `test_newest_group_is_latest_ts_not_label` |
| Exclusions (cache hit, null `chosen`, `no_eligible`/`backend_failed`/`context_overflow`) contribute no evidence | `tests/eval/test_calibration_estimator.py::test_exclusions` |
| Only the latest label per `(request, score_name)` counts — repeats don't inflate `n_labels` | `tests/eval/test_calibration_estimator.py::test_repeated_labels_count_once_latest_wins`, `test_label_ts_tie_goes_to_last_in_file_order` |
| `quality` and `route_correct` are independent trials on the same row; `verify_failed` adds its own | `tests/eval/test_calibration_estimator.py::test_quality_plus_route_correct_is_two_trials`, `test_verify_failed_plus_quality_is_two_trials` |
| A `calibrated`/`customer` quality term's own value is never used as a prior (no compounding); its `base` is, so a second round finds its prior | `tests/eval/test_calibration_estimator.py::test_calibrated_src_is_not_a_prior`, `test_round_two_prior_comes_from_the_override_base`; `tests/test_quality_final_fixes.py::test_end_to_end_calibration_round_trip_and_round_two` |
| No prior and no override → `insufficient`, reason `no prior` | `tests/eval/test_calibration_estimator.py::test_no_prior_insufficient` |
| The recommendation gate needs all three of `min_labels`, `max_ci_width`, `min_shift` | `tests/eval/test_calibration_estimator.py::test_min_labels_insufficient`, `test_small_shift_no_change`, `test_hand_example_ci_too_wide_at_default` |
| The Beta quantile is pure Python, cross-checked against an independent exact method (binomial-identity inversion in rational arithmetic) to ~1e-13, never `scipy` | `tests/eval/test_calibration_estimator.py::test_beta_ppf_known_values`, `test_beta_cdf_matches_binomial_identity`, module docstring |
| Replay's equivalence check compares the **full recorded candidate order**, not just the winner | `tests/eval/test_calibration_replay.py::test_unchanged_replay_reproduces_every_real_logged_choice`, `test_swapping_two_non_winning_candidates_fails_equivalence` |
| A scoring record missing a term is skipped and counted `not_recomputable`; only a fully-specified record whose unchanged replay disagrees fails equivalence | `tests/eval/test_calibration_replay.py::test_an_unrecomputable_scoring_record_is_skipped_and_counted`, `test_a_tampered_record_fails_equivalence` |
| Replay only reorders the logged eligible set; a non-recommended unit changes nothing | `tests/eval/test_calibration_replay.py::test_non_recommended_units_never_change_a_decision` |
| A new pick never served for a workload is flagged `no evidence`, not given fabricated numbers | `tests/eval/test_calibration_replay.py::test_a_new_pick_never_served_is_flagged_no_evidence` |
| The CLI aborts (exit 2, writes nothing) on an equivalence failure; a load/option error is exit 1; a clean run with skipped-but-not-failing records is exit 0 | `tests/eval/test_calibration_replay.py::test_cli_exits_2_on_an_equivalence_failure`, `test_cli_exits_1_on_option_or_load_errors`, `test_cli_skips_partial_scoring_records_and_exits_0` |
| Apply validates schema, hash, model existence, workload shape, score range, duplicate units, a non-empty recommended set, and registry version before writing anything; every failure is 422 | `tests/test_quality_calibrations_api.py::test_hash_mismatch_gives_422_and_writes_nothing`, `test_unknown_model_gives_422`, `test_registry_mismatch_gives_422_without_force_and_200_with_force`; `tests/test_quality_final_fixes.py::test_invalid_unit_workload_is_422`, `test_duplicate_recommended_units_are_422`, `test_zero_recommended_units_is_422_for_dry_run_and_apply` |
| Re-applying the same `set_id` is refused (409), never silently duplicated | `tests/test_quality_overrides_store.py::test_write_calibration_set_twice_raises_value_error`; `tests/test_quality_calibrations_api.py::test_reapplying_same_set_gives_409` |
| Revert deletes only its own set's rows; other sets and `customer` rows are untouched | `tests/test_quality_overrides_store.py::test_delete_calibration_set_removes_only_that_set`; `tests/test_quality_calibrations_api.py::test_revert_restores_previous_resolution` |
| `customer` always beats `calibrated` — for the same key and across keys (a model-wide customer row beats a per-workload calibrated row), in the resolver and the dry-run preview; the store keeps both candidates per key | `tests/test_quality_resolver.py::test_override_rung_beats_published_and_tier_default`; `tests/test_quality_overrides_store.py::test_customer_and_calibrated_for_same_key_are_both_kept`; `tests/test_quality_calibrations_api.py::test_customer_row_wins_in_dry_run_and_after_apply`; `tests/test_quality_final_fixes.py::test_resolver_full_ladder_order`, `test_cross_key_customer_wins_in_dry_run_and_after_apply` |
| A calibrated `set_id` is stamped on the served candidate's scoring record | `tests/test_quality_calibrations_api.py::test_apply_writes_rows_and_routed_request_carries_calibrated_source` |
| With zero `model_quality` rows, decisions are unchanged from pre-P2b-3 — except the B54 fix: onboarded models no longer all score the 0.5 floor; one with a published `quality` scores it (any tier, cheap included), else its tier default | `tests/test_quality_serving.py::test_empty_overrides_decide_exactly_like_the_default_resolver`, `test_golden_yaml_pool_scoring_record_is_unchanged`, `test_b54_onboarded_mid_model_scores_its_tier_default_over_http`; `tests/test_quality_final_fixes.py::test_b54_onboarded_cheap_model_published_quality_is_scored` |
| A per-workload calibration changes the pick only for that workload; a refresh failure keeps the last good snapshot; serving never imports `app.eval` | `tests/test_quality_serving.py::test_workload_scoped_override_changes_only_that_workload`, `test_refresh_picks_up_new_rows_and_keeps_snapshot_on_failure`; `tests/eval/test_isolation.py` |
| `onprem_only` eligibility is never affected by a calibrated quality — quality only reorders | `tests/test_quality_serving.py::test_calibrated_cloud_quality_never_lifts_onprem_only` |

### 14.3 What it can and cannot support

**Can support:**

- **Turning §13.3's "quality labels for the served endpoint" into routing.** That was the
  one thing §13.3 said the P2b-2 dataset *could* support but nothing consumed; P2b-3 is
  that consumer, gated by a human reading a replay diff first.
- **An auditable, reversible change.** Every applied set is a versioned `set_id`, listed
  and individually revertible; the router's decision trace names which set produced a
  quality term whenever one applied.
- **Correcting the specific failure mode §10/§11.1 observed** (a model that fails a
  workload keeps being routed to it, because nothing measured that it was failing) —
  *once labels exist*, and only after a human reviews and applies the result.

**Cannot support (same boundary §13.3 already drew for the dataset, now also true of what
calibration computes from it):**

- **Counterfactuals or unconfounded comparisons.** The estimator credits only the served
  model; it cannot know what an unserved eligible model would have scored, so a
  low estimate for a rarely-served model and a high one for a heavily-served model are not
  directly comparable ranking quality — they may just reflect different request mixes.
  Nothing here revisits that; FeasibleRouteBench is still the source of counterfactuals.
- **Faster labelling.** The estimator is bounded by whatever `quality`/`route_correct`
  feedback already exists; it invents no new labels and runs no exploration to go collect
  more.
- **Self-correction.** A calibration is a one-time offline snapshot. If the underlying
  model's behaviour changes after a set is applied, nothing here notices; re-running the
  workflow on a fresh export is the only way to update it.

### 14.4 What a later exploration slice would still need

§13.4's list stands almost unchanged — P2b-3 did not build any of it, only the offline
recommend/apply loop on top of the served-arm dataset:

1. **Propensities, logged at decision time** — the prerequisite for any exploration and
   for off-policy evaluation from production data (§13.4 item 1, §13.3 "off-policy
   evaluation").
2. **The full feasible set on the record**, not reconstructed from `rejected` codes
   (§13.4 item 2) — needed if exploration is to range across the whole feasible set rather
   than just the target tier.
3. **A denser, less voluntary quality label** (§13.4 item 3) — P2b-3's `min_labels`/`
   max_ci_width` gates exist precisely because voluntary feedback is sparse; a denser label
   would let more units clear them sooner (§2.5's "roughly 25+ labels" note in the
   reference doc).
4. **A workload taxonomy clients actually send** (§13.4 item 4) — calibration is per
   workload; untagged traffic can only ever produce a `(model, *)` estimate.
5. **The bench as the pre-apply check** (§13.4 item 5) — P2b-3 deliberately ships with no
   FeasibleRouteBench gate wired into apply (spec, out of scope); an operator can still run
   one manually before applying, but nothing enforces it.
6. **The served model on cache hits** (§13.4 item 6) — a cache hit still credits no model
   in the P2b-3 estimator either, for the same reason §13.1 gives.

## 15. Policy counterfactual simulator (P3a)

§13 gave RouteForge a dataset of what actually happened. §14 taught it to recommend a
narrow correction (a quality score) from that dataset. Neither answers the operator's
day-to-day question: "if I tighten this app's residency, or drop this endpoint, or reprice
that model, what changes for the traffic I already have?" P3a is that offline answer. This
section records the simulator as built on `feat/policy-simulator`. Design:
`docs/superpowers/specs/2026-09-24-policy-simulator-design.md`. Field-level reference:
[`docs/reference/policy-simulator.md`](../reference/policy-simulator.md). Code:
`app/apps/snapshot.py`, `app/api/admin_routes.py` (`GET /admin/config-snapshot`),
`app/eval/scenario.py`, `app/eval/proposal.py`, `app/eval/simulate.py`,
`scripts/policy_simulate.py`. **Not deployed**, so — like §13 before it was deployed —
this section reports what the code does, not a production result.

### 15.1 The model

A **counterfactual over recorded traffic**, not a new estimator. Every simulated row is a
real P2b-2 outcome record; the simulator rebuilds that request's policy context
(`build_context`) and re-runs the actual eligibility filter (`feasible_set`) and the actual
router (`Router.select`) against it — the same functions the serving process calls on a
live request, imported and called directly, never reimplemented. What varies is the
**config** the two calls run under:

- `current` runs against the config snapshot exactly as `GET /admin/config-snapshot`
  exported it;
- `proposed` runs against that same snapshot with an operator-written policy proposal
  applied (`apply_proposal`, a pure function returning a new, deep-copied snapshot
  document).

Only the request's **first routing decision** is replayed — no failover, no
confidence-escalation, no circuit-breaker state, no re-run of PII detection (recorded
entity **types** are replayed as given). Both arms share one frozen reading of
`SignalStore` (`FrozenSignals`, built once from the snapshot's `signals` block and never
aged), so a signal difference between the two arms is impossible by construction — it can
only ever be the proposal's effect.

### 15.2 The two-arm design, and why logged-vs-simulated is avoided

The obvious naive design compares the **logged** outcome (what production actually did)
against a **simulated** `proposed` arm. P3a instead simulates **both** arms — `current` as
well as `proposed` — and diffs simulated against simulated.

The reason is the same fidelity gap every offline-replay tool in this programme has to
account for (§13.3, §14.3): a simulated re-run of `current` does not perfectly reproduce
what was logged, because time has passed since the export window — endpoints get onboarded
or removed, prices change, signals move — and because two fields the outcome record did
not carry until B55 (a pinned model, `max_tokens`) made some rows replay differently than
they were served. The first real Spark run measured baseline agreement of 0.0206 for exactly
that reason: Qwen Code pins its model, so 190 of 194 rows recorded one model and replayed
unpinned to another (B55; fixed 2026-09-24, and rows exported before the fix still replay
unpinned). If the headline number were logged-vs-simulated, every
one of those fidelity losses would show up mixed into the "effect of the proposal" figure,
with no way to tell how much of a cost or route-change delta was the proposal and how much
was drift. Simulating both arms cancels that: whatever fidelity loss the `current` arm
carries, the `proposed` arm carries the identical loss, because it is built from the same
snapshot, the same frozen signals and the same rows — so a `current`-vs-`proposed` delta
isolates the proposal's effect. The **separate** baseline check
(`current`-vs-*logged*, §5 of the reference doc) still exists, but only to report fidelity
as a caveat on the headline, never to compute it.

### 15.3 Invariants §9.1–§9.8, and the test that pins each

| # | Invariant | Pinned by |
|---|---|---|
| 9.1 | An empty proposal predicts zero change: every row routes identically in both arms | `tests/eval/test_simulate.py::test_empty_proposal_zero_change` |
| 9.2 | Stricter never widens — a Hypothesis property: tightening residency, narrowing an allow-list, adding `allowed_regions`, setting `require_no_retention: true`, or removing/disabling an endpoint can only ever leave `proposed`'s eligible set a subset of `current`'s | `tests/eval/test_simulate.py::test_stricter_never_widens` |
| 9.3 | Residency is absolute in simulation too: tightening an app to `onprem_only` gives no `proposed` pick a cloud provider, and rows with no on-prem candidate become `newly_rejected` | `tests/eval/test_simulate.py::test_onprem_only_never_cloud_and_rejects_without_onprem` |
| 9.4 | Removing the sole eligible model for an app turns exactly those rows `newly_rejected` | `tests/eval/test_simulate.py::test_sole_model_removal_rejects_exactly_those_rows` |
| 9.5 | Repricing one model without changing any route moves `cost_usd.delta` by exactly Σ(Δprice × tokens) over the rows it served | `tests/eval/test_simulate.py::test_price_change_moves_cost_exactly` |
| 9.6 | A proposed `sensitivity_rules` edit that escalates a recorded entity type to `onprem_only` moves exactly the rows carrying that type (and is exercised non-vacuously, moving a cloud-served row on-prem) | `tests/eval/test_simulate.py::test_sensitivity_rule_replays_recorded_entity_types`, `test_proposed_rule_on_custom_entity_moves_cloud_row_onprem` |
| 9.7 | Baseline fidelity: a scenario built from a **live** snapshot, replayed against a **live** export from the same app, reaches 100% agreement with the served model — pinned requests included (B55) | `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_baseline_is_perfect`, `test_live_round_trip_pinned_baseline_is_perfect` |
| 9.8 | Determinism: two runs over the same three inputs give byte-identical `simulation.json` | `tests/eval/test_simulate.py::test_summarize_deterministic`; end to end, `tests/eval/test_policy_simulate_cli.py::test_cli_writes_deterministic_outputs` |

(§9.9–§9.11 — load-error locations, the snapshot's admin gate/content-free shape/live
round trip, and serving isolation — are pinned by `tests/eval/test_scenario.py`,
`tests/eval/test_proposal.py`, `tests/test_config_snapshot.py` and
`tests/eval/test_isolation.py`; see the reference doc and the spec for the full mapping.)

### 15.4 Results

These are real runs on the DGX Spark deployment (2026-09-25). Each run used the Spark's own
`/admin/config-snapshot` and `/admin/outcomes/export`, and the CLI ran offline on a laptop.
The on-prem pool had five onboarded models: `llama3.2:latest`, `nemotron-3.5-lightning`,
`qwen3.8-27b` and an operator-onboarded 8B test model in the cheap tier, and a 32B test model
in the mid tier. Every model was priced
at $0, so every cost figure is 0 and Δcost/day is not informative.

| run | proposal | simulated | agreement | route_changed | newly_rejected | Δcost/day |
|---|---|---|---|---|---|---|
| A: before B55 (P3a only) | empty | 194 | 0.0206 | 0 | 0 | 0 |
| B: after B55, fresh rows | empty | 5 | 1.0 | 0 | 0 | 0 |
| C: after B55, fresh rows | disable `qwen3.8-27b` | 5 | 1.0 | 0 | 3 | 0 |

- **Run A:** a day of Qwen Code traffic from app `kit-gtm`. In 190 of 194 rows the recorded
  pick (`qwen3.8-27b`) disagreed with the simulated pick (`nemotron-3.5-lightning`).
  - The client pins `model: qwen3.8-27b`. The outcome rows did not record the pin, so replay
    routed the requests unpinned.
  - The baseline check caught this: the report opened with the agreement warning.
  - This is **B55**, fixed in PR #73. Outcome rows now record `requested_model` and
    `max_tokens`, and replay applies both.
- **Run B:** five fresh requests from a throwaway app.
  - Three were pinned to `qwen3.8-27b`, and two used `model: auto`, which routed to
    `llama3.2:latest`. All five had `max_tokens: 8`.
  - Every simulated pick matched the served one.
- **Run C:** the same rows with `qwen3.8-27b` disabled.
  - The three pinned rows became `newly_rejected`, with code `not_requested`.
  - The two unpinned rows did not move.
  - With `--fail-on-new-rejections` the CLI exited 3.

**Lesson:** the baseline agreement check paid for itself on its first real run. An unrecorded
routing input hid behind a plausible-looking report. The check made it visible before anyone
could act on a wrong prediction. Any future routing input that the outcome row does not
record will fail the same way, so extend the outcome row whenever the router starts reading
something new.

## 16. The minimal conflicting core (P3b)

§15's simulator answers "what changes for the traffic I already have?" It says nothing
about a request that is rejected today: an empty feasible set's 422 used to name only the
first-failed stage of each candidate, leaving an operator to work out by hand which
requirements actually conflict. P3b names that set directly. Design:
`docs/superpowers/specs/2026-09-25-conflicting-core-design.md`. Field-level reference:
[`docs/reference/conflicting-core.md`](../reference/conflicting-core.md). Code:
`app/policy/core.py` (pure computation), `app/policy/feasible.py`
(`candidate_violations`/`violation_matrix`), `app/routing/router.py` (the two raise sites).

### 16.1 The model

The **minimal conflicting core** is a minimum hitting set over a violation matrix. Every
available pool candidate (one not in a §16.3-below *state*) contributes the set of
requirement ids it violates — `V(c)`, from `app/policy/feasible.py::candidate_violations`
for the policy case, or the router's own two budget predicates
(`max_cost_usd_per_request`, `risk_floor`) for the budget case. The feasible set is empty
exactly when every `V(c)` is non-empty. A **conflicting core** `K` is a set of requirement
ids that intersects every `V(c)`; the **minimal** conflicting core is such a `K` of smallest
size (`app/policy/core.py::minimum_hitting_set`).

A minimum hitting set is automatically **irreducible**: dropping any of its ids leaves some
candidate whose `V(c)` is disjoint from the rest of `K`, i.e. a candidate violating nothing
the smaller set names. That is what lets the core be read as "these are exactly the
requirements standing in the way" rather than a superset that includes an id no candidate
actually needs relaxed.

### 16.2 Why exact search is exact at this size

Minimum hitting set is NP-hard in general — the reason most systems that compute one settle
for a greedy or approximate cover. P3b does not need an approximation, because the
alphabet is fixed and small: `REQUIREMENT_ORDER` names at most 11 requirement ids (§2.1 of
the design). Exhaustive search over `2^11 - 1 = 2047` non-empty subsets, tried in
increasing size via `itertools.combinations`, is a brute-force computation that completes
in microseconds — there is no scale at which this stops being cheap, because the universe
size is bounded by the number of *requirement kinds* the product defines, not by the pool
size, the number of candidates, or traffic volume. Ties are broken by `REQUIREMENT_ORDER`
(the design's own table order), so the result is deterministic and does not depend on pool
iteration order — the same conflict always names the same core, whichever order the pool
happens to be enumerated in.

### 16.3 Invariants §5.1–§5.12, and the test that pins each

| # | Invariant | Pinned by |
|---|---|---|
| 5.1–5.3 | Covering (every available candidate's `V(c)` meets the core), minimum (no smaller set covers, checked against a brute-force oracle), irreducible (dropping any core id leaves an uncovered candidate) | `tests/test_conflicting_core.py::test_covering_minimum_irreducible` (Hypothesis) |
| 5.4 | Agreement with the live seam: `candidate_rejection(c)` is `None` exactly when `candidate_violations(c)` is empty; when not `None`, its code maps to one of the violated ids | `tests/test_policy_violations.py::test_agreement_with_candidate_rejection` (Hypothesis) |
| 5.5 | Order independence: shuffling the pool gives an identical core | `tests/test_conflicting_core.py::test_order_independent` (Hypothesis) |
| 5.6 | State is never in the core: a pool of only disabled/circuit-open/prior-failure candidates gives `core == []` and `state` lists them; an empty pool gives `state: [no_endpoints]` | `tests/test_conflicting_core.py::test_state_only_core_is_empty`, `tests/test_policy_violations.py::test_empty_pool_state`, `tests/test_router_conflicting_core.py::test_empty_pool_is_no_endpoints_state`, `test_a_disabled_only_pool_reads_disabled_not_no_endpoints` |
| 5.7 | Security hints never relax: every `security: true` id's hint templates fail a denylist check ("remove", "disable", "relax", "lower", "allow cloud", …) | `tests/test_conflicting_core.py::test_security_hints_never_relax` |
| 5.8 | Worked examples: `onprem_only` + a pinned cloud model gives `{residency, requested_model}`; `allowed_regions` + `require_no_retention` against endpoints each satisfying only one gives `{allowed_regions, require_no_retention}`; a budget case with every in-floor candidate over cap and a cheaper candidate below floor gives `{max_cost_usd_per_request, risk_floor}`; a sensitivity-rule escalation names the rule's entity types | `tests/test_conflicting_core.py::test_worked_example_onprem_pin`; `tests/test_router_conflicting_core.py::test_onprem_only_with_a_pin_to_neither_gives_residency_and_requested_model`, `test_region_and_retention_each_met_by_a_different_endpoint`, `test_budget_floor_over_cap_and_cheap_below_floor`, `test_sensitivity_rule_escalation_names_the_entity_types` |
| 5.9 | Live 422: an `onprem_only` app with no on-prem model gives `error.conflicting_core.requirements[0].id == "residency"` in a B49-valid envelope, `code` staying `no_eligible_model` | `tests/test_conflicting_core_api.py::test_422_body_carries_the_conflicting_core`, `test_422_envelope_keeps_every_b49_field` |
| 5.10 | Outcome row and trace surface: a `no_eligible` outcome row carries `conflicting_core`, the export includes it, and the structured `serving.api` log line carries it (no Langfuse trace exists for a request that never reaches `_finalize`) | `tests/test_conflicting_core_api.py::test_outcome_row_records_the_core`, `test_export_line_includes_the_core`, `test_failed_request_log_line_carries_the_core`, `test_keepalive_stream_error_frame_carries_the_core` |
| 5.11 | Simulator: a proposal that removes the only model satisfying a pin gives `by_core == {"requested_model": n}` (or the real computed core), and the report groups new rejections by core | `tests/eval/test_simulate.py::test_summarize_counts_new_rejections_by_core`, `test_summarize_disabled_pinned_model_groups_by_requested_model_core`, `test_summarize_empty_core_falls_back_to_state_key`, `test_report_by_conflicting_core_table_present`, `test_report_by_conflicting_core_table_absent_when_no_new_rejections` |
| 5.12 | Isolation and content-freeness: no `detail` field ever holds request content — a prompt seeded with a unique marker appears in none of the 422 core, the log line, or the outcome row | `tests/test_conflicting_core_api.py::test_conflicting_core_is_content_free` |

Robustness beyond the numbered invariants: a core computation failure never changes which
exception is raised or lets a request through — it only costs the explanation
(`core=None`, logged at `ERROR`) — pinned by
`tests/test_router_conflicting_core.py::test_core_failure_is_fail_safe`. A hand-built
context that refuses a candidate through `ctx.allowed_models` while naming no
`app_allowed_tiers`/`app_allowed_models` produces an unexplained candidate that is dropped
from the matrix (with a warning), never crashing the 422 —
`test_hand_built_context_with_an_unexplained_rejection_does_not_crash`. And the budget
core's own internal consistency is asserted, not assumed: a candidate the matrix builds
that violates neither cost nor the risk floor would mean the router's budget fallback and
the core disagree, and `_budget_core` raises loudly rather than build a silently wrong core
— `test_budget_core_asserts_on_a_candidate_violating_neither`.

### 16.4 Spark example

Live on the DGX Spark (2026-09-25, `dd3790c`). A throwaway app with default residency sent a
`model: auto` request whose prompt carried a unique marker. The pool held five enabled on-prem
models in the cheap and mid tiers, with no region declared; cloud providers were disabled.

| case | core |
|---|---|
| app `allowed_tiers: [frontier]` | `allowed_tiers` ("tier must be one of frontier"), 5 candidates, no state |
| app `allowed_regions: [eu]` | `allowed_regions` (security; hint: "Onboard or approve an endpoint declared in an allowed region."), 5 candidates, no state |

- Both responses were 422 `no_eligible_model` with `error.conflicting_core`.
- Both `routing_outcomes` rows (`status = no_eligible`) carried the `to_record()` core.
- The prompt marker appeared in neither.
- The `serving.api` `no_eligible_model` log line did **not** reach the container log. RouteForge
  configures no logging handler, so every `serving.*` INFO record is dropped in deployment.
  This is filed as B57; the 422 and the outcome row are the working channels until it is fixed.

## 17. Shadow prices (P3c-1)

§15's simulator answers "what would this specific proposal change?" §16's minimal conflicting
core answers "why is this one request rejected today?" Neither answers a question an operator
routinely has *before* writing any proposal: of everything an app's policy currently
constrains, **which constraint is actually expensive, and which is cheap?** Design:
`docs/superpowers/specs/2026-09-26-shadow-prices-design.md`. Field-level reference:
[`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §8. Code:
`app/eval/shadow.py` (`Relaxation`, `enumerate_relaxations`, `shadow_prices`,
`render_shadow_section`), wired into `scripts/policy_simulate.py` by `--shadow-prices`.

### 17.1 The model

A **shadow price**, in the operations-research sense this borrows its name from, is what one
unit of relaxing a binding constraint would be worth. P3c-1 computes an empirical version of
that idea directly, with no linear-programming dual and no model of the objective beyond "the
same cost/quality/latency/on-prem-share metrics §15 already uses": for each requirement an
app's policy currently enforces, build a copy of the baseline config with **only that one
requirement relaxed** (the spec §3 table — `residency` widens to `any` with escalation off,
an allow-list clears to `null`, `raw_sensitive` flips every `false` declaration to `true`,
`max_cost_usd_per_request` widens to `1e12`, `risk_floor` flattens every tier to `cheap`, …),
replay the same recorded rows through the real `feasible_set`/`Router` code under that one
relaxed config, and diff against the baseline arm the exact same way §15's `current` vs
`proposed` arms are diffed. Every relaxation is priced independently, against the same
baseline — never against each other's relaxed config — so two prices can always be read side
by side without wondering whether one relaxation's effect leaked into another's number.
Candidates are enumerated deterministically (app id, then P3b's `REQUIREMENT_ORDER`, then
endpoint slug) and capped by `--max-relaxations` (default 50); a relaxation whose rows all
route identically under both arms is **non-binding** and is counted, not listed — the two
report tables only ever show constraints that actually did something to the replayed traffic.

Security requirements (`app/policy/core.py::SECURITY_IDS` — `residency`, `allowed_providers`,
`allowed_regions`, `require_no_retention`, `raw_sensitive`, `risk_floor`) are priced through
the identical mechanism as operational ones, but reported in a separate table, every row
carrying the literal label `price of a protection — not a recommendation`. This follows
handoff §37 ("Security constraints are never automatically relaxed") to its logical
conclusion for a *measurement* tool: pricing a protection is legitimate and useful — an
operator should be able to see what `onprem_only` is costing them — but nothing about
computing that number may look like, or be usable as, a one-click "relax this" action. The
CLI never writes anything back; every relaxed doc is a `copy.deepcopy` discarded at the end of
that one relaxation's evaluation.

### 17.2 Scoped attribution and why

Two of the eleven relaxations edit data that is not app-scoped at all: `raw_sensitive` flips
declarations on onboarded rows and on `settings.providers.*`/`settings.routing.model_pool[*]`,
all of which are shared by every app in the deployment; `risk_floor` flattens
`settings.routing.risk_floors`, a single global map. Editing the *document* for either has to
be global — there is no per-app copy of an endpoint's declared `accepts_raw_sensitive` or of
the risk-floor table to edit instead. But re-simulating **every** app's rows under that edit
would misattribute the price: if flipping `raw_sensitive` globally also happens to change an
unrelated app B's routing (because B independently has some other reason a candidate was
excluded, and this relaxation coincidentally frees a different one up), that effect is not
what was being priced — it is noise from data those two relaxations happen to share, not the
cost of *this app's* `raw_sensitive` constraint. `Relaxation.apply` (`app/eval/shadow.py`)
resolves this by editing the document globally but filtering `rows` down to only the app being
priced before returning them for re-simulation — every other app's rows are left out of scope
by construction, never even replayed under that relaxed doc, so the price is attributable to
exactly one app for every relaxation except one.

`endpoint_app_allowlist` is the deliberate exception, scoped to an **endpoint**, not an app:
`allowed_app_ids` names which apps may use one onboarded endpoint, so relaxing it can free up
that endpoint for *any* app whose other requirements it now satisfies — there is no single app
to attribute the price to, so all rows are back in scope for that one relaxation. This is the
same distinction P3b's core computation draws between a requirement's *scope* and its
*security* flag: scope is about which rows a relaxation's price can honestly claim to explain,
independent of whether the requirement is a protection.

Pinned by `tests/eval/test_shadow.py::test_apply_never_mutates_and_scopes_rows` (app-scoped:
never mutates the input, scopes to one app's rows), `test_each_relaxation_edits_the_right_place`
(every requirement edits the document location the table says), `test_endpoint_relaxation_scopes_all_rows`
and `test_endpoint_allowlist_resimulates_all_rows` (endpoint-scoped: all rows re-simulated, but
`affected_apps` still names only the apps that actually moved), and
`test_residency_relaxation_is_security_and_scoped` (an app-scoped security relaxation never
touches a second app's rows or count).

### 17.3 The eligible-superset invariant, a counterexample, and test names

Every relaxation only ever *widens* what an app may use — nothing in the spec §3 table can
narrow eligibility. That gives a checkable invariant over every re-simulated row: **the
relaxed eligible set (`feasible_set`) is a superset of the baseline eligible set.** A row that
violates it means a bug in the relaxation logic itself (an `_APPLIERS` entry that narrows
something, or a scoping mistake) — never a real finding, so it is not reported as one.
`_check_eligible_superset` (`app/eval/shadow.py`) raises `ShadowInvariantError`, and
`scripts/policy_simulate.py` maps that straight to exit `2` with `error: internal invariant:
...` on stderr — the same "never a traceback, name what broke" contract every other
input-validation failure in this CLI already has. This is the only hard invariant.

**Counterexample: the original claim was wrong.** The design (commit `9205c18`, and the spec
§3 as first written) claimed a second half: "a row routed at baseline is never rejected under
the relaxation", so `newly_rejected` would always be 0 and any non-zero value would exit 2.
That is false. Eligibility is monotone in the relaxation, but **the router is not monotone in
the eligible set.** The model, from `app/routing/router.py`:

- *Target.* The target tier is `max(complexity tier, risk_floors[risk_class], escalation
  min_tier)`.
- *The clamp (rule 8, residency beats the risk floor).* If the target is above the best tier
  among the policy-allowed candidates, it is clamped down to that best allowed tier — which
  can sit **below** the risk floor. This is what keeps a high-risk `onprem_only` request on
  prem instead of lifting it to a cloud tier.
- *The budget fallback.* If every candidate at the picked tier is over
  `max_cost_usd_per_request`, the router steps down to the most capable cheaper allowed tier
  that fits — but only down to `floor = max(risk floor, escalation min_tier)`, never below
  it. When the clamp already put the pick below the floor, there is nothing to step to.

So a relaxation that makes the floor tier eligible **removes the clamp**: the target lifts to
the floor tier, which a tight budget cannot afford, and the fallback cannot go below the
floor. The row that routed at baseline is now rejected `over_budget`. The final review found
it on ordinary fixtures and, with it, the product's most important persona:

- **Case B — the hospital persona.** App `a` is `onprem_only` with
  `max_cost_usd_per_request: 0.002`. On-prem serving has onboarded only a cheap and a mid
  endpoint; Azure and Bedrock are enabled, with a cloud frontier model. One row: a detected
  `US_SSN` (high risk, floor `frontier`), 1000 prompt tokens, complexity 0.1. At baseline the
  eligible set is the two on-prem endpoints; the target clamps from `frontier` to `mid`, the
  mid endpoint fits the budget, and the row routes to it. Relaxing `residency` (to `any`,
  escalation off) makes the cloud models eligible — a strict superset, so the invariant
  holds. The target is no longer clamped: it is `frontier`, the frontier model is over the
  0.002 cap, the fallback floor is `frontier`, and the row is rejected `over_budget`.
- **Case A.** `allowed_tiers: [cheap]`, `max_cost_usd_per_request: 0.001`, the same high-risk
  row. At baseline the target clamps to `cheap` and routes. Relaxing `allowed_tiers` makes the
  frontier floor eligible, and the row is rejected `over_budget` the same way.

Both are ordinary configurations, and the shipped check raised `ShadowInvariantError` on them
— an operator pricing their hospital app's residency would have got exit 2 instead of an
answer. The router is behaving as designed (rule 8 plus a floor-respecting budget); the error
was the claim. The as-built fix (spec §3 amendment, 2026-09-26) keeps the eligible-superset
check as the only hard invariant and **reports** the other half: each entry carries
`newly_rejected` and `newly_rejected_reasons` (`{"over_budget": 1}` in both cases), such an
entry is binding, and `report.md` prints a "Newly rejected" column plus a one-line
explanation under the tables. Read as a shadow price, a newly rejected row is a real finding:
it says the constraint was silently *holding the risk floor down* for that app, and relaxing
it alone would expose a budget that cannot pay for the floor.

The invariant and the reported half are pinned five ways:

- **Property-based, across random fixtures and every relaxation kind:**
  `tests/eval/test_shadow.py::test_eligible_superset_for_random_policies` (Hypothesis, 25
  examples per run, plus Cases A and B as explicit `@example`s) — random per-app policies
  (residency, allowed tiers, allowed regions, require-no-retention, a max cost that includes
  the tight 0.001/0.002 budgets) crossed with random detected entity types, complexity, a 200
  or 1000-token prompt and Bedrock on or off. It asserts `shadow_prices` never raises, every
  entry is binding, and each entry's `newly_rejected` and `newly_rejected_reasons` match an
  independent row-by-row recount of the two arms (which also re-checks the superset).
- **The two counterexamples, deterministically:** `test_case_a_allowed_tiers_relaxation_newly_rejects_over_budget`
  and `test_case_b_hospital_residency_relaxation_newly_rejects_over_budget` — no raise, the
  entry is binding with `newly_rejected == 1` and reason `over_budget`.
- **Routed → rejected is reported, not raised:** `test_routed_to_rejected_is_reported_not_raised`
  — a monkeypatched relaxed arm that flips a baseline-routed row to rejected (eligible set
  untouched) produces a binding entry with `newly_rejected == 1`. It replaces the former
  `test_invariant_routed_to_rejected_raises_alone`.
- **The "lost eligible candidate" branch in isolation:**
  `test_invariant_lost_eligible_raises_alone` — a monkeypatched relaxed arm that drops one
  eligible candidate (while staying routed) raises with `"lost eligible candidate"`.
- **The CLI exit code:** `tests/eval/test_policy_simulate_cli.py` still injects a
  `ShadowInvariantError` and asserts exit `2`; `test_invariant_breach_raises` (an arbitrary
  monkeypatched relaxed arm that also empties the eligible set) still raises.

Determinism — the same three inputs give byte-identical `simulation.json` — is pinned by
`test_deterministic` (the block alone) and, end to end through the CLI,
`test_shadow_prices_cli_deterministic` (`tests/eval/test_policy_simulate_cli.py`). The
like-for-like cost accounting (§8.4 of the reference doc) is pinned by
`test_block_shape_and_cost_per_day_like_for_like`, `test_cost_saved_per_day_is_like_for_like`,
`test_cost_saved_per_day_null_under_one_day_and_ranking` and
`test_cost_saved_per_day_uses_the_full_export_window`; the full ranking order (including the
scope-key tie-break) by `test_ranking_full_order_with_tie_break`. The CLI wiring — optional
`--proposal`, the two `simulation.json`/`report.md` shapes, `--max-relaxations` validation and
the live round trip — is pinned by `tests/eval/test_policy_simulate_cli.py`:
`test_shadow_prices_without_proposal`, `test_shadow_prices_with_proposal_appends_to_p3a_output`,
`test_max_relaxations_zero_exits_2`, `test_neither_proposal_nor_shadow_prices_exits_2` and
`test_live_round_trip_shadow_prices_allowed_tiers` (a real app restricted to a tier with no
seeded model gets two real 422s, and the shadow-priced `allowed_tiers` entry reports
`rejections_avoided == 2`).

### 17.4 Results

Run on 2026-09-26 against the DGX Spark after PR #79 was deployed (`4bfb0b3`). Inputs:
`GET /admin/config-snapshot` (`snapshot_sha256` `aeaa4647…`, `routing_policy_version`
`6e6fe2027862`) and a 30-day `GET /admin/outcomes/export`: 521 rows, all from one app
(`kit-gtm`), recorded 2026-09-24 00:44 to 2026-09-26 16:21 UTC. Command:
`policy_simulate.py --shadow-prices`, no proposal, so the baseline is the snapshot.

**Pre-B55 rows must be excluded.** The first 221 rows predate B55 (§15.4): they carry no
`requested_model`, although the client pinned `qwen3.8-27b` on them as on every later row. The
simulator replays them unpinned, so the baseline arm already disagrees with what was recorded
there. Over all 521 rows, shadow prices reported a binding `allowed_models` for `kit-gtm`: 221
route changes, −0.66 USD/day, +0.10 quality. That entry is entirely an artefact of those 221
rows, and it disappears when they are removed. This is the same lesson as §15.4: a price is
only as good as the baseline's agreement with the record, and the shadow-price report does
not re-check that agreement itself. Run a no-op `--proposal` first and read
`baseline_agreement`, or cut the export at the first B55-era row, as here.

The result on the 300 B55-era rows (2026-09-25 01:25 onward):

| constraint | scope | rows | route changed | rejections avoided | newly rejected | cost saved/day | Δp50 | security |
|---|---|---|---|---|---|---|---|---|
| `requested_model` | app `kit-gtm` | 300 | 300 | 0 | 0 | 0 | −26.3 s | no |

Evaluated 4, non-binding 3, skipped by cap 0. No security constraint binds, so the
cost-of-protection table is empty.

What it says:

- **The one constraint that binds is the client's own pin.** `kit-gtm` (Qwen Code) pins
  `qwen3.8-27b` on every request. Unpinned, the router would send all 300 to
  `nemotron-3.5-lightning`.
- **The price of the pin is latency, not money.** Both models are unpriced on-prem endpoints,
  so the cost delta is exactly zero. The median replayed latency would fall by 26.3 s. That is
  the frozen measured p50 of the two endpoints in the snapshot, not a controlled comparison.
- **Quality reads as unchanged (Δ 0) only because both candidates sit on the tier-default
  quality prior.** No calibration set has been applied (P2b-3), so the simulator cannot say
  whether the faster model is good enough for a coding agent. That decision stays with the
  operator.
- **This run did not exercise the §17.3 counterexample.** No high-risk rows appear in this
  traffic, and newly rejected is 0 everywhere.

A zero saving first rendered as `-0/day`. `-(0.0) / days` is a signed zero. It is fixed in
the same change as these results (`_round6` now normalises `-0.0`).

## 18. Alternative scenarios (P3c-2)

§15 replays one proposal. §17 prices one constraint at a time. Neither lets an administrator
put several joint changes next to each other. P3c-2 does. Design:
`docs/superpowers/specs/2026-10-05-p3c2-scenarios-design.md`. Reference:
[`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §9. Code:
`app/eval/scenarios.py`, with the new replay behaviour in `app/eval/simulate.py`,
`app/eval/proposal.py` and `app/eval/scenario.py`. **Not deployed**; this section reports
what the code does.

### 18.1 The model

- **One shared baseline.** The `current` arm is built and simulated once. Each scenario is a
  v1 proposal body, applied with `apply_proposal` and replayed against that same arm. Baseline
  agreement is a property of the `current` arm, so it is reported once.
- **No ranking.** Scenarios are reported in file order. No field ranks, scores or recommends
  (handoff §28).
- **Assumptions are separate from configuration.** A proposal can now assume a token scale, a
  cached-prompt share and a per-endpoint latency. They are carried in an `assumptions` block
  that only `apply_proposal` writes, and only the scenario arm reads. The diff therefore
  isolates their effect.
- **Token scale.** Complexity is `min(1, tokens / 2000)` live. Below saturation it is linear,
  so scaling is exact. At saturation the true token count is hidden; it was at least 2000, so
  the scaled complexity is at least `scale`. Replay uses
  `min(1, max(scale, prompt_tokens' / 2000))` and counts the row as an estimate when scaling
  down. The spec's simpler formula can go *down* when scaling *up*, because the recorded
  prompt tokens are the backend's count, not the router's.
- **Asserted latency.** Scored as a measurement, so it also supplies the same-tier peer prior.
  Otherwise re-asserting the current value could remove a prior and move a route.
- **Context-window overflow.** Both arms mirror the gateway's non-streaming path: exclude the
  overflowing pick, select again, at most `max_failover_attempts` extra times. This is the
  first later-hop behaviour replayed, and it is safe to replay because it is a pure function
  of declared data -- but that also means it is a PREDICTION, not a replay of what happened.
  Live, overflow is the backend's own refusal on its real tokenisation and its real limit;
  replay has no backend to ask, so it predicts the refusal from the operator's declared
  `context_window` instead (final review, §18.2 below). The live STREAMING path
  (`Gateway.run_stream`) makes exactly one `Router.select` call and never re-selects, so a
  streamed request that overflows live is a 400 with no retry -- replay's re-selection is
  strictly more forgiving there than what the stream path actually does.

### 18.2 Two things the design expected that the code does not give

- **Agreement does not rise.** The design expected overflow replay to raise baseline
  agreement. The baseline compares first decisions over served rows, and overflow replay does
  not change the first decision. `ArmResult.attempted` exists so the comparison stays on the
  first decision; without it a correctly reproduced overflow failover would count as a
  disagreement and agreement would fall.
- **Recorded overflow rejections are not reproduced.** A failure row records
  `prompt_tokens: 0`, so replay sees a prompt that fits any window. Reproducing those rows
  needs the outcome row to record the prompt size on failure — a serving change, not made
  here. This is the §15.4 lesson again: replay can only be as faithful as the row.
- **A served pick's own predicted overflow was invisible to agreement (final-review finding,
  closed).** The first bullet above is about a reproduced FAILOVER -- it says nothing about a
  record that proves the first pick ALONE served the row (`chosen == attempts[0]`, one
  recorded attempt) while replay's `current` arm excludes that same pick as a predicted
  overflow. First picks still match in that case, so the original design read it as perfect
  agreement, silently. That is backwards: it is exactly the row where the prediction (§18.1)
  disagrees with the record. `_baseline` now counts it as a disagreement and reports the count
  separately as `overflow_contradicted`, so it reads as a fidelity check on each endpoint's
  DECLARED `context_window` specifically, not a routing-logic disagreement: a non-zero count
  says some endpoint's declared window probably understates its real one (or no `max_tokens`
  was recorded and the default output estimate overstates the request --
  `docs/reference/policy-simulator.md` §6).

### 18.3 Invariants, and the test that pins each

| # | Invariant | Pinned by |
|---|---|---|
| S1 | An empty scenario predicts zero in every delta column | `tests/eval/test_scenarios_compare.py::test_s1_empty_scenario_is_a_noop` |
| S2 | One scenario gives exactly what `--proposal` gives | `tests/eval/test_scenarios_compare.py::test_s2_single_scenario_equals_single_proposal`; `tests/eval/test_policy_simulate_cli.py::test_cli_single_scenario_matches_the_proposal_run` |
| S3 | Permuting scenarios permutes the table and nothing else | `tests/eval/test_scenarios_compare.py::test_s3_order_independence` |
| S4 | Neutral assumptions change no route and no cost | `tests/eval/test_proposal_workload.py::test_s4_neutral_workload_changes_nothing`, `::test_s4_neutral_latency_changes_no_route_or_cost`, `::test_neutral_latency_keeps_the_peer_prior_it_supplies` |
| S5 | With routing fixed, cost is non-decreasing in `token_scale` | `tests/eval/test_proposal_workload.py::test_s5_cost_monotone_in_token_scale` |
| S6 | Cost is non-increasing in `cached_share`; unchanged with no cached price | `tests/eval/test_proposal_workload.py::test_s6_cached_share_never_raises_cost`, `::test_s6_cached_share_without_declared_price_is_neutral` |
| S7 | An overflowing pick is never served; an undeclared window never overflows; all-overflow is a `context_window` rejection | `tests/eval/test_replay_overflow.py::test_s7_overflowing_pick_is_never_served` (Hypothesis), `::test_s7_undeclared_window_never_overflows`, `::test_s7_all_overflow_rejects_context_window`, `::test_bound_is_max_failover_attempts_exactly_as_live` |
| S8 | No ranking field; file order | `tests/eval/test_scenarios_compare.py::test_s8_no_ranking_and_file_order` |
| S9 | Tightening never widens eligibility, per scenario; `onprem_only` stays absolute | `tests/eval/test_scenarios_compare.py::test_s9_scenario_proposals_never_widen_eligibility` (Hypothesis) |
| S10 | Rows before `--since` are excluded, counted, and feed nothing | `tests/eval/test_simulate.py::test_s10_since_excludes_and_counts_old_rows`, `::test_since_equals_a_precut_export` |
| S11 | Agreement below `--min-agreement` exits 4 and writes nothing | `tests/eval/test_policy_simulate_cli.py::test_s11_min_agreement_exits_4_and_writes_nothing` |

S4's latency clause holds where the router's latency term and the replayed latency are the
same number. Replay's ladder is observed → signal → tier default; the router's is signal →
peer prior → tier default. The test uses a measured snapshot signal with no observed rows,
where they coincide.

### 18.4 Results

#### 18.4.1 First live run — DGX lab, 2026-10-06

**Setup.** DGX lab Run:ai cluster, RouteForge api `0.26.0` (`main` through PR #84). One
onboarded endpoint: `qwen3-coder-next` (vLLM, tier `mid`, declared context window 262144,
priced $10/$10 per 1M by the operator). One application, `paper-test`, default policy
(per-request cap $0.05). 24 single-turn requests sent once, prompt sizes 23–2265 backend
tokens for the served ones, `max_tokens` 16–64, two client workload labels. This is a
plumbing check of the feature on real records, **not** a workload study: one app, one
endpoint, one pass, 29 seconds of traffic.

Artifacts (content-free): `docs/research/results/p3c2-dgx-lab-2026-10-06/`
(`scenarios.json`, `comparison.json`, `report.md`). `comparison_id e76c748673457d86`,
registry version `51464fef8c98`, run with `--since 2026-10-06T16:00:00Z --min-agreement 0.9`.

**Live outcome.** 18 requests served (17 from the batch plus one smoke request), 7 rejected
`no_eligible` with HTTP 422. Every rejected row carries the same minimal conflicting core,
recorded live: `max_cost_usd_per_request` (cap 0.05, cheapest estimate 0.05344), no state
component, one candidate considered. That is P3b working on a real deployment.

**Replay.** 25 rows simulated (1 older row cut by `--since`), six scenarios.

| measurement | value |
|---|---|
| baseline agreement | 1.0 over 18 compared (`ok`) rows |
| rows with `overflow_hops > 0` in the current arm | 0 |
| `overflow_contradicted` | 0 |
| `no-change` scenario | zero in every column (S1 holds on live data) |
| `smaller-prompts-0.6` (`token_scale: 0.6`) | cost −0.05546 USD of 0.14227 (−39.0%); 0 route changes; 11 rows with estimated saturated complexity |
| `cheaper-price` (0.5 / 1.5 per 1M) | cost −0.134794 USD (−94.7%); 0 route changes |
| `window-2000` (declared window 262144 → 2000) | 2 newly rejected, both `context_window`; 2 overflow hops |
| `slower-5s` (`latency_ms: 5000`) | Δp50 = Δp95 = +4848.6 ms; 0 route changes (one endpoint, nothing to move to) |
| `raise-request-budget` (cap 0.05 → 0.5) | **0 newly served — wrong, see below** |

**Finding: live-rejected rows replay as routed, so a budget scenario cannot show what it
would rescue (B60).** The seven `no_eligible` rows record `prompt_tokens: 0` and
`complexity: 1.0`. Replay prices a zero-token prompt under any cap, so the `current` arm
*routes* all seven, although the record says they were rejected. Consequences, all visible in
this run:

- `raise-request-budget` reports 0 newly served. Live, raising the cap to $0.5 would have
  served all seven (their estimates were ≈ $0.053).
- Baseline agreement stays 1.0, because agreement is computed over `ok` rows only. The
  disagreement between the record (`no_eligible`) and the current arm (routed) is not
  counted anywhere.
- The seven rows are the "11 rows with estimated saturated complexity" minus the four served
  rows above 2000 routing tokens: their scaled cost is computed from zero tokens.

This is the limit §18.2 predicted for `context_overflow` rows, observed on the commoner case
of budget rejections. It needs one serving change (record the routing token count on rejected
outcome rows) and one replay change (count a recorded rejection that the current arm routes
as a disagreement). Both are scoped into the replay-fidelity slice.

**What this run does not show.** No route ever changes: there is one endpoint. No eligibility
constraint binds except the budget. Latency p50 is the frozen measured median (151.4 ms), not
a per-request model. Nothing here supports a claim about routing quality or savings.

#### 18.4.2 Still to measure

| measurement | value |
|---|---|
| Agreement before/after overflow replay on an export that contains real overflow failovers | TO MEASURE |
| `overflow_contradicted` on an export from an endpoint with a tight declared window | TO MEASURE |
| Healthcare triad (all on-prem / raw-PII-only on-prem / all on-prem + second endpoint) on persona traffic: the comparison table | TO MEASURE |
| Static 4-request test vs traffic-weighted replay of the same triad | TO MEASURE |

The triad needs at least two eligible endpoints with different placement (an on-prem and a
cloud or second on-prem endpoint) and persona traffic with PII; the DGX lab has one endpoint
today.

## 19. Replayable rejections: the routing token count on the record (B60)

§18.4.1 found that the simulator replays a live-rejected request as routed, and that the
baseline check did not notice. This section records the fix. Design:
`docs/superpowers/specs/2026-10-06-replayable-rejections-design.md`. Reference:
[`docs/reference/routing-outcomes.md`](../reference/routing-outcomes.md) §3.2 and
[`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §5, §6. Code:
`app/pipeline.py` and `app/apps/outcomes.py` (the record), `app/eval/simulate.py` (replay and
agreement). **Deployed to the DGX lab on 2026-10-06** (api `0.27.0`); not on the Spark. §19.4.1 has the first live results; the remaining ones are to be
measured (§19.4).

### 19.1 The model

A routing decision is a function of the request's features and the configuration. The
simulator can replay a decision only if every feature the decision read is on the record.
§13.1 lists what an outcome row holds. Two rounds of replay have each found a feature that
was read by the decision and missing from the row:

| Feature | Read by | Missing until | Symptom while missing |
|---|---|---|---|
| the client's model pin, its output cap | eligibility (the pin stage), the budget check | B55 (§15.4) | replay picked a different **model** |
| the token count the router was given | the budget check, the cost term of the score | B60 (this section) | replay **served** a request the record says was rejected |

The second gap hid behind a property of the record. `prompt_tokens` is the backend's billed
count, so it is zero on a row no backend answered. For a served row it is a usable stand-in
for the router's count. For a rejected row it says the prompt was empty, and an empty prompt
fits any per-request budget and any context window.

- **Two counts, two meanings.** `routing_tokens` is RouteForge's own count, made after the
  gate and after compression and before any backend is called. It is what admission and
  scoring read. `prompt_tokens` is what the backend counted and billed. Replay gives the
  router the first and prices the call at the second.
- **A row no backend answered is an estimate.** It has no billed count and no completion.
  When an arm routes it, replay prices it at the pair the live budget check priced it at:
  its routing tokens and the router's output estimate. That is the admission estimate, not a
  bill, and the output counts such rows.
- **A row with no size is not replayed.** A rejected or failed row from before the fix is
  excluded and counted. Replaying it as an empty prompt is the defect, so there is no
  degraded mode for it.
- **Agreement has two parts.** Model agreement compares the first pick with the recorded
  first attempt, over served rows. Outcome agreement compares routed-or-rejected with the
  recorded status's class, over every simulated row. Each is blind to the other's failure:
  B55 was invisible to an outcome check (every row still routed), and B60 was invisible to a
  model check (it only looks at served rows).

### 19.2 Why outcome agreement needs a count beside it

Rejected requests are usually a small share of traffic. Outcome agreement is taken over all
simulated rows, so every rejection can replay wrong while the rate stays high: 2 wrong out
of 30 rows is 93%. The number that says whether a "newly served" figure can be trusted is
the count out of the recorded rejections. So `recorded_rejected_but_routed` is reported with
`recorded_rejected`, and a non-zero count prints a note whatever the rate is
(`tests/eval/test_replay_rejections.py::test_diluted_outcome_agreement_still_reports_every_rejection_wrong`).
`--min-agreement` gates on the lower of the two rates; it is diluted in the same way, and
only `--min-agreement 1.0` refuses every disagreement. A per-class threshold was not added.

### 19.3 Invariants, and the test that pins each

| # | Invariant | Pinned by |
|---|---|---|
| R1 | The table is still content-free: `routing_tokens` is a nullable integer, and a rejected request carrying a marker, an SSN and an e-mail address writes none of them | `tests/test_outcome_store.py::test_schema_is_content_free`; `tests/test_outcome_routing_tokens.py::test_r1_rejected_row_with_pii_carries_a_count_and_no_content` |
| R2 | A served row replays to an identical `ArmResult` when `routing_tokens == prompt_tokens`, in both arms, under any `token_scale` | `tests/eval/test_replay_rejections.py::test_r2_served_row_replay_is_unchanged_when_the_counts_coincide` (Hypothesis) |
| R3 | A request rejected live for its per-request budget is rejected in the `current` arm under the same snapshot | `tests/eval/test_replay_rejections.py::test_r3_budget_rejected_row_is_rejected_in_the_current_arm`; live: `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected` |
| R4 | Raising the cap in a proposal newly serves it, costed at the admission estimate | `tests/eval/test_replay_rejections.py::test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate`; live: the same round-trip test |
| R5 | Outcome agreement is 1.0 on a live round trip that contains rejections | `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected`, `::test_live_round_trip_scenarios_show_the_rescued_rejections` |
| R6 | A rejected or failed row with no recorded size is excluded, counted, and reaches no arm | `tests/eval/test_replay_rejections.py::test_r6_unsized_rows_are_excluded_and_counted_never_routed`; live export: `tests/eval/test_policy_simulate_cli.py::test_live_export_stripped_of_routing_tokens_excludes_the_rejections` |
| R7 | No serving module imports `app.eval` | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` |
| R8 | The OpenAI wire shape is unchanged: success validates against the SDK model, the rejection envelope keeps exactly its keys, the new field appears in neither | `tests/test_openai_compat.py::test_outcome_recording_adds_nothing_to_the_success_wire`, `::test_outcome_recording_adds_nothing_to_the_rejection_wire` |
| R9 | An export without the field loads, and its served rows replay as before | `tests/eval/test_replay_rejections.py::test_r9_export_without_the_field_still_loads_and_replays_served_rows` |
| R10 | The recorded value is the `tokens_after` the router was called with, on every status; null on a semantic-cache hit | `tests/test_outcome_routing_tokens.py` (one test per status, each against a spy on `Router.select`), `::test_semantic_cache_hit_row_has_null_routing_tokens` |
| R11 | Every status serving can write has a recorded outcome class | `tests/eval/test_replay_rejections.py::test_r11_every_status_has_a_recorded_outcome_class` |
| R12 | `--min-agreement` takes the lower of model and outcome agreement | `tests/eval/test_replay_rejections.py::test_r12_gate_takes_the_lower_of_the_two`; `tests/eval/test_policy_simulate_cli.py::test_r12_min_agreement_exits_4_on_outcome_disagreement` |
| R13 | An unmigrated table does not break serving, and the documented `ALTER` repairs it | `tests/test_outcome_routing_tokens.py::test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it` |

R2 states exactly what "unchanged" means. When the two counts differ, a served row's route
can legitimately differ from what replay produced before, because the router is now given
the number it was given live.

### 19.4 Results

#### 19.4.1 DGX lab re-run, 2026-10-06 (after the upgrade)

**Setup.** The §18.4.1 deployment after `ALTER TABLE routing_outcomes ADD COLUMN IF NOT
EXISTS routing_tokens INTEGER` and the roll to api `0.27.0` (`main` through PR #86): the same
application (`paper-test`), endpoint (`qwen3-coder-next`, $10/$10 per 1M, declared window
262144), per-request cap ($0.05), the same 24 requests and the same six scenarios. Replay
used `--since 2026-10-06T20:07:37Z` (the first post-upgrade row) and `--min-agreement 0.9`.
Artifacts: `docs/research/results/b60-dgx-lab-2026-10-06/` (`comparison_id
c7d945359f55993d`). One app, one endpoint, one pass: a check that the defect is closed on
real records, not a workload study.

| measurement | value |
|---|---|
| requests rejected live with core `max_cost_usd_per_request` | 7 of 24 |
| of those, rejected by the `current` arm | 7 of 7 (`recorded_rejected_but_routed` = 0) |
| model agreement, rows compared | 1.0 over 17 |
| outcome agreement, rows simulated | 1.0 over 24 (`recorded_routed_but_rejected` = 0) |
| `raise-request-budget` (cap 0.05 → 0.5): newly served | 7 (before the fix: 0) |
| `raise-request-budget`: cost of the rescued rows (admission estimate) | +0.69024 USD on a current-arm total of 0.14204 |
| `cheaper-price` (0.5 / 1.5 per 1M): newly served | 7; both-routed cost −0.134583 USD (−94.7%) |
| `smaller-prompts-0.6` (`token_scale: 0.6`): newly served | 3 of 7; both-routed cost −0.0554 USD (−39.0%) |
| `window-2000`: newly rejected | 2 (`context_window`), 2 overflow hops |
| `rows.estimated_tokens` | 7 (the seven rejected rows) |
| `overflow_contradicted` | 0 |
| the pre-upgrade export from §18.4.1 replayed with the new code | 7 rows under `missing_routing_tokens`, 18 simulated, `raise-request-budget` newly served 0 — the rejections are now excluded and counted instead of replayed as empty prompts |

Both agreements were 1.0 before the fix as well (§18.4.1). What changed is that the outcome
measure now exists and is computed over the rejected rows too: before, the seven rejections
were replayed as routed and nothing counted it.

**`routing_tokens` against the backend's `prompt_tokens`, served rows.** Five distinct
prompt sizes were served (17 rows):

| `routing_tokens` (RouteForge, `len // 4`) | `prompt_tokens` (vLLM, Qwen tokenizer) | ratio |
|---|---|---|
| 16 | 23 | 0.70 |
| 896 | 584 | 1.53 |
| 1776 | 1145 | 1.55 |
| 2656 | 1705 | 1.56 |
| 3536 | 2265 | 1.56 |

Summed over the 17 served rows: 21392 against 13849, a ratio of 1.54. On this text —
repeated English prose, which tokenises unusually well — RouteForge's character-based count
is about half again the backend's for anything past a few hundred tokens, and below it for a
very short prompt (the chat template's overhead).

**Consequence, observed (B64).** The smallest rejected request had `routing_tokens` 5296,
which with the 48-token output estimate priced at $0.05344 against the $0.05 cap. If the 1.55
ratio holds at that size, the backend would have counted about 3400 prompt tokens, a cost of
about $0.035 — under the cap. So the live admission check rejected requests that would
probably have cost less than the limit. This is an inference from the served rows' ratio, not
a measurement: the rejected requests were never sent. It also bounds how far to trust a
rescued row's cost in this section: it is priced from the same over-count.

**What this run does not show.** One synthetic text, one tokenizer, one endpoint. The ratio
will differ for code, for non-English text and for other tokenizers. No route changes (one
endpoint). No `context_overflow`, `backend_failed` or `verify_failed` row occurred, so those
classes are exercised only by the test suite.

#### 19.4.2 Still to measure

| measurement | value |
|---|---|
| `routing_tokens` / `prompt_tokens` on real application traffic (code, mixed languages) | TO MEASURE |
| outcome agreement on an export containing `context_overflow` and `backend_failed` rows | TO MEASURE |
| the same re-run on the Spark | TO MEASURE (the Spark has not had the ALTER or the image) |

### 19.5 Limits

- A recorded `context_overflow` is reproduced only on an endpoint with a declared context
  window, and with RouteForge's token count rather than the backend's.
- A rescued request's cost is the admission estimate. Where the client's output cap is far
  above what it would have generated, the estimate overstates the cost. The same estimate
  prices an already-`ok` row an endpoint reported no usage for (e.g. a stream whose endpoint
  sent no usage block) — it is replayed, not a bill either.
- Nothing here replays failover, escalation, breaker state or daily-spend and RPM admission.
  A request refused for its daily budget or its rate limit writes no outcome row at all.
- A `backend_failed` row is costed as if the call had succeeded.

## 20. Uncertainty ranges: a paired, stratified bootstrap over replayed rows (P3c-3)

Every figure in §15–§19 is a point estimate from one replay of one export. §19.4.1 reports
"cost −39.0%" from 24 rows; nothing on the page distinguishes that from the same number on
two million. This section records the range that now goes beside each figure, and what it
does and does not mean. Design: `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md`.
Reference: [`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §10. Code:
`app/eval/uncertainty.py`. **Not deployed anywhere and not run on real records**: the only
numbers below are synthetic, and every measurement on recorded traffic is to be made (§20.6).
The whole-branch review changed the stratification rule and several leaves before the slice
merged; §20.7 records what was found and what was done, and the text here describes the
method as it now is.

### 20.1 The model

Let the export hold rows `r_1 … r_n`, grouped by application into groups of size `n_a`, and
let `m` be `--min-rows` (default 30). The **strata** are: one stratum `S_a` for every
application with `n_a ≥ m`, and **one pooled stratum** `S_0` holding all rows of the
applications with `n_a < m`. Replay is a deterministic map `r ↦ (c(r), p(r))`: the row's
result under the current config and under the proposal. A figure is a statistic `T` of the
multiset `{(c(r), p(r))}` — a sum (cost), a count, a ratio of counts (a share, the on-prem
share), a ratio of sums (the quality mean), a quantile (latency p50, p95), or a difference of
two of these between the arms.

A bootstrap replicate `b` draws, for each stratum, as many positions as it has rows, with
replacement, and evaluates `T` on the union. An application that is its own stratum also has
`T` evaluated on its own draw. The reported range is the equal-tailed percentile interval of
`T` over `N` replicates.

- **Rows are resampled, not re-simulated.** `(c(r), p(r))` is computed once per row. A
  replicate costs a draw and a sum, not a routing decision.
- **Stratified.** Each stratum's size is fixed, so the figure of an application that is its
  own stratum is defined in every replicate, and the mix between those applications is
  conditioned on. A pooled application has no range of its own (it is below `m`).
- **Why the small applications are pooled.** A bootstrap of `n_s` rows draws from the
  empirical distribution, whose variance is `(n_s − 1)/n_s` times the unbiased estimate. For
  a total over `S` strata of equal variance the bootstrap therefore reproduces `(n − S)/n` of
  the variance: nearly all of it with a few large strata, two thirds with strata of three
  rows, **none** with strata of one row. Pooling the small applications bounds `S`. The
  output states `S`, the smallest stratum and `(n − S)/n` (`totals_strata`,
  `variance_factor`), labels the factor analytic, and does **not** rescale the range by it.
- **Paired.** A drawn row contributes `c(r)` and `p(r)` together. So `T_p − T_c` in a
  replicate is a difference over the same rows, and its range is the range of the paired
  difference. If `c(r) = p(r)` for every row, the difference is exactly `0` in every
  replicate.
- **Common random numbers across counterfactuals.** The positions drawn for stratum `a` in
  replicate `b` are a function of `(seed, b, a, n_a)` alone, and those of the pooled stratum
  a function of `(seed, b, |S_0|)` alone. Every scenario of a comparison, and every
  shadow-price relaxation, is replayed over the same rows in the same order, so all of them
  are evaluated on the same replicates without any of them being held in memory with another.
  Consequences that are pinned by tests: a scenario's ranges do not depend on which other
  scenarios are in the file; a relaxation scoped to one application has the range of that
  application's bucket under the proposal that makes the same edit; and the one
  endpoint-scoped relaxation, which re-simulates every row, has the range of that proposal's
  totals.

**What it estimates.** The sampling variability of `T` if the rows of each stratum are
exchangeable, independent draws from a fixed distribution, **for this set of applications**.
Neither assumption holds for LLM traffic. An agent loop sends a sequence of related calls,
each its own row; traffic changes with releases and time of day. Both make the true
variability larger than the range. The design does not estimate by how much (§20.7). And it
is silent about variation between applications: §20.6.1 measures how much that can matter.

### 20.2 What it deliberately is not

The range is conditional on the replay being right. Four things are outside it, named in
every report (`UNCERTAINTY_CAVEAT`) and in the output (`not_covered`):

| Outside the range | Evidence that it matters here |
|---|---|
| replay model error — first decision only, frozen signals, predicted overflow | §15.2, §18.2 |
| the admission estimate for rows with no backend token count (rejected, failed before a response, or served with no reported usage; counted per bucket as `estimated_token_rows`) | §19.4.1: RouteForge's count was 1.54× the backend's on 17 served rows, and the seven rescued rows are priced from it |
| inputs the row does not record | B55 (§15.4) and B60 (§19): each was invisible until a figure was plainly wrong |
| traffic shift | not measured |

A fifth follows from the replay itself: a routed row is given the latency of its endpoint
(asserted, observed median, signal or tier default), so latency has as many distinct values
as there are endpoints. The p50 and p95 are computed exactly in every replicate, and their
range reflects only which endpoints the resampled rows used. On a one-endpoint deployment a
latency range is a single value. That is a correct statement about the replay, and an
uninformative one about latency.

The point worth making in the paper: **a narrow range on a biased replay is still biased.**
The range and the baseline agreement answer different questions, and a report needs both.

### 20.3 Quality labels

A calibrated quality (§14) is the mean of a `Beta(α, β)` posterior. Using the mean as a point
discards the one stated uncertainty the system has about quality.

- The snapshot exports the mean and the `set_id`, not `α` and `β`; nor does the database hold
  them. They are in the P2b-3 recommendation file, whose `set_id` is a hash of its own content
  — the hash the admin API verified before applying the set. So the file authenticates itself
  against the snapshot, and no serving change is needed.
- In replicate `b`, each calibrated `(model, workload)` unit in use gets **one** draw from its
  posterior, shared by every row that resolves to it and by both arms. The unknown is the
  unit's quality, not each request's.
- Because the draw is shared between arms, a row that stays on the same model contributes
  exactly `0` to the quality delta. Label uncertainty enters a quality **delta** only through
  rows that change model.
- A published or tier-default quality has no stated uncertainty and is given none. The output
  reports the coverage (`quality_posterior`: routed, calibrated, with a posterior) so a reader
  can see how much of a quality range is labels and how much is only the route mix.

This is a partial answer to handoff open question 10 ("how will the simulator estimate quality
for routes not historically selected"): it does not estimate it better, it says how uncertain
the existing estimate is — and for a route with no calibration, that the answer is a prior
with no interval at all.

### 20.4 Zero counts, and two honest non-answers

**Zero counts.** If no row is newly rejected, no resample contains one, and the bootstrap
range is `[0, 0]` at any sample size. The bootstrap cannot express "we looked at 24 rows and
saw none". The output therefore adds an analytic bound for a zero count:
`u = 1 − (1 − P)^(1/n)`, the largest rate at which `n` independent rows would all be clean
with probability `1 − P`. For a stratified sample with per-app rates `p_a`, the chance of an
all-clean sample is `∏(1 − p_a)^(n_a) ≤ (1 − p̄)^n` by Jensen's inequality (`ln(1 − x)` is
concave), where `p̄` is the mix-weighted rate; so the bound holds for the totals as it does
for one app. At `P = 0.90` it is about `2.3/n`: 9.2% for 24 rows.

Three things about that bound are stated wherever it is printed. It is **one-sided**: an
upper limit at level `P`. It bounds the rate **under the replay**, not the live rate — a
replay that wrongly serves rows the live system would reject shows zero at any sample size,
and that is bias, not sampling. And it assumes independent rows. The key (`zero_bound`) is on
each of the three count shares, on a shadow-price relaxation's `newly_rejected` (over the
rows in scope) and on an on-prem share of exactly 0 (over that arm's routed rows). The two
tables that print a count as `0 [0, 0]` — the scenario comparison and the shadow-price
ranges — are followed by a fixed sentence saying it is not zero risk.

**Saturated shares.** The mirror case is a share of exactly 1 — every row of a bucket newly
rejected, every routed row on-prem — whose range is `[1, 1]` at any sample size. It carries
`one_bound = (1 − P)^(1/n)`, the same statement about the complement. One caution belongs
with it: an `onprem_only` application's on-prem share is 1 **by construction** (a cloud
provider is filtered out before anything is scored), not by sampling. The bound describes
sampling a rate and says nothing about a guarantee; the report says so in the same sentence.

**Figures over the routed rows.** Latency, the quality mean and the on-prem share are
statistics of the rows an arm routed. A bucket of 100 rows whose proposed arm routes 10 has a
quality mean of 10 values. The insufficient-data rule is therefore applied to the routed
count for those leaves (and for the deltas they enter, on the smaller count): below
`--min-rows` they report `insufficient data (n = k routed)` and no range (`insufficient_data`
and `n` on the leaf). Each bucket states both arms' routed counts (`routed`), and how many of
its rows have no backend token count (`estimated_token_rows`, the rows the admission-estimate
exclusion of §20.2 is about).

**A point outside its own range.** A percentile interval need not contain the point estimate
(a very skewed posterior, a quantile of a discrete distribution, one replicate). When it does
not, the leaf is flagged (`point_outside`) and counted (`points_outside_interval`, in each
results block and in `shadow_prices.uncertainty`); the range is not widened to hide it. In
the two tables that print `point [lo, hi]` the cell is marked `*` and footnoted.

**A figure that does not always exist.** An arm with one routed row has no latency in a
replicate that does not draw it. An interval over the replicates where the figure exists
would be conditional on that; so the figure gets no range and the output says in how many
replicates it was undefined.

**Too few rows.** Below `--min-rows` (default 30) a bucket reports `insufficient data (n = k)`
and nothing else. Thirty is a convention.

**Too few replicates.** With fewer than `2/(1 − P)` replicates — 20 at the default level —
the percentile interval leaves no value out on either side: the range is the replicates'
minimum and maximum. The output flags it (`interval_is_min_max`), the report says it in a
sentence and the CLI warns. The index `k = floor((1 − P)/2 · N)` is also clamped so that the
two ends never cross at a very small level.

### 20.5 Invariants, and the test that pins each

U1–U13 are the design's; U14–U17 were added by the final review (§20.7).

| # | Invariant | Pinned by |
|---|---|---|
| U1 | Without `--uncertainty` every output file of every mode is byte-identical to the tree before this slice; with it, only `uncertainty`, `inputs.uncertainty` and `shadow_prices.uncertainty` are added | `tests/eval/test_uncertainty_golden.py::test_u1_output_without_the_flag_is_byte_identical` (two cases × four modes; both cases' files were written from the tree before any product code existed — the second from a `git archive` of base commit `566acc7`), `::test_the_golden_run_is_not_trivial`, `::test_the_rejections_case_exercises_rejections`, `::test_regenerating_in_a_tree_with_p3c3_code_is_refused`; `tests/eval/test_uncertainty_cli.py::test_u1_the_flag_adds_keys_and_changes_no_existing_value`; `tests/eval/test_uncertainty_scenarios.py::test_u1_a_plan_adds_one_key_and_changes_no_other`; `tests/eval/test_uncertainty_shadow.py::test_u1_a_plan_adds_one_key_to_the_block` |
| U2 | Deterministic with the flag; the global `random` state is not read or moved | `tests/eval/test_uncertainty.py::test_u2_same_plan_same_block_and_the_seed_changes_it`, `::test_u2_the_global_random_state_is_not_read_or_moved`; `tests/eval/test_uncertainty_cli.py::test_u2_the_flagged_run_is_byte_identical_twice`; `tests/eval/test_uncertainty_shadow.py::test_u2_shadow_ranges_are_deterministic` |
| U3 | Every point is inside its range, or the leaf is flagged; nothing is widened; `lo ≤ hi` at every level; a point outside its range is marked wherever the range is printed; a range that is only a minimum and a maximum says so | `tests/eval/test_uncertainty.py::test_u3_every_point_is_inside_its_range_or_is_flagged`, `::test_u3_one_replicate_can_leave_the_point_outside_and_says_so`; `tests/eval/test_uncertainty_shadow.py::test_u3_every_shadow_point_is_inside_its_range_or_flagged`; `tests/eval/test_uncertainty_leaves.py::test_f3_interval_of_never_returns_an_inverted_pair`, `::test_f3_too_few_replicates_for_the_level_is_said_in_the_head`, `::test_f3_the_report_and_stderr_say_a_range_is_a_min_max`, `::test_f3_with_range_marks_a_point_outside_and_an_insufficient_leaf`, `::test_f3_the_comparison_table_marks_points_outside_and_explains_the_mark`, `::test_f3_shadow_ranges_count_and_mark_points_outside` |
| U4 | Paired: a no-op proposal has every delta range exactly `[0, 0]`, with pooled applications present too; the same draws across scopes | `tests/eval/test_uncertainty.py::test_u4_noop_proposal_has_every_delta_range_exactly_zero`, `::test_u4_an_app_is_drawn_the_same_way_alone_and_inside_the_whole_export`; `tests/eval/test_uncertainty_quality.py::test_u4_one_unit_in_both_arms_cancels_in_the_delta`; `tests/eval/test_uncertainty_scenarios.py::test_u4_an_empty_scenario_has_every_range_at_zero`, `::test_u4_a_scenario_gets_the_same_ranges_alone_and_among_others`; `tests/eval/test_uncertainty_shadow.py::test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal`; `tests/eval/test_uncertainty_strata.py::test_f1_noop_is_exactly_zero_with_pooled_apps`, `::test_f1_a_scenario_gets_the_same_ranges_alone_and_among_others_with_small_apps`, `::test_f1_an_endpoint_relaxation_equals_the_equivalent_proposals_totals` |
| U5 | Ranges narrow as the rows are replicated 1×, 4×, 16× | `tests/eval/test_uncertainty.py::test_u5_ranges_narrow_as_the_rows_are_replicated` |
| U6 | A replicate draws exactly `n` rows for every app with its own stratum and exactly the pooled count for the pool; an app with its own stratum is in every replicate; every replicate value is `_bucket_stats` over the rows the two seed formulas draw | `tests/eval/test_uncertainty.py::test_u6_every_replicate_draws_exactly_the_apps_row_count`, `::test_u6_a_one_row_app_is_present_in_every_replicate`; `tests/eval/test_uncertainty_strata.py::test_f1_every_replicate_is_the_rows_the_seed_formulas_draw` (brute force, three values of `--min-rows`) |
| U7 | Below `--min-rows`: `insufficient_data` and `n`, no range; the reports print `insufficient data (n = k)`. A latency, quality or on-prem-share leaf of an arm that routed fewer than `--min-rows` rows gets the same, on the routed count | `tests/eval/test_uncertainty.py::test_u7_a_small_app_gets_no_range_and_says_how_many_rows`, `::test_u7_too_few_rows_in_all_draws_nothing`, `::test_u7_no_rows_at_all`; `tests/eval/test_uncertainty_report.py::test_u7_report_prints_insufficient_data_instead_of_a_range`; `tests/eval/test_uncertainty_scenarios.py::test_u7_too_few_rows_prints_insufficient_data_and_no_brackets`; `tests/eval/test_uncertainty_shadow.py::test_u7_a_relaxation_with_few_rows_in_scope_gets_no_range`; `tests/eval/test_uncertainty_leaves.py::test_f2_ten_routed_rows_of_a_hundred_get_no_range_for_conditional_figures`, `::test_f2_forty_routed_rows_of_a_hundred_get_a_range`, `::test_f2_the_threshold_is_the_plans_and_is_inclusive`, `::test_f2_an_arm_that_routes_nothing_has_no_range_and_no_point`, `::test_f2_the_report_prints_how_many_routed_rows_there_were` |
| U8 | Quality-label uncertainty exists only where a posterior does | `tests/eval/test_uncertainty_quality.py::test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`, `::test_u8_without_the_file_a_calibrated_quality_stays_a_point`, `::test_u8_a_tier_default_quality_has_no_label_uncertainty`, `::test_u8_coverage_counts_a_mixed_export`, `::test_a_file_never_attaches_to_a_score_it_did_not_produce` |
| U9 | The caveat is the spec's text and opens every ranges section; `not_covered` has the five ids; the rows behind `admission_estimate_error` are counted beside it | `tests/eval/test_uncertainty_report.py::test_u9_caveat_is_the_spec_text`, `::test_u9_not_covered_names_every_excluded_source`, `::test_u9_section_opens_with_the_caveat_and_says_how_ranges_were_made`; `tests/eval/test_uncertainty_leaves.py::test_f6_each_bucket_counts_its_rows_with_no_backend_token_count` |
| U10 | The plan is in `inputs`; ids tell plans apart | `tests/eval/test_uncertainty_cli.py::test_u10_plan_values_are_in_inputs_with_their_defaults`, `::test_u10_the_id_tells_plans_apart` |
| U11 | No key ranks; file order is kept; a security range keeps the protection label | `tests/eval/test_uncertainty.py::test_u11_no_key_ranks_or_recommends`; `tests/eval/test_uncertainty_report.py::test_u11_report_text_does_not_rank_or_recommend`; `tests/eval/test_uncertainty_scenarios.py::test_u11_ranges_add_no_ranking_key_and_keep_file_order`; `tests/eval/test_uncertainty_shadow.py::test_u11_a_security_range_keeps_the_protection_label` |
| U12 | No serving module imports `app.eval` | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` |
| U13 | The engine's figures are the document's figures | `tests/eval/test_uncertainty.py::test_engine_figures_equal_bucket_stats`, `::test_leaf_point_equals_the_figure_the_document_carries`; `tests/eval/test_uncertainty_scenarios.py::test_every_table_range_is_about_the_figure_in_that_column`; `tests/eval/test_uncertainty_strata.py::test_f1_every_replicate_is_the_rows_the_seed_formulas_draw` |
| U14 | The totals are resampled in strata — each app at or above `--min-rows` on its own, the rest pooled — and the block says which (`totals_strata`) and what share of variance that keeps (`variance_factor`, analytic); many small apps give the totals the width of the same rows as one app; with `--min-rows 1` the disclosure reads 0 | `tests/eval/test_uncertainty_strata.py::test_f1_many_small_apps_give_the_totals_the_width_of_one_app`, `::test_f1_min_rows_1_pools_nothing_and_the_block_says_the_width_is_zero`, `::test_f1_totals_strata_counts_a_mixed_export`, `::test_f1_nothing_drawn_has_no_strata`, `::test_f1_the_report_states_the_strata_and_that_ranges_are_not_corrected` |
| U15 | A one-value range carries its one-sided bound: `zero_bound` for a count of 0 (a share, a relaxation's `newly_rejected`, an on-prem share), `one_bound` for a share of 1; the tables that print `0 [0, 0]` say it is not zero risk | `tests/eval/test_uncertainty.py::test_a_zero_count_is_zero_in_every_replicate_and_carries_a_bound`, `::test_zero_bound_values`; `tests/eval/test_uncertainty_report.py::test_zero_newly_rejected_is_never_presented_as_no_risk`; `tests/eval/test_uncertainty_leaves.py::test_f3_a_relaxation_with_no_new_rejection_carries_the_zero_bound`, `::test_f3_the_comparison_table_is_followed_by_the_zero_sentence`, `::test_f3_the_zero_count_note_says_one_sided_and_replayed`, `::test_f3_a_share_of_one_carries_the_lower_bound`, `::test_f3_an_on_prem_share_of_zero_carries_the_zero_bound`, `::test_f3_a_share_strictly_between_has_neither_bound`, `::test_f3_leaf_bounds` |
| U16 | Memory does not grow with the number of applications, and the reordering changed nothing: for an export with no pooled app every leaf equals the one the first build wrote; each app's samples are reduced as that app finishes; a totals-only caller computes no per-app figures | `tests/eval/test_uncertainty_strata.py::test_f4_an_export_with_only_large_apps_gives_the_leaves_it_gave_before` (against a block written by the engine at `4344546`), `::test_f4_each_apps_samples_are_reduced_as_soon_as_that_app_is_drawn`, `::test_f4_totals_only_computes_no_per_app_figures` |
| U17 | Before each bootstrap phase the CLI says on stderr what it will cost, from a row term and a per-stratum term; never without the flag, never when the `--min-agreement` gate refuses; a report names at most ten apps with insufficient data, after their count | `tests/eval/test_uncertainty_cost.py::test_f4_the_estimate_has_a_row_term_and_a_per_application_term`, `::test_f4_pooled_applications_cost_one_stratum`, `::test_f4_nothing_is_drawn_below_min_rows`, `::test_f4_cost_line_wording`, `::test_f4_the_cli_says_what_each_bootstrap_will_cost`, `::test_f4_no_cost_line_without_the_flag_or_when_the_gate_refuses`, `::test_f4_the_shadow_estimate_is_one_pass_per_relaxation_over_its_rows`, `::test_f4_an_endpoint_relaxation_is_a_pass_over_every_row`, `::test_f4_many_insufficient_apps_are_counted_and_the_first_ten_named`, `::test_f4_up_to_ten_insufficient_apps_are_all_named` |

U1 is pinned against files generated from the tree **before** any code of this slice
existed, not against a second run of the new code. A test that compares two runs of the same
build cannot see a change both runs share. The first golden case never rejected a row in
either arm, so the pin did not cover the rejection paths; the second case does, and was
generated from the same base commit rather than from the branch for the same reason.

### 20.6 Results

#### 20.6.1 Synthetic only (2026-10-06)

Nothing below is a measurement of real traffic. The rows are built in memory, directly as
replay results; the purpose is to show the method costs what it should, reproduces a known
interval, and to measure the one defect the review found. Machine for everything in this
subsection: Python 3.12.3, one core of an Intel Core Ultra 7 165U, with other processes
running (load average about 3 of 14 cores). "First build" is the engine at commit `4344546`;
"as amended" is the engine after the review's fixes. Both were run by the same script, the
tree under test selected with `PYTHONPATH`.

**Stratification and the totals range** (the review's finding, §20.7). Five shapes of the
same size, 240 rows: lognormal costs (`μ = −5`, `σ = 0.8`), 30% of rows moved to a model at
0.2× the cost, 4% newly rejected, every application drawn from the same distribution. For
each shape, 30 datasets (seeds 1000–1029), 1000 replicates, `--min-rows 30`. The figure is
the **mean over the 30 datasets of the bootstrap 90% width of a total, divided by
`2 · 1.645 · √n · s`** — the normal-theory width of a sum of `n` independent rows, `s` being
that dataset's sample standard deviation of the per-row value. 1.00 is a range of the right
width; the standard deviation over the 30 datasets is in brackets.

| shape (apps × rows) | theory for one stratum per app, `√((n − S)/n)` | first build: `cost_usd.current` | as amended: `cost_usd.current` | as amended: `cost_usd.delta` | as amended: `newly_rejected` |
|---|---|---|---|---|---|
| 1 × 240 | 0.998 | 0.997 (0.033) | 0.997 (0.033) | 0.994 (0.029) | 0.993 (0.049) |
| 8 × 30 | 0.983 | 0.981 (0.031) | 0.981 (0.031) | 0.986 (0.024) | 0.972 (0.043) |
| 80 × 3 | 0.816 | 0.818 (0.035) | 0.992 (0.029) | 0.986 (0.029) | 0.985 (0.055) |
| 120 × 2 | 0.707 | 0.697 (0.039) | 0.992 (0.029) | 0.986 (0.029) | 0.985 (0.055) |
| 240 × 1 | 0.000 | 0.000 (0.000) | 0.992 (0.029) | 0.986 (0.029) | 0.985 (0.055) |

The first build follows the theory for one stratum per application to within 0.01,
including the range of width **zero** when every application has one row. The first build's
`cost_usd.delta` and `newly_rejected` columns (not shown) are within 0.02 of its
`cost_usd.current` column. As amended, the last three shapes pool every application and are
the same rows drawn with the same pool seed, so their three rows are one measurement, not
three. The first two shapes pool nothing and are unchanged, as they should be.

**The same, with applications that differ.** Each application's costs are multiplied by
`exp(N(0, 0.7))`. Two different questions, 200 datasets per cell, 1000 replicates, the figure
`cost_usd.current` of the totals:

- **Fixed mix (FM).** The multipliers are fixed; only rows are redrawn. This is the question
  the method answers. Reference: normal theory with the exact variance of the total.
- **Applications redrawn (RA).** The multipliers are redrawn with every dataset — the
  applications are themselves a sample (of users, of tenants). Reference: the 5%–95% width of
  the total over the 200 datasets. No rule in this design covers this.

Each cell is `mean width ÷ reference` and, in brackets, how often the 90% range contained the
true expected total.

| shape | FM, first build | FM, as amended | RA, first build | RA, as amended |
|---|---|---|---|---|
| 1 × 240 | 0.98 (0.88) | 0.98 (0.88) | 0.09 (0.09) | 0.09 (0.09) |
| 8 × 30 | 0.97 (0.90) | 0.97 (0.90) | 0.29 (0.31) | 0.29 (0.31) |
| 80 × 3 | 0.81 (0.83) | 1.11 (0.93) | 0.56 (0.62) | 0.81 (0.80) |
| 240 × 1 | 0.00 (0.00) | 1.14 (0.93) | 0.00 (0.00) | 0.97 (0.88) |
| 2 × 60 + 40 × 3 | 0.89 (0.90) | 1.05 (0.94) | 0.24 (0.31) | 0.29 (0.36) |
| sizes 50, 34, 34, 30, 21, 13, 13, 8, 8, 5, 5, 5, 3, 3, 3, 2, 2, 1 | 0.94 (0.88) | 1.00 (0.89) | 0.26 (0.32) | 0.29 (0.35) |

Three readings. (1) Under a fixed mix the amended rule is within a few percent where the
applications are large or alike, and **over**-wide by up to 14% where many small applications
differ: pooling treats their rows as exchangeable and so counts the differences between them
as noise. That errs on the cautious side, and it is the price of not printing a width of
zero. (2) Where some applications are large, the range is 0.09 to 0.29 of the width that
redrawing the applications would give, under either rule: the method conditions on the
applications it saw, and a reader who wants to generalise to other applications must not use
these ranges for that. (3) Only when every application is small does pooling happen to come
close to the applications-redrawn width, because then the pool is nearly an unstratified
resample; that is a coincidence of the shape, not a property to rely on.

**Cost of the bootstrap.** `results_block` on synthetic rows, including the one-off setup;
peak RSS is the growth of the process's `ru_maxrss` across the call; each shape in a fresh
process.

| shape (apps × rows) | replicates | first build | as amended |
|---|---|---|---|
| 1,000 × 40 | 1000 | 44.4 s, peak RSS +669 MB | 38.4 s, peak RSS +20 MB |
| 10,000 × 3 | 1000 | 166.0 s, +12 MB | 10.1 s, +13 MB |
| 3 × 33,334 | 20 | 93 ms per replicate | 99 ms per replicate |
| 100,000 × 3 | 5 | 14.3 s | 5.2 s |

The cost is `replicates × (rows × c_row + strata × c_stratum)`: linear in replicates × rows,
**plus a term per application that is its own stratum** (a freshly seeded generator and one
set of figures per replicate). 1,000 applications of 40 rows cost about as much as 100,000
rows in three applications. The first build held every application's replicate values until
the end, which is the 669 MB; the amended engine loops strata outside and replicates inside
and reduces each application as it finishes. Pooling is also what took 10,000 three-row
applications from 166 s to 10 s: they are one stratum now. The estimate the CLI prints before
a bootstrap uses `c_row = 0.4 µs` and `c_stratum = 30 µs`, fitted by eye to runs like these;
it predicts 46 s and 12 s for the first two rows of the table, and it is labelled rough.

An earlier measurement, made on a quiet machine with three applications of which one was
untouched by the proposal (so one cost column instead of four): 100,000 rows × 1000
replicates in 25.2 s (27.5 s on a second run), and 1,000,000 rows × 50 replicates in 36.0 s.
That is the favourable case. No `numpy`.

**A known interval is reproduced** (a test assertion, not a study): with every row served by
one calibrated unit whose posterior is `Beta(41, 9)`, row resampling cannot move the quality
mean, and 4000 replicates give a 90% range within 0.01 of the Beta's own 5% and 95% quantiles
(`test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`).

**Width against sample size** (a test assertion): on the suite's synthetic export, the
relative width of the cost delta's range at 16× the rows is under half its width at 1×
(`test_u5_ranges_narrow_as_the_rows_are_replicated`); `1/√16 = 0.25` is the expected ratio.

#### 20.6.2 First live run — DGX lab, 2026-10-07

**Setup.** The §19.4.1 deployment at api `0.28.0` (`main` through PR #88): application
`paper-test`, endpoint `qwen3-coder-next` ($10/$10 per 1M), cap $0.05, the six scenarios of
§18.4.1. Two exports, both cut with `--since 2026-10-06T20:07:37Z`:

- **24 rows** — the single pass of §19.4.1.
- **96 rows** — that pass plus three more identical passes of the same 24 requests.

Defaults throughout: 1000 resamples, 0.90 level, seed 0, `--min-rows 30`. Artifacts for the
96-row run: `docs/research/results/p3c3-dgx-lab-2026-10-07/` (`comparison_id
1e1e084be8fd9d6e`).

**Read this first.** The 96 rows are four repeats of one fixed set of 24 requests. They are
not 96 independent draws from a workload, so these ranges describe resampling a replicated
design, not the variability of real traffic. The run shows that the feature works end to end
on real records and what its output looks like; it says nothing about coverage.

| measurement | value |
|---|---|
| 24-row export at defaults | every scenario and the one application print `insufficient data (n = 24)`; no range anywhere |
| 96-row export: model agreement / outcome agreement | 1.0 over 68 / 1.0 over 96 (28 recorded rejections, all rejected in replay) |
| 96 rows, totals strata | 1 own stratum, 0 pooled; `variance_factor` 0.989583 |
| `no-change` | every delta exactly `[0, 0]` (the paired property, on live data) |
| `raise-request-budget`: newly served | 28 `[21, 35]` |
| `cheaper-price`: newly served; like-for-like cost | 28 `[21, 35]`; −0.538255 USD `[−0.66033, −0.425937]` |
| `smaller-prompts-0.6`: newly served; like-for-like cost | 12 `[7, 18]`; −0.2216 USD `[−0.27317, −0.17439]` |
| `window-2000`: newly rejected | 8 `[4, 13]` |
| `slower-5s`: Δp50 | 4860.07 ms `[4860.07, 4860.07]` — a one-value range: one endpoint, one asserted latency (the caveat's `per_request_latency`) |
| pre-run cost line, `--scenarios --uncertainty`, 96 rows × 6 scenarios | printed "1000 resamples x 96 rows x 6 passes: under 1 s"; wall time was not measured |

What the numbers do show: a count such as "28 newly served" on 96 rows carries a range of
about ±25%, which is the honest size of a result read off a hundred requests; and the two
one-value cases (`[0, 0]` for a no-op, a single asserted latency) are printed as what they
are rather than as precision.

#### 20.6.3 Still to measure

| measurement | value |
|---|---|
| the same scenarios on a persona-generated export of 500, 5,000 and 50,000 rows: range width against `1/√n` | TO MEASURE |
| empirical coverage: split a large export into disjoint halves many times; how often does the half-A 90% range contain the half-B point estimate | TO MEASURE |
| coverage under dependence: the same, on traffic generated by agent loops (many related calls per task) | TO MEASURE |
| quality range with and without `--quality-posteriors` after a real calibration round | TO MEASURE |
| a multi-application export with pooled applications (`totals_strata.pooled_apps > 0`) on real traffic | TO MEASURE |

### 20.7 Limits, and what the review changed

**The finding.** The first build drew every application as its own stratum. A bootstrap of a
stratum reproduces `(n_s − 1)/n_s` of its variance, so the **totals** range was too narrow
whenever the export had many small applications — by `√((n − S)/n)`, and to a width of
exactly zero when every application had one row — although each application's own range was
right. A range of width zero printed beside a total reads as certainty. §20.6.1 has the
measurement: 0.997, 0.981, 0.818, 0.697 and 0.000 of the normal-theory width for 240 rows as
1 × 240, 8 × 30, 80 × 3, 120 × 2 and 240 × 1 (synthetic, 30 datasets each).

**The fix.** Applications below `--min-rows` are one pooled stratum for the totals; the three
affected shapes then measure 0.992. The pool has a seed formula of its own, a function of the
seed, the replicate and the pooled row count, so every pairing property survives (§20.1,
U4). What stratification still leaves out is stated in every block (`totals_strata`,
`variance_factor`) and in every report with the words "ranges are not corrected for it". A
correction was not applied because the factor assumes equal variances across strata, which
the data need not satisfy; a disclosed approximation was chosen over a silently rescaled
interval.

**What the review also changed**, each for the same reason — a figure that looked more
certain than it was: a mean of ten routed rows no longer gets a range (§20.4); a count of 0
and a share of 1 carry a one-sided bound and the tables that print them say so (§20.4); a
point outside its range is marked in every table; too few replicates for the level is
flagged; and the rows whose size is RouteForge's own estimate are counted beside the caveat
that names them.

**Limits that remain.**

- **Variation between applications is not covered.** The method conditions on the
  applications in the export. If they are themselves a sample, the range is far too narrow
  whenever some applications are large: 0.09 to 0.29 of the applications-redrawn width in
  §20.6.1 (synthetic). A two-stage bootstrap that also resamples applications would address
  it; it would break the pairing with per-application shadow prices and is not built.
- **Rows are assumed independent within a stratum.** LLM traffic does not satisfy it. A
  cluster bootstrap over client sessions or agent tasks would be the right unit; the record
  has no session identifier (the same gap R3 in `arxiv.md` §5 needs closed).
- **Pooling treats the small applications' rows as exchangeable.** Where those applications
  differ in mean the pooled totals range is wider than the fixed-mix truth, by up to 14% in
  §20.6.1 (synthetic).
- **Stratification still understates variance by `(n − S)/n`**, disclosed and uncorrected. At
  the default `--min-rows 30` the factor is at least `29/31 ≈ 0.94` whenever something is
  drawn (the worst case is one 30-row application and a one-row pool); with `--min-rows 1`
  it can be 0.
- **Cost has a per-application term**, and is otherwise linear in replicates × rows
  (§20.6.1). Memory no longer grows with the number of applications.
- A day-block bootstrap was considered and left out: exports are short, blocks would be few,
  and `ts` is optional.
- The percentile interval is neither bias-corrected nor studentized, and with fewer than
  `2/(1 − P)` replicates it is a minimum and a maximum (flagged).
- `--min-rows 30` is a convention.
- Latency ranges carry no per-request variation (§20.2).
- `zero_bound` and `one_bound` are one-sided, assume independent rows, and bound the rate
  under the replay, not the live rate.
- Reproducibility is per Python minor version.
- Every number in §20.6.1 is synthetic. Coverage on recorded traffic is **TO MEASURE**
  (§20.6.3).

## 21. Capacity: stated limits against replayed demand (P3c-4)

§15–§20 predict where requests would go and what that costs, with every endpoint treated as
having unlimited room. The handoff's own headline example is the case that misses: a policy
that is stricter, costs more, and — in the handoff's illustration, not a measurement — puts
the on-prem endpoint at 230% of what it can serve (§25, §31). This section records the model
that now reports the last of those, **and the first model that was built and withdrawn
before any figure was quoted** (§21.5.2): its numbers were arithmetically what its
specification said and were still unsafe to cite. Design:
`docs/superpowers/specs/2026-10-07-p3c4-capacity-design.md` (the body is the model as built;
§15 lists the changes). Reference:
[`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §11. Code:
`app/eval/capacity.py`. **Not deployed anywhere and not run on real records**: §21.5 holds
synthetic verification only, labelled, and every measurement on recorded traffic is to be
made. A second review, of the whole branch, then found four ways the revised model's
**presentation** could still mislead; §21.5.5 records those, and the text below is the model
after them.

### 21.1 The model

Let the export hold simulated rows `r`, each with a write time `ts(r)`, a recorded latency
`ℓ(r)` and an application `app(r)`. Replay is a deterministic map from a row and an arm
`a ∈ {current, proposed}` to either a rejection or a served endpoint `e_a(r)` with the token
counts the call was priced at, `p_a(r)` (prompt) and `c_a(r)` (completion). All times are
integer milliseconds.

**Arrival.** `ts` is the time the record was written, which is the end of the request. The
arrival estimate is `t(r) = ts(r) − ℓ(r)`, a property of the request and so the same in both
arms. A latency that is missing, negative, non-finite or above 24 hours is unusable and
subtracts nothing. With bucket length `T`, the row's arrival bucket is `b(r) = ⌊t(r) / T⌋`,
aligned to the Unix epoch.

**Service time.** `S_a(r)` is, in this order:

1. the latency the proposal **asserts** for `e_a(r)`, when it asserts one (proposed arm);
2. `ℓ(r)`, the row's **own recorded** latency, when `e_a(r)` is the endpoint that served the
   row alone — recorded status `ok`, exactly one recorded attempt, chosen = `e_a(r)`;
3. the **mean** of `ℓ` over the simulated rows that `e_a(r)` served alone, when there are
   any;
4. replay's latency for the endpoint (its observed median, else the snapshot's signal, else
   the tier default).

The row is in service on `e_a(r)` over `I_a(r) = [t(r), t(r) + S_a(r))`.

**No service time.** When rule 4 yields the tier default, `S_a(r)` is a constant of the
routing score (200 / 500 / 1000 ms by tier in the shipped configuration), not an estimate of
anything about the endpoint. For such an endpoint and arm the concurrency demand `A_a(e,·)`
is **undefined**, and so is the utilisation of a stated `max_concurrency`: the output
carries the state `not_computed: "tier_default_service_time"` and no number, and the arm's
headline maximum is taken over the other limits and says which endpoint it left out. `N`,
`K` and `O` below do not involve `S` and are unaffected.

**Demand.** With `v_a(r)` the volume scale of the row's application in arm `a` (§21.2), the
four demands of endpoint `e` in bucket `b` are

```
requests/min        N_a(e,b) = (60/T) · Σ_{r : e_a(r)=e, b(r)=b} v_a(r)
tokens/min          K_a(e,b) = (60/T) · Σ_{r : e_a(r)=e, b(r)=b} v_a(r) · (p_a(r) + c_a(r))
output tokens/min   O_a(e,b) = (60/T) · Σ_{r : e_a(r)=e, b(r)=b} v_a(r) · c_a(r)
offered concurrency A_a(e,b) = (1/T)  · Σ_{r : e_a(r)=e}          v_a(r) · |I_a(r) ∩ b|
```

Requests and tokens are counted where the request **arrives** (a rate limit counts
admissions). Service time is counted where it is **spent**: `|I ∩ b| ≤ T`, so one request
contributes at most 1 to `A` in any bucket. `A_a(e,b)` is the time-average over the bucket of
the number in service of an infinite-server system fed the same arrivals and the same
service times — the offered load of the bucket. It is "the number in service had none
waited" **only when `S` is free of waiting**. A recorded `ℓ(r)` is what RouteForge recorded
for the whole request, including any time the request spent in the endpoint's own queue, so
for recorded rows `A` is the observed number in flight at the router, waiting included.

**The window.** `W` is every bucket from the first arrival bucket of any simulated row to
the last bucket in which any *recorded* interval `[t(r), ts(r))` lies. It depends on the
recorded rows alone: the same for both arms, every endpoint and every scenario. Service time
that a source other than `recorded` pushes past the last bucket of `W` is in no bucket and is
reported separately, so `Σ_b A · T + R_a(e) = Σ_r v · S` with `R_a(e)` that remainder. The
record is right-truncated, so an arm whose service times are longer than the recorded ones
is understated; the output carries the share

```
service_after_window_share = R_a(e) / Σ_{r : e_a(r)=e} v_a(r) · S_a(r)
```

per endpoint and arm (§21.5.5, FR3).

**Utilisation.** For a stated limit `C` on demand `D`, with `Act(e)` the buckets of `W` in
which `e` had an arrival or a request in service:

```
peak                = max_{b ∈ W} D(e,b) / C
mean_window         = (Σ_{b ∈ W} D(e,b) / |W|)      / C      the time-average
mean_active         = (Σ_{b ∈ W} D(e,b) / |Act(e)|) / C
buckets_over_share  = |{b ∈ W : D(e,b) > C}| / |W|
requests_over_share = Σ_{b : D(e,b) > C} Σ_{arrivals in b} v(r)  /  Σ_b Σ_{arrivals in b} v(r)
```

For concurrency, `mean_window · C = Σ_r v · S / (|W| · T)` up to the remainder above: the
arrival rate times the mean service time, `λ · E[S]`.

**A peak is an extreme-value statistic.** `peak` is the maximum of `|W|` bucket averages.
For a stationary stream it exceeds the mean by an amount that grows with `|W|` and with
`1/T` (§21.5.2, F3). It is therefore never reported without `mean_window`, and — because
the pair still cannot tell a limit exceeded in 1% of the buckets from one exceeded in 18%
(§21.5.5, FR4) — never without `buckets_over_share` either. Every printed peak reads

```
peak X% / avg Y%, over 100% in Z% of |W| buckets (endpoint, limit, T s buckets)
```

The headline of an arm is the largest `peak` over every endpoint and every stated limit that
has a figure: a maximum over endpoints × limits as well as over buckets, so stating more
limits can only raise it.

**Window too short.** If fewer than `m` buckets hold an **arrival**
(`--capacity-min-buckets`, default 10) no figure is computed. The state is
`window_too_short`, not a number. Buckets a request only runs through do not count.

**Exactness.** Per (endpoint, bucket, application) the engine holds four exact integers.
The integers of applications with the same `v` are added first; each distinct `v` multiplies
its integer total once, and the products are summed with `math.fsum`. Output is independent
of row order and of hash randomisation, and a uniform scale `v` leaves every peak in the
bucket it was in (§21.5.5, FR5).

### 21.2 Volume scale

`v_a(r)` is a product of two operator-stated factors: `s_0(app)` from the capacity file,
applied in both arms ("the real volume is `s_0` times the export"), and `s_1(app)` from the
proposal, applied in the proposed arm only ("and suppose it grew by `s_1`"). Each resolves
per application, else globally, else to 1.

Every demand is linear in `v`, so utilisation is linear in a global scale. A stated limit is
never scaled. The scale thins or thickens the recorded arrivals in place: it does not model
that a tenfold stream is smoother within a bucket, and it cannot create arrival times an
export does not have. It moves no route, no cost and no service time; only the capacity
section reads it. Whenever a scale is not 1 the section prints one fixed sentence saying
so: "A volume scale counts each recorded request that many times, at its recorded time; it
does not model how more traffic would arrive."

### 21.3 What it deliberately is not

- **Not a queueing model.** There is no waiting time and no blocking probability. With a
  hard concurrency limit and no queue the blocked share is Erlang-B in the offered load and
  the limit; it is above zero below 100% utilisation and depends on arrival variability that
  bucket totals do not carry. Reporting "the excess over the limit" as lost requests would
  be a lower bound presented as an estimate.
- **Not a prediction of what overload does.** RouteForge does not know whether an endpoint
  queues, sheds or slows down. And its own gateway would not simply lose a refused request:
  it fails over, which first-decision replay does not follow. So no rejection figure is
  derived from capacity, and `newly_rejected` stays a policy figure.
- **Not a measurement of capacity.** Every limit is stated by the operator. RouteForge
  manages no capacity and verifies no declaration.
- **Not a measurement of backend service time.** A recorded latency is RouteForge's
  end-to-end time for the request: it includes the gate, compression and verification, and
  whatever time the request waited inside the serving engine. Demand computed from a period
  in which the endpoint was itself overloaded therefore carries that waiting as "service".
- **Not ranged.** The P3c-3 bootstrap (§20) resamples rows within an application as if their
  order did not matter. Capacity depends on the order: a resample changes how many rows fall
  in each bucket at random, and a peak is a maximum, for which a percentile bootstrap is a
  poor estimator anyway. Capacity figures are points; a block bootstrap over time buckets is
  the appropriate method and is not built.
- **Not a recommendation.** No remedy is named, scenarios stay in file order, and no
  requirement is presented as the thing to change because capacity is short.

### 21.4 Invariants, and the test that pins each

| # | Invariant | Pinned by |
|---|---|---|
| C1 | With no capacity stated, every output of every mode is byte-identical to the tree before this slice; with one, only `capacity`, `inputs.capacity` and six `peak_*` table keys are added; a proposal's `capacity` and `volume_scale` never reach the applied document | `tests/eval/test_uncertainty_golden.py::test_u1_output_without_the_flag_is_byte_identical` (unchanged, not regenerated); `tests/eval/test_capacity_proposal.py::test_c1_capacity_and_volume_never_reach_the_applied_document`; `tests/eval/test_capacity_scenarios.py::test_c1_without_a_plan_nothing_is_added`, `::test_c1_a_plan_adds_one_key_and_the_peak_table_keys_and_changes_nothing_else`; `tests/eval/test_capacity_cli.py::test_c1_a_capacity_file_adds_keys_and_changes_no_existing_value`, `::test_c1_the_report_is_the_old_report_plus_one_section`, `::test_c1_shadow_only_output_is_untouched_and_takes_no_capacity` |
| C2 | Deterministic; independent of row order (non-dyadic volume scales, shuffled), of the order of tied buckets, and of hash randomisation | `tests/eval/test_capacity.py::test_c2_the_block_is_byte_identical_whatever_the_row_order`, `::test_c2_many_applications_in_one_bucket_sum_the_same_in_any_order`, `::test_c2_a_tie_for_the_peak_goes_to_the_earliest_bucket_whatever_the_order`, `::test_c2_the_block_does_not_depend_on_hash_randomisation`; `tests/eval/test_capacity_cli.py::test_c2_the_run_is_byte_identical_twice` |
| C3 | Exact conservation of requests, tokens, output tokens and service milliseconds, at bucket lengths 1, 60 and 3600 s; service time past the window is kept and reported | `tests/eval/test_capacity.py::test_c3_requests_tokens_and_service_time_are_conserved`, `::test_c3_service_time_after_the_window_is_kept_and_reported` |
| C4 | Linear in `volume_scale`; inversely linear in a stated limit; a limit moves no demand and is printed as stated; volume moves no route, no cost and no service time; a uniform scale shared by several applications moves no peak bucket; the volume sentence is printed once whenever a scale is not 1 | `tests/eval/test_capacity.py::test_c4_utilisation_is_linear_in_volume_scale`, `::test_c4_utilisation_is_inversely_linear_in_a_stated_limit`, `::test_c4_volume_changes_no_route_and_no_cost`, `::test_c4_a_uniform_scale_over_several_applications_moves_no_peak_bucket`; `tests/eval/test_capacity_report.py::test_a_volume_scale_is_said_to_be_a_count_not_an_arrival_model`; `tests/eval/test_capacity_scenarios.py::test_the_report_has_one_capacity_section_and_one_per_scenario` |
| C5 | A no-op proposal gives identical demand in both arms | `tests/eval/test_capacity.py::test_c5_a_noop_proposal_gives_identical_demand_in_both_arms`; `tests/eval/test_capacity_scenarios.py::test_c5_the_empty_scenario_has_the_same_load_in_both_arms` |
| C6 | A removed endpoint's demand goes where replay routes it | `tests/eval/test_capacity.py::test_c6_a_removed_endpoints_demand_goes_where_replay_routes_it` (against the replayed picks and the summary's own `moves`) |
| C7 | A rejected row adds no demand, and is in no endpoint's count of estimated rows | `tests/eval/test_capacity.py::test_c7_a_rejected_row_adds_no_demand`, `::test_a_row_no_arm_routes_is_not_counted_as_estimated_demand` |
| C8 | Too few buckets with an arrival is a state, not a number, in the JSON, the table and the report | `tests/eval/test_capacity.py::test_c8_fewer_buckets_than_the_minimum_is_a_state_not_a_number`, `::test_c8_the_minimum_counts_buckets_with_an_arrival_not_the_window`, `::test_c8_the_minimum_is_inclusive_and_can_be_lowered`, `::test_c8_no_rows_at_all`; `tests/eval/test_capacity_report.py::test_c8_a_short_window_prints_the_state_and_no_figure`; `tests/eval/test_capacity_scenarios.py::test_c8_a_short_window_fills_the_cell_with_the_state`; `tests/eval/test_capacity_cli.py::test_c8_a_short_export_says_so_in_the_json_the_table_and_the_report` |
| C9 | `--uncertainty` leaves capacity as points, and capacity leaves the ranges alone | `tests/eval/test_capacity_report.py::test_c9_with_ranges_in_the_same_report_capacity_says_why_it_has_none`; `tests/eval/test_capacity_scenarios.py::test_c9_ranges_and_capacity_together_leave_capacity_as_points`, `::test_c9_volume_scale_does_not_move_a_range_even_with_small_apps_pooled`; `tests/eval/test_capacity_cli.py::test_c9_uncertainty_leaves_capacity_as_points` |
| C10 | No ranking, recommendation or remedy vocabulary; file order kept | `tests/eval/test_capacity.py::test_c10_no_key_ranks_recommends_or_prescribes`; `tests/eval/test_capacity_report.py::test_c10_no_sentence_ranks_recommends_or_prescribes`; `tests/eval/test_capacity_scenarios.py::test_c10_the_table_stays_in_file_order_and_nothing_ranks` |
| C11 | No serving module imports `app.eval` | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` |
| C12 | The caveat is the spec's text and opens every capacity section; the spec and the reference quote the constant word for word | `tests/eval/test_capacity_report.py::test_c12_the_caveat_is_the_spec_text`, `::test_c12_the_spec_and_the_reference_quote_the_caveat_word_for_word`, `::test_c12_the_section_opens_with_the_caveat_and_the_no_remedy_sentence`; `tests/eval/test_capacity_scenarios.py::test_the_report_has_one_capacity_section_and_one_per_scenario` |
| C13 | The plan is in `inputs.capacity`; ids tell plans apart | `tests/eval/test_capacity_cli.py::test_c13_the_plan_is_in_inputs_and_the_id_tells_plans_apart` |
| C14 | A known arrival pattern gives its closed form, for requests inside one bucket and for requests that cross buckets; exactly the limit is not over it | `tests/eval/test_capacity.py::test_c14_utilisation_matches_the_closed_form`, `::test_c14_a_request_that_crosses_buckets_is_spread_over_them`, `::test_c14_exactly_at_the_limit_is_not_over_it`; `tests/eval/test_capacity_cli.py::test_c14_the_cli_reports_the_hand_computed_utilisation` |
| C15 | Nothing stated silently does nothing; a latency too long to place is located where it came from | `tests/eval/test_capacity.py::test_c15_a_bad_capacity_file_is_refused_naming_the_location`, `::test_c15_a_slug_or_app_the_snapshot_does_not_hold_is_refused`, `::test_c15_a_plan_value_out_of_range_is_refused`, `::test_c15_an_asserted_latency_too_long_to_place_is_refused`, `::test_c15_a_replayed_latency_too_long_to_place_names_where_it_came_from`; `tests/eval/test_capacity_proposal.py::test_c15_a_bad_limit_is_refused_naming_it`, `::test_c15_a_bad_volume_scale_is_refused`, `::test_c15_a_capacity_for_an_endpoint_that_is_not_there_is_refused`; `tests/eval/test_capacity_scenarios.py::test_c15_a_latency_too_long_to_place_names_the_scenario`, `::test_c15_a_snapshot_signal_too_long_to_place_is_not_blamed_on_a_scenario`; `tests/eval/test_capacity_cli.py::test_c15_a_tuning_flag_with_no_capacity_stated_is_refused`, `::test_c15_a_volume_scale_with_no_capacity_stated_is_refused`, `::test_c15_a_bad_capacity_file_is_refused`, `::test_c15_a_flag_out_of_range_is_refused`, `::test_c15_an_asserted_latency_too_long_to_place_is_refused` |
| C16 | Placement by estimated arrival in epoch-aligned buckets, in exact integer milliseconds, whatever the offset; an unusable latency (one above 24 hours included) subtracts nothing and is counted; no `ts` is counted, not placed | `tests/eval/test_capacity.py::test_c16_a_row_is_placed_by_its_estimated_arrival`, `::test_c16_an_unusable_latency_subtracts_nothing`, `::test_c16_a_latency_of_exactly_24_hours_is_the_longest_usable_one`, `::test_c16_buckets_are_epoch_aligned_whatever_the_offset`, `::test_c16_a_timestamp_is_floored_to_the_millisecond_in_exact_integers`, `::test_c16_rows_without_a_timestamp_are_counted_and_in_no_bucket`, `::test_rows_with_an_unusable_latency_are_counted_and_placed_at_ts` |
| C17 | In a comparison, each scenario's block is the single-proposal block; the current arm is shared | `tests/eval/test_capacity_scenarios.py::test_c17_each_scenario_is_the_single_proposal_block_in_file_order`, `::test_c17_the_current_arm_is_the_same_in_every_scenario` |
| C18 | One request is never more than 1.0 of concurrency in any bucket | `tests/eval/test_capacity.py::test_c18_twelve_requests_two_hours_apart_never_exceed_one_in_service`, `::test_c18_one_lone_five_minute_request_is_one_request`, `::test_c18_a_single_request_never_fills_more_than_a_bucket`; `tests/eval/test_capacity_report.py::test_a_sparse_export_reads_as_sparse` |
| C19 | With rows served where they were recorded, `mean_window` concurrency is the true time-average number in service under lognormal service times (σ = 1) and is `λ · E[S]` within 5%; it does not move with the window or the bucket | `tests/eval/test_capacity.py::test_c19_the_window_mean_is_the_time_average_under_skewed_service_times`, `::test_c19_the_window_mean_is_steady_and_the_peak_depends_on_window_and_bucket` |
| C20 | No output prints a peak utilisation without the window mean **and the share of buckets above the limit** beside it | `tests/eval/test_capacity.py::test_c20_no_peak_is_reported_without_the_window_mean_and_the_share_beside_it`; `tests/eval/test_capacity_report.py::test_c20_no_sentence_prints_a_peak_without_the_average_and_the_share`, `::test_the_headline_tells_often_over_the_limit_from_rarely`; `tests/eval/test_capacity_scenarios.py::test_c20_no_cell_or_sentence_prints_a_peak_without_the_average`; `tests/eval/test_capacity_cli.py::test_c20_the_written_report_never_prints_a_peak_alone` |
| C21 | A tier default is not a service time: where it is all an arm has for an endpoint there is no concurrency demand and no `max_concurrency` utilisation, in the block, the section, the headline or the comparison cell; requests and tokens are unaffected; a signal, an observed median and an asserted latency are still used | `tests/eval/test_capacity.py::test_a_tier_default_is_not_a_service_time_so_no_concurrency_is_computed`, `::test_a_concurrency_limit_that_cannot_be_computed_is_never_the_headline`, `::test_a_measured_or_asserted_latency_is_a_service_time`, `::test_the_real_replay_marks_an_endpoint_no_row_was_recorded_on`; `tests/eval/test_capacity_report.py::test_a_tier_default_prints_a_state_and_never_a_concurrency_percentage`, `::test_a_tier_default_alone_leaves_no_headline_and_says_so`, `::test_the_current_arm_cannot_be_given_a_latency_so_its_state_names_no_proposal_key`; `tests/eval/test_capacity_scenarios.py::test_a_tier_default_fills_the_cell_with_a_state_not_a_percentage` |
| C22 | Service time placed after the window is reported as a share of the arm's service time on the endpoint, printed as a percentage and seconds, and named in the headline at a tenth or more; no report figure carries an exponent | `tests/eval/test_capacity.py::test_service_time_after_the_window_is_a_share_of_the_arms_service_time`, `::test_an_endpoint_with_no_service_time_has_no_share_after_the_window`; `tests/eval/test_capacity_report.py::test_service_time_past_the_window_is_said`, `::test_a_material_share_after_the_window_is_in_the_headline`, `::test_no_figure_is_printed_as_a_raw_millisecond_count` |
| C23 | `--min-agreement` refuses first in every mode, including `--scenarios` with a body the capacity model would refuse | `tests/eval/test_capacity_cli.py::test_the_agreement_gate_still_refuses_first` |

The service-time rule of §21.1 is pinned source by source:
`tests/eval/test_capacity.py::test_a_row_served_where_it_was_recorded_keeps_its_own_service_time`,
`::test_a_moved_row_gets_the_endpoints_mean_recorded_latency_not_the_median`,
`::test_an_endpoint_no_row_was_recorded_on_falls_back_to_replays_latency`,
`::test_an_endpoint_only_excluded_rows_ran_on_says_its_latency_is_replays`,
`::test_a_failed_over_row_never_lends_its_latency`,
`::test_an_asserted_latency_replaces_every_other_source_in_the_proposed_arm`.

C1 is pinned against the files written before P3c-3 (§20.5), which this slice does not
regenerate: an operator who states no capacity gets the bytes they got two slices ago.

### 21.5 Results

**Everything in §21.5.1–§21.5.3 and §21.5.5 is synthetic.** None of it is a measurement of
real traffic or of a real endpoint. It checks that the code computes the model of §21.1 and that the
model's summary figures mean what they are labelled as — not that the model describes a
deployment. §21.5.4 lists what has to be measured for that.

Machine and date: Linux x86_64 (WSL2, kernel 6.18), Python 3.12.3; §21.5.1–§21.5.3 on
2026-10-06, §21.5.5 on 2026-10-07. The arithmetic is deterministic, so the figures do not
depend on the machine. Printed headlines quoted from §21.5.1–§21.5.2 predate the share
clause of §21.5.5 (`…, over 100% in Z% of N buckets`); the numbers in them are unchanged.

#### 21.5.1 Two hand-computed patterns (synthetic; replayed through the real `simulate()`)

**(a) Requests that end inside their arrival bucket.** One on-prem endpoint; 20 minutes at 2
requests a minute, then 10 minutes at 12 (160 requests); every request 6 s, arriving no later
than second 49; 900 prompt and 100 completion tokens. Stated limits: 1 concurrent,
10 requests/min, 10,000 tokens/min, 500 output tokens/min.

| figure | by hand | computed |
|---|---|---|
| quiet minute, concurrency | 2 × 6 / 60 = 0.2 → 20% | 20% |
| busy minute, concurrency | 12 × 6 / 60 = 1.2 → 120% | 120.0% |
| busy minute, requests / tokens / output tokens | 120% / 120% / 240% | 120.0% / 120.0% / 240.0% |
| window | 30 buckets, 30 with an arrival, 30 active | 30 / 30 / 30 |
| `mean_window` = `mean_active`, concurrency | (20·2 + 10·12)/30 · 6/60 = 0.5333 → 53.33% | 53.3% |
| `mean_window`, output tokens | 5.333 · 100 / 500 → 106.67% | 106.7% |
| buckets over 100% | 10 of 30 | 10 of 30 |
| requests arriving in those buckets | 120 of 160 = 75% | 75.0% |
| peak bucket | the 21st minute | `2026-09-20T12:20:00+00:00` |
| at `volume_scale: 2`, peak / avg concurrency | 240% / 106.67% | 240.0% / 106.7% |
| at `volume_scale: 5`, quiet minutes over 100% for requests | none (exactly 100%) | none |

**(b) Requests that cross buckets.** One request a minute for 12 minutes (12 requests), each
arriving at second 30 and taking 90 s. Stated limit: 1 concurrent.

| figure | by hand | computed |
|---|---|---|
| service seconds per minute | 30; then 60 + 30 = 90 for 11 minutes; then 60 | 30, 90 × 11, 60 |
| concurrency per minute | 0.5; 1.5 × 11; 1.0 | 0.5; 1.5 × 11; 1.0 |
| window | 13 buckets, 12 with an arrival, 13 active | 13 / 12 / 13 |
| `mean_window` concurrency | 12 × 90 / (13 × 60) = 1.3846 | 1.384615 |
| `mean_window` requests/min | 12 / 13 = 0.9231 | 0.923077 |
| buckets over `max_concurrency: 1` | 11 of 13 | 11 of 13 |
| requests arriving in those buckets | 11 of 12 | 0.916667 |
| service seconds placed | 12 × 90 = 1,080 | 1,080 (none after the window) |

#### 21.5.2 The first model against the model as built (synthetic streams, measured twice)

The first model — one latency per endpoint, a request's whole service time counted in its
arrival bucket, means over the buckets that held an arrival, the peak printed alone — was
built exactly as specified and passed its tests. An independent review ran synthetic streams
through it and found four defects in the **model**. Each was re-measured here on the engine
before the revision (commit `422d234`) and on the engine as built, on the same streams.

*Method.* Arrivals are a Poisson process: inter-arrival times from
`random.Random(seed).expovariate(rate)`, arrival times rounded to the millisecond, until the
stated duration. Each request becomes one row recorded as served alone by one endpoint
(status `ok`, one attempt), with its own service time as `latency_ms` and `ts` = arrival +
service time; the arm routes it to that endpoint with the latency replay would report for
it (the constant `S`, or for F4 the sample median). `max_concurrency` is stated for that
endpoint; 60 s buckets unless said. "True" values come from the raw (arrival, service)
pairs, outside the engine: the per-bucket time-average number in service, the largest number
in service at any instant (an event sweep), and the time-average over the window.

**F1 — row order (C2).** 3,000 rows over one hour, three applications at volume scales
0.1 / 0.7 / 1.3, service times 0.1234, 3.2173 or 41.2347 s, seed 4; the block serialised
before rounding, for 60 shuffles of the same rows.

| | distinct blocks over 60 shuffles | peak bucket of two equal buckets |
|---|---|---|
| before | 29 | moved with row order (5.5 against 5.500000000000001) |
| as built | 1 | the earlier bucket, in all 201 orders tried |

**F2 — where service time is counted (C18).** True mean offered load 2.0 (rate 2/S), 24 h,
seed 7, constant service time `S`. The largest number in service at one instant was 12 in
every row.

| S (s) | requests | peak before | peak as built | true per-bucket peak | before ÷ true | as built ÷ true | flag before |
|---|---|---|---|---|---|---|---|
| 4 | 43,278 | 3.667 | 3.671 | 3.671 | 1.00 | 1.00 | no |
| 30 | 5,854 | 6.500 | 6.155 | 6.155 | 1.06 | 1.00 | no |
| 60 | 2,965 | 8.000 | 9.016 | 9.016 | 0.89 | 1.00 | no |
| 90 | 1,967 | 10.500 | 9.313 | 9.313 | 1.13 | 1.00 | yes |
| 300 | 596 | 15.000 | 10.782 | 10.782 | 1.39 | 1.00 | yes |
| 900 | 204 | 45.000 | 12.000 | 12.000 | 3.75 | 1.00 | yes |

The mean went wrong with it: at `S` = 900 s the first model's mean was 16.452 against a true
time-average of 2.115; as built, `mean_window` is 2.115.

The example that would have reached a paper — 12 requests, one every two hours, each 138 s,
`max_concurrency: 1`, never more than one in service:

| | peak | mean | buckets over | requests over | window |
|---|---|---|---|---|---|
| before | 230% | 230% (one figure, over 12 arrival buckets) | 12 of 12 | 100% | 1,321 buckets, 12 with an arrival |
| as built | 100% | `mean_window` 2.09% · `mean_active` 76.7% | 0 of 1,323 | 0% | 1,323 buckets, 12 with an arrival, 36 active |
| true | at most 1 in service | time-average 0.0209 | — | — | — |

One lone five-minute request: 5.0 concurrent before; 1.0 as built (`mean_window` 0.833 over
the 6 minutes it spans).

**F3 — a peak with no context (C20).** A stationary stream, 0.5/s, `S` = 4 s (true mean
2.0), seed 3, 302,245 requests over 7 days, cut at 1 h (1,824 requests) and 24 h (43,117).
Peak ÷ mean:

| window | T (s) | peak before | mean before | ratio before | peak as built | `mean_window` | ratio as built |
|---|---|---|---|---|---|---|---|
| 1 h | 60 | 3.333 | 2.027 | 1.64 | 3.230 | 2.027 | 1.59 |
| 24 h | 60 | 3.400 | 1.996 | 1.70 | 3.269 | 1.995 | 1.64 |
| 7 d | 60 | 3.400 | 1.999 | 1.70 | 3.350 | 1.999 | 1.68 |
| 1 h | 10 | 5.600 | 2.044 | 2.74 | 6.024 | 2.027 | 2.97 |
| 24 h | 10 | 6.400 | 2.010 | 3.18 | 6.024 | 1.996 | 3.02 |
| 7 d | 10 | 6.800 | 2.012 | 3.38 | 6.418 | 1.999 | 3.21 |

The ratio is a property of a maximum, not of either model: a steady stream at a mean of 2.0
has a busiest minute near 3.4 and a busiest ten seconds above 6. The revision does not
remove it. It prints the mean beside the peak, and the mean is now the same in every row.

A low rate — 1.2 requests a minute, `S` = 30 s (true mean 0.6), seed 5, 11,946 requests over
7 days:

| window | requests | true time-average | mean before | `mean_window` | `mean_active` | peak before | peak as built | true per-bucket peak |
|---|---|---|---|---|---|---|---|---|
| 1 h | 69 | 0.575 | 0.863 | 0.575 | 0.704 | 2.000 | 2.277 | 2.277 |
| 24 h | 1,798 | 0.624 | 0.869 | 0.624 | 0.738 | 3.500 | 2.795 | 2.795 |
| 7 d | 11,946 | 0.593 | 0.852 | 0.593 | 0.715 | 4.000 | 3.552 | 3.552 |

The first model's mean was a mean over buckets that held an arrival, with nothing naming
that: 44% above the time-average over 7 days. As built the two means are separate and
labelled. The peak over 7 days is still 6.0 times the window mean — which is the reason it is
never printed without it.

**F4 — median against mean service time (C19).** Rate 1/3 per second for 6 h, seed 9, 7,269
requests; service times lognormal with median 6.0 s and σ = 1.0 (sample median 6.00 s, sample
mean 9.73 s; `λ · E[S]` from the sample = 3.274).

| | mean concurrency | against the true time-average (3.265) | peak | true per-bucket peak |
|---|---|---|---|---|
| before (rate × the endpoint's median) | 2.018 | −38.2% | 3.298 | 6.372 |
| as built (each row's own recorded service time) | 3.265 | 0.0% | 6.372 | 6.372 |

With skewed service times the first model understated the peak as well as the mean, by 48%.

**F5 — minors**, each now a test: `rows.estimated_tokens` counted 4 rows that were rejected
in both arms (now `estimated_token_rows`, per endpoint per arm, 0 for those rows); a row
written at 12:00:59.9996 was placed in the 12:01 bucket (now 12:00, exact integer floor); a
recorded latency of 1e12 ms placed a row in January 1995 and 1e15 ms raised (above 24 hours
is now unusable and counted).

What the "as built" columns do **not** show: that these figures describe a real endpoint.
Every stream above has rows served where they were recorded, so each row's service time is
exact by construction. On recorded traffic a row that moves gets a mean, and a recorded
latency is not a pure backend time (§21.6).

#### 21.5.3 Mutations (synthetic)

Thirteen single-line changes to the engine as built (`app/eval/capacity.py`), each applied
alone and the four capacity test modules run until the first failure (2026-10-06). Every one
was caught:

| mutation | first test to fail |
|---|---|
| the arrival estimate ignores the recorded latency | `test_c16_a_row_is_placed_by_its_estimated_arrival` |
| the whole service time is counted in the arrival bucket (the first model) | `test_c14_a_request_that_crosses_buckets_is_spread_over_them` |
| `≥` for `>` at the limit | `test_c14_a_request_that_crosses_buckets_is_spread_over_them` |
| the proposal's volume scale is ignored | `test_c14_exactly_at_the_limit_is_not_over_it` |
| rejected rows are counted | `test_c3_requests_tokens_and_service_time_are_conserved` |
| `mean_window` over the endpoint's active buckets | `test_c18_twelve_requests_two_hours_apart_never_exceed_one_in_service` |
| replay's one latency per endpoint instead of the row's own (the first model) | `test_rows_with_an_unusable_latency_are_counted_and_placed_at_ts` |
| volume-scaled floats added one by one in row order (the first model) | `test_c2_the_block_is_byte_identical_whatever_the_row_order` |
| a wrong endpoint mean for a moved row | `test_rows_with_an_unusable_latency_are_counted_and_placed_at_ts` |
| a failed-over row lends its latency | `test_a_failed_over_row_never_lends_its_latency` |
| a latency above 24 hours is usable | `test_c16_an_unusable_latency_subtracts_nothing` |
| the headline peak drops its window mean | `test_c14_utilisation_matches_the_closed_form` |
| a printed peak drops its average | `test_the_section_shows_each_stated_limit_per_arm_and_the_demand` |

These thirteen were run on the engine before the final-review changes of §21.5.5 and have
**not been re-run** since; the tests they name all still exist and pass.

One further change **survived** and is recorded as that: replacing `math.fsum` in sorted
order with Python's built-in `sum` in row order. On Python 3.12 `sum` over floats is itself
compensated, and it produced the same bytes on every stream tried, including one built to
separate them (`test_c2_many_applications_in_one_bucket_sum_the_same_in_any_order`). The
engine keeps `fsum` in sorted order because that is exact by construction on any Python,
not because a test here tells the two apart.

#### 21.5.4 On recorded traffic — **TO MEASURE**

None of the following has been run. Each is listed with what it needs.

- **The healthcare scenario pair on the DGX lab** (`docs/research/arxiv.md` E10a): persona
  traffic at a known arrival rate through two endpoints of different placement, the
  "all on-prem" and "raw PII on-prem only" scenarios, and a capacity file for the on-prem
  endpoint. Reports: peak, window average and active average utilisation per arm per
  scenario, with the bucket length and the window. **TO MEASURE.**
- **Stated against observed** (E10b): state `max_concurrency` from the serving engine's
  configured limit, drive the endpoint at rates the model puts at 50%, 100% and 150% on
  average, and record what the endpoint actually did (latency, refusals). This is the only
  check of whether "offered concurrency ÷ stated limit" tracks anything. **TO MEASURE.**
- **Recorded latency against backend time** (E10c): on an export from an endpoint whose own
  metrics give time in service, how far the recorded latency overstates it, idle and under
  load. The recorded figure on an `ok` row is the **sum of the trace records' latencies**,
  not wall-clock time (`docs/reference/routing-outcomes.md` §3.2), so this has two parts:
  the sum against wall-clock, and wall-clock against backend time. **TO MEASURE.**
- **Service time of moved requests** (E10f): hold out rows actually served on endpoint B,
  predict their service time as B's mean over the other rows, and report the error by
  request size. **TO MEASURE.**
- **Bucket sensitivity** (E10d): peak and window-average utilisation against
  `--capacity-bucket` at 10, 60, 300 and 3600 s on one export. The synthetic counterpart is
  F3. **TO MEASURE.**
- **Arrival estimate** (E10e): the share of rows whose bucket changes when `ts` is used in
  place of `ts − latency`, and — since that latency is a sum of step latencies on `ok` rows
  and wall-clock on failure rows — the error of `ts − latency` against the request's real
  start where a deployment logs one. **TO MEASURE.**
- **Right-truncation on a real export**: `service_after_window_share` per endpoint for a
  realistic proposal that moves traffic or asserts a latency. The synthetic counterpart is
  §21.5.5 FR3. **TO MEASURE.**
- **Calendar-minute against sliding-minute peaks** on recorded arrivals, for an endpoint
  behind a provider quota. Synthetic counterpart: §21.5.5. **TO MEASURE.**
- **Service-time sources on a real export**: the share of rows, and of service time, under
  `recorded`, `endpoint_mean` and `replay` for a realistic proposal. **TO MEASURE.**

#### 21.5.4a First run on recorded rows — DGX lab, 2026-10-07 (a plumbing check)

**Setup.** The lab deployment at api `0.29.0` (`main` through PR #90): application
`paper-test`, endpoint `qwen3-coder-next`, the 96-row export of §20.6.2 (four identical passes
of 24 requests, each pass about 30 s, spread over 5 h 23 min), cut with `--since
2026-10-06T20:07:37Z`. Limits stated for the run — invented for it, not a property of the
endpoint: `max_concurrency: 1`, `requests_per_minute: 60`, `tokens_per_minute: 40000`. Four
scenarios: no-op, volume ×10, raise the per-request cap ($0.05 → $0.5), and both. Artifacts:
`docs/research/results/p3c4-dgx-lab-2026-10-07/` (`comparison_id b803d9e233314a6a`, 5 s
buckets).

**Read this first.** Four bursts of test requests are not a workload. The run shows the
states and the headline working on real records; no figure below says anything about the
endpoint's capacity.

| measurement | value |
|---|---|
| default 60 s buckets | `window too short (3 of the 10 buckets needed)` in every scenario — no figure printed |
| 10 s buckets | `window too short (9 of the 10 buckets needed)` |
| 5 s buckets | 16 buckets with an arrival in a window of 3,880; figures computed |
| service time source, current arm | 68 of 68 routed rows `recorded` (mean 163 ms, longest 864 ms); 0 mean, 0 replay; 0% after the window |
| `max_concurrency: 1`, current arm | peak 45.0%, window average 0.06%, 0 buckets over |
| headline, current arm (the largest peak is on `tokens_per_minute`) | `peak 331.7% / avg 0.4%, over 100% in 0.2% of 3880 buckets (qwen3-coder-next, tokens_per_minute, 5 s buckets)` |
| `volume-x10`, proposed | `peak 3317.4% / avg 4.4%, over 100% in 0.2% of 3880 buckets` — exactly 10× the peak and the average; buckets over go from 7 to 9 of 3,880 (the same bursts, taller) |
| `raise-budget` (28 requests newly served), proposed | `peak 1272.5% / avg 2.6%, over 100% in 0.4% of 3880 buckets` — the rescued requests are the large ones |
| `raise-budget-x10`, proposed | `peak 12724.8% / avg 25.7%, over 100% in 0.4% of 3880 buckets` |

What the run does show, on real rows: (1) the short-window state fires where it should — at
the default bucket this export supports no capacity figure at all; (2) the three-part headline
is what keeps a burst from being misread: a "peak 332%" that sits beside a 0.4% average and
0.2% of buckets over is visibly a few seconds of a test burst, which the peak alone would not
tell a reader; (3) a per-minute limit evaluated on 5-second buckets scales a burst up to a
per-minute rate — the bucket length is in the headline for that reason; (4) the interaction
with the policy simulator is real: raising the budget cap changes the token demand on the
endpoint (it serves the large requests it used to reject), which is the point of computing
capacity from the replayed arm rather than from the recorded one.

#### 21.5.5 The final review: a correct engine that could still mislead (synthetic)

A second independent review, of the whole branch at commit `1928427`, compared the revised
engine with an event-sweep truth and found them equal to the last digit. It then found four
ways the **output** could still be misread, and some minors. Each was reproduced, fixed
test-first, and re-measured on the same rows before (engine at `1928427`) and after (the
engine as built), on 2026-10-07.

*Method.* Rows are built by hand, not replayed: each `(arrival, service)` pair becomes one
row recorded as served alone by endpoint `e` (status `ok`, one attempt, `latency_ms` = the
service time, `ts` = arrival + service time) and routed to `e` by the current arm. The
proposed arm is given, per case, another endpoint or an asserted latency, with the latency
source replay would report (`tier_default`, `signal`, `proposal`). 60 s buckets. Poisson
streams are `random.Random(seed).expovariate(rate)` as in §21.5.2.

**FR1 — a tier default read as a service time (C21).** 180 requests, one every 10 s, each
recorded at 10 s on `e` (1.0 in service). The proposal moves all of them to `new`: no
recorded request, no latency signal, no asserted latency, so replay's latency for it is the
cheap tier's default, 200 ms. `max_concurrency: 1` on both.

| | current arm | proposed arm |
|---|---|---|
| before | `peak 100.0% / avg 96.8% (e, …)` | `peak 2.0% / avg 1.9% (new, …)` |
| as built | `peak 100.0% / avg 96.8%, over 100% in 0.0% of 31 buckets (e, …)` | `peak 0.0% / avg 0.0%, over 100% in 0.0% of 31 buckets (e, …); concurrency not computed for new` |

Before, the scenario read as a fifty-fold cut in load. Nothing had been cut: ten-second
requests had been placed at a fifth of a second each, a constant from the routing score.
And because the headline is a maximum, the understated endpoint would have dropped out of
it beside any other limit. As built there is no concurrency figure for `new` in that arm
(`not_computed: "tier_default_service_time"`, `demand.concurrency` null); its limit row
reads `n/a (no service time: state endpoints.new.latency_ms)`; requests and tokens are
counted as before (6 requests a minute). The `e` in the proposed headline is the endpoint
the proposal emptied, at 0%. Same rows with a snapshot signal of 9 s for `new`:
`peak 90.0% / avg 87.1%` — a measured latency is still used.

**FR2 — the agreement gate ran second in one mode (C23).** Baseline agreement 0,
`--min-agreement 0.9`, a scenario asserting `latency_ms: 1e12` (which the capacity model
refuses). Exit codes before: `--proposal` 4, `--scenarios --uncertainty` 4, `--scenarios`
**2**. As built: 4, 4, 4.

**FR3 — right-truncation (C22).** A 30-minute export, 6 requests a minute, each recorded at
6 s on `e`; the proposal asserts a service time `S` for `e`; `max_concurrency: 4`. The
window is 30 buckets and ends when the last recorded request ended.

| asserted S | steady state `λ·S` | reported peak | reported `mean_window` | share of the arm's service time after the window |
|---|---|---|---|---|
| 6 s | 0.6 | 0.60 | 0.60 | 0.0% |
| 60 s | 6 | 6.00 | 5.91 | 1.4% |
| 600 s | 60 | 60.00 | 50.13 | 16.4% |
| 1,800 s | 180 | 177.40 | 90.40 | 49.8% |
| 7,200 s | 720 | 177.40 | 90.40 | 87.4% |
| 86,400 s | 8,640 | 177.40 | 90.40 | 99.0% |

(Concurrency in requests; the shares are also derived by hand in
`test_service_time_after_the_window_is_a_share_of_the_arms_service_time`.) Once `S` exceeds
the window's length the figures stop responding to it: the rows for 1,800 s, 7,200 s and a
day are identical. Before, the only sign was a sentence beginning "1.13328e+09 ms of the
proposed arm's service time". As built the block carries `service_after_window_share`, the
note reads "87.4% of the proposed arm's service time on this endpoint (1,133,280 s) falls
after the window's last bucket and is in no bucket, so this arm's average on this endpoint
is understated and its peak may be", and the headline ends
`; 87.4% of service time on e falls after the window`. **The figures are disclosed, not
corrected**: extending the window to hold the proposed arm's service time would make the
current arm's averages depend on the proposal, and each scenario's block would stop being
the single-proposal block (C17).

**FR4 — peak and average cannot say how often (C20).** A stationary week: Poisson at
0.02/s, `S` = 30 s, seed 5, 11,946 requests, `mean_window` 0.593 in service.

| `max_concurrency` | before | as built | buckets over |
|---|---|---|---|
| 1 | `peak 355.2% / avg 59.3%` | `peak 355.2% / avg 59.3%, over 100% in 17.6% of 10081 buckets` | 1,775 of 10,081 |
| 2 | `peak 177.6% / avg 29.6%` | `peak 177.6% / avg 29.6%, over 100% in 1.2% of 10081 buckets` | 122 of 10,081 |

The two "before" strings are the same shape; one limit is exceeded in a sixth of the
minutes and the other almost never. And the pair is not stable in the length of the export
while the share is — the same stream against a limit of 1, cut at three lengths:

| window | peak ÷ `mean_window` | share of buckets over |
|---|---|---|
| 1 h | 3.96 | 0.167 |
| 24 h | 4.48 | 0.193 |
| 7 d | 5.99 | 0.176 |

As built the headline and the comparison cell carry the share and the bucket count, the
table has `peak_buckets_over_share`, and the C20 detectors fail a report or a block that
prints a peak utilisation without it. **Deliberately not added:** an "expected peak of a
steady stream" for the peak to be read against. It would be a model output, and it needs an
arrival assumption (Poisson, say) that RouteForge has no ground to assert about a tenant's
traffic.

**FR5 — minors.**

- *A uniform scale could move a peak.* 1,500 rows over an hour, three applications, service
  times 0.4 / 7.3 / 95 / 410 s, seed 11, one volume scale for all three. Scaling each
  application's integer separately and summing gave `0.7·a + 0.7·b + 0.7·c`, one unit in
  the last place from `0.7·(a + b + c)`: at 0.7 the `requests_per_minute` peak bucket moved
  from 12:24 to 12:34 (two buckets with equal demand). As built, equal factors are summed
  as integers first; the peak bucket is the unscaled one at 0.1, 0.7, 3 and 1,000,000.
- *The method id.* `"bucketed_offered_load"` named the first model and was still the id of
  the second. It is `"spread_offered_load"`.
- *Exponents.* A day-long asserted service time printed `mean 8.64e+07 ms`. No report figure
  is printed with an exponent now.
- *Error location.* A snapshot latency signal too long to place was reported at
  `endpoints.<slug>.latency_ms`, a key nobody wrote, under the name of whichever scenario
  came first. It is reported at `snapshot.signals.<slug>.latency_ms`, with no scenario.

**Three properties of the model the review measured, which are limits and not defects**
(§21.6). Same method; the truth in each is computed outside the engine.

- *Failed rows.* The 180-request stream above, with one row in ten recorded as a
  `backend_failed` timeout of 120 s (so not "served alone", and given the endpoint's mean of
  10 s): `mean_window` concurrency 0.968; had each failure held a slot for its 120 s it
  would be (162 × 10 + 18 × 120) / (31 × 60) = 2.03.
- *Sliding rate limits.* Requests with zero service time against `requests_per_minute`.
  Twenty requests in 10 s straddling a minute boundary (from second 55, half a second
  apart), limit 10 a minute: `peak 100.0%`, no bucket over; the busiest sliding 60 s holds
  20. Steady Poisson streams — 0.5/s for 24 h (seed 3), 0.2/s for 6 h (seed 2), 0.02/s for
  7 d (seed 5): busiest calendar minute 51, 23 and 8 requests; busiest sliding 60 s 56, 24
  and 8 (1.10×, 1.04×, 1.00×).
- *A quantile instead of a maximum.* On the steady stream of §21.5.2 F3 (0.5/s, `S` = 4 s,
  seed 3, 60 s buckets) the 95th percentile of the true bucket averages was 1.33, 1.29 and
  1.30 times the mean over 1 h, 24 h and 7 d, where the maximum was 1.59, 1.64 and 1.68
  times. A p95-bucket figure needs no arrival model and does not grow with the window. It
  is not built (B66).

### 21.6 Limits

Figures in this list are synthetic; the method for each is in §21.5.5.

- **No queue, no blocking, no delay**, and no statement of what an endpoint does above a
  stated limit (§21.3).
- **A recorded latency is end-to-end**, not backend service time, and includes any waiting
  inside the serving engine. Concurrency computed from recorded rows is the number in flight
  at the router as recorded, waiting included; it is an upper reading of backend load when
  RouteForge's own overhead is material, and is inflated by congestion in the recorded
  period.
- **On an `ok` row the recorded latency is a sum of step latencies, not wall-clock time**
  (the sum of the trace records' `latency_ms`; `docs/reference/routing-outcomes.md` §3.2).
  Both the arrival estimate and the service time rest on it. How far the sum is from
  wall-clock is unmeasured (E10c, E10e).
- **A row recorded as failed is given the endpoint's mean service time.** A row recorded as
  `backend_failed` or `verify_failed`, failed over, or rejected, that an arm routes, never
  uses its own latency — a failure row's latency is wall-clock from the request handler and
  not a service time. A failure that held a slot until a long timeout is therefore counted
  at the mean: with one row in ten a 120 s `backend_failed` timeout among 10 s requests,
  mean concurrency read 0.968 where holding the slot for 120 s would give 2.03.
- **A moved request has no service time of its own.** It is given the mean of the endpoint
  it moves to, or replay's latency for an endpoint nothing was recorded on. Where that is
  only a tier default there is no concurrency figure at all: it is a state (§21.1, FR1).
- **Right-truncation understates an arm whose service times are not the recorded ones.**
  The window ends when the last recorded request ended, and service time an asserted, mean
  or replayed latency places after it is in no bucket. On a 30-minute record at 6 requests a
  minute, with an asserted service time `S`, the share of the proposed arm's service time
  after the window was 1.4% (60 s), 16.4% (600 s; `mean_window` 50.1 against a steady state
  of 60), 49.8% (1,800 s; 90.4 against 180) and 87.4% (7,200 s); past the window's length
  the figures do not respond to `S`. The share is reported per endpoint and arm and, at a
  tenth or more, in the headline; nothing is corrected (FR3).
- **A peak is a maximum over buckets.** It depends on the bucket length and the window
  length (F3); it is reported with the window average and the share of buckets above the
  limit, and must be quoted with both, the bucket length and the window.
- **The headline is also a maximum over endpoints and limits.** Stating more limits, or
  limits for more endpoints, can only raise it. Two headlines are comparable only between
  runs that state the same limits.
- **Epoch-aligned buckets understate a sliding-window rate limit.** Twenty requests in 10 s
  across a minute boundary against 10 a minute read `peak 100.0%` with no bucket over; the
  busiest sliding minute holds 20. On three steady Poisson streams the busiest sliding 60 s
  held 1.00 to 1.10 times the busiest calendar minute.
- **Client retries are counted as independent demand.** A request retried after a live
  failure or timeout is several rows. A scenario in which the first attempt would have
  succeeded would not have had the retries, and the model keeps them.
- **Censoring.** A request refused before an outcome row was written is not in the export;
  rows excluded from simulation (`unknown_status`, `unknown_app`, `missing_features`,
  `missing_routing_tokens`) are in no demand. The excluded count is printed
  (`excluded_not_counted`); the direction is understatement.
- **A congested recording compounds.** Latencies recorded while an endpoint was queueing are
  inflated service times; `volume_scale`, or a proposal that moves more traffic onto that
  endpoint, multiplies them, and nothing feeds the added load back (no queue is modelled).
- **Tokens are counted at arrival**, not spread over the generation.
- **Arrival is estimated** from the write time and the recorded latency.
- **The window's left edge.** Requests in service when the export starts are absent.
- **Demand is first-decision demand.** Failover attempts, hedged calls and anything that
  does not pass through RouteForge are absent.
- **A stated limit is one number.** Real capacity depends on the lengths being served, the
  batch size and the engine; a `max_concurrency` measured on short prompts overstates what
  the endpoint carries on long ones.
- **`volume_scale` multiplies; it does not generate traffic** (§21.2).
- **Ten buckets is a floor**, not evidence that eleven describe a deployment.
- **No range**, and none can be had from the row bootstrap of §20 (§21.3).
- **Cost of the computation.** Spreading takes one step per bucket a request runs through,
  so a row costs O(service time ÷ bucket length); a difference-array accumulation would
  make it O(1) per row (B66).
- Everything in §21.5.1–§21.5.3 and §21.5.5 is synthetic. Every figure on recorded traffic
  is **TO MEASURE** (§21.5.4).

---

## 22. Group report and the offline router-version check (P2 evaluation metrics, 2026-10-07)

**Status:** built, offline, opt-in. Spec
`docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` (§4, §5, §7, §8); plan
`docs/superpowers/plans/2026-10-07-p2-eval-metrics.md`. Branch `feat/p2-eval-metrics`.
Operator guide: `docs/guides/feasible-route-bench.md` §6.2–§6.3. **Every number in §22.5 is
from a simulated run.** Read §22.6 before writing a sentence that claims a result.

This is the first of two plans. The second builds churn robustness and confidence
calibration (handoff §18 metrics 6–8); neither exists yet.

### 22.1 Per-case scores and the group report

**Per-case scores.** `strategy_metrics` computed every aggregate inside one loop. That loop
is now `oracle.case_scores`, which returns one `CaseScore` per case: what the case adds to
each aggregate, or `None` where the case is outside that aggregate's denominator.
`strategy_metrics` is `aggregate_scores(case_scores(...))`, rounded, and its output did not
move (§22.4, E1). A group figure is an aggregate over a subset of scores; a resample is an
aggregate over a multiset of them.

**Denominators.** For a strategy `s` and a set of cases `G`:

| Metric | Denominator |
|---|---|
| `violation_rate`, `empty_set_mismatch_rate` | `|G|` |
| `pass_rate`, `quality_regret` | `|{c ∈ G : E(c) ≠ ∅}|` |
| `floor_miss_rate` | `|{c ∈ G : P(c) ≠ ∅}|` |
| `cost_regret` | `|{c ∈ G : s(c) ∈ P(c)}|` |
| `latency_regret` | the same, with both latencies known |

Every printed figure carries its denominator `n`. For a partition of the suite into
groups, `overall = Σ n_g · value_g / Σ n_g` with `n_g` the **figure's** denominator in
group `g`. Weighting by the group's case count is correct for the two metrics over `|G|`
and wrong for the others whenever groups differ in how many of their cases are empty or
passed.

*Worked example* (eight cases; `tests/eval/test_groups.py`): cost regret is 0.15 over four
passing cases in one group of five and 0.6 over one passing case in a group of three. The
overall figure is `1.2 / 5 = 0.24`. By denominator: `(4 × 0.15 + 1 × 0.6) / 5 = 0.24`. By
case count: `(5 × 0.15 + 3 × 0.6) / 8 = 0.31875`.

**Dimensions.** `placement`, `protection`, `workload` (author tags, as §3's metric 6) and
`application`: `tags.app`, else a fingerprint of the case's `app_policy`. The bench has one
tenant id, so there is no tenant dimension. A case with no value is in `(untagged)`: every
case is in exactly one group per dimension.

**Small groups.** A figure with `n` below `--min-group-size` (default 5, inclusive) is
`insufficient_cases` and has no value. The rule is on the figure's denominator. Counts of
violations and empty-set mismatches are never suppressed.

**Counts per strategy.** `groups.md` also prints a table of cases, violations and
empty-set mismatches over the whole run, for every strategy, in sorted order.

**Worst group.** Per metric, the worst printed figure across all groups of all dimensions,
with its `n`, the group's size, how many groups print that metric (of how many exist) and
how many of the printed ones share the worst value. When all printed groups share it, none
is named. A tie between some (not all) of the printed groups is listed, not broken.

### 22.2 The router-version check

**What is compared.** A candidate is an overlay of `routing.yaml` keys
(`routeforge.router-candidate/v1`), laid over the deployment's routing config in memory. It
is scored like a weight variant: three more router arms over **the same sweep matrix**.
The check compares `router_online` (baseline) with `router_online:candidate`.

This is a clean comparison because of two properties of the code, both checked before the
design was written: `feasible_set` reads no routing weight or threshold, so the eligible
set `E(c)` is the same for both arms; and a matrix row is one pinned call to one endpoint,
so its quality, cost and latency do not depend on routing config either. Every difference
between the two arms is the candidate's.

**Worsening.** For a metric `m` over a set of cases, `w = m_cand − m_base`, or
`m_base − m_cand` for `pass_rate`. Positive is worse.

**Range.** A paired case bootstrap. Cases are sorted by id; replicate `b` draws `n`
positions with replacement (`uncertainty.draw_indices(seed, b, scope, n)`); both arms'
aggregates are recomputed on that one draw; `[lo, hi]` is the equal-tailed percentile
interval of the `B` values of `w` (default `B = 1000`, level 0.90). `uncertainty.py`'s
draw and interval functions are reused; its replicate loop, built around simulator rows
and application strata, is not. `--check-replicates` must be at least
`ceil(2 / (1 - interval))` (20 for the default 0.90 interval) or the run is a usage error
(exit 1): fewer and a tail would drop none of the resamples, so the interval would really
be the resamples' min and max.

**Decision rule** (amended 2026-10-07; see §22.7). For tolerance `τ ≥ 0`:

1. `w` undefined or not finite → `undefined`
2. no range → `inconclusive`
3. `lo > τ` → `regressed` (the **whole** range is beyond the tolerance)
4. `hi ≤ τ` → `within_tolerance` (the **whole** range is within the tolerance)
5. otherwise → `inconclusive`; when the point estimate `w > τ` the figure also carries
   `point_beyond_tolerance: true` and `check.md` prints `inconclusive (point beyond
   tolerance)`

The point estimate alone decides nothing: a figure is judged on its whole range.
`change ∈ {none, worse, better, not_detectable}` is reported beside the state and never
decides it; a `not_detectable` range (one that includes 0) is common among
`within_tolerance` rows too, since the decision is about where the whole range sits
against `τ`, not about whether it reaches 0. No correction is made for the number of
figures judged (`CHECK_MULTIPLICITY`).

**Verdict.** `hard_fail` (exit 2) if the candidate has any violation — an attempt outside
`E(c)`, including any attempt where `E(c) = ∅` — or the run's own integrity code is not 0.
Otherwise the worst state over the judged figures: `regressed` (3), `inconclusive` (4),
`pass` (0); `no_hard_failure` (0) when no tolerance was named. Judged figures: every named
metric overall, and in every group with at least `--min-group-size` cases whose figure is
not itself `insufficient_cases` (a group the *candidate* thinned below the minimum is
still judged, and is `inconclusive`; only a figure with too few **baseline** cases is
listed, not judged). An empty suite, or a run with tolerances named but zero figures
judged, is `inconclusive` (exit 4), never a pass.

### 22.3 What it deliberately is not

- **Not a promotion path.** Nothing is applied. The modules import no admin route, no
  application store and no HTTP client and call nothing that writes; the only writes are
  the runner's, under `--out` (E11). The handoff's "promotion gate" is, here, a check an
  operator runs before they edit a file.
- **Not a ranking.** The report orders nothing by size and names no model or endpoint as
  better.
- **Not a test of routing keys the bench cannot evaluate.** The escalation threshold and
  temperature, the escalation and failover bounds, the tier list and the pool are refused
  with a reason. Accepting them would produce a false "no regression".
- **Not corrected for multiplicity.** More groups mean more judged figures and more chances
  of `regressed` or `inconclusive`. For a check that is the safe direction; it is not a
  calibrated error rate.
- **Not over seeds.** One run is one simulation seed and one case order (§22.5.3).

### 22.4 Invariants, and the test that pins each

All under `tests/eval/`. "Falsified" means the rule was broken on purpose and the named
test failed (§22.5.4).

| # | Invariant | Pinned by | Falsified? |
|---|---|---|---|
| E1 | With no new flag the output is byte-identical to the pre-slice tree | `test_bench_golden.py::test_e1_default_output_is_byte_identical` (four cases, written before any product code of the slice; the commit is in the fixture); `::test_e1_the_default_run_writes_exactly_the_five_files`; `test_group_report_run.py::test_e1_the_flag_adds_two_files_and_changes_no_other` | **Yes** (M15) |
| E2 | Same inputs and flags give byte-identical new files | `test_group_report_run.py::test_e2_the_group_report_is_byte_identical_twice`; `test_version_check_run.py::test_e2_the_check_is_byte_identical_twice` | n/a |
| E3 | Each case is in exactly one group per dimension; sizes sum to the suite | `test_groups.py::test_e3_every_case_is_in_exactly_one_group_per_dimension`, `::test_e3_group_sizes_sum_to_the_total`, `::test_e3_an_untagged_case_is_counted_not_dropped`; `test_group_report_run.py::test_e3_the_run_partitions_its_cases` | Yes (M8) |
| E4 | Overall = the groups combined by each figure's own denominator; by case count is wrong for a conditional metric | `test_groups.py::test_e4_overall_is_the_denominator_weighted_combination` (7 metrics × 4 dimensions), `::test_e4_weighting_by_case_count_is_wrong_for_a_conditional_metric` | n/a (the second test is the counter-example) |
| E5 | Below the minimum: a count and no rate; on the figure's denominator; counts never suppressed; inclusive | `test_groups.py::test_e5_*` (five tests); `test_group_report_run.py::test_e5_an_injected_violation_shows_in_its_group_however_small`; `test_version_check.py::test_e5_*` (two tests) | Yes (M6, M9) |
| E6 | Group figures and the worst group match the hand-computed example | `test_groups.py::test_e6_*` (five tests) | Yes (M7) |
| E7 | Any ineligible choice by the candidate is `hard_fail`, exit 2, whatever the tolerances | `test_version_check.py::test_e7_*` (three tests); `test_version_check_run.py::test_e7_an_injected_ineligible_candidate_exits_2_whatever_the_tolerances`, `::test_e7_a_candidate_that_serves_an_empty_feasible_set_is_a_hard_fail` | **Yes** (M3, M4): the run tests inject the fault |
| E8 | Candidate = baseline: worsening 0, range `[0, 0]`, exit 0 at zero tolerance | `test_version_check.py::test_e8_identical_arms_are_no_change_everywhere`; `test_version_check_run.py::test_e8_a_candidate_that_restates_the_config_passes_with_zero_tolerance` | n/a |
| E9 | The check does not depend on case order | `test_version_check.py::test_e9_the_document_does_not_depend_on_case_order` | Yes (M2) |
| E10 | Worsening, the rule and the boundary match hand-computed examples | `test_version_check.py::test_e10_*` (four tests) | Yes (M1, M5) |
| E11 | Nothing is applied | `test_version_check_run.py::test_e11_the_check_writes_only_under_out_and_touches_no_config`, `::test_e11_the_new_modules_have_no_way_to_apply_anything`, `::test_e11_no_admin_route_or_setting_was_added` | n/a |
| E12 | No serving module imports `app.eval` | `test_isolation.py::test_no_serving_module_imports_app_eval` (unchanged) | Yes (its own scanner test) |
| E13 | No ranking, recommendation or promotion vocabulary | `test_groups.py::test_e13_no_key_or_sentence_ranks_or_recommends`; `test_version_check.py::test_e13_no_key_or_sentence_ranks_recommends_or_promotes` | n/a |
| E14 | The fixed text is the spec's and opens every new report | `test_groups.py::test_e14_*` (three tests); `test_version_check.py::test_e14_*` (two tests) | n/a |
| E15 | Nothing stated is silently ignored | `test_candidate.py::test_e15_*`; `test_version_check_run.py::test_e15_*`, `::test_cli_a_check_flag_without_a_candidate_is_refused`, `::test_cli_a_bad_check_value_is_refused`; `test_group_report_run.py::test_cli_a_minimum_without_the_report_is_refused` | Yes (M10) |
| E16 | The candidate adds arms and changes nothing else | `test_version_check_run.py::test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm`; `test_candidate.py::test_the_harness_settings_are_never_mutated` | Yes (M13) |
| E17 | A figure is judged on its whole range, never the point estimate alone | `test_version_check.py::test_e17_*` | n/a |

### 22.5 Results (simulated)

**Stamp.** Starter suite × `healthcare-sim.yaml`, simulated, seed 0 unless stated,
`seconds_per_case` 30, `attempt_cap` 3, 22 cases. `suite_hash` `3339c773…`,
`endpoints_hash` `04eccd8f…`, `base_routing_policy_version` `6e6fe2027862`,
`registry_version` `86294e62adea`. Quality in this profile is a hand-written probability
per endpoint and workload (§6.1). Reproduced 2026-10-07 against the code as built
(commit `afa03d2`); every number below was run, not copied.

#### 22.5.1 Group report, `router_online`, seed 0

Minimum 5. `-` is `insufficient cases`, with the `n` it had.

| dimension | group | cases | violations | pass rate | cost regret (USD) | latency regret (ms) |
|---|---|---|---|---|---|---|
| overall | all | 22 | 0 | 0.909 (n = 22) | 0.000032 (n = 20) | 30.7 (n = 20) |
| placement | any | 8 | 0 | 0.75 (n = 8) | 0.000107 (n = 6) | 102.5 (n = 6) |
| placement | us_only | 8 | 0 | 1.0 (n = 8) | 0 (n = 8) | 0 (n = 8) |
| placement | onprem_only | 4 | 0 | - (n = 4) | - (n = 4) | - (n = 4) |
| placement | escalated | 2 | 0 | - (n = 2) | - (n = 2) | - (n = 2) |
| workload | faq | 8 | 0 | 0.75 (n = 8) | 0.000107 (n = 6) | 102.5 (n = 6) |
| workload | claim_summary | 10 | 0 | 1.0 (n = 10) | 0 (n = 10) | 0 (n = 10) |
| workload | onprem_required | 4 | 0 | - (n = 4) | - (n = 4) | - (n = 4) |
| protection | enforced | 22 | 0 | 0.909 (n = 22) | 0.000032 (n = 20) | 30.7 (n = 20) |
| application | 4 groups | 8 / 8 / 4 / 2 | 0 | two printed, two not | | |

What this shows, and all it shows:

- **Every violation count is 0 in every group**, including the groups too small for a
  rate.
- **All of `router_online`'s regret on this suite is in one group**: the eight `faq` cases
  (which are also the `placement: any` cases and one application). That is §6.7 again — the
  tier rule excludes cheaper `mid` endpoints for short requests — now visible per group.
- **Two of four placement groups and one of three workload groups print no rate.** The
  `onprem_only` group, the one the product's main guarantee is about, has four cases.
  Before this slice a "worst group" could be one of those. The suite needs more cases
  there before a group claim about placement can be made.
- **Only one protection posture is in the starter suite** (`enforced`). There is no
  protection breakdown to read.
- Check on the weighted identity: pass rate `(8 × 0.75 + 8 × 1.0 + 4 × p₁ + 2 × p₂) / 22 =
  0.909` gives `4p₁ + 2p₂ = 6`, i.e. both unprinted groups passed every case; cost regret
  `(6 × 0.000107 + 8 × 0 + …) / 20 = 0.000032` with the unprinted groups at 0. ✓

#### 22.5.2 Three candidates, seed 0

Tolerances `pass_rate=0.02`, `mean_chosen_cost=0.0001`; 1000 resamples, level 0.90,
resampling seed 0.

| Candidate | Chains changed | Violations | Mean chosen cost: baseline → candidate, worsening `[range]` | Pass rate worsening `[range]` | Verdict |
|---|---|---|---|---|---|
| `same` (restates the shipped weights) | 0 of 22 | 0 | 0.001490 → 0.001490, 0 `[0, 0]` | 0 `[0, 0]` | `pass` (0) |
| `latency-only` (`weights: {cost: 0, latency: 2}`) | 16 of 22 | 0 | 0.001490 → 0.002596, +0.001106 `[0.000754, 0.001470]`, `worse` | −0.091 `[−0.182, 0]`, `not_detectable` | `regressed` (3) |
| `no-cheap-tier` (`complexity_thresholds: {cheap_max: 0}`) | 8 of 22 | 0 | 0.001490 → 0.001441, −0.000050 `[−0.000077, −0.000025]`, `better` | −0.091 `[−0.182, 0]`, `not_detectable` | `pass` (0) |

- `same` is the negative control: an overlay that changes nothing changes nothing, to the
  last digit.
- `latency-only` moves 16 of 22 choices and makes **no** ineligible choice: the eligible
  set is not the router's to change. It costs 74% more per request
  (`0.002596 / 0.001490 = 1.742`). The cost figure's whole range sits above the 0.0001
  tolerance, so it is `regressed` overall and in all seven groups with enough cases; the
  pass-rate figure's range reaches 0 (`not_detectable`) and stays `within_tolerance`
  because its whole range sits at or below 0.02.
- `no-cheap-tier` is the tier rule of §6.7 seen from the other side: with the cheap tier's
  upper bound at 0, short requests target `mid`, where this profile's cheaper endpoints
  are, and the candidate is cheaper. On simulated quality that proves nothing about
  whether it is a good idea.

#### 22.5.3 The same candidates at simulation seeds 0–9

Reproduced against the code as built; the verdict/exit columns differ from an earlier run
made under the check's first decision rule (§22.7) — the point values, ranges and
violation counts do not.

| Candidate | `pass` | `regressed` | `inconclusive` | `hard_fail` |
|---|---|---|---|---|
| `same` | 10 | 0 | 0 | 0 |
| `latency-only` | 0 | 10 | 0 | 0 |
| `no-cheap-tier` | 8 | 0 | 2 (seeds 3, 5) | 0 |

- A large effect is stable: `latency-only`'s cost worsening is +0.00109 to +0.00113 at
  every seed, and its range's lower end never comes near the tolerance, so it is
  `regressed` at all ten seeds under either decision rule.
- **A verdict that rests on simulated pass rate is not stable.** `no-cheap-tier` is `pass`
  at eight seeds and `inconclusive` at two: seed 3 (pass rate 0.818 → 0.773, worsening
  0.045 against a tolerance of 0.02, range `[0, 0.136]` — the range straddles the
  tolerance, so the whole-range rule calls it `inconclusive` with `point_beyond_tolerance`
  rather than `regressed`) and seed 5 (worsening −0.045, range `[−0.182, 0.091]`, which
  also straddles 0 and the tolerance). The pass or fail of a case is a seeded draw against
  a hand-written probability, so this is the harness's own noise, read through 22 cases.
  It is the measured form of the fixed caveat "the verdict is conditional on … this
  simulation seed".
- No candidate at any seed made an ineligible choice.

#### 22.5.4 The detectors, falsified

Fifteen single-rule mutations, each run against the named test file; all fifteen were
caught (2026-10-07, scratch tree, not committed):

| # | Mutation | Caught by |
|---|---|---|
| M1 | the tolerance boundary made exclusive | `test_e10_the_decision_rule_branch_by_branch` |
| M2 | the arms not sorted by case id | `test_e9_the_document_does_not_depend_on_case_order` |
| M3 | candidate violations ignored by the hard check | `test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances` |
| M4 | the run's exit code ignored by the hard check | `test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate` |
| M5 | the range never withholds a pass | `test_e10_the_decision_rule_branch_by_branch` |
| M6 | the minimum made exclusive | `test_e6_group_figures_match_the_hand_computed_example` |
| M7 | the worst `pass_rate` taken as the highest | `test_e6_the_worst_group_is_named_with_its_size` |
| M8 | untagged cases dropped | `test_e3_an_untagged_case_is_counted_not_dropped` |
| M9 | counts suppressed in a small group | `test_e5_a_violation_in_a_one_case_group_is_still_counted` |
| M10 | a key the bench cannot evaluate accepted | `test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason` |
| M11 | the two arms resampled separately | `test_the_draw_is_paired_and_seeded` |
| M12 | the check's exit code not returned | `test_a_candidate_that_costs_more_than_the_tolerance_exits_3` |
| M13 | the candidate built on the shared settings object | `test_the_harness_settings_are_never_mutated` |
| M14 | a floor miss scored where no endpoint passes (the per-case refactor) | `test_each_case_matches_the_hand_computed_matrix` |
| M15 | the same mutation, against the golden pin alone | `test_e1_default_output_is_byte_identical[starter_seed1]` |

M15 is why the golden pin has a fourth case. At seed 0 no starter case is infeasible at
floor, and the first three cases did not notice M14; at seed 1 some are, and the pin does.

#### 22.5.5 Live

**TO MEASURE.** No `--live` run has been made with either flag. Wanted (arxiv.md E11):

- the group report on a live suite with at least five cases in every placement and
  protection group, and at least two endpoints of different placement;
- the check for a weights candidate and a thresholds candidate on live endpoints, with the
  verdict over repeated runs (a live run has no seed: repeat it);
- the bootstrap range's coverage — split a larger suite in two and count how often one
  half's range contains the other half's worsening.

### 22.6 Limits: what this does NOT show

- **Simulated quality is an input** (§6.1). Every pass rate, quality regret and floor-miss
  figure in §22.5, and every verdict that rests on one, measures the harness. Cost and
  latency are simulated too: prices from the file over real token counts, latency from a
  declared mean and jitter.
- **The router comparison is mostly the tier rule** (§6.7). Both non-trivial candidates
  above act through it or around it.
- **22 synthetic, single-turn cases.** Five of the twelve groups are below five cases; the
  group that matters most (`onprem_only`) is one of them.
- **A group is a label the case author wrote.** A case tagged `placement: any` with an
  `onprem_only` policy is grouped as `any`. Grouping by the compiled policy is B67.
- **The range is over cases only.** It does not cover the simulation seed, the case order
  (the online arm learns in suite order), the endpoint file, or whether the suite
  resembles any real traffic. Its coverage has not been measured.
- **A percentile bootstrap on 22 cases is coarse.** A pass rate moves in steps of 1/22;
  several ranges above end exactly at 0.
- **No multiplicity correction** (§22.3).
- **One arm is compared.** `router_online`; the cold and warm-prior candidate arms are in
  `metrics.json` and are not judged.
- **Calibration and churn are not built.** Handoff §18 metrics 6, 7 and 8 are still not
  reported by any tool.

### 22.7 Amendment: the decision rule, and a recorded counterexample

The module shipped once with a first decision rule — `w > tol` alone failed a figure,
with the range only able to withhold a pass that the point estimate had already earned —
and was amended the same day, before this document's results were written, to the rule in
§22.2: a figure is judged on its **whole range** against the tolerance; the point estimate
never decides alone.

**Why.** An independent review simulated a candidate identical in truth — same
distribution, only which cases happened to be drawn differed — against both rules. Under
the first rule, that candidate was labelled `regressed` in 66–100% of draws on a 22-case
suite (20–100% at 200 cases), because five-case groups were being judged on their point
estimate alone: at that size, a point estimate crossing a small tolerance by chance is
common. The counterexample is recorded against commit `0a49682`.

A synthetic re-run under both rules (**SYNTHETIC**: 300 draws per row, 400 resamples, two
arms scored from one shared distribution so there is no true difference, a share `f` of
cases redrawn between draws; the script that produced this table is not in the repository):

| n | redrawn share | tolerances | figures judged | first rule: regressed | new rule: regressed | new rule: inconclusive | new rule: pass |
|---|---|---|---|---|---|---|---|
| 22 | 1.0 | pass_rate 0.05 | 14 | 90.3% | 24.7% | 74.3% | 1.0% |
| 22 | 1.0 | pass_rate 0.10 + mean_chosen_cost 0.25 | 28 | 97.3% | 30.7% | 69.3% | 0% |
| 22 | 1.0 | all eight metrics at 0 | 102 | 100% | 82.7% | 17.3% | 0% |
| 22 | 0.3 | pass_rate 0.05 | 14 | 66.3% | 2.0% | 66.7% | 31.3% |
| 22 | 0.3 | pass_rate 0.10 + mean_chosen_cost 0.25 | 28 | 73.0% | 0.3% | 92.7% | 7.0% |
| 22 | 0.3 | all eight metrics at 0 | 108 | 100% | 55.0% | 45.0% | 0% |
| 200 | 1.0 | pass_rate 0.05 | 14 | 74.7% | 9.3% | 90.7% | 0% |

Read honestly: the new rule still calls an identical-in-truth candidate `regressed` in a
quarter to a third of draws on 22 cases when every case is redrawn, because **no
correction is made for the number of figures judged** (14–28 figures, each a one-sided 5%
test; fixed text `CHECK_MULTIPLICITY`). A zero tolerance on an unchanged-in-truth metric is
a coin flip by construction. Do not present `regressed` as a detection with a known error
rate; it is a backlog item (a family-wise correction, B67 follow-up 4).

### 22.8 Amendments (as built)

Written against the code as it stands (commit `afa03d2`), after §22's text above was
drafted against an earlier revision of the module. Where the two differ, this section is
the correct one; §7.4 of
`docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` and its §16 deviation 8
describe the superseded first rule.

- **A. The decision rule** (§22.2, §22.7). Superseded: any sentence reading "the point
  estimate alone fails a figure", "the range does not excuse it", "a figure can be
  `regressed` and `not_detectable` at once" or "`lo` is never an input" described the
  first rule and is false of the code as built. The rule now judges the whole range:
  `lo > tol` → `regressed`, `hi <= tol` → `within_tolerance`, otherwise `inconclusive`
  (with `point_beyond_tolerance` when the point itself crosses `tol`). Exit codes are
  unchanged.
- **B. Other behaviour.** A non-finite figure (NaN/inf score) is `undefined`, never a
  pass, and `check.json` stays clean JSON. A group figure the **candidate** thinned below
  the minimum is judged and is `inconclusive`; only a figure with too few **baseline**
  cases is listed, not judged. An empty suite, or tolerances named with zero figures
  judged, is `inconclusive` (exit 4), never exit 0. `--check-replicates` must be at least
  `ceil(2 / (1 - interval))` (20 for the default 0.90) or the run exits 1. `check.json`
  gains `judged: {figures, metrics_named, metrics_available}` and `notes`; `check.md`
  prints "Figures judged: N (k of 8 metrics have a tolerance …)" and a note when
  `cost_regret`/`latency_regret` has a tolerance and `pass_rate` does not. The fixed text
  `CHECK_CONDITIONAL` and `CHECK_RANGE` were reworded for the new rule; `CHECK_MULTIPLICITY`
  and `CHECK_CONDITIONAL_METRICS` are new.
- **C. Group report.** `groups.md` gained a "Counts per strategy" table (cases,
  violations, empty-set mismatches for every strategy). The worst-group table gained a
  `groups` column ("k of m printed; t at this value"); a tie between some (not all)
  groups is listed, not broken; strategies print in sorted order. `groups.json`'s `worst`
  entries gained `groups_total` and `tied`. Fixed text gained `CAVEAT_WORST` (the worst
  group is a selection effect, and the same cases can carry several labels) and
  `CAVEAT_LABELS` (labels are the case author's tags, not the compiled policy). An unknown
  `mode` argument to `bench_caveats` now raises, instead of silently reading as a live run.
- **Not built.** The spec's §6 (churn robustness) and §9 (confidence calibration) are
  designed, not built — a second plan (§22 preamble, §6.5 status note above).

---

## 23. Live run on the DGX lab, one endpoint, three applications (2026-10-08)

The first run with **independent** rows. Every earlier lab export was a 24-request pattern
repeated (§18.4.1, §19.4.1, §20.6.2, §21.5.4a). Artifacts, all content-free:
`docs/research/data/live-dgx-lab-2026-10-08/` (the outcome export, the config snapshot, the
client log, the scenario and capacity files, every output quoted below, and the two scripts).

> **Correction to §18–§21.** Those sections cite artifact folders under
> `docs/research/results/`. That path is git-ignored (`.gitignore`: `results/`), so those
> folders were **never committed** and no longer exist. The figures quoted in §18–§21 stand
> as recorded at the time; they cannot be re-derived from the repository. From this section
> on, artifacts live under `docs/research/data/`.

### 23.1 Setup

- **Deployment.** DGX lab, api `0.30.0` (`main` at PR #92). One onboarded endpoint:
  `glm-5.3-flash` (vLLM, on-prem, tier `mid`, $10 / $10 per 1M tokens, context window
  262,144), declared for this run as region `us-lab`, `prompt_retention: none`,
  `accepts_raw_sensitive: true`. `qwen3-coder-next` (the endpoint of §18–§21) was no longer
  deployed.
- **Applications.** `paper-claims` (`max_cost_usd_per_request: 0.05`), `paper-dev` (no cap of
  its own) and `paper-clinic` (`onprem_only`, `allowed_regions: [us-lab]`,
  `require_no_retention`). All three `pii_detection: regex`, protection `enforced`. The
  deployment default `budgets.default_max_spend_usd` is 0.05, so the two apps without their
  own cap are capped at the same figure.
- **Traffic** (`traffic_generator.py`, seed 2). 900 requests, each drawn independently:
  application uniform over the three; content kind by application (prose, code, log lines,
  JSON; 15% a bare question); context length heavy-tailed, 0 to 9,000 words; `max_tokens`
  from {32, 48, 64, 96, 128}; a synthetic SSN and email in 50% of clinic requests and 15% of
  the others. Poisson arrivals at 1 per second, six client workers, 917 s. A 12-request trial
  (seed 1) precedes it in the export: **912 rows**.
- **What this is not.** Synthetic text (random words, generated code, log and JSON lines),
  single-turn, one endpoint, one tier. No request had more than one eligible endpoint, so
  nothing here exercises ranking, failover, the breaker, or a placement difference between
  endpoints. Those need a second endpoint and remain **TO MEASURE** (§23.7).

### 23.2 Outcome and replay agreement

876 served, 36 rejected, every rejection for `max_cost_usd_per_request` (claims 17, clinic
12, dev 7). The baseline replay reproduces the record exactly: model agreement 1.0 over 876
served rows, outcome agreement 1.0 over 912 (36 of 36 recorded rejections rejected in
replay; 0 routed-but-rejected, 0 rejected-but-routed), no row excluded.

### 23.3 Six scenarios, with 90% ranges

`policy_simulate.py --scenarios scenarios.json --uncertainty` (1000 resamples, seed 0, each
application its own stratum). Baseline cost of the served rows: $8.47.

| scenario | newly served | newly rejected | Δcost USD |
|---|---|---|---|
| `no-change` | 0 `[0, 0]` | 0 `[0, 0]` | 0 `[0, 0]` |
| `raise-request-budget` (0.5 for all three) | 36 `[27, 46]` | 0 | +3.16 `[2.34, 4.10]` |
| `tighten-claims-budget-0.02` | 0 | 29 `[21, 38]` | −0.68 `[−0.94, −0.46]` |
| `smaller-prompts-0.6` (`token_scale`) | 18 `[12, 25]` | 0 | −2.44 `[−2.77, −2.07]` |
| `window-4000` (endpoint context window) | 0 | 26 `[18, 35]` | −1.38 `[−1.84, −0.94]` |
| `clinic-eu-only` (`allowed_regions: [eu-lab]`) | 0 | 298 `[292, 303]` | −2.77 `[−3.11, −2.45]` |

`clinic-eu-only` rejects every clinic row that was served (298 of 310; the other 12 were
already rejected on budget): the one endpoint is declared `us-lab`. Route changes are 0 in
every scenario, as they must be with one endpoint.

### 23.4 The admission token estimate depends on the kind of text (B64)

`routing_tokens` (RouteForge's count, `len // 4` over the model-facing text) against the
backend's `prompt_tokens` (GLM's tokenizer), on served rows with at least 200 backend prompt
tokens:

| content | rows | median ratio | p10 | p90 |
|---|---|---|---|---|
| prose (random English words) | 274 | 1.72 | 1.63 | 1.76 |
| generated Python | 123 | 0.79 | 0.78 | 0.80 |
| JSON rows | 127 | 0.70 | 0.68 | 0.70 |
| log lines (timestamps, hex ids) | 121 | 0.52 | 0.52 | 0.53 |
| all | 645 | 0.80 | 0.52 | 1.75 |

Summed over all 645 rows the two counts nearly cancel (ratio 0.955), which hides the
effect. The estimate **over-counts prose by about 70% and under-counts log lines by about
half**. A per-request budget enforced on this estimate therefore rejects prose requests the
backend would have billed under the cap, and admits log-heavy requests it would have billed
over it. §19.4.1 saw only the first half (1.54 on 17 prose rows). The text is synthetic —
random words have no punctuation or short function words — so the prose figure is an upper
indication, not a measurement of real documents: **real-traffic ratio TO MEASURE.**

### 23.5 Recorded latency, wall-clock and the arrival estimate (E10c, E10e)

The client logged each request's start and end. On the 876 served rows:

| figure | median | p10 | p90 |
|---|---|---|---|
| recorded latency (ms; sum of the pipeline steps) | 497 | 264 | 865 |
| client wall-clock − recorded latency (ms) | 159 | 152 | 207 |
| recorded ÷ wall-clock | 0.75 | 0.61 | 0.84 |
| (`ts` − recorded latency) − client start (s) | 0.21 | 0.19 | 0.25 |

The recorded figure is short of client wall-clock by a near-constant ~160 ms (network, the
gate and everything outside the timed steps), so it understates time in the system by about
a quarter on these short requests; the capacity model's concurrency inherits that. The
arrival estimate is late by ~0.2 s, a figure that **includes any clock offset** between the
client and the API pod, which was not measured. Using `ts` in place of `ts − latency` moves
4.2% of rows to another bucket at 10 s, 0.9% at 60 s and none at 300 s. Wall-clock against
the backend's own time in service was not measured (no backend metrics were collected).

### 23.6 Capacity headline against bucket length (E10d)

Stated limits for the experiment (`capacity-assumptions.json`; **illustrative — not the
serving engine's configured limits**): 2 concurrent, 90 requests per minute, 150,000 tokens
per minute. The same export, the current arm:

| bucket | buckets | peak | average | over 100% | binding limit |
|---|---|---|---|---|---|
| 10 s | 95 | 212.1% | 27.0% | 1.1% | `max_concurrency` |
| 60 s | 17 | 87.8% | 57.3% | 0.0% | `requests_per_minute` |
| 300 s | 4 | 64.2% | 48.7% | 0.0% | `requests_per_minute` |
| 3600 s | 1 | 16.2% | 16.2% | 0.0% | `requests_per_minute` |

One recording reads from "twice over" to "one sixth used" as the bucket goes from 10 s to an
hour, and the binding limit changes. Two effects: a peak is a maximum over more, shorter
buckets; and the window is whole buckets, so a 15-minute run in a one-hour bucket is
averaged over the hour. Under `volume-x3` the 60 s headline is peak 263.3% / average 171.8%,
over 100% in 88.2% of 17 buckets. Mean recorded service time 587 ms, longest 8,686 ms, all
876 rows `recorded`.

### 23.7 Range width against sample size, and split-half behaviour (E9a, E9b)

`width_coverage_experiment.py`; four scenarios, 1000 resamples each.

**Width against n.** Eight random subsamples at each size. Mean range width of the count,
divided by n, times √n — constant if the width shrinks as 1/√n:

| figure | n = 50 | 100 | 200 | 400 | 800 |
|---|---|---|---|---|---|
| `raise-request-budget`, newly served | 0.39 | 0.50 | 0.67 | 0.68 | 0.62 |
| `tighten-claims-budget-0.02`, newly rejected | 0.41 | 0.55 | 0.53 | 0.54 | 0.56 |
| `window-4000`, newly rejected | 0.58 | 0.59 | 0.53 | 0.51 | 0.55 |
| `smaller-prompts-0.6`, newly served | 0.23 | 0.40 | 0.49 | 0.48 | 0.46 |

From n = 200 the normalised width is flat (within sampling noise of eight subsamples), i.e.
the range narrows as 1/√n. Below that it is **narrower** than that law predicts: these are
rare events (about 4% of rows), and at n = 50 a resample holds only a handful, so the
percentile interval is squeezed against 0. At n = 50 all three applications are below
`--min-rows` and are pooled into one stratum. 5,000 and 50,000 rows: **TO MEASURE.**

**Split halves.** 100 random splits into two halves of 456 rows. How often the half-A 90%
range contains the half-B point estimate:

| figure | contained |
|---|---|
| `raise-request-budget`, newly served / Δcost | 72% / 73% |
| `tighten-claims-budget-0.02`, newly rejected / Δcost | 70% / 66% |
| `window-4000`, newly rejected / Δcost | 73% / 74% |
| `smaller-prompts-0.6`, newly served / Δcost | 76% / 68% |

This is **not** a coverage shortfall, and the measurement as §20.6.3 defined it was the
wrong test. A 90% range is about where the true value lies; the half-B figure is itself an
estimate with the same sampling error, so the difference of the two has twice the variance,
and a calibrated 90% range (±1.645σ) contains an independent equal-sized estimate with
probability P(|Z| < 1.645/√2) = **75.5%**. The eight observed shares average 71.5%, each
from 100 splits (standard error about 4 points, and the eight are not independent) —
consistent with a calibrated range, possibly a little narrow. A direct coverage test needs a
known truth: resample from a much larger export and check the range against the
full-export figure. **TO MEASURE** (with E9a's larger exports).

### 23.8 Still to measure, and what each needs

| item | needs |
|---|---|
| ranking, failover, breaker, overflow re-selection on live traffic (E4, E7) | a second endpoint |
| the healthcare scenario triad; the shadow-price counterexample live (E6, E8, E10a) | two endpoints of different placement or declaration |
| group report and router-version check on live endpoints (E11d) | two endpoints; ≥ 5 cases per group |
| token-estimate ratio on real documents (E7c) | real (non-synthetic) text |
| direct coverage; width at 5,000 and 50,000 rows (E9a, E9b) | a larger export |
| coverage under dependence (E9c) | agent-loop traffic |
| stated capacity against observed behaviour; backend time in service (E10b, E10c) | the engine's configured limits and metrics |

## 24. Live run 2 on the DGX lab: three endpoint names, one outage, and agent-loop rows (2026-10-08)

Artifacts, all content-free: `docs/research/data/live-dgx-lab-2026-10-08-run2/` (outcome
export, config snapshot, client log, outage log, scenario and capacity files, every output
quoted below, the bench output, the generator, and the agent-loop export).

> **What this run is and is not.** The lab has one model server. The three endpoint names
> are **one model on one server**: `glm-5.3-flash` directly, and `glm-flash-east` and
> `glm-flash-econ` through a small test proxy (`deploy/dgx_lab/alias-proxy.yaml`) that
> renames the model and can be told to refuse one name. They differ only in what the
> operator **declared** at onboarding. So this run measures eligibility, routing, failover,
> the breaker and replay across endpoints with different declarations and prices. It says
> **nothing** about quality differences, and nothing about cloud against on-prem placement
> (all three are provider `onprem`). The request text is synthetic.

### 24.1 Setup

| endpoint name | tier | declared price in/out, $ per 1M | region | prompt retention | accepts raw sensitive |
|---|---|---|---|---|---|
| `glm-5.3-flash` | mid | 10 / 10 | us-lab | none | true |
| `glm-flash-east` | mid | 6 / 6 | eu-lab | none | true |
| `glm-flash-econ` | cheap | 1 / 1 | us-lab | retained | false |

Applications as in §23.1: `paper-claims` (per-request cap $0.05), `paper-dev`,
`paper-clinic` (`onprem_only`, `allowed_regions: [us-lab]`, `require_no_retention`), all
`pii_detection: regex`. 900 requests at 1 per second, seed 3. 300 s into the run the proxy
was told to refuse `glm-flash-east`; 181.5 s later it was told to serve it again
(`outage.log`).

### 24.2 Outcomes and attempt chains

880 served, 18 `no_eligible` (budget), 2 `backend_failed`.

| attempt chain | rows |
|---|---|
| `glm-5.3-flash` | 394 |
| `glm-flash-econ` | 252 |
| `glm-flash-east` | 230 |
| `glm-flash-east` → `glm-5.3-flash` | 5 |
| `glm-flash-east` → `glm-flash-econ` | 1 |
| none (rejected before routing) | 18 |

### 24.3 The outage: failover and the breaker (E4, live)

Times are seconds after the proxy began refusing `glm-flash-east`.

- Three requests in a row failed on it at 3.3 s, 6.8 s and 8.2 s. Each failed over and was
  served. The third failure opened the breaker.
- 65 rows carry `circuit_open` for `glm-flash-east`, from 19.7 s to 219.4 s. The endpoint
  was back at 181.5 s, so the breaker stayed open about 38 s after recovery.
- Half-open probes were sent at about 33.9, 40.2, 41.3, 62.7 and 101.2 s.
- Of the 58 routed mid-tier rows from the two unrestricted applications in the outage
  window, **8 touched the dead endpoint**; the breaker kept the rest off it.
- **Two requests failed with 502** (5,877 and 7,207 routing tokens). Under the $0.05 cap only
  the $6 endpoint was affordable, so there was no second candidate to fail over to. Their
  outcome rows record `eligible: []` and `tier_target: null`.
- One row has `circuit_bypass`: 7,555 tokens, only `glm-flash-east` in budget. It bypassed
  the open breaker, failed, and was served by `glm-flash-econ`, one tier down.

These are filed as B70. One outage, one endpoint, one run: this is an observation of the
mechanism, not a rate.

### 24.4 Replay agreement

| | value |
|---|---|
| outcome agreement (served / rejected / failed), 900 rows | 1.0 |
| model agreement, 880 served rows | 0.926 |
| by application: claims / dev / clinic | 0.872 / 0.891 / 1.0 |

All 65 model disagreements have the same shape: live chose `glm-5.3-flash`, replay chose
`glm-flash-east`. They are exactly the rows that carry `circuit_open`. Replay does not model
breaker state, so it sends those requests to the endpoint the live router was avoiding.
`paper-clinic` could never use `glm-flash-east` (region), which is why it agrees fully.

### 24.5 Scenarios, with 90% ranges

Baseline cost over the export: $6.00. Ranges are the paired bootstrap of §20.

| scenario | route changed | newly rejected | newly served | Δ cost, $ |
|---|---|---|---|---|
| `all-apps-no-retention` | 248 [230, 268] | 4 [1, 7] | 0 | +0.438 [0.381, 0.495] |
| `all-apps-us-lab-only` | 296 [277, 314] | 5 [2, 9] | 0 | +1.172 [0.839, 1.503] |
| `clinic-may-use-eu-lab` | 329 [324, 334] | 0 | 10 | −0.626 [−0.858, −0.357] |
| `raise-request-budget` | 4 [1, 7] | 0 | 18 | +1.455 [0.925, 2.005] |

Moves: no-retention sends 248 rows from `econ` to `east` and rejects 4; us-lab-only sends
286 from `east` to `glm-5.3-flash`, 10 from `east` to `econ`, and rejects 5. A tightening
that looks free on a static check (another endpoint still qualifies) rejected 4 and 5
requests here, because the remaining endpoint was over those requests' budget.

### 24.6 Shadow prices (E6: not exercised)

| application | requirement relaxed | route changes | rejections avoided | Δ cost, $ |
|---|---|---|---|---|
| clinic | `allowed_regions` | 329 | 10 [5, 15] | −1.037 [−1.155, −0.926] |
| clinic | `require_no_retention` | 78 | 8 [4, 13] | −0.163 |
| clinic | `max_cost_usd_per_request` | — | 12 [7, 17] | — |
| dev / claims | `risk_floor` | 36 / 39 | 3 / 3 | — |
| dev / claims | `max_cost_usd_per_request` | — | 3 / 3 | — |

**Newly rejected was 0 on every relaxation.** The non-monotonicity counterexample of §17.3
(relaxing one requirement makes a request newly rejected) did not occur in this traffic. It
remains a constructed example, not a live observation.

### 24.7 Capacity under each scenario (E10a analogue)

Illustrative stated limits, the same for each name: 2 concurrent, 40 requests per minute,
80,000 tokens per minute; 60 s buckets, 17 buckets. The limits are assumptions, not the
server's configuration.

| arm | busiest endpoint | peak | time-average | buckets over |
|---|---|---|---|---|
| no change | `glm-5.3-flash` | 77.5% | 48.4% | 0 |
| `all-apps-no-retention` | `glm-flash-east` | 115.0% | 80.7% | 17.6% |
| `all-apps-us-lab-only` | `glm-5.3-flash` | 127.5% | 90.4% | 47.1% |
| `clinic-may-use-eu-lab` | `glm-flash-east` | 132.5% | 94.1% | 52.9% |
| `raise-request-budget` | `glm-5.3-flash` | 77.5% | 50.1% | 0 |

Three of the four changes push one endpoint over its stated limit in some minutes while the
no-change arm stays under. That is the point of reading capacity beside the scenario: the
route and cost table alone does not show it. Because the three names are one server, the
real server saw the same total load in every arm; the table is about stated limits only.

### 24.8 Group report and router-version check on live endpoints (E11d)

`feasible_bench --live` on the 22-case starter suite, three endpoint names, 66 calls, seed
0, with `--group-report` and a candidate overlay `weights: {cost: 0, latency: 2}`,
tolerances `pass_rate` 0.05 and `mean_chosen_cost` 0.0005. Output in `bench/`.

- **Check verdict: PASS, exit 0.** 16 figures judged, all `within_tolerance`, every range
  `[0, 0]`. **The candidate changed the attempt chain on 0 of 22 cases**, so this is the
  tool running end to end on live endpoints, not evidence that it can detect a regression.
  On this suite the tier rule and the feasible set decide the route; the scoring weights
  never got a choice to change.
- Hard checks: 0 candidate attempts outside the eligible set; 0 on an empty feasible set.
- Group report, `router_online`: violations 0 of 22; pass rate 0.727 overall, 1.0 for
  `placement=any` (n = 8), 0.375 for `placement=us_only` (n = 8), 0.5 for
  `workload=claim_summary` (n = 10). `escalated` (n = 2) and `onprem_only` (n = 4) are below
  the minimum of 5 and print no rate. Why the `us_only` cases fail the grader was **not
  investigated**.
- Every strategy shows the same pass rates, as expected when all endpoints are one model.

The planned E11d asked for at least 5 cases in each placement and protection group and two
placements. This run has one protection group and two placement groups below 5, so E11d is
**partly** met: a larger suite is still needed.

### 24.9 Agent-loop rows from a real coding client

The owner used a coding agent (opencode) through RouteForge on the same lab, application
`opencode`, enforced posture with `presidio`, and allowed its content-free outcome rows to
be used. 40 rows over about 15 minutes, one session (`outcomes-opencode-agent-loop.ndjson`).

| | value |
|---|---|
| rows | 40: 21 served, 19 `no_eligible` |
| the 19 rejections | all `max_cost_usd_per_request` (cap $0.05, before it was raised); routing tokens 14,256 to 14,264 — about four requests, retried by the client |
| backend prompt tokens, served rows | 13,962 to 27,480, median 24,965 |
| completion tokens, median | 35 |
| routing estimate / backend prompt tokens | summed 0.976; per row 0.960 to 1.022, median 0.976 |
| recorded latency | median 441 ms, 90th percentile 1,162 ms |
| cost recorded for 21 calls | $4.98 at the declared $10 per 1M |
| entity types detected | `PERSON`, `LOCATION`, `NRP`, `DATE_TIME`, `URL`, `US_DRIVER_LICENSE` on all 40; `IP_ADDRESS` on 16 |

Three things this shows that the synthetic runs could not:

1. **The first agent call is already large.** The smallest request was about 14,000 tokens
   (system prompt plus tool definitions). A per-request cap set for chat traffic rejects an
   agent client on its first call.
2. **On real agent prompts the token estimate was close**: 0.976 summed and 0.96 to 1.02 per
   row, against 0.52 to 1.72 by kind of text in §23.4. This is 21 rows from one session and
   one client, whose prompts share most of their text, so the rows are **not independent**
   and it is one data point for E7c, not a distribution.
3. **A general-purpose detector fires on every coding prompt.** The same six entity types
   are detected on every row, which is what a constant system prompt would produce. On the
   output side this broke tool calls (B69): `xargs -0 -n1` came back as
   `xargs -0 -<US_DRIVER_LICENSE_1>`.

These rows are short and dependent (each call carries the conversation so far). They are a
first look at agent-loop traffic for E9c, far too few to measure coverage under dependence.

### 24.10 Still to measure

| item | needs |
|---|---|
| quality-sensitive routing; cloud against on-prem placement (E8) | endpoints that are different models, and a second provider |
| shadow-price counterexample, live (E6) | a fixture built to produce it; ordinary traffic did not |
| a check that detects a real difference (E11d) | a suite where scoring weights change a route; ≥ 5 cases per group |
| replay with breaker state (E7) | B70 item 5 |
| token-estimate ratio as a distribution on real text (E7c) | many sessions and clients |
| coverage under dependence; width at 5,000 and 50,000 rows (E9a–c) | a long agent-loop export |
| stated capacity against observed behaviour (E10b, E10c) | the engine's configured limits and metrics |

## 25. Live run 3 on the DGX lab: four different models, a cloud path, and a provider that ran out (2026-10-09)

Artifacts, all content-free: `docs/research/data/live-dgx-lab-2026-10-09-run3/` (outcome
export, config snapshot, client log, scenario and capacity files, every output quoted
below, the bench output, the generator).

This is the first run where the endpoints are **different models** and where one provider
is **off the customer's infrastructure**.

> **Read this first.** The cloud provider is OpenRouter's free model variants. The account
> used allows **50 free requests a day**. That allowance ran out 6 minutes into the
> 30-minute run; every cloud call after that was refused (HTTP 429,
> `free-models-per-day`). So this run measures two things, in sequence: routing across
> four working endpoints (6 minutes, 31 cloud answers), and a provider that is unavailable
> for 24 minutes. It does **not** measure steady-state cloud share, cloud quality, or a
> saving against a frontier baseline. The request text is synthetic. Prices are stated
> (§25.1), not bills.

### 25.1 Setup

| endpoint | provider | tier | stated price in/out, $ per 1M | region | prompt retention | accepts raw sensitive |
|---|---|---|---|---|---|---|
| `qwen3.6-35b` (Qwen3.6-35B-A3B, FP8, one H200) | onprem | cheap | 1 / 1 | us-lab | none | true |
| `glm-5.3-flash` | onprem | mid | 10 / 10 | us-lab | none | true |
| `openrouter-mid` (Nemotron 3 Super 120B, free variant) | openrouter | mid | 0.08 / 0.45 | undeclared | retained | false |
| `openrouter-frontier` (Nemotron 3 Ultra 550B, free variant) | openrouter | frontier | 0.50 / 2.20 | undeclared | retained | false |

The on-prem prices are illustrative. The cloud prices are the same models' paid list
prices, used so that a $0 endpoint does not win every cost comparison. With these numbers
the cloud is cheaper than the on-prem mid tier, so **no saving against a frontier
baseline can be read from this run**; that needs prices an operator actually pays.

The cloud provider is declared through a deployment config overlay
(`deploy/dgx_lab/config-overlay/`, PR #98). Applications as before: `paper-claims` (cap
$0.05 a request), `paper-dev`, `paper-clinic` (`onprem_only`, `allowed_regions: [us-lab]`,
`require_no_retention`), all `pii_detection: regex`. 900 requests at one every two seconds,
seed 4, `max_tokens` 768 to 1,536 (see §25.7 for why). 899 outcome rows were recorded; one
request timed out at the client before RouteForge answered and has no row.

### 25.2 Outcomes

878 served, 21 `no_eligible` (budget). **No request failed**, although one provider was
down for most of the run.

| served by | rows | share of served |
|---|---|---|
| `glm-5.3-flash` | 543 | 61.8% |
| `qwen3.6-35b` | 304 | 34.6% |
| `openrouter-mid` | 19 | 2.2% |
| `openrouter-frontier` | 12 | 1.4% |

Recorded latency, median / 90th percentile, ms: Qwen 2,225 / 3,813; GLM 2,627 / 5,059;
cloud mid 4,971 / 11,252 (n = 19); cloud frontier 18,965 / 25,028 (n = 12).

### 25.3 Placement held (rule 3, live)

- `paper-clinic` (325 rows) was never offered a cloud endpoint. A pinned request for
  `openrouter-mid` from it returns 422, "provider 'openrouter' is excluded by residency".
- **173 rows from the two unrestricted applications were moved on-prem by detection**: the
  request carried a detected entity, the sensitivity rule raised placement to
  `onprem_only`, and the row records it. None of them was attempted on a cloud endpoint.
- Of those 173, 84 were the planted SSN and e-mail. **63 were `CREDIT_CARD` +
  `US_BANK_NUMBER` matches on random numbers** in synthetic logs and JSON, which carried no
  planted PII. A false positive here costs money, not privacy: the request stays on-prem
  at the on-prem price. It is a real cost of a pattern detector and should be reported as
  one.

### 25.4 A provider that ran out: failover, the breaker, and the bypass (E4)

Cloud calls were answered from 13:41:42 to 13:47:41 UTC (31 answers, with 7 upstream
"service overloaded" errors among them). From 13:47:42 every cloud call returned 429 for
the daily allowance; the retained API log has 94 such errors.

| endpoint | attempts | served | failed |
|---|---|---|---|
| `openrouter-frontier` | 91 | 12 | 79 |
| `openrouter-mid` | 41 | 19 | 22 |
| `qwen3.6-35b` | 305 | 304 | 1 |
| `glm-5.3-flash` | 543 | 543 | 0 |

86 served requests needed a failover, 102 attempts were wasted, and every one of the 86
was served.

- **Mid tier: the breaker worked.** `openrouter-mid` shares the mid tier with
  `glm-5.3-flash`. Its breaker opened and 71 of the 89 unrestricted mid-tier rows record
  it as `circuit_open`; they went straight to GLM with no wasted call.
- **Frontier tier: the breaker could not help.** `openrouter-frontier` is the only
  frontier endpoint. With its breaker open there is no other endpoint at the target tier,
  so the request **bypasses** the breaker (63 rows carry `circuit_bypass`), calls the dead
  endpoint, fails, and only then fails over. 79 of the 102 wasted attempts are these. The
  bypass exists so a request is never refused only because a breaker is open; with a
  lower-tier endpoint available it buys nothing and costs one failed call per request.
- **Where the frontier requests ended up:** `glm-5.3-flash` 59, `qwen3.6-35b` 13,
  `openrouter-mid` 7. Thirteen frontier-target requests were served by the **cheap**
  endpoint, because `openrouter-mid` was tried next and Qwen after it. This is the
  failover-down-a-tier behaviour of B70, now at larger scale.

### 25.5 Replay agreement when endpoints fail (E7)

| | value |
|---|---|
| outcome agreement, 899 rows | 1.0 |
| model agreement, 878 served rows | 0.919 |
| by application: claims / dev / clinic | 0.878 / 0.870 / 1.0 |
| cost of the served rows, recorded | $9.76 |
| cost of the same rows in replay's current arm | $7.26 |

Replay assumes every eligible endpoint answers. It sends 180 rows to the cloud; live, 31
were answered there. So replay's "current" arm is a healthy deployment that did not exist
for 24 of the 30 minutes, and its baseline cost is $2.50 (26%) below what was recorded.
`paper-clinic` agrees fully because it has no cloud candidate. **Every scenario figure in
§25.6 is a statement about the healthy deployment**, not about the half hour that
happened. Replaying recorded endpoint failures is B70 item 5, extended in B71.

### 25.6 The placement scenarios (E8), with 90% ranges

Baseline: replay's current arm, $7.26 over 899 rows, on-prem share of served rows 0.795.

| scenario | route changed | newly rejected | newly served | Δ cost, $ | on-prem share after |
|---|---|---|---|---|---|
| `all-apps-onprem-only` | 180 [163, 199] | 0 | 0 | +3.12 [2.69, 3.54] | 1.0 |
| `all-apps-no-retention` | 180 [163, 199] | 0 | 0 | +3.12 [2.69, 3.54] | 1.0 |
| `clinic-may-use-cloud` | 59 [48, 70] | 0 | 0 | −0.76 [−0.95, −0.59] | 0.728 |
| `raise-request-budget` | 6 [2, 10] | 0 | 21 [14, 28] | +0.22 [0.07, 0.38] | 0.800 |

- Keeping the two unrestricted applications on-prem moves 180 requests and adds $3.12
  (43% of the baseline) at the stated prices, and rejects nothing, because an on-prem
  endpoint exists at every tier those requests can fall to.
- `require_no_retention` produces the **same** 180 moves by a different requirement: the
  cloud endpoints are declared `retained`. Two different policies, one effect, and the
  report shows each under its own name.
- Letting the clinic application use the cloud moves 59 of its 325 requests. The other
  266 stay on-prem in the replay; the cheap tier has only an on-prem endpoint, and
  detection still raises placement for requests that carry an entity. The report does
  not split the 266 between those two reasons.
- The replay also reports a p95 latency change of −15.2 s for the on-prem arms: it is the
  slow free cloud frontier endpoint leaving the mix, measured on 12 recorded calls.

Shadow prices (`shadow-prices/report.md`): relaxing `residency` for `paper-claims` changes
96 [83, 110] routes and saves $1.53 [1.26, 1.84]; for `paper-dev`, 68 [56, 79] and $1.19
[0.92, 1.48]; each labelled "price of a protection — not a recommendation". **Newly
rejected is 0 on every relaxation**, so the counterexample of §17.3 (E6) is again not
exercised by ordinary traffic.

### 25.7 Thinking models and the token limit

Both on-prem models reason before they answer, and the reasoning is billed as completion
tokens. Qwen spent 137 of 142 completion tokens on reasoning for a one-word answer. With
the 32 to 128 token limits of runs 1 and 2 the answer would have been empty, so this run
used 768 to 1,536.

| served by | answers with no text | median completion tokens |
|---|---|---|
| `qwen3.6-35b` | 141 of 304 (46%) | 768 |
| `glm-5.3-flash` | 87 of 543 (16%) | 441 |
| `openrouter-mid` | 5 of 19 | 356 |
| `openrouter-frontier` | 0 of 12 | 220 |

An empty answer is an HTTP 200 with a full bill. RouteForge records it as served; nothing
on the outcome row distinguishes it. The prompts are synthetic word salad, which invites
long reasoning, so the rates are specific to this traffic; the mechanism is not.

### 25.8 Stated capacity against what the provider did (E10b)

Stated limits: on-prem endpoints 2 concurrent, 40 requests and 80,000 tokens a minute
(illustrative); cloud endpoints **20 requests a minute, the provider's published limit
for free models**. 60 s buckets, 31 buckets.

| arm | busiest stated limit | peak | time-average | buckets over |
|---|---|---|---|---|
| no change | `glm-5.3-flash`, concurrency | 63.1% | 33.2% | 0 |
| `all-apps-onprem-only` | `glm-5.3-flash`, concurrency | 82.6% | 45.9% | 0 |
| `clinic-may-use-cloud` | `qwen3.6-35b`, concurrency | 58.8% | 17.6% | 0 |

Replayed cloud demand peaked at 6 requests a minute, **30% of the stated 20**. The model
says the cloud endpoints had ample room. They were refusing every call. The limit that
bound was a **daily** allowance of 50, which the capacity file has no field for. This is
the caveat of §21 observed: the comparison is only as good as the limits someone states,
and a stated per-minute limit says nothing about a quota on another time scale.

### 25.9 The benchmark on different models (E11d)

`feasible_bench --live`, 22-case starter suite, four endpoints, one call per case and
endpoint, seed 0, group report, candidate overlay `weights: {cost: 3.0, latency: 0}`. It
ran after the daily allowance was spent, so **all 16 cloud calls failed and the cloud
endpoints are not measured**.

| endpoint | tier | cases it was eligible for | passed |
|---|---|---|---|
| `qwen3.6-35b` | cheap | 22 | 18 |
| `glm-5.3-flash` | mid | 22 | 16 |

| strategy | pass rate | quality regret | mean chosen cost, $ |
|---|---|---|---|
| `cheapest` | 0.818 | 0.091 | 0.0005 |
| `best_published_quality` | 0.727 | 0.182 | 0.0064 |
| `router_online` | 0.682 | 0.227 | 0.0057 |

- **The cheap model passed more cases than the mid model**, and "always the cheapest"
  beat the router on pass rate at roughly a tenth of the cost. The router's quality term for an
  endpoint with no published score is its tier's default (mid 0.75, cheap 0.5), and for
  this pair the tier order is the wrong way round. 22 cases, one sample each, a
  deterministic grader and no published quality for either model: it is one observation
  that a tier is not a quality measurement, not a ranking of the two models.
- Violations: 0 of 22 for every strategy. No strategy attempted an endpoint outside a
  case's eligible set.
- **Version check: PASS, and the candidate again changed 0 of 22 attempt chains.** On
  this suite the target tier comes from request length and each on-prem tier has one
  endpoint, so no weight has a choice to change. The check still has not been shown a
  difference to detect on live endpoints.

### 25.10 What this run adds, and what it does not

Adds: placement enforced against a real off-premises provider; detection moving requests
on-prem; a real provider outage absorbed with no failed request; the breaker's limit when
a tier has one endpoint; replay's blind spot for failures, with its size in dollars; a
stated capacity limit that was not the binding one; a tier default contradicted by
measurement.

Does not add: steady-state cloud share; cloud quality; a saving against a frontier
baseline; a live shadow-price counterexample; a version check that detects something.

| still to measure | needs |
|---|---|
| steady-state routing with a working cloud path; cloud rows in the benchmark | the provider's 1,000-a-day allowance (bought credits), then a re-run |
| saving against an all-frontier baseline | prices an operator pays, not stated ones |
| a check that detects a real difference | two endpoints in one tier, or a suite with longer cases |
| quality per endpoint as more than a tier default | a larger graded suite; several samples per case |
| shadow-price counterexample, live (E6) | a fixture built to produce it |
| coverage under dependence; width at 5,000 and 50,000 rows (E9a–c) | a long agent-loop export |

## 26. Live run 4: the same four models with the cloud path working (2026-10-09)

Artifacts, all content-free: `docs/research/data/live-dgx-lab-2026-10-09-run4/`.

Run 3 (§25) lost its cloud provider after 6 minutes. This is the same setup (§25.1), same
generator, seed 5, after the provider account's allowance was raised to 1,000 free
requests a day. 900 requests, one every two seconds, 900 outcome rows. Synthetic text;
stated prices; one run.

### 26.1 Outcomes

878 served, 22 `no_eligible` (budget), 0 failed.

| served by | rows | share of served | recorded latency, median / 90th pct, ms | answers with no text |
|---|---|---|---|---|
| `glm-5.3-flash` (on-prem, mid) | 376 | 42.8% | 2,692 / 4,576 | 74 (20%) |
| `qwen3.6-35b` (on-prem, cheap) | 324 | 36.9% | 2,256 / 3,753 | 139 (43%) |
| `openrouter-mid` (cloud) | 119 | 13.6% | 4,184 / 10,907 | 25 (21%) |
| `openrouter-frontier` (cloud) | 59 | 6.7% | 12,076 / 45,685 | 0 |

Cloud share of served requests: **20.3%** (178 of 878). Recorded cost $6.93, of which
on-prem mid $6.30, on-prem cheap $0.43, cloud $0.20 at stated prices.

### 26.2 Placement and scoring

- Of 411 requests free to go anywhere, the 84 that targeted the mid tier had both mid
  endpoints eligible and **all 84 went to the cloud one**: the scorer's cost term decides
  when two endpoints share a tier and a quality default.
- 316 `paper-clinic` rows and **173 detection-raised rows** from the other two
  applications were never attempted on a cloud endpoint. 75 of the 173 carried the planted
  SSN and e-mail; 83 matched `CREDIT_CARD` or `US_BANK_NUMBER` on random numbers with no
  planted PII (§25.3's false-positive cost, repeated).
- 15 mid-target on-prem requests were served by the cheap endpoint with a single attempt:
  consistent with the per-request budget leaving only the cheap endpoint affordable (not
checked row by row).

### 26.3 Failures

The cloud frontier endpoint returned "service temporarily overloaded" on 35 of 94
attempts. All 35 failed over to `openrouter-mid` and were served: 35 frontier-target
requests answered one tier down, 35 wasted attempts, no failed request. 7 rows carry
`circuit_bypass`, so the frontier endpoint's breaker was open at times and, being alone in
its tier, was bypassed (B71 item 1). No row records a `circuit_open` exclusion.

### 26.4 Replay agreement

| | value |
|---|---|
| outcome agreement, 900 rows | 1.0 |
| model agreement (first decision), 878 served rows | 1.0 |
| recorded cost / replayed current-arm cost | $6.93 / $7.02 |

With endpoints mostly answering, replay reproduces every first decision and its cost is
within 1.2% of the recorded cost. Run 3's 0.919 and 26% gap were the outage, not the
method. The 35 failover rows agree because agreement compares the first decision.

### 26.5 The placement scenarios (E8), with 90% ranges

Baseline: replay's current arm, $7.02 over 900 rows, on-prem share of served rows 0.797.

| scenario | route changed | newly rejected | newly served | Δ cost, $ | on-prem share after |
|---|---|---|---|---|---|
| `all-apps-onprem-only` | 178 [160, 197] | 0 | 0 | +3.74 [3.20, 4.30] | 1.0 |
| `all-apps-no-retention` | 178 [160, 197] | 0 | 0 | +3.74 [3.20, 4.30] | 1.0 |
| `clinic-may-use-cloud` | 63 [52, 75] | 0 | 0 | −0.89 [−1.19, −0.65] | 0.726 |
| `raise-request-budget` | 15 [9, 22] | 0 | 22 [15, 30] | +0.76 [0.46, 1.15] | 0.802 |

- **The replay predicted the cloud share before it was observed.** Run 3's replay, made
  while the provider was down, put 180 [163, 199] requests on the cloud. Run 4, a separate
  draw of the same traffic with the provider up, recorded 178. That is one agreement
  between a prediction and a later measurement, on a different sample of the same
  generator.
- Keeping the two unrestricted applications on-prem costs +$3.74 [3.20, 4.30], 53% of the
  baseline, at stated prices. Run 3's figure for the same proposal was +$3.12 [2.69, 3.54];
  the two ranges overlap. Nothing is rejected in either.
- Shadow prices: relaxing `residency` for `paper-dev` changes 84 [71, 96] routes and saves
  $1.47 [1.20, 1.77]; for `paper-claims`, 76 [64, 88] and $1.26 [0.99, 1.51]. Newly
  rejected is 0 on every relaxation (E6 not exercised, a third time).

### 26.6 Capacity (E10a, E10b)

Stated limits as in §25.8. 60 s buckets, 31 buckets.

| arm | busiest stated limit | peak | time-average | buckets over |
|---|---|---|---|---|
| no change | `glm-5.3-flash`, requests a minute | 50.0% | 30.3% | 0 |
| `all-apps-onprem-only` | `glm-5.3-flash`, tokens a minute | 90.9% | 41.1% | 0 |
| `clinic-may-use-cloud` | `openrouter-frontier`, requests a minute | 55.0% | 21.9% | 0 |
| `raise-request-budget` | `glm-5.3-flash`, tokens a minute | 77.4% | 36.7% | 0 |

Moving everything on-prem takes the on-prem mid endpoint from 50% to 91% of its stated
limit at peak. That is the figure the cost table does not show. Against observed
behaviour: neither cloud endpoint went above 45% of its stated 20 requests a minute in
replay, and the frontier one refused 35 of 94 calls (37%) as overloaded. A stated limit
was again not what bound.

### 26.7 The benchmark and the version check with all four endpoints (E11d)

22-case starter suite, one call per case and eligible endpoint, seed 0.

| endpoint | tier | cases | passed |
|---|---|---|---|
| `qwen3.6-35b` | cheap | 22 | 19 |
| `glm-5.3-flash` | mid | 22 | 16 |
| cloud mid | mid | 8 | 8 |
| cloud frontier | frontier | 8 | 3 (5 calls refused as overloaded) |

| strategy | pass rate | quality regret | mean chosen cost, $ |
|---|---|---|---|
| `cheapest` | 0.864 | 0.000 | 0.0005 |
| `best_published_quality` | 0.727 | 0.136 | 0.0059 |
| `router_online` | 0.727 | 0.136 | 0.0053 |

Violations: 0 of 22 for every strategy. The cheap on-prem model again passed more cases
than the mid one (19 against 16; 18 against 16 in §25.9), and `cheapest` again beat the
router. Two runs of 22 cases agree in direction; it is still a small suite with one
sample per case.

**Version check with a candidate that changes routes.** Candidate
`complexity_thresholds: {cheap_max: 0.0}` removes the cheap tier, so every case targets
mid or above. It changed the attempt chain on **12 of 22 cases**.

| metric | baseline | candidate | worsening | 90% range | tolerance | state |
|---|---|---|---|---|---|---|
| `pass_rate` | 0.727 | 0.727 | 0 | [0, 0] | 0.05 | within tolerance |
| `mean_chosen_cost` | 0.00526 | 0.00589 | 0.00062 | [0.00003, 0.00154] | 0.0005 | **inconclusive** (point beyond tolerance) |
| `latency_regret`, ms (shown, not judged) | 1,276 | 2,165 | 889 | [204, 1,732] | — | worse |

**Verdict: INCONCLUSIVE, exit 4.** The candidate costs more on these cases and the range
of the difference excludes zero, but the range also reaches below the tolerance, so the
check will not call it a regression on 22 cases. It did not pass it either. This is the
whole-range rule of §22.8 doing what it was changed to do: on a suite this small it
refuses to turn a point estimate into a verdict. It is the first live run in which the
check had a real difference in front of it. A verdict of `regressed` on live endpoints
has still not been observed and needs a larger suite.

### 26.8 What runs 3 and 4 establish together

| | run 3 (provider down 24 of 30 min) | run 4 (provider up) |
|---|---|---|
| requests failed | 0 of 899 | 0 of 900 |
| cloud share of served | 3.5% | 20.3% |
| wasted attempts | 102 | 35 |
| model agreement in replay | 0.919 | 1.0 |
| recorded cost against replayed baseline | $9.76 / $7.26 | $6.93 / $7.02 |
| requests replay puts on the cloud | 180 [163, 199] | 178 [160, 197] (recorded: 178) |
| cost of on-prem only for two apps | +$3.12 [2.69, 3.54] | +$3.74 [3.20, 4.30] |
| on-prem-only or detection-raised rows attempted on cloud | 0 of 498 | 0 of 489 |

Still to measure: a `regressed` verdict on live endpoints (larger suite); quality per
endpoint beyond a tier default (larger graded suite, several samples); a saving against a
frontier baseline (paid prices); the shadow-price counterexample on live traffic (a built
fixture); coverage under dependence and width at 5,000 and 50,000 rows (a long agent-loop
export); real text.
