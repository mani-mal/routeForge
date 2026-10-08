# P2 Evaluation Metrics (1 of 2): Group Report and Offline Router-Version Check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give FeasibleRouteBench two opt-in read-outs it lacks: every metric broken down by placement, protection, application and workload with honest denominators and a minimum group size; and an offline check that scores a candidate routing config beside the current one and exits non-zero when the candidate makes any ineligible choice or worsens a named metric beyond an operator-set tolerance.

**Architecture:** `strategy_metrics`' loop becomes `oracle.case_scores` (one `CaseScore` per case), so a group or a resample is an aggregate over a subset. Three new pure modules sit on that: `app/eval/groups.py` (the group report), `app/eval/candidate.py` (a routing overlay, applied in memory) and `app/eval/version_check.py` (paired comparison, decision rule, verdict). The runner scores a candidate exactly as it already scores a weight variant — extra router arms over the same sweep matrix — and writes `groups.json` / `groups.md` and `check.json` / `check.md` only when asked. A golden test, written before any product code, pins the default output.

**Tech Stack:** Python 3.12 standard library plus PyYAML (already a dependency), pytest, `uv`. No new dependency. No serving change, no database change, nothing to migrate.

**Spec:** docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md (§3–§5, §7, §8, §10–§13 are this plan; §6 and §9 are the second plan).

## Deviations from spec

The spec and this plan were written together and agree: there is no plan-versus-spec deviation. This plan covers the **first half** of the spec. Churn robustness (spec §6) and confidence calibration (spec §9), with invariants E18–E25, are a second plan that is not written yet; nothing here builds or stubs them.

The places where the design departs from the brief the spec was written from are listed in the spec's §16. The ones this plan builds, each following the code as it is and pinned by a test:

1. **"Tenant" is `application`.** The bench runs every case under one tenant id (`runner.TENANT_ID`), and a case carries a policy, not an identity. An application is `tags.app`, else a fingerprint of the case's `app_policy`. Task 3: `test_application_is_the_app_tag_or_the_policy_fingerprint`. Task 4: `test_e3_the_run_partitions_its_cases`, `test_the_app_tag_names_the_application`.
2. **The group report is behind `--group-report`.** "Standing output" and "default output unchanged" cannot both hold for one file. `metrics.json`'s existing `groups` / `worst_groups` blocks are untouched, and the report must agree with them. Task 4: `test_e1_the_flag_adds_two_files_and_changes_no_other`, `test_tag_groups_agree_with_the_existing_groups_block`.
3. **The minimum applies to each figure's denominator, not to the group's case count.** Cost regret is over the cases whose served answer passed; a ten-case group can have a cost regret over one. Task 3: `test_e5_the_rule_is_on_the_figures_own_denominator`.
4. **Counts are printed for a group below the minimum.** A violation must not disappear into "insufficient cases". Task 3: `test_e5_a_violation_in_a_one_case_group_is_still_counted`. Task 4: `test_e5_an_injected_violation_shows_in_its_group_however_small`.
5. **The candidate is an overlay on the loaded config, not a second config directory.** The bench loads one config (`load_settings()`), already scores weight variants as extra arms over one sweep, and `feasible_set` reads no routing weight or threshold — so two arms of one run share the cases, the eligible sets, the seed and every endpoint outcome. Task 7: `test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm`.
6. **Some routing keys are refused**, the escalation threshold among them: the sweep runs with escalation off, so accepting them would produce a false "no regression". Task 5: `test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason`, `test_the_allowed_and_refused_keys_cover_routing_yaml_exactly`.
7. **Four outcomes with their own exit codes**, not one "non-zero": `2` hard fail, `3` regressed, `4` inconclusive, `0` pass. Task 6: `test_the_exit_codes`, `test_the_verdict_is_the_worst_state`. Task 7: `test_a_candidate_that_costs_more_than_the_tolerance_exits_3`, `test_a_named_metric_with_too_few_cases_is_inconclusive_and_exits_4`.
8. **A regression is decided by the point estimate; the range can only withhold a pass.** Task 6: `test_e10_the_decision_rule_branch_by_branch`, `test_e17_a_regression_whose_range_includes_zero_is_both_things`.
9. **With no tolerance named the verdict is `no_hard_failure`, not `pass`.** Task 6: `test_no_tolerance_named_judges_nothing_and_says_so`.
10. **The check fails on the run's own integrity code, not only on the candidate's violations.** Task 6: `test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate`.
11. **The golden pin has four cases, not three.** The fourth (the starter suite at seed 1) exists because a mutation of the refactored loop survived the first three: at seed 0 no starter case is infeasible at floor. Task 1; Task 9 Step 3 (`M15`).

## Global Constraints

- **Offline only.** Touch `app/eval/`, `scripts/feasible_bench.py`, `tests/eval/` and `docs/`. No file under `app/` outside `app/eval/` changes. No serving module may import `app.eval` (`tests/eval/test_isolation.py` must keep passing); `app/eval` may import serving modules, never the reverse.
- **No promotion path.** RouteForge has no in-process promotion path (CLAUDE.md rule 4) and this plan does not add one. Nothing here writes a config file, calls an admin route, or is read by serving code. The check writes `check.json` and `check.md` under `--out` and returns an exit code; that is all. If a step seems to need "apply", "activate", a config write or an HTTP call, stop: it is out of scope.
- **Opt-in and additive.** With neither `--group-report` nor `--candidate`, `metrics.json`, `matrix.jsonl`, `choices.jsonl` and the body of `report.md` are byte-identical to what the tree produced before this plan. `tests/eval/test_bench_golden.py` is that pin from Task 1 on. **The golden fixtures under `tests/eval/fixtures/` are written once, in Task 1, and never regenerated at head.** If the golden test fails in any later task, the change that broke it is wrong.
- **RouteForge never ranks or recommends.** No output key or fixed sentence of this plan contains `rank`, `recommend`, `best`, `winner`, `prefer`, `should`, `relax` or `vendor`; no key contains `promot`, `apply` or `applied`. Nothing orders models, endpoints or vendors. Strategy names are data and exempt (`best_published_quality` predates this plan). No output suggests loosening a security requirement.
- **Honesty about the figures.** The fixed text of the spec's §8 opens every new report and is pinned word for word; do not paraphrase it. On the simulated backend quality is an input: no doc sentence may read a pass rate or a quality regret as a property of a model.
- **Exact values from the spec.** Dimensions, in this order: `placement`, `protection`, `application`, `workload`. `DEFAULT_MIN_GROUP_SIZE = 5`, inclusive. Group metrics: `violation_rate`, `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`, `floor_miss_rate`, `empty_set_mismatch_rate`. Check metrics: `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`, `floor_miss_rate`, `mean_chosen_cost`, `wasted_attempts`, `exhausted_rate`. Check defaults: 1000 replicates, level 0.90, seed 0. Exit codes: `0` pass or no hard failure, `1` load/usage, `2` hard fail, `3` regressed, `4` inconclusive. Schemas: `routeforge.bench-group-report/v1`, `routeforge.router-candidate/v1`, `routeforge.router-version-check/v1`.
- **Deterministic.** No clock, no global random state, no dict-order dependence in output. JSON is written as the runner writes it (`runner._dump`: sorted keys, indent 2), floats through `oracle._round_value` (12 decimals). Renderers iterate the `DIMENSIONS` and metric constants, never a dict's order.
- **Tests.** Always run pytest as `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest ...`; without the variable pytest hangs on import. There is no pytest-asyncio: use `asyncio.run`. The tiny simulated suite and its helpers are in `tests/eval/test_runner.py` (`_write_inputs`, `_run`, `_cli`, `_case`, `_jsonl`, `DEFAULT_CASES`, `REPO_ROOT`); import them, do not copy them. The commands below do not pass `-q`: `pyproject.toml` already sets it, and a second `-q` hides the summary line.
- **What each task runs.** The focused tests named in its steps, then `tests/eval` (about nine minutes; it prints `N passed`). The whole suite runs once, in Task 9, with `-rf`: it takes more than ten minutes and prints no summary line under `-q`, so `-rf` is what lists a failure. One failure is pre-existing and not caused by this branch: `tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app`.
- **Do not bend a test to the code.** Never edit a test this plan prescribes to make code pass, and never add behaviour this plan does not specify. If a prescribed test fails after the prescribed code is in place, or an expected output differs, stop and report it.
- **Code style.** Type-hint everything. Match the surrounding comment density and idiom. `app/eval` modules import each other's underscore-private helpers (as `scenarios.py` and `shadow.py` already do); keep that idiom. No application name in a symbol, test id, fixture or comment (`.claude/skills/consumer-contract-tests`).
- **How to read the code steps.** A step that says "Create" shows the whole new file. A step that says "Patch" shows a real patch against the tree as the previous task left it. Save the block to a file and run `git apply <file>`, or make the same edits by hand. A blank context line in a patch is a line holding one space; `git apply` also accepts it empty. Every block in this plan was applied in order to a scratch clone of the branch and every listed command was run against it, with one exception: the nine-minute `tests/eval` step was run after Tasks 2 and 7 only (1332 and 1538 passed). The counts given for Tasks 1 and 3–6 are those measurements plus the number of tests each task adds (6, 10, 54, 18, 35, 61, 38 over a base of 1316).
- **Git.** Stage files by name, never `git add -A`. Never `git stash`. Every commit message ends with exactly this line; do not substitute your own model name:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- **Do not push.** There is nothing to deploy: this is operator-run tooling, and it adds no column, route or setting.

## Review Focus

Seven input classes the spec implies, that are likely to bite a user, and that an obvious task test would miss. Each has a named test in the owning task.

1. **A denominator that is not the case count.** Cost regret over one passing case inside a large group; an empty case inside a group; a case no endpoint passes (it has a quality regret and no floor-miss entry); combining groups by case count instead of by each figure's `n`. Task 2: `test_each_case_matches_the_hand_computed_matrix`, `test_each_metric_reports_its_own_denominator`. Task 3: `test_e4_overall_is_the_denominator_weighted_combination`, `test_e4_weighting_by_case_count_is_wrong_for_a_conditional_metric`, `test_e5_the_rule_is_on_the_figures_own_denominator`, `test_e5_an_empty_case_counts_in_the_group_and_in_no_quality_figure`.
2. **Groups at the edges.** A case with no tag (it must be counted, not dropped); a one-case group that holds the only violation; every group equal (no group may be named "worst" for zero violations); a dimension with a single group; `tags.app` empty or not a string; a suite smaller than the minimum. Task 3: `test_e3_an_untagged_case_is_counted_not_dropped`, `test_e5_a_violation_in_a_one_case_group_is_still_counted`, `test_e6_no_group_is_named_when_every_printed_group_is_equal`, `test_e6_a_metric_no_group_prints_has_no_worst_group`, `test_application_is_the_app_tag_or_the_policy_fingerprint`. Task 7: `test_a_named_metric_with_too_few_cases_is_inconclusive_and_exits_4`.
3. **A candidate that silently does nothing.** A key the bench cannot evaluate (the escalation threshold); a misspelt key; a scalar where a mapping is expected; an unknown strategy name; a tier that does not exist; a variant already named `candidate`; a check flag given without `--candidate`; `--min-group-size` given alone. Task 5: `test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason`, `test_e15_an_unknown_routing_key_is_refused`, `test_e15_a_candidate_the_deployment_could_not_boot_with_is_refused`. Task 7: `test_e15_a_variant_may_not_take_the_candidates_name`, `test_cli_a_check_flag_without_a_candidate_is_refused`. Task 4: `test_cli_a_minimum_without_the_report_is_refused`.
4. **The tolerance boundary.** Worsening exactly at the tolerance (not a regression); `0.92 − 0.90` in floating point against 0.02; a tolerance of 0 with identical arms; a negative, NaN, infinite or boolean tolerance; a metric named twice. Task 6: `test_e10_the_decision_rule_branch_by_branch`, `test_e10_the_tolerance_boundary_is_not_decided_by_float_noise`, `test_e8_identical_arms_are_no_change_everywhere`, `test_a_bad_plan_is_refused`, `test_a_malformed_tolerance_is_refused`.
5. **Ranges that mislead.** A regression whose range includes 0 (still a regression); a point estimate inside the tolerance with a range outside it (not a pass); a figure undefined in some resample (no range, so not a pass); two arms resampled separately instead of together; the result changing with the order cases were scored in. Task 6: `test_e17_a_regression_whose_range_includes_zero_is_both_things`, `test_an_inconclusive_figure_is_not_a_pass`, `test_a_figure_undefined_in_some_resample_has_no_range_and_cannot_pass`, `test_the_draw_is_paired_and_seeded`, `test_e9_the_document_does_not_depend_on_case_order`.
6. **A violation a tolerance could hide.** An ineligible choice with every tolerance at a billion, and with none; an attempt on a case whose feasible set is empty; a run that failed its own integrity check with a clean candidate; an unchanged overall figure hiding a regression in one group. Task 6: `test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances`, `test_e7_an_attempt_on_an_empty_feasible_set_is_counted_as_such`, `test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate`, `test_a_regression_in_one_group_fails_when_overall_is_unchanged`. Task 7: `test_e7_an_injected_ineligible_candidate_exits_2_whatever_the_tolerances`, `test_e7_a_candidate_that_serves_an_empty_feasible_set_is_a_hard_fail`.
7. **The default output, and anything that looks like applying.** Any byte of `metrics.json`, `matrix.jsonl`, `choices.jsonl` or the report body moving; a new key in an unflagged `run.json`; a stale `check.json` left beside a later run; the candidate changing another arm; a write outside `--out`. Task 1: `test_e1_default_output_is_byte_identical`. Task 4: `test_e1_without_the_flag_nothing_new_is_written`, `test_a_stale_group_report_is_removed_by_an_unflagged_run`. Task 7: `test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm`, `test_e11_the_check_writes_only_under_out_and_touches_no_config`, `test_e11_the_new_modules_have_no_way_to_apply_anything`, `test_a_stale_check_is_removed_by_a_run_without_a_candidate`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `tests/eval/test_bench_golden.py` | create | E1: the default output, pinned against stored copies |
| `tests/eval/fixtures/bench_golden/` | create (generated in Task 1, never again) | a copy of every input and of the four config YAMLs; the expected outputs of four cases |
| `app/eval/oracle.py` | modify | `CaseScore`, `case_scores`, `aggregate_scores`, `metric_support`; `strategy_metrics` built on them |
| `app/eval/groups.py` | create | dimensions and group keys, figures with denominators, the minimum, the worst group, the fixed caveat text, `groups.md` |
| `app/eval/candidate.py` | create | the candidate file: loading, the allowed and refused keys, the in-memory overlay |
| `app/eval/version_check.py` | create | worsening, the paired case bootstrap, the decision rule, the verdict, `check.md` |
| `app/eval/runner.py` | modify | `group_report=`, `min_group_size=`, `candidate_path=`, `check_plan=`; `RunResult.groups` / `.check`; the four new files |
| `scripts/feasible_bench.py` | modify | `--group-report`, `--min-group-size`, `--candidate`, `--tolerance`, `--check-replicates`, `--check-interval`, `--check-seed`; the usage errors |
| `tests/eval/test_oracle_scores.py` | create | the per-case scores against a hand-computed matrix |
| `tests/eval/test_groups.py` | create | the group report, pure: E3–E6, E13, E14 |
| `tests/eval/test_group_report_run.py` | create | `--group-report` end to end: E1–E3, E5 |
| `tests/eval/test_candidate.py` | create | the candidate file: E15 |
| `tests/eval/test_version_check.py` | create | the check, pure: E7–E10, E13, E14, E17 |
| `tests/eval/test_version_check_run.py` | create | `--candidate` end to end: E1, E2, E7, E8, E11, E15, E16 |
| `docs/guides/feasible-route-bench.md` | modify | the flags in §5; new §6.2 and §6.3; exit codes 3 and 4 in §7 |
| `docs/research/feasible-route-bench.md` | modify | an update note; §6.5's status; new §22 |
| `docs/research/arxiv.md` | modify | §3.3; §4.6; experiments E11a–E11f; a threat to validity |
| `docs/roadmap/priorities-1-3-status.md` | modify | the slice in Built; §2.2; §3 |
| `docs/backlog/bugs.md` | modify | B67, the follow-ups |

`tests/eval/test_isolation.py`, `tests/eval/test_oracle.py`, `tests/eval/test_runner.py` and every existing fixture are **not** modified.

## Spec invariants → tests

| # | Invariant | Test | Task |
|---|---|---|---|
| E1 | with no new flag the deterministic output is byte-identical to the pre-plan tree; a default run writes exactly five files | `tests/eval/test_bench_golden.py::test_e1_default_output_is_byte_identical` (four cases; run in every task), `::test_e1_the_default_run_writes_exactly_the_five_files`; `tests/eval/test_group_report_run.py::test_e1_without_the_flag_nothing_new_is_written`, `::test_e1_the_flag_adds_two_files_and_changes_no_other`; `tests/eval/test_version_check_run.py::test_e1_without_a_candidate_no_check_is_written` | 1–7 |
| E2 | same inputs and flags give byte-identical new files | `tests/eval/test_group_report_run.py::test_e2_the_group_report_is_byte_identical_twice`; `tests/eval/test_version_check_run.py::test_e2_the_check_is_byte_identical_twice`; `tests/eval/test_groups.py::test_the_document_is_deterministic_and_json_clean` | 3, 4, 7 |
| E3 | every case is in exactly one group per dimension; group sizes sum to the suite | `tests/eval/test_groups.py::test_e3_every_case_is_in_exactly_one_group_per_dimension`, `::test_e3_group_sizes_sum_to_the_total`, `::test_e3_an_untagged_case_is_counted_not_dropped`; `tests/eval/test_group_report_run.py::test_e3_the_run_partitions_its_cases` | 3, 4 |
| E4 | overall = the groups combined by each figure's own denominator; by case count is wrong for a conditional metric | `tests/eval/test_groups.py::test_e4_overall_is_the_denominator_weighted_combination`, `::test_e4_weighting_by_case_count_is_wrong_for_a_conditional_metric` | 3 |
| E5 | below the minimum: a count and no rate; on the figure's denominator; counts never suppressed; inclusive | `tests/eval/test_groups.py::test_e5_a_figure_below_the_minimum_prints_a_count_and_no_rate`, `::test_e5_the_rule_is_on_the_figures_own_denominator`, `::test_e5_the_minimum_is_inclusive`, `::test_e5_a_violation_in_a_one_case_group_is_still_counted`, `::test_e5_an_empty_case_counts_in_the_group_and_in_no_quality_figure`; `tests/eval/test_group_report_run.py::test_e5_small_groups_print_no_rate_at_the_default_minimum`, `::test_e5_an_injected_violation_shows_in_its_group_however_small`; `tests/eval/test_version_check.py::test_e5_a_group_below_the_minimum_is_listed_and_not_judged`, `::test_e5_a_thin_figure_is_skipped_in_a_group_and_inconclusive_overall` | 3, 4, 6 |
| E6 | group figures and the worst group match the hand-computed example | `tests/eval/test_groups.py::test_e6_group_figures_match_the_hand_computed_example`, `::test_e6_the_worst_group_is_named_with_its_size`, `::test_e6_no_group_is_named_when_every_printed_group_is_equal`, `::test_e6_a_metric_no_group_prints_has_no_worst_group`, `::test_e6_a_tie_keeps_the_first_dimension_then_label`; Task 9 Step 4 | 3, 9 |
| E7 | any ineligible choice by the candidate is `hard_fail`, exit 2, whatever the tolerances | `tests/eval/test_version_check.py::test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances`, `::test_e7_an_attempt_on_an_empty_feasible_set_is_counted_as_such`, `::test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate`; `tests/eval/test_version_check_run.py::test_e7_an_injected_ineligible_candidate_exits_2_whatever_the_tolerances`, `::test_e7_a_candidate_that_serves_an_empty_feasible_set_is_a_hard_fail` | 6, 7 |
| E8 | candidate = baseline: worsening 0, range `[0, 0]`, exit 0 at zero tolerance | `tests/eval/test_version_check.py::test_e8_identical_arms_are_no_change_everywhere`; `tests/eval/test_version_check_run.py::test_e8_a_candidate_that_restates_the_config_passes_with_zero_tolerance`, `::test_starter_restating_the_config_passes` | 6, 7 |
| E9 | the check does not depend on case order | `tests/eval/test_version_check.py::test_e9_the_document_does_not_depend_on_case_order` | 6 |
| E10 | worsening, the decision rule and the boundary match hand-computed examples | `tests/eval/test_version_check.py::test_e10_worsening_is_signed_so_that_positive_is_worse`, `::test_e10_the_decision_rule_branch_by_branch`, `::test_e10_the_tolerance_boundary_is_not_decided_by_float_noise`, `::test_e10_a_constant_shift_has_a_degenerate_range_by_hand`; Task 9 Step 4 | 6, 9 |
| E11 | nothing is applied | `tests/eval/test_version_check_run.py::test_e11_the_check_writes_only_under_out_and_touches_no_config`, `::test_e11_the_new_modules_have_no_way_to_apply_anything`, `::test_e11_no_admin_route_or_setting_was_added`; `tests/eval/test_candidate.py::test_nothing_is_written_next_to_the_candidate_file` | 5, 7 |
| E12 | serving isolation | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` (unchanged; run in every task with `tests/eval`) | 1–7, 9 |
| E13 | no ranking, recommendation or promotion vocabulary | `tests/eval/test_groups.py::test_e13_no_key_or_sentence_ranks_or_recommends`; `tests/eval/test_version_check.py::test_e13_no_key_or_sentence_ranks_recommends_or_promotes` | 3, 6 |
| E14 | the fixed text is the spec's and opens every new report | `tests/eval/test_groups.py::test_e14_the_caveats_are_the_spec_text`, `::test_e14_a_simulated_report_opens_with_the_caveats_in_order`, `::test_e14_a_live_run_does_not_claim_quality_is_an_input`; `tests/eval/test_version_check.py::test_e14_the_check_text_is_the_spec_text`, `::test_e14_every_report_opens_with_the_caveats_in_order` | 3, 6 |
| E15 | nothing stated is silently ignored | `tests/eval/test_candidate.py::test_e15_a_malformed_file_is_refused_naming_the_location`, `::test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason`, `::test_e15_an_unknown_routing_key_is_refused`, `::test_e15_a_candidate_the_deployment_could_not_boot_with_is_refused`; `tests/eval/test_version_check_run.py::test_e15_a_bad_candidate_is_a_load_error_before_anything_is_written`, `::test_e15_a_missing_candidate_file_is_a_load_error`, `::test_e15_a_variant_may_not_take_the_candidates_name`, `::test_e15_a_plan_without_a_candidate_is_a_load_error`, `::test_cli_a_check_flag_without_a_candidate_is_refused`, `::test_cli_a_bad_check_value_is_refused`; `tests/eval/test_group_report_run.py::test_a_bad_minimum_is_a_load_error`, `::test_cli_a_minimum_without_the_report_is_refused` | 4, 5, 7 |
| E16 | the candidate adds arms and changes nothing else | `tests/eval/test_version_check_run.py::test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm`; `tests/eval/test_candidate.py::test_the_harness_settings_are_never_mutated` | 5, 7 |
| E17 | a range that includes 0 decides nothing by itself | `tests/eval/test_version_check.py::test_e17_the_lower_end_of_a_range_never_enters_the_decision`, `::test_e17_a_regression_whose_range_includes_zero_is_both_things` | 6 |

E18–E25 (churn, calibration) belong to the second plan.

---

## Tasks

### Task 1: Pin the bench's default output (spec §10; E1)

**Files:**
- Create: `tests/eval/test_bench_golden.py`
- Create (generated by the command in Step 3, not written by hand): `tests/eval/fixtures/bench_golden/` — `GENERATED_FROM`, `inputs/` (seven suite and endpoint files, `inputs/config/` with the four YAMLs), `expected/<case>/` × 4 (`metrics.json`, `matrix.jsonl`, `choices.jsonl`, `report_body.md`)

**Interfaces:**
- Consumes: `app/eval/runner.py::run_bench(suite_path, endpoints_path, *, out_dir, seed, variants_path, ...) -> RunResult` and its module-level name `runner.load_settings` (called with no argument inside `run_bench`); `app/config/loader.py::load_settings(config_dir: str = "config") -> Settings`.
- Produces: `tests/eval/test_bench_golden.py::run_case(name: str, out: Path) -> dict[str, bytes]`, `CASES: dict[str, tuple[str, str, str | None, int]]`, `PINNED`, and the fixture directory. Nothing else in the plan imports them; every later task **runs** this file.

**Why first, and why now.** The pin must be written from the tree **before any product code of this plan exists**: that is what makes it mean "unchanged from before". Task 2 refactors the loop behind every figure in `metrics.json`; this is the test that says the refactor moved nothing. The generator refuses to run once `app/eval/groups.py` exists (Task 3), so it cannot be re-run later by accident.

**Why four cases.** `starter` (with the weight variants), `outage` (error rows, failover chains, the breaker arm) and `empty` (every case rejected) cover the three shapes of run. `starter_seed1` is there because at seed 0 no starter case is *infeasible at floor* (no eligible endpoint passes), and a mutation of that branch survived the first three cases; at seed 1 some cases are, and the pin catches it (Task 9, mutant M15).

**Why the config is copied.** `run_bench` reads `config/`. Pinning against the live `config/` would make the fixture move whenever a weight or a sensitivity rule is edited. The fixture holds its own copy of the four YAMLs and the test points `runner.load_settings` at it, so the pin is of the harness code.

- [ ] **Step 1: Write the test and the generator**

Create `tests/eval/test_bench_golden.py`:

````python
"""P2 evaluation metrics, invariant E1: with none of the new flags
(`--group-report`, `--candidate`), every deterministic output of
`scripts/feasible_bench.py` is byte-identical to what it was before this
slice.

Four cases, all simulated. Each pins `metrics.json`, `matrix.jsonl`,
`choices.jsonl` and the body of `report.md` (everything from "## How to read
this" on: the "## Run" block above it holds timestamps and the git sha, and
`run.json` holds the same, so neither is pinned):

    starter        starter suite x healthcare-sim x the b45 weight variants,
                   seed 0
    starter_seed1  the same suite and endpoints at seed 1, no variants: at
                   this seed some cases have no passing endpoint
                   (infeasible at floor), a path seed 0 never takes
    outage         outage suite x outage-sim, seed 0 (error rows, failover
                   chains, the breaker arm and its control)
    empty          empty-set suite x cloud-only-sim, seed 0 (every case
                   rejected)

`fixtures/bench_golden/inputs/` holds a COPY of every input, the four config
YAMLs included, so the expected files do not move when `eval/` or `config/`
is edited: this pins the harness code, not today's configuration.

**The expected files were written from the tree as it was BEFORE any product
code of this slice existed** (`GENERATED_FROM` names the commit). That is what
makes the pin mean "unchanged from before": a file regenerated from the
current tree would only say the current tree agrees with itself. So never
regenerate a case to make a test pass; a failure is a real change to what an
operator who passes no new flag sees.

After a DELIBERATE change to the bench's default output, regenerate and
review every changed byte:

    LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_bench_golden --deliberate-change

Without `--deliberate-change` the command refuses to run in a tree that has
`app/eval/groups.py`.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "bench_golden"
INPUTS = FIXTURE / "inputs"
EXPECTED = FIXTURE / "expected"
RUN_TIMEOUT_S = 180
REPORT_BODY_START = "## How to read this"
PINNED = ("metrics.json", "matrix.jsonl", "choices.jsonl", "report_body.md")

#: case -> (suite, endpoints, variants or None, seed); file names under `inputs/`.
CASES: dict[str, tuple[str, str, str | None, int]] = {
    "starter": ("starter.jsonl", "healthcare-sim.yaml", "b45.yaml", 0),
    "starter_seed1": ("starter.jsonl", "healthcare-sim.yaml", None, 1),
    "outage": ("outage.jsonl", "outage-sim.yaml", None, 0),
    "empty": ("empty-set.jsonl", "cloud-only-sim.yaml", None, 0),
}

#: where each input is copied FROM when the fixture is (re)generated.
_SOURCES = {
    "starter.jsonl": "eval/suites/starter.jsonl",
    "outage.jsonl": "eval/suites/outage.jsonl",
    "empty-set.jsonl": "eval/suites/empty-set.jsonl",
    "healthcare-sim.yaml": "eval/endpoints/healthcare-sim.yaml",
    "outage-sim.yaml": "eval/endpoints/outage-sim.yaml",
    "cloud-only-sim.yaml": "eval/endpoints/cloud-only-sim.yaml",
    "b45.yaml": "eval/variants/b45.yaml",
    "config/serving.yaml": "config/serving.yaml",
    "config/routing.yaml": "config/routing.yaml",
    "config/policy.yaml": "config/policy.yaml",
    "config/pricing.yaml": "config/pricing.yaml",
}


def report_body(report: str) -> str:
    """`report.md` from the first section after "## Run" to the end."""
    return report[report.index(REPORT_BODY_START):]


def run_case(name: str, out: Path) -> dict[str, bytes]:
    """Run one golden case into `out`; returns the four pinned files' bytes.

    `runner.load_settings` is pointed at the fixture's own config copy, and
    `SP_PII_DETECTION` is forced to `regex`, for the duration of the run."""
    from app.config.loader import load_settings
    from app.eval import runner

    suite, endpoints, variants, seed = CASES[name]
    real_load, real_env = runner.load_settings, os.environ.get("SP_PII_DETECTION")
    runner.load_settings = lambda: load_settings(str(INPUTS / "config"))
    os.environ["SP_PII_DETECTION"] = "regex"
    try:
        result = asyncio.run(asyncio.wait_for(runner.run_bench(
            str(INPUTS / suite), str(INPUTS / endpoints), out_dir=str(out), seed=seed,
            variants_path=str(INPUTS / variants) if variants else None),
            timeout=RUN_TIMEOUT_S))
    finally:
        runner.load_settings = real_load
        if real_env is None:
            os.environ.pop("SP_PII_DETECTION", None)
        else:
            os.environ["SP_PII_DETECTION"] = real_env
    assert result.exit_code == 0, f"golden case {name!r} exited {result.exit_code}"
    files = {n: (out / n).read_bytes() for n in PINNED[:3]}
    files["report_body.md"] = report_body(
        (out / "report.md").read_text(encoding="utf-8")).encode("utf-8")
    return files


@pytest.mark.parametrize("name", sorted(CASES))
def test_e1_default_output_is_byte_identical(name, tmp_path):
    got = run_case(name, tmp_path / "out")
    for filename in PINNED:
        expected = (EXPECTED / name / filename).read_bytes()
        assert got[filename] == expected, (
            f"{name}/{filename} changed. The bench's default output is pinned "
            "(invariant E1): find the change that moved it; do not regenerate.")


def test_e1_the_default_run_writes_exactly_the_five_files(tmp_path):
    run_case("empty", tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "choices.jsonl", "matrix.jsonl", "metrics.json", "report.md", "run.json"]


def test_the_fixture_names_the_commit_it_was_generated_from():
    text = (FIXTURE / "GENERATED_FROM").read_text(encoding="utf-8")
    assert "commit: " in text and "unknown" not in text


def _git_sha() -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def _generate(deliberate: bool) -> int:
    if (ROOT / "app" / "eval" / "groups.py").exists() and not deliberate:
        print("refusing: app/eval/groups.py exists, so this tree is no longer the "
              "pre-slice tree. Pass --deliberate-change only after a deliberate "
              "change to the bench's default output.", file=sys.stderr)
        return 2
    for target, source in _SOURCES.items():
        dest = INPUTS / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, dest)
    scratch = FIXTURE / "_scratch"
    for name in sorted(CASES):
        shutil.rmtree(scratch, ignore_errors=True)
        files = run_case(name, scratch)
        (EXPECTED / name).mkdir(parents=True, exist_ok=True)
        for filename, data in files.items():
            (EXPECTED / name / filename).write_bytes(data)
    shutil.rmtree(scratch, ignore_errors=True)
    (FIXTURE / "GENERATED_FROM").write_text(
        f"commit: {_git_sha()}\n"
        "command: LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m "
        "tests.eval.test_bench_golden\n"
        "Never regenerate to make a test pass (see tests/eval/test_bench_golden.py).\n",
        encoding="utf-8")
    print(f"wrote {FIXTURE}")
    return 0


if __name__ == "__main__":
    sys.exit(_generate("--deliberate-change" in sys.argv[1:]))
````

- [ ] **Step 2: Run it and watch it fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_bench_golden.py -p no:warnings -rf
```

Expected: `6 failed`. There is no fixture directory yet: the four parametrised cases and the five-files test fail on `golden case '<name>' exited 1` (the run cannot find its inputs), and the `GENERATED_FROM` test on `FileNotFoundError`.

- [ ] **Step 3: Generate the fixture — once, now**

Confirm the tree holds no product code of this plan, then generate:

```bash
test ! -e app/eval/groups.py && git status --short
LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_bench_golden
```

Expected: `git status --short` lists only `?? tests/eval/test_bench_golden.py`. The generator logs the simulated outage's `backend call failed` lines (they are the outage case working) and ends with `wrote .../tests/eval/fixtures/bench_golden`.

```bash
cat tests/eval/fixtures/bench_golden/GENERATED_FROM
find tests/eval/fixtures/bench_golden -type f | wc -l
```

Expected: `commit:` followed by the sha of this branch's head (the plan commit — a commit with no product code of this plan), and `28` files.

- [ ] **Step 4: Run the test**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_bench_golden.py -p no:warnings -rf
```

Expected: `6 passed`.

Run it once more under another hash seed; the output must not depend on it:

```bash
PYTHONHASHSEED=3 LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_bench_golden.py -p no:warnings -rf
```

Expected: `6 passed`.

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1322 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add tests/eval/test_bench_golden.py tests/eval/fixtures/bench_golden
git commit -m "$(cat <<'EOF'
test(eval): pin FeasibleRouteBench's default output (P2 metrics, E1)

Four simulated cases -- starter with the weight variants, starter at seed 1,
outage, empty-set -- compared byte for byte with stored copies of
metrics.json, matrix.jsonl, choices.jsonl and the report body. The fixture
holds its own copy of the inputs and of config/, and was written from this
tree before any product code of the slice.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Per-case scores in the oracle (spec §5)

**Files:**
- Modify: `app/eval/oracle.py` (a new dataclass before `_round_value`; `strategy_metrics`' body moved into `case_scores`)
- Create: `tests/eval/test_oracle_scores.py`

**Interfaces:**
- Consumes: `app/eval/oracle.py` — `CaseInfo`, `MatrixRow`, `Choice`, `_attempt_chain`, `_oracle_raw`, `_attempt_outcome_raw`, `_mean`, `_round_value`, all unchanged.
- Produces, in `app/eval/oracle.py`:
  - `CaseScore` — frozen dataclass, fields in this order: `case_id: str`, `empty: bool`, `violation: bool`, `empty_mismatch: bool`, `infeasible_at_floor: bool`, `passed: float | None`, `quality_regret: float | None`, `floor_miss: float | None`, `cost_regret: float | None`, `latency_regret: float | None`, `chosen_cost: float | None`, `wasted_attempts: float | None`, `exhausted: float | None`. Unrounded. A `None` means the case is outside that metric's denominator.
  - `case_scores(cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice], strategy: str) -> list[CaseScore]` — one per case, in the order given.
  - `aggregate_scores(scores: list[CaseScore]) -> dict` — `strategy_metrics`' keys, unrounded.
  - `metric_support(scores: list[CaseScore]) -> dict[str, int]` — each rate's or mean's denominator.
  - `_SCORE_FIELD: dict[str, str]` — aggregate key → `CaseScore` field, in this order: `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`, `floor_miss_rate`, `mean_chosen_cost`, `wasted_attempts`, `exhausted_rate`.
  - `_values(scores: list[CaseScore], field: str) -> list[float]` — the non-`None` values of one field, in order.
  - `strategy_metrics(...)` keeps its signature and docstring and returns `_round_value(aggregate_scores(case_scores(...)))`.

  Tasks 3, 4, 6 and 7 use all of these.

**Why a refactor and not a second loop.** A group figure and a resample both need what one case contributes. A second copy of the rules in `strategy_metrics` (which chain is a violation, when a floor miss counts, when cost regret is defined) would drift from the first. The loop is moved, not rewritten: every comment and every branch is the one that is there now, and each `append` to an aggregate list becomes a field of the case's score. Sums then run over the same cases in the same order, so the output is identical to the last bit — which Task 1's pin checks.

**Hand-computed matrix** (the test's fixture). Two endpoints, `a` (cost 1, 300 ms) and `b` (cost 3, 100 ms); floor 1.0.

| case | rows | chain | passed | quality regret | floor miss | cost regret | latency regret | chosen cost | wasted |
|---|---|---|---|---|---|---|---|---|---|
| c1 | `a` passes, `b` passes | `(b)` | 1 | 0 | 0 | 3 − 1 = 2 | 100 − 100 = 0 | 3 | 0 |
| c2 | `a` fails the floor, `b` passes | `(a)` | 0 | 1 | 1 | - | - | 1 | 0 |
| c3 | `a` errors (40 ms), `b` passes | `(a, b)` | 1 | 0 | 0 | 3 − 3 = 0 | 40 + 100 − 100 = 40 | 3 | 1 |
| c4 | both fail the floor | `(a)` | 0 | 0 (q\* is 0) | - (P is empty) | - | - | 1 | 0 |
| c5 | empty feasible set, rejected | `()` | - | - | - | - | - | - | - |

Aggregates: `pass_rate` 2/4; `quality_regret` 1/4; `floor_miss_rate` 1/3; `cost_regret` (2 + 0)/2 = 1; `latency_regret` (0 + 40)/2 = 20; `mean_chosen_cost` 8/4 = 2; `wasted_attempts` 1/4. Denominators: 5 (violations), 4, 4, 2, 2, 3, 4, 4, 4.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_oracle_scores.py`:

````python
"""Per-case scores in `app.eval.oracle` (P2 evaluation metrics, Task 2).

`strategy_metrics` is now `aggregate_scores(case_scores(...))`, rounded. The
existing `tests/eval/test_oracle.py` and the golden pin
(`tests/eval/test_bench_golden.py`) show the aggregate did not move; these
tests pin the per-case form that the group report and the router-version
check are built on, against a matrix computed by hand.
"""
from __future__ import annotations

from app.eval.oracle import (
    CaseInfo,
    CaseScore,
    Choice,
    MatrixRow,
    aggregate_scores,
    case_scores,
    metric_support,
    strategy_metrics,
)

S = "s"


def _row(case_id, endpoint, quality, cost, latency, *, error=None) -> MatrixRow:
    return MatrixRow(case_id=case_id, endpoint=endpoint, quality=quality,
                     passed=error is None and quality >= 1.0, cost_usd=cost,
                     latency_ms=latency, error=error,
                     counts_unhealth=True if error else None,
                     failure_latency_ms=40.0 if error else None)


def _case(case_id, eligible, *, expect_empty=False, rejected_ok=False) -> CaseInfo:
    return CaseInfo(case_id=case_id, eligible=tuple(eligible), expect_empty=expect_empty,
                    rejected_ok=rejected_ok, tags={})


# Five cases, two endpoints `a` (cheap, slow) and `b` (dear, fast).
#
#   c1  a passes (cost 1, 300 ms), b passes (cost 3, 100 ms); chosen b
#         -> pass, quality regret 0, cost regret 3-1 = 2, latency regret 0
#   c2  a fails the floor (q 0), b passes (cost 3, 100 ms); chosen a
#         -> floor miss, quality regret 1, no cost or latency regret
#   c3  a errors, b passes (cost 3, 100 ms); chain (a, b)
#         -> pass, cost regret 0, latency 40 + 100 - 100 = 40, one wasted attempt
#   c4  a and b both fail the floor; chosen a
#         -> infeasible at floor: quality regret 0, no floor-miss entry
#   c5  empty feasible set, rejected correctly, no chain
CASES = [
    _case("c1", ["a", "b"]), _case("c2", ["a", "b"]), _case("c3", ["a", "b"]),
    _case("c4", ["a", "b"]), _case("c5", [], expect_empty=True, rejected_ok=True),
]
ROWS = [
    _row("c1", "a", 1.0, 1.0, 300.0), _row("c1", "b", 1.0, 3.0, 100.0),
    _row("c2", "a", 0.0, 1.0, 300.0), _row("c2", "b", 1.0, 3.0, 100.0),
    _row("c3", "a", 0.0, 0.0, None, error="ConnectionError: down"),
    _row("c3", "b", 1.0, 3.0, 100.0),
    _row("c4", "a", 0.0, 1.0, 300.0), _row("c4", "b", 0.0, 3.0, 100.0),
]
CHOICES = [
    Choice("c1", S, "b", ("b",)), Choice("c2", S, "a", ("a",)),
    Choice("c3", S, "b", ("a", "b")), Choice("c4", S, "a", ("a",)),
    Choice("c5", S, None, ()),
]


def _scores() -> dict[str, CaseScore]:
    return {s.case_id: s for s in case_scores(CASES, ROWS, CHOICES, S)}


def test_one_score_per_case_in_the_order_given():
    assert [s.case_id for s in case_scores(CASES, ROWS, CHOICES, S)] == [
        "c1", "c2", "c3", "c4", "c5"]
    assert [s.case_id for s in case_scores(CASES[::-1], ROWS, CHOICES, S)] == [
        "c5", "c4", "c3", "c2", "c1"]


def test_each_case_matches_the_hand_computed_matrix():
    s = _scores()
    assert s["c1"] == CaseScore("c1", False, False, False, False, 1.0, 0.0, 0.0, 2.0, 0.0,
                                3.0, 0.0, 0.0)
    assert s["c2"] == CaseScore("c2", False, False, False, False, 0.0, 1.0, 1.0, None, None,
                                1.0, 0.0, 0.0)
    assert s["c3"] == CaseScore("c3", False, False, False, False, 1.0, 0.0, 0.0, 0.0, 40.0,
                                3.0, 1.0, 0.0)
    # Infeasible at floor: q* is 0, so no quality regret and no floor-miss entry.
    assert s["c4"] == CaseScore("c4", False, False, False, True, 0.0, 0.0, None, None, None,
                                1.0, 0.0, 0.0)
    # An empty case is outside every quality, cost and latency denominator.
    assert s["c5"] == CaseScore("c5", True, False, False, False, None, None, None, None,
                                None, None, None, None)


def test_the_aggregate_is_the_hand_computed_one():
    m = aggregate_scores(case_scores(CASES, ROWS, CHOICES, S))
    assert m["n_cases"] == 5
    assert m["violation_rate"] == 0.0
    assert m["pass_rate"] == 2 / 4                 # c1, c3 of the four non-empty cases
    assert m["quality_regret"] == 1 / 4            # c2 only
    assert m["floor_miss_rate"] == 1 / 3           # c2 of c1, c2, c3 (c4 has P = {})
    assert m["cost_regret"] == (2.0 + 0.0) / 2     # c1, c3: the passing choices only
    assert m["latency_regret"] == (0.0 + 40.0) / 2
    assert m["mean_chosen_cost"] == (3.0 + 1.0 + 3.0 + 1.0) / 4
    assert m["wasted_attempts"] == 1 / 4
    assert m["exhausted_rate"] == 0.0
    assert m["infeasible_at_floor"] == 1
    assert m["empty_set_mismatches"] == 0


def test_strategy_metrics_is_the_rounded_aggregate():
    scores = case_scores(CASES, ROWS, CHOICES, S)
    assert strategy_metrics(CASES, ROWS, CHOICES, S) == {
        k: (round(v, 12) if isinstance(v, float) else v)
        for k, v in aggregate_scores(scores).items()}


def test_each_metric_reports_its_own_denominator():
    assert metric_support(case_scores(CASES, ROWS, CHOICES, S)) == {
        "violation_rate": 5, "pass_rate": 4, "quality_regret": 4, "cost_regret": 2,
        "latency_regret": 2, "floor_miss_rate": 3, "mean_chosen_cost": 4,
        "wasted_attempts": 4, "exhausted_rate": 4}


def test_a_subset_of_scores_aggregates_like_a_subset_of_cases():
    """What the group report relies on: scoring every case once and
    aggregating a subset gives exactly what scoring that subset gives."""
    scores = _scores()
    for subset in (["c1", "c2"], ["c3", "c5"], ["c4"], ["c5"], []):
        cases = [c for c in CASES if c.case_id in subset]
        assert (aggregate_scores([scores[i] for i in subset])
                == aggregate_scores(case_scores(cases, ROWS, CHOICES, S)))


def test_an_empty_list_has_no_rate_and_no_mean():
    m = aggregate_scores([])
    assert m["n_cases"] == 0 and m["violation_rate"] is None and m["pass_rate"] is None
    assert metric_support([])["violation_rate"] == 0


def test_a_violating_chain_scores_like_no_choice_and_is_flagged():
    choices = [Choice("c1", S, "zzz", ("zzz",))]
    (score,) = case_scores(CASES[:1], ROWS, choices, S)
    assert score.violation is True
    assert (score.passed, score.quality_regret, score.chosen_cost) == (0.0, 1.0, 0.0)
    assert score.cost_regret is None and score.latency_regret is None


def test_any_attempt_on_an_empty_case_is_a_violation():
    choices = [Choice("c5", S, "a", ("a",))]
    (score,) = case_scores(CASES[4:], ROWS, choices, S)
    assert score.empty is True and score.violation is True
    assert score.passed is None


def test_an_empty_case_that_was_not_rejected_is_a_mismatch_not_a_violation():
    case = _case("c5", [], expect_empty=True, rejected_ok=False)
    (score,) = case_scores([case], ROWS, [Choice("c5", S, None, ())], S)
    assert score.empty_mismatch is True and score.violation is False
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_oracle_scores.py -p no:warnings -rf
```

Expected: `1 error` during collection — `ImportError: cannot import name 'CaseScore' from 'app.eval.oracle'`.

- [ ] **Step 3: Move the loop**

Patch (save as a file and run `git apply <file>`):

````diff
diff --git a/app/eval/oracle.py b/app/eval/oracle.py
index 494b05f..d695a8e 100644
--- a/app/eval/oracle.py
+++ b/app/eval/oracle.py
@@ -101,6 +101,33 @@ def _attempt_chain(choice: Choice) -> tuple[str, ...]:
         return tuple(choice.attempts)
     return (choice.endpoint,) if choice.endpoint is not None else ()
 
+@dataclass(frozen=True)
+class CaseScore:
+    """One `(case, strategy)` outcome, **unrounded**: what that case adds to
+    each aggregate `strategy_metrics` reports. A field is `None` when the
+    case is outside that metric's denominator -- never 0.0, for the same
+    reason `_mean` returns `None` on an empty list.
+
+    `strategy_metrics` is `aggregate_scores(case_scores(...))`, rounded. The
+    per-case form exists so a caller can aggregate over a SUBSET of cases (a
+    group, `app/eval/groups.py`) or resample cases (the paired comparison in
+    `app/eval/version_check.py`) without re-deriving any rule below."""
+
+    case_id: str
+    empty: bool                      # E(c) = {}
+    violation: bool                  # at most one per case
+    empty_mismatch: bool             # expect_empty disagrees, or not rejected_ok
+    infeasible_at_floor: bool        # non-empty and P(c) = {}
+    passed: float | None             # 1.0 / 0.0; None on an empty case
+    quality_regret: float | None     # q* - served quality; None on an empty case
+    floor_miss: float | None         # 1.0 / 0.0; None unless P(c) != {}
+    cost_regret: float | None        # only when the served outcome passed
+    latency_regret: float | None     # likewise, and both latencies known
+    chosen_cost: float | None        # the chain's summed cost; None on an empty case
+    wasted_attempts: float | None    # failed attempts; None on an empty case
+    exhausted: float | None          # 1.0 / 0.0; None on an empty case
+
+
 def _round_value(value):
     """Round every float leaf in `value` to 12 dp; recurse through dict,
     list, and tuple containers; pass everything else through unchanged.
@@ -257,55 +284,12 @@ def case_oracle(rows: list[MatrixRow]) -> dict:
     """
     return _round_value(_oracle_raw(rows))
 
-def strategy_metrics(
+def case_scores(
     cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice], strategy: str,
-) -> dict:
-    """Aggregate metrics 1-5 (spec §6) for one `strategy`, over `cases`.
-
-    Returns:
-    - `n_cases`: `len(cases)`.
-    - `violation_rate`: share of `cases` whose attempt chain names **any**
-      endpoint not in that case's `eligible` set (including any attempt on
-      an empty case), plus a chain naming an eligible endpoint with no
-      matching sweep row, plus a non-empty case with no recorded `Choice`
-      at all for `strategy` -- all "should not happen" (brief ruling /
-      fix-round-1 finding 4), but counted as violations rather than
-      crashing. For an explicit `attempts` chain it is also a violation
-      when the chain repeats an endpoint, records entries after the served
-      attempt, or `Choice.endpoint` is not the chain's served endpoint
-      (`None` when exhausted). At most one violation per case: this is a
-      per-case rate, unlike `violations()`'s per-attempt count.
-    - `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`,
-      `floor_miss_rate`: as spec §6 points 2-4, computed over non-empty
-      cases only (an empty case has no quality/cost/latency notion), from
-      the chain's `attempt_outcome` (B48 spec §5): a case passes iff the
-      *served* row passed; quality is the served row's (0 if exhausted);
-      cost regret uses the chain's summed cost and latency regret its
-      summed latency (failed attempts' latency plus the served call's).
-      An empty chain, an exhausted chain, or a violating chain scores
-      quality 0 and never contributes to cost/latency regret (those are
-      computed only when the served outcome passed).
-    - `mean_chosen_cost`: mean, over non-empty cases, of the chain's
-      failover-aware cost (sum of attempt costs; failed attempts, an
-      empty chain, and a violating chain cost 0.0) -- not itself one of
-      spec §6's five metrics, but needed by `headline`'s saving-percentage
-      calculation, which only receives this function's output.
-    - `wasted_attempts`: mean, over non-empty cases, of the chain's failed
-      attempts (B48 spec §5).
-    - `exhausted_rate`: share of non-empty cases whose chain was exhausted
-      (at least one attempt, none succeeded).
-    - `infeasible_at_floor`: count of non-empty cases where `P(c) = {}`.
-      Strategy-independent (an oracle property of the case), exposed here
-      per-strategy for convenient reporting.
-    - `empty_set_mismatches`: count of cases (strategy-independent) where
-      `expect_empty` disagrees with actual emptiness (`eligible == ()`), or
-      an actually-empty case was not `rejected_ok`. A case tripping both
-      conditions is counted once.
-
-    Any of the five regret-shaped metrics is `None`, not 0.0, when its
-    denominator is empty (e.g. no non-empty cases, or no case where the
-    choice passed) -- an aggregate with no data is undefined, not zero.
-    """
+) -> list[CaseScore]:
+    """One `CaseScore` per case in `cases`, in the order given, for
+    `strategy`. Every rule is `strategy_metrics`' (its docstring is the
+    definition): this is the per-case walk that function aggregates."""
     rows_by_case: dict[str, list[MatrixRow]] = {}
     rows_by_case_endpoint: dict[tuple[str, str], MatrixRow] = {}
     for row in rows:
@@ -313,27 +297,12 @@ def strategy_metrics(
         rows_by_case_endpoint[(row.case_id, row.endpoint)] = row
 
     choice_by_case = {c.case_id: c for c in choices if c.strategy == strategy}
-
-    n_cases = len(cases)
-    violations_count = 0
-    empty_mismatches = 0
-    infeasible_count = 0
-
-    quality_regrets: list[float] = []
-    pass_flags: list[float] = []
-    floor_miss_flags: list[float] = []
-    cost_regrets: list[float] = []
-    latency_regrets: list[float] = []
-    chosen_costs: list[float] = []
-    wasted_counts: list[float] = []
-    exhausted_flags: list[float] = []
+    scores: list[CaseScore] = []
 
     for case in cases:
         actual_empty = len(case.eligible) == 0
         mismatch = (case.expect_empty != actual_empty) or (
             actual_empty and not case.rejected_ok)
-        if mismatch:
-            empty_mismatches += 1
 
         has_choice_record = case.case_id in choice_by_case
         choice = choice_by_case.get(case.case_id)
@@ -344,8 +313,12 @@ def strategy_metrics(
             # an endpoint outside E(c) = {} by construction. A missing
             # Choice record is also correct here (nothing to record) --
             # the harness-bug rule below applies only to non-empty cases.
-            if chain:
-                violations_count += 1
+            scores.append(CaseScore(
+                case_id=case.case_id, empty=True, violation=bool(chain),
+                empty_mismatch=mismatch, infeasible_at_floor=False, passed=None,
+                quality_regret=None, floor_miss=None, cost_regret=None,
+                latency_regret=None, chosen_cost=None, wasted_attempts=None,
+                exhausted=None))
             continue
 
         # Spec §6: q*, P, c*, l* are all defined over E(c). A row for an
@@ -357,8 +330,6 @@ def strategy_metrics(
             if row.endpoint in case.eligible
         ]
         oracle = _oracle_raw(case_rows) if case_rows else None
-        if oracle is not None and oracle["infeasible_at_floor"]:
-            infeasible_count += 1
 
         # Failover-aware outcome (B48 spec §5). A violating chain -- any
         # attempt outside E(c), or an eligible attempt with no sweep row --
@@ -366,13 +337,14 @@ def strategy_metrics(
         # attempts, not exhausted) so one bad case cannot silently distort
         # the aggregate; the violation itself is what fails the run.
         outcome = _attempt_outcome_raw({}, ())
+        violation = False
         if not has_choice_record:
             # A non-empty case with no Choice record at all for this
             # strategy is a harness bug -- the runner is expected to
             # record one Choice per (case, strategy) it ran. Distinct from
             # an explicit Choice(endpoint=None) with no attempts, which is
             # a legitimate "no choice" and stays violation-free (below).
-            violations_count += 1
+            violation = True
         elif chain:
             # Every recorded attempt must be in E(c) and have a sweep row
             # -- including any recorded past the served one (spec §5:
@@ -387,7 +359,7 @@ def strategy_metrics(
                     or len(set(chain)) != len(chain)):
                 # an attempt outside E(c) / with no sweep row, or the same
                 # endpoint attempted twice (the gateway excludes a failed one)
-                violations_count += 1
+                violation = True
             else:
                 candidate = _attempt_outcome_raw(case_rows_by_endpoint, chain)
                 if choice.attempts and (candidate["attempts"] != len(chain)
@@ -395,7 +367,7 @@ def strategy_metrics(
                     # entries recorded after the served attempt, or a
                     # `Choice.endpoint` that is not the chain's served one
                     # (ruling D) -- the record contradicts itself
-                    violations_count += 1
+                    violation = True
                 else:
                     outcome = candidate
 
@@ -404,43 +376,134 @@ def strategy_metrics(
         chosen_passed = served_row.passed if served_row is not None else False
         chosen_cost = outcome["cost_usd"]
         chosen_latency = outcome["latency_ms"]
-
         q_star = oracle["q_star"] if oracle is not None else 0.0
-        quality_regrets.append(q_star - outcome["quality"])
-        pass_flags.append(1.0 if chosen_passed else 0.0)
-        chosen_costs.append(chosen_cost)
-        wasted_counts.append(float(outcome["wasted_attempts"]))
-        exhausted_flags.append(1.0 if outcome["exhausted"] else 0.0)
 
+        floor_miss: float | None = None
+        cost_regret: float | None = None
+        latency_regret: float | None = None
         if oracle is not None and oracle["passing"]:
-            floor_miss_flags.append(0.0 if chosen_passed else 1.0)
+            floor_miss = 0.0 if chosen_passed else 1.0
             if chosen_passed:
                 # Only a served, passing outcome reaches here, so an
                 # exhausted chain's (failure-only) latency never enters
                 # latency regret.
                 c_star = oracle["c_star"]
                 if c_star is not None:
-                    cost_regrets.append(chosen_cost - c_star)
+                    cost_regret = chosen_cost - c_star
                 l_star = oracle["l_star"]
                 if chosen_latency is not None and l_star is not None:
-                    latency_regrets.append(chosen_latency - l_star)
+                    latency_regret = chosen_latency - l_star
+
+        scores.append(CaseScore(
+            case_id=case.case_id, empty=False, violation=violation,
+            empty_mismatch=mismatch,
+            infeasible_at_floor=oracle is not None and oracle["infeasible_at_floor"],
+            passed=1.0 if chosen_passed else 0.0,
+            quality_regret=q_star - outcome["quality"],
+            floor_miss=floor_miss, cost_regret=cost_regret,
+            latency_regret=latency_regret, chosen_cost=chosen_cost,
+            wasted_attempts=float(outcome["wasted_attempts"]),
+            exhausted=1.0 if outcome["exhausted"] else 0.0))
+    return scores
+
+
+#: `aggregate_scores` key -> the `CaseScore` field it is the mean of. A case
+#: whose field is `None` is outside that metric's denominator.
+_SCORE_FIELD = {
+    "pass_rate": "passed",
+    "quality_regret": "quality_regret",
+    "cost_regret": "cost_regret",
+    "latency_regret": "latency_regret",
+    "floor_miss_rate": "floor_miss",
+    "mean_chosen_cost": "chosen_cost",
+    "wasted_attempts": "wasted_attempts",
+    "exhausted_rate": "exhausted",
+}
+
+
+def _values(scores: list[CaseScore], field: str) -> list[float]:
+    return [v for v in (getattr(s, field) for s in scores) if v is not None]
+
+
+def aggregate_scores(scores: list[CaseScore]) -> dict:
+    """`strategy_metrics`' dict from per-case scores, **unrounded**. Sums run
+    in the order `scores` is given."""
+    n_cases = len(scores)
+    out = {
+        "n_cases": n_cases,
+        "violation_rate": (sum(1 for s in scores if s.violation) / n_cases
+                           if n_cases else None),
+    }
+    for key in ("pass_rate", "quality_regret", "cost_regret", "latency_regret",
+                "floor_miss_rate"):
+        out[key] = _mean(_values(scores, _SCORE_FIELD[key]))
+    out["infeasible_at_floor"] = sum(1 for s in scores if s.infeasible_at_floor)
+    out["empty_set_mismatches"] = sum(1 for s in scores if s.empty_mismatch)
+    for key in ("mean_chosen_cost", "wasted_attempts", "exhausted_rate"):
+        out[key] = _mean(_values(scores, _SCORE_FIELD[key]))
+    return out
+
+
+def metric_support(scores: list[CaseScore]) -> dict[str, int]:
+    """How many cases each `aggregate_scores` rate or mean is over: its
+    denominator. `violation_rate` is over every case; the rest are over the
+    cases whose `CaseScore` field is not `None`."""
+    support = {"violation_rate": len(scores)}
+    for key, field in _SCORE_FIELD.items():
+        support[key] = len(_values(scores, field))
+    return support
 
-    violation_rate = violations_count / n_cases if n_cases else None
 
-    return _round_value({
-        "n_cases": n_cases,
-        "violation_rate": violation_rate,
-        "pass_rate": _mean(pass_flags),
-        "quality_regret": _mean(quality_regrets),
-        "cost_regret": _mean(cost_regrets),
-        "latency_regret": _mean(latency_regrets),
-        "floor_miss_rate": _mean(floor_miss_flags),
-        "infeasible_at_floor": infeasible_count,
-        "empty_set_mismatches": empty_mismatches,
-        "mean_chosen_cost": _mean(chosen_costs),
-        "wasted_attempts": _mean(wasted_counts),
-        "exhausted_rate": _mean(exhausted_flags),
-    })
+def strategy_metrics(
+    cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice], strategy: str,
+) -> dict:
+    """Aggregate metrics 1-5 (spec §6) for one `strategy`, over `cases`.
+
+    Returns:
+    - `n_cases`: `len(cases)`.
+    - `violation_rate`: share of `cases` whose attempt chain names **any**
+      endpoint not in that case's `eligible` set (including any attempt on
+      an empty case), plus a chain naming an eligible endpoint with no
+      matching sweep row, plus a non-empty case with no recorded `Choice`
+      at all for `strategy` -- all "should not happen" (brief ruling /
+      fix-round-1 finding 4), but counted as violations rather than
+      crashing. For an explicit `attempts` chain it is also a violation
+      when the chain repeats an endpoint, records entries after the served
+      attempt, or `Choice.endpoint` is not the chain's served endpoint
+      (`None` when exhausted). At most one violation per case: this is a
+      per-case rate, unlike `violations()`'s per-attempt count.
+    - `pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`,
+      `floor_miss_rate`: as spec §6 points 2-4, computed over non-empty
+      cases only (an empty case has no quality/cost/latency notion), from
+      the chain's `attempt_outcome` (B48 spec §5): a case passes iff the
+      *served* row passed; quality is the served row's (0 if exhausted);
+      cost regret uses the chain's summed cost and latency regret its
+      summed latency (failed attempts' latency plus the served call's).
+      An empty chain, an exhausted chain, or a violating chain scores
+      quality 0 and never contributes to cost/latency regret (those are
+      computed only when the served outcome passed).
+    - `mean_chosen_cost`: mean, over non-empty cases, of the chain's
+      failover-aware cost (sum of attempt costs; failed attempts, an
+      empty chain, and a violating chain cost 0.0) -- not itself one of
+      spec §6's five metrics, but needed by `headline`'s saving-percentage
+      calculation, which only receives this function's output.
+    - `wasted_attempts`: mean, over non-empty cases, of the chain's failed
+      attempts (B48 spec §5).
+    - `exhausted_rate`: share of non-empty cases whose chain was exhausted
+      (at least one attempt, none succeeded).
+    - `infeasible_at_floor`: count of non-empty cases where `P(c) = {}`.
+      Strategy-independent (an oracle property of the case), exposed here
+      per-strategy for convenient reporting.
+    - `empty_set_mismatches`: count of cases (strategy-independent) where
+      `expect_empty` disagrees with actual emptiness (`eligible == ()`), or
+      an actually-empty case was not `rejected_ok`. A case tripping both
+      conditions is counted once.
+
+    Any of the five regret-shaped metrics is `None`, not 0.0, when its
+    denominator is empty (e.g. no non-empty cases, or no case where the
+    choice passed) -- an aggregate with no data is undefined, not zero.
+    """
+    return _round_value(aggregate_scores(case_scores(cases, rows, choices, strategy)))
 
 def grouped(
     cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice],
````

Read the patch before applying it: apart from the new dataclass and the three small functions at the end, every added line of `case_scores` is a line the patch removes from `strategy_metrics`, with `violations_count += 1` become `violation = True` and each `xs.append(v)` become a field of the `CaseScore`.

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_oracle_scores.py tests/eval/test_oracle.py tests/eval/test_bench_golden.py -p no:warnings -rf
```

Expected: `73 passed`. `test_oracle.py` (57 tests, untouched) and the golden test passing here are the evidence that `strategy_metrics` did not move.

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1332 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add app/eval/oracle.py tests/eval/test_oracle_scores.py
git commit -m "$(cat <<'EOF'
refactor(eval): per-case scores behind strategy_metrics (P2 metrics)

The loop inside strategy_metrics becomes case_scores, one CaseScore per
case; strategy_metrics is the rounded aggregate of them and its output is
byte-identical (the golden pin). A group or a resample can now aggregate a
subset of cases without re-deriving any rule.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The group report, pure (spec §4, §8; E3–E6, E13, E14)

**Files:**
- Create: `app/eval/groups.py`
- Create: `tests/eval/test_groups.py`

**Interfaces:**
- Consumes: `app/eval/oracle.py` — `CaseScore`, `_mean`, `_round_value`, `_values` (Task 2).
- Produces, in `app/eval/groups.py`:
  - Constants: `SCHEMA = "routeforge.bench-group-report/v1"`; `DIMENSIONS = ("placement", "protection", "application", "workload")`; `UNTAGGED = "(untagged)"`; `DEFAULT_MIN_GROUP_SIZE = 5`; `INSUFFICIENT = "insufficient_cases"`; `GROUP_METRICS` (seven names, the order of the Global Constraints); `LOWER_IS_WORSE = ("pass_rate",)`.
  - Fixed text: `CAVEAT_SIMULATED`, `CAVEAT_LIVE`, `CAVEAT_TIER_RULE`, `CAVEAT_SYNTHETIC`, `CAVEAT_GROUPS`, `CAVEAT_TO_MEASURE`; `bench_caveats(mode: str) -> list[str]` (`mode` is `"simulated"` or `"live"`).
  - `application_key(tags: dict, app_policy: dict) -> str`; `case_keys(tags: dict, app_policy: dict) -> dict[str, str]` (one label per dimension, never a missing key).
  - `partition(case_ids: list[str], keys: dict[str, dict[str, str]], dimension: str) -> dict[str, list[str]]` — groups sorted by label, ids in the order given.
  - `metric_value(scores, metric) -> tuple[float | None, int]`; `cell(scores, metric, min_size) -> dict` (`{"value", "n"}` or `{"state": "insufficient_cases", "n"}`); `group_block(scores, min_size) -> dict`; `worst_groups(dimensions) -> dict[str, dict | None]`; `strategy_groups(scores, keys, min_size) -> dict` (`{"overall", "dimensions", "worst"}`, unrounded).
  - `group_report(scores_by_strategy: dict[str, list[CaseScore]], keys: dict[str, dict[str, str]], *, min_size: int, mode: str) -> dict` — the `groups.json` document, rounded; raises `ValueError` on a bad `min_size`.
  - `figure_text(figure: dict) -> str`; `render_group_report(doc: dict, *, detail: str) -> str`.

  Task 4 calls `case_keys`, `group_report`, `render_group_report` and reads `DEFAULT_MIN_GROUP_SIZE`. Task 6 imports `DIMENSIONS`, `INSUFFICIENT`, `bench_caveats`, `partition` and the caveat constants.

**The hand-computed example** (spec §12.1; the test module's docstring repeats it). Eight cases, one strategy; `-` is "outside that figure's denominator".

| case | workload | placement | passed | quality regret | floor miss | cost regret |
|---|---|---|---|---|---|---|
| c1 | faq | any | 1 | 0 | 0 | 0.2 |
| c2 | faq | any | 1 | 0 | 0 | 0.0 |
| c3 | faq | any | 0 | 1 | 1 | - |
| c4 | faq | any | 1 | 0 | 0 | 0.4 |
| c5 | faq | onprem_only | 1 | 0 | 0 | 0.0 |
| c6 | claim | onprem_only | 1 | 0 | 0 | 0.6 |
| c7 | claim | onprem_only | 0 | 1 | 1 | - |
| c8 | claim | onprem_only | 0 | 1 | - | - |

- `pass_rate`: faq 4/5 = 0.8 (n = 5); claim 1/3 (n = 3); overall 5/8. `(5 × 0.8 + 3 × 1/3)/8 = 5/8`.
- `cost_regret`: faq 0.6/4 = 0.15 (n = 4); claim 0.6 (n = 1); overall 1.2/5 = 0.24. By denominator `(4 × 0.15 + 1 × 0.6)/5 = 0.24`; by case count `(5 × 0.15 + 3 × 0.6)/8 = 0.31875` — wrong, and pinned as wrong.
- At a minimum of 3, claim prints a pass rate, no cost regret (n = 1) and no floor-miss rate (n = 2).
- With applications `app-a = {c1, c2, c6}` and `app-b` the rest: seven groups print a pass rate (two placements, one protection, two applications, two workloads); the lowest is `workload = claim`, 1/3. The highest printed cost regret is `application = app-a`, `(0.2 + 0 + 0.6)/3`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_groups.py`:

````python
"""The group report (`app/eval/groups.py`; spec §4; invariants E3-E6, E13,
E14). Pure: every test builds `CaseScore`s by hand.

The hand-computed example (eight cases, two workloads):

    case  workload  placement    passed  q-regret  floor-miss  cost-regret  lat-regret
    c1    faq       any          1       0         0           0.2          10
    c2    faq       any          1       0         0           0.0          0
    c3    faq       any          0       1         1           -            -
    c4    faq       any          1       0         0           0.4          30
    c5    faq       onprem_only  1       0         0           0.0          0
    c6    claim     onprem_only  1       0         0           0.6          60
    c7    claim     onprem_only  0       1         1           -            -
    c8    claim     onprem_only  0       1         -           -            -     (P = {})

    pass_rate    faq 4/5 = 0.8     claim 1/3            overall 5/8
    cost_regret  faq 0.6/4 = 0.15  claim 0.6/1 = 0.6    overall 1.2/5 = 0.24

`5 x 0.8 + 3 x (1/3) = 5`, over 8: the overall pass rate is the combination
of the groups weighted by each figure's OWN denominator. Cost regret shows
why the weight cannot be the case count: `(4 x 0.15 + 1 x 0.6) / 5 = 0.24`
is right, `(5 x 0.15 + 3 x 0.6) / 8 = 0.31875` is not.
"""
from __future__ import annotations

import json

import pytest

from app.eval import groups
from app.eval.groups import (
    CAVEAT_GROUPS,
    CAVEAT_LIVE,
    CAVEAT_SIMULATED,
    CAVEAT_SYNTHETIC,
    CAVEAT_TIER_RULE,
    CAVEAT_TO_MEASURE,
    DIMENSIONS,
    GROUP_METRICS,
    INSUFFICIENT,
    UNTAGGED,
    application_key,
    bench_caveats,
    case_keys,
    cell,
    group_report,
    metric_value,
    partition,
    render_group_report,
    strategy_groups,
)
from app.eval.oracle import CaseScore


def _score(case_id, passed, *, floor_miss="same", cost=None, latency=None,
           violation=False, empty=False, mismatch=False) -> CaseScore:
    """A non-empty case by default: quality regret and floor miss are `1 -
    passed` unless `floor_miss` says otherwise (None = P(c) is empty)."""
    if empty:
        return CaseScore(case_id, True, violation, mismatch, False, None, None, None,
                         None, None, None, None, None)
    miss = (1.0 - passed) if floor_miss == "same" else floor_miss
    return CaseScore(case_id, False, violation, mismatch, miss is None, float(passed),
                     1.0 - passed, miss, cost, latency, 1.0, 0.0, 0.0)


SCORES = [
    _score("c1", 1, cost=0.2, latency=10.0),
    _score("c2", 1, cost=0.0, latency=0.0),
    _score("c3", 0),
    _score("c4", 1, cost=0.4, latency=30.0),
    _score("c5", 1, cost=0.0, latency=0.0),
    _score("c6", 1, cost=0.6, latency=60.0),
    _score("c7", 0),
    _score("c8", 0, floor_miss=None),
]
KEYS = {
    s.case_id: {
        "workload": "faq" if s.case_id <= "c5" else "claim",
        "placement": "any" if s.case_id <= "c4" else "onprem_only",
        "protection": "enforced",
        "application": "app-a" if s.case_id in ("c1", "c2", "c6") else "app-b",
    } for s in SCORES}


def _doc(min_size=3, *, scores=None, keys=None, mode="simulated") -> dict:
    return group_report({"s": scores or SCORES}, keys or KEYS, min_size=min_size,
                        mode=mode)


# --- which group a case is in (E3) ---------------------------------------------

def test_application_is_the_app_tag_or_the_policy_fingerprint():
    assert application_key({"app": "billing"}, {"residency": "any"}) == "billing"
    a = application_key({}, {"residency": "any", "policy_mode": "enforced"})
    b = application_key({}, {"policy_mode": "enforced", "residency": "any"})
    c = application_key({}, {"residency": "onprem_only", "policy_mode": "enforced"})
    assert a == b and a != c                      # key order does not matter; values do
    assert a.startswith("policy-") and len(a) == len("policy-") + 8
    # An empty or non-string tag is not an application name.
    assert application_key({"app": ""}, {}) == application_key({"app": True}, {})


def test_a_missing_tag_is_the_untagged_group():
    keys = case_keys({"workload": "faq"}, {})
    assert keys["workload"] == "faq"
    assert keys["placement"] == UNTAGGED and keys["protection"] == UNTAGGED
    assert set(keys) == set(DIMENSIONS)
    # A boolean tag value is a label like any other.
    assert case_keys({"protection": True}, {})["protection"] == "True"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_e3_every_case_is_in_exactly_one_group_per_dimension(dimension):
    ids = [s.case_id for s in SCORES]
    parts = partition(ids, KEYS, dimension)
    members = [i for group in parts.values() for i in group]
    assert sorted(members) == sorted(ids) and len(members) == len(set(members))
    assert list(parts) == sorted(parts)


def test_e3_group_sizes_sum_to_the_total():
    doc = _doc()
    for dim in DIMENSIONS:
        assert sum(doc["group_sizes"][dim].values()) == doc["n_cases"] == 8
        blocks = doc["strategies"]["s"]["dimensions"][dim]
        assert sum(b["n_cases"] for b in blocks.values()) == 8
        assert {k: b["n_cases"] for k, b in blocks.items()} == doc["group_sizes"][dim]


def test_e3_an_untagged_case_is_counted_not_dropped():
    keys = {**KEYS, "c8": {**KEYS["c8"], "workload": UNTAGGED}}
    doc = _doc(keys=keys)
    assert doc["group_sizes"]["workload"] == {UNTAGGED: 1, "claim": 2, "faq": 5}


# --- the hand-computed example (E6) --------------------------------------------

def test_e6_group_figures_match_the_hand_computed_example():
    workload = _doc()["strategies"]["s"]["dimensions"]["workload"]
    faq, claim = workload["faq"]["metrics"], workload["claim"]["metrics"]
    assert faq["pass_rate"] == {"value": 0.8, "n": 5}
    assert faq["quality_regret"] == {"value": 0.2, "n": 5}
    assert faq["floor_miss_rate"] == {"value": 0.2, "n": 5}
    assert faq["cost_regret"] == {"value": 0.15, "n": 4}
    assert faq["latency_regret"] == {"value": 10.0, "n": 4}
    assert faq["violation_rate"] == {"value": 0.0, "n": 5}
    assert claim["pass_rate"] == {"value": round(1 / 3, 12), "n": 3}
    assert claim["quality_regret"] == {"value": round(2 / 3, 12), "n": 3}
    overall = _doc()["strategies"]["s"]["overall"]["metrics"]
    assert overall["pass_rate"] == {"value": 0.625, "n": 8}
    assert overall["cost_regret"] == {"value": 0.24, "n": 5}


def test_e6_the_worst_group_is_named_with_its_size():
    worst = _doc()["strategies"]["s"]["worst"]
    # Lowest pass rate: claim (1/3), not the onprem_only placement (2/4).
    assert worst["pass_rate"] == {
        "dimension": "workload", "group": "claim", "value": round(1 / 3, 12), "n": 3,
        "n_cases": 3, "groups_printed": 7, "groups_at_value": 1}
    # Highest cost regret among the PRINTED figures: claim's 0.6 is over one
    # case and is not printed; application app-a is (0.2 + 0 + 0.6) / 3.
    assert worst["cost_regret"]["group"] == "app-a"
    assert worst["cost_regret"]["dimension"] == "application"
    assert worst["cost_regret"]["value"] == round(0.8 / 3, 12)


def test_e6_no_group_is_named_when_every_printed_group_is_equal():
    doc = _doc()
    worst = doc["strategies"]["s"]["worst"]["violation_rate"]
    assert worst["value"] == 0.0
    assert worst["groups_at_value"] == worst["groups_printed"] == 7
    line = next(row for row in render_group_report(doc, detail="s").splitlines()
                if row.startswith("| s | violation_rate |"))
    assert line == "| s | violation_rate | all | all | 0 in every printed group (7) | n/a |"


def test_e6_a_metric_no_group_prints_has_no_worst_group():
    doc = _doc(min_size=9)
    assert all(v is None for v in doc["strategies"]["s"]["worst"].values())
    assert "no group has enough cases" in render_group_report(doc, detail="s")


def test_e6_a_tie_keeps_the_first_dimension_then_label():
    scores = [_score("a1", 0), _score("a2", 1), _score("b1", 0), _score("b2", 1)]
    keys = {s.case_id: {"placement": s.case_id[0], "protection": "p",
                        "application": s.case_id[0], "workload": "w"} for s in scores}
    worst = strategy_groups(scores, keys, 1)["worst"]["pass_rate"]
    assert (worst["dimension"], worst["group"]) == ("placement", "a")


# --- the weighted combination (E4) ---------------------------------------------

@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("metric", GROUP_METRICS)
def test_e4_overall_is_the_denominator_weighted_combination(dimension, metric):
    """With every group printed (minimum 1): overall = sum(n_g x value_g) /
    sum(n_g), where n_g is the FIGURE's denominator in group g."""
    block = strategy_groups(SCORES, KEYS, 1)
    figures = [g["metrics"][metric] for g in block["dimensions"][dimension].values()]
    printed = [f for f in figures if "value" in f]
    overall = block["overall"]["metrics"][metric]
    assert sum(f["n"] for f in figures) == overall["n"]
    assert sum(f["n"] * f["value"] for f in printed) / overall["n"] == pytest.approx(
        overall["value"], abs=1e-12)


def test_e4_weighting_by_case_count_is_wrong_for_a_conditional_metric():
    block = strategy_groups(SCORES, KEYS, 1)
    workload = block["dimensions"]["workload"]
    by_cases = sum(g["n_cases"] * g["metrics"]["cost_regret"]["value"]
                   for g in workload.values()) / 8
    assert by_cases == pytest.approx(0.31875)
    assert block["overall"]["metrics"]["cost_regret"]["value"] == pytest.approx(0.24)


# --- small groups (E5) ---------------------------------------------------------

def test_e5_a_figure_below_the_minimum_prints_a_count_and_no_rate():
    claim = _doc(min_size=4)["strategies"]["s"]["dimensions"]["workload"]["claim"]
    assert claim["n_cases"] == 3
    for metric in GROUP_METRICS:
        assert "value" not in claim["metrics"][metric]
        assert claim["metrics"][metric]["state"] == INSUFFICIENT
    assert claim["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 3}
    text = render_group_report(_doc(min_size=4), detail="s")
    assert "insufficient cases (n = 3)" in text


def test_e5_the_rule_is_on_the_figures_own_denominator():
    """claim has 3 cases, enough for a pass rate at minimum 3 -- but its cost
    regret is over ONE passing case and its floor-miss rate over two."""
    claim = _doc(min_size=3)["strategies"]["s"]["dimensions"]["workload"]["claim"]
    assert "value" in claim["metrics"]["pass_rate"]
    assert claim["metrics"]["cost_regret"] == {"state": INSUFFICIENT, "n": 1}
    assert claim["metrics"]["floor_miss_rate"] == {"state": INSUFFICIENT, "n": 2}


def test_e5_the_minimum_is_inclusive():
    scores = SCORES[:5]
    assert "value" in cell(scores, "pass_rate", 5)
    assert cell(scores, "pass_rate", 6) == {"state": INSUFFICIENT, "n": 5}


def test_e5_a_violation_in_a_one_case_group_is_still_counted():
    scores = [*SCORES, _score("c9", 0, violation=True)]
    keys = {**KEYS, "c9": {"workload": "rare", "placement": "any",
                           "protection": "enforced", "application": "app-b"}}
    rare = _doc(min_size=5, scores=scores, keys=keys)[
        "strategies"]["s"]["dimensions"]["workload"]["rare"]
    assert rare["n_cases"] == 1 and rare["violations"] == 1
    assert rare["metrics"]["violation_rate"] == {"state": INSUFFICIENT, "n": 1}
    text = render_group_report(_doc(min_size=5, scores=scores, keys=keys), detail="s")
    # cases 1, violations 1, empty-set mismatches 0 -- then no rate at all.
    assert ("| workload | rare | 1 | 1 | 0 | insufficient cases (n = 1) |"
            in text)


def test_e5_an_empty_case_counts_in_the_group_and_in_no_quality_figure():
    scores = [*SCORES, _score("c9", 0, empty=True, mismatch=True)]
    keys = {**KEYS, "c9": KEYS["c1"]}
    faq = strategy_groups(scores, keys, 1)["dimensions"]["workload"]["faq"]
    assert faq["n_cases"] == 6 and faq["empty_cases"] == 1
    assert faq["empty_set_mismatches"] == 1
    assert faq["metrics"]["pass_rate"]["n"] == 5
    assert faq["metrics"]["empty_set_mismatch_rate"] == {"value": 1 / 6, "n": 6}
    assert metric_value([scores[-1]], "pass_rate") == (None, 0)


def test_a_bad_minimum_is_refused():
    for bad in (0, -1, 2.5, True, "5"):
        with pytest.raises(ValueError, match="min_size"):
            _doc(min_size=bad)


# --- the fixed text (E13, E14) -------------------------------------------------

def test_e14_the_caveats_are_the_spec_text():
    assert CAVEAT_SIMULATED == (
        "On the simulated backend, quality is an input: each pass or fail is a seeded "
        "draw against a probability written in the endpoint file. Pass rate and quality "
        "regret measure the harness, not the models.")
    assert CAVEAT_TIER_RULE == (
        "The router comparison is mostly the tier rule: the target tier is chosen from "
        "the request's length, so on short cases the router's regret against the oracle "
        "is set by which tier it may use, not by the four-term scorer.")
    assert CAVEAT_SYNTHETIC == "Cases are synthetic and single-turn."
    assert CAVEAT_GROUPS == (
        "A group is the cases of this suite that carry one label. A figure over fewer "
        "cases than the minimum prints its case count and no rate. A group above the "
        "minimum is still small: read its case count with every figure.")
    assert CAVEAT_TO_MEASURE == "Live-backend results: TO MEASURE."
    assert CAVEAT_LIVE == (
        "This run called the endpoints in the endpoint file, once per case and "
        "endpoint, and graded each answer with a deterministic grader. One call is one "
        "sample.")


def test_e14_a_simulated_report_opens_with_the_caveats_in_order():
    doc = _doc()
    assert doc["caveats"] == [CAVEAT_SIMULATED, CAVEAT_TIER_RULE, CAVEAT_SYNTHETIC,
                              CAVEAT_TO_MEASURE, CAVEAT_GROUPS]
    lines = render_group_report(doc, detail="s").splitlines()
    assert lines[0] == "# FeasibleRouteBench group report"
    assert lines[2:7] == [f"> {text}" for text in doc["caveats"]]


def test_e14_a_live_run_does_not_claim_quality_is_an_input():
    assert bench_caveats("live") == [CAVEAT_LIVE, CAVEAT_TIER_RULE, CAVEAT_SYNTHETIC]
    assert CAVEAT_SIMULATED not in _doc(mode="live")["caveats"]


_FORBIDDEN = ("rank", "recommend", "best", "winner", "prefer", "should", "relax",
              "promot", "vendor")


def _keys_of(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys_of(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys_of(v)}
    return set()


def test_e13_no_key_or_sentence_ranks_or_recommends():
    doc = _doc()
    text = render_group_report(doc, detail="s").lower()
    names = " ".join(sorted(_keys_of(doc))).lower()
    constants = " ".join(v for k, v in vars(groups).items()
                         if k.startswith("CAVEAT_") and isinstance(v, str)).lower()
    for word in _FORBIDDEN:
        assert word not in names, word
        assert word not in text, word
        assert word not in constants, word


# --- shape ---------------------------------------------------------------------

def test_the_document_is_deterministic_and_json_clean():
    one = json.dumps(_doc(), sort_keys=True)
    two = json.dumps(group_report({"s": list(SCORES)}, dict(reversed(list(KEYS.items()))),
                                  min_size=3, mode="simulated"), sort_keys=True)
    assert one == two
    assert json.loads(one)["schema"] == "routeforge.bench-group-report/v1"


def test_the_detail_table_is_only_for_the_named_strategy():
    doc = group_report({"s": SCORES, "t": SCORES}, KEYS, min_size=3, mode="simulated")
    text = render_group_report(doc, detail="t")
    assert "## Every group: `t`" in text and "## Every group: `s`" not in text
    assert "## Every group" not in render_group_report(doc, detail="absent")
    assert "None" not in text
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_groups.py -p no:warnings -rf
```

Expected: `1 error` during collection — `ImportError: cannot import name 'groups' from 'app.eval'`.

- [ ] **Step 3: Write the module**

Create `app/eval/groups.py`:

````python
"""Group report for FeasibleRouteBench (P2 evaluation metrics, spec §4).

Every bench metric, per strategy, broken down by four dimensions of a case:

- `placement`, `protection`, `workload` -- the case author's own tags (the
  same three `oracle.grouped` reads);
- `application` -- `tags.app` when the author set one, else a fingerprint
  of the case's `app_policy`: cases with the same policy are one
  application. The bench runs every case under one tenant id, so there is
  no tenant to group by.

A case with no value for a dimension is in that dimension's `(untagged)`
group, so every case is in exactly one group per dimension.

**Small groups print no rate.** A figure over fewer than `min_size` cases
is `{"state": "insufficient_cases", "n": k}`. The rule is applied to each
figure's own denominator, not to the group's case count: cost regret is over
the cases whose served answer passed, which can be far fewer. Counts
(`violations`, `empty_set_mismatches`) are always printed -- a violation in
a one-case group must stay visible.

**Worst group.** Per metric, the group with the worst figure among the
figures that are printed. It names a set of cases. It orders no model,
endpoint or vendor.

Pure: no pipeline, registry, settings or clock. Never imported by serving
code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json

from app.eval.oracle import CaseScore, _mean, _round_value, _values

SCHEMA = "routeforge.bench-group-report/v1"

DIMENSIONS = ("placement", "protection", "application", "workload")
#: dimension -> the case tag it is read from (`application` falls back to
#: the policy fingerprint, see `application_key`).
_TAG = {"placement": "placement", "protection": "protection",
        "application": "app", "workload": "workload"}
UNTAGGED = "(untagged)"
DEFAULT_MIN_GROUP_SIZE = 5
INSUFFICIENT = "insufficient_cases"

#: metric -> the `CaseScore` field it is the mean of (see `cell`).
_FIELD = {
    "violation_rate": "violation",
    "pass_rate": "passed",
    "quality_regret": "quality_regret",
    "cost_regret": "cost_regret",
    "latency_regret": "latency_regret",
    "floor_miss_rate": "floor_miss",
    "empty_set_mismatch_rate": "empty_mismatch",
}
GROUP_METRICS = tuple(_FIELD)
#: `pass_rate` is the one metric where a LOWER value is the worse one.
LOWER_IS_WORSE = ("pass_rate",)

# --- fixed caveat text (spec §8; pinned by tests/eval/test_groups.py) ----------

CAVEAT_SIMULATED = (
    "On the simulated backend, quality is an input: each pass or fail is a seeded "
    "draw against a probability written in the endpoint file. Pass rate and quality "
    "regret measure the harness, not the models.")
CAVEAT_LIVE = (
    "This run called the endpoints in the endpoint file, once per case and "
    "endpoint, and graded each answer with a deterministic grader. One call is one "
    "sample.")
CAVEAT_TIER_RULE = (
    "The router comparison is mostly the tier rule: the target tier is chosen from "
    "the request's length, so on short cases the router's regret against the oracle "
    "is set by which tier it may use, not by the four-term scorer.")
CAVEAT_SYNTHETIC = "Cases are synthetic and single-turn."
CAVEAT_GROUPS = (
    "A group is the cases of this suite that carry one label. A figure over fewer "
    "cases than the minimum prints its case count and no rate. A group above the "
    "minimum is still small: read its case count with every figure.")
CAVEAT_TO_MEASURE = "Live-backend results: TO MEASURE."


def bench_caveats(mode: str) -> list[str]:
    """The caveats every new report opens with. `mode` is `run.json`'s
    `"simulated"` or `"live"`."""
    first = CAVEAT_SIMULATED if mode == "simulated" else CAVEAT_LIVE
    tail = [CAVEAT_TO_MEASURE] if mode == "simulated" else []
    return [first, CAVEAT_TIER_RULE, CAVEAT_SYNTHETIC, *tail]


# --- which group a case is in --------------------------------------------------

def application_key(tags: dict, app_policy: dict) -> str:
    """`tags["app"]` when it is a non-empty string, else `policy-` plus the
    first 8 hex of sha256 over the case's `app_policy` (sorted keys): cases
    with the same policy are the same application."""
    app = tags.get("app")
    if isinstance(app, str) and app:
        return app
    blob = json.dumps(app_policy, sort_keys=True, default=str)
    return "policy-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def case_keys(tags: dict, app_policy: dict) -> dict[str, str]:
    """One group label per dimension for one case. Never missing a key."""
    keys: dict[str, str] = {}
    for dim in DIMENSIONS:
        if dim == "application":
            keys[dim] = application_key(tags, app_policy)
        else:
            value = tags.get(_TAG[dim])
            keys[dim] = UNTAGGED if value is None else str(value)
    return keys


def partition(case_ids: list[str], keys: dict[str, dict[str, str]],
              dimension: str) -> dict[str, list[str]]:
    """`{group: [case ids]}` for one dimension, groups sorted by label, each
    group's ids in the order given. Every id lands in exactly one group."""
    groups: dict[str, list[str]] = {}
    for case_id in case_ids:
        groups.setdefault(keys[case_id][dimension], []).append(case_id)
    return dict(sorted(groups.items()))


# --- figures -------------------------------------------------------------------

def metric_value(scores: list[CaseScore], metric: str) -> tuple[float | None, int]:
    """`(value, n)`: one metric over `scores`, unrounded, and the number of
    cases it is over. The two boolean fields are rates over every case; the
    rest are means over the cases whose field is not `None` -- the same
    denominators as `oracle.aggregate_scores`."""
    field = _FIELD[metric]
    if metric in ("violation_rate", "empty_set_mismatch_rate"):
        n = len(scores)
        return (sum(1 for s in scores if getattr(s, field)) / n if n else None), n
    values = _values(scores, field)
    return _mean(values), len(values)


def cell(scores: list[CaseScore], metric: str, min_size: int) -> dict:
    """One printed figure: `{"value", "n"}`, or `{"state":
    "insufficient_cases", "n"}` when it is over fewer than `min_size` cases."""
    value, n = metric_value(scores, metric)
    if n < min_size or value is None:
        return {"state": INSUFFICIENT, "n": n}
    return {"value": value, "n": n}


def group_block(scores: list[CaseScore], min_size: int) -> dict:
    """One group's block: its counts (always) and one `cell` per metric."""
    return {
        "n_cases": len(scores),
        "empty_cases": sum(1 for s in scores if s.empty),
        "violations": sum(1 for s in scores if s.violation),
        "empty_set_mismatches": sum(1 for s in scores if s.empty_mismatch),
        "metrics": {m: cell(scores, m, min_size) for m in GROUP_METRICS},
    }


def worst_groups(dimensions: dict[str, dict[str, dict]]) -> dict[str, dict | None]:
    """Per metric, the printed figure that is worst across every group of
    every dimension: highest, or lowest for `pass_rate`. `None` when no group
    prints that metric. A tie keeps the first in `DIMENSIONS` order, then
    label order.

    `groups_printed` is how many groups print the metric and `groups_at_value`
    how many of them share the worst value: when the two are equal no group
    is worse than another, and the report says so instead of naming one."""
    worst: dict[str, dict | None] = {m: None for m in GROUP_METRICS}
    for metric in GROUP_METRICS:
        printed = [(dim, label, block) for dim in DIMENSIONS
                   for label, block in dimensions.get(dim, {}).items()
                   if "value" in block["metrics"][metric]]
        if not printed:
            continue
        values = [block["metrics"][metric]["value"] for _, _, block in printed]
        target = min(values) if metric in LOWER_IS_WORSE else max(values)
        dim, label, block = next(p for p, v in zip(printed, values) if v == target)
        worst[metric] = {"dimension": dim, "group": label, "value": target,
                         "n": block["metrics"][metric]["n"],
                         "n_cases": block["n_cases"],
                         "groups_printed": len(printed),
                         "groups_at_value": sum(1 for v in values if v == target)}
    return worst


def strategy_groups(scores: list[CaseScore], keys: dict[str, dict[str, str]],
                    min_size: int) -> dict:
    """One strategy's `{"overall", "dimensions", "worst"}`. `scores` is that
    strategy's `oracle.case_scores`, in suite order."""
    by_id = {s.case_id: s for s in scores}
    ids = [s.case_id for s in scores]
    dimensions = {
        dim: {label: group_block([by_id[i] for i in members], min_size)
              for label, members in partition(ids, keys, dim).items()}
        for dim in DIMENSIONS}
    return {"overall": group_block(scores, min_size), "dimensions": dimensions,
            "worst": worst_groups(dimensions)}


def group_report(scores_by_strategy: dict[str, list[CaseScore]],
                 keys: dict[str, dict[str, str]], *, min_size: int, mode: str) -> dict:
    """The `groups.json` document. Floats are rounded to 12 dp, like every
    other bench output."""
    if isinstance(min_size, bool) or not isinstance(min_size, int) or min_size < 1:
        raise ValueError(f"min_size must be an int >= 1; got {min_size!r}")
    ids = sorted(keys)
    return _round_value({
        "schema": SCHEMA,
        "min_group_size": min_size,
        "dimensions": list(DIMENSIONS),
        "metrics": list(GROUP_METRICS),
        "n_cases": len(ids),
        "group_sizes": {dim: {label: len(members)
                              for label, members in partition(ids, keys, dim).items()}
                        for dim in DIMENSIONS},
        "strategies": {name: strategy_groups(scores, keys, min_size)
                       for name, scores in scores_by_strategy.items()},
        "caveats": [*bench_caveats(mode), CAVEAT_GROUPS],
    })


# --- groups.md -----------------------------------------------------------------

def figure_text(figure: dict) -> str:
    """A printed figure, or `insufficient cases (n = k)`."""
    if "value" not in figure:
        return f"insufficient cases (n = {figure['n']})"
    return f"{figure['value']:.6g} (n = {figure['n']})"


def render_group_report(doc: dict, *, detail: str) -> str:
    """`groups.md`: the caveats, the group sizes, every strategy's worst
    groups, and the full breakdown of the one strategy named `detail`."""
    lines = ["# FeasibleRouteBench group report", ""]
    lines += [f"> {text}" for text in doc["caveats"]]
    lines += ["", f"Minimum cases behind a printed rate: **{doc['min_group_size']}** "
              f"(`--min-group-size`). Cases in this run: {doc['n_cases']}.", ""]
    lines += ["## Group sizes", "", "| dimension | group | cases |", "|---|---|---|"]
    for dim in doc["dimensions"]:
        for label, n in doc["group_sizes"][dim].items():
            lines.append(f"| {dim} | {label} | {n} |")
    lines += ["", "## Worst group per metric", "",
              "The worst printed figure across every group of every dimension. It "
              "names a set of cases, not a model or an endpoint.", "",
              "| strategy | metric | dimension | group | figure | group cases |",
              "|---|---|---|---|---|---|"]
    for name, block in doc["strategies"].items():
        for metric in doc["metrics"]:
            worst = block["worst"][metric]
            if worst is None:
                lines.append(f"| {name} | {metric} | n/a | n/a | "
                             "no group has enough cases | n/a |")
            elif (worst["groups_printed"] > 1
                  and worst["groups_at_value"] == worst["groups_printed"]):
                lines.append(f"| {name} | {metric} | all | all | {worst['value']:.6g} in "
                             f"every printed group ({worst['groups_printed']}) | n/a |")
            else:
                lines.append(f"| {name} | {metric} | {worst['dimension']} | "
                             f"{worst['group']} | {figure_text(worst)} | "
                             f"{worst['n_cases']} |")
    block = doc["strategies"].get(detail)
    if block is not None:
        lines += ["", f"## Every group: `{detail}`", "",
                  "`groups.json` holds the same table for every strategy.", ""]
        header = ("| dimension | group | cases | violations | empty-set mismatches | "
                  + " | ".join(doc["metrics"]) + " |")
        lines += [header, "|---|---|---|---|---|" + "---|" * len(doc["metrics"])]
        rows = [("overall", "all cases", block["overall"])]
        rows += [(dim, label, group) for dim in doc["dimensions"]
                 for label, group in block["dimensions"][dim].items()]
        for dim, label, group in rows:
            lines.append(
                f"| {dim} | {label} | {group['n_cases']} | {group['violations']} | "
                f"{group['empty_set_mismatches']} | "
                + " | ".join(figure_text(group["metrics"][m]) for m in doc["metrics"])
                + " |")
    lines.append("")
    return "\n".join(lines)
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_groups.py tests/eval/test_bench_golden.py tests/eval/test_isolation.py -p no:warnings -rf
```

Expected: `62 passed` (54 + 6 + 2).

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1386 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add app/eval/groups.py tests/eval/test_groups.py
git commit -m "$(cat <<'EOF'
feat(eval): group figures with denominators and a minimum size (P2 metrics)

app/eval/groups.py: every bench metric by placement, protection,
application and workload. Each figure carries its own denominator; one over
fewer cases than the minimum is a count and no rate, while violation counts
are never suppressed; the worst group is named with its size, and not at
all when every group is equal. Pure; the runner wiring is the next commit.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `--group-report` in the runner and the CLI (spec §4.5; E1–E3, E5, E15)

**Files:**
- Modify: `app/eval/runner.py` (module docstring, imports, `RunResult`, `run_bench`, `_run_loaded`)
- Modify: `scripts/feasible_bench.py` (module docstring, an import, two flags, one usage error, the call)
- Create: `tests/eval/test_group_report_run.py`

**Interfaces:**
- Consumes: `app/eval/groups.py::DEFAULT_MIN_GROUP_SIZE`, `case_keys`, `group_report`, `render_group_report` (Task 3); `app/eval/oracle.py::case_scores` (Task 2); `tests/eval/test_runner.py::DEFAULT_CASES`, `_case`, `_cli`, `_run`, `_write_inputs`.
- Produces:
  - `run_bench(..., group_report: bool = False, min_group_size: int = DEFAULT_MIN_GROUP_SIZE) -> RunResult` — both keyword-only, after `seconds_per_case`. A `min_group_size` that is not an `int >= 1` is a load error (exit 1, nothing written).
  - `RunResult.groups: dict | None = None` — the `groups.json` document, as on disk.
  - With `group_report=True`: `<out>/groups.json` and `<out>/groups.md` (the detail table is `HEADLINE_ROUTER`, `router_online`), and `run.json`'s `args` gains `group_report: true` and `min_group_size`. Without it `args` keeps exactly its current keys.
  - Every run removes a stale `groups.json` / `groups.md` from `<out>` before writing.
  - CLI: `--group-report` (flag), `--min-group-size N` (default `None` → 5). `--min-group-size` without `--group-report` prints `feasible_bench: load error: --min-group-size needs --group-report` and returns 1.

  Task 7 patches the same two files again, on top of this.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_group_report_run.py`:

````python
"""`--group-report` end to end (spec §4.4; invariants E1-E3, E5): the runner
and the CLI, over the same tiny simulated suite `tests/eval/test_runner.py`
uses. Every run is simulated; nothing makes a network call.
"""
from __future__ import annotations

import json

import pytest

from app.eval import runner
from app.eval.groups import DIMENSIONS, INSUFFICIENT
from tests.eval.test_runner import (DEFAULT_CASES, _case, _cli, _run, _write_inputs)

PINNED = ("metrics.json", "matrix.jsonl", "choices.jsonl")
#: groups.json metric -> the same figure's key in metrics.json.
SHARED = ("violation_rate", "pass_rate", "quality_regret", "cost_regret",
          "latency_regret", "floor_miss_rate")


def _figure(cell: dict):
    return cell.get("value")


def test_e1_without_the_flag_nothing_new_is_written(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.groups is None
    assert sorted(p.name for p in out.iterdir()) == [
        "choices.jsonl", "matrix.jsonl", "metrics.json", "report.md", "run.json"]
    stamp = json.loads((out / "run.json").read_text())
    assert "group_report" not in stamp["args"] and "min_group_size" not in stamp["args"]


def test_e1_the_flag_adds_two_files_and_changes_no_other(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    plain, flagged = tmp_path / "plain", tmp_path / "flagged"
    _run(suite, eps, plain, seed=3)
    result = _run(suite, eps, flagged, seed=3, group_report=True)
    for name in PINNED:
        assert (plain / name).read_bytes() == (flagged / name).read_bytes(), name
    assert sorted(p.name for p in flagged.iterdir()) == [
        "choices.jsonl", "groups.json", "groups.md", "matrix.jsonl", "metrics.json",
        "report.md", "run.json"]
    assert result.groups == json.loads((flagged / "groups.json").read_text())
    stamp = json.loads((flagged / "run.json").read_text())
    assert stamp["args"]["group_report"] is True and stamp["args"]["min_group_size"] == 5


def test_e2_the_group_report_is_byte_identical_twice(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    _run(suite, eps, a, seed=7, group_report=True, min_group_size=1)
    _run(suite, eps, b, seed=7, group_report=True, min_group_size=1)
    for name in ("groups.json", "groups.md"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_e3_the_run_partitions_its_cases(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    doc = _run(suite, eps, tmp_path / "out", group_report=True).groups
    assert doc["n_cases"] == len(DEFAULT_CASES) == 5
    for dim in DIMENSIONS:
        assert sum(doc["group_sizes"][dim].values()) == 5, dim
    assert doc["group_sizes"]["workload"] == {"claim": 2, "faq": 3}
    assert doc["group_sizes"]["placement"] == {"any": 4, "onprem_only": 1}
    assert doc["group_sizes"]["protection"] == {"enforced": 4, "passthrough": 1}
    # No case sets tags.app: an application is a distinct app_policy. Three
    # here -- any/enforced (3 cases), any/passthrough, onprem_only/enforced.
    sizes = doc["group_sizes"]["application"]
    assert sorted(sizes.values()) == [1, 1, 3]
    assert all(label.startswith("policy-") for label in sizes)


def test_the_app_tag_names_the_application(tmp_path):
    cases = [json.loads(json.dumps(c)) for c in DEFAULT_CASES]
    for case, app in zip(cases, ("billing", "billing", "claims", "claims", "claims")):
        case["tags"]["app"] = app
    suite, eps = _write_inputs(tmp_path, cases)
    doc = _run(suite, eps, tmp_path / "out", group_report=True).groups
    assert doc["group_sizes"]["application"] == {"billing": 2, "claims": 3}


def test_overall_figures_are_the_metrics_json_figures(tmp_path):
    """One set of numbers: `groups.json`'s overall block restates
    `metrics.json`'s strategies, with each figure's denominator beside it."""
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1)
    assert set(result.groups["strategies"]) == set(result.metrics["strategies"])
    for name, block in result.groups["strategies"].items():
        for metric in SHARED:
            assert (_figure(block["overall"]["metrics"][metric])
                    == result.metrics["strategies"][name][metric]), (name, metric)
        assert block["overall"]["n_cases"] == result.metrics["strategies"][name]["n_cases"]


def test_tag_groups_agree_with_the_existing_groups_block(tmp_path):
    """`metrics.json` already carries `groups` for the three tags. The report
    extends that block; it must not disagree with it."""
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1)
    for name, block in result.groups["strategies"].items():
        for tag in ("workload", "placement", "protection"):
            old = result.metrics["groups"][name][tag]
            new = block["dimensions"][tag]
            assert set(old) == set(new), (name, tag)
            for label in old:
                for metric in SHARED:
                    assert (_figure(new[label]["metrics"][metric])
                            == old[label][metric]), (name, tag, label, metric)


def test_e5_small_groups_print_no_rate_at_the_default_minimum(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    doc = _run(suite, eps, out, group_report=True).groups
    block = doc["strategies"]["router_online"]
    assert "value" in block["overall"]["metrics"]["pass_rate"]          # 5 cases
    onprem = block["dimensions"]["placement"]["onprem_only"]
    assert onprem["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 1}
    text = (out / "groups.md").read_text()
    assert "insufficient cases (n = 1)" in text and "None" not in text
    assert "## Every group: `router_online`" in text


def test_e5_an_injected_violation_shows_in_its_group_however_small(tmp_path, monkeypatch):
    """A strategy that always picks the cloud endpoint violates on the one
    `onprem_only` case. That group has one case: no rate, and the count."""
    monkeypatch.setattr(runner, "cheapest_attempts",
                        lambda registry, cap: (lambda inp, failed: ("cloud-c",)))
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True)
    assert result.exit_code == 2
    cheapest = result.groups["strategies"]["cheapest"]
    onprem = cheapest["dimensions"]["placement"]["onprem_only"]
    assert onprem["n_cases"] == 1 and onprem["violations"] == 1
    assert onprem["metrics"]["violation_rate"] == {"state": INSUFFICIENT, "n": 1}
    # Counts partition like cases do: the groups of a dimension sum to the total.
    assert cheapest["overall"]["violations"] == sum(
        g["violations"] for g in cheapest["dimensions"]["placement"].values()) >= 1
    assert result.groups["strategies"]["router_online"]["overall"]["violations"] == 0


def test_an_empty_case_is_in_its_group_and_in_no_quality_figure(tmp_path):
    cases = [_case("faq-1", "What is the capital of France?"),
             _case("onprem-empty", "Where is Paris?", residency="onprem_only",
                   expect_empty=True)]
    cloud_only = [{"slug": "cloud-c", "provider": "azure", "tier": "frontier",
                   "price": {"input": 5.0, "output": 5.0}, "region": "us",
                   "prompt_retention": "none", "accepts_raw_sensitive": False,
                   "simulated": {"quality": {"default": 1.0},
                                 "latency_ms": {"mean": 900, "jitter": 20}}}]
    suite, eps = _write_inputs(tmp_path, cases, cloud_only)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1)
    assert result.exit_code == 0
    onprem = result.groups["strategies"]["router_online"]["dimensions"]["placement"][
        "onprem_only"]
    assert onprem["n_cases"] == 1 and onprem["empty_cases"] == 1
    assert onprem["empty_set_mismatches"] == 0 and onprem["violations"] == 0
    assert onprem["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 0}
    assert onprem["metrics"]["empty_set_mismatch_rate"] == {"value": 0.0, "n": 1}


def test_a_stale_group_report_is_removed_by_an_unflagged_run(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    _run(suite, eps, out, group_report=True)
    assert (out / "groups.json").is_file()
    _run(suite, eps, out)
    assert not (out / "groups.json").exists() and not (out / "groups.md").exists()


@pytest.mark.parametrize("bad", [0, -3, True, 2.5])
def test_a_bad_minimum_is_a_load_error(tmp_path, bad, capsys):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, group_report=True, min_group_size=bad)
    assert result.exit_code == 1 and not out.exists()
    assert "min_group_size" in capsys.readouterr().err


def test_the_starter_suite_groups(tmp_path):
    doc = _run("eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml",
               tmp_path / "out", group_report=True).groups
    assert doc["group_sizes"]["placement"] == {
        "any": 8, "escalated": 2, "onprem_only": 4, "us_only": 8}
    assert doc["group_sizes"]["workload"] == {
        "claim_summary": 10, "faq": 8, "onprem_required": 4}
    assert sorted(doc["group_sizes"]["application"].values()) == [2, 4, 8, 8]
    for block in doc["strategies"].values():
        assert block["overall"]["violations"] == 0
        escalated = block["dimensions"]["placement"]["escalated"]
        assert escalated["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 2}


# --- CLI ------------------------------------------------------------------------

def test_cli_group_report_writes_both_files(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "cli-out"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--group-report", "--min-group-size", "2")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((out / "groups.json").read_text())
    assert doc["min_group_size"] == 2
    assert (out / "groups.md").read_text().startswith("# FeasibleRouteBench group report")


def test_cli_a_minimum_without_the_report_is_refused(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--min-group-size", "2")
    assert proc.returncode == 1 and "--min-group-size needs" in proc.stderr
    assert not out.exists()
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_group_report_run.py -p no:warnings -rf
```

Expected: `18 failed`. Sixteen fail with `TypeError: run_bench() got an unexpected keyword argument 'group_report'` or `AttributeError: 'RunResult' object has no attribute 'groups'`; the two CLI tests fail because argparse rejects the unknown flag (exit 2).

- [ ] **Step 3: Wire the report into the runner and the CLI**

Patch (save as a file and run `git apply <file>`):

````diff
diff --git a/app/eval/runner.py b/app/eval/runner.py
index 827c4e4..1bc1925 100644
--- a/app/eval/runner.py
+++ b/app/eval/runner.py
@@ -16,7 +16,9 @@ case:
    attempts on this case, after it was scored (spec §6).
 
 After the last case it computes the feasible-oracle metrics (`metrics.json`),
-a human report (`report.md`) and a run stamp (`run.json`). Exit codes: `0` ok,
+a human report (`report.md`) and a run stamp (`run.json`). With
+`group_report=True` it also writes `groups.json` and `groups.md`
+(`app/eval/groups.py`); without it, no output changes. Exit codes: `0` ok,
 `1` load/usage error (including a `seconds_per_case` too small for this run's
 endpoint count or attempt cap -- `strategies.min_seconds_per_case`), `2`
 policy violation, empty-set mismatch, post-gate widening, pinned-served
@@ -52,11 +54,18 @@ from app.eval.endpoints import (
     load_endpoints,
 )
 from app.eval.graders import grade
+from app.eval.groups import (
+    DEFAULT_MIN_GROUP_SIZE,
+    case_keys,
+    group_report as build_group_report,
+    render_group_report,
+)
 from app.eval.oracle import (
     CaseInfo,
     Choice,
     MatrixRow,
     attempt_outcome,
+    case_scores,
     grouped,
     headline,
     strategy_metrics,
@@ -171,6 +180,7 @@ class RunResult:
     exit_code: int
     out_dir: str
     metrics: dict
+    groups: dict | None = None    # the `groups.json` document, when asked for
 
 
 class _CountingBackend:
@@ -376,18 +386,29 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
                     seed: int = 0, live: bool = False, keep_text: bool = False,
                     variants_path: str | None = None, parity_tol: float = 0.02,
                     fixed: str | None = None,
-                    seconds_per_case: float = DEFAULT_SECONDS_PER_CASE) -> RunResult:
+                    seconds_per_case: float = DEFAULT_SECONDS_PER_CASE,
+                    group_report: bool = False,
+                    min_group_size: int = DEFAULT_MIN_GROUP_SIZE) -> RunResult:
     """Run the benchmark end to end. Load/usage errors print to stderr and
     return exit code 1 before `out_dir` is touched.
 
     `seconds_per_case` (> 0) is the synthetic case clock's spacing (B48 spec
-    §6, amended): case `k` is scored at `t_k = k * seconds_per_case`."""
+    §6, amended): case `k` is scored at `t_k = k * seconds_per_case`.
+
+    `group_report` also writes `groups.json` and `groups.md`: every metric
+    by placement, protection, application and workload, a figure over fewer
+    than `min_group_size` cases printed as a count and no rate."""
     args = {"suite": suite_path, "endpoints": endpoints_path, "seed": seed,
             "live": live, "keep_text": keep_text, "variants": variants_path,
             "parity_tol": parity_tol, "fixed": fixed,
             "seconds_per_case": seconds_per_case}
+    if group_report:   # only then: an unflagged run's `run.json` keeps its keys
+        args.update(group_report=True, min_group_size=min_group_size)
     started = _now()
     try:
+        if (isinstance(min_group_size, bool) or not isinstance(min_group_size, int)
+                or min_group_size < 1):
+            raise BenchError(f"min_group_size must be an int >= 1; got {min_group_size!r}")
         seconds_per_case = check_seconds_per_case(seconds_per_case)
         cases, suite_hash = load_suite(suite_path)
         base = load_settings()
@@ -442,6 +463,7 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
                 max_attempts=max_attempts, seconds_per_case=seconds_per_case,
                 out_dir=out_dir, seed=seed,
                 live=live, keep_text=keep_text, parity_tol=parity_tol, fixed=fixed,
+                group_report=group_report, min_group_size=min_group_size,
                 stamp={"suite_hash": suite_hash, "endpoints_hash": endpoints_hash,
                        "args": args, "started_at": started})
         except LeakageError as exc:
@@ -459,7 +481,7 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
 async def _run_loaded(*, cases, specs, settings, registry, detector,
                       variant_cfgs, max_attempts, seconds_per_case,
                       out_dir, seed, live, keep_text, parity_tol, fixed,
-                      stamp) -> RunResult:
+                      group_report, min_group_size, stamp) -> RunResult:
     inner = make_backend(settings, registry) if live else SimulatedBackend(specs, seed)
     backend = _CountingBackend(inner)
     gate = Gate(settings, detector, registry=registry)
@@ -520,7 +542,7 @@ async def _run_loaded(*, cases, specs, settings, registry, detector,
 
     out = Path(out_dir)
     out.mkdir(parents=True, exist_ok=True)
-    for stale in ("metrics.json", "report.md", "run.json"):
+    for stale in ("metrics.json", "report.md", "run.json", "groups.json", "groups.md"):
         (out / stale).unlink(missing_ok=True)
 
     case_infos: list[CaseInfo] = []
@@ -715,9 +737,19 @@ async def _run_loaded(*, cases, specs, settings, registry, detector,
         "attempt_cap": max_attempts,
         "notes": {**ARM_NOTES, "base_routing_policy_version": BASE_RPV_NOTE},
     }
+    groups_doc: dict | None = None
+    if group_report:
+        keys = {case.id: case_keys(case.tags, case.app_policy) for case in cases}
+        scores = {n: case_scores(case_infos, rows, choices, n) for n in strategy_names}
+        groups_doc = json.loads(_dump(build_group_report(
+            scores, keys, min_size=min_group_size, mode=run_stamp["mode"])))
+        (out / "groups.json").write_text(_dump(groups_doc), encoding="utf-8")
+        (out / "groups.md").write_text(
+            render_group_report(groups_doc, detail=HEADLINE_ROUTER), encoding="utf-8")
     (out / "run.json").write_text(_dump(run_stamp), encoding="utf-8")
     (out / "report.md").write_text(_report(metrics, run_stamp), encoding="utf-8")
-    return RunResult(exit_code=exit_code, out_dir=str(out), metrics=metrics)
+    return RunResult(exit_code=exit_code, out_dir=str(out), metrics=metrics,
+                     groups=groups_doc)
 
 
 def _served(attempts: tuple[str, ...], rows_by_endpoint: dict[str, MatrixRow]
diff --git a/scripts/feasible_bench.py b/scripts/feasible_bench.py
index caf66dc..8d6775d 100644
--- a/scripts/feasible_bench.py
+++ b/scripts/feasible_bench.py
@@ -13,7 +13,9 @@ first prints the call budget and asks for confirmation (`--yes` skips the
 prompt for non-interactive use).
 
 Writes `matrix.jsonl`, `choices.jsonl`, `metrics.json`, `report.md` and
-`run.json` into `--out` (default `results/<utc timestamp>`).
+`run.json` into `--out` (default `results/<utc timestamp>`). `--group-report`
+adds `groups.json` and `groups.md`: every metric by placement, protection,
+application and workload.
 
 Exit codes: 0 ok; 1 load/usage error (or a declined live run); 2 policy
 violation, empty-set mismatch, post-gate widening or pinned-served mismatch.
@@ -40,6 +42,7 @@ if str(REPO_ROOT) not in sys.path:
 from app.config.loader import load_settings  # noqa: E402
 from app.eval.cases import SuiteError, load_suite  # noqa: E402
 from app.eval.endpoints import EndpointFileError, load_endpoints  # noqa: E402
+from app.eval.groups import DEFAULT_MIN_GROUP_SIZE  # noqa: E402
 from app.eval.runner import run_bench  # noqa: E402
 from app.eval.strategies import DEFAULT_SECONDS_PER_CASE  # noqa: E402
 
@@ -70,6 +73,12 @@ def _parser() -> argparse.ArgumentParser:
                    help="synthetic case-clock spacing: case k is scored at "
                         "k x this many seconds; drives signal decay "
                         f"(default {DEFAULT_SECONDS_PER_CASE:g}; must be > 0)")
+    p.add_argument("--group-report", action="store_true",
+                   help="also write groups.json and groups.md: every metric by "
+                        "placement, protection, application and workload")
+    p.add_argument("--min-group-size", type=int, default=None,
+                   help="fewest cases behind a printed group rate "
+                        f"(default {DEFAULT_MIN_GROUP_SIZE}; needs --group-report)")
     return p
 
 
@@ -93,6 +102,11 @@ def _confirm_live(suite: str, endpoints: str, assume_yes: bool) -> bool:
 
 def main(argv: list[str] | None = None) -> int:
     args = _parser().parse_args(argv)
+    if args.min_group_size is not None and not args.group_report:
+        # A flag that would silently do nothing is a usage error.
+        print("feasible_bench: load error: --min-group-size needs --group-report",
+              file=sys.stderr)
+        return 1
     out = args.out or str(
         REPO_ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
 
@@ -109,7 +123,10 @@ def main(argv: list[str] | None = None) -> int:
         args.suite, args.endpoints, out_dir=out, seed=args.seed, live=args.live,
         keep_text=args.keep_text, variants_path=args.variants,
         parity_tol=args.parity_tol, fixed=args.fixed,
-        seconds_per_case=args.seconds_per_case))
+        seconds_per_case=args.seconds_per_case,
+        group_report=args.group_report,
+        min_group_size=(DEFAULT_MIN_GROUP_SIZE if args.min_group_size is None
+                        else args.min_group_size)))
     if result.exit_code != 1:
         print(f"feasible_bench: results in {result.out_dir} (exit {result.exit_code})")
     return result.exit_code
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_group_report_run.py tests/eval/test_bench_golden.py tests/eval/test_runner.py -p no:warnings -rf
```

Expected: `63 passed` (18 + 6 + 39). The golden test passing is E1 for this task: new keyword arguments with their defaults change no byte of an unflagged run.

- [ ] **Step 5: Look at one report**

```bash
SP_PII_DETECTION=regex LITELLM_LOCAL_MODEL_COST_MAP=True uv run python scripts/feasible_bench.py \
    --suite eval/suites/starter.jsonl --endpoints eval/endpoints/healthcare-sim.yaml \
    --seed 0 --group-report --out /tmp/p2_groups
sed -n '1,12p;/## Every group/,$p' /tmp/p2_groups/groups.md
```

Expected: exit 0. The file opens with five block-quoted caveats (the first begins "On the simulated backend, quality is an input"). In the `router_online` table, `placement | any` reads `0.75 (n = 8)` for the pass rate and `0.000106667 (n = 6)` for the cost regret; `placement | escalated` and `placement | onprem_only` read `insufficient cases (n = 2)` and `insufficient cases (n = 4)` in every figure column; every `violations` cell is `0`.

- [ ] **Step 6: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1404 passed`, no `FAILED` line.

- [ ] **Step 7: Commit**

```bash
git add app/eval/runner.py scripts/feasible_bench.py tests/eval/test_group_report_run.py
git commit -m "$(cat <<'EOF'
feat(eval): feasible_bench --group-report (P2 metrics)

Writes groups.json and groups.md beside the five existing files: every
strategy's metrics overall and by placement, protection, application and
workload. Opt-in; without the flag no output changes (the golden pin).
--min-group-size alone is a usage error.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The candidate routing overlay (spec §7.1; E11, E15, E16)

**Files:**
- Create: `app/eval/candidate.py`
- Create: `tests/eval/test_candidate.py`

**Interfaces:**
- Consumes: `app/config/schema.py::RoutingCfg`, `Settings`; `app/config/lint.py::lint_settings(s: Settings) -> list[str]`; `app/routing/strategy.py::build_strategy(name: str) -> RoutingStrategy` (raises `ValueError` on an unknown name); `app/eval/strategies.py::_REQUIRED_WEIGHT_KEYS`; `app/eval/endpoints.py::harness_settings(base, specs) -> Settings` (tests only).
- Produces, in `app/eval/candidate.py`:
  - `SCHEMA = "routeforge.router-candidate/v1"`; `ARM = "candidate"`.
  - `ALLOWED_KEYS = ("weights", "tier_quality", "complexity_thresholds", "risk_floors", "strategy", "signals", "breaker")`.
  - `NOT_EVALUABLE: dict[str, str]` — the ten refused `routing.yaml` keys, each with its reason. `set(ALLOWED_KEYS) | set(NOT_EVALUABLE)` equals the fields of `RoutingCfg` exactly (a test asserts it).
  - `CandidateError(ValueError)` with `.where` and `.message`; its `str` is `"<where>: <message>"`.
  - `Candidate` — frozen dataclass: `name: str | None`, `routing: dict`, `id: str`.
  - `candidate_id(routing: dict) -> str` — first 12 hex of sha256 over the overlay, sorted keys.
  - `load_candidate(path: str) -> Candidate` — raises `CandidateError`, or `OSError` when the file cannot be read.
  - `candidate_settings(harness: Settings, candidate: Candidate) -> Settings` — a deep copy; a mapping key merged one level, a scalar replaced; raises `CandidateError`.

  Task 7 imports `ARM`, `Candidate`, `candidate_settings`, `load_candidate`. `CandidateError` is a `ValueError`, which `run_bench`'s load block already turns into exit 1.

**Why the strategy name is checked here and not by the lint.** `lint_settings` does not check `routing.strategy`; `lint.enforce` does, beside checks of telemetry and secrets that have nothing to do with a candidate and would fail on the harness's settings. `build_strategy` is the registry lookup the `Router` itself makes.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_candidate.py`:

````python
"""The candidate routing overlay (`app/eval/candidate.py`; spec §7.1;
invariant E15: nothing a candidate states is silently ignored).
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.config.loader import load_settings
from app.eval.candidate import (
    ALLOWED_KEYS,
    ARM,
    NOT_EVALUABLE,
    SCHEMA,
    Candidate,
    CandidateError,
    candidate_id,
    candidate_settings,
    load_candidate,
)
from app.eval.endpoints import harness_settings

BASE = load_settings()
HARNESS = harness_settings(BASE, [])


def _write(tmp_path, doc) -> str:
    path = tmp_path / "candidate.yaml"
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc))
    return str(path)


def _doc(routing, **extra) -> dict:
    return {"schema": SCHEMA, "routing": routing, **extra}


def _cand(routing) -> Candidate:
    return Candidate(name=None, routing=routing, id=candidate_id(routing))


# --- loading --------------------------------------------------------------------

def test_a_candidate_loads_with_its_name_and_id(tmp_path):
    cand = load_candidate(_write(tmp_path, _doc({"weights": {"latency": 0.6}},
                                                name="latency-heavy")))
    assert cand.name == "latency-heavy"
    assert cand.routing == {"weights": {"latency": 0.6}}
    assert cand.id == candidate_id({"weights": {"latency": 0.6}}) and len(cand.id) == 12
    assert ARM == "candidate"


def test_the_id_is_of_the_overlay_not_of_the_files_formatting(tmp_path):
    a = load_candidate(_write(tmp_path, _doc(
        {"weights": {"latency": 0.6, "cost": 0.1}}, name="a")))
    b = load_candidate(_write(tmp_path, (
        f"# a comment\nname: b\nschema: {SCHEMA}\n"
        "routing:\n  weights:\n    cost: 0.1\n    latency: 0.6\n")))
    c = load_candidate(_write(tmp_path, _doc({"weights": {"latency": 0.7, "cost": 0.1}})))
    assert a.id == b.id != c.id


@pytest.mark.parametrize("doc, where", [
    ("- a\n- b\n", "must be a mapping"),
    ("{{{", "invalid YAML"),
    ({"schema": "routeforge.router-candidate/v2", "routing": {"strategy": "default"}},
     "schema"),
    ({"routing": {"strategy": "default"}}, "schema"),
    ({"schema": SCHEMA}, "routing"),
    ({"schema": SCHEMA, "routing": {}}, "routing"),
    ({"schema": SCHEMA, "routing": ["weights"]}, "routing"),
    ({"schema": SCHEMA, "routing": {"strategy": "default"}, "apply": True}, "unknown key"),
    ({"schema": SCHEMA, "routing": {"strategy": "default"}, "name": ""}, "name"),
    ({"schema": SCHEMA, "routing": {"strategy": "default"}, "name": 7}, "name"),
])
def test_e15_a_malformed_file_is_refused_naming_the_location(tmp_path, doc, where):
    with pytest.raises(CandidateError, match=where):
        load_candidate(_write(tmp_path, doc))


def test_a_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(OSError):
        load_candidate(str(tmp_path / "absent.yaml"))


@pytest.mark.parametrize("key", sorted(NOT_EVALUABLE))
def test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason(tmp_path, key):
    """Never ignored: a candidate that silently changed nothing would read
    as "no regression"."""
    with pytest.raises(CandidateError) as exc:
        load_candidate(_write(tmp_path, _doc({key: 1})))
    assert f"routing.{key}" in str(exc.value)
    assert "cannot be evaluated by this check" in str(exc.value)
    assert NOT_EVALUABLE[key] in str(exc.value)


def test_e15_an_unknown_routing_key_is_refused(tmp_path):
    with pytest.raises(CandidateError, match="routing.wieghts"):
        load_candidate(_write(tmp_path, _doc({"wieghts": {"latency": 1.0}})))


def test_the_allowed_and_refused_keys_cover_routing_yaml_exactly():
    """A new `routing.yaml` key must be put in one list or the other."""
    assert set(ALLOWED_KEYS) | set(NOT_EVALUABLE) == set(BASE.routing.model_dump())
    assert not set(ALLOWED_KEYS) & set(NOT_EVALUABLE)


# --- applying it in memory ------------------------------------------------------

def test_a_mapping_is_merged_one_level_and_a_scalar_replaces():
    before = copy.deepcopy(HARNESS.routing.model_dump())
    settings = candidate_settings(HARNESS, _cand({
        "weights": {"latency": 0.6},
        "complexity_thresholds": {"cheap_max": 0.25},
        "breaker": {"enabled": False},
        "strategy": "default"}))
    assert settings.routing.weights == {**before["weights"], "latency": 0.6}
    assert settings.routing.complexity_thresholds == {
        **before["complexity_thresholds"], "cheap_max": 0.25}
    assert settings.routing.breaker.enabled is False
    assert settings.routing.breaker.cooldown_s == before["breaker"]["cooldown_s"]
    # Everything the candidate did not state is the harness's.
    after = settings.routing.model_dump()
    for key in set(before) - {"weights", "complexity_thresholds", "breaker"}:
        assert after[key] == before[key], key


def test_the_harness_settings_are_never_mutated():
    before = copy.deepcopy(HARNESS.model_dump())
    candidate_settings(HARNESS, _cand({"weights": {"latency": 9.0},
                                       "signals": {"enabled": False}}))
    assert HARNESS.model_dump() == before


def test_restating_the_base_values_changes_nothing():
    routing = {"weights": dict(HARNESS.routing.weights),
               "tier_quality": dict(HARNESS.routing.tier_quality)}
    assert candidate_settings(HARNESS, _cand(routing)).model_dump() == HARNESS.model_dump()


@pytest.mark.parametrize("routing, where", [
    ({"weights": 3}, "routing.weights"),                       # a mapping key given a scalar
    ({"weights": {"latency": "fast"}}, "routing"),             # RoutingCfg validation
    ({"strategy": "no-such-strategy"}, "routing.strategy"),    # the strategy registry
    ({"risk_floors": {"high": "strong"}}, "config lint"),      # there is no `strong` tier
    ({"breaker": {"failure_threshold": "many"}}, "routing"),
])
def test_e15_a_candidate_the_deployment_could_not_boot_with_is_refused(routing, where):
    with pytest.raises(CandidateError, match=where):
        candidate_settings(HARNESS, _cand(routing))


def test_the_legacy_capacity_weight_is_renamed_like_at_boot():
    settings = candidate_settings(HARNESS, _cand({"weights": {"capacity": 0.9}}))
    assert "capacity" not in settings.routing.weights


def test_nothing_is_written_next_to_the_candidate_file(tmp_path):
    path = _write(tmp_path, _doc({"weights": {"latency": 0.6}}))
    before = (tmp_path / "candidate.yaml").read_bytes()
    candidate_settings(HARNESS, load_candidate(path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["candidate.yaml"]
    assert (tmp_path / "candidate.yaml").read_bytes() == before
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_candidate.py -p no:warnings -rf
```

Expected: `1 error` during collection — `ModuleNotFoundError: No module named 'app.eval.candidate'`.

- [ ] **Step 3: Write the module**

Create `app/eval/candidate.py`:

````python
"""A candidate router configuration for the offline router-version check
(P2 evaluation metrics, spec §7.1).

A candidate is a YAML file: the `routing.yaml` keys an operator is thinking
of changing, and nothing else. It is laid over the deployment's own routing
config IN MEMORY, for one bench run. Nothing here writes a config file,
calls an admin route, or is read by serving code: the file an operator
finally edits is `config/routing.yaml`, by hand, after reading the report.

    schema: routeforge.router-candidate/v1
    name: latency-heavy            # optional, a label for the report
    routing:
      weights: {latency: 0.6}      # merged over the base weights
      complexity_thresholds: {cheap_max: 0.25}

Only the keys in `ALLOWED_KEYS` are accepted. A key the harness cannot
evaluate is refused with the reason (`NOT_EVALUABLE`), never ignored: a
candidate that silently changed nothing would read as "no regression".

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config.lint import lint_settings
from app.config.schema import RoutingCfg, Settings
from app.eval.strategies import _REQUIRED_WEIGHT_KEYS
from app.routing.strategy import build_strategy

SCHEMA = "routeforge.router-candidate/v1"
#: The strategy-name suffix of the candidate's arms (`router_online:candidate`).
ARM = "candidate"

#: `routing.yaml` keys a candidate may state: the ones the bench's router
#: arms read when they choose.
ALLOWED_KEYS = ("weights", "tier_quality", "complexity_thresholds", "risk_floors",
                "strategy", "signals", "breaker")

#: Known `routing.yaml` keys a candidate may NOT state, and why.
NOT_EVALUABLE = {
    "confidence_escalation_threshold":
        "the bench sweep makes one call per case and endpoint with escalation off, "
        "so a change to it moves no bench figure",
    "confidence_logprob_temperature":
        "it shapes a live backend's confidence, which the bench does not score",
    "max_escalations": "the harness sets it to 0 for the sweep",
    "max_failover_attempts":
        "the attempt cap is read from the deployment's own config for every arm",
    "tiers": "the tier list is shared with the endpoint file and the policy config",
    "model_pool": "the endpoint file is the whole endpoint universe of a bench run",
    "score_on": "the bench always scores post-compression tokens",
    "fallback": "the bench does not model same-provider retries",
    "prompt_opt": "managed prompts are not part of a bench run",
    "quality_refresh_s": "it is a serving-process refresh interval",
}


class CandidateError(ValueError):
    """A candidate file failed to load or validate. Names the location."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class Candidate:
    """A loaded candidate. `routing` is the overlay exactly as stated; `id`
    is the first 12 hex of sha256 over it (sorted keys), so two files that
    state the same overlay have the same id whatever their formatting."""

    name: str | None
    routing: dict
    id: str


def candidate_id(routing: dict) -> str:
    blob = json.dumps(routing, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load_candidate(path: str) -> Candidate:
    """Read and validate a `routeforge.router-candidate/v1` file. Raises
    `CandidateError` naming the file and the offending key; `OSError` when
    the file cannot be read."""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CandidateError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise CandidateError(path, "must be a mapping")
    unknown = sorted(set(doc) - {"schema", "name", "routing"})
    if unknown:
        raise CandidateError(path, f"unknown key(s) {unknown}; "
                                   "expected schema, name, routing")
    if doc.get("schema") != SCHEMA:
        raise CandidateError(f"{path}: schema",
                             f"must be {SCHEMA!r}; got {doc.get('schema')!r}")
    name = doc.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise CandidateError(f"{path}: name", "must be a non-empty string when given")
    routing = doc.get("routing")
    if not isinstance(routing, dict) or not routing:
        raise CandidateError(f"{path}: routing",
                             "must be a non-empty mapping of routing.yaml keys")
    for key in sorted(routing, key=str):
        if key in NOT_EVALUABLE:
            raise CandidateError(f"{path}: routing.{key}",
                                 f"cannot be evaluated by this check: {NOT_EVALUABLE[key]}")
        if key not in ALLOWED_KEYS:
            raise CandidateError(f"{path}: routing.{key}",
                                 f"is not a key a candidate may state "
                                 f"(allowed: {list(ALLOWED_KEYS)})")
    return Candidate(name=name, routing=copy.deepcopy(routing), id=candidate_id(routing))


def candidate_settings(harness: Settings, candidate: Candidate) -> Settings:
    """A deep copy of `harness` (the bench's own settings, never mutated)
    with the candidate's routing keys laid over it.

    A mapping-valued key is merged ONE level deep over the base value, so
    `weights: {latency: 0.6}` keeps the other three weights; a scalar
    replaces. The result goes through the real `RoutingCfg` validation, the
    required-weights check the weight variants use, the strategy registry
    and the config lint: a candidate the deployment could not boot with is
    refused here.

    Raises `CandidateError`."""
    merged = harness.routing.model_dump()
    for key, value in candidate.routing.items():
        if isinstance(merged.get(key), dict):
            if not isinstance(value, dict):
                raise CandidateError(f"routing.{key}", "must be a mapping")
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    settings = copy.deepcopy(harness)
    try:
        settings.routing = RoutingCfg.model_validate(merged)
    except ValueError as exc:
        raise CandidateError("routing", str(exc)) from exc
    missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in settings.routing.weights]
    if missing:
        raise CandidateError("routing.weights", f"is missing required key(s) {missing}")
    try:
        # The boot check for this key is in `lint.enforce`, beside checks of
        # telemetry and secrets that are no business of a candidate; this is
        # the same registry lookup the Router does when it is built.
        build_strategy(settings.routing.strategy)
    except ValueError as exc:
        raise CandidateError("routing.strategy", str(exc)) from exc
    # Only what the overlay ADDED: a lint error the base config already has
    # is the deployment's, and the bench has always run despite it.
    before = set(lint_settings(harness))
    errors = [e for e in lint_settings(settings) if e not in before]
    if errors:
        raise CandidateError("routing", "config lint: " + "; ".join(errors))
    return settings
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_candidate.py tests/eval/test_isolation.py -p no:warnings -rf
```

Expected: `37 passed` (35 + 2).

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1439 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add app/eval/candidate.py tests/eval/test_candidate.py
git commit -m "$(cat <<'EOF'
feat(eval): a candidate routing overlay, applied in memory (P2 metrics)

app/eval/candidate.py reads a routeforge.router-candidate/v1 file and lays
its routing keys over the bench's settings for one run. Seven keys are
accepted; every other routing.yaml key is refused with the reason the bench
cannot evaluate it, so a candidate can never silently change nothing. It
writes nothing.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: The router-version check, pure (spec §7.3–§7.6, §8; E5, E7–E10, E13, E14, E17)

**Files:**
- Create: `app/eval/version_check.py`
- Create: `tests/eval/test_version_check.py`

**Interfaces:**
- Consumes: `app/eval/oracle.py::CaseScore`, `_SCORE_FIELD`, `_mean`, `_round_value` (Task 2); `app/eval/groups.py::DIMENSIONS`, `INSUFFICIENT`, `bench_caveats`, `partition` (Task 3); `app/eval/uncertainty.py::draw_indices(seed: int, replicate: int, app: str, n: int) -> list[int]` (the `app` argument is any label: here the scope), `interval_of(values: list[float], level: float) -> tuple[float, float]`, `MAX_REPLICATES = 20000`.
- Produces, in `app/eval/version_check.py`:
  - `SCHEMA = "routeforge.router-version-check/v1"`; `METHOD = "paired_case_bootstrap"`; `CHECK_METRICS` (eight names, `_SCORE_FIELD`'s order); `LOWER_IS_WORSE = ("pass_rate",)`; `EXIT_PASS, EXIT_HARD_FAIL, EXIT_REGRESSED, EXIT_INCONCLUSIVE = 0, 2, 3, 4`.
  - Fixed text: `CHECK_NO_EFFECT`, `CHECK_CONDITIONAL`, `CHECK_RANGE`, `CHECK_NO_TOLERANCE`; `check_caveats(mode: str) -> list[str]`.
  - `CheckPlan` — frozen dataclass: `tolerances: dict[str, float] = {}`, `replicates: int = 1000`, `interval: float = 0.90`, `seed: int = 0`, `min_group_size: int = 5`; validates in `__post_init__` (`ValueError`).
  - `parse_tolerances(items: list[str]) -> dict[str, float]` — `["pass_rate=0.02"]` → `{"pass_rate": 0.02}`; `ValueError` on a malformed item or a repeated metric.
  - `worsening(metric, baseline, candidate) -> float | None`; `decide(w, hi, tol) -> str`; `change_of(identical, lo, hi) -> str`.
  - `compare(baseline: list[CaseScore], candidate: list[CaseScore], scope: str, plan: CheckPlan) -> dict[str, dict]` — one figure per metric: `baseline`, `candidate`, `support`, `tolerance`, `worsening`, `lo`, `hi`, `change`, `state`.
  - `verdict_of(hard_failed: bool, states: list[str], tolerances_named: bool) -> str`.
  - `run_check(baseline, candidate, keys, chains, plan, *, arms: tuple[str, str], candidate_doc: dict, run_exit_code: int, mode: str) -> dict` — the `check.json` document, rounded. `chains` is `(baseline, candidate)`, each `{case_id: attempt chain}`.
  - `render_check(doc: dict) -> str`.

  Task 7 imports `CheckPlan`, `render_check`, `run_check` in the runner and `CHECK_METRICS`, `CheckPlan`, `parse_tolerances` in the CLI.

**The decision rule**, for worsening `w`, range upper end `hi`, tolerance `tol` — the first row that holds decides:

| # | Condition | State |
|---|---|---|
| 1 | `w` is undefined | `undefined` |
| 2 | `w > tol` | `regressed` |
| 3 | there is no range, or `hi > tol` | `inconclusive` |
| 4 | otherwise | `within_tolerance` |

Comparisons round to 12 decimals first. `lo` is not an input.

**Two hand-computed examples** (spec §12.2–§12.3; the test module's docstring repeats them).

- *A constant shift.* Ten cases; the candidate costs exactly 0.25 more on each. Any resample's paired difference of means is 0.25: worsening 0.25, range `[0.25, 0.25]`, `change: worse`. Tolerance 0.25 → `within_tolerance` (inclusive). Tolerance 0.2 → `regressed`.
- *Regressed and not detectable at once.* Six cases; baseline cost 1 each; the candidate costs 10 on one and 0 on five. Worsening `10/6 − 1 = 0.667`. A resample misses the dear case with probability `(5/6)^6 = 0.335` and then differs by −1, so the 90% range starts at −1 and includes 0. Tolerance 0.5 → `regressed`, with `change: not_detectable`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_version_check.py`:

````python
"""The offline router-version check (`app/eval/version_check.py`; spec §7;
invariants E7-E10, E13, E14, E17). Pure: every test builds `CaseScore`s by
hand; the end-to-end tests are in `test_version_check_run.py`.

Two examples are computed by hand.

**A constant shift.** Ten cases; the candidate costs exactly 0.25 more on
every case. Whatever cases a resample draws, the paired difference of the two
means is 0.25, so the range is `[0.25, 0.25]`: worsening 0.25, `change`
"worse". Against a tolerance of 0.25 that is within tolerance (the boundary
is inclusive); against 0.2 it is regressed.

**Six cases, one of them much dearer.** Baseline cost 1 on every case; the
candidate costs 10 on `k1` and 0 on the other five. Worsening of the mean
cost: `(10 + 0x5)/6 - 1 = 4/6`. A resample of six cases misses `k1` with
probability `(5/6)^6 = 0.335`, and then the difference is -1: the 90% range
starts at -1 and includes 0. So the figure is `regressed` against a
tolerance of 0.5 (the point estimate decides) AND `change` is
"not_detectable" (the range includes 0). Neither overrides the other.
"""
from __future__ import annotations

import json
import random

import pytest

from app.eval import version_check
from app.eval.groups import (CAVEAT_SIMULATED, CAVEAT_SYNTHETIC, CAVEAT_TIER_RULE,
                             CAVEAT_TO_MEASURE, INSUFFICIENT)
from app.eval.oracle import CaseScore
from app.eval.version_check import (
    CHECK_CONDITIONAL,
    CHECK_METRICS,
    CHECK_NO_EFFECT,
    CHECK_NO_TOLERANCE,
    CHECK_RANGE,
    CheckPlan,
    change_of,
    compare,
    decide,
    parse_tolerances,
    render_check,
    run_check,
    verdict_of,
    worsening,
)

ARMS = ("router_online", "router_online:candidate")
CAND = {"id": "abc123abc123", "name": "t", "routing": {"weights": {"latency": 0.6}}}


def _score(case_id, *, passed=1.0, cost=1.0, violation=False, empty=False) -> CaseScore:
    if empty:
        return CaseScore(case_id, True, violation, False, False, None, None, None, None,
                         None, None, None, None)
    return CaseScore(case_id, False, violation, False, False, passed, 1.0 - passed,
                     1.0 - passed, 0.0 if passed else None, 0.0 if passed else None,
                     cost, 0.0, 0.0)


def _ids(n, prefix="k") -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


def _keys(ids, workload=lambda i: "w") -> dict:
    return {i: {"placement": "any", "protection": "enforced", "application": "app",
                "workload": workload(i)} for i in ids}


def _check(base, cand, *, tolerances=None, keys=None, run_exit_code=0, chains=None,
           **plan) -> dict:
    ids = [s.case_id for s in base]
    return run_check(base, cand, keys or _keys(ids), chains or ({}, {}),
                     CheckPlan(tolerances=tolerances or {}, **plan), arms=ARMS,
                     candidate_doc=CAND, run_exit_code=run_exit_code, mode="simulated")


# --- the decision rule (E10, E17) ----------------------------------------------

def test_e10_worsening_is_signed_so_that_positive_is_worse():
    assert worsening("mean_chosen_cost", 1.0, 1.5) == 0.5      # dearer
    assert worsening("latency_regret", 30.0, 10.0) == -20.0    # faster
    assert worsening("pass_rate", 0.9, 0.8) == pytest.approx(0.1)   # fewer passes
    assert worsening("pass_rate", 0.8, 0.9) == pytest.approx(-0.1)
    assert worsening("cost_regret", None, 1.0) is None
    assert worsening("cost_regret", 1.0, None) is None


@pytest.mark.parametrize("w, hi, tol, state", [
    (None, None, 0.1, "undefined"),
    (0.2, 0.3, 0.1, "regressed"),
    (0.2, None, 0.1, "regressed"),          # the point decides, range or no range
    (0.05, 0.2, 0.1, "inconclusive"),       # the point is inside, the range is not
    (0.05, None, 0.1, "inconclusive"),      # no range: nothing shows it is inside
    (0.05, 0.1, 0.1, "within_tolerance"),   # the boundary is inclusive
    (0.1, 0.1, 0.1, "within_tolerance"),
    (-0.3, -0.1, 0.0, "within_tolerance"),  # an improvement
    (0.0, 0.0, 0.0, "within_tolerance"),
])
def test_e10_the_decision_rule_branch_by_branch(w, hi, tol, state):
    assert decide(w, hi, tol) == state


def test_e10_the_tolerance_boundary_is_not_decided_by_float_noise():
    w = 0.92 - 0.90
    assert w > 0.02                                   # 0.020000000000000018
    assert decide(w, w, 0.02) == "within_tolerance"


def test_e17_the_lower_end_of_a_range_never_enters_the_decision():
    """`decide` does not take `lo` at all; `change_of` does not take a
    tolerance. A range that includes 0 therefore decides nothing by itself."""
    assert change_of(False, -0.1, 0.3) == "not_detectable"
    assert change_of(False, 0.0, 0.3) == "not_detectable"
    assert change_of(False, 0.1, 0.3) == "worse"
    assert change_of(False, -0.3, -0.1) == "better"
    assert change_of(False, None, None) == "undefined"
    assert change_of(True, 0.0, 0.0) == "none"


def test_e10_a_constant_shift_has_a_degenerate_range_by_hand():
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, cost=1.25) for i in _ids(10)]
    plan = CheckPlan(tolerances={"mean_chosen_cost": 0.25})
    f = compare(base, cand, "overall", plan)["mean_chosen_cost"]
    assert (f["baseline"], f["candidate"]) == (1.0, 1.25)
    assert (f["worsening"], f["lo"], f["hi"]) == (0.25, 0.25, 0.25)
    assert f["change"] == "worse" and f["state"] == "within_tolerance"
    assert f["support"] == {"baseline": 10, "candidate": 10}
    tighter = CheckPlan(tolerances={"mean_chosen_cost": 0.2})
    assert compare(base, cand, "overall", tighter)["mean_chosen_cost"]["state"] == "regressed"


def test_e17_a_regression_whose_range_includes_zero_is_both_things():
    base = [_score(i) for i in _ids(6)]
    cand = [_score(i, cost=10.0 if i == "k01" else 0.0) for i in _ids(6)]
    plan = CheckPlan(tolerances={"mean_chosen_cost": 0.5})
    f = compare(base, cand, "overall", plan)["mean_chosen_cost"]
    assert f["worsening"] == pytest.approx(4 / 6)
    assert f["lo"] == -1.0 and f["hi"] > 0.5
    assert f["change"] == "not_detectable" and f["state"] == "regressed"
    assert _check(base, cand, tolerances={"mean_chosen_cost": 0.5})["verdict"] == "regressed"


def test_every_range_value_is_a_difference_some_resample_can_have():
    """Two cases, per-case cost difference 0 and 1: a resample's difference
    is 0, 0.5 or 1 and nothing else."""
    base = [_score("a"), _score("b")]
    cand = [_score("a"), _score("b", cost=2.0)]
    f = compare(base, cand, "overall", CheckPlan(min_group_size=1))["mean_chosen_cost"]
    assert f["worsening"] == 0.5
    assert {f["lo"], f["hi"]} <= {0.0, 0.5, 1.0} and f["lo"] <= 0.5 <= f["hi"]


def test_the_range_widens_with_the_level():
    base = [_score(i) for i in _ids(12)]
    cand = [_score(i, cost=1.0 + (n % 4)) for n, i in enumerate(_ids(12))]
    narrow = compare(base, cand, "overall", CheckPlan(interval=0.5))["mean_chosen_cost"]
    wide = compare(base, cand, "overall", CheckPlan(interval=0.95))["mean_chosen_cost"]
    assert wide["lo"] <= narrow["lo"] <= narrow["worsening"] <= narrow["hi"] <= wide["hi"]
    assert wide["lo"] < wide["hi"]


def test_the_draw_is_paired_and_seeded():
    base = [_score(i) for i in _ids(12)]
    cand = [_score(i, cost=1.0 + (n % 4)) for n, i in enumerate(_ids(12))]
    one = compare(base, cand, "overall", CheckPlan(seed=1))
    assert one == compare(base, cand, "overall", CheckPlan(seed=1))
    two = compare(base, cand, "overall", CheckPlan(seed=2))
    assert (one["mean_chosen_cost"]["lo"], one["mean_chosen_cost"]["hi"]) != (
        two["mean_chosen_cost"]["lo"], two["mean_chosen_cost"]["hi"])
    # A metric both arms agree on case by case has no spread at all: the two
    # arms were drawn together, not each on its own.
    assert (one["pass_rate"]["lo"], one["pass_rate"]["hi"]) == (0.0, 0.0)
    assert one["pass_rate"]["change"] == "none"


def test_a_figure_undefined_in_some_resample_has_no_range_and_cannot_pass():
    """Cost regret is over the passing cases. With one passing case in five,
    a third of the resamples draw none: the figure is undefined there, so it
    has no range, and a figure with no range is never within tolerance."""
    ids = _ids(5)
    base = [_score(i, passed=float(i == "k01")) for i in ids]
    plan = CheckPlan(tolerances={"cost_regret": 1.0}, min_group_size=1)
    f = compare(base, list(base), "overall", plan)["cost_regret"]
    assert f["support"] == {"baseline": 1, "candidate": 1}
    assert (f["worsening"], f["lo"], f["hi"]) == (0.0, None, None)
    assert f["state"] == "inconclusive"
    # Every case agrees between the arms, and `change` still says so.
    assert f["change"] == "none"


# --- identical arms (E8) -------------------------------------------------------

def test_e8_identical_arms_are_no_change_everywhere():
    scores = [_score(i, passed=float(n % 3 != 0), cost=1.0 + n)
              for n, i in enumerate(_ids(12))]
    tolerances = {m: 0.0 for m in CHECK_METRICS}
    doc = _check(scores, list(scores), tolerances=tolerances)
    assert doc["verdict"] == "pass" and doc["exit_code"] == 0
    assert doc["regressed"] == [] and doc["inconclusive"] == []
    for metric, f in doc["overall"].items():
        assert f["baseline"] == f["candidate"], metric
        assert (f["worsening"], f["lo"], f["hi"]) == (0.0, 0.0, 0.0), metric
        assert f["change"] == "none" and f["state"] == "within_tolerance", metric


# --- case order (E9) -----------------------------------------------------------

def test_e9_the_document_does_not_depend_on_case_order():
    ids = _ids(14)
    base = [_score(i, passed=float(n % 4 != 0), cost=1.0 + n % 3)
            for n, i in enumerate(ids)]
    cand = [_score(i, passed=float(n % 5 != 0), cost=1.0 + n % 2)
            for n, i in enumerate(ids)]
    keys = _keys(ids, workload=lambda i: "a" if int(i[1:]) <= 7 else "b")
    tolerances = {"pass_rate": 0.05, "mean_chosen_cost": 0.1}
    first = _check(base, cand, tolerances=tolerances, keys=keys)
    rng = random.Random(5)
    for _ in range(3):
        b2, c2 = list(base), list(cand)
        rng.shuffle(b2)
        rng.shuffle(c2)                    # the two arms in DIFFERENT orders
        k2 = dict(sorted(keys.items(), key=lambda kv: rng.random()))
        assert _check(b2, c2, tolerances=tolerances, keys=k2) == first


def test_arms_that_do_not_cover_the_same_cases_are_refused():
    with pytest.raises(ValueError, match="same cases"):
        compare([_score("a"), _score("b")], [_score("a"), _score("c")], "overall",
                CheckPlan())


# --- hard checks (E7) ----------------------------------------------------------

@pytest.mark.parametrize("tolerances", [
    {}, {m: 1e9 for m in CHECK_METRICS}, {"pass_rate": 0.0}])
def test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances(tolerances):
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, violation=(i == "k03")) for i in _ids(10)]
    doc = _check(base, cand, tolerances=tolerances)
    assert doc["verdict"] == "hard_fail" and doc["exit_code"] == 2
    assert doc["hard"] == {"failed": True, "candidate_violations": 1,
                           "candidate_served_on_empty": 0, "run_exit_code": 0}


def test_e7_an_attempt_on_an_empty_feasible_set_is_counted_as_such():
    base = [*(_score(i) for i in _ids(9)), _score("z", empty=True)]
    cand = [*(_score(i) for i in _ids(9)), _score("z", empty=True, violation=True)]
    doc = _check(base, cand, tolerances={"pass_rate": 1.0})
    assert doc["verdict"] == "hard_fail"
    assert doc["hard"]["candidate_violations"] == 1
    assert doc["hard"]["candidate_served_on_empty"] == 1


def test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate():
    scores = [_score(i) for i in _ids(10)]
    doc = _check(scores, list(scores), tolerances={"pass_rate": 1.0}, run_exit_code=2)
    assert doc["verdict"] == "hard_fail" and doc["exit_code"] == 2
    assert doc["hard"]["candidate_violations"] == 0 and doc["hard"]["run_exit_code"] == 2


def test_a_baseline_violation_alone_does_not_fail_the_candidate():
    """The baseline arm's own violation reaches the check through the run's
    exit code (the bench exits 2 for any strategy), not through this count."""
    base = [_score(i, violation=(i == "k01")) for i in _ids(10)]
    cand = [_score(i) for i in _ids(10)]
    assert _check(base, cand)["hard"]["failed"] is False


# --- verdicts -------------------------------------------------------------------

@pytest.mark.parametrize("hard, states, named, verdict", [
    (True, ["regressed"], True, "hard_fail"),
    (False, ["within_tolerance", "regressed", "inconclusive"], True, "regressed"),
    (False, ["within_tolerance", "inconclusive"], True, "inconclusive"),
    (False, ["within_tolerance", "undefined"], True, "inconclusive"),
    (False, ["within_tolerance", INSUFFICIENT], True, "inconclusive"),
    (False, ["within_tolerance"], True, "pass"),
    (False, [], False, "no_hard_failure"),
])
def test_the_verdict_is_the_worst_state(hard, states, named, verdict):
    assert verdict_of(hard, states, named) == verdict


def test_the_exit_codes():
    assert (version_check.EXIT_PASS, version_check.EXIT_HARD_FAIL,
            version_check.EXIT_REGRESSED, version_check.EXIT_INCONCLUSIVE) == (0, 2, 3, 4)
    assert version_check._EXIT == {"pass": 0, "no_hard_failure": 0, "hard_fail": 2,
                                   "regressed": 3, "inconclusive": 4}


def test_no_tolerance_named_judges_nothing_and_says_so():
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, cost=99.0) for i in _ids(10)]
    doc = _check(base, cand)
    assert doc["verdict"] == "no_hard_failure" and doc["exit_code"] == 0
    assert all(f["state"] == "not_checked" for f in doc["overall"].values())
    assert doc["caveats"][-1] == CHECK_NO_TOLERANCE
    assert doc["overall"]["mean_chosen_cost"]["worsening"] == 98.0     # shown, not judged
    assert "NO HARD FAILURE" in render_check(doc)


def test_an_inconclusive_figure_is_not_a_pass():
    """k01 costs 10 more; the mean worsens by 1.0 <= 1.5, but a resample
    that draws k01 twice is at 2.0 or more: the range reaches past it."""
    base = [_score(i) for i in _ids(10)]
    cand = [_score(i, cost=11.0 if i == "k01" else 1.0) for i in _ids(10)]
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 1.5})
    f = doc["overall"]["mean_chosen_cost"]
    assert f["worsening"] == 1.0 and f["hi"] > 1.5 and f["state"] == "inconclusive"
    assert doc["verdict"] == "inconclusive" and doc["exit_code"] == 4
    assert {"scope": "overall", "metric": "mean_chosen_cost",
            "state": "inconclusive"} in doc["inconclusive"]


# --- groups ---------------------------------------------------------------------

def _two_groups():
    ids = _ids(10)
    in_a = lambda i: int(i[1:]) <= 5                               # noqa: E731
    base = [_score(i) for i in ids]
    cand = [_score(i, cost=2.0 if in_a(i) else 0.0) for i in ids]
    return base, cand, _keys(ids, workload=lambda i: "a" if in_a(i) else "b")


def test_a_regression_in_one_group_fails_when_overall_is_unchanged():
    """Group a costs 1 more on every case, group b 1 less: the overall mean
    cost is unchanged; group a's is worse by exactly 1."""
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys)
    assert doc["overall"]["mean_chosen_cost"]["worsening"] == 0.0
    assert doc["overall"]["mean_chosen_cost"]["state"] == "inconclusive"
    a = doc["groups"]["workload"]["a"]["metrics"]["mean_chosen_cost"]
    assert (a["worsening"], a["lo"], a["hi"], a["state"]) == (1.0, 1.0, 1.0, "regressed")
    b = doc["groups"]["workload"]["b"]["metrics"]["mean_chosen_cost"]
    assert (b["worsening"], b["state"]) == (-1.0, "within_tolerance")
    assert doc["verdict"] == "regressed" and doc["exit_code"] == 3
    assert doc["regressed"] == [{"scope": "workload=a", "metric": "mean_chosen_cost",
                                 "state": "regressed"}]


def test_e5_a_group_below_the_minimum_is_listed_and_not_judged():
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys,
                 min_group_size=6)
    assert doc["groups"]["workload"]["a"] == {"n_cases": 5, "state": INSUFFICIENT}
    assert doc["regressed"] == []                       # group a was not judged
    assert doc["verdict"] == "inconclusive"             # the overall figure still is
    assert "| workload=a | all | n/a | n/a | insufficient cases (n = 5) |" in (
        render_check(doc))


def test_e5_a_thin_figure_is_skipped_in_a_group_and_inconclusive_overall():
    """Cost regret is over the passing cases only. Ten cases, four passing:
    below a minimum of five the figure has no rate. In a group that is
    "not judged"; overall, a named metric that cannot be judged cannot pass."""
    ids = _ids(10)
    base = [_score(i, passed=float(n < 4)) for n, i in enumerate(ids)]
    doc = _check(base, list(base), tolerances={"cost_regret": 1.0})
    f = doc["overall"]["cost_regret"]
    assert f["state"] == INSUFFICIENT and f["support"] == {"baseline": 4, "candidate": 4}
    assert f["worsening"] is None and f["lo"] is None
    assert f["baseline"] is None and f["candidate"] is None      # no figure at all
    assert doc["verdict"] == "inconclusive"
    assert doc["inconclusive"] == [{"scope": "overall", "metric": "cost_regret",
                                    "state": INSUFFICIENT}]
    # The same thin figure inside each (sufficiently large) group is skipped.
    assert doc["groups"]["workload"]["w"]["metrics"]["cost_regret"]["state"] == INSUFFICIENT


def test_changed_counts_the_cases_whose_attempt_chain_differs():
    scores = [_score(i) for i in _ids(5)]
    chains = ({"k01": ("a",), "k02": ("a", "b"), "k03": ("a",), "k04": (), "k05": ("b",)},
              {"k01": ("a",), "k02": ("a",), "k03": ("b",), "k04": (), "k05": ("b",)})
    doc = _check(scores, list(scores), chains=chains)
    assert doc["changed"] == {"n_cases": 5, "choice_changed": 2, "share": 0.4}


# --- inputs ---------------------------------------------------------------------

def test_tolerances_parse_from_metric_equals_value():
    assert parse_tolerances(["pass_rate=0.02", " cost_regret = 1e-4"]) == {
        "pass_rate": 0.02, "cost_regret": 1e-4}
    assert parse_tolerances([]) == {}


@pytest.mark.parametrize("items, message", [
    (["pass_rate"], "METRIC=VALUE"),
    (["=0.1"], "METRIC=VALUE"),
    (["pass_rate=lots"], "not a number"),
    (["pass_rate=0.1", "pass_rate=0.2"], "given twice"),
])
def test_a_malformed_tolerance_is_refused(items, message):
    with pytest.raises(ValueError, match=message):
        parse_tolerances(items)


@pytest.mark.parametrize("kwargs, message", [
    ({"tolerances": {"accuracy": 0.1}}, "unknown metric"),
    ({"tolerances": {"pass_rate": -0.1}}, "finite number >= 0"),
    ({"tolerances": {"pass_rate": float("nan")}}, "finite number >= 0"),
    ({"tolerances": {"pass_rate": float("inf")}}, "finite number >= 0"),
    ({"tolerances": {"pass_rate": True}}, "finite number >= 0"),
    ({"replicates": 0}, "replicates"),
    ({"replicates": 20001}, "replicates"),
    ({"interval": 1.0}, "interval"),
    ({"interval": 0}, "interval"),
    ({"seed": -1}, "seed"),
    ({"min_group_size": 0}, "min_group_size"),
])
def test_a_bad_plan_is_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        CheckPlan(**kwargs)


# --- the fixed text (E13, E14) -------------------------------------------------

def test_e14_the_check_text_is_the_spec_text():
    assert CHECK_NO_EFFECT == (
        "This check applies nothing and promotes nothing. It reads a candidate routing "
        "config, scores it offline beside the current one, and writes this report. A "
        "routing change is still made by an operator editing config/routing.yaml.")
    assert CHECK_CONDITIONAL == (
        "The verdict is conditional on this suite, this endpoint file, this simulation "
        "seed and this case order. It is not a statement about live traffic.")
    assert CHECK_RANGE == (
        "A range is the spread of the paired difference when the suite's cases are "
        "resampled. It says how much the difference depends on which cases are in the "
        "suite, and nothing about whether the suite resembles real traffic.")
    assert CHECK_NO_TOLERANCE == (
        "No tolerance was named, so only the hard checks were applied: the differences "
        "below are shown, not judged.")


def test_e14_every_report_opens_with_the_caveats_in_order():
    scores = [_score(i) for i in _ids(10)]
    doc = _check(scores, list(scores), tolerances={"pass_rate": 0.0})
    assert doc["caveats"] == [CHECK_NO_EFFECT, CAVEAT_SIMULATED, CAVEAT_TIER_RULE,
                              CAVEAT_SYNTHETIC, CAVEAT_TO_MEASURE, CHECK_CONDITIONAL,
                              CHECK_RANGE]
    lines = render_check(doc).splitlines()
    assert lines[0] == "# Router-version check (offline)"
    assert lines[2:9] == [f"> {text}" for text in doc["caveats"]]
    assert doc["effect"] == "none"
    assert "**PASS** (exit 0)" in render_check(doc)


_FORBIDDEN = ("rank", "recommend", "best", "winner", "prefer", "should", "relax",
              "vendor", "deploy", "activate", "rollout")


def _keys_of(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys_of(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys_of(v)}
    return set()


def test_e13_no_key_or_sentence_ranks_recommends_or_promotes():
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys)
    names = " ".join(sorted(_keys_of(doc))).lower()
    text = render_check(doc).lower()
    for word in _FORBIDDEN:
        assert word not in names, word
        assert word not in text, word
    # "promote" appears exactly once in a report: in the sentence that says
    # this check does not do it. No key says it at all.
    for word in ("promot", "apply", "applied"):
        assert word not in names, word
    assert text.count("promot") == 1 and "promotes nothing" in text


def test_the_document_is_json_clean_and_rounded():
    base, cand, keys = _two_groups()
    doc = _check(base, cand, tolerances={"mean_chosen_cost": 0.5}, keys=keys)
    assert json.loads(json.dumps(doc, sort_keys=True)) == doc
    assert doc["schema"] == "routeforge.router-version-check/v1"
    assert doc["plan"] == {"method": "paired_case_bootstrap",
                           "tolerances": {"mean_chosen_cost": 0.5}, "replicates": 1000,
                           "interval": 0.9, "seed": 0, "min_group_size": 5}
    assert doc["arms"] == {"baseline": ARMS[0], "candidate": ARMS[1]}
    assert doc["candidate"] == CAND
    assert "None" not in render_check(doc)
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_version_check.py -p no:warnings -rf
```

Expected: `1 error` during collection — `ImportError: cannot import name 'version_check' from 'app.eval'`.

- [ ] **Step 3: Write the module**

Create `app/eval/version_check.py`:

````python
"""Offline router-version check (P2 evaluation metrics, spec §7).

One bench run scores two router arms over the SAME sweep matrix: the
deployment's routing config (`router_online`) and that config with a
candidate overlay (`router_online:candidate`, `app/eval/candidate.py`). The
two arms see the same cases, the same eligible sets, the same seed and the
same per-endpoint outcomes, so every difference between them is the
candidate's.

**It applies nothing.** The result is two files and an exit code. There is
no admin call, no config write and no hook into serving: the handoff's
"promotion gate" is, here, a check an operator runs before THEY edit
`config/routing.yaml`. RouteForge has no in-process promotion path
(CLAUDE.md rule 4) and this module is not one.

**The decision rule** (`decide`, spec §7.4). `w` is the candidate's
worsening of a metric (positive = worse), `[lo, hi]` its paired-bootstrap
range, `tol` the operator's tolerance for that metric:

1. `w` undefined                -> `undefined`
2. `w > tol`                    -> `regressed`  (whatever the range says)
3. no range, or `hi > tol`      -> `inconclusive`
4. otherwise                    -> `within_tolerance`

A range that includes 0 decides nothing on its own: it is reported as
`change: "not_detectable"` beside whichever state the rule gave.

**Verdict and exit code** (`verdict_of`): `hard_fail` 2 (the candidate made
an ineligible choice or served a case with an empty feasible set, or the run
itself failed an integrity check -- no tolerance applies); `regressed` 3;
`inconclusive` 4; `pass` 0; `no_hard_failure` 0 when no tolerance was named.

Pure: no pipeline, registry, settings or clock. Never imported by serving
code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.eval.groups import DIMENSIONS, INSUFFICIENT, bench_caveats, partition
from app.eval.oracle import _SCORE_FIELD, CaseScore, _mean, _round_value
from app.eval.uncertainty import MAX_REPLICATES, draw_indices, interval_of

SCHEMA = "routeforge.router-version-check/v1"
METHOD = "paired_case_bootstrap"

#: The metrics a tolerance may be named for, in report order.
CHECK_METRICS = tuple(_SCORE_FIELD)
#: `pass_rate` is the one metric where a LOWER value is the worse one.
LOWER_IS_WORSE = ("pass_rate",)

EXIT_PASS, EXIT_HARD_FAIL, EXIT_REGRESSED, EXIT_INCONCLUSIVE = 0, 2, 3, 4
_EXIT = {"pass": EXIT_PASS, "no_hard_failure": EXIT_PASS, "hard_fail": EXIT_HARD_FAIL,
         "regressed": EXIT_REGRESSED, "inconclusive": EXIT_INCONCLUSIVE}

# Tolerance comparisons round to 12 dp first, like `oracle.headline`'s parity
# check: `0.92 - 0.90` is `0.020000000000000018` and must not exceed 0.02.
_DP = 12

# --- fixed text (spec §8; pinned by tests/eval/test_version_check.py) ----------

CHECK_NO_EFFECT = (
    "This check applies nothing and promotes nothing. It reads a candidate routing "
    "config, scores it offline beside the current one, and writes this report. A "
    "routing change is still made by an operator editing config/routing.yaml.")
CHECK_CONDITIONAL = (
    "The verdict is conditional on this suite, this endpoint file, this simulation "
    "seed and this case order. It is not a statement about live traffic.")
CHECK_RANGE = (
    "A range is the spread of the paired difference when the suite's cases are "
    "resampled. It says how much the difference depends on which cases are in the "
    "suite, and nothing about whether the suite resembles real traffic.")
CHECK_NO_TOLERANCE = (
    "No tolerance was named, so only the hard checks were applied: the differences "
    "below are shown, not judged.")


def check_caveats(mode: str) -> list[str]:
    return [CHECK_NO_EFFECT, *bench_caveats(mode), CHECK_CONDITIONAL, CHECK_RANGE]


@dataclass(frozen=True)
class CheckPlan:
    """What `--candidate` asked for. The defaults are the CLI's."""

    tolerances: dict[str, float] = field(default_factory=dict)
    replicates: int = 1000
    interval: float = 0.90
    seed: int = 0
    min_group_size: int = 5

    def __post_init__(self) -> None:
        for metric, tol in self.tolerances.items():
            if metric not in CHECK_METRICS:
                raise ValueError(f"tolerance for unknown metric {metric!r} "
                                 f"(metrics: {list(CHECK_METRICS)})")
            if (isinstance(tol, bool) or not isinstance(tol, (int, float))
                    or not 0.0 <= tol < float("inf")):
                raise ValueError(f"tolerance for {metric} must be a finite number "
                                 f">= 0; got {tol!r}")
        if (isinstance(self.replicates, bool) or not isinstance(self.replicates, int)
                or not 1 <= self.replicates <= MAX_REPLICATES):
            raise ValueError(f"replicates must be an int from 1 to {MAX_REPLICATES}; "
                             f"got {self.replicates!r}")
        if (isinstance(self.interval, bool) or not isinstance(self.interval, (int, float))
                or not 0.0 < self.interval < 1.0):
            raise ValueError(f"interval must be a number strictly between 0 and 1; "
                             f"got {self.interval!r}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError(f"seed must be an int >= 0; got {self.seed!r}")
        if (isinstance(self.min_group_size, bool)
                or not isinstance(self.min_group_size, int) or self.min_group_size < 1):
            raise ValueError(f"min_group_size must be an int >= 1; "
                             f"got {self.min_group_size!r}")


def parse_tolerances(items: list[str]) -> dict[str, float]:
    """`["pass_rate=0.02", ...]` -> `{"pass_rate": 0.02}`. Raises `ValueError`
    on a malformed item, a repeated metric, or a value that is not a number."""
    out: dict[str, float] = {}
    for item in items:
        metric, sep, raw = item.partition("=")
        metric = metric.strip()
        if not sep or not metric:
            raise ValueError(f"tolerance {item!r} is not METRIC=VALUE")
        if metric in out:
            raise ValueError(f"tolerance for {metric} given twice")
        try:
            out[metric] = float(raw)
        except ValueError:
            raise ValueError(f"tolerance {item!r}: {raw!r} is not a number") from None
    return out


# --- one figure ----------------------------------------------------------------

def _column(scores: list[CaseScore], metric: str) -> list[float | None]:
    field_name = _SCORE_FIELD[metric]
    return [getattr(s, field_name) for s in scores]


def _mean_at(column: list[float | None], idx: list[int]) -> float | None:
    return _mean([v for v in (column[i] for i in idx) if v is not None])


def worsening(metric: str, baseline: float | None, candidate: float | None
              ) -> float | None:
    """The candidate's change in the direction that is WORSE for `metric`:
    `candidate - baseline`, or `baseline - candidate` for `pass_rate`. `None`
    when either side is undefined."""
    if baseline is None or candidate is None:
        return None
    return baseline - candidate if metric in LOWER_IS_WORSE else candidate - baseline


def decide(w: float | None, hi: float | None, tol: float) -> str:
    """The decision rule (module docstring, spec §7.4)."""
    if w is None:
        return "undefined"
    if round(w, _DP) > tol:
        return "regressed"
    if hi is None or round(hi, _DP) > tol:
        return "inconclusive"
    return "within_tolerance"


def change_of(identical: bool, lo: float | None, hi: float | None) -> str:
    """What the range says, independent of any tolerance: `none` (every
    case has the same value in both arms), `undefined` (no range),
    `not_detectable` (the range includes 0), `worse` or `better`."""
    if identical:
        return "none"
    if lo is None or hi is None:
        return "undefined"
    if lo > 0:
        return "worse"
    if hi < 0:
        return "better"
    return "not_detectable"


def compare(baseline: list[CaseScore], candidate: list[CaseScore], scope: str,
            plan: CheckPlan) -> dict[str, dict]:
    """Every `CHECK_METRICS` figure for one scope (`"overall"` or
    `"<dimension>=<group>"`), keyed by metric.

    `baseline` and `candidate` are the two arms' scores for the SAME cases.
    Both are sorted by case id here, so the result does not depend on the
    order they are given in. Replicate `b` draws case positions with
    `uncertainty.draw_indices(plan.seed, b, scope, n)` -- one draw, used for
    both arms and every metric: that is the pairing.

    A figure whose denominator is below `plan.min_group_size` in either arm
    is `{"state": "insufficient_cases", ...}`: it keeps its `support` and
    carries no value, no worsening and no range."""
    base = sorted(baseline, key=lambda s: s.case_id)
    cand = sorted(candidate, key=lambda s: s.case_id)
    if [s.case_id for s in base] != [s.case_id for s in cand]:
        raise ValueError(f"{scope}: the two arms do not cover the same cases")
    n = len(base)
    columns = {m: (_column(base, m), _column(cand, m)) for m in CHECK_METRICS}
    samples: dict[str, list[float | None]] = {m: [] for m in CHECK_METRICS}
    everything = list(range(n))
    if n:
        for b in range(plan.replicates):
            idx = draw_indices(plan.seed, b, scope, n)
            for metric, (bcol, ccol) in columns.items():
                samples[metric].append(
                    worsening(metric, _mean_at(bcol, idx), _mean_at(ccol, idx)))
    out: dict[str, dict] = {}
    for metric, (bcol, ccol) in columns.items():
        b_value, c_value = _mean_at(bcol, everything), _mean_at(ccol, everything)
        support = {"baseline": sum(1 for v in bcol if v is not None),
                   "candidate": sum(1 for v in ccol if v is not None)}
        tol = plan.tolerances.get(metric)
        figure: dict = {"baseline": b_value, "candidate": c_value, "support": support,
                        "tolerance": tol}
        if min(support.values()) < plan.min_group_size:
            # Too few cases: the counts, and no figure at all (spec §4.3).
            figure.update(baseline=None, candidate=None, worsening=None, lo=None,
                          hi=None, change="undefined", state=INSUFFICIENT)
        else:
            w = worsening(metric, b_value, c_value)
            values = samples[metric]
            lo = hi = None
            if values and all(v is not None for v in values):
                lo, hi = interval_of(values, plan.interval)
            figure.update(
                worsening=w, lo=lo, hi=hi,
                change=change_of(bcol == ccol, lo, hi),
                state="not_checked" if tol is None else decide(w, hi, tol))
        out[metric] = figure
    return out


# --- the whole check -----------------------------------------------------------

def verdict_of(hard_failed: bool, states: list[str], tolerances_named: bool) -> str:
    """The verdict from the hard checks and every JUDGED figure's state
    (the overall figures, and each sufficiently large group's)."""
    if hard_failed:
        return "hard_fail"
    if "regressed" in states:
        return "regressed"
    if any(s in ("inconclusive", "undefined", INSUFFICIENT) for s in states):
        return "inconclusive"
    return "pass" if tolerances_named else "no_hard_failure"


def run_check(baseline: list[CaseScore], candidate: list[CaseScore],
              keys: dict[str, dict[str, str]],
              chains: tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]],
              plan: CheckPlan, *, arms: tuple[str, str], candidate_doc: dict,
              run_exit_code: int, mode: str) -> dict:
    """The `check.json` document.

    `chains` is `(baseline, candidate)`: each arm's attempt chain per case
    id. `run_exit_code` is the bench run's own code (`runner._exit_code`): 2
    there is a hard failure here whichever arm caused it."""
    ids = sorted(s.case_id for s in baseline)
    by_base = {s.case_id: s for s in baseline}
    by_cand = {s.case_id: s for s in candidate}
    violations = sum(1 for s in candidate if s.violation)
    served_on_empty = sum(1 for s in candidate if s.empty and s.violation)
    hard_failed = violations > 0 or run_exit_code != 0
    changed = sum(1 for i in ids if tuple(chains[0].get(i, ())) != tuple(chains[1].get(i, ())))

    overall = compare(baseline, candidate, "overall", plan)
    # Overall: every named metric is judged; one that cannot be is inconclusive.
    judged: list[tuple[str, str, str]] = [
        ("overall", m, f["state"]) for m, f in overall.items() if f["tolerance"] is not None]
    groups: dict[str, dict[str, dict]] = {}
    for dim in DIMENSIONS:
        groups[dim] = {}
        for label, members in partition(ids, keys, dim).items():
            block: dict = {"n_cases": len(members)}
            if len(members) < plan.min_group_size:
                block["state"] = INSUFFICIENT
            else:
                scope = f"{dim}={label}"
                block["metrics"] = compare([by_base[i] for i in members],
                                           [by_cand[i] for i in members], scope, plan)
                # A group: a figure with too few cases behind it is listed,
                # not judged ("any sufficiently large group").
                judged += [(scope, m, f["state"]) for m, f in block["metrics"].items()
                           if f["tolerance"] is not None and f["state"] != INSUFFICIENT]
            groups[dim][label] = block

    verdict = verdict_of(hard_failed, [state for _, _, state in judged],
                         bool(plan.tolerances))

    def named(*states: str) -> list[dict]:
        return [{"scope": scope, "metric": metric, "state": state}
                for scope, metric, state in judged if state in states]

    caveats = check_caveats(mode)
    if not plan.tolerances:
        caveats.append(CHECK_NO_TOLERANCE)
    return _round_value({
        "schema": SCHEMA,
        "verdict": verdict,
        "exit_code": _EXIT[verdict],
        "effect": "none",
        "arms": {"baseline": arms[0], "candidate": arms[1]},
        "candidate": candidate_doc,
        "plan": {"method": METHOD, "tolerances": dict(sorted(plan.tolerances.items())),
                 "replicates": plan.replicates, "interval": plan.interval,
                 "seed": plan.seed, "min_group_size": plan.min_group_size},
        "hard": {"failed": hard_failed, "candidate_violations": violations,
                 "candidate_served_on_empty": served_on_empty,
                 "run_exit_code": run_exit_code},
        "changed": {"n_cases": len(ids), "choice_changed": changed,
                    "share": changed / len(ids) if ids else None},
        "overall": overall,
        "groups": groups,
        "regressed": named("regressed"),
        "inconclusive": named("inconclusive", "undefined", INSUFFICIENT),
        "caveats": caveats,
    })


# --- check.md ------------------------------------------------------------------

_VERDICT_LINE = {
    "hard_fail": "**HARD FAIL** (exit 2). The candidate made an ineligible choice, "
                 "served a case whose feasible set is empty, or the run failed an "
                 "integrity check. No tolerance applies to this.",
    "regressed": "**REGRESSED** (exit 3). At least one named metric is worse than its "
                 "tolerance allows, overall or in a group with enough cases.",
    "inconclusive": "**INCONCLUSIVE** (exit 4). No named metric is worse than its "
                    "tolerance at the point estimate, but at least one could not be "
                    "shown to be within it.",
    "pass": "**PASS** (exit 0). No hard failure, and every named metric is within its "
            "tolerance, range included, overall and in every group with enough cases.",
    "no_hard_failure": "**NO HARD FAILURE** (exit 0). No tolerance was named, so no "
                       "metric was judged.",
}


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def _figure_row(scope: str, metric: str, f: dict) -> str:
    if f["state"] == INSUFFICIENT:
        n = min(f["support"].values())
        return (f"| {scope} | {metric} | {_num(f['baseline'])} | {_num(f['candidate'])} | "
                f"insufficient cases (n = {n}) | n/a | n/a | {_num(f['tolerance'])} | "
                f"{INSUFFICIENT} |")
    rng = ("n/a" if f["lo"] is None
           else f"[{_num(f['lo'])}, {_num(f['hi'])}]")
    return (f"| {scope} | {metric} | {_num(f['baseline'])} | {_num(f['candidate'])} | "
            f"{_num(f['worsening'])} | {rng} | {f['change']} | {_num(f['tolerance'])} | "
            f"{f['state']} |")


def render_check(doc: dict) -> str:
    """`check.md`: the fixed text, the verdict, the hard checks, every
    overall figure, then each group's figures for the metrics a tolerance was
    named for. Rows follow `DIMENSIONS`, label and `CHECK_METRICS` order;
    nothing is sorted by size."""
    plan = doc["plan"]
    level = plan["interval"]
    cand = doc["candidate"]
    lines = ["# Router-version check (offline)", ""]
    lines += [f"> {text}" for text in doc["caveats"]]
    lines += ["", "## Verdict", "", _VERDICT_LINE[doc["verdict"]], ""]
    lines += [f"- **baseline arm**: `{doc['arms']['baseline']}` (the routing config as "
              "loaded)",
              f"- **candidate arm**: `{doc['arms']['candidate']}` (id `{cand['id']}`"
              + (f", name `{cand['name']}`" if cand.get("name") else "") + ")",
              f"- **candidate overlay**: `{cand['routing']}`",
              f"- **cases whose attempt chain changed**: "
              f"{doc['changed']['choice_changed']} of {doc['changed']['n_cases']}",
              f"- **tolerances**: "
              + (", ".join(f"{m} {_num(t)}" for m, t in plan["tolerances"].items())
                 or "none named"),
              f"- **range**: {level:.0%} percentile interval of the paired difference, "
              f"{plan['replicates']} resamples of the cases, seed {plan['seed']}",
              f"- **minimum cases behind a judged figure**: {plan['min_group_size']}", ""]
    hard = doc["hard"]
    lines += ["## Hard checks (no tolerance)", "",
              f"- **candidate attempts outside the eligible set (cases)**: "
              f"{hard['candidate_violations']}",
              f"- **of those, on a case whose feasible set is empty**: "
              f"{hard['candidate_served_on_empty']}",
              f"- **bench run exit code**: {hard['run_exit_code']}", ""]
    header = ["| scope | metric | baseline | candidate | worsening | range | change | "
              "tolerance | state |", "|---|---|---|---|---|---|---|---|---|"]
    lines += ["## Overall", "",
              "`worsening` is candidate minus baseline in the direction that is worse "
              "for that metric (for `pass_rate`, baseline minus candidate). Positive "
              "is worse.", "", *header]
    lines += [_figure_row("overall", m, doc["overall"][m]) for m in CHECK_METRICS]
    named = [m for m in CHECK_METRICS if m in plan["tolerances"]]
    lines += ["", "## Groups", "",
              "Only the metrics a tolerance was named for; `check.json` holds every "
              "metric for every group.", "", *header]
    for dim in DIMENSIONS:
        for label, block in doc["groups"][dim].items():
            scope = f"{dim}={label}"
            if "metrics" not in block:
                lines.append(f"| {scope} | all | n/a | n/a | insufficient cases "
                             f"(n = {block['n_cases']}) | n/a | n/a | n/a | "
                             f"{INSUFFICIENT} |")
                continue
            lines += [_figure_row(scope, m, block["metrics"][m]) for m in named]
    lines.append("")
    return "\n".join(lines)
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_version_check.py tests/eval/test_isolation.py -p no:warnings -rf
```

Expected: `63 passed` (61 + 2).

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1500 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add app/eval/version_check.py tests/eval/test_version_check.py
git commit -m "$(cat <<'EOF'
feat(eval): the offline router-version check, pure (P2 metrics)

app/eval/version_check.py compares two arms' per-case scores: worsening per
metric, a paired case bootstrap for its range, and a decision rule in which
the point estimate alone fails a figure and the range can only withhold a
pass. Any ineligible choice is a hard fail that no tolerance changes. It
applies nothing; the runner wiring is the next commit.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `--candidate` in the runner and the CLI (spec §7.2, §7.5, §7.6; E1, E2, E7, E8, E11, E15, E16)

**Files:**
- Modify: `app/eval/runner.py`
- Modify: `scripts/feasible_bench.py`
- Create: `tests/eval/test_version_check_run.py`

**Interfaces:**
- Consumes: `app/eval/candidate.py::ARM`, `Candidate`, `candidate_settings`, `load_candidate` (Task 5); `app/eval/version_check.py::CHECK_METRICS`, `CheckPlan`, `parse_tolerances`, `render_check`, `run_check` (Task 6); `app/eval/strategies.py::OnlineArm` (the test subclasses it to inject an ineligible arm; the runner constructs it as `OnlineArm(cfg, registry, max_attempts, seconds_per_case=...)`, and its `choose(inp, failed, *, case_index)` and `observe(case_index, attempts, rows_by_endpoint)` are the two methods overridden); `tests/eval/test_runner.py::REPO_ROOT`, `_case`, `_cli`, `_jsonl`, `_run`, `_write_inputs`.
- Produces:
  - `run_bench(..., candidate_path: str | None = None, check_plan: CheckPlan | None = None) -> RunResult` — keyword-only, after `min_group_size`. A `check_plan` without a `candidate_path` is a load error. The candidate's settings go into the runner's existing `variant_cfgs` under the name `candidate`, so it gets the three arms a variant gets; a `--variants` file that already has a `candidate` entry is then a load error. `check_plan`'s `min_group_size` is replaced by the call's.
  - `RunResult.check: dict | None = None` — the `check.json` document, as on disk. With a candidate, `RunResult.exit_code` is the check's (0, 2, 3 or 4).
  - With a candidate: `<out>/check.json`, `<out>/check.md`; `run.json` gains `candidate: {id, name, verdict}`, its `exit_code` is the process's, and `args` gains `candidate` and `min_group_size`. `metrics.json`'s own `exit_code` stays the bench run's (0 or 2).
  - Every run removes a stale `check.json` / `check.md`.
  - CLI: `--candidate FILE`, `--tolerance METRIC=VALUE` (repeatable), `--check-replicates N`, `--check-interval X`, `--check-seed N`. Each of the last four without `--candidate` is `feasible_bench: load error: <flags> needs --candidate`, exit 1. `--min-group-size` now needs `--group-report` **or** `--candidate`. After a run with a candidate the CLI prints `feasible_bench: router-version check: <verdict> (nothing was applied)`.

**How the hard failure reaches exit 2 twice over.** The candidate's arm is a strategy of the run. If it names an ineligible endpoint, `runner._exit_code` already returns 2 — the bench's own rule, untouched — and the check then reads both that code and the candidate's per-case violations. Either alone is a `hard_fail`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_version_check_run.py`:

````python
"""`--candidate` end to end (spec §7; invariants E1, E2, E7, E8, E11, E15,
E16): the runner and the CLI over the tiny simulated suite of
`tests/eval/test_runner.py`, plus the starter suite. Every run is simulated.

The tiny suite has three endpoints, one per tier (`local-a` cheap, `local-b`
mid, `cloud-c` frontier, each dearer than the last). `ALL_FRONTIER` sets both
complexity thresholds to 0, so the candidate targets the frontier tier for
every request: it moves three of the five attempt chains and raises the mean
chosen cost. `SAME` restates the shipped weights and so changes nothing.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app.config.loader import load_settings
from app.eval import runner
from app.eval.candidate import SCHEMA
from app.eval.strategies import OnlineArm
from app.eval.version_check import CHECK_METRICS, CheckPlan
from tests.eval.test_runner import REPO_ROOT, _case, _cli, _jsonl, _run, _write_inputs

ALL_FRONTIER = {"complexity_thresholds": {"cheap_max": 0.0, "mid_max": 0.0}}
SAME = {"weights": dict(load_settings().routing.weights)}
#: The weight that marks the injected arm below; no shipped weight has it.
MARK = 0.123456
PINNED = ("metrics.json", "matrix.jsonl", "choices.jsonl")
BASE_ARM, CAND_ARM = "router_online", "router_online:candidate"


def _candidate(tmp_path, routing, name="t") -> str:
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump({"schema": SCHEMA, "name": name, "routing": routing}))
    return str(path)


def _plan(**tolerances) -> CheckPlan:
    return CheckPlan(tolerances=tolerances)


def _inject_ineligible(monkeypatch, chain=("cloud-c",)) -> None:
    """Make the CANDIDATE's online arm (and only it) choose `chain` for
    every case. `cloud-c` is eligible for most cases and never for the
    `onprem_only` one -- a realistic violation, not a made-up slug."""
    class Injected(OnlineArm):
        def __init__(self, settings, *args, **kwargs):
            super().__init__(settings, *args, **kwargs)
            self._injected = settings.routing.weights.get("latency") == MARK

        def choose(self, inp, failed, *, case_index):
            real = super().choose(inp, failed, case_index=case_index)
            return tuple(chain) if self._injected else real

        def observe(self, case_index, attempts, rows_by_endpoint):
            super().observe(case_index, () if self._injected else attempts,
                            rows_by_endpoint)

    monkeypatch.setattr(runner, "OnlineArm", Injected)


# --- E1 / E16: the candidate is extra arms over the same sweep -----------------

def test_e1_without_a_candidate_no_check_is_written(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.check is None
    assert not (out / "check.json").exists() and not (out / "check.md").exists()
    stamp = json.loads((out / "run.json").read_text())
    assert "candidate" not in stamp and "candidate" not in stamp["args"]


def test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    plain, flagged = tmp_path / "plain", tmp_path / "flagged"
    before = _run(suite, eps, plain, seed=3)
    after = _run(suite, eps, flagged, seed=3,
                 candidate_path=_candidate(tmp_path, ALL_FRONTIER))
    # One sweep: the same (case, endpoint) outcomes for both arms.
    assert (plain / "matrix.jsonl").read_bytes() == (flagged / "matrix.jsonl").read_bytes()
    # Every arm that existed is untouched; the candidate only ADDS three.
    added = set(after.metrics["strategies"]) - set(before.metrics["strategies"])
    assert added == {"router_cold:candidate", CAND_ARM, "router_warm_prior:candidate"}
    for name, m in before.metrics["strategies"].items():
        assert after.metrics["strategies"][name] == m, name
    old = [c for c in _jsonl(flagged / "choices.jsonl")
           if not c["strategy"].endswith(":candidate")]
    assert old == _jsonl(plain / "choices.jsonl")
    assert after.metrics["cases"] == before.metrics["cases"]
    assert sorted(p.name for p in flagged.iterdir()) == [
        "check.json", "check.md", "choices.jsonl", "matrix.jsonl", "metrics.json",
        "report.md", "run.json"]


def test_e2_the_check_is_byte_identical_twice(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    cand = _candidate(tmp_path, ALL_FRONTIER)
    a, b = tmp_path / "a", tmp_path / "b"
    _run(suite, eps, a, seed=7, candidate_path=cand, check_plan=_plan(pass_rate=0.5))
    _run(suite, eps, b, seed=7, candidate_path=cand, check_plan=_plan(pass_rate=0.5))
    for name in ("check.json", "check.md", *PINNED):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


# --- E8: candidate == baseline -------------------------------------------------

def test_e8_a_candidate_that_restates_the_config_passes_with_zero_tolerance(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, candidate_path=_candidate(tmp_path, SAME),
                  check_plan=_plan(pass_rate=0.0, quality_regret=0.0, mean_chosen_cost=0.0,
                                   wasted_attempts=0.0, exhausted_rate=0.0))
    assert result.exit_code == 0 and result.check["verdict"] == "pass"
    assert result.check["changed"] == {"n_cases": 5, "choice_changed": 0, "share": 0.0}
    for metric in ("pass_rate", "quality_regret", "mean_chosen_cost"):
        f = result.check["overall"][metric]
        assert (f["worsening"], f["lo"], f["hi"], f["change"]) == (0.0, 0.0, 0.0, "none")
    m = result.metrics["strategies"]
    assert m[CAND_ARM] == m[BASE_ARM]
    assert result.check == json.loads((out / "check.json").read_text())


# --- E7: the hard checks -------------------------------------------------------

@pytest.mark.parametrize("tolerances", [
    {}, {m: 1e9 for m in CHECK_METRICS}], ids=["no-tolerance", "huge-tolerances"])
def test_e7_an_injected_ineligible_candidate_exits_2_whatever_the_tolerances(
        tmp_path, monkeypatch, tolerances):
    _inject_ineligible(monkeypatch)
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out,
                  candidate_path=_candidate(tmp_path, {"weights": {"latency": MARK}}),
                  check_plan=CheckPlan(tolerances=tolerances))
    assert result.exit_code == 2
    check = result.check
    assert check["verdict"] == "hard_fail" and check["exit_code"] == 2
    assert check["hard"]["failed"] is True and check["hard"]["candidate_violations"] >= 1
    assert check["hard"]["run_exit_code"] == 2
    # The detector fired for the candidate and for nothing else.
    assert result.metrics["strategies"][CAND_ARM]["violation_rate"] > 0
    assert result.metrics["strategies"][BASE_ARM]["violation_rate"] == 0.0
    assert "**HARD FAIL** (exit 2)" in (out / "check.md").read_text()
    assert json.loads((out / "run.json").read_text())["exit_code"] == 2


def test_e7_a_candidate_that_serves_an_empty_feasible_set_is_a_hard_fail(
        tmp_path, monkeypatch):
    """One case is `onprem_only` with no on-prem endpoint: its feasible set
    is empty and the real pipeline rejects it. The injected candidate names
    an endpoint for it anyway."""
    _inject_ineligible(monkeypatch)
    cases = [_case("faq-1", "What is the capital of France?"),
             _case("onprem-empty", "Where is Paris?", residency="onprem_only",
                   expect_empty=True)]
    cloud_only = [{"slug": "cloud-c", "provider": "azure", "tier": "frontier",
                   "price": {"input": 5.0, "output": 5.0}, "region": "us",
                   "prompt_retention": "none", "accepts_raw_sensitive": False,
                   "simulated": {"quality": {"default": 1.0},
                                 "latency_ms": {"mean": 900, "jitter": 20}}}]
    suite, eps = _write_inputs(tmp_path, cases, cloud_only)
    result = _run(suite, eps, tmp_path / "out",
                  candidate_path=_candidate(tmp_path, {"weights": {"latency": MARK}}),
                  check_plan=_plan(pass_rate=1.0))
    assert result.exit_code == 2 and result.check["verdict"] == "hard_fail"
    assert result.check["hard"]["candidate_served_on_empty"] == 1
    summary = {c["case_id"]: c for c in result.metrics["cases"]}["onprem-empty"]
    assert summary["rejected_ok"] is True and summary["calls"] == 0   # nothing was SERVED


# --- regressed / inconclusive ---------------------------------------------------

def test_a_candidate_that_costs_more_than_the_tolerance_exits_3(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, candidate_path=_candidate(tmp_path, ALL_FRONTIER),
                  check_plan=_plan(mean_chosen_cost=1e-6))
    assert result.exit_code == 3 and result.check["verdict"] == "regressed"
    f = result.check["overall"]["mean_chosen_cost"]
    m = result.metrics["strategies"]
    assert f["baseline"] == m[BASE_ARM]["mean_chosen_cost"]
    assert f["candidate"] == m[CAND_ARM]["mean_chosen_cost"]
    assert f["worsening"] == pytest.approx(f["candidate"] - f["baseline"], abs=1e-12)
    assert f["worsening"] > 1e-6 and f["state"] == "regressed"
    assert result.check["changed"]["choice_changed"] == 3
    assert result.check["hard"]["failed"] is False
    # `metrics.json` keeps the bench run's own integrity code; the process
    # exit code is in `check.json` and `run.json`.
    assert json.loads((out / "metrics.json").read_text())["exit_code"] == 0
    stamp = json.loads((out / "run.json").read_text())
    assert stamp["exit_code"] == 3
    assert stamp["candidate"] == {"id": result.check["candidate"]["id"], "name": "t",
                                  "verdict": "regressed"}
    assert "**REGRESSED** (exit 3)" in (out / "check.md").read_text()


def test_the_same_candidate_with_no_tolerance_is_shown_and_not_judged(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out",
                  candidate_path=_candidate(tmp_path, ALL_FRONTIER))
    assert result.exit_code == 0 and result.check["verdict"] == "no_hard_failure"
    assert result.check["overall"]["mean_chosen_cost"]["worsening"] > 0


def test_a_named_metric_with_too_few_cases_is_inconclusive_and_exits_4(tmp_path):
    """Five cases against a minimum of six: nothing can be judged."""
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", min_group_size=6,
                  candidate_path=_candidate(tmp_path, SAME),
                  check_plan=_plan(pass_rate=0.5))
    assert result.exit_code == 4 and result.check["verdict"] == "inconclusive"
    assert result.check["plan"]["min_group_size"] == 6


# --- E11: nothing is applied ---------------------------------------------------

def _digest(paths) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def test_e11_the_check_writes_only_under_out_and_touches_no_config(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    cand = _candidate(tmp_path, ALL_FRONTIER)
    config = _digest((REPO_ROOT / "config").rglob("*.yaml"))
    inputs = _digest(p for p in tmp_path.iterdir() if p.is_file())
    out = tmp_path / "out"
    _run(suite, eps, out, candidate_path=cand, check_plan=_plan(mean_chosen_cost=1e-6),
         group_report=True)
    assert _digest((REPO_ROOT / "config").rglob("*.yaml")) == config
    assert _digest(p for p in tmp_path.iterdir() if p.is_file()) == inputs
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [Path(suite).name, Path(eps).name, "candidate.yaml", "out"])
    assert sorted(p.name for p in out.iterdir()) == [
        "check.json", "check.md", "choices.jsonl", "groups.json", "groups.md",
        "matrix.jsonl", "metrics.json", "report.md", "run.json"]


#: module -> every non-stdlib module it may import. None of them is an admin
#: route, the application store, the pipeline or an HTTP client.
_MAY_IMPORT = {
    "groups.py": {"app.eval.oracle"},
    "version_check.py": {"app.eval.groups", "app.eval.oracle", "app.eval.uncertainty"},
    "candidate.py": {"yaml", "app.config.lint", "app.config.schema",
                     "app.eval.strategies", "app.routing.strategy"},
}
_STDLIB = {"__future__", "copy", "dataclasses", "hashlib", "json", "pathlib"}
_WRITES = {"write_text", "write_bytes", "unlink", "mkdir", "rename",
           "post", "put", "patch", "delete", "request", "commit", "execute", "open"}


@pytest.mark.parametrize("module", sorted(_MAY_IMPORT))
def test_e11_the_new_modules_have_no_way_to_apply_anything(module):
    """No import of a serving surface that could change a deployment, and no
    call that writes a file, a database row or an HTTP request. The only
    writes of this slice are the runner's, into `--out`."""
    tree = ast.parse((REPO_ROOT / "app" / "eval" / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported - _STDLIB == _MAY_IMPORT[module]
    calls = {(n.func.attr if isinstance(n.func, ast.Attribute) else
              getattr(n.func, "id", "")) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & _WRITES, calls & _WRITES


def test_e11_no_admin_route_or_setting_was_added():
    """The slice adds no way in: `app/` outside `app/eval/` does not mention
    the candidate schema, the check, or the group report."""
    needles = ("router-candidate", "router-version-check", "bench-group-report",
               "version_check", "candidate_settings")
    for path in (REPO_ROOT / "app").rglob("*.py"):
        if (REPO_ROOT / "app" / "eval") in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, (str(path), needle)


# --- E15: load errors ----------------------------------------------------------

@pytest.mark.parametrize("doc, message", [
    ({"schema": SCHEMA, "routing": {"max_escalations": 2}}, "cannot be evaluated"),
    ({"schema": SCHEMA, "routing": {"wieghts": {"cost": 1}}}, "routing.wieghts"),
    ({"schema": "nope", "routing": {"strategy": "default"}}, "schema"),
    ({"schema": SCHEMA, "routing": {"strategy": "no-such"}}, "routing.strategy"),
])
def test_e15_a_bad_candidate_is_a_load_error_before_anything_is_written(
        tmp_path, capsys, doc, message):
    suite, eps = _write_inputs(tmp_path)
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump(doc))
    out = tmp_path / "out"
    result = _run(suite, eps, out, candidate_path=str(path))
    assert result.exit_code == 1 and not out.exists()
    assert message in capsys.readouterr().err


def test_e15_a_missing_candidate_file_is_a_load_error(tmp_path, capsys):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out",
                  candidate_path=str(tmp_path / "absent.yaml"))
    assert result.exit_code == 1 and "load error" in capsys.readouterr().err


def test_e15_a_variant_may_not_take_the_candidates_name(tmp_path, capsys):
    suite, eps = _write_inputs(tmp_path)
    variants = tmp_path / "variants.yaml"
    variants.write_text(yaml.safe_dump({"candidate": dict(SAME["weights"])}))
    result = _run(suite, eps, tmp_path / "out", variants_path=str(variants),
                  candidate_path=_candidate(tmp_path, SAME))
    assert result.exit_code == 1 and "reserved" in capsys.readouterr().err
    # Without a candidate the same variants file still runs, as it always did.
    assert _run(suite, eps, tmp_path / "ok", variants_path=str(variants)).exit_code == 0


def test_e15_a_plan_without_a_candidate_is_a_load_error(tmp_path, capsys):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", check_plan=_plan(pass_rate=0.1))
    assert result.exit_code == 1 and "without a candidate" in capsys.readouterr().err


# --- with the other outputs -----------------------------------------------------

def test_the_group_report_covers_the_candidates_arms(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1,
                  candidate_path=_candidate(tmp_path, ALL_FRONTIER))
    assert CAND_ARM in result.groups["strategies"]
    assert result.check["plan"]["min_group_size"] == 1
    # One set of numbers: the check's overall figure is the group report's.
    for arm, side in ((BASE_ARM, "baseline"), (CAND_ARM, "candidate")):
        assert (result.groups["strategies"][arm]["overall"]["metrics"]["pass_rate"]["value"]
                == result.check["overall"]["pass_rate"][side])


def test_a_stale_check_is_removed_by_a_run_without_a_candidate(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    _run(suite, eps, out, candidate_path=_candidate(tmp_path, SAME))
    assert (out / "check.json").is_file()
    _run(suite, eps, out)
    assert not (out / "check.json").exists() and not (out / "check.md").exists()


# --- the starter suite ----------------------------------------------------------

STARTER = ("eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml")


def test_starter_restating_the_config_passes(tmp_path):
    result = _run(*STARTER, tmp_path / "out", candidate_path=_candidate(tmp_path, SAME),
                  check_plan=_plan(pass_rate=0.0, mean_chosen_cost=0.0))
    assert result.exit_code == 0 and result.check["verdict"] == "pass"
    assert result.check["changed"]["choice_changed"] == 0


def test_starter_a_latency_only_candidate_regresses_on_cost(tmp_path):
    """Cost weight 0, latency weight 2: the candidate prefers the faster,
    dearer cloud endpoint wherever both are eligible. It makes no ineligible
    choice -- the eligible set is not the router's to change -- and it costs
    more than a tolerance of $0.0001 a request allows."""
    result = _run(*STARTER, tmp_path / "out",
                  candidate_path=_candidate(tmp_path, {"weights": {"cost": 0.0,
                                                                   "latency": 2.0}}),
                  check_plan=_plan(pass_rate=0.02, mean_chosen_cost=0.0001))
    assert result.exit_code == 3
    check = result.check
    assert check["hard"] == {"failed": False, "candidate_violations": 0,
                             "candidate_served_on_empty": 0, "run_exit_code": 0}
    assert check["overall"]["mean_chosen_cost"]["state"] == "regressed"
    assert check["overall"]["mean_chosen_cost"]["change"] == "worse"
    assert {"scope": "overall", "metric": "mean_chosen_cost",
            "state": "regressed"} in check["regressed"]
    # The two groups below the minimum are listed, never judged.
    assert check["groups"]["placement"]["escalated"] == {
        "n_cases": 2, "state": "insufficient_cases"}
    assert not [r for r in check["regressed"] if "escalated" in r["scope"]]


# --- CLI ------------------------------------------------------------------------

def test_cli_a_regressing_candidate_exits_3_and_says_nothing_was_applied(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "cli-out"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--candidate", _candidate(tmp_path, ALL_FRONTIER),
                "--tolerance", "mean_chosen_cost=0.000001", "--tolerance", "pass_rate=0.5",
                "--check-replicates", "200", "--check-interval", "0.8", "--check-seed", "4")
    assert proc.returncode == 3, proc.stderr
    assert "router-version check: regressed (nothing was applied)" in proc.stdout
    plan = json.loads((out / "check.json").read_text())["plan"]
    assert plan == {"method": "paired_case_bootstrap", "replicates": 200, "interval": 0.8,
                    "seed": 4, "min_group_size": 5,
                    "tolerances": {"mean_chosen_cost": 1e-06, "pass_rate": 0.5}}


def test_cli_a_candidate_that_changes_nothing_exits_0(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(tmp_path / "o"),
                "--candidate", _candidate(tmp_path, SAME), "--tolerance", "pass_rate=0")
    assert proc.returncode == 0, proc.stderr
    assert "router-version check: pass (nothing was applied)" in proc.stdout


def test_cli_too_few_cases_exits_4(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(tmp_path / "o"),
                "--candidate", _candidate(tmp_path, SAME), "--tolerance", "pass_rate=0.5",
                "--min-group-size", "6")
    assert proc.returncode == 4, proc.stderr
    assert "router-version check: inconclusive" in proc.stdout


@pytest.mark.parametrize("flags, message", [
    (["--tolerance", "pass_rate=0.1"], "--tolerance needs --candidate"),
    (["--check-replicates", "50"], "--check-replicates needs --candidate"),
    (["--check-interval", "0.5", "--check-seed", "1"],
     "--check-interval, --check-seed needs --candidate"),
])
def test_cli_a_check_flag_without_a_candidate_is_refused(tmp_path, flags, message):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out), *flags)
    assert proc.returncode == 1 and message in proc.stderr
    assert not out.exists()


@pytest.mark.parametrize("flags, message", [
    (["--tolerance", "accuracy=0.1"], "unknown metric"),
    (["--tolerance", "pass_rate=-1"], "finite number >= 0"),
    (["--tolerance", "pass_rate"], "METRIC=VALUE"),
    (["--check-replicates", "0"], "replicates"),
    (["--check-interval", "1.5"], "interval"),
    (["--check-seed", "-2"], "seed"),
])
def test_cli_a_bad_check_value_is_refused(tmp_path, flags, message):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--candidate", _candidate(tmp_path, SAME), *flags)
    assert proc.returncode == 1 and message in proc.stderr
    assert not out.exists()
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_version_check_run.py -p no:warnings -rf
```

Expected: `34 failed, 4 passed`. The failures are `TypeError: run_bench() got an unexpected keyword argument 'candidate_path'` (or `'check_plan'`), `AttributeError: 'RunResult' object has no attribute 'check'`, and, for the CLI tests, argparse rejecting an unknown flag with exit 2 where 0, 1, 3 or 4 is expected. The four that pass already are the E11 tests that only read source files: `test_e11_the_new_modules_have_no_way_to_apply_anything` for the three modules of Tasks 3, 5 and 6, and `test_e11_no_admin_route_or_setting_was_added`. No test that calls `run_bench` with a candidate may pass yet.

- [ ] **Step 3: Wire the check into the runner and the CLI**

Patch (save as a file and run `git apply <file>`):

````diff
diff --git a/app/eval/runner.py b/app/eval/runner.py
index 1bc1925..419e979 100644
--- a/app/eval/runner.py
+++ b/app/eval/runner.py
@@ -18,7 +18,12 @@ case:
 After the last case it computes the feasible-oracle metrics (`metrics.json`),
 a human report (`report.md`) and a run stamp (`run.json`). With
 `group_report=True` it also writes `groups.json` and `groups.md`
-(`app/eval/groups.py`); without it, no output changes. Exit codes: `0` ok,
+(`app/eval/groups.py`). With `candidate_path` it scores a candidate routing
+config as extra router arms over the same sweep and writes `check.json` and
+`check.md` (`app/eval/version_check.py`), which adds exit codes `3` (a named
+metric regressed beyond its tolerance) and `4` (inconclusive). Neither is a
+promotion path: both write files and nothing else. Without those two
+arguments no output changes. Exit codes: `0` ok,
 `1` load/usage error (including a `seconds_per_case` too small for this run's
 endpoint count or attempt cap -- `strategies.min_seconds_per_case`), `2`
 policy violation, empty-set mismatch, post-gate widening, pinned-served
@@ -36,7 +41,7 @@ from __future__ import annotations
 import json
 import subprocess
 import sys
-from dataclasses import dataclass
+from dataclasses import dataclass, replace
 from datetime import datetime, timezone
 from pathlib import Path
 
@@ -46,6 +51,8 @@ from app.apps.privacy import AppPrivacyProfile
 from app.compression.ccr import CCRStore
 from app.config.loader import load_settings
 from app.errors import BackendUnavailable, ContextWindowExceeded, NoEligibleCandidate
+from app.eval.candidate import ARM as CANDIDATE_ARM
+from app.eval.candidate import Candidate, candidate_settings, load_candidate
 from app.eval.cases import Case, SuiteError, load_suite
 from app.eval.endpoints import (
     EndpointFileError,
@@ -73,6 +80,7 @@ from app.eval.oracle import (
     worst_groups,
 )
 from app.eval.simulated import SimulatedBackend
+from app.eval.version_check import CheckPlan, render_check, run_check
 from app.eval.strategies import (
     DEFAULT_SECONDS_PER_CASE,
     AttemptStrategy,
@@ -181,6 +189,7 @@ class RunResult:
     out_dir: str
     metrics: dict
     groups: dict | None = None    # the `groups.json` document, when asked for
+    check: dict | None = None     # the `check.json` document, when asked for
 
 
 class _CountingBackend:
@@ -388,7 +397,9 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
                     fixed: str | None = None,
                     seconds_per_case: float = DEFAULT_SECONDS_PER_CASE,
                     group_report: bool = False,
-                    min_group_size: int = DEFAULT_MIN_GROUP_SIZE) -> RunResult:
+                    min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
+                    candidate_path: str | None = None,
+                    check_plan: CheckPlan | None = None) -> RunResult:
     """Run the benchmark end to end. Load/usage errors print to stderr and
     return exit code 1 before `out_dir` is touched.
 
@@ -397,18 +408,30 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
 
     `group_report` also writes `groups.json` and `groups.md`: every metric
     by placement, protection, application and workload, a figure over fewer
-    than `min_group_size` cases printed as a count and no rate."""
+    than `min_group_size` cases printed as a count and no rate.
+
+    `candidate_path` names a `routeforge.router-candidate/v1` file. Its
+    routing overlay is scored as `router_cold:candidate`,
+    `router_online:candidate` and `router_warm_prior:candidate` over the
+    same sweep, and `check.json` / `check.md` compare `router_online` with
+    `router_online:candidate` under `check_plan` (default `CheckPlan()`,
+    whose `min_group_size` is replaced by this call's). The returned
+    `exit_code` is then the check's: 0, 2, 3 or 4. Nothing is applied."""
     args = {"suite": suite_path, "endpoints": endpoints_path, "seed": seed,
             "live": live, "keep_text": keep_text, "variants": variants_path,
             "parity_tol": parity_tol, "fixed": fixed,
             "seconds_per_case": seconds_per_case}
     if group_report:   # only then: an unflagged run's `run.json` keeps its keys
         args.update(group_report=True, min_group_size=min_group_size)
+    if candidate_path is not None:
+        args.update(candidate=candidate_path, min_group_size=min_group_size)
     started = _now()
     try:
         if (isinstance(min_group_size, bool) or not isinstance(min_group_size, int)
                 or min_group_size < 1):
             raise BenchError(f"min_group_size must be an int >= 1; got {min_group_size!r}")
+        if check_plan is not None and candidate_path is None:
+            raise BenchError("a check plan was given without a candidate")
         seconds_per_case = check_seconds_per_case(seconds_per_case)
         cases, suite_hash = load_suite(suite_path)
         base = load_settings()
@@ -440,6 +463,16 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
                 variant_cfgs[name] = variant_settings(settings, weights)
             except Exception as exc:
                 raise BenchError(f"variant {name!r}: {exc}") from exc
+        candidate: Candidate | None = None
+        if candidate_path is not None:
+            if CANDIDATE_ARM in variants:
+                raise BenchError(f"variant name {CANDIDATE_ARM!r} is reserved when a "
+                                 "candidate is given")
+            candidate = load_candidate(candidate_path)
+            # Scored exactly like a weight variant: its own Settings, its own
+            # three router arms, the shared sweep matrix.
+            variant_cfgs[CANDIDATE_ARM] = candidate_settings(settings, candidate)
+            check_plan = replace(check_plan or CheckPlan(), min_group_size=min_group_size)
         try:
             detector = make_detector(settings.serving.pii_detection)
         except ImportError as exc:
@@ -464,6 +497,7 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
                 out_dir=out_dir, seed=seed,
                 live=live, keep_text=keep_text, parity_tol=parity_tol, fixed=fixed,
                 group_report=group_report, min_group_size=min_group_size,
+                candidate=candidate, check_plan=check_plan,
                 stamp={"suite_hash": suite_hash, "endpoints_hash": endpoints_hash,
                        "args": args, "started_at": started})
         except LeakageError as exc:
@@ -481,7 +515,8 @@ async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
 async def _run_loaded(*, cases, specs, settings, registry, detector,
                       variant_cfgs, max_attempts, seconds_per_case,
                       out_dir, seed, live, keep_text, parity_tol, fixed,
-                      group_report, min_group_size, stamp) -> RunResult:
+                      group_report, min_group_size, candidate, check_plan,
+                      stamp) -> RunResult:
     inner = make_backend(settings, registry) if live else SimulatedBackend(specs, seed)
     backend = _CountingBackend(inner)
     gate = Gate(settings, detector, registry=registry)
@@ -542,7 +577,8 @@ async def _run_loaded(*, cases, specs, settings, registry, detector,
 
     out = Path(out_dir)
     out.mkdir(parents=True, exist_ok=True)
-    for stale in ("metrics.json", "report.md", "run.json", "groups.json", "groups.md"):
+    for stale in ("metrics.json", "report.md", "run.json", "groups.json", "groups.md",
+                  "check.json", "check.md"):
         (out / stale).unlink(missing_ok=True)
 
     case_infos: list[CaseInfo] = []
@@ -738,8 +774,28 @@ async def _run_loaded(*, cases, specs, settings, registry, detector,
         "notes": {**ARM_NOTES, "base_routing_policy_version": BASE_RPV_NOTE},
     }
     groups_doc: dict | None = None
+    check_doc: dict | None = None
+    keys = {case.id: case_keys(case.tags, case.app_policy) for case in cases}
+    if candidate is not None:
+        arms = (HEADLINE_ROUTER, f"{HEADLINE_ROUTER}:{CANDIDATE_ARM}")
+        chains = tuple({c.case_id: c.attempts for c in choices if c.strategy == arm}
+                       for arm in arms)
+        check_doc = json.loads(_dump(run_check(
+            case_scores(case_infos, rows, choices, arms[0]),
+            case_scores(case_infos, rows, choices, arms[1]),
+            keys, chains, check_plan, arms=arms,
+            candidate_doc={"id": candidate.id, "name": candidate.name,
+                           "routing": candidate.routing},
+            run_exit_code=exit_code, mode=run_stamp["mode"])))
+        (out / "check.json").write_text(_dump(check_doc), encoding="utf-8")
+        (out / "check.md").write_text(render_check(check_doc), encoding="utf-8")
+        # The process exit code. `metrics.json`'s `exit_code` stays the bench
+        # run's own integrity code (0 or 2), as it always was.
+        run_stamp["candidate"] = {"id": candidate.id, "name": candidate.name,
+                                  "verdict": check_doc["verdict"]}
+        run_stamp["exit_code"] = check_doc["exit_code"]
+        exit_code = check_doc["exit_code"]
     if group_report:
-        keys = {case.id: case_keys(case.tags, case.app_policy) for case in cases}
         scores = {n: case_scores(case_infos, rows, choices, n) for n in strategy_names}
         groups_doc = json.loads(_dump(build_group_report(
             scores, keys, min_size=min_group_size, mode=run_stamp["mode"])))
@@ -749,7 +805,7 @@ async def _run_loaded(*, cases, specs, settings, registry, detector,
     (out / "run.json").write_text(_dump(run_stamp), encoding="utf-8")
     (out / "report.md").write_text(_report(metrics, run_stamp), encoding="utf-8")
     return RunResult(exit_code=exit_code, out_dir=str(out), metrics=metrics,
-                     groups=groups_doc)
+                     groups=groups_doc, check=check_doc)
 
 
 def _served(attempts: tuple[str, ...], rows_by_endpoint: dict[str, MatrixRow]
diff --git a/scripts/feasible_bench.py b/scripts/feasible_bench.py
index 8d6775d..57fd7b2 100644
--- a/scripts/feasible_bench.py
+++ b/scripts/feasible_bench.py
@@ -17,8 +17,16 @@ Writes `matrix.jsonl`, `choices.jsonl`, `metrics.json`, `report.md` and
 adds `groups.json` and `groups.md`: every metric by placement, protection,
 application and workload.
 
+`--candidate FILE` is the offline router-version check: it scores a candidate
+routing overlay beside the current routing config over the same sweep and
+writes `check.json` and `check.md`. It applies nothing and promotes nothing --
+an operator still edits `config/routing.yaml` by hand.
+
 Exit codes: 0 ok; 1 load/usage error (or a declined live run); 2 policy
-violation, empty-set mismatch, post-gate widening or pinned-served mismatch.
+violation, empty-set mismatch, post-gate widening or pinned-served mismatch
+(with `--candidate`: also any ineligible choice by the candidate, whatever the
+tolerances). With `--candidate` only: 3 a named metric regressed beyond its
+tolerance; 4 inconclusive.
 """
 from __future__ import annotations
 
@@ -45,6 +53,7 @@ from app.eval.endpoints import EndpointFileError, load_endpoints  # noqa: E402
 from app.eval.groups import DEFAULT_MIN_GROUP_SIZE  # noqa: E402
 from app.eval.runner import run_bench  # noqa: E402
 from app.eval.strategies import DEFAULT_SECONDS_PER_CASE  # noqa: E402
+from app.eval.version_check import CHECK_METRICS, CheckPlan, parse_tolerances  # noqa: E402
 
 
 def _parser() -> argparse.ArgumentParser:
@@ -78,7 +87,21 @@ def _parser() -> argparse.ArgumentParser:
                         "placement, protection, application and workload")
     p.add_argument("--min-group-size", type=int, default=None,
                    help="fewest cases behind a printed group rate "
-                        f"(default {DEFAULT_MIN_GROUP_SIZE}; needs --group-report)")
+                        f"(default {DEFAULT_MIN_GROUP_SIZE}; needs --group-report "
+                        "or --candidate)")
+    p.add_argument("--candidate", default=None,
+                   help="offline router-version check: a routeforge.router-candidate/v1 "
+                        "file, scored beside the current routing config; writes "
+                        "check.json and check.md and applies nothing")
+    p.add_argument("--tolerance", action="append", default=None, metavar="METRIC=VALUE",
+                   help="with --candidate: the most a metric may worsen, in its own "
+                        f"units; repeatable (metrics: {', '.join(CHECK_METRICS)})")
+    p.add_argument("--check-replicates", type=int, default=None,
+                   help="with --candidate: resamples behind each range (default 1000)")
+    p.add_argument("--check-interval", type=float, default=None,
+                   help="with --candidate: the range's level (default 0.90)")
+    p.add_argument("--check-seed", type=int, default=None,
+                   help="with --candidate: the resampling seed (default 0)")
     return p
 
 
@@ -102,11 +125,32 @@ def _confirm_live(suite: str, endpoints: str, assume_yes: bool) -> bool:
 
 def main(argv: list[str] | None = None) -> int:
     args = _parser().parse_args(argv)
-    if args.min_group_size is not None and not args.group_report:
-        # A flag that would silently do nothing is a usage error.
-        print("feasible_bench: load error: --min-group-size needs --group-report",
+    # A flag that would silently do nothing is a usage error.
+    if args.min_group_size is not None and not (args.group_report or args.candidate):
+        print("feasible_bench: load error: --min-group-size needs --group-report "
+              "or --candidate", file=sys.stderr)
+        return 1
+    check_flags = {"--tolerance": args.tolerance, "--check-replicates": args.check_replicates,
+                   "--check-interval": args.check_interval, "--check-seed": args.check_seed}
+    given = [flag for flag, value in check_flags.items() if value is not None]
+    if given and args.candidate is None:
+        print(f"feasible_bench: load error: {', '.join(given)} needs --candidate",
               file=sys.stderr)
         return 1
+    check_plan: CheckPlan | None = None
+    if args.candidate is not None:
+        defaults = CheckPlan()
+        try:
+            check_plan = CheckPlan(
+                tolerances=parse_tolerances(args.tolerance or []),
+                replicates=(defaults.replicates if args.check_replicates is None
+                            else args.check_replicates),
+                interval=(defaults.interval if args.check_interval is None
+                          else args.check_interval),
+                seed=defaults.seed if args.check_seed is None else args.check_seed)
+        except ValueError as exc:
+            print(f"feasible_bench: load error: {exc}", file=sys.stderr)
+            return 1
     out = args.out or str(
         REPO_ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
 
@@ -126,9 +170,13 @@ def main(argv: list[str] | None = None) -> int:
         seconds_per_case=args.seconds_per_case,
         group_report=args.group_report,
         min_group_size=(DEFAULT_MIN_GROUP_SIZE if args.min_group_size is None
-                        else args.min_group_size)))
+                        else args.min_group_size),
+        candidate_path=args.candidate, check_plan=check_plan))
     if result.exit_code != 1:
         print(f"feasible_bench: results in {result.out_dir} (exit {result.exit_code})")
+    if result.check is not None:
+        print(f"feasible_bench: router-version check: {result.check['verdict']} "
+              "(nothing was applied)")
     return result.exit_code
 
 
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_version_check_run.py tests/eval/test_group_report_run.py tests/eval/test_bench_golden.py tests/eval/test_runner.py tests/eval/test_isolation.py -p no:warnings -rf
```

Expected: `103 passed` (38 + 18 + 6 + 39 + 2). The golden test is E1 for this task; `test_isolation.py` is E12.

- [ ] **Step 5: Run one check by hand**

```bash
cat > /tmp/p2_candidate.yaml <<'EOF'
schema: routeforge.router-candidate/v1
name: latency-only
routing:
  weights: {cost: 0.0, latency: 2.0}
EOF
SP_PII_DETECTION=regex LITELLM_LOCAL_MODEL_COST_MAP=True uv run python scripts/feasible_bench.py \
    --suite eval/suites/starter.jsonl --endpoints eval/endpoints/healthcare-sim.yaml \
    --seed 0 --candidate /tmp/p2_candidate.yaml \
    --tolerance pass_rate=0.02 --tolerance mean_chosen_cost=0.0001 --out /tmp/p2_check
echo "exit $?"
sed -n '11,27p' /tmp/p2_check/check.md
```

Expected: the last two lines of the bench's output are `feasible_bench: results in /tmp/p2_check (exit 3)` and `feasible_bench: router-version check: regressed (nothing was applied)`, then `exit 3`. The excerpt shows `**REGRESSED** (exit 3).`, `cases whose attempt chain changed: 16 of 22`, and under "Hard checks" three zeros (no ineligible attempt, none on an empty set, run exit code 0).

```bash
git status --short config/ eval/
```

Expected: nothing. The check changed no config and no input.

- [ ] **Step 6: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1538 passed`, no `FAILED` line.

- [ ] **Step 7: Commit**

```bash
git add app/eval/runner.py scripts/feasible_bench.py tests/eval/test_version_check_run.py
git commit -m "$(cat <<'EOF'
feat(eval): feasible_bench --candidate, an offline router-version check (P2 metrics)

Scores a candidate routing overlay as extra router arms over the same sweep
and writes check.json and check.md. Exit 2 on any ineligible choice whatever
the tolerances, 3 when a named metric is beyond its tolerance, 4 when
inconclusive. It applies nothing and promotes nothing: no config write, no
admin call, no serving hook. Without the flag no output changes.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Documentation (spec §13)

**Files:**
- Modify: `docs/guides/feasible-route-bench.md`
- Modify: `docs/research/feasible-route-bench.md`
- Modify: `docs/research/arxiv.md`
- Modify: `docs/roadmap/priorities-1-3-status.md`
- Modify: `docs/backlog/bugs.md`

**Interfaces:**
- Consumes: the behaviour built in Tasks 1–7. Every number in the research notes is from a simulated run and is reproduced by Step 1 before any file is edited.
- Produces: the operator guide for the two flags; research §22; the paper notes; the roadmap row; backlog B67.

**Rules for this task.** The docs say what was built and what was not. They do not call the check a promotion gate without saying in the same breath that it promotes nothing. They do not read a simulated pass rate as a property of a model. Churn and calibration are described as designed and not built. `report.md`'s "Deferred metrics" section is unchanged by this plan (it is part of the pinned default output), and the docs say so.

- [ ] **Step 1: Reproduce the numbers the notes quote**

Nothing is committed in this step. The script runs the starter suite, simulated, with the group report at seed 0 and with three candidates at simulation seeds 0–9.

Create `/tmp/p2_results.py`:

````python
"""Collect the simulated results quoted in the research notes (not committed)."""
import asyncio
import json
import tempfile
from pathlib import Path

import yaml

from app.eval.runner import run_bench
from app.eval.version_check import CheckPlan

SUITE, EPS = "eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml"
tmp = Path(tempfile.mkdtemp())


def cand(name, routing):
    p = tmp / f"{name}.yaml"
    p.write_text(yaml.safe_dump({"schema": "routeforge.router-candidate/v1", "name": name,
                                 "routing": routing}))
    return str(p)


def run(out, **kw):
    return asyncio.run(run_bench(SUITE, EPS, out_dir=str(tmp / out), **kw))


r = run("g", seed=0, group_report=True)
stamp = json.loads((tmp / "g" / "run.json").read_text())
print({k: stamp[k] for k in ("suite_hash", "endpoints_hash", "base_routing_policy_version",
                             "registry_version", "seed", "seconds_per_case", "attempt_cap",
                             "n_cases")})
print((tmp / "g" / "groups.md").read_text().split("## Every group")[1])

TOL = {"pass_rate": 0.02, "mean_chosen_cost": 0.0001}
CANDS = {
    "same": {"weights": {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2}},
    "latency-only": {"weights": {"cost": 0.0, "latency": 2.0}},
    "no-cheap-tier": {"complexity_thresholds": {"cheap_max": 0.0}},
}
for name, routing in CANDS.items():
    print("==", name, routing)
    for seed in range(10):
        r = run(f"{name}-{seed}", seed=seed, candidate_path=cand(name, routing),
                check_plan=CheckPlan(tolerances=TOL))
        c = r.check
        f, p = c["overall"]["mean_chosen_cost"], c["overall"]["pass_rate"]
        print(seed, "exit", r.exit_code, c["verdict"], "changed", c["changed"]["choice_changed"],
              "| cost", f["baseline"], f["candidate"], f["worsening"], f["lo"], f["hi"],
              f["change"], f["state"],
              "| pass", p["baseline"], p["candidate"], p["worsening"], p["lo"], p["hi"],
              p["change"], p["state"], "| viol", c["hard"]["candidate_violations"],
              "| regressed", len(c["regressed"]), "inconclusive", len(c["inconclusive"]))
        if seed == 0 and name == "latency-only":
            print((tmp / f"{name}-0" / "check.md").read_text())
            print("id", c["candidate"]["id"])
````

```bash
SP_PII_DETECTION=regex LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=. uv run python /tmp/p2_results.py 2>/dev/null | grep -E "^(== |[0-9] exit|\{'suite_hash)"
```

Expected, exactly (the runs are seeded; the first line is the stamp):

```
{'suite_hash': '3339c7735b5d22df84dbd3ec470ee91513c06c0d50ea033e2a99ac79a95d8199', 'endpoints_hash': '04eccd8f75060e554eac7584ee3238667cb0d61773f42551837c738c855c4d96', 'base_routing_policy_version': '6e6fe2027862', 'registry_version': '86294e62adea', 'seed': 0, 'seconds_per_case': 30.0, 'attempt_cap': 3, 'n_cases': 22}
== same {'weights': {'quality': 1.0, 'cost': 0.6, 'latency': 0.3, 'availability': 0.2}}
0 exit 0 pass changed 0 | cost 0.001490454545 0.001490454545 0.0 0.0 0.0 none within_tolerance | pass 0.909090909091 0.909090909091 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
1 exit 0 pass changed 0 | cost 0.001487727273 0.001487727273 0.0 0.0 0.0 none within_tolerance | pass 0.772727272727 0.772727272727 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
2 exit 0 pass changed 0 | cost 0.001490454545 0.001490454545 0.0 0.0 0.0 none within_tolerance | pass 0.909090909091 0.909090909091 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
3 exit 0 pass changed 0 | cost 0.001475 0.001475 0.0 0.0 0.0 none within_tolerance | pass 0.818181818182 0.818181818182 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
4 exit 0 pass changed 0 | cost 0.001487727273 0.001487727273 0.0 0.0 0.0 none within_tolerance | pass 0.954545454545 0.954545454545 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
5 exit 0 pass changed 0 | cost 0.001490454545 0.001490454545 0.0 0.0 0.0 none within_tolerance | pass 0.909090909091 0.909090909091 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
6 exit 0 pass changed 0 | cost 0.001492272727 0.001492272727 0.0 0.0 0.0 none within_tolerance | pass 0.954545454545 0.954545454545 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
7 exit 0 pass changed 0 | cost 0.001494090909 0.001494090909 0.0 0.0 0.0 none within_tolerance | pass 0.863636363636 0.863636363636 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
8 exit 0 pass changed 0 | cost 0.001485 0.001485 0.0 0.0 0.0 none within_tolerance | pass 1.0 1.0 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
9 exit 0 pass changed 0 | cost 0.001476818182 0.001476818182 0.0 0.0 0.0 none within_tolerance | pass 0.909090909091 0.909090909091 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
== latency-only {'weights': {'cost': 0.0, 'latency': 2.0}}
0 exit 3 regressed changed 16 | cost 0.001490454545 0.002596363636 0.001105909091 0.000753636364 0.001469545455 worse regressed | pass 0.909090909091 1.0 -0.090909090909 -0.181818181818 0.0 not_detectable within_tolerance | viol 0 | regressed 8 inconclusive 0
1 exit 3 regressed changed 16 | cost 0.001487727273 0.002614545455 0.001126818182 0.000775454545 0.001501363636 worse regressed | pass 0.772727272727 0.863636363636 -0.090909090909 -0.272727272727 0.090909090909 not_detectable inconclusive | viol 0 | regressed 8 inconclusive 5
2 exit 3 regressed changed 16 | cost 0.001490454545 0.0026 0.001109545455 0.00076 0.001478636364 worse regressed | pass 0.909090909091 0.954545454545 -0.045454545455 -0.181818181818 0.090909090909 not_detectable inconclusive | viol 0 | regressed 8 inconclusive 5
3 exit 3 regressed changed 16 | cost 0.001475 0.002592727273 0.001117727273 0.000762727273 0.00149 worse regressed | pass 0.818181818182 0.863636363636 -0.045454545455 -0.181818181818 0.090909090909 not_detectable inconclusive | viol 0 | regressed 8 inconclusive 5
4 exit 3 regressed changed 16 | cost 0.001487727273 0.002596363636 0.001108636364 0.000761363636 0.001472272727 worse regressed | pass 0.954545454545 1.0 -0.045454545455 -0.136363636364 0.0 not_detectable within_tolerance | viol 0 | regressed 8 inconclusive 0
5 exit 3 regressed changed 16 | cost 0.001490454545 0.002581818182 0.001091363636 0.000750454545 0.001452727273 worse regressed | pass 0.909090909091 0.954545454545 -0.045454545455 -0.181818181818 0.090909090909 not_detectable inconclusive | viol 0 | regressed 11 inconclusive 2
6 exit 3 regressed changed 16 | cost 0.001492272727 0.002603636364 0.001111363636 0.000761363636 0.001475 worse regressed | pass 0.954545454545 0.954545454545 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 8 inconclusive 0
7 exit 3 regressed changed 16 | cost 0.001494090909 0.0026 0.001105909091 0.000755909091 0.001469545455 worse regressed | pass 0.863636363636 0.954545454545 -0.090909090909 -0.181818181818 0.0 not_detectable within_tolerance | viol 0 | regressed 8 inconclusive 0
8 exit 3 regressed changed 16 | cost 0.001485 0.002596363636 0.001111363636 0.000761363636 0.001475 worse regressed | pass 1.0 1.0 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 8 inconclusive 0
9 exit 3 regressed changed 16 | cost 0.001476818182 0.002596363636 0.001119545455 0.000761363636 0.001499545455 worse regressed | pass 0.909090909091 1.0 -0.090909090909 -0.227272727273 0.0 not_detectable within_tolerance | viol 0 | regressed 8 inconclusive 0
== no-cheap-tier {'complexity_thresholds': {'cheap_max': 0.0}}
0 exit 0 pass changed 8 | cost 0.001490454545 0.001440909091 -4.9545455e-05 -7.6818182e-05 -2.5e-05 better within_tolerance | pass 0.909090909091 1.0 -0.090909090909 -0.181818181818 0.0 not_detectable within_tolerance | viol 0 | regressed 0 inconclusive 0
1 exit 0 pass changed 8 | cost 0.001487727273 0.001438181818 -4.9545455e-05 -7.4545455e-05 -2.6363636e-05 better within_tolerance | pass 0.772727272727 0.863636363636 -0.090909090909 -0.181818181818 0.0 not_detectable within_tolerance | viol 0 | regressed 0 inconclusive 0
2 exit 0 pass changed 8 | cost 0.001490454545 0.001443636364 -4.6818182e-05 -7.1818182e-05 -2.5e-05 better within_tolerance | pass 0.909090909091 0.954545454545 -0.045454545455 -0.136363636364 0.0 not_detectable within_tolerance | viol 0 | regressed 0 inconclusive 0
3 exit 3 regressed changed 8 | cost 0.001475 0.001432727273 -4.2272727e-05 -6.4545455e-05 -2.2727273e-05 better within_tolerance | pass 0.818181818182 0.772727272727 0.045454545455 0.0 0.136363636364 not_detectable regressed | viol 0 | regressed 5 inconclusive 0
4 exit 0 pass changed 8 | cost 0.001487727273 0.001440909091 -4.6818182e-05 -7.0909091e-05 -2.4545455e-05 better within_tolerance | pass 0.954545454545 1.0 -0.045454545455 -0.136363636364 0.0 not_detectable within_tolerance | viol 0 | regressed 0 inconclusive 0
5 exit 4 inconclusive changed 8 | cost 0.001490454545 0.001442727273 -4.7727273e-05 -7.5e-05 -2.5e-05 better within_tolerance | pass 0.909090909091 0.954545454545 -0.045454545455 -0.181818181818 0.090909090909 not_detectable inconclusive | viol 0 | regressed 0 inconclusive 5
6 exit 0 pass changed 8 | cost 0.001492272727 0.001448181818 -4.4090909e-05 -6.7727273e-05 -2.3181818e-05 better within_tolerance | pass 0.954545454545 0.954545454545 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
7 exit 0 pass changed 8 | cost 0.001494090909 0.001447272727 -4.6818182e-05 -7.1818182e-05 -2.4090909e-05 better within_tolerance | pass 0.863636363636 0.909090909091 -0.045454545455 -0.136363636364 0.0 not_detectable within_tolerance | viol 0 | regressed 0 inconclusive 0
8 exit 0 pass changed 8 | cost 0.001485 0.001440909091 -4.4090909e-05 -6.7727273e-05 -2.3181818e-05 better within_tolerance | pass 1.0 1.0 0.0 0.0 0.0 none within_tolerance | viol 0 | regressed 0 inconclusive 0
9 exit 0 pass changed 8 | cost 0.001476818182 0.00143 -4.6818182e-05 -7.1818182e-05 -2.4090909e-05 better within_tolerance | pass 0.909090909091 0.954545454545 -0.045454545455 -0.136363636364 0.0 not_detectable within_tolerance | viol 0 | regressed 0 inconclusive 0
```

Each line is: simulation seed, exit code, verdict, chains changed; then for the mean chosen cost and for the pass rate — baseline, candidate, worsening, range low, range high, `change`, `state`; then the candidate's violations and the number of regressed and inconclusive figures.

If the stamp's `suite_hash`, `endpoints_hash` or `base_routing_policy_version` differs, `eval/` or `config/` changed since this plan was written: the numbers below may then differ too. If any other line differs, stop and report it. Do not edit the docs to match a different number without saying so.

How the docs use them: research §22.5.2 is the three `0 exit …` lines; §22.5.3 is the count of verdicts per candidate over the ten seeds (10 / 0 / 0, 0 / 10 / 0, 8 / 1 / 1), with seeds 3 and 5 of `no-cheap-tier` quoted; "74% more" is `0.002596 / 0.001490`.

- [ ] **Step 2: The operator guide**

Patch (save as a file and run `git apply <file>`):

````diff
diff --git a/docs/guides/feasible-route-bench.md b/docs/guides/feasible-route-bench.md
index e0fc529..ecf894f 100644
--- a/docs/guides/feasible-route-bench.md
+++ b/docs/guides/feasible-route-bench.md
@@ -186,6 +186,14 @@ the gateway is built to protect, so decide where `results/` lives before you kee
 | `--fixed SLUG` | adds `fixed:<slug>`: always that endpoint when it is eligible, otherwise `cheapest` (recorded as `fallback_used`) |
 | `--parity-tol X` | maximum pass-rate gap for the headline cost claim (default 0.02) |
 | `--keep-text` | stores completion text in `matrix.jsonl` |
+| `--group-report` | also writes `groups.json` and `groups.md`: every metric by placement, protection, application and workload (§6.2) |
+| `--min-group-size N` | the fewest cases behind a printed group figure (default 5, integer ≥ 1). Needs `--group-report` or `--candidate` |
+| `--candidate FILE` | the offline router-version check: scores a candidate routing overlay beside the current routing config and writes `check.json` and `check.md` (§6.3). **It applies nothing** |
+| `--tolerance METRIC=VALUE` | with `--candidate`, repeatable: the most a metric may worsen, in its own units (§6.3) |
+| `--check-replicates N`, `--check-interval X`, `--check-seed N` | with `--candidate`: the resamples behind each range (default 1000), its level (default 0.90) and the resampling seed (default 0) |
+
+A flag that needs another flag is a usage error (exit 1) when given alone: it would
+otherwise silently do nothing.
 
 **The sweep disables confidence escalation.** Each `(case, endpoint)` is a single-shot
 measurement: one backend call. With a pinned endpoint, the gateway's confidence
@@ -323,14 +331,167 @@ The first answer, simulated, is in
 [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §9. Changing
 the production weight is a decision for the owner, not something this tool does.
 
+### 6.2 The group report (`--group-report`)
+
+```bash
+uv run python scripts/feasible_bench.py \
+    --suite eval/suites/starter.jsonl \
+    --endpoints eval/endpoints/healthcare-sim.yaml \
+    --seed 0 --group-report --out results/groups
+```
+
+Two more files, and no change to the other five:
+
+- `groups.json`: for **every** strategy, every metric overall and by four dimensions.
+- `groups.md`: the fixed text, the group sizes, the worst group per metric for every
+  strategy, and the full table for `router_online`.
+
+**Dimensions.** `placement`, `protection` and `workload` are the case's tags of those
+names. `application` is `tags.app` when you set it, otherwise `policy-` plus a fingerprint
+of the case's `app_policy`: cases with the same policy are one application. There is no
+tenant dimension, because the bench runs every case under one tenant id. A case with no
+value for a dimension is in that dimension's `(untagged)` group, so every case is in
+exactly one group per dimension.
+
+**Every figure has its own `n`.** `0.75 (n = 8)` is a rate over eight cases. The
+denominators differ by metric:
+
+| Metric | Over |
+|---|---|
+| `violation_rate`, `empty_set_mismatch_rate` | every case in the group |
+| `pass_rate`, `quality_regret` | cases with a non-empty feasible set |
+| `floor_miss_rate` | those where at least one eligible endpoint passed |
+| `cost_regret`, `latency_regret` | those where the **served** answer passed |
+
+So a group of eight cases can print a cost regret that is over six, or over one. To
+combine groups, weight each figure by its own `n`, not by the group's case count.
+
+**Small groups print no rate.** A figure over fewer than `--min-group-size` cases
+(default 5) is `insufficient cases (n = k)`. The counts beside it — cases, violations,
+empty-set mismatches — are always printed: a violation in a one-case group stays visible.
+Five is a floor against reading a rate off two cases. A group above it is still small.
+
+**The worst group** is, per metric, the worst printed figure across all groups of all
+dimensions, with the group's size. When every printed group has the same value the table
+says so (`0 in every printed group (7)`) and names none. It names a set of cases. It does
+not order models or endpoints.
+
+`metrics.json` still carries its older `groups` and `worst_groups` blocks, unchanged. The
+report restates the same three tags with denominators and adds `application`.
+
+### 6.3 The router-version check (`--candidate`)
+
+**It applies nothing and promotes nothing.** It answers one question before **you** edit
+`config/routing.yaml`: scored offline on this suite, is this routing config worse than the
+current one? RouteForge has no in-process promotion path, and this is not one. It writes
+two files and an exit code.
+
+Write the routing keys you are thinking of changing:
+
+```yaml
+# candidate.yaml
+schema: routeforge.router-candidate/v1
+name: latency-only            # optional, a label for the report
+routing:
+  weights: {cost: 0.0, latency: 2.0}
+```
+
+```bash
+uv run python scripts/feasible_bench.py \
+    --suite eval/suites/starter.jsonl \
+    --endpoints eval/endpoints/healthcare-sim.yaml \
+    --seed 0 --candidate candidate.yaml \
+    --tolerance pass_rate=0.02 --tolerance mean_chosen_cost=0.0001 \
+    --out results/check
+echo $?    # 3: mean_chosen_cost regressed
+```
+
+- The **baseline** is your routing config as the bench loads it. The **candidate** is that
+  config with your keys laid over it, in memory. A mapping (`weights`, `signals`, …) is
+  merged one level over the base value; a scalar replaces.
+- Keys you may state: `weights`, `tier_quality`, `complexity_thresholds`, `risk_floors`,
+  `strategy`, `signals`, `breaker`. Any other `routing.yaml` key is **refused with the
+  reason** — for example `confidence_escalation_threshold`, because the sweep runs with
+  escalation off and a change to it would move no figure. A candidate the deployment could
+  not boot with (an unknown strategy, a tier that does not exist) is a load error.
+- The candidate is scored as three more router arms (`router_cold:candidate`,
+  `router_online:candidate`, `router_warm_prior:candidate`) **over the same sweep** as
+  every other strategy: the same cases, the same eligible sets, the same seed, the same
+  answer from every endpoint. The check compares `router_online` with
+  `router_online:candidate`.
+
+**Hard checks. No tolerance applies.** If the candidate names an endpoint outside a case's
+eligible set, or any endpoint for a case whose feasible set is empty, the verdict is
+`hard_fail` and the exit code is `2`, whatever tolerances you gave. So does a run that
+fails any of the bench's own integrity checks (§7).
+
+**Tolerances.** `--tolerance METRIC=VALUE` names a metric and the most it may worsen, in
+the metric's own units: `pass_rate=0.02` is two points, `mean_chosen_cost=0.0001` is a
+hundredth of a cent per request, `latency_regret=50` is 50 ms. The metrics are
+`pass_rate`, `quality_regret`, `cost_regret`, `latency_regret`, `floor_miss_rate`,
+`mean_chosen_cost`, `wasted_attempts` and `exhausted_rate`. A metric you do not name is
+shown and not judged.
+
+**Reading a row of `check.md`.**
+
+| Column | Meaning |
+|---|---|
+| `worsening` | candidate minus baseline **in the direction that is worse** (for `pass_rate`, baseline minus candidate). Positive is worse |
+| `range` | the 90% interval of that difference when the suite's cases are resampled, the same cases for both arms |
+| `change` | what the range says: `none` (the two arms agree case by case), `worse`, `better`, `not_detectable` (the range includes 0) |
+| `state` | what the tolerance says, below |
+
+| State | When |
+|---|---|
+| `regressed` | the worsening is beyond the tolerance. The range does not excuse it |
+| `inconclusive` | the worsening is within the tolerance, but the range reaches beyond it |
+| `within_tolerance` | the worsening **and** the whole range are within the tolerance |
+| `insufficient_cases` | fewer than `--min-group-size` cases behind the figure: no figure at all |
+| `not_checked` | no tolerance was named for this metric |
+
+A range that includes 0 decides nothing on its own. A figure can be `regressed` and
+`not_detectable` at once: the point estimate is beyond your tolerance, and the suite is
+too small to tell the difference from none. The check does not pass it.
+
+The same figures are judged in every group with at least `--min-group-size` cases. A
+candidate that is unchanged overall and worse for one workload is `regressed`.
+
+**Verdict and exit code.**
+
+| Verdict | Exit | Meaning |
+|---|---|---|
+| `hard_fail` | `2` | an ineligible choice, or a failed integrity check |
+| `regressed` | `3` | a named metric is worse than its tolerance, overall or in a large enough group |
+| `inconclusive` | `4` | nothing is beyond a tolerance at the point estimate, but something could not be shown to be within it |
+| `pass` | `0` | every named metric is within its tolerance, range included |
+| `no_hard_failure` | `0` | no tolerance was named: only the hard checks ran |
+
+On a suite of 22 cases, expect `inconclusive` often. That is the honest answer: the suite
+cannot show the difference is small. Use a larger suite or a wider tolerance.
+
+**What a verdict is not.** It is for this suite, this endpoint file, this simulation seed
+and this case order; the same candidate can pass at one seed and fail at another (research
+§22.5 has an example). On the simulated backend quality is an input, so a `pass_rate`
+verdict there measures the harness. The range covers which cases are in the suite, not
+whether the suite resembles your traffic. Every `check.md` opens with this text.
+
+`metrics.json`'s `exit_code` stays the bench run's own (0 or 2). The process exit code is
+in `check.json` and `run.json`.
+
 ## 7. Exit codes
 
 | Code | Meaning |
 |---|---|
 | `0` | ok |
-| `1` | load or usage error: a bad suite, endpoint file or variants file, an unknown `--fixed` slug, a missing detector, a declined `--live` prompt, a `--seconds-per-case` too small for this run's endpoint count or attempt cap, or **a gate exception on any case** |
+| `1` | load or usage error: a bad suite, endpoint file or variants file, an unknown `--fixed` slug, a missing detector, a declined `--live` prompt, a `--seconds-per-case` too small for this run's endpoint count or attempt cap, **a gate exception on any case**, a bad `--candidate` file (a key the check cannot evaluate, an unknown key, a config the deployment could not boot with), a bad `--tolerance`, `--min-group-size` or `--check-*` value, or one of those flags without the flag it needs |
 | `2` | policy violation or integrity failure: any strategy with `violation_rate > 0`, any sweep call (or empty-set probe) that served an ineligible model, any post-gate eligible set that is not a subset of the pre-gate set, any `expect_empty` disagreement, any `pinned_mismatches` (a pinned call served another endpoint; impossible by construction, so any occurrence is a harness integrity bug), or a signal arm's own leak guard firing (`LeakageError` -- an arm was asked to score a case out of order; also a harness integrity bug) |
 
+| `3` | **only with `--candidate`**: no hard failure, and a named metric is worse than its tolerance (§6.3) |
+| `4` | **only with `--candidate`**: no hard failure and no regression, but a named metric could not be shown to be within its tolerance (§6.3) |
+
+With `--candidate`, `2` also covers any ineligible choice by the candidate. No tolerance
+changes that.
+
 `sweep_generate_mismatches` does not by itself change the exit code: those rows are
 already error rows, scored as failed calls rather than credited to the endpoint.
 
````

- [ ] **Step 3: The research notes**

Patch:

````diff
diff --git a/docs/research/feasible-route-bench.md b/docs/research/feasible-route-bench.md
index f082f32..45be3f5 100644
--- a/docs/research/feasible-route-bench.md
+++ b/docs/research/feasible-route-bench.md
@@ -48,6 +48,12 @@ dataset. §13 records the dataset's model, its invariants, and what it can and c
 support. It contains no results: nothing has been deployed, so there is no production
 data yet.
 
+**Update 2026-10-07 (§22).** Two of the metrics §6.5 lists as missing are now reported,
+both opt-in and both offline: a group report with denominators and a minimum group size,
+and an offline router-version check that compares a candidate routing config with the
+current one over the same sweep. Calibration and churn robustness are designed and not yet
+built. §22's numbers are simulated.
+
 ---
 
 ## 0. File before you publish
@@ -555,6 +561,22 @@ The router emits no confidence, so calibration is undefined. Policy or endpoint
 for a run. Noninterference is P1a's harness. All three are named as deferred in every
 `report.md`.
 
+*Status 2026-10-07.* Still true of the code, and `report.md` still says so. What changed:
+
+- **Worst-group results** now have sizes, denominators, a minimum group size and an
+  application dimension (§22.1). That was §3's metric 6, half built.
+- **An offline router-version check** exists (§22.2).
+- **Calibration is designed, not built.** The router does emit no confidence. The gateway
+  does read one: the backend's own figure for an answer, which the escalation threshold is
+  compared with, and which is recorded nowhere. That is the only thing there is to
+  calibrate; the design measures it and says in fixed text that RouteForge has no
+  calibrated routing confidence. The simulated backend reports 1.0 for every answer, so in
+  simulation the result is, by construction, "not measurable".
+- **Churn robustness is designed, not built**: one declared change to the policy or the
+  registry at a time, the suite re-run, violations required to stay 0.
+
+Both designs are in `docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` §6 and §9.
+
 ### 6.6 No live result
 
 No `--live` run has been made. `spark-live.example.yaml` is loaded by a test with
@@ -3764,3 +3786,283 @@ Figures in this list are synthetic; the method for each is in §21.5.5.
   make it O(1) per row (B66).
 - Everything in §21.5.1–§21.5.3 and §21.5.5 is synthetic. Every figure on recorded traffic
   is **TO MEASURE** (§21.5.4).
+
+---
+
+## 22. Group report and the offline router-version check (P2 evaluation metrics, 2026-10-07)
+
+**Status:** built, offline, opt-in. Spec
+`docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` (§4, §5, §7, §8); plan
+`docs/superpowers/plans/2026-10-07-p2-eval-metrics.md`. Branch `feat/p2-eval-metrics`.
+Operator guide: `docs/guides/feasible-route-bench.md` §6.2–§6.3. **Every number in §22.5 is
+from a simulated run.** Read §22.6 before writing a sentence that claims a result.
+
+This is the first of two plans. The second builds churn robustness and confidence
+calibration (handoff §18 metrics 6–8); neither exists yet.
+
+### 22.1 Per-case scores and the group report
+
+**Per-case scores.** `strategy_metrics` computed every aggregate inside one loop. That loop
+is now `oracle.case_scores`, which returns one `CaseScore` per case: what the case adds to
+each aggregate, or `None` where the case is outside that aggregate's denominator.
+`strategy_metrics` is `aggregate_scores(case_scores(...))`, rounded, and its output did not
+move (§22.4, E1). A group figure is an aggregate over a subset of scores; a resample is an
+aggregate over a multiset of them.
+
+**Denominators.** For a strategy `s` and a set of cases `G`:
+
+| Metric | Denominator |
+|---|---|
+| `violation_rate`, `empty_set_mismatch_rate` | `|G|` |
+| `pass_rate`, `quality_regret` | `|{c ∈ G : E(c) ≠ ∅}|` |
+| `floor_miss_rate` | `|{c ∈ G : P(c) ≠ ∅}|` |
+| `cost_regret` | `|{c ∈ G : s(c) ∈ P(c)}|` |
+| `latency_regret` | the same, with both latencies known |
+
+Every printed figure carries its denominator `n`. For a partition of the suite into
+groups, `overall = Σ n_g · value_g / Σ n_g` with `n_g` the **figure's** denominator in
+group `g`. Weighting by the group's case count is correct for the two metrics over `|G|`
+and wrong for the others whenever groups differ in how many of their cases are empty or
+passed.
+
+*Worked example* (eight cases; `tests/eval/test_groups.py`): cost regret is 0.15 over four
+passing cases in one group of five and 0.6 over one passing case in a group of three. The
+overall figure is `1.2 / 5 = 0.24`. By denominator: `(4 × 0.15 + 1 × 0.6) / 5 = 0.24`. By
+case count: `(5 × 0.15 + 3 × 0.6) / 8 = 0.31875`.
+
+**Dimensions.** `placement`, `protection`, `workload` (author tags, as §3's metric 6) and
+`application`: `tags.app`, else a fingerprint of the case's `app_policy`. The bench has one
+tenant id, so there is no tenant dimension. A case with no value is in `(untagged)`: every
+case is in exactly one group per dimension.
+
+**Small groups.** A figure with `n` below `--min-group-size` (default 5, inclusive) is
+`insufficient_cases` and has no value. The rule is on the figure's denominator. Counts of
+violations and empty-set mismatches are never suppressed.
+
+**Worst group.** Per metric, the worst printed figure across all groups of all dimensions,
+with its `n` and the group's size, and how many printed groups share that value. When all
+do, none is named.
+
+### 22.2 The router-version check
+
+**What is compared.** A candidate is an overlay of `routing.yaml` keys
+(`routeforge.router-candidate/v1`), laid over the deployment's routing config in memory. It
+is scored like a weight variant: three more router arms over **the same sweep matrix**.
+The check compares `router_online` (baseline) with `router_online:candidate`.
+
+This is a clean comparison because of two properties of the code, both checked before the
+design was written: `feasible_set` reads no routing weight or threshold, so the eligible
+set `E(c)` is the same for both arms; and a matrix row is one pinned call to one endpoint,
+so its quality, cost and latency do not depend on routing config either. Every difference
+between the two arms is the candidate's.
+
+**Worsening.** For a metric `m` over a set of cases, `w = m_cand − m_base`, or
+`m_base − m_cand` for `pass_rate`. Positive is worse.
+
+**Range.** A paired case bootstrap. Cases are sorted by id; replicate `b` draws `n`
+positions with replacement (`uncertainty.draw_indices(seed, b, scope, n)`); both arms'
+aggregates are recomputed on that one draw; `[lo, hi]` is the equal-tailed percentile
+interval of the `B` values of `w` (default `B = 1000`, level 0.90). `uncertainty.py`'s
+draw and interval functions are reused; its replicate loop, built around simulator rows
+and application strata, is not.
+
+**Decision rule**, for tolerance `τ ≥ 0`, in order:
+
+1. `w` undefined → `undefined`
+2. `w > τ` → `regressed`
+3. no range, or `hi > τ` → `inconclusive`
+4. otherwise → `within_tolerance`
+
+The point estimate alone fails a figure (2). The range can only withhold a pass (3). `lo`
+is not an input: `change ∈ {none, worse, better, not_detectable}` is reported beside the
+state and never decides it.
+
+**Verdict.** `hard_fail` (exit 2) if the candidate has any violation — an attempt outside
+`E(c)`, including any attempt where `E(c) = ∅` — or the run's own integrity code is not 0.
+Otherwise the worst state over the judged figures: `regressed` (3), `inconclusive` (4),
+`pass` (0); `no_hard_failure` (0) when no tolerance was named. Judged figures: every named
+metric overall, and in every group with at least `--min-group-size` cases whose figure is
+not itself `insufficient_cases`.
+
+### 22.3 What it deliberately is not
+
+- **Not a promotion path.** Nothing is applied. The modules import no admin route, no
+  application store and no HTTP client and call nothing that writes; the only writes are
+  the runner's, under `--out` (E11). The handoff's "promotion gate" is, here, a check an
+  operator runs before they edit a file.
+- **Not a ranking.** The report orders nothing by size and names no model or endpoint as
+  better.
+- **Not a test of routing keys the bench cannot evaluate.** The escalation threshold and
+  temperature, the escalation and failover bounds, the tier list and the pool are refused
+  with a reason. Accepting them would produce a false "no regression".
+- **Not corrected for multiplicity.** More groups mean more judged figures and more chances
+  of `regressed` or `inconclusive`. For a check that is the safe direction; it is not a
+  calibrated error rate.
+- **Not over seeds.** One run is one simulation seed and one case order (§22.5.3).
+
+### 22.4 Invariants, and the test that pins each
+
+All under `tests/eval/`. "Falsified" means the rule was broken on purpose and the named
+test failed (§22.5.4).
+
+| # | Invariant | Pinned by | Falsified? |
+|---|---|---|---|
+| E1 | With no new flag the output is byte-identical to the pre-slice tree | `test_bench_golden.py::test_e1_default_output_is_byte_identical` (four cases, written before any product code of the slice; the commit is in the fixture); `::test_e1_the_default_run_writes_exactly_the_five_files`; `test_group_report_run.py::test_e1_the_flag_adds_two_files_and_changes_no_other` | **Yes** (M15) |
+| E2 | Same inputs and flags give byte-identical new files | `test_group_report_run.py::test_e2_the_group_report_is_byte_identical_twice`; `test_version_check_run.py::test_e2_the_check_is_byte_identical_twice` | n/a |
+| E3 | Each case is in exactly one group per dimension; sizes sum to the suite | `test_groups.py::test_e3_every_case_is_in_exactly_one_group_per_dimension`, `::test_e3_group_sizes_sum_to_the_total`, `::test_e3_an_untagged_case_is_counted_not_dropped`; `test_group_report_run.py::test_e3_the_run_partitions_its_cases` | Yes (M8) |
+| E4 | Overall = the groups combined by each figure's own denominator; by case count is wrong for a conditional metric | `test_groups.py::test_e4_overall_is_the_denominator_weighted_combination` (7 metrics × 4 dimensions), `::test_e4_weighting_by_case_count_is_wrong_for_a_conditional_metric` | n/a (the second test is the counter-example) |
+| E5 | Below the minimum: a count and no rate; on the figure's denominator; counts never suppressed; inclusive | `test_groups.py::test_e5_*` (five tests); `test_group_report_run.py::test_e5_an_injected_violation_shows_in_its_group_however_small`; `test_version_check.py::test_e5_*` (two tests) | Yes (M6, M9) |
+| E6 | Group figures and the worst group match the hand-computed example | `test_groups.py::test_e6_*` (five tests) | Yes (M7) |
+| E7 | Any ineligible choice by the candidate is `hard_fail`, exit 2, whatever the tolerances | `test_version_check.py::test_e7_*` (three tests); `test_version_check_run.py::test_e7_an_injected_ineligible_candidate_exits_2_whatever_the_tolerances`, `::test_e7_a_candidate_that_serves_an_empty_feasible_set_is_a_hard_fail` | **Yes** (M3, M4): the run tests inject the fault |
+| E8 | Candidate = baseline: worsening 0, range `[0, 0]`, exit 0 at zero tolerance | `test_version_check.py::test_e8_identical_arms_are_no_change_everywhere`; `test_version_check_run.py::test_e8_a_candidate_that_restates_the_config_passes_with_zero_tolerance` | n/a |
+| E9 | The check does not depend on case order | `test_version_check.py::test_e9_the_document_does_not_depend_on_case_order` | Yes (M2) |
+| E10 | Worsening, the rule and the boundary match hand-computed examples | `test_version_check.py::test_e10_*` (four tests) | Yes (M1, M5) |
+| E11 | Nothing is applied | `test_version_check_run.py::test_e11_the_check_writes_only_under_out_and_touches_no_config`, `::test_e11_the_new_modules_have_no_way_to_apply_anything`, `::test_e11_no_admin_route_or_setting_was_added` | n/a |
+| E12 | No serving module imports `app.eval` | `test_isolation.py::test_no_serving_module_imports_app_eval` (unchanged) | Yes (its own scanner test) |
+| E13 | No ranking, recommendation or promotion vocabulary | `test_groups.py::test_e13_no_key_or_sentence_ranks_or_recommends`; `test_version_check.py::test_e13_no_key_or_sentence_ranks_recommends_or_promotes` | n/a |
+| E14 | The fixed text is the spec's and opens every new report | `test_groups.py::test_e14_*` (three tests); `test_version_check.py::test_e14_*` (two tests) | n/a |
+| E15 | Nothing stated is silently ignored | `test_candidate.py::test_e15_*`; `test_version_check_run.py::test_e15_*`, `::test_cli_a_check_flag_without_a_candidate_is_refused`, `::test_cli_a_bad_check_value_is_refused`; `test_group_report_run.py::test_cli_a_minimum_without_the_report_is_refused` | Yes (M10) |
+| E16 | The candidate adds arms and changes nothing else | `test_version_check_run.py::test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm`; `test_candidate.py::test_the_harness_settings_are_never_mutated` | Yes (M13) |
+| E17 | A range that includes 0 decides nothing by itself | `test_version_check.py::test_e17_the_lower_end_of_a_range_never_enters_the_decision`, `::test_e17_a_regression_whose_range_includes_zero_is_both_things` | n/a |
+
+### 22.5 Results (simulated)
+
+**Stamp.** Starter suite × `healthcare-sim.yaml`, simulated, seed 0 unless stated,
+`seconds_per_case` 30, `attempt_cap` 3, 22 cases. `suite_hash` `3339c773…`,
+`endpoints_hash` `04eccd8f…`, `base_routing_policy_version` `6e6fe2027862`,
+`registry_version` `86294e62adea`. Quality in this profile is a hand-written probability
+per endpoint and workload (§6.1).
+
+#### 22.5.1 Group report, `router_online`, seed 0
+
+Minimum 5. `-` is `insufficient cases`, with the `n` it had.
+
+| dimension | group | cases | violations | pass rate | cost regret (USD) | latency regret (ms) |
+|---|---|---|---|---|---|---|
+| overall | all | 22 | 0 | 0.909 (n = 22) | 0.000032 (n = 20) | 30.7 (n = 20) |
+| placement | any | 8 | 0 | 0.75 (n = 8) | 0.000107 (n = 6) | 102.5 (n = 6) |
+| placement | us_only | 8 | 0 | 1.0 (n = 8) | 0 (n = 8) | 0 (n = 8) |
+| placement | onprem_only | 4 | 0 | - (n = 4) | - (n = 4) | - (n = 4) |
+| placement | escalated | 2 | 0 | - (n = 2) | - (n = 2) | - (n = 2) |
+| workload | faq | 8 | 0 | 0.75 (n = 8) | 0.000107 (n = 6) | 102.5 (n = 6) |
+| workload | claim_summary | 10 | 0 | 1.0 (n = 10) | 0 (n = 10) | 0 (n = 10) |
+| workload | onprem_required | 4 | 0 | - (n = 4) | - (n = 4) | - (n = 4) |
+| protection | enforced | 22 | 0 | 0.909 (n = 22) | 0.000032 (n = 20) | 30.7 (n = 20) |
+| application | 4 groups | 8 / 8 / 4 / 2 | 0 | two printed, two not | | |
+
+What this shows, and all it shows:
+
+- **Every violation count is 0 in every group**, including the groups too small for a
+  rate.
+- **All of `router_online`'s regret on this suite is in one group**: the eight `faq` cases
+  (which are also the `placement: any` cases and one application). That is §6.7 again — the
+  tier rule excludes cheaper `mid` endpoints for short requests — now visible per group.
+- **Two of four placement groups and one of three workload groups print no rate.** The
+  `onprem_only` group, the one the product's main guarantee is about, has four cases.
+  Before this slice a "worst group" could be one of those. The suite needs more cases
+  there before a group claim about placement can be made.
+- **Only one protection posture is in the starter suite** (`enforced`). There is no
+  protection breakdown to read.
+- Check on the weighted identity: pass rate `(8 × 0.75 + 8 × 1.0 + 4 × p₁ + 2 × p₂) / 22 =
+  0.909` gives `4p₁ + 2p₂ = 6`, i.e. both unprinted groups passed every case; cost regret
+  `(6 × 0.000107 + 8 × 0 + …) / 20 = 0.000032` with the unprinted groups at 0. ✓
+
+#### 22.5.2 Three candidates, seed 0
+
+Tolerances `pass_rate=0.02`, `mean_chosen_cost=0.0001`; 1000 resamples, level 0.90,
+resampling seed 0.
+
+| Candidate | Chains changed | Violations | Mean chosen cost: baseline → candidate, worsening `[range]` | Pass rate worsening `[range]` | Verdict |
+|---|---|---|---|---|---|
+| `same` (restates the shipped weights) | 0 of 22 | 0 | 0.001490 → 0.001490, 0 `[0, 0]` | 0 `[0, 0]` | `pass` (0) |
+| `latency-only` (`weights: {cost: 0, latency: 2}`) | 16 of 22 | 0 | 0.001490 → 0.002596, +0.001106 `[0.000754, 0.001470]`, `worse` | −0.091 `[−0.182, 0]`, `not_detectable` | `regressed` (3) |
+| `no-cheap-tier` (`complexity_thresholds: {cheap_max: 0}`) | 8 of 22 | 0 | 0.001490 → 0.001441, −0.000050 `[−0.000077, −0.000025]`, `better` | −0.091 `[−0.182, 0]`, `not_detectable` | `pass` (0) |
+
+- `same` is the negative control: an overlay that changes nothing changes nothing, to the
+  last digit.
+- `latency-only` moves 16 of 22 choices and makes **no** ineligible choice: the eligible
+  set is not the router's to change. It costs 74% more per request. The cost figure is
+  `regressed` overall and in all seven groups with enough cases.
+- `no-cheap-tier` is the tier rule of §6.7 seen from the other side: with the cheap tier's
+  upper bound at 0, short requests target `mid`, where this profile's cheaper endpoints
+  are, and the candidate is cheaper. On simulated quality that proves nothing about
+  whether it is a good idea.
+
+#### 22.5.3 The same candidates at simulation seeds 0–9
+
+| Candidate | `pass` | `regressed` | `inconclusive` | `hard_fail` |
+|---|---|---|---|---|
+| `same` | 10 | 0 | 0 | 0 |
+| `latency-only` | 0 | 10 | 0 | 0 |
+| `no-cheap-tier` | 8 | 1 (seed 3) | 1 (seed 5) | 0 |
+
+- A large effect is stable: `latency-only`'s cost worsening is +0.00109 to +0.00113 at
+  every seed, and its range never comes near the tolerance.
+- **A verdict that rests on simulated pass rate is not stable.** `no-cheap-tier` passes at
+  eight seeds, is `regressed` at seed 3 (pass rate 0.818 → 0.773, one case, worsening
+  0.045 against a tolerance of 0.02, range `[0, 0.136]`) and `inconclusive` at seed 5
+  (worsening −0.045, range `[−0.182, 0.091]`). The pass or fail of a case is a seeded draw
+  against a hand-written probability, so this is the harness's own noise, read through
+  22 cases. It is the measured form of the fixed caveat "the verdict is conditional on …
+  this simulation seed".
+- No candidate at any seed made an ineligible choice.
+
+#### 22.5.4 The detectors, falsified
+
+Fifteen single-rule mutations, each run against the named test file; all fifteen were
+caught (2026-10-07, scratch tree, not committed):
+
+| # | Mutation | Caught by |
+|---|---|---|
+| M1 | the tolerance boundary made exclusive | `test_e10_the_decision_rule_branch_by_branch` |
+| M2 | the arms not sorted by case id | `test_e9_the_document_does_not_depend_on_case_order` |
+| M3 | candidate violations ignored by the hard check | `test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances` |
+| M4 | the run's exit code ignored by the hard check | `test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate` |
+| M5 | the range never withholds a pass | `test_e10_the_decision_rule_branch_by_branch` |
+| M6 | the minimum made exclusive | `test_e6_group_figures_match_the_hand_computed_example` |
+| M7 | the worst `pass_rate` taken as the highest | `test_e6_the_worst_group_is_named_with_its_size` |
+| M8 | untagged cases dropped | `test_e3_an_untagged_case_is_counted_not_dropped` |
+| M9 | counts suppressed in a small group | `test_e5_a_violation_in_a_one_case_group_is_still_counted` |
+| M10 | a key the bench cannot evaluate accepted | `test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason` |
+| M11 | the two arms resampled separately | `test_the_draw_is_paired_and_seeded` |
+| M12 | the check's exit code not returned | `test_a_candidate_that_costs_more_than_the_tolerance_exits_3` |
+| M13 | the candidate built on the shared settings object | `test_the_harness_settings_are_never_mutated` |
+| M14 | a floor miss scored where no endpoint passes (the per-case refactor) | `test_each_case_matches_the_hand_computed_matrix` |
+| M15 | the same mutation, against the golden pin alone | `test_e1_default_output_is_byte_identical[starter_seed1]` |
+
+M15 is why the golden pin has a fourth case. At seed 0 no starter case is infeasible at
+floor, and the first three cases did not notice M14; at seed 1 some are, and the pin does.
+
+#### 22.5.5 Live
+
+**TO MEASURE.** No `--live` run has been made with either flag. Wanted (arxiv.md E11):
+
+- the group report on a live suite with at least five cases in every placement and
+  protection group, and at least two endpoints of different placement;
+- the check for a weights candidate and a thresholds candidate on live endpoints, with the
+  verdict over repeated runs (a live run has no seed: repeat it);
+- the bootstrap range's coverage — split a larger suite in two and count how often one
+  half's range contains the other half's worsening.
+
+### 22.6 Limits: what this does NOT show
+
+- **Simulated quality is an input** (§6.1). Every pass rate, quality regret and floor-miss
+  figure in §22.5, and every verdict that rests on one, measures the harness. Cost and
+  latency are simulated too: prices from the file over real token counts, latency from a
+  declared mean and jitter.
+- **The router comparison is mostly the tier rule** (§6.7). Both non-trivial candidates
+  above act through it or around it.
+- **22 synthetic, single-turn cases.** Five of the twelve groups are below five cases; the
+  group that matters most (`onprem_only`) is one of them.
+- **A group is a label the case author wrote.** A case tagged `placement: any` with an
+  `onprem_only` policy is grouped as `any`. Grouping by the compiled policy is B67.
+- **The range is over cases only.** It does not cover the simulation seed, the case order
+  (the online arm learns in suite order), the endpoint file, or whether the suite
+  resembles any real traffic. Its coverage has not been measured.
+- **A percentile bootstrap on 22 cases is coarse.** A pass rate moves in steps of 1/22;
+  several ranges above end exactly at 0.
+- **No multiplicity correction** (§22.3).
+- **One arm is compared.** `router_online`; the cold and warm-prior candidate arms are in
+  `metrics.json` and are not judged.
+- **Calibration and churn are not built.** Handoff §18 metrics 6, 7 and 8 are still not
+  reported by any tool.
````

- [ ] **Step 4: The paper notes**

Patch:

````diff
diff --git a/docs/research/arxiv.md b/docs/research/arxiv.md
index 49f3b9a..1700321 100644
--- a/docs/research/arxiv.md
+++ b/docs/research/arxiv.md
@@ -185,6 +185,7 @@ literature doesn't model".
   closing a circuit (`2d0d029`); `-0.0` regret (`820bec2`).
 - **Limits:** simulated quality is an input, not a measurement; deterministic graders;
   synthetic single-turn cases; one designed outage scenario; no live breaker run.
+- **Group report and router-version check** (FB §22, built 2026-10-07): §4.6.
 
 ### 3.4 P2b-2 content-free outcome records (PR #68–#70)
 
@@ -672,7 +673,61 @@ literature doesn't model".
 
 #### 4.4.2 Still pending: failover/escalation, breaker state, spend/RPM admission — *spec pending*
 ### 4.5 Administrator review workflow — *spec pending* (closes HO §37 P3 "admin approval")
-### 4.6 P2 evaluation metrics: confidence calibration, churn robustness, worst-group, router-version promotion gate — *spec pending* (FB §6.5 lists these as not yet demonstrated)
+### 4.6 P2 evaluation metrics (spec `docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md`; HO §18 metrics 6–9, §19, §36 Phase 2)
+
+Two plans. **Built (FB §22):** per-case scores, the group report, the offline
+router-version check. **Designed, not built:** churn robustness and confidence calibration.
+
+**What the built half lets the paper claim.**
+
+- *A feasible-set evaluation can report per-group results honestly at small n.* Each figure
+  carries its own denominator; the overall figure is the groups combined by those
+  denominators (a worked counter-example shows case-count weights are wrong for a
+  conditional metric: 0.31875 against 0.24); a figure over fewer than a stated minimum
+  prints a count and no rate, while violation counts are never suppressed (FB §22.1).
+- *Two router configurations can be compared with everything else held fixed.* Because the
+  eligible set is derived before scoring and a sweep row is a pinned call, a candidate
+  routing config scored as extra arms of one run shares the cases, the eligible sets, the
+  seed and every endpoint outcome with the baseline (FB §22.2). This is a property of the
+  eligibility-first architecture, and worth stating as one.
+- *A change to the router cannot produce a policy violation.* A candidate that moves 16 of
+  22 choices makes no ineligible choice at any of ten seeds (FB §22.5.2–§22.5.3). The check
+  treats any such choice as a hard failure that no tolerance can buy back, and a test
+  injects one to show the detector fires.
+- *An explicit, conservative decision rule*: the point estimate alone fails a figure; a
+  bootstrap range can only withhold a pass; a range that includes 0 decides nothing by
+  itself (FB §22.2). Fifteen single-rule mutations are each caught by a named test
+  (FB §22.5.4).
+
+**What it does not let the paper claim.**
+
+- Nothing about routing **quality**: on the simulated backend quality is an input
+  (FB §6.1), and the measured consequence is in FB §22.5.3 — the same candidate passes at
+  eight seeds, regresses at one and is inconclusive at one, on pass rate alone.
+- Nothing about **which** router configuration to use. The check judges against tolerances
+  an operator sets; it recommends nothing.
+- No per-group claim about `onprem_only` placement or about protection posture from the
+  starter suite: those groups are below the minimum, or there is only one (FB §22.5.1).
+- No statement that the bootstrap range has its nominal coverage (unmeasured), and no
+  multiplicity control.
+- No "promotion gate" in the sense of something that promotes. Say "offline pre-change
+  check"; nothing is applied (CLAUDE.md rule 4).
+
+**Still to build, and what each must deliver for the paper.**
+
+- *Churn robustness* (spec §6): a declared set of single changes to the policy and the
+  registry; per change, violations (the headline: 0), newly empty feasible sets and whether
+  each was rejected with a conflicting core, the share of choices that moved, and regret
+  deltas; and each change's expected effect on eligibility checked ("a tightening never
+  widens") — the bench-side witness of the P1 monotonicity result.
+- *Confidence calibration* (spec §9). The finding to state first is negative and
+  structural: **RouteForge's router emits no confidence.** The only confidence in the
+  system is the backend's figure for one answer (a temperature-softened geometric-mean
+  token probability, or a constant 0.9 when the provider returns no logprobs), which the
+  gateway's escalation threshold reads and nothing records. The measurement is that
+  figure's reliability and the threshold's operating point on live endpoints. In
+  simulation it is not measurable by construction (the simulated backend reports 1.0), and
+  the paper should say that rather than omit the metric.
 
 ---
 
@@ -713,7 +768,13 @@ literature doesn't model".
 | E10d | Peak and time-average utilisation against bucket length (10 / 60 / 300 / 3600 s) | §4.3, FB §21.5.4 | **TO MEASURE** on recorded traffic (synthetic: FB §21.5.2 F3) |
 | E10e | Share of rows whose bucket changes with the arrival estimate; error of write time − recorded latency (a sum of step latencies) against a logged request start | §4.3, FB §21.5.4 | **TO MEASURE** |
 | E10f | Error of giving a moved request its new endpoint's mean service time, by request size | §4.3, FB §21.5.4 | **TO MEASURE** |
-| E11+ | fidelity, P2 metrics, R2/R3/R4 | §4–§5 | specs pending |
+| E11a | Group report on the simulated starter suite: figures with denominators, groups below the minimum | §4.6, FB §22.5.1 | done (simulated, seed 0) |
+| E11b | Router-version check: negative control, a cost-regressing candidate, a tier-threshold candidate; verdicts at seeds 0–9 | §4.6, FB §22.5.2–§22.5.3 | done (simulated) |
+| E11c | The check's detectors under single-rule mutation | §4.6, FB §22.5.4 | done (15 of 15 caught) |
+| E11d | Group report and check on live endpoints, ≥ 5 cases per placement and protection group, two placements | §4.6, FB §22.5.5 | **TO MEASURE** |
+| E11e | Coverage of the paired case bootstrap on split halves of a larger suite | §4.6, FB §22.5.5 | **TO MEASURE** |
+| E11f | Churn robustness and confidence calibration | §4.6 | not built (second P2 plan) |
+| E12+ | fidelity, R2/R3/R4 | §4–§5 | specs pending |
 
 **Data caveat for every live number:** the Spark serves one app (`kit-gtm`, Qwen Code), all
 models priced $0 (FB §17.4). The DGX lab (the test target from 2026-10-06) has one priced
@@ -729,6 +790,12 @@ before writing the evaluation section.
 - Tier selection is length-based; the "router" contribution is mostly the eligibility seam,
   not learned routing (FB §6.7).
 - Simulated quality is an input (FB §6.1); deterministic graders; synthetic single-turn cases.
+- The router-version check (§4.6) judges one arm on one suite, one simulation seed and one
+  case order. Its range is a percentile bootstrap over 22 cases: coarse, of unmeasured
+  coverage, and blind to the seed — the same candidate gets three different verdicts
+  across ten seeds when the verdict rests on simulated pass rate (FB §22.5.3). Group
+  results rest on labels the case author wrote, and five of twelve groups are below the
+  five-case minimum. No multiplicity correction is made.
 - Single tenant, single seed live runs; $0 prices on the Spark.
 - Replay is first-decision only (until §4.4), with frozen signals; cached tokens not recorded.
 - Uncertainty ranges (§4.2) are sampling variability under an independence assumption that
````

- [ ] **Step 5: The roadmap**

Patch:

````diff
diff --git a/docs/roadmap/priorities-1-3-status.md b/docs/roadmap/priorities-1-3-status.md
index e5698c7..b79356e 100644
--- a/docs/roadmap/priorities-1-3-status.md
+++ b/docs/roadmap/priorities-1-3-status.md
@@ -42,6 +42,7 @@ Handoff §37 Priority 1 criteria met:
 | Circuit breaker | #67 | Cross-request, per-endpoint circuit breaker, on by default; never lowers the target tier (closes B45) | `app/gateway/` |
 | **P2b-2** routing outcomes | #68, #69, #70 | Content-free `routing_outcomes` and `outcome_feedback` records, export, and report; nothing reads them back into routing | `docs/reference/routing-outcomes.md` |
 | **P2b-3** quality calibration | #71 | Offline, human-approved quality calibration: the estimator, a replay diff, `scripts/quality_recommend.py` (B54 fixed alongside) | `docs/reference/quality-calibration.md` |
+| **P2 metrics (1 of 2)** group report and router-version check | pending | `feasible_bench.py --group-report`: every metric by placement, protection, application and workload, each figure with its own denominator; a figure over fewer than `--min-group-size` cases prints a count and no rate; violation counts are never suppressed. `feasible_bench.py --candidate FILE`: an **offline** router-version check — a candidate routing overlay scored beside the current config over the same sweep; any ineligible choice is exit 2 whatever the tolerances; a named metric beyond its tolerance is exit 3; inconclusive is exit 4. **It applies nothing and promotes nothing.** Both opt-in; without the flags the bench's output is byte-identical, now pinned by a golden test. | `docs/guides/feasible-route-bench.md` §6.2–§6.3, research §22 |
 
 Handoff §37 Priority 2 criteria met:
 - ineligible endpoints are masked before scoring;
@@ -153,13 +154,21 @@ uncertainty (P3c-3) and capacity (P3c-4) are built.
   - a workload taxonomy that clients actually send;
   - FeasibleRouteBench wired in as a pre-apply gate;
   - the served model credited on cache hits.
-- **Router-version promotion gates** (handoff §19, §36 Phase 2). The bench can compare
-  strategies offline, but nothing compares alternative router versions or blocks a promotion
-  on it.
-- **Handoff §18 metrics not yet reported:**
-  - calibration of routing confidence;
-  - policy-churn and endpoint-churn robustness as standing metrics;
-  - worst-group results by placement, protection, tenant and workload.
+- **Handoff §18 metrics not yet reported** — designed
+  (`docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` §6, §9), not built; the
+  second plan of that spec:
+  - **policy-churn and endpoint-churn robustness**: one declared change at a time, the
+    suite re-run, violations required to stay 0;
+  - **calibration of confidence**. The router emits no confidence. The one that exists is
+    the backend's figure for an answer, which the gateway's escalation threshold reads and
+    nothing records; that is what the design measures. In simulation it is constant, so
+    the simulated result will be a stated "not measurable".
+
+Built from this list on 2026-10-07 (§1): worst-group results (as the group report; "tenant"
+is `application`, because the bench has one tenant id) and the router-version check. The
+check is the handoff's "promotion gate" in the only form the product boundary allows: it
+blocks nothing in process and promotes nothing. An operator, or their CI, reads its exit
+code before changing `config/routing.yaml`.
 
 ### 2.3 Open owner decisions
 
@@ -190,6 +199,11 @@ uncertainty (P3c-3) and capacity (P3c-4) are built.
 2. The simulator slices are built. The next Priority 3 item is the administrator review
    workflow (§2.1 item 3), or a fidelity gap from item 4; each starts with a design
    brainstorm, then a spec, plan and subagent build.
+2a. The next Priority 2 item is the second plan of the P2 metrics spec (churn robustness and
+   confidence calibration). Write that plan from the spec's §6 and §9; do not start from
+   the handoff. Before quoting a group figure or a check verdict, quote the suite, the
+   seed and each figure's `n`; a verdict that rests on simulated pass rate changes with
+   the seed (research §22.5.3).
 3. Before quoting any simulator number from the Spark, check both baseline agreements, and
    exclude rows recorded before B55. Rejected rows recorded before B60 are excluded
    automatically (`missing_routing_tokens`). A range from `--uncertainty` is sampling
````

- [ ] **Step 6: The backlog**

Patch:

````diff
diff --git a/docs/backlog/bugs.md b/docs/backlog/bugs.md
index 2ee314d..f0e46da 100644
--- a/docs/backlog/bugs.md
+++ b/docs/backlog/bugs.md
@@ -127,6 +127,50 @@ the audit trail in that sweep's task report.
 > [`docs/reference/routing-observability.md`](../reference/routing-observability.md) §4
 > for the operator-facing contract.
 
+## B67 — FEATURE: P2 evaluation-metric follow-ups — confidence on the record, policy-derived groups, a check over several seeds, a multiplicity note, unevaluable candidate keys, a baseline overlay  🔴 OPEN
+
+**Severity:** low (the group report and the router-version check work as designed; these
+are what they do not yet cover).
+**Found:** 2026-10-07, the P2 evaluation-metrics design
+(`docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` §14).
+
+1. **The answer confidence is recorded nowhere.** `GenerationResult.confidence` — the
+   backend's figure, `min(1, exp(mean token logprob / T))`, or `DEFAULT_CONFIDENCE = 0.9`
+   when the provider returns no logprobs — is read once, by `Gateway.run`, to decide an
+   escalation, and then dropped. It is not on the `step="router"` `DecisionRecord`, not in
+   `sp.scoring`, not on the response certificate and not in `routing_outcomes`. So the
+   escalation threshold cannot be calibrated on recorded traffic, and a 0.9 that is a real
+   measurement cannot be told from the default. **Smallest change:** two content-free
+   fields on the router `DecisionRecord` and the `routing_outcomes` row — the confidence,
+   and its source (`logprob` / `default` / `none`). A serving change (a column, an `ALTER`,
+   the export, the trace attribute), with its own review. A score-margin analysis from
+   `sp.scoring` (does the gap between the top two candidates predict regret?) belongs with
+   it. The bench-side measurement is the second plan of the P2 metrics spec (§9) and needs
+   none of this.
+2. **Groups are author tags, not the compiled policy.** A case tagged `placement: any`
+   whose policy is `onprem_only` is grouped as `any`. The run already computes the compiled
+   residency and protection axes per case; group by those, beside or instead of the tags.
+3. **A check is one simulation seed and one case order.** The same candidate got three
+   different verdicts across seeds 0–9 when the verdict rested on simulated pass rate
+   (research §22.5.3). A `--check-seeds 0-9` that runs the sweep per seed and reports the
+   spread of verdicts is the obvious next step; decide first what the exit code is when
+   they disagree.
+4. **No multiplicity correction.** A check with two tolerances judges up to
+   `2 × (1 + groups)` figures. Either state the count beside the verdict or correct for it.
+5. **Candidate keys the bench cannot evaluate**, the escalation threshold above all. It
+   needs an escalation-aware sweep (a second call per case at the next tier), which the
+   bench deliberately does not make because it doubles a live run's cost.
+6. **A baseline overlay.** The baseline is always the routing config as loaded. Comparing
+   two candidates with each other needs one of them applied to a scratch config directory.
+7. **The starter suite is too small for its own group report.** Five of twelve groups are
+   below five cases, `onprem_only` among them, and there is one protection posture
+   (research §22.5.1). A suite with at least five cases per placement and protection group
+   is a data task, not a code one.
+
+See also B46 (weak test oracles) and research §14.4 (what online exploration would need;
+"FeasibleRouteBench wired in as a pre-apply gate" there is this check, run by hand or in
+CI — still not wired into anything).
+
 ## B66 — FEATURE: capacity follow-ups — a declaration on the onboarded endpoint, a time-block bootstrap, a service-time model for moved requests, a p95-bucket figure, a faster accumulation, a backend-time field  🔴 OPEN
 
 **Severity:** low (the capacity section works from a hand-written file; these make it less
````

- [ ] **Step 7: Check the links and the vocabulary**

```bash
uv run python scripts/check_docs_links.py
```

Expected: `0 broken internal link(s), 0 stale inbound path(s), 0 purged-vocabulary hit(s).`

```bash
grep -n -i "promotion gate" docs/guides/feasible-route-bench.md docs/roadmap/priorities-1-3-status.md docs/research/feasible-route-bench.md | grep -v -i -E "nothing|not a|no promotion|handoff|promotes"
```

Expected: no output. Every mention of a "promotion gate" in the three files a reader meets first is in a sentence that says nothing is promoted or attributes the phrase to the handoff.

- [ ] **Step 8: Commit**

```bash
git add docs/guides/feasible-route-bench.md docs/research/feasible-route-bench.md docs/research/arxiv.md docs/roadmap/priorities-1-3-status.md docs/backlog/bugs.md
git commit -m "$(cat <<'EOF'
docs: the group report and the offline router-version check (P2 metrics)

Guide §6.2-§6.3 and the two new exit codes; research §22 with definitions,
invariants and their tests, simulated results at seeds 0-9 and live results
TO MEASURE; arxiv §4.6 and experiments E11a-E11f; the roadmap row; B67.
Churn robustness and confidence calibration are described as designed, not
built.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: The whole suite, the falsification run, the hand-computed check, and the hand-off note

**Files:** none modified, unless a failure caused by this branch is found.

**Interfaces:**
- Consumes: everything above.
- Produces: a clean suite run, fifteen killed mutants, the sanity-check output, and the note for whoever opens the pull request.

- [ ] **Step 1: Run the whole suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest -q -rf -p no:warnings 2>&1 | tee /tmp/rf_full_suite.log | tail -n 30
```

Expected: it takes more than ten minutes. Under `-q` pytest prints no summary line in this repo, so read the `-rf` section: there is either no `FAILED` line at all, or exactly one, which is pre-existing and not caused by this branch:
`tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app`.
It fails with `ModuleNotFoundError` in an environment without the Presidio extra, on the base commit too.

```bash
grep -c "^FAILED" /tmp/rf_full_suite.log; grep "^FAILED" /tmp/rf_full_suite.log
```

Expected: `0`, or `1` and that test. Any other `FAILED` line is this branch's: fix it under the task that owns the file, re-run that task's tests, and re-run this step.

- [ ] **Step 2: Re-run the invariant tests by name**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_bench_golden.py tests/eval/test_oracle_scores.py tests/eval/test_groups.py tests/eval/test_group_report_run.py tests/eval/test_candidate.py tests/eval/test_version_check.py tests/eval/test_version_check_run.py tests/eval/test_isolation.py -p no:warnings -rf
```

Expected: `224 passed` (6 + 10 + 54 + 18 + 35 + 61 + 38 + 2). This run is E1–E17.

- [ ] **Step 3: Falsify the detectors**

Nothing is committed in this step. The script breaks one rule at a time, runs the test file that should notice, and restores the file.

Create `/tmp/p2_mutants.py`:

````python
"""Falsification for the P2 evaluation metrics, first plan. Not committed.

Breaks one rule at a time, runs the test file that should notice, and
restores the file. A mutant that survives means an invariant is not pinned.
Run from the repository root with a clean working tree.
"""
import os
import subprocess
import sys

VC, GR, CA, RU, OR = ("app/eval/version_check.py", "app/eval/groups.py",
                      "app/eval/candidate.py", "app/eval/runner.py", "app/eval/oracle.py")
FLOOR_OLD = ("        if oracle is not None and oracle[\"passing\"]:\n"
             "            floor_miss = 0.0 if chosen_passed else 1.0")
FLOOR_NEW = ("        if oracle is not None:\n"
             "            floor_miss = 0.0 if chosen_passed else 1.0")
MUTANTS = [
    ("M1 the tolerance boundary made exclusive", VC,
     "    if round(w, _DP) > tol:\n        return \"regressed\"",
     "    if round(w, _DP) >= tol:\n        return \"regressed\"",
     "tests/eval/test_version_check.py"),
    ("M2 the arms not sorted by case id", VC,
     "    base = sorted(baseline, key=lambda s: s.case_id)\n"
     "    cand = sorted(candidate, key=lambda s: s.case_id)",
     "    base = list(baseline)\n"
     "    cand = sorted(candidate, key=lambda s: s.case_id)\n"
     "    cand = [next(c for c in cand if c.case_id == b.case_id) for b in base]",
     "tests/eval/test_version_check.py"),
    ("M3 candidate violations ignored by the hard check", VC,
     "    hard_failed = violations > 0 or run_exit_code != 0",
     "    hard_failed = run_exit_code != 0", "tests/eval/test_version_check.py"),
    ("M4 the run's exit code ignored by the hard check", VC,
     "    hard_failed = violations > 0 or run_exit_code != 0",
     "    hard_failed = violations > 0", "tests/eval/test_version_check.py"),
    ("M5 the range never withholds a pass", VC,
     "    if hi is None or round(hi, _DP) > tol:\n        return \"inconclusive\"",
     "    if False:\n        return \"inconclusive\"", "tests/eval/test_version_check.py"),
    ("M6 the minimum made exclusive", GR, "    if n < min_size or value is None:",
     "    if n <= min_size or value is None:", "tests/eval/test_groups.py"),
    ("M7 the worst pass_rate taken as the highest", GR,
     "        target = min(values) if metric in LOWER_IS_WORSE else max(values)",
     "        target = max(values)", "tests/eval/test_groups.py"),
    ("M8 untagged cases dropped", GR,
     "    for case_id in case_ids:\n"
     "        groups.setdefault(keys[case_id][dimension], []).append(case_id)",
     "    for case_id in case_ids:\n"
     "        if keys[case_id][dimension] == UNTAGGED:\n"
     "            continue\n"
     "        groups.setdefault(keys[case_id][dimension], []).append(case_id)",
     "tests/eval/test_groups.py"),
    ("M9 counts suppressed in a small group", GR,
     "        \"violations\": sum(1 for s in scores if s.violation),",
     "        \"violations\": (sum(1 for s in scores if s.violation)\n"
     "                       if len(scores) >= min_size else 0),",
     "tests/eval/test_groups.py"),
    ("M10 a key the bench cannot evaluate accepted", CA, "        if key in NOT_EVALUABLE:",
     "        if False:", "tests/eval/test_candidate.py"),
    ("M11 the two arms resampled separately", VC,
     "                    worsening(metric, _mean_at(bcol, idx), _mean_at(ccol, idx)))",
     "                    worsening(metric, _mean_at(bcol, idx), _mean_at(\n"
     "                        ccol, draw_indices(plan.seed, b, scope + \"x\", n))))",
     "tests/eval/test_version_check.py"),
    ("M12 the check's exit code not returned", RU,
     "        exit_code = check_doc[\"exit_code\"]\n", "        pass\n",
     "tests/eval/test_version_check_run.py"),
    ("M13 the candidate built on the shared settings object", CA,
     "    settings = copy.deepcopy(harness)\n", "    settings = harness\n",
     "tests/eval/test_candidate.py"),
    ("M14 a floor miss scored where no endpoint passes", OR, FLOOR_OLD, FLOOR_NEW,
     "tests/eval/test_oracle_scores.py"),
    ("M15 the same, against the golden pin alone", OR, FLOOR_OLD, FLOOR_NEW,
     "tests/eval/test_bench_golden.py"),
]

env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
survived = 0
for name, path, old, new, tests in MUTANTS:
    src = open(path, encoding="utf-8").read()
    if src.count(old) != 1:
        print(f"{name}: ANCHOR NOT FOUND ({src.count(old)})")
        survived += 1
        continue
    open(path, "w", encoding="utf-8").write(src.replace(old, new))
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", *tests.split(), "-p", "no:warnings", "-rf"],
            env=env, capture_output=True, text=True)
    finally:
        open(path, "w", encoding="utf-8").write(src)
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    killed = proc.returncode != 0 and bool(failed)
    survived += not killed
    first = failed[0].split(" - ")[0].removeprefix("FAILED ") if failed else ""
    print(f"{name}: {'killed' if killed else 'SURVIVED'} ({len(failed)} failed)  {first}")
print(f"{len(MUTANTS) - survived} of {len(MUTANTS)} killed")
sys.exit(1 if survived else 0)
````

```bash
git status --short
uv run python /tmp/p2_mutants.py
git status --short
```

Expected: both `git status --short` print nothing (the tree is clean before, and every file is restored after). The script takes about three minutes and prints fifteen lines that each say `killed`, then:

```
15 of 15 killed
```

The first failing test per mutant:

| Mutant | First test that fails |
|---|---|
| M1 the tolerance boundary made exclusive | `test_version_check.py::test_e10_the_decision_rule_branch_by_branch[0.1-0.1-0.1-within_tolerance]` |
| M2 the arms not sorted by case id | `test_version_check.py::test_e9_the_document_does_not_depend_on_case_order` |
| M3 candidate violations ignored by the hard check | `test_version_check.py::test_e7_one_ineligible_choice_is_a_hard_fail_whatever_the_tolerances[tolerances0]` |
| M4 the run's exit code ignored by the hard check | `test_version_check.py::test_e7_a_failed_run_is_a_hard_fail_even_with_a_clean_candidate` |
| M5 the range never withholds a pass | `test_version_check.py::test_e10_the_decision_rule_branch_by_branch[0.05-0.2-0.1-inconclusive]` |
| M6 the minimum made exclusive | `test_groups.py::test_e6_group_figures_match_the_hand_computed_example` |
| M7 the worst pass_rate taken as the highest | `test_groups.py::test_e6_the_worst_group_is_named_with_its_size` |
| M8 untagged cases dropped | `test_groups.py::test_e3_an_untagged_case_is_counted_not_dropped` |
| M9 counts suppressed in a small group | `test_groups.py::test_e5_a_violation_in_a_one_case_group_is_still_counted` |
| M10 a key the bench cannot evaluate accepted | `test_candidate.py::test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason[confidence_escalation_threshold]` |
| M11 the two arms resampled separately | `test_version_check.py::test_the_draw_is_paired_and_seeded` |
| M12 the check's exit code not returned | `test_version_check_run.py::test_a_candidate_that_costs_more_than_the_tolerance_exits_3` |
| M13 the candidate built on the shared settings object | `test_candidate.py::test_the_harness_settings_are_never_mutated` |
| M14 a floor miss scored where no endpoint passes | `test_oracle_scores.py::test_each_case_matches_the_hand_computed_matrix` |
| M15 the same, against the golden pin alone | `test_bench_golden.py::test_e1_default_output_is_byte_identical[starter_seed1]` |

A line that says `SURVIVED` or `ANCHOR NOT FOUND` means an invariant is not pinned, or the code differs from this plan: stop and report it. Do not weaken the mutant.

- [ ] **Step 4: The hand-computed check — three examples whose answer is known in advance, and the starter suite**

Nothing is committed in this step. The first part recomputes the spec's §12.1–§12.3 through the real functions; the second runs the starter suite, simulated, with the group report and one candidate.

Known in advance (Tasks 3 and 6 carry the full derivations):

| | By hand |
|---|---|
| §12.1 pass rate | faq 4/5 = 0.8 (n = 5); claim 1/3 (n = 3); overall 5/8 = 0.625 |
| §12.1 cost regret | faq 0.6/4 = 0.15 (n = 4); claim 0.6 (n = 1) — not printed at a minimum of 3; overall 1.2/5 = 0.24 |
| §12.1 combining the groups | by denominator `(4 × 0.15 + 1 × 0.6)/5 = 0.24`; by case count `(5 × 0.15 + 3 × 0.6)/8 = 0.31875` |
| §12.1 worst pass rate | `workload = claim`, 1/3, n = 3 |
| §12.2 constant shift of 0.25 | worsening 0.25, range `[0.25, 0.25]`; within tolerance at 0.25, regressed at 0.2 |
| §12.3 one dear case in six | worsening `10/6 − 1 = 0.666667`; range starts at −1 (the dear case not drawn) and can reach `3 × 10/6 − 1 = 4` (drawn three times); regressed at 0.5 **and** not detectable |

Create `/tmp/p2_sanity.py`:

````python
"""Sanity check for the P2 evaluation metrics, first plan. Not committed.

Part 1 recomputes the three hand-computed examples of the spec (§12.1-§12.3)
through the real functions. Part 2 runs the starter suite, simulated, and
prints the figures the research notes quote (§22.5).
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ["SP_PII_DETECTION"] = "regex"

import yaml  # noqa: E402

from app.eval.groups import strategy_groups  # noqa: E402
from app.eval.oracle import CaseScore  # noqa: E402
from app.eval.runner import run_bench  # noqa: E402
from app.eval.version_check import CheckPlan, compare  # noqa: E402


def score(case_id, passed, cost_regret=None, floor_miss="same", chosen=1.0) -> CaseScore:
    miss = (1.0 - passed) if floor_miss == "same" else floor_miss
    return CaseScore(case_id, False, False, False, miss is None, float(passed), 1.0 - passed,
                     miss, cost_regret, None, chosen, 0.0, 0.0)


def fig(f: dict) -> str:
    return f"{f['value']:.6g} (n = {f['n']})" if "value" in f else f"insufficient (n = {f['n']})"


# --- 12.1: eight cases, two workloads ------------------------------------------
scores = [score("c1", 1, 0.2), score("c2", 1, 0.0), score("c3", 0), score("c4", 1, 0.4),
          score("c5", 1, 0.0), score("c6", 1, 0.6), score("c7", 0),
          score("c8", 0, floor_miss=None)]
keys = {s.case_id: {"workload": "faq" if s.case_id <= "c5" else "claim",
                    "placement": "any" if s.case_id <= "c4" else "onprem_only",
                    "protection": "enforced", "application": "app"} for s in scores}
for minimum in (1, 3):
    block = strategy_groups(scores, keys, minimum)
    w = block["dimensions"]["workload"]
    print(f"12.1 min {minimum}: pass_rate faq {fig(w['faq']['metrics']['pass_rate'])}, "
          f"claim {fig(w['claim']['metrics']['pass_rate'])}, "
          f"overall {fig(block['overall']['metrics']['pass_rate'])}")
    print(f"12.1 min {minimum}: cost_regret faq {fig(w['faq']['metrics']['cost_regret'])}, "
          f"claim {fig(w['claim']['metrics']['cost_regret'])}, "
          f"overall {fig(block['overall']['metrics']['cost_regret'])}")
block = strategy_groups(scores, keys, 1)
w = block["dimensions"]["workload"]
by_n = sum(g["metrics"]["cost_regret"]["n"] * g["metrics"]["cost_regret"]["value"]
           for g in w.values()) / block["overall"]["metrics"]["cost_regret"]["n"]
by_cases = sum(g["n_cases"] * g["metrics"]["cost_regret"]["value"] for g in w.values()) / 8
print(f"12.1 cost_regret combined by denominator {by_n:.6g}, by case count {by_cases:.6g}")
worst = strategy_groups(scores, keys, 3)["worst"]["pass_rate"]
print(f"12.1 worst pass_rate: {worst['dimension']}={worst['group']} "
      f"{worst['value']:.6g} (n = {worst['n']})")

# --- 12.2: a constant shift ----------------------------------------------------
ids = [f"k{i:02d}" for i in range(1, 11)]
base = [score(i, 1, 0.0) for i in ids]
cand = [score(i, 1, 0.0, chosen=1.25) for i in ids]
for tol in (0.25, 0.2):
    f = compare(base, cand, "overall", CheckPlan(tolerances={"mean_chosen_cost": tol}))[
        "mean_chosen_cost"]
    print(f"12.2 tol {tol}: worsening {f['worsening']} range [{f['lo']}, {f['hi']}] "
          f"change {f['change']} state {f['state']}")

# --- 12.3: regressed and not detectable at once --------------------------------
ids = [f"k{i:02d}" for i in range(1, 7)]
base = [score(i, 1, 0.0) for i in ids]
cand = [score(i, 1, 0.0, chosen=10.0 if i == "k01" else 0.0) for i in ids]
f = compare(base, cand, "overall", CheckPlan(tolerances={"mean_chosen_cost": 0.5}))[
    "mean_chosen_cost"]
print(f"12.3 worsening {f['worsening']:.6f} lo {f['lo']} hi {f['hi']:.6f} "
      f"change {f['change']} state {f['state']}")

# --- the starter suite, simulated ----------------------------------------------
tmp = Path(tempfile.mkdtemp())
(tmp / "c.yaml").write_text(yaml.safe_dump({
    "schema": "routeforge.router-candidate/v1", "name": "latency-only",
    "routing": {"weights": {"cost": 0.0, "latency": 2.0}}}))
r = asyncio.run(run_bench(
    "eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml", out_dir=str(tmp / "o"),
    seed=0, group_report=True, candidate_path=str(tmp / "c.yaml"),
    check_plan=CheckPlan(tolerances={"pass_rate": 0.02, "mean_chosen_cost": 0.0001})))
online = r.groups["strategies"]["router_online"]
faq = online["dimensions"]["workload"]["faq"]["metrics"]
print(f"starter groups: sizes {json.dumps(r.groups['group_sizes']['placement'])}")
print(f"starter router_online faq: pass_rate {fig(faq['pass_rate'])}, "
      f"cost_regret {fig(faq['cost_regret'])}; overall violations "
      f"{online['overall']['violations']}")
print(f"starter onprem_only: {fig(online['dimensions']['placement']['onprem_only']['metrics']['pass_rate'])}")
c = r.check
f = c["overall"]["mean_chosen_cost"]
print(f"starter check: exit {r.exit_code} verdict {c['verdict']} changed "
      f"{c['changed']['choice_changed']}/{c['changed']['n_cases']} violations "
      f"{c['hard']['candidate_violations']}")
print(f"starter check mean_chosen_cost: {f['baseline']:.6f} -> {f['candidate']:.6f} "
      f"worsening {f['worsening']:.6f} range [{f['lo']:.6f}, {f['hi']:.6f}] "
      f"{f['change']} {f['state']}; regressed figures {len(c['regressed'])}")
````

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=. uv run python /tmp/p2_sanity.py 2>/dev/null
```

Expected, exactly (every run is seeded):

```
12.1 min 1: pass_rate faq 0.8 (n = 5), claim 0.333333 (n = 3), overall 0.625 (n = 8)
12.1 min 1: cost_regret faq 0.15 (n = 4), claim 0.6 (n = 1), overall 0.24 (n = 5)
12.1 min 3: pass_rate faq 0.8 (n = 5), claim 0.333333 (n = 3), overall 0.625 (n = 8)
12.1 min 3: cost_regret faq 0.15 (n = 4), claim insufficient (n = 1), overall 0.24 (n = 5)
12.1 cost_regret combined by denominator 0.24, by case count 0.31875
12.1 worst pass_rate: workload=claim 0.333333 (n = 3)
12.2 tol 0.25: worsening 0.25 range [0.25, 0.25] change worse state within_tolerance
12.2 tol 0.2: worsening 0.25 range [0.25, 0.25] change worse state regressed
12.3 worsening 0.666667 lo -1.0 hi 4.000000 change not_detectable state regressed
starter groups: sizes {"any": 8, "escalated": 2, "onprem_only": 4, "us_only": 8}
starter router_online faq: pass_rate 0.75 (n = 8), cost_regret 0.000106667 (n = 6); overall violations 0
starter onprem_only: insufficient (n = 4)
starter check: exit 3 verdict regressed changed 16/22 violations 0
starter check mean_chosen_cost: 0.001490 -> 0.002596 worsening 0.001106 range [0.000754, 0.001470] worse regressed; regressed figures 8
```

Read it against the table: the first nine lines are the hand-computed values. The last five are the simulated starter run of research §22.5: the group sizes; the one group that holds all of `router_online`'s regret; the `onprem_only` group too small for a rate; and a candidate that moves 16 of 22 choices, makes no ineligible one, and is `regressed` on cost overall and in seven groups.

If a line differs, stop and report it: it is a defect in the code or in this plan's arithmetic, and it is not to be fixed by editing the expected text.

- [ ] **Step 5: Confirm the branch is clean and nothing was pushed**

```bash
git status --short
git log --oneline main..HEAD
```

Expected: `git status --short` prints nothing. The log lists, newest first, the eight task commits (Tasks 8 down to 1), then the plan commit and the spec commit. Do not push.

- [ ] **Step 6: Hand the note to whoever opens the pull request**

Nothing to run. Report these facts with the branch:

1. **Nothing to migrate and nothing to deploy.** No column, route, setting or dependency changed. `feasible_bench.py --group-report` and `--candidate` work against any suite and endpoint file the bench already reads.
2. **Nothing is applied by anything in this branch.** The check writes two files under `--out` and returns an exit code. It is the handoff's "promotion gate" only in that sense.
3. **The roadmap row says PR `pending`.** Replace it with the number in a follow-up docs commit.
4. **The golden fixture was written once**, in Task 1, from the commit named in `tests/eval/fixtures/bench_golden/GENERATED_FROM`, and not regenerated. The unflagged output did not change.
5. **This is the first of two plans.** Churn robustness and confidence calibration (spec §6, §9; invariants E18–E25) are designed and not built. `report.md` still lists calibration and churn under "Deferred metrics"; that is correct until the second plan.
6. **Nothing has been run live.** Research §22.5.5 and `docs/research/arxiv.md` E11d–E11e list the measurements, all **TO MEASURE**. They need a suite with at least five cases in every placement and protection group and two endpoints of different placement; neither the Spark nor the DGX lab has the second.
7. **The output of Steps 3 and 4** on this machine.
8. **B67** holds the follow-ups: the answer confidence on the record, policy-derived groups, a check over several seeds, a multiplicity note, the candidate keys the bench cannot evaluate, a baseline overlay, a larger starter suite.

---

## Open risks

- **A verdict can be mistaken for a decision.** `pass` means "not shown to be worse than the tolerances, on this suite, at this seed". On the simulated backend a pass-rate tolerance judges a hand-written probability: the same candidate passed at eight seeds, regressed at one and was inconclusive at one (research §22.5.3). The fixed text says so in every report; a verdict quoted without its suite and seed has lost that.
- **The word "gate".** The handoff, the roadmap history and some readers will call this a promotion gate. Nothing promotes. The CLI flag, the file names, the schema and the report avoid the word, and the one sentence that uses "promotes" denies it — but a CI job named "promotion gate" built on exit code 0 would re-create the misreading outside this repository.
- **`inconclusive` will be the common verdict on 22 cases.** That is the honest answer and the conservative rule (a figure passes only when its whole range is inside the tolerance), but an operator who meets exit 4 three times may widen tolerances until it goes away. The docs say a larger suite is the other option; the starter suite is not that suite (B67 item 7).
- **The range covers cases only.** Not the simulation seed, not the case order (the online arm learns in suite order), not the endpoint file. Its coverage is unmeasured (arxiv.md E11e). A percentile bootstrap on 22 cases moves in steps of 1/22.
- **No multiplicity correction.** Two tolerances over a suite with eleven sufficiently large groups judge up to 24 figures. More chances of `regressed` or `inconclusive` is the safe direction for a check, not a calibrated one.
- **Groups are labels a case author wrote.** A wrong `placement` tag puts a case in the wrong group and nothing notices. Deriving the policy dimensions from the gate's output is B67 item 2.
- **`application` is a policy fingerprint when no `tags.app` is set.** Two applications with identical policies are one group; one application whose cases differ in policy is several. Authors who care must set `tags.app`.
- **Refused keys.** The escalation threshold — the routing key an operator is perhaps most likely to want to tune — cannot be checked. The refusal names the reason, but the capability is missing (B67 item 5).
- **The candidate is compared on `router_online` only.** A candidate that helps the learning arm and hurts a cold start, or the reverse, is judged on the first; the cold and warm-prior candidate arms are in `metrics.json` unjudged.
- **One hard-failure path is shared.** A run that exits 2 because of the *baseline* arm fails the check too. That is deliberate (a comparison made on a run that failed its integrity checks is not a comparison), and it means a broken baseline blocks every candidate until it is fixed.
- **The golden pin holds a copy of `config/`.** It pins the harness code, so a deliberate change to the bench's default output needs a reviewed regeneration, and a change to `config/` is *not* noticed by it. `test_starter_suite.py` still runs against the live config.
- **The second plan changes the default report.** Building calibration and churn means editing `runner.DEFERRED_METRICS`, which is in the pinned report body: that plan must regenerate the golden report bodies deliberately, and say so.
