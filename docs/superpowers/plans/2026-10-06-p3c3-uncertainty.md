# P3c-3 Uncertainty Ranges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every headline figure of the offline policy simulator a range, so a reader can tell "−39% cost" on 24 rows from the same number on two million, and say plainly what that range does not cover.

**Architecture:** One new offline module, `app/eval/uncertainty.py`. Each row is still replayed once; a bootstrap then resamples the replayed **rows** with replacement, within each application, and recomputes the figures. The rows drawn for an application in a replicate are a function of (seed, replicate, application, row count) only, so both arms, every scenario and every shadow-price relaxation are evaluated on the same resampled rows without being held in memory together. `app/eval/scenarios.py`, `app/eval/shadow.py` and `scripts/policy_simulate.py` each gain an opt-in path that adds one key to their output. Where a quality is a P2b-3 calibrated score and the operator passes the recommendation file it came from, each replicate also draws that unit's quality from its Beta posterior.

**Tech Stack:** Python 3.12 standard library only (`random`, `collections.Counter`, `math`), pytest, Hypothesis, `uv`. No new dependency (`numpy` is not available to the offline tooling and is not added). No serving change, no database change, nothing to migrate.

**Spec:** docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md

## Deviations from spec

The spec was amended while this plan was written, so the plan and the spec agree: there is no plan-versus-spec deviation. What follows are the places where the design departs from the brief the spec was written from, or where the brief was silent. The spec lists the same items in its §15. Each follows the code as it is, keeps the brief's intent, and is pinned by a test.

1. **Quality posteriors come from a file, not from the snapshot.** The brief said to check what the config snapshot carries about calibrated qualities and, if the Beta parameters are not there, to scope a serving change or defer. They are not there: the snapshot exports `{model, workload, score, set_id}` (`app/apps/snapshot.py::_quality_entries`) and `model_quality` has no column for them. A third option needs neither a serving change nor a deferral: P2b-3's `recommendation.json` carries `alpha` and `beta`, and its `set_id` is a hash of its own content, the hash the admin API verified at apply time. So `--quality-posteriors FILE` reads it. Task 3: `test_a_real_recommendation_file_loads`, `test_an_edited_file_is_refused`, `test_a_file_never_attaches_to_a_score_it_did_not_produce`.
2. **No `--block day`.** The brief allowed it only if cheap and testable. It is neither on the exports this runs on: too few day-blocks for a percentile interval, a row count that varies by replicate, and `ts` is optional on a row (spec §4.5). It is a named follow-up in the roadmap (Task 8).
3. **The automatic export of posteriors is deferred.** What it would need is written in spec §6.2 and in the roadmap (Task 8).
4. **`--min-rows` also applies to a scenario's totals and to a relaxation's rows in scope**, not only to an app's bucket. Task 5: `test_u7_too_few_rows_prints_insufficient_data_and_no_brackets`. Task 6: `test_u7_a_relaxation_with_few_rows_in_scope_gets_no_range`.
5. **The three shares are new figures.** The brief asked for the counts "as counts and as shares"; today's documents have no share. They exist only inside `uncertainty`, each with its own `point`. Task 2: `test_leaf_point_equals_the_figure_the_document_carries`.
6. **Every leaf carries `point`; the row count is on the bucket (`n`).** The brief asked for `lo`, `hi` and enough to reproduce.
7. **A zero count gets a stated bound**, `1 − (1 − P)^(1/n)`, on all three shares, with the sentence printed for `newly_rejected`. The brief allowed a rule-of-three statement only if it could be justified for this sampling scheme; spec §7.2 gives the argument (Jensen's inequality over the strata) and its assumption. It uses the run's own level, so at the default it is `2.3/n`, not `3/n`. Task 2: `test_a_zero_count_is_zero_in_every_replicate_and_carries_a_bound`, `test_zero_bound_values`. Task 4: `test_zero_newly_rejected_is_never_presented_as_no_risk`.
8. **A point outside its own range is flagged and counted, never prevented.** Task 2: `test_u3_every_point_is_inside_its_range_or_is_flagged`, `test_u3_one_replicate_can_leave_the_point_outside_and_says_so`.
9. **A figure undefined in any replicate has no range** (`undefined_replicates`). Not in the brief. Task 2: `test_a_figure_undefined_in_some_replicate_has_no_range`.
10. **Latency percentiles are exact per replicate, and the caveat has one more sentence than the brief listed.** Replay gives a routed row its endpoint's latency, so latency takes one value per endpoint and a latency range reflects only the route mix. `not_covered` has a fifth id, `per_request_latency`. Task 2: `test_weighted_rank_matches_nearest_rank`. Task 4: `test_u9_caveat_is_the_spec_text`.
11. **Shadow ranges are at `shadow_prices.uncertainty`**, not under the top-level `uncertainty`: `render_shadow_section` takes the shadow block alone. U1 names all three added keys. Task 6: `test_u1_a_plan_adds_one_key_to_the_block`. Task 7: `test_u1_the_flag_adds_keys_and_changes_no_existing_value`.
12. **`--scenarios` with `--min-agreement` and `--uncertainty` replays the snapshot once more**, so the gate still runs before any bootstrap. Task 7: `test_min_agreement_refuses_before_any_bootstrap`.
13. **The comparison table's on-prem share cell shows no range** (it is two numbers in one cell). Both ranges are in the JSON and in each scenario's section. Task 5: `test_report_puts_ranges_in_the_table_and_explains_them`.
14. **`ArmResult` gains `quality_set_id`**, defaulted, serialised nowhere. Task 3: `test_the_arm_carries_the_calibration_set_id`.
15. **"Byte-identical to today" is pinned against files written before any product code exists**, not against a second run of the new code. Task 1 generates and commits them from the untouched tree. Task 1: `test_u1_output_without_the_flag_is_byte_identical`.
16. **One figure copies the comparison table's arithmetic.** `scenarios._table_row` divides the like-for-like delta by the window **after** rounding it to 6 places. The leaf for that cell does the same in every replicate, so its point is the cell exactly (spec §9.4). Task 5: `test_every_table_range_is_about_the_figure_in_that_column`.

## Global Constraints

- **Offline only.** Touch `app/eval/`, `scripts/policy_simulate.py`, `tests/eval/` and `docs/`. No file under `app/` outside `app/eval/` changes. No serving module may import `app.eval` (`tests/eval/test_isolation.py` must keep passing); `app/eval` may import serving modules, never the reverse.
- **Opt-in and additive.** Without `--uncertainty` every output file is byte-identical to what the tree produced before this plan. `tests/eval/test_uncertainty_golden.py` (Task 1) is that pin: if it fails in any later task, the change that broke it is wrong. Do not regenerate the golden files to make it pass.
- **RouteForge never ranks or recommends.** Nothing sorts scenarios or relaxations by a range. No output key contains `rank`, `recommend`, `best`, `winner`, `prefer` or `score`. A security requirement's range keeps the `price of a protection — not a recommendation` label wherever it is printed.
- **Honesty about the range.** It is sampling variability of the recorded rows under the replay model. `UNCERTAINTY_CAVEAT` is printed in every report that shows a range and is pinned word for word; do not paraphrase it. Never invent uncertainty for a figure that has none stated (a published or tier-default quality stays a point).
- **Exact values from the spec.** Defaults: `--bootstrap 1000`, `--interval 0.90`, `--seed 0`, `--min-rows 30`. Bounds: `1 ≤ replicates ≤ 20000`, `0 < interval < 1` (NaN refused), `seed ≥ 0`, `min_rows ≥ 1`. `METHOD = "stratified_paired_row_bootstrap"`, `STRATA = "application_id"`. `NOT_COVERED = ("replay_model_error", "admission_estimate_error", "unrecorded_inputs", "traffic_shift", "per_request_latency")`. Draw seeds: `f"{seed}:{replicate}:app:{app}"` and `f"{seed}:{replicate}:quality:{set_id}:{model}:{workload or '*'}"`. Interval: `k = floor((1 − P)/2 · N + 1e-9)`, `lo = sorted[k]`, `hi = sorted[N − 1 − k]`. Exit codes are unchanged: `0`, `2`, `3`, `4`.
- **Deterministic.** No clock, no global `random` state, no dict-order dependence in output. JSON is written as the existing code writes it (`json.dumps(..., sort_keys=True, indent=2)`), floats through `_round6` (`app/eval/outcomes.py`).
- **Tests.** Always run pytest as `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest ...`; without the variable pytest hangs on import. There is no pytest-asyncio: use `asyncio.run`. Shared simulator fixtures are in `tests/eval/sim_fixtures.py` (`BASE_SETTINGS`, `make_snapshot`, `app_entry`, `onboarded_row`, `make_row`). The commands below do not pass `-q`: `pyproject.toml` already sets it, and a second `-q` hides the summary line.
- **What each task runs.** The focused tests named in its steps, then `tests/eval` (about six minutes; it prints `N passed`). The whole suite runs once, in Task 9, with `-rf`: it takes more than ten minutes and prints no summary line under `-q`, so `-rf` is what lists a failure. One failure is pre-existing and not caused by this branch: `tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app`.
- **Code style.** Type-hint everything. Match the surrounding comment density and idiom. `app/eval` modules import each other's underscore-private helpers (as `scenarios.py` and `shadow.py` already do); keep that idiom. No application name in a symbol, test id, fixture or comment (`.claude/skills/consumer-contract-tests`).
- **How to read the code steps.** A step that says "Create" shows the whole new file. A step that says "Patch" shows a real patch against the tree as the previous task left it. Save the block to a file and run `git apply <file>`, or make the same edits by hand. Every block in this plan was applied in order to a scratch copy of the branch and the listed tests were run against it.
- **Git.** Stage files by name, never `git add -A`. Never `git stash`. Every commit message ends with exactly this line; do not substitute your own model name:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- **Do not push.** There is nothing to deploy: this is operator-run tooling, and it adds no column, route or setting.

## Review Focus

Six input classes the spec implies, that are likely to bite a user, and that an obvious task test would miss. Each has a named test in the owning task.

1. **Thin and lopsided data.** A one-row app (an unstratified resample drops it a third of the time); an app at exactly `--min-rows` and one row under; fewer than `--min-rows` rows in all; no rows; an arm with one routed row among forty. Task 2: `test_u6_a_one_row_app_is_present_in_every_replicate`, `test_min_rows_is_inclusive`, `test_u7_too_few_rows_in_all_draws_nothing`, `test_u7_no_rows_at_all`, `test_a_figure_undefined_in_some_replicate_has_no_range`.
2. **Degenerate figures.** A count that is 0 everywhere (the range `[0, 0]` must not read as "no risk"); a figure with no variance at all; one replicate (`lo == hi`, and the point can be outside). Task 2: `test_a_zero_count_is_zero_in_every_replicate_and_carries_a_bound`, `test_u3_one_replicate_can_leave_the_point_outside_and_says_so`. Task 4: `test_zero_newly_rejected_is_never_presented_as_no_risk`, `test_point_outside_is_said_in_the_report`.
3. **Pairing across scopes.** The same app alone and inside the whole export; a relaxation against the proposal that makes the same edit; one scenario alone and in a file of three, in both orders; a no-op whose deltas must be exactly zero, not nearly zero. Task 2: `test_u4_noop_proposal_has_every_delta_range_exactly_zero`, `test_u4_an_app_is_drawn_the_same_way_alone_and_inside_the_whole_export`. Task 5: `test_u4_a_scenario_gets_the_same_ranges_alone_and_among_others`. Task 6: `test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal`.
4. **Posterior files that are not what they claim.** Edited after writing; for a set the snapshot does not hold; for a score the snapshot was hand-edited away from; a set with both a workload-scoped and a model-wide unit; the same file twice; a unit that was never recommended; not JSON. Task 3: `test_an_edited_file_is_refused`, `test_a_file_never_attaches_to_a_score_it_did_not_produce`, `test_posterior_key_follows_the_resolver_order`, `test_units_that_were_not_recommended_are_skipped`, `test_a_malformed_file_is_refused_naming_the_location`. Task 7: `test_a_posterior_file_for_another_set_warns_and_adds_nothing`.
5. **Flags that silently do nothing.** A tuning flag without `--uncertainty`; each bound and one step past it; `--interval nan`. Task 2: `test_plan_refuses_values_out_of_range`. Task 7: `test_bad_or_orphan_flags_exit_2_and_write_nothing`, `test_limits_are_accepted`.
6. **The unflagged path, and the gate.** Any change at all to a file written without the flag, in each of the four ways to run the CLI; `--min-agreement` failing with `--uncertainty` (exit 4, nothing written, no bootstrap started — in `--scenarios` mode too). Task 1: `test_u1_output_without_the_flag_is_byte_identical`. Task 7: `test_u1_the_flag_adds_keys_and_changes_no_existing_value`, `test_min_agreement_refuses_before_any_bootstrap`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `tests/eval/test_uncertainty_golden.py` | create | U1: the four ways to run the CLI without the flag, byte for byte; the shared CLI runner |
| `tests/eval/fixtures/uncertainty_golden/` | create (generated) | four committed input files and the eight expected output files |
| `app/eval/uncertainty.py` | create | the plan, the stratified paired bootstrap, leaves and buckets, posterior loading and draws, the caveat and report text |
| `app/eval/simulate.py` | modify | `ArmResult.quality_set_id`, set by `simulate_row` |
| `app/eval/scenarios.py` | modify | `compare(uncertainty=)`, `Comparison.uncertainty`, the key in `comparison_doc`, ranges in `render_comparison` |
| `app/eval/shadow.py` | modify | `shadow_prices(uncertainty=)`, `_entry_ranges`, the "Ranges" table |
| `scripts/policy_simulate.py` | modify | the six flags, `inputs.uncertainty`, the block and the report section in every mode, the early gate |
| `tests/eval/test_uncertainty.py` | create | the engine: U2–U7, U11, U13; shared test helpers |
| `tests/eval/test_uncertainty_quality.py` | create | posterior files and quality draws: U4, U8 |
| `tests/eval/test_uncertainty_report.py` | create | the caveat and the report text: U7, U9, U11 |
| `tests/eval/test_uncertainty_scenarios.py` | create | the comparison: U1, U4, U7, U11, U13 |
| `tests/eval/test_uncertainty_shadow.py` | create | shadow prices: U1–U4, U7, U11 |
| `tests/eval/test_uncertainty_cli.py` | create | the CLI in every mode: U1, U2, U10; flags; the gate |
| `docs/reference/policy-simulator.md` | modify | new §10; flags in §3; keys in §4.1; §4.2, §6, §8.7, §9.6 |
| `docs/research/feasible-route-bench.md` | modify | new §20 |
| `docs/research/arxiv.md` | modify | §4.2; experiments E9a–E9d; a threat to validity |
| `docs/roadmap/priorities-1-3-status.md` | modify | the slice in Built; §2.1; §3 |

## Spec invariants → tests

| # | Invariant | Test | Task |
|---|---|---|---|
| U1 | byte-identical without the flag; the flag only adds the three keys | `tests/eval/test_uncertainty_golden.py::test_u1_output_without_the_flag_is_byte_identical`; `tests/eval/test_uncertainty_cli.py::test_u1_the_flag_adds_keys_and_changes_no_existing_value`; `tests/eval/test_uncertainty_scenarios.py::test_u1_a_plan_adds_one_key_and_changes_no_other`; `tests/eval/test_uncertainty_shadow.py::test_u1_a_plan_adds_one_key_to_the_block` | 1, 5, 6, 7 |
| U2 | deterministic with the flag; no global `random` state | `tests/eval/test_uncertainty.py::test_u2_same_plan_same_block_and_the_seed_changes_it`, `::test_u2_the_global_random_state_is_not_read_or_moved`; `tests/eval/test_uncertainty_shadow.py::test_u2_shadow_ranges_are_deterministic`; `tests/eval/test_uncertainty_cli.py::test_u2_the_flagged_run_is_byte_identical_twice` | 2, 6, 7 |
| U3 | every point is inside its range or the leaf is flagged; nothing is widened | `tests/eval/test_uncertainty.py::test_u3_every_point_is_inside_its_range_or_is_flagged`, `::test_u3_one_replicate_can_leave_the_point_outside_and_says_so`; `tests/eval/test_uncertainty_shadow.py::test_u3_every_shadow_point_is_inside_its_range_or_flagged` | 2, 6 |
| U4 | paired: a no-op has every delta range exactly `[0, 0]`; the same draws across scopes | `tests/eval/test_uncertainty.py::test_u4_noop_proposal_has_every_delta_range_exactly_zero`, `::test_u4_an_app_is_drawn_the_same_way_alone_and_inside_the_whole_export`; `tests/eval/test_uncertainty_quality.py::test_u4_one_unit_in_both_arms_cancels_in_the_delta`; `tests/eval/test_uncertainty_scenarios.py::test_u4_an_empty_scenario_has_every_range_at_zero`, `::test_u4_a_scenario_gets_the_same_ranges_alone_and_among_others`; `tests/eval/test_uncertainty_shadow.py::test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal` | 2, 3, 5, 6 |
| U5 | ranges narrow as the rows are replicated k-fold | `tests/eval/test_uncertainty.py::test_u5_ranges_narrow_as_the_rows_are_replicated` | 2 |
| U6 | stratification never empties an app bucket | `tests/eval/test_uncertainty.py::test_u6_every_replicate_draws_exactly_the_apps_row_count`, `::test_u6_a_one_row_app_is_present_in_every_replicate` | 2 |
| U7 | below `--min-rows`: `insufficient_data` and `n`, no range; reports print `insufficient data (n = k)` | `tests/eval/test_uncertainty.py::test_u7_a_small_app_gets_no_range_and_says_how_many_rows`, `::test_u7_too_few_rows_in_all_draws_nothing`, `::test_u7_no_rows_at_all`; `tests/eval/test_uncertainty_report.py::test_u7_report_prints_insufficient_data_instead_of_a_range`; `tests/eval/test_uncertainty_scenarios.py::test_u7_too_few_rows_prints_insufficient_data_and_no_brackets`; `tests/eval/test_uncertainty_shadow.py::test_u7_a_relaxation_with_few_rows_in_scope_gets_no_range` | 2, 4, 5, 6 |
| U8 | quality-label uncertainty only where a posterior exists | `tests/eval/test_uncertainty_quality.py::test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`, `::test_u8_without_the_file_a_calibrated_quality_stays_a_point`, `::test_u8_a_tier_default_quality_has_no_label_uncertainty`, `::test_u8_coverage_counts_a_mixed_export`, `::test_a_file_never_attaches_to_a_score_it_did_not_produce` | 3 |
| U9 | the caveat is the spec's text and is printed with every range; `not_covered` has the five ids | `tests/eval/test_uncertainty_report.py::test_u9_caveat_is_the_spec_text`, `::test_u9_not_covered_names_every_excluded_source`, `::test_u9_section_opens_with_the_caveat_and_says_how_ranges_were_made`; `tests/eval/test_uncertainty_scenarios.py::test_report_puts_ranges_in_the_table_and_explains_them`; `tests/eval/test_uncertainty_cli.py::test_proposal_report_gets_the_section_between_the_report_and_shadow_prices` | 4, 5, 7 |
| U10 | the plan is in `inputs`; ids tell plans apart | `tests/eval/test_uncertainty_cli.py::test_u10_plan_values_are_in_inputs_with_their_defaults`, `::test_u10_the_id_tells_plans_apart` | 7 |
| U11 | no ranking vocabulary; file order kept; a security range keeps its label | `tests/eval/test_uncertainty.py::test_u11_no_key_ranks_or_recommends`; `tests/eval/test_uncertainty_report.py::test_u11_report_text_does_not_rank_or_recommend`; `tests/eval/test_uncertainty_scenarios.py::test_u11_ranges_add_no_ranking_key_and_keep_file_order`; `tests/eval/test_uncertainty_shadow.py::test_u11_a_security_range_keeps_the_protection_label` | 2, 4, 5, 6 |
| U12 | serving isolation | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` (unchanged; run in every task with `tests/eval`) | 2–7, 9 |
| U13 | the engine's figures are the document's figures | `tests/eval/test_uncertainty.py::test_engine_figures_equal_bucket_stats`, `::test_leaf_point_equals_the_figure_the_document_carries`; `tests/eval/test_uncertainty_scenarios.py::test_every_table_range_is_about_the_figure_in_that_column`; `tests/eval/test_uncertainty_shadow.py::test_ranges_are_parallel_to_constraints_and_about_the_same_figures` | 2, 5, 6 |

---

## Tasks

### Task 1: Pin today's output before changing anything (spec §13, U1)

**Files:**
- Create: `tests/eval/test_uncertainty_golden.py`
- Create (generated by Step 4, then committed): `tests/eval/fixtures/uncertainty_golden/inputs/{export.ndjson,snapshot.json,proposal.json,scenarios.json}` and `tests/eval/fixtures/uncertainty_golden/expected/{proposal,proposal_shadow,shadow,scenarios}/` (two files each)

**Interfaces:**
- Consumes:
  - `scripts/policy_simulate.py::main(argv: list[str] | None = None) -> int` — returns the exit code; with `--proposal` or `--shadow-prices` it writes `simulation.json` and `report.md` into `--out`, with `--scenarios` it writes `comparison.json` and `report.md`.
  - `tests/eval/sim_fixtures.py`: `make_snapshot(*, onboarded=None, apps=None, providers=None, quality=None, signals=None, serving_policy_over=None) -> dict`, `app_entry(app_id, *, sensitivity_rules=None, **policy) -> dict`, `onboarded_row(slug, tier, **over) -> dict`, `make_row(request_id, app_id, **over) -> dict`.
- Produces (module-level names later tasks import from `tests.eval.test_uncertainty_golden`):
  - `ROOT`, `SCRIPT`, `GOLDEN`, `INPUTS`, `EXPECTED`: `pathlib.Path`.
  - `EXPORT`, `SNAPSHOT`, `PROPOSAL`, `SCENARIOS`: the four committed input files.
  - `MODES: dict[str, tuple[list[str], tuple[str, str]]]` — keys `"proposal"`, `"proposal_shadow"`, `"shadow"`, `"scenarios"`; each value is (the flags that select the mode, the two file names it writes; the JSON document is the first).
  - `load_cli()` — `scripts/policy_simulate.py` as a module.
  - `run_mode(mode: str, out: Path, *extra: str) -> int` — runs `main` in-process over the committed inputs, writing into `out`.
  - `regenerate() -> None` — rewrites the inputs and every expected output.

**Rules.**

- **This task comes first, and it runs on an untouched tree.** The expected files are only a pin of "today" if they are written before any code of this plan exists. Step 1 checks that.
- The inputs are committed too, so the expected outputs do not move when `config/` or a fixture builder changes later.
- Do not hand-edit a generated file.

- [ ] **Step 1: Confirm nothing under `app/`, `scripts/` or `tests/` has changed on this branch**

```bash
git status --short app scripts tests
git diff --stat main -- app scripts tests
```

Expected: both print nothing. (The branch holds the spec and plan commits only.) If either prints anything, stop and report it: the golden files would not describe the tree before this plan.

- [ ] **Step 2: Write the test**

Create `tests/eval/test_uncertainty_golden.py`:

````python
"""P3c-3 invariant U1: without `--uncertainty`, every output of
`scripts/policy_simulate.py` is byte-identical to what it was before P3c-3.

The files under `tests/eval/fixtures/uncertainty_golden/` were written by
this module from the tree as it was BEFORE any P3c-3 product code existed
(plan Task 1, Step 2), in all four ways the CLI can be run:

    proposal          --proposal
    proposal_shadow   --proposal --shadow-prices
    shadow            --shadow-prices
    scenarios         --scenarios

`inputs/` holds the four input files, committed, so the expected outputs do
not move when `config/` or a fixture builder changes. To regenerate after a
DELIBERATE change to the simulator's default output, run

    LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_uncertainty_golden

and review the diff: every changed byte is a change to what an operator who
never passes `--uncertainty` sees.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "policy_simulate.py"
GOLDEN = Path(__file__).parent / "fixtures" / "uncertainty_golden"
INPUTS = GOLDEN / "inputs"
EXPECTED = GOLDEN / "expected"

EXPORT, SNAPSHOT = INPUTS / "export.ndjson", INPUTS / "snapshot.json"
PROPOSAL, SCENARIOS = INPUTS / "proposal.json", INPUTS / "scenarios.json"

#: mode -> (the flags that select it, the two files it writes)
MODES: dict[str, tuple[list[str], tuple[str, str]]] = {
    "proposal": (["--proposal", str(PROPOSAL)], ("simulation.json", "report.md")),
    "proposal_shadow": (["--proposal", str(PROPOSAL), "--shadow-prices"],
                        ("simulation.json", "report.md")),
    "shadow": (["--shadow-prices"], ("simulation.json", "report.md")),
    "scenarios": (["--scenarios", str(SCENARIOS)], ("comparison.json", "report.md")),
}


def load_cli():
    """`scripts/policy_simulate.py` as a module, so `main(argv)` runs
    in-process (the pattern `tests/eval/test_policy_simulate_cli.py` uses)."""
    spec = importlib.util.spec_from_file_location("policy_simulate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_mode(mode: str, out: Path, *extra: str) -> int:
    """Run the CLI in `mode` over the committed inputs, writing into `out`."""
    flags, _files = MODES[mode]
    return load_cli().main(["--export", str(EXPORT), "--snapshot", str(SNAPSHOT),
                            "--out", str(out), "--config-dir", str(ROOT / "config"),
                            *flags, *extra])


def _input_files() -> dict[str, str]:
    """The four input files' text, from the shared fixture builders.

    Three apps on a deployment whose frontier tier is cloud-only: `big` is
    pinned to the mid tier (an operational constraint that binds), `safe`
    is on-prem only (a security constraint that binds), `few` has six rows
    (below the default `--min-rows`). 111 rows over three days, every row a
    different prompt size."""
    from tests.eval.sim_fixtures import app_entry, make_row, make_snapshot, onboarded_row

    snapshot = make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid")],
        apps=[app_entry("big", allowed_tiers=["mid"]),
              app_entry("few", allowed_tiers=["mid"]),
              app_entry("safe", residency="onprem_only")],
        providers={"azure": {"enabled": True}})
    lines = []
    for app, n in (("big", 60), ("few", 6), ("safe", 45)):
        for i in range(n):
            row = make_row(f"{app}-{i:03d}", app, complexity=[0.05, 0.6, 1.0][i % 3],
                           prompt_tokens=[200, 1200, 3000][i % 3] + 5 * i,
                           ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            feedback = row.pop("feedback")
            lines.append(json.dumps({"schema": "routeforge.outcome/v1", "outcome": row,
                                     "feedback": feedback}, sort_keys=True))
    proposal = {"schema": "routeforge.policy-proposal/v1", "title": "Drop the mid endpoint",
                "endpoints": {"remove": ["local-mid"]}}
    scenarios = {"schema": "routeforge.policy-scenarios/v1", "title": "Golden options",
                 "scenarios": [
                     {"name": "no-change", "proposal": {}},
                     {"name": "drop-mid", "proposal": {"endpoints": {"remove": ["local-mid"]}}},
                     {"name": "half-tokens", "proposal": {"workload": {"token_scale": 0.5}}}]}
    return {"export.ndjson": "".join(line + "\n" for line in lines),
            "snapshot.json": json.dumps(snapshot, sort_keys=True, indent=2) + "\n",
            "proposal.json": json.dumps(proposal, sort_keys=True, indent=2) + "\n",
            "scenarios.json": json.dumps(scenarios, sort_keys=True, indent=2) + "\n"}


def regenerate() -> None:
    """Rewrite the inputs and every expected output from the current tree."""
    shutil.rmtree(GOLDEN, ignore_errors=True)
    INPUTS.mkdir(parents=True)
    for name, text in _input_files().items():
        (INPUTS / name).write_text(text, encoding="utf-8")
    for mode in MODES:
        code = run_mode(mode, EXPECTED / mode)
        if code != 0:
            raise SystemExit(f"{mode}: exit {code}")
        print(f"wrote {EXPECTED / mode}")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_u1_output_without_the_flag_is_byte_identical(mode, tmp_path):     # U1
    out = tmp_path / "out"
    assert run_mode(mode, out) == 0
    _flags, files = MODES[mode]
    assert sorted(p.name for p in out.iterdir()) == sorted(files)
    for name in files:
        assert (out / name).read_bytes() == (EXPECTED / mode / name).read_bytes(), \
            f"{mode}/{name} changed without --uncertainty"


def test_the_golden_run_is_not_trivial():
    """The pin is only worth something if the outputs it pins have figures
    in them: routes that move, rows newly rejected, binding constraints of
    both kinds, and three scenarios that differ."""
    sim = json.loads((EXPECTED / "proposal_shadow" / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 111 and sim["baseline"]["agreement"] is not None
    assert sim["totals"]["route_changed"] > 0 and sim["totals"]["cost_usd"]["delta"] != 0
    kinds = {c["security"] for c in sim["shadow_prices"]["constraints"]}
    assert kinds == {True, False}
    doc = json.loads((EXPECTED / "scenarios" / "comparison.json").read_text())
    assert [row["name"] for row in doc["table"]] == ["no-change", "drop-mid", "half-tokens"]
    assert len({row["cost_delta_usd"] for row in doc["table"]}) == 3
    for mode in MODES:
        for path in (EXPECTED / mode).iterdir():
            assert "uncertainty" not in path.read_text().lower(), path


if __name__ == "__main__":
    regenerate()
````

- [ ] **Step 3: Run it and watch it fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_golden.py -p no:warnings -rf
```

Expected: `5 failed`. The four `test_u1_output_without_the_flag_is_byte_identical[...]` cases fail on `assert run_mode(mode, out) == 0` (the export file does not exist, so the CLI returns `2`), and `test_the_golden_run_is_not_trivial` fails with `FileNotFoundError`.

- [ ] **Step 4: Generate the inputs and the expected outputs from the untouched tree**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_uncertainty_golden
```

Expected: the CLI's own lines for four runs, and four lines beginning `wrote `, the last ending `expected/scenarios`. Twelve files now exist under `tests/eval/fixtures/uncertainty_golden/`.

- [ ] **Step 5: Check the generated files are the ones this plan was validated against**

```bash
(cd tests/eval/fixtures/uncertainty_golden && sha256sum -c - <<'EOF'
cb878409756bdba7b6f8c0d8faba1d395ea8402c05dc82129966e4ff4f27b979  expected/proposal/report.md
22c13fe95f6607e27afb9c15476686ab7eed68da0ae3c9764ad93c1fa44cd1fd  expected/proposal/simulation.json
56a3cde32f6089fbf9e0972f5bcdb6cccd099630aa638aa0e248e63a86351b57  expected/proposal_shadow/report.md
807299e65d2043e203117b5d97abdfc3670ca8b1ae754792dc23b748a2d739af  expected/proposal_shadow/simulation.json
249af0f30751b44bce786d04f9becd058b942372135b123e78c3a4fec399bf73  expected/scenarios/comparison.json
39728aa86745841c90a029b634e854817b8ebfc3a0ea2e38be9005c061cea302  expected/scenarios/report.md
21390841e1be883cec8a8215aae0e14dbe7c0bf929621bace4852766ea570075  expected/shadow/report.md
fa1e89d8258cbede65a54ff28e15ab4d6dcd5229e82ccb489d20fe4bdfe44187  expected/shadow/simulation.json
e3ad325f0524506c14358611c568a10e5a42eda6d9af336fd9fb8ae160e37a52  inputs/export.ndjson
4adb8bce11f3a79a416cad54e0ff16058449a1dfb8704018dd7d3033ef743c60  inputs/proposal.json
9303b1200552c34e92cec38437e5ce1b58053103bd344b16f8927dfd797f957b  inputs/scenarios.json
5d6cc6e09051a338ad1070ed0789a6e859a34c44f17c112f5c0e92973a8ceadc  inputs/snapshot.json
EOF
)
```

Expected: twelve lines ending `: OK`. If a line says `FAILED`, your tree or environment differs from the one this plan was validated on. Report it before going on. The files you generated are still a valid pin of your own untouched tree, provided Step 1 printed nothing; the later tasks' other tests do not depend on these checksums.

- [ ] **Step 6: Run the test**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_golden.py -p no:warnings -rf
```

Expected: `5 passed`.

- [ ] **Step 7: Commit**

```bash
git add tests/eval/test_uncertainty_golden.py tests/eval/fixtures/uncertainty_golden
git commit -m "$(cat <<'EOF'
test(eval): pin the simulator's output before P3c-3 (U1)

Golden inputs and outputs for the four ways to run policy_simulate.py,
written from the tree before any uncertainty code exists. Every later
task must leave them byte-identical.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The bootstrap engine (spec §4, §5.1, §5.2, §7, §9.1, §11; U2–U7, U11, U13)

**Files:**
- Create: `app/eval/uncertainty.py`
- Create: `tests/eval/test_uncertainty.py`

**Interfaces:**
- Consumes (`app/eval/simulate.py`, unchanged in this task):
  - `RowResult(row: dict, current: ArmResult, proposed: ArmResult)` (frozen dataclass); `row["application_id"]` is the app.
  - `ArmResult` (frozen dataclass), positional fields in order: `outcome` (`"routed"` | `"rejected"`), `model`, `provider`, `tier`, `eligible`, `reject_codes`, `cost_usd`, `latency_ms`, `latency_source`, `quality`, `quality_source`; then defaulted `core`, `core_state`, `complexity_estimated`, `attempted`.
  - `_bucket_stats(rows: list[RowResult]) -> dict` with keys `requests`, `route_changed`, `newly_rejected`, `newly_served`, `cost_usd`, `cost_usd_both_routed`, `quality_mean` (each `{current, proposed, delta}`), `latency_ms` (`{p50, p95}`, each `{current, proposed, delta}`), `onprem_share` (`{current, proposed}`), `overflow_hops`.
  - `_route_changed(r: RowResult) -> bool`; `_parse_ts(ts: str) -> datetime`.
  - `async simulate(records, snapshot, proposal, *, base, since=None) -> tuple[list[RowResult], dict[str, int]]`.
  - `app/eval/outcomes.py`: `_round6(value)`, `_nearest_rank(sorted_values: list[float], pct: float) -> float | None`.
- Produces (`app/eval/uncertainty.py`):
  - `METHOD`, `STRATA`, `NOT_COVERED: tuple[str, ...]` (five ids), `MAX_REPLICATES = 20000`.
  - `UncertaintyPlan(replicates: int = 1000, interval: float = 0.90, seed: int = 0, min_rows: int = 30)` — frozen dataclass; `__post_init__` raises `ValueError` naming the field for a value out of range.
  - `Bootstrap(n: int, totals: dict[str, list] | None, by_app: dict[str, tuple[int, dict[str, list] | None]])`.
  - `window_days(window: dict | None) -> float | None`.
  - `draw_indices(seed: int, replicate: int, app: str, n: int) -> list[int]`.
  - `run_bootstrap(results: list[RowResult], plan: UncertaintyPlan, *, days: float | None = None) -> Bootstrap`. Flat figure names in a samples map: `cost_usd.{current,proposed,delta}`, `cost_usd_both_routed.{current,proposed,delta}`, `route_changed`, `newly_rejected`, `newly_served`, each of those three with `_share` appended, `latency_ms.p50.{current,proposed,delta}`, `latency_ms.p95.{…}`, `quality_mean.{current,proposed,delta}`, `onprem_share.{current,proposed}`; in the totals only and only with `days`: `per_day.cost_usd.{current,proposed,delta}`, `per_day.cost_usd_both_routed.delta`.
  - `interval_of(values: list[float], level: float) -> tuple[float, float]`; `zero_bound(n: int, level: float) -> float`.
  - `leaf(point: float | None, values: list, plan: UncertaintyPlan, *, zero_n: int | None = None) -> dict` — `{"point", "lo", "hi"}` plus `point_outside`, `undefined_replicates`, `zero_bound` when they apply.
  - `results_block(results: list[RowResult], plan: UncertaintyPlan, *, days: float | None = None) -> dict` — keys `totals`, `by_app`, `per_day`, `points_outside_interval`. A bucket is `{"n", <nested leaves>}` or `{"insufficient_data": True, "n"}`.
  - `plan_inputs(plan) -> dict` (`replicates`, `interval`, `seed`, `min_rows`); `plan_block(plan) -> dict` (`method`, `strata`, those four, `not_covered`).
  - Private, used by the tests: `_strata(results) -> tuple[list[_Stratum], list[tuple]]`, `_figures(n, counts, costs, classes, days) -> dict`, `_flatten(stats, n, days) -> dict`, `_weighted_rank(weights: dict[float, int], pct: float) -> float | None`.
- Produces (`tests/eval/test_uncertainty.py`, imported by later test files): `REJECTED: ArmResult`, `_arm(model, cost, latency=800.0, quality=0.75, provider="onprem") -> ArmResult`, `_result(i, app, current, proposed) -> RowResult`, `_synthetic(n=240, seed=7) -> list[RowResult]`, `_leaves(node, path="")` (yields `(dotted path, leaf)`), `_keys(value)`, `BANNED: tuple[str, ...]`.

**Rules.**

- Rows are resampled; nothing in this module calls `simulate_row` or builds a scenario.
- A leaf's `point` comes from `_bucket_stats`, the code that writes the figure into the document. The engine's own figure code (`_figures`) is checked against it by `test_engine_figures_equal_bucket_stats` and must not be used for points.
- Do not add a `numpy` import, a progress bar, or a parallel pool.
- `per_day.cost_usd_both_routed.delta` divides the delta **rounded to 6 places** (it is the comparison table's cell, which is computed that way). Keep the `round(…, 6)` in both `_figures` and `_flatten`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_uncertainty.py`:

````python
"""P3c-3: uncertainty ranges (spec §4-§9, invariants U2-U7, U11).

The engine is tested on hand-built `RowResult`s (fast, every figure
controllable) and against a real replay (`simulate`), so its figure
definitions cannot drift from `simulate._bucket_stats`.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections import Counter

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.eval.outcomes import _nearest_rank
from app.eval.simulate import ArmResult, RowResult, _bucket_stats, simulate
from app.eval.uncertainty import (MAX_REPLICATES, METHOD, NOT_COVERED, UncertaintyPlan,
                                  _figures, _flatten, _strata, _weighted_rank,
                                  draw_indices, interval_of, leaf, plan_block, plan_inputs,
                                  results_block, run_bootstrap, window_days, zero_bound)
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot

P = "routeforge.policy-proposal/v1"
REJECTED = ArmResult("rejected", None, None, None, (), ("over_budget",),
                     None, None, None, None, None)


def _arm(model, cost, latency=800.0, quality=0.75, provider="onprem") -> ArmResult:
    return ArmResult("routed", model, provider, "mid", (model,), (), cost, latency,
                     "observed", quality, "tier_default")


def _result(i: int, app: str, current: ArmResult, proposed: ArmResult) -> RowResult:
    return RowResult({"request_id": f"r{i}", "application_id": app, "workload": None},
                     current, proposed)


def _synthetic(n: int = 240, seed: int = 7) -> list[RowResult]:
    """Three apps. `app-0` is untouched by the proposal; in the other two
    about 30% of rows move to a cheaper, faster, lower-quality model and
    about 4% are newly rejected. Costs differ row by row."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        app = f"app-{i % 3}"
        cost = rng.uniform(0.001, 0.02)
        current = _arm("local-mid", cost)
        roll = rng.random()
        if app == "app-0" or roll >= 0.34:
            proposed = current
        elif roll < 0.30:
            proposed = _arm("local-cheap", cost * 0.2, latency=300.0, quality=0.5)
        else:
            proposed = REJECTED
        out.append(_result(i, app, current, proposed))
    return out


def _leaves(node, path=""):
    """Every `{"point", "lo", "hi"}` leaf under `node`, with its dotted path."""
    if isinstance(node, dict):
        if "point" in node and "lo" in node:
            yield path, node
        else:
            for key, inner in node.items():
                yield from _leaves(inner, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, inner in enumerate(node):
            yield from _leaves(inner, f"{path}[{i}]")


def _width(leaf_: dict) -> float:
    return leaf_["hi"] - leaf_["lo"]


def _simulated(rows, snap, proposal):
    results, _excluded = asyncio.run(simulate(rows, snap, proposal, base=BASE_SETTINGS))
    return results


def _two_app_replay():
    """A real replay: two apps, three sizes, and a proposal that removes the
    mid endpoint (routes move, costs change)."""
    snap = make_snapshot(apps=[app_entry("claims"), app_entry("chat")])
    rows = [make_row(f"r{i:03d}", "claims" if i % 2 else "chat",
                     complexity=[0.05, 0.6, 1.0][i % 3],
                     prompt_tokens=[200, 1200, 3000][i % 3] + i,
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            for i in range(90)]
    return _simulated(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})


def _identity_figures(strata, classes, days):
    """`_figures` over the rows as recorded -- every row drawn exactly once:
    what the engine itself says the point estimate is."""
    counts: Counter = Counter()
    costs = [0.0, 0.0, 0.0, 0.0]
    for s in strata:
        counts.update(s.cls)
        for j, i in enumerate(s.col_of):
            costs[j] += sum(s.cols[i])
    return _figures(sum(s.n for s in strata), counts, costs, classes, days)


# --- the engine agrees with the figures the documents already carry -------------

@pytest.mark.parametrize("build", [_synthetic, _two_app_replay])
def test_engine_figures_equal_bucket_stats(build):
    """The engine recomputes every figure from class counts and cost sums.
    Drawing each row exactly once must give `_bucket_stats`' own numbers,
    for the totals and for each app."""
    results = build()
    strata, classes = _strata(results)
    expected = _flatten(_bucket_stats(results), len(results), 2.0)
    got = _identity_figures(strata, classes, 2.0)
    # This one figure divides a delta ROUNDED to 6 dp (it mirrors the
    # comparison table's cell), so a last-bit difference in a float sum can
    # move it by one rounding step.
    rounded = "per_day.cost_usd_both_routed.delta"
    assert got.pop(rounded) == pytest.approx(expected.pop(rounded), abs=1e-6)
    assert got == pytest.approx(expected, abs=1e-9)
    for s in strata:
        rows = [r for r in results if r.row["application_id"] == s.app]
        assert _identity_figures([s], classes, None) == pytest.approx(
            _flatten(_bucket_stats(rows), s.n, None), abs=1e-9)


def test_equal_cost_columns_are_stored_once():
    """An app the proposal does not touch has one distinct cost column."""
    strata, _classes = _strata(_synthetic())
    untouched = next(s for s in strata if s.app == "app-0")
    assert len(untouched.cols) == 1 and untouched.col_of == (0, 0, 0, 0)
    touched = next(s for s in strata if s.app == "app-1")
    assert len(touched.cols) > 1


@given(st.lists(st.tuples(st.floats(0, 5000, allow_nan=False), st.integers(1, 6)),
                min_size=0, max_size=12),
       st.sampled_from([50, 95, 1, 100]))
@hsettings(max_examples=60, deadline=None)
def test_weighted_rank_matches_nearest_rank(pairs, pct):
    weights: dict[float, int] = {}
    for value, count in pairs:
        weights[value] = weights.get(value, 0) + count
    expanded = sorted(v for v, c in weights.items() for _ in range(c))
    assert _weighted_rank(weights, pct) == _nearest_rank(expanded, pct)


# --- U2: deterministic ----------------------------------------------------------

def test_u2_same_plan_same_block_and_the_seed_changes_it():                # U2
    results = _synthetic()
    plan = UncertaintyPlan(replicates=200)
    first = json.dumps(results_block(results, plan, days=3.0), sort_keys=True)
    assert json.dumps(results_block(results, plan, days=3.0), sort_keys=True) == first
    other = json.dumps(results_block(results, UncertaintyPlan(replicates=200, seed=1),
                                     days=3.0), sort_keys=True)
    assert other != first


def test_u2_the_global_random_state_is_not_read_or_moved():                # U2
    results = _synthetic(60)
    random.seed(123)
    before = random.getstate()
    a = results_block(results, UncertaintyPlan(replicates=20))
    assert random.getstate() == before
    random.seed(999)
    assert results_block(results, UncertaintyPlan(replicates=20)) == a


# --- U3: the point and its own range --------------------------------------------

def test_u3_every_point_is_inside_its_range_or_is_flagged():                # U3
    block = results_block(_synthetic(), UncertaintyPlan(replicates=400), days=3.0)
    leaves = list(_leaves(block))
    assert len(leaves) > 40
    for path, node in leaves:
        if node["lo"] is None:
            assert "undefined_replicates" in node, path
        elif not node["lo"] <= node["point"] <= node["hi"]:
            assert node.get("point_outside") is True, path
        else:
            assert "point_outside" not in node, path
    # On this data, at 400 replicates and a 90% interval, no point is outside.
    assert block["points_outside_interval"] == 0


def test_u3_one_replicate_can_leave_the_point_outside_and_says_so():        # U3
    """A percentile interval does not promise to contain the point. With one
    replicate `lo == hi ==` that replicate's value, which is not the point;
    the leaf says so and the block counts it. Nothing is widened."""
    block = results_block(_synthetic(), UncertaintyPlan(replicates=1))
    cost = block["totals"]["cost_usd"]["delta"]
    assert cost["lo"] == cost["hi"] != cost["point"]
    assert cost["point_outside"] is True
    assert block["points_outside_interval"] == sum(
        1 for _path, node in _leaves(block) if node.get("point_outside"))
    assert block["points_outside_interval"] > 0


def test_leaf_point_equals_the_figure_the_document_carries():
    """`point` is `_bucket_stats`' own value (rounded as the document is)."""
    results = _two_app_replay()
    block = results_block(results, UncertaintyPlan(replicates=50))
    stats = _bucket_stats(results)
    totals = block["totals"]
    assert totals["n"] == stats["requests"] == 90
    for key in ("current", "proposed", "delta"):
        assert totals["cost_usd"][key]["point"] == round(stats["cost_usd"][key], 6)
        assert totals["latency_ms"]["p95"][key]["point"] == stats["latency_ms"]["p95"][key]
    assert totals["route_changed"]["point"] == stats["route_changed"]
    assert totals["route_changed_share"]["point"] == round(stats["route_changed"] / 90, 6)
    assert set(block["by_app"]) == {"chat", "claims"}


# --- U4: paired -----------------------------------------------------------------

def _delta_leaves(block):
    return [(path, node) for path, node in _leaves(block)
            if path.endswith(".delta") or path.split(".")[-1] in (
                "route_changed", "newly_rejected", "newly_served", "route_changed_share",
                "newly_rejected_share", "newly_served_share")]


def test_u4_noop_proposal_has_every_delta_range_exactly_zero():             # U4
    """Both arms are the same rows in every replicate, so a proposal that
    changes nothing has a delta of exactly 0 in every replicate -- not a
    small range around 0, which two independent draws would give."""
    snap = make_snapshot(apps=[app_entry("claims"), app_entry("chat")])
    rows = [make_row(f"r{i:03d}", "claims" if i % 2 else "chat",
                     complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=200 + 37 * i,
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            for i in range(80)]
    block = results_block(_simulated(rows, snap, {"schema": P}),
                          UncertaintyPlan(replicates=200), days=2.0)
    deltas = _delta_leaves(block)
    # 11 per bucket (5 deltas, 3 counts, 3 shares) x (totals + 2 apps), + 2 per-day.
    assert len(deltas) == 35
    for path, node in deltas:
        assert (node["point"], node["lo"], node["hi"]) == (0, 0, 0), path
    # ...while the two arms themselves do vary from replicate to replicate.
    assert _width(block["totals"]["cost_usd"]["current"]) > 0
    assert block["totals"]["cost_usd"]["current"] == block["totals"]["cost_usd"]["proposed"]


def test_u4_an_app_is_drawn_the_same_way_alone_and_inside_the_whole_export():  # U4
    """The draw depends on (seed, replicate, app, row count) only. That is
    what pairs a shadow-price relaxation scoped to one app with the main
    run, and one scenario with the next."""
    results = _synthetic()
    plan = UncertaintyPlan(replicates=100)
    whole = results_block(results, plan)
    alone = results_block([r for r in results if r.row["application_id"] == "app-1"], plan)
    assert alone["by_app"]["app-1"] == whole["by_app"]["app-1"]
    assert alone["totals"] == whole["by_app"]["app-1"]


# --- U5: ranges narrow with more rows ---------------------------------------------

def test_u5_ranges_narrow_as_the_rows_are_replicated():                    # U5
    """The same rows 1x, 4x and 16x: a sum's relative width, and a share's
    or a mean's width, shrink at each step (about 1/sqrt(k))."""
    base = _synthetic(240)
    plan = UncertaintyPlan(replicates=300)

    def measures(k: int) -> tuple[float, float, float, float]:
        rows = [_result(j * 1000 + i, r.row["application_id"], r.current, r.proposed)
                for j in range(k) for i, r in enumerate(base)]
        totals = results_block(rows, plan)["totals"]
        cost = totals["cost_usd"]["delta"]
        return (_width(cost) / abs(cost["point"]),
                _width(totals["newly_rejected_share"]),
                _width(totals["route_changed_share"]),
                _width(totals["quality_mean"]["delta"]))

    one, four, sixteen = measures(1), measures(4), measures(16)
    for a, b, c in zip(one, four, sixteen):
        assert a > b > c > 0
    # Roughly halves per 4x; a loose bound that still fails for a range that
    # does not depend on the row count at all.
    assert sixteen[0] < 0.5 * one[0]


# --- U6: stratification -----------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 7, 500])
def test_u6_every_replicate_draws_exactly_the_apps_row_count(n):           # U6
    for replicate in range(20):
        idx = draw_indices(3, replicate, "app-x", n)
        assert len(idx) == n and all(0 <= i < n for i in idx)


def test_u6_a_one_row_app_is_present_in_every_replicate():                 # U6
    """One row among 300: an unstratified resample would leave the app out
    of about a third of the replicates. Stratified, it is drawn every time,
    so none of its figures is ever undefined."""
    results = _synthetic(300) + [_result(9999, "tiny", _arm("local-mid", 0.01),
                                         _arm("local-cheap", 0.002, latency=300.0))]
    block = results_block(results, UncertaintyPlan(replicates=100, min_rows=1))
    tiny = block["by_app"]["tiny"]
    assert tiny["n"] == 1
    nodes = list(_leaves(tiny))
    assert nodes
    for path, node in nodes:
        assert "undefined_replicates" not in node, path
        assert node["lo"] == node["hi"] == node["point"], path


# --- U7: insufficient data ----------------------------------------------------------

def test_u7_a_small_app_gets_no_range_and_says_how_many_rows():            # U7
    results = _synthetic(240) + [
        _result(5000 + i, "small", _arm("local-mid", 0.01), REJECTED) for i in range(5)]
    block = results_block(results, UncertaintyPlan(replicates=50, min_rows=30))
    assert block["by_app"]["small"] == {"insufficient_data": True, "n": 5}
    assert "insufficient_data" not in block["by_app"]["app-1"]
    # Its rows still count in the totals.
    assert block["totals"]["n"] == 245
    assert block["totals"]["newly_rejected"]["point"] >= 5


def test_u7_too_few_rows_in_all_draws_nothing():                           # U7
    results = _synthetic(12)
    boot = run_bootstrap(results, UncertaintyPlan(replicates=50, min_rows=30), days=3.0)
    assert boot.totals is None and all(samples is None for _n, samples in boot.by_app.values())
    block = results_block(results, UncertaintyPlan(replicates=50, min_rows=30), days=3.0)
    assert block["totals"] == {"insufficient_data": True, "n": 12}
    assert block["per_day"] is None and block["points_outside_interval"] == 0
    assert all(b == {"insufficient_data": True, "n": 4} for b in block["by_app"].values())


def test_u7_no_rows_at_all():                                              # U7
    block = results_block([], UncertaintyPlan())
    assert block == {"totals": {"insufficient_data": True, "n": 0}, "by_app": {},
                     "per_day": None, "points_outside_interval": 0}


def test_min_rows_is_inclusive():
    results = _synthetic(30)
    assert "insufficient_data" not in results_block(
        results, UncertaintyPlan(replicates=10, min_rows=30))["totals"]
    assert results_block(results, UncertaintyPlan(replicates=10, min_rows=31))["totals"] == {
        "insufficient_data": True, "n": 30}


# --- zero counts, undefined figures, per-day ----------------------------------------

def test_a_zero_count_is_zero_in_every_replicate_and_carries_a_bound():
    """`app-0` has no newly rejected row, so no resample of it can have one:
    the range is [0, 0]. That is not "no risk", so the share carries the
    largest rate that would still show nothing in n rows."""
    block = results_block(_synthetic(240), UncertaintyPlan(replicates=50))
    app0 = block["by_app"]["app-0"]
    assert app0["newly_rejected"] == {"point": 0, "lo": 0, "hi": 0}
    share = app0["newly_rejected_share"]
    assert (share["point"], share["lo"], share["hi"]) == (0, 0, 0)
    assert share["zero_bound"] == pytest.approx(zero_bound(80, 0.90), abs=1e-6)
    assert "zero_bound" not in block["by_app"]["app-1"]["newly_rejected_share"]


def test_zero_bound_values():
    assert zero_bound(1, 0.90) == pytest.approx(0.9)
    assert zero_bound(24, 0.90) == pytest.approx(0.0915, abs=1e-4)      # the DGX lab's 24 rows
    assert zero_bound(2_000_000, 0.90) == pytest.approx(1.1513e-6, rel=1e-3)
    assert zero_bound(100, 0.95) == pytest.approx(0.0295, abs=1e-4)     # the "rule of three"
    # (1 - bound) ** n is exactly the chance of seeing nothing at that rate.
    assert (1 - zero_bound(50, 0.90)) ** 50 == pytest.approx(0.10)


def test_a_figure_undefined_in_some_replicate_has_no_range():
    """One routed row among forty in the proposed arm: a replicate that does
    not draw it has no proposed latency at all. An interval over the other
    replicates would be conditional on the figure existing, so there is none."""
    results = [_result(i, "a", _arm("local-mid", 0.01), REJECTED) for i in range(39)]
    results.append(_result(39, "a", _arm("local-mid", 0.01), _arm("local-mid", 0.01)))
    plan = UncertaintyPlan(replicates=200)
    block = results_block(results, plan)
    node = block["totals"]["latency_ms"]["p50"]["proposed"]
    assert node["point"] == 800.0 and node["lo"] is None and node["hi"] is None
    assert 0 < node["undefined_replicates"] < 200
    assert "point_outside" not in node
    # The current arm routes every row, so its latency is always defined.
    assert block["totals"]["latency_ms"]["p50"]["current"] == {
        "point": 800.0, "lo": 800.0, "hi": 800.0}


def test_per_day_divides_by_the_fixed_window():
    results = _synthetic(240)
    plan = UncertaintyPlan(replicates=100)
    block = results_block(results, plan, days=4.0)
    for key in ("current", "proposed", "delta"):
        day, whole = block["per_day"]["cost_usd"][key], block["totals"]["cost_usd"][key]
        for part in ("point", "lo", "hi"):
            assert day[part] == pytest.approx(whole[part] / 4.0, abs=1e-6)
    both = block["per_day"]["cost_usd_both_routed"]["delta"]
    assert both["point"] == pytest.approx(
        block["totals"]["cost_usd_both_routed"]["delta"]["point"] / 4.0, abs=1e-6)
    assert "per_day" not in block["totals"]
    assert results_block(results, plan)["per_day"] is None


def test_window_days():
    assert window_days(None) is None
    assert window_days({"start": "2026-09-20T00:00:00+00:00",
                        "end": "2026-09-20T23:59:59+00:00"}) is None        # under a day
    assert window_days({"start": "2026-09-20T00:00:00+00:00",
                        "end": "2026-09-22T12:00:00Z"}) == pytest.approx(2.5)


# --- small pieces ---------------------------------------------------------------------

def test_interval_of():
    values = [float(v) for v in range(1000)]
    random.Random(0).shuffle(values)
    assert interval_of(values, 0.90) == (50.0, 949.0)
    assert interval_of(values, 0.50) == (250.0, 749.0)
    assert interval_of([3.0], 0.90) == (3.0, 3.0)
    assert interval_of([1.0, 2.0], 0.90) == (1.0, 2.0)
    assert interval_of([1.0, 2.0, 3.0], 0.02) == (2.0, 2.0)


def test_leaf_rounds_and_flags():
    plan = UncertaintyPlan(replicates=4, interval=0.5)
    assert leaf(0.1234567, [0.1, 0.12, 0.13, 0.2], plan) == {
        "point": 0.123457, "lo": 0.12, "hi": 0.13}
    assert leaf(0.5, [0.1, 0.12, 0.13, 0.2], plan) == {
        "point": 0.5, "lo": 0.12, "hi": 0.13, "point_outside": True}
    assert leaf(None, [None, None, None, None], plan) == {
        "point": None, "lo": None, "hi": None, "undefined_replicates": 4}
    assert leaf(-0.0, [-0.0] * 4, plan) == {"point": 0.0, "lo": 0.0, "hi": 0.0}


@pytest.mark.parametrize("kwargs", [
    {"replicates": 0}, {"replicates": MAX_REPLICATES + 1}, {"replicates": 10.0},
    {"replicates": True}, {"interval": 0.0}, {"interval": 1.0}, {"interval": float("nan")},
    {"interval": -0.1}, {"seed": -1}, {"seed": 1.5}, {"min_rows": 0}, {"min_rows": 2.5}])
def test_plan_refuses_values_out_of_range(kwargs):
    with pytest.raises(ValueError):
        UncertaintyPlan(**kwargs)


def test_plan_block_and_inputs():
    plan = UncertaintyPlan(replicates=250, interval=0.8, seed=4, min_rows=10)
    assert plan_inputs(plan) == {"replicates": 250, "interval": 0.8, "seed": 4, "min_rows": 10}
    assert plan_block(plan) == {
        "method": METHOD, "strata": "application_id", "replicates": 250, "interval": 0.8,
        "seed": 4, "min_rows": 10, "not_covered": list(NOT_COVERED)}
    assert UncertaintyPlan() == UncertaintyPlan(replicates=1000, interval=0.90, seed=0,
                                                min_rows=30)


# --- U11: nothing ranks ---------------------------------------------------------------

def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


BANNED = ("rank", "recommend", "best", "winner", "prefer", "score")


def test_u11_no_key_ranks_or_recommends():                                 # U11
    block = {**plan_block(UncertaintyPlan()),
             **results_block(_synthetic(), UncertaintyPlan(replicates=20), days=3.0)}
    assert not [k for k in _keys(block) if any(word in k.lower() for word in BANNED)]
    assert not [v for v in block["not_covered"] if any(word in v for word in BANNED)]
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty.py -p no:warnings -rf
```

Expected: a collection error, `ModuleNotFoundError: No module named 'app.eval.uncertainty'`.

- [ ] **Step 3: Write the module**

Create `app/eval/uncertainty.py`:

````python
"""P3c-3: uncertainty ranges for the policy simulator (offline, operator-run).

Every simulator figure is a point estimate from one replay of one export.
This module puts a range beside it: a percentile interval from a bootstrap
over the simulated rows.

- **Rows are resampled, never re-simulated.** Replay is a deterministic
  function of a row, so each row's two `ArmResult`s are computed once
  (`app/eval/simulate.py`) and a replicate only draws row positions.
- **Stratified by application.** Each app's rows are resampled to the same
  count, so a per-app bucket never empties and the mix of traffic between
  apps is held fixed.
- **Paired.** The draw for `(seed, replicate, app)` depends on nothing else,
  so both arms, every scenario and every shadow-price relaxation see the
  same resampled rows. A delta's range is the range of the paired
  difference.
- **Deterministic.** Same rows, plan and Python minor version give the same
  numbers. Nothing here reads a clock or the global `random` state.

The range is sampling variability of the recorded rows under this replay
model, and nothing else -- `UNCERTAINTY_CAVEAT` and `NOT_COVERED` say what
it leaves out, and every report prints the caveat.

RouteForge never ranks or recommends: nothing here orders scenarios or
relaxations, and no key in the output says which is better.

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass

from app.eval.outcomes import _round6
from app.eval.simulate import RowResult, _bucket_stats, _parse_ts, _route_changed

METHOD = "stratified_paired_row_bootstrap"
STRATA = "application_id"

#: What a range does NOT include, as stable ids for `uncertainty.not_covered`.
NOT_COVERED = ("replay_model_error", "admission_estimate_error", "unrecorded_inputs",
               "traffic_shift", "per_request_latency")

MAX_REPLICATES = 20000
_COUNTS = ("route_changed", "newly_rejected", "newly_served")


@dataclass(frozen=True)
class UncertaintyPlan:
    """What `--uncertainty` asked for. The defaults are the CLI's."""

    replicates: int = 1000
    interval: float = 0.90
    seed: int = 0
    min_rows: int = 30

    def __post_init__(self) -> None:
        if (not isinstance(self.replicates, int) or isinstance(self.replicates, bool)
                or not 1 <= self.replicates <= MAX_REPLICATES):
            raise ValueError(f"replicates must be an int from 1 to {MAX_REPLICATES}; "
                             f"got {self.replicates!r}")
        # `not (0 < x < 1)` also refuses NaN, which compares false both ways.
        if isinstance(self.interval, bool) or not 0.0 < self.interval < 1.0:
            raise ValueError(f"interval must be strictly between 0 and 1; got {self.interval!r}")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError(f"seed must be an int >= 0; got {self.seed!r}")
        if (not isinstance(self.min_rows, int) or isinstance(self.min_rows, bool)
                or self.min_rows < 1):
            raise ValueError(f"min_rows must be an int >= 1; got {self.min_rows!r}")


@dataclass(frozen=True)
class Bootstrap:
    """Raw replicate values. `totals` maps a figure name to one value per
    replicate, or is None when there are fewer than `min_rows` rows.
    `by_app` maps an app id to `(row count, the same map or None)`."""

    n: int
    totals: dict[str, list] | None
    by_app: dict[str, tuple[int, dict[str, list] | None]]


@dataclass(frozen=True)
class _Stratum:
    app: str
    n: int
    cls: list[int]                      # class id per row, in row order
    cols: list[list[float]]             # the DISTINCT cost columns
    col_of: tuple[int, ...]             # column index of (current, proposed, both-current, both-proposed)


def window_days(window: dict | None) -> float | None:
    """The export window in days, or None when there is no window or it is
    shorter than one day -- the rule `simulate._per_day` and
    `shadow._window_days` apply before printing a per-day figure."""
    if window is None:
        return None
    days = (_parse_ts(window["end"]) - _parse_ts(window["start"])).total_seconds() / 86400
    return days if days >= 1 else None


def _arm_sig(arm) -> tuple:
    """Everything a figure reads from one arm of one row EXCEPT its cost:
    `(routed, on-prem, latency_ms, quality)`. Latency and quality are
    properties of the picked model, so rows share a small number of these."""
    if arm.outcome != "routed":
        return (False, False, None, None)
    return (True, arm.provider == "onprem", arm.latency_ms, arm.quality)


def _strata(results: list[RowResult]) -> tuple[list[_Stratum], list[tuple]]:
    """One stratum per application (sorted by id), plus the class table.

    A row's CLASS is `(current signature, proposed signature, route
    changed)`. Every figure except cost is a function of how many rows of
    each class a replicate drew, so a replicate counts classes instead of
    walking rows. Cost is the one per-row value: four columns per stratum,
    with equal columns stored once (an app the proposal does not touch has
    one distinct column, not four)."""
    classes: dict[tuple, int] = {}
    by_app: dict[str, list[RowResult]] = {}
    for r in results:
        by_app.setdefault(r.row["application_id"], []).append(r)
    strata: list[_Stratum] = []
    for app in sorted(by_app):
        cls: list[int] = []
        columns: tuple[list[float], ...] = ([], [], [], [])
        for r in by_app[app]:
            key = (_arm_sig(r.current), _arm_sig(r.proposed), _route_changed(r))
            cls.append(classes.setdefault(key, len(classes)))
            current = (r.current.cost_usd or 0.0) if r.current.outcome == "routed" else 0.0
            proposed = (r.proposed.cost_usd or 0.0) if r.proposed.outcome == "routed" else 0.0
            both = r.current.outcome == "routed" and r.proposed.outcome == "routed"
            for column, value in zip(columns, (current, proposed,
                                               current if both else 0.0,
                                               proposed if both else 0.0)):
                column.append(value)
        cols: list[list[float]] = []
        col_of: list[int] = []
        for column in columns:
            for i, seen in enumerate(cols):
                if seen == column:
                    col_of.append(i)
                    break
            else:
                cols.append(column)
                col_of.append(len(cols) - 1)
        strata.append(_Stratum(app, len(cls), cls, cols, tuple(col_of)))
    return strata, list(classes)


def _weighted_rank(weights: dict[float, int], pct: float) -> float | None:
    """`outcomes._nearest_rank` over a multiset given as `{value: count}`."""
    n = sum(weights.values())
    if n == 0:
        return None
    rank = max(1, min(n, math.ceil(pct / 100 * n)))
    seen = 0
    for value in sorted(weights):
        seen += weights[value]
        if seen >= rank:
            return value
    return None


def _put(out: dict, name: str, current: float | None, proposed: float | None) -> None:
    out[f"{name}.current"] = current
    out[f"{name}.proposed"] = proposed
    out[f"{name}.delta"] = (proposed - current
                            if current is not None and proposed is not None else None)


def _figures(n: int, counts: dict[int, int], costs: list[float], classes: list[tuple],
             days: float | None) -> dict[str, float | None]:
    """Every figure of one bucket, from its class counts and four cost sums.
    The same definitions as `simulate._bucket_stats`, stated on counts;
    `tests/eval/test_uncertainty.py` pins the two against each other."""
    tally = dict.fromkeys(_COUNTS, 0)
    routed, onprem, q_n = [0, 0], [0, 0], [0, 0]
    q_sum = [0.0, 0.0]
    latency: tuple[dict, dict] = ({}, {})
    for cid, m in counts.items():
        current, proposed, changed = classes[cid]
        if changed:
            tally["route_changed"] += m
        if current[0] and not proposed[0]:
            tally["newly_rejected"] += m
        if proposed[0] and not current[0]:
            tally["newly_served"] += m
        for a, (is_routed, is_onprem, lat, quality) in enumerate((current, proposed)):
            if not is_routed:
                continue
            routed[a] += m
            if is_onprem:
                onprem[a] += m
            if lat is not None:
                latency[a][lat] = latency[a].get(lat, 0) + m
            if quality is not None:
                q_sum[a] += m * quality
                q_n[a] += m

    out: dict[str, float | None] = {}
    _put(out, "cost_usd", costs[0], costs[1])
    _put(out, "cost_usd_both_routed", costs[2], costs[3])
    for name in _COUNTS:
        out[name] = tally[name]
        out[f"{name}_share"] = tally[name] / n if n else None
    for label, pct in (("p50", 50), ("p95", 95)):
        _put(out, f"latency_ms.{label}",
             _weighted_rank(latency[0], pct), _weighted_rank(latency[1], pct))
    _put(out, "quality_mean",
         q_sum[0] / q_n[0] if q_n[0] else None, q_sum[1] / q_n[1] if q_n[1] else None)
    out["onprem_share.current"] = onprem[0] / routed[0] if routed[0] else None
    out["onprem_share.proposed"] = onprem[1] / routed[1] if routed[1] else None
    if days is not None:
        _put(out, "per_day.cost_usd", costs[0] / days, costs[1] / days)
        # The comparison table's own arithmetic (`scenarios._table_row` divides
        # the ROUNDED like-for-like delta), so this leaf's point is that cell.
        out["per_day.cost_usd_both_routed.delta"] = round(costs[3] - costs[2], 6) / days
    return out


def draw_indices(seed: int, replicate: int, app: str, n: int) -> list[int]:
    """The row positions replicate `replicate` draws for one app: `n` draws
    with replacement from `range(n)`. A function of its four arguments and
    nothing else -- that is the pairing (module docstring)."""
    return random.Random(f"{seed}:{replicate}:app:{app}").choices(range(n), k=n)


def _record(samples: dict[str, list], figures: dict[str, float | None]) -> None:
    for name, value in figures.items():
        samples.setdefault(name, []).append(value)


def run_bootstrap(results: list[RowResult], plan: UncertaintyPlan, *,
                  days: float | None = None) -> Bootstrap:
    """`plan.replicates` stratified resamples of `results`. `days` (the
    export window, fixed, never resampled) adds the `per_day.*` figures to
    the totals. A bucket with fewer than `plan.min_rows` rows gets no
    samples; its rows are still drawn, because they are part of the totals.
    With fewer than `min_rows` rows in all, nothing is drawn."""
    strata, classes = _strata(results)
    n_total = len(results)
    if n_total < plan.min_rows:
        return Bootstrap(n_total, None, {s.app: (s.n, None) for s in strata})
    totals: dict[str, list] = {}
    by_app: dict[str, tuple[int, dict[str, list] | None]] = {
        s.app: (s.n, {} if s.n >= plan.min_rows else None) for s in strata}
    for b in range(plan.replicates):
        total_counts: Counter = Counter()
        total_costs = [0.0, 0.0, 0.0, 0.0]
        for s in strata:
            idx = draw_indices(plan.seed, b, s.app, s.n)
            counts = Counter(map(s.cls.__getitem__, idx))
            sums = [sum(map(col.__getitem__, idx)) for col in s.cols]
            costs = [sums[i] for i in s.col_of]
            app_samples = by_app[s.app][1]
            if app_samples is not None:
                _record(app_samples, _figures(s.n, counts, costs, classes, None))
            total_counts.update(counts)
            for j, value in enumerate(costs):
                total_costs[j] += value
        _record(totals, _figures(n_total, total_counts, total_costs, classes, days))
    return Bootstrap(n_total, totals, by_app)


def _flatten(stats: dict, n: int, days: float | None) -> dict[str, float | None]:
    """A `_bucket_stats` block as the engine's flat figure names -- the
    POINT of every leaf, taken from the code that writes the figure itself."""
    out: dict[str, float | None] = {}
    for name in ("cost_usd", "cost_usd_both_routed", "quality_mean"):
        for key in ("current", "proposed", "delta"):
            out[f"{name}.{key}"] = stats[name][key]
    for label in ("p50", "p95"):
        for key in ("current", "proposed", "delta"):
            out[f"latency_ms.{label}.{key}"] = stats["latency_ms"][label][key]
    for name in _COUNTS:
        out[name] = stats[name]
        out[f"{name}_share"] = stats[name] / n if n else None
    out["onprem_share.current"] = stats["onprem_share"]["current"]
    out["onprem_share.proposed"] = stats["onprem_share"]["proposed"]
    if days is not None:
        for key in ("current", "proposed", "delta"):
            value = stats["cost_usd"][key]
            out[f"per_day.cost_usd.{key}"] = value / days if value is not None else None
        both = stats["cost_usd_both_routed"]["delta"]
        out["per_day.cost_usd_both_routed.delta"] = (round(both, 6) / days
                                                     if both is not None else None)
    return out


def interval_of(values: list[float], level: float) -> tuple[float, float]:
    """The equal-tailed percentile interval of `values` at `level`: with
    `k = floor((1 - level) / 2 * N)`, the `(k+1)`-th smallest and the
    `(k+1)`-th largest. One replicate gives `lo == hi`."""
    ordered = sorted(values)
    k = int(math.floor((1.0 - level) / 2 * len(ordered) + 1e-9))
    return ordered[k], ordered[len(ordered) - 1 - k]


def zero_bound(n: int, level: float) -> float:
    """For a count that is 0 in all `n` rows: the largest per-row rate that
    would still show no such row in `n` independent draws with probability
    `1 - level`, i.e. `1 - (1 - level) ** (1 / n)`. At the default 0.90 it
    is about `2.3 / n` (the "rule of three" is the same formula at 0.95).
    It assumes independent rows; correlated rows make the true bound larger."""
    return 1.0 - (1.0 - level) ** (1.0 / n)


def leaf(point: float | None, values: list, plan: UncertaintyPlan, *,
         zero_n: int | None = None) -> dict:
    """One figure's `{"point", "lo", "hi"}`, rounded to 6 dp.

    - A figure undefined (None) in ANY replicate has no range: `lo`/`hi` are
      null and `undefined_replicates` says in how many. An interval over the
      defined replicates only would be conditional on the figure existing.
    - `point_outside: true` when the point is not inside its own range. A
      percentile interval does not promise that; it is reported, not hidden
      by widening the range.
    - `zero_n` (a share whose count is 0 in all `zero_n` rows) adds
      `zero_bound`."""
    out: dict = {"point": point, "lo": None, "hi": None}
    undefined = sum(1 for v in values if v is None)
    if undefined:
        out["undefined_replicates"] = undefined
        return _round6(out)
    out["lo"], out["hi"] = interval_of(values, plan.interval)
    if zero_n:
        out["zero_bound"] = zero_bound(zero_n, plan.interval)
    out = _round6(out)
    if out["point"] is not None and not out["lo"] <= out["point"] <= out["hi"]:
        out["point_outside"] = True
    return out


def _nest(flat: dict[str, dict]) -> dict:
    out: dict = {}
    for name, value in flat.items():
        node = out
        *parents, last = name.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    return out


def _bucket(n: int, points: dict[str, float | None], samples: dict[str, list] | None,
            plan: UncertaintyPlan) -> dict:
    """One bucket of the block: `{"n", <figure tree of leaves>}`, or
    `{"insufficient_data": true, "n"}` below `plan.min_rows`."""
    if samples is None:
        return {"insufficient_data": True, "n": n}
    flat = {}
    for name, values in samples.items():
        zero = n if name.endswith("_share") and points[name] == 0 else None
        flat[name] = leaf(points[name], values, plan, zero_n=zero)
    return {"n": n, **_nest(flat)}


def _count_outside(node) -> int:
    if isinstance(node, dict):
        return (1 if node.get("point_outside") else 0) + sum(
            _count_outside(v) for v in node.values())
    return 0


def results_block(results: list[RowResult], plan: UncertaintyPlan, *,
                  days: float | None = None) -> dict:
    """The ranges for ONE proposal's rows: `totals`, `by_app` (sorted),
    `per_day` (None without a window of at least a day) and
    `points_outside_interval`. Points come from `simulate._bucket_stats`,
    so a leaf's `point` is the figure the document already carries."""
    boot = run_bootstrap(results, plan, days=days)
    totals = _bucket(boot.n, _flatten(_bucket_stats(results), boot.n, days), boot.totals, plan)
    per_day = totals.pop("per_day", None)
    rows_of: dict[str, list[RowResult]] = {}
    for r in results:
        rows_of.setdefault(r.row["application_id"], []).append(r)
    by_app = {app: _bucket(n, _flatten(_bucket_stats(rows_of[app]), n, None), samples, plan)
              for app, (n, samples) in boot.by_app.items()}
    block = {"totals": totals, "by_app": by_app, "per_day": per_day}
    block["points_outside_interval"] = _count_outside(block)
    return block


def plan_inputs(plan: UncertaintyPlan) -> dict:
    """What enters a document's `inputs` (and so its id) with `--uncertainty`."""
    return {"replicates": plan.replicates, "interval": plan.interval, "seed": plan.seed,
            "min_rows": plan.min_rows}


def plan_block(plan: UncertaintyPlan) -> dict:
    """The head of every `uncertainty` block: how the ranges were made and
    what they leave out."""
    return {"method": METHOD, "strata": STRATA, **plan_inputs(plan),
            "not_covered": list(NOT_COVERED)}
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty.py tests/eval/test_uncertainty_golden.py -p no:warnings -rf
```

Expected: `47 passed`.

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `911 passed`, no `FAILED` line. It takes about six minutes.

- [ ] **Step 6: Commit**

```bash
git add app/eval/uncertainty.py tests/eval/test_uncertainty.py
git commit -m "$(cat <<'EOF'
feat(eval): stratified paired bootstrap over replayed rows (P3c-3)

Rows are resampled within each application, never re-simulated. The draw
for an app in a replicate depends only on (seed, replicate, app, rows),
so both arms and every later scope see the same resamples. Below
min_rows a bucket reports insufficient data; a zero count carries a
bound; a point outside its range is flagged, not hidden.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Quality-label uncertainty from the calibration file (spec §6, U4, U8)

**Files:**
- Modify: `app/eval/simulate.py` (`ArmResult`, after `attempted`; the last `return ArmResult(...)` of `simulate_row`)
- Modify: `app/eval/uncertainty.py`
- Create: `tests/eval/test_uncertainty_quality.py`
- Modify: `tests/eval/test_uncertainty.py` (three call sites and two expected dicts)

**Interfaces:**
- Consumes:
  - Task 2's `app/eval/uncertainty.py` and `tests/eval/test_uncertainty.py`.
  - `app/eval/calibration.py`: `recommendation_set_id(rec: dict) -> str` (first 16 hex of sha256 over the canonical JSON without `set_id`); for tests, `estimate(records, *, k=10.0, min_labels=20, max_ci_width=0.25, min_shift=0.02, priors=None, registry_version=None) -> dict`, `build_recommendation(estimate_dict, *, export_sha256) -> dict`, `dump_recommendation(rec) -> str`, `_beta_ppf(p, a, b) -> float`. A recommendation's `units[i]` has `model`, `workload` (`None` for the `(model, *)` unit), `alpha`, `beta`, `mean`, `status` (`"recommended"` | `"insufficient"` | `"no_change"`).
  - `app/routing/quality.py::QualityScore.set_id` — the calibration set of a `calibrated` score, `None` otherwise. `simulate_row` already holds it as `q = scn.quality.resolve(pick.name, row.get("workload"))`.
  - A snapshot's `quality.calibrated` is a list of `{"model", "workload", "score", "set_id"}`.
- Produces:
  - `ArmResult.quality_set_id: str | None = None` (the last field).
  - `app/eval/uncertainty.py`: `ARMS = ("current", "proposed")`; `PosteriorKey = tuple[str, str, str | None]` (`(set_id, model, workload)`); `Posterior(alpha: float, beta: float, mean: float)`; `UncertaintyError(where: str, message: str)` (a `ValueError`, with `.where` and `.message`).
  - `UncertaintyPlan` gains `posteriors: dict[PosteriorKey, Posterior]` (default empty) and `posterior_files: tuple[tuple[str, str], ...]` (default `()`; `(set_id, sha256 of the file)`).
  - `load_posteriors(paths: list[str]) -> tuple[dict[PosteriorKey, Posterior], tuple[tuple[str, str], ...]]` — raises `UncertaintyError`.
  - `posterior_key(arm: ArmResult, row: dict, posteriors) -> PosteriorKey | None`.
  - `draw_quality(seed: int, replicate: int, key: PosteriorKey, post: Posterior) -> float`.
  - `quality_coverage(results, plan) -> dict` — `{"current": {"routed", "calibrated", "posterior"}, "proposed": {…}}`.
  - Changed signatures: `_strata(results, posteriors)`; `_figures(n, counts, costs, classes, days, qdraw=None)`. `results_block` gains the key `quality_posterior`; `plan_inputs` gains `quality_posteriors_sha256` (a list); `plan_block` gains `quality_posterior_sets` (a list).

**Rules.**

- A posterior attaches to a routed arm only when: `quality_source == "calibrated"`, `quality_set_id` is the file's set, the unit is `(model, row workload)` or else `(model, None)`, **and** `abs(unit mean − arm.quality) <= 1e-6`. Do not relax the last condition.
- One draw per unit per replicate, from its own random stream. The row draws of Task 2 must not move: `test_the_posterior_does_not_move_any_other_figure`.
- A published or tier-default quality is never given a spread.
- `ArmResult.quality_set_id` is defaulted and is the last field, so no existing constructor call changes. It reaches no output file; Task 1's golden test must still pass.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_uncertainty_quality.py`:

````python
"""P3c-3: quality-label uncertainty (spec §6, invariants U4, U8).

A calibrated quality's Beta posterior is read from the P2b-3
`recommendation.json` the set was applied from. The fixtures build that file
with the real estimator (`calibration.estimate`) and replay real snapshots,
so the set id, the mean and the resolver's order are the product's own.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.eval.calibration import (_beta_ppf, build_recommendation, dump_recommendation,
                                  estimate, recommendation_set_id)
from app.eval.simulate import simulate
from app.eval.uncertainty import (Posterior, UncertaintyError, UncertaintyPlan, draw_quality,
                                  load_posteriors, plan_block, plan_inputs, posterior_key,
                                  quality_coverage, results_block)
from tests.eval.sim_fixtures import BASE_SETTINGS, make_row, make_snapshot

P = "routeforge.policy-proposal/v1"


def _labelled(model: str, workload: str | None, good: int, bad: int, prior: float) -> list[dict]:
    """Outcome records the estimator accepts: `good` rows labelled
    `route_correct: 1` and `bad` labelled 0, all served by `model`."""
    scoring = {"candidates": [{"name": model, "quality": {"v": prior, "src": "tier_default"}}]}
    return [{"request_id": f"{model}-{workload}-{i}", "ts": "2026-09-20T00:00:00+00:00",
             "status": "ok", "endpoint_registry_version": "test", "workload": workload,
             "chosen": model, "cache_hit": False, "scoring": scoring,
             "feedback": [{"score_name": "route_correct", "value": 1 if i < good else 0,
                           "source": "admin", "ts": "2026-09-20T00:00:00+00:00"}]}
            for i in range(good + bad)]


def _recommendation(records: list[dict]) -> dict:
    return build_recommendation(estimate(records, max_ci_width=1.0), export_sha256="e" * 64)


def _write(tmp_path, rec: dict, name: str = "recommendation.json") -> str:
    path = tmp_path / name
    path.write_text(dump_recommendation(rec), encoding="utf-8")
    return str(path)


def _cheap_rec() -> dict:
    """`local-cheap`, model-wide: prior 0.5 (k = 10), 36 good and 4 bad
    labels -> Beta(41, 9), mean 0.82."""
    return _recommendation(_labelled("local-cheap", None, 36, 4, 0.5))


def _unit(rec: dict, model: str, workload: str | None) -> dict:
    return next(u for u in rec["units"] if u["model"] == model and u["workload"] == workload)


def _snap(rec: dict, *entries: tuple[str, str | None]) -> dict:
    """A snapshot whose `quality.calibrated` holds `rec`'s unit for each
    `(model, workload)` -- what `GET /admin/config-snapshot` exports after
    the set is applied."""
    calibrated = [{"model": m, "workload": w, "score": _unit(rec, m, w)["mean"],
                   "set_id": rec["set_id"]} for m, w in entries]
    return make_snapshot(quality={"customer": [], "calibrated": calibrated})


def _rows(n: int = 60, **over) -> list[dict]:
    return [make_row(f"r{i:03d}", "app-a", prompt_tokens=200 + 11 * i,
                     ts=f"2026-09-2{i % 3}T12:{i % 60:02d}:00+00:00", **over)
            for i in range(n)]


def _replay(rows, snap, proposal=None):
    results, _excluded = asyncio.run(
        simulate(rows, snap, proposal or {"schema": P}, base=BASE_SETTINGS))
    return results


def _plan(tmp_path, rec, **kw) -> UncertaintyPlan:
    posteriors, files = load_posteriors([_write(tmp_path, rec)])
    return UncertaintyPlan(posteriors=posteriors, posterior_files=files, **kw)


# --- loading --------------------------------------------------------------------

def test_a_real_recommendation_file_loads(tmp_path):
    rec = _cheap_rec()
    unit = _unit(rec, "local-cheap", None)
    assert (unit["status"], unit["alpha"], unit["beta"], unit["mean"]) == (
        "recommended", 41.0, 9.0, 0.82)
    path = _write(tmp_path, rec)
    posteriors, files = load_posteriors([path, path])          # the same file twice: once
    assert posteriors == {(rec["set_id"], "local-cheap", None): Posterior(41.0, 9.0, 0.82)}
    assert len(files) == 1 and files[0][0] == rec["set_id"] and len(files[0][1]) == 64
    assert load_posteriors([]) == ({}, ())


def test_units_that_were_not_recommended_are_skipped(tmp_path):
    """Three labels: `insufficient`. It was never applied, so it is no one's
    posterior."""
    rec = _recommendation(_labelled("local-cheap", None, 36, 4, 0.5)
                          + _labelled("local-mid", None, 2, 1, 0.75))
    assert _unit(rec, "local-mid", None)["status"] == "insufficient"
    posteriors, _files = load_posteriors([_write(tmp_path, rec)])
    assert set(posteriors) == {(rec["set_id"], "local-cheap", None)}


def _resealed(rec: dict, mutate) -> dict:
    """`rec` with `mutate` applied and its `set_id` recomputed, so the file
    passes the content-hash check and reaches the unit validation."""
    out = json.loads(json.dumps(rec))
    mutate(out)
    out["set_id"] = recommendation_set_id(out)
    return out


@pytest.mark.parametrize("mutate,where", [
    (lambda r: r.update(schema="routeforge.quality-recommendation/v2"), "schema"),
    (lambda r: r.pop("schema"), "schema"),
    (lambda r: r.update(units={}), "units"),
    (lambda r: r["units"].__setitem__(0, "x"), "units[0]"),
    (lambda r: r["units"][0].update(model=""), "units[0].model"),
    (lambda r: r["units"][0].update(workload=7), "units[0].workload"),
    (lambda r: r["units"][0].update(alpha=0), "units[0].alpha"),
    (lambda r: r["units"][0].update(beta=None), "units[0].beta"),
    (lambda r: r["units"][0].update(alpha=True), "units[0].alpha"),
    (lambda r: r["units"][0].update(mean=1.5), "units[0].mean"),
])
def test_a_malformed_file_is_refused_naming_the_location(tmp_path, mutate, where):
    path = _write(tmp_path, _resealed(_cheap_rec(), mutate))
    with pytest.raises(UncertaintyError) as err:
        load_posteriors([path])
    assert where in str(err.value) and path in str(err.value)


def test_an_edited_file_is_refused(tmp_path):
    """The `set_id` is a hash of the content. A file edited after it was
    written (here: a narrower posterior) no longer matches its own id."""
    rec = _cheap_rec()
    rec["units"][0]["alpha"] = 4100.0
    with pytest.raises(UncertaintyError, match="set_id"):
        load_posteriors([_write(tmp_path, rec)])


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_a_file_that_is_not_a_json_object_is_refused(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text)
    with pytest.raises(UncertaintyError):
        load_posteriors([str(path)])


# --- which posterior belongs to an arm ----------------------------------------------

def test_the_arm_carries_the_calibration_set_id(tmp_path):
    rec = _cheap_rec()
    results = _replay(_rows(6), _snap(rec, ("local-cheap", None)))
    for r in results:
        assert (r.current.model, r.current.quality_source) == ("local-cheap", "calibrated")
        assert r.current.quality == 0.82 and r.current.quality_set_id == rec["set_id"]
    plain = _replay(_rows(3), make_snapshot())
    assert all(r.current.quality_set_id is None and r.current.quality_source == "tier_default"
               for r in plain)


def test_posterior_key_follows_the_resolver_order(tmp_path):
    """A workload-scoped unit wins for its workload; every other row of the
    model uses the `(model, *)` unit -- `QualityResolver.resolve`'s order."""
    rec = _recommendation(_labelled("local-cheap", "claims", 30, 10, 0.5)
                          + _labelled("local-cheap", None, 5, 0, 0.5))
    wide = _unit(rec, "local-cheap", None)
    assert _unit(rec, "local-cheap", "claims")["status"] == wide["status"] == "recommended"
    snap = _snap(rec, ("local-cheap", "claims"), ("local-cheap", None))
    posteriors, _files = load_posteriors([_write(tmp_path, rec)])
    claims, chat = _replay([make_row("c", "app-a", workload="claims"),
                            make_row("d", "app-a", workload="chat")], snap)
    sid = rec["set_id"]
    assert posterior_key(claims.current, claims.row, posteriors) == (sid, "local-cheap", "claims")
    assert posterior_key(chat.current, chat.row, posteriors) == (sid, "local-cheap", None)
    assert posterior_key(chat.current, chat.row, {}) is None


def test_a_file_never_attaches_to_a_score_it_did_not_produce(tmp_path):
    rec = _cheap_rec()
    posteriors, _files = load_posteriors([_write(tmp_path, rec)])
    snap = _snap(rec, ("local-cheap", None))
    # The snapshot's score for that set was edited by hand: not this file's mean.
    snap["quality"]["calibrated"][0]["score"] = 0.6
    (edited,) = _replay([make_row("r1", "app-a")], snap)
    assert edited.current.quality == 0.6 and edited.current.quality_set_id == rec["set_id"]
    assert posterior_key(edited.current, edited.row, posteriors) is None
    # A different set's score for the same model.
    snap = _snap(rec, ("local-cheap", None))
    snap["quality"]["calibrated"][0]["set_id"] = "0" * 16
    (other,) = _replay([make_row("r1", "app-a")], snap)
    assert posterior_key(other.current, other.row, posteriors) is None
    # A quality that is not calibrated at all.
    (plain,) = _replay([make_row("r1", "app-a")], make_snapshot())
    assert posterior_key(plain.current, plain.row, posteriors) is None


# --- U8: the quality range carries label uncertainty, and only where there is one ----

def test_u8_one_calibrated_unit_gives_the_posteriors_own_interval(tmp_path):   # U8
    """Every row is served by one calibrated unit, so resampling rows cannot
    move the quality mean at all: the whole range is the posterior's. It
    must be the Beta(41, 9) 5% and 95% quantiles."""
    rec = _cheap_rec()
    results = _replay(_rows(60), _snap(rec, ("local-cheap", None)))
    block = results_block(results, _plan(tmp_path, rec, replicates=4000))
    quality = block["totals"]["quality_mean"]["current"]
    assert quality["point"] == 0.82
    assert quality["lo"] == pytest.approx(_beta_ppf(0.05, 41.0, 9.0), abs=0.01)
    assert quality["hi"] == pytest.approx(_beta_ppf(0.95, 41.0, 9.0), abs=0.01)
    assert block["quality_posterior"]["current"] == {
        "routed": 60, "calibrated": 60, "posterior": 60}


def test_u8_without_the_file_a_calibrated_quality_stays_a_point(tmp_path):     # U8
    """No `--quality-posteriors` file: the score is used as a point, the
    range has no width, and the block says the rows are calibrated but
    carry no posterior. No uncertainty is invented."""
    rec = _cheap_rec()
    results = _replay(_rows(60), _snap(rec, ("local-cheap", None)))
    block = results_block(results, UncertaintyPlan(replicates=200))
    assert block["totals"]["quality_mean"]["current"] == {"point": 0.82, "lo": 0.82, "hi": 0.82}
    assert block["quality_posterior"]["current"] == {
        "routed": 60, "calibrated": 60, "posterior": 0}


def test_u8_a_tier_default_quality_has_no_label_uncertainty(tmp_path):         # U8
    rec = _cheap_rec()
    results = _replay(_rows(60), make_snapshot())
    block = results_block(results, _plan(tmp_path, rec, replicates=200))
    quality = block["totals"]["quality_mean"]["current"]
    assert quality["lo"] == quality["hi"] == quality["point"] == 0.5
    assert block["quality_posterior"] == {
        "current": {"routed": 60, "calibrated": 0, "posterior": 0},
        "proposed": {"routed": 60, "calibrated": 0, "posterior": 0}}


def test_u8_coverage_counts_a_mixed_export(tmp_path):                         # U8
    """Half the rows route to the calibrated cheap model, half (high
    complexity) to a model with a tier-default quality."""
    rec = _cheap_rec()
    rows = _rows(30) + [make_row(f"big{i:02d}", "app-a", complexity=1.0, prompt_tokens=3000)
                        for i in range(30)]
    results = _replay(rows, _snap(rec, ("local-cheap", None)))
    cheap = sum(1 for r in results if r.current.model == "local-cheap")
    assert 0 < cheap < 60
    plan = _plan(tmp_path, rec, replicates=300)
    assert quality_coverage(results, plan)["current"] == {
        "routed": 60, "calibrated": cheap, "posterior": cheap}
    with_labels = results_block(results, plan)["totals"]["quality_mean"]["current"]
    rows_only = results_block(results, UncertaintyPlan(replicates=300))[
        "totals"]["quality_mean"]["current"]
    # Label uncertainty adds to row sampling; it never narrows the range.
    assert with_labels["hi"] - with_labels["lo"] > rows_only["hi"] - rows_only["lo"] > 0


def test_u4_one_unit_in_both_arms_cancels_in_the_delta(tmp_path):              # U4
    """A no-op proposal: both arms use the same unit and so the same draw.
    The quality delta is exactly 0 in every replicate although each arm's
    quality has a wide range."""
    rec = _cheap_rec()
    results = _replay(_rows(60), _snap(rec, ("local-cheap", None)))
    totals = results_block(results, _plan(tmp_path, rec, replicates=300))["totals"]
    assert totals["quality_mean"]["delta"] == {"point": 0.0, "lo": 0.0, "hi": 0.0}
    assert totals["quality_mean"]["current"] == totals["quality_mean"]["proposed"]
    assert totals["quality_mean"]["current"]["hi"] - totals["quality_mean"]["current"]["lo"] > 0.1


def test_the_posterior_does_not_move_any_other_figure(tmp_path):
    """Quality draws use their own random streams: cost, latency and count
    ranges are the same with and without the file."""
    rec = _cheap_rec()
    snap = _snap(rec, ("local-cheap", None))
    rows = _rows(30) + [make_row(f"big{i:02d}", "app-a", complexity=1.0, prompt_tokens=3000)
                        for i in range(30)]
    results = _replay(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})
    a = results_block(results, _plan(tmp_path, rec, replicates=100))["totals"]
    b = results_block(results, UncertaintyPlan(replicates=100))["totals"]
    for key in a:
        if key != "quality_mean":
            assert a[key] == b[key], key


def test_quality_draws_are_a_function_of_seed_replicate_and_unit():
    post = Posterior(41.0, 9.0, 0.82)
    key = ("abc", "local-cheap", None)
    assert draw_quality(0, 3, key, post) == draw_quality(0, 3, key, post)
    assert draw_quality(0, 3, key, post) != draw_quality(0, 4, key, post)
    assert draw_quality(0, 3, key, post) != draw_quality(1, 3, key, post)
    assert draw_quality(0, 3, key, post) != draw_quality(0, 3, ("abc", "local-cheap", "x"), post)
    assert all(0.0 < draw_quality(0, b, key, post) < 1.0 for b in range(50))


def test_plan_block_and_inputs_name_the_files(tmp_path):
    rec = _cheap_rec()
    plan = _plan(tmp_path, rec)
    (set_id, sha), = plan.posterior_files
    assert plan_inputs(plan)["quality_posteriors_sha256"] == [sha]
    assert plan_block(plan)["quality_posterior_sets"] == [set_id] == [rec["set_id"]]
    assert plan_inputs(UncertaintyPlan())["quality_posteriors_sha256"] == []
    assert plan_block(UncertaintyPlan())["quality_posterior_sets"] == []
````

Patch `tests/eval/test_uncertainty.py`:

````diff
diff --git a/tests/eval/test_uncertainty.py b/tests/eval/test_uncertainty.py
index a674db7..30ed54b 100644
--- a/tests/eval/test_uncertainty.py
+++ b/tests/eval/test_uncertainty.py
@@ -112,7 +112,7 @@ def test_engine_figures_equal_bucket_stats(build):
     Drawing each row exactly once must give `_bucket_stats`' own numbers,
     for the totals and for each app."""
     results = build()
-    strata, classes = _strata(results)
+    strata, classes = _strata(results, {})
     expected = _flatten(_bucket_stats(results), len(results), 2.0)
     got = _identity_figures(strata, classes, 2.0)
     # This one figure divides a delta ROUNDED to 6 dp (it mirrors the
@@ -129,7 +129,7 @@ def test_engine_figures_equal_bucket_stats(build):
 
 def test_equal_cost_columns_are_stored_once():
     """An app the proposal does not touch has one distinct cost column."""
-    strata, _classes = _strata(_synthetic())
+    strata, _classes = _strata(_synthetic(), {})
     untouched = next(s for s in strata if s.app == "app-0")
     assert len(untouched.cols) == 1 and untouched.col_of == (0, 0, 0, 0)
     touched = next(s for s in strata if s.app == "app-1")
@@ -333,8 +333,10 @@ def test_u7_too_few_rows_in_all_draws_nothing():                           # U7
 
 def test_u7_no_rows_at_all():                                              # U7
     block = results_block([], UncertaintyPlan())
+    empty = {"routed": 0, "calibrated": 0, "posterior": 0}
     assert block == {"totals": {"insufficient_data": True, "n": 0}, "by_app": {},
-                     "per_day": None, "points_outside_interval": 0}
+                     "per_day": None, "points_outside_interval": 0,
+                     "quality_posterior": {"current": empty, "proposed": empty}}
 
 
 def test_min_rows_is_inclusive():
@@ -443,10 +445,12 @@ def test_plan_refuses_values_out_of_range(kwargs):
 
 def test_plan_block_and_inputs():
     plan = UncertaintyPlan(replicates=250, interval=0.8, seed=4, min_rows=10)
-    assert plan_inputs(plan) == {"replicates": 250, "interval": 0.8, "seed": 4, "min_rows": 10}
+    assert plan_inputs(plan) == {"replicates": 250, "interval": 0.8, "seed": 4, "min_rows": 10,
+                                 "quality_posteriors_sha256": []}
     assert plan_block(plan) == {
         "method": METHOD, "strata": "application_id", "replicates": 250, "interval": 0.8,
-        "seed": 4, "min_rows": 10, "not_covered": list(NOT_COVERED)}
+        "seed": 4, "min_rows": 10, "quality_posterior_sets": [],
+        "not_covered": list(NOT_COVERED)}
     assert UncertaintyPlan() == UncertaintyPlan(replicates=1000, interval=0.90, seed=0,
                                                 min_rows=30)
 
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_quality.py tests/eval/test_uncertainty.py -p no:warnings -rf
```

Expected: a collection error in `test_uncertainty_quality.py`, `ImportError: cannot import name 'Posterior' from 'app.eval.uncertainty'`. Pytest stops there. (Run `tests/eval/test_uncertainty.py` alone to see the patched tests fail too: `_strata()` takes one argument, and two expected dicts have new keys.)

- [ ] **Step 3: Carry the set id on the arm**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 79c1f8c..5a20e02 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -90,6 +90,10 @@ class ArmResult:
     # that overflowed their declared context window, then (when routed) the
     # one served. `attempted[0]` is the first routing decision.
     attempted: tuple[str, ...] = ()
+    # P3c-3: the calibration set a `calibrated` quality came from
+    # (`QualityScore.set_id`), None for every other source. It lets
+    # `app/eval/uncertainty.py` find that score's posterior.
+    quality_set_id: str | None = None
 
     @property
     def overflow_hops(self) -> int:
@@ -446,7 +450,8 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
     return ArmResult("routed", pick.name, pick.provider, pick.tier, eligible, (),
                      cost, latency, latency_source, q.score, q.source,
                      complexity_estimated=estimated,
-                     attempted=(*overflowed, pick.name))
+                     attempted=(*overflowed, pick.name),
+                     quality_set_id=q.set_id)
 
 
 def arm_results(records: list[dict], scn: Scenario, *,
````

- [ ] **Step 4: Load posteriors, match them, draw from them**

Patch `app/eval/uncertainty.py`:

````diff
diff --git a/app/eval/uncertainty.py b/app/eval/uncertainty.py
index 3de1129..49790b7 100644
--- a/app/eval/uncertainty.py
+++ b/app/eval/uncertainty.py
@@ -17,6 +17,14 @@ over the simulated rows.
 - **Deterministic.** Same rows, plan and Python minor version give the same
   numbers. Nothing here reads a clock or the global `random` state.
 
+**Quality labels.** Where a routed row's quality is a P2b-3 calibrated
+score AND the operator passed the recommendation file that set was applied
+from (`--quality-posteriors`), each replicate also draws that
+`(model, workload)` unit's quality from its Beta posterior -- one draw per
+unit per replicate, shared by both arms and every scenario. A published or
+tier-default quality has no stated uncertainty and none is invented: it
+stays a point, and the block says how many rows that is.
+
 The range is sampling variability of the recorded rows under this replay
 model, and nothing else -- `UNCERTAINTY_CAVEAT` and `NOT_COVERED` say what
 it leaves out, and every report prints the caveat.
@@ -28,13 +36,16 @@ Never imported by serving code (`tests/eval/test_isolation.py`).
 """
 from __future__ import annotations
 
+import hashlib
+import json
 import math
 import random
 from collections import Counter
-from dataclasses import dataclass
+from dataclasses import dataclass, field
 
+from app.eval.calibration import recommendation_set_id
 from app.eval.outcomes import _round6
-from app.eval.simulate import RowResult, _bucket_stats, _parse_ts, _route_changed
+from app.eval.simulate import ArmResult, RowResult, _bucket_stats, _parse_ts, _route_changed
 
 METHOD = "stratified_paired_row_bootstrap"
 STRATA = "application_id"
@@ -45,6 +56,33 @@ NOT_COVERED = ("replay_model_error", "admission_estimate_error", "unrecorded_inp
 
 MAX_REPLICATES = 20000
 _COUNTS = ("route_changed", "newly_rejected", "newly_served")
+ARMS = ("current", "proposed")
+
+_RECOMMENDATION_PREFIX = "routeforge.quality-recommendation/"
+_RECOMMENDATION_MAJOR = "v1"
+
+#: `(set_id, model, workload)`; workload None is the `(model, *)` unit.
+PosteriorKey = tuple[str, str, str | None]
+
+
+class UncertaintyError(ValueError):
+    """A `--quality-posteriors` file failed to load or validate. Names the
+    file and the offending location, like `SnapshotError`."""
+
+    def __init__(self, where: str, message: str) -> None:
+        self.where = where
+        self.message = message
+        super().__init__(f"{where}: {message}")
+
+
+@dataclass(frozen=True)
+class Posterior:
+    """One calibrated unit's `Beta(alpha, beta)`, and the mean that was
+    applied as its score."""
+
+    alpha: float
+    beta: float
+    mean: float
 
 
 @dataclass(frozen=True)
@@ -55,6 +93,10 @@ class UncertaintyPlan:
     interval: float = 0.90
     seed: int = 0
     min_rows: int = 30
+    # `load_posteriors`' two results: the units, and one `(set_id, sha256)`
+    # per file. Empty when no `--quality-posteriors` file was given.
+    posteriors: dict[PosteriorKey, Posterior] = field(default_factory=dict)
+    posterior_files: tuple[tuple[str, str], ...] = ()
 
     def __post_init__(self) -> None:
         if (not isinstance(self.replicates, int) or isinstance(self.replicates, bool)
@@ -101,16 +143,102 @@ def window_days(window: dict | None) -> float | None:
     return days if days >= 1 else None
 
 
-def _arm_sig(arm) -> tuple:
+def _finite(value) -> bool:
+    return (isinstance(value, (int, float)) and not isinstance(value, bool)
+            and math.isfinite(value))
+
+
+def load_posteriors(paths: list[str]
+                    ) -> tuple[dict[PosteriorKey, Posterior], tuple[tuple[str, str], ...]]:
+    """Read `routeforge.quality-recommendation/v1` files (P2b-3's
+    `recommendation.json`). Returns the `recommended` units' posteriors keyed
+    `(set_id, model, workload)`, and one `(set_id, sha256 of the file)` per
+    distinct file, sorted.
+
+    A file's `set_id` must equal the hash of its own content
+    (`calibration.recommendation_set_id`): that is the same check the admin
+    API ran when the set was applied, so a file that passes it and whose
+    `set_id` is in the snapshot IS the set the snapshot's score came from.
+    Units that were not `recommended` were never applied and are skipped.
+    Raises `UncertaintyError` naming the file and the location."""
+    posteriors: dict[PosteriorKey, Posterior] = {}
+    files: set[tuple[str, str]] = set()
+    for path in paths:
+        with open(path, "rb") as f:
+            raw = f.read()
+        try:
+            rec = json.loads(raw)
+        except json.JSONDecodeError as exc:
+            raise UncertaintyError(path, f"invalid JSON: {exc}") from exc
+        if not isinstance(rec, dict):
+            raise UncertaintyError(path, "must be an object")
+        schema = rec.get("schema")
+        if (not isinstance(schema, str) or not schema.startswith(_RECOMMENDATION_PREFIX)
+                or schema[len(_RECOMMENDATION_PREFIX):].split(".", 1)[0]
+                != _RECOMMENDATION_MAJOR):
+            raise UncertaintyError(
+                f"{path}: schema",
+                f"must be {_RECOMMENDATION_PREFIX}{_RECOMMENDATION_MAJOR}; got {schema!r}")
+        set_id = rec.get("set_id")
+        if not isinstance(set_id, str) or set_id != recommendation_set_id(rec):
+            raise UncertaintyError(f"{path}: set_id",
+                                   "does not match the file's own content")
+        units = rec.get("units")
+        if not isinstance(units, list):
+            raise UncertaintyError(f"{path}: units", "must be a list")
+        for i, unit in enumerate(units):
+            where = f"{path}: units[{i}]"
+            if not isinstance(unit, dict):
+                raise UncertaintyError(where, "must be an object")
+            if unit.get("status") != "recommended":
+                continue
+            model, workload = unit.get("model"), unit.get("workload")
+            if not isinstance(model, str) or not model:
+                raise UncertaintyError(f"{where}.model", "must be a non-empty string")
+            if workload is not None and not isinstance(workload, str):
+                raise UncertaintyError(f"{where}.workload", "must be null or a string")
+            for key in ("alpha", "beta"):
+                if not _finite(unit.get(key)) or unit[key] <= 0:
+                    raise UncertaintyError(f"{where}.{key}", "must be a finite number > 0")
+            if not _finite(unit.get("mean")) or not 0.0 <= unit["mean"] <= 1.0:
+                raise UncertaintyError(f"{where}.mean", "must be a finite number in [0, 1]")
+            posteriors[(set_id, model, workload)] = Posterior(
+                float(unit["alpha"]), float(unit["beta"]), float(unit["mean"]))
+        files.add((set_id, hashlib.sha256(raw).hexdigest()))
+    return posteriors, tuple(sorted(files))
+
+
+def posterior_key(arm: ArmResult, row: dict,
+                  posteriors: dict[PosteriorKey, Posterior]) -> PosteriorKey | None:
+    """The posterior behind this arm's quality, or None when it has none: the
+    arm is not routed, its quality is not `calibrated`, or no loaded unit of
+    that set matches. The resolver's own order is followed -- the row's
+    workload, then `(model, *)` -- and the unit's mean must be the score the
+    arm carries, so a file can never attach to a score it did not produce."""
+    if (arm.outcome != "routed" or arm.quality_source != "calibrated"
+            or arm.quality_set_id is None or arm.quality is None):
+        return None
+    for workload in (row.get("workload"), None):
+        key = (arm.quality_set_id, arm.model, workload)
+        post = posteriors.get(key)
+        if post is not None and abs(post.mean - arm.quality) <= 1e-6:
+            return key
+    return None
+
+
+def _arm_sig(arm: ArmResult, row: dict, posteriors: dict[PosteriorKey, Posterior]) -> tuple:
     """Everything a figure reads from one arm of one row EXCEPT its cost:
-    `(routed, on-prem, latency_ms, quality)`. Latency and quality are
-    properties of the picked model, so rows share a small number of these."""
+    `(routed, on-prem, latency_ms, quality, posterior key)`. Latency and
+    quality are properties of the picked model (and workload), so rows share
+    a small number of these."""
     if arm.outcome != "routed":
-        return (False, False, None, None)
-    return (True, arm.provider == "onprem", arm.latency_ms, arm.quality)
+        return (False, False, None, None, None)
+    return (True, arm.provider == "onprem", arm.latency_ms, arm.quality,
+            posterior_key(arm, row, posteriors))
 
 
-def _strata(results: list[RowResult]) -> tuple[list[_Stratum], list[tuple]]:
+def _strata(results: list[RowResult], posteriors: dict[PosteriorKey, Posterior]
+            ) -> tuple[list[_Stratum], list[tuple]]:
     """One stratum per application (sorted by id), plus the class table.
 
     A row's CLASS is `(current signature, proposed signature, route
@@ -128,7 +256,8 @@ def _strata(results: list[RowResult]) -> tuple[list[_Stratum], list[tuple]]:
         cls: list[int] = []
         columns: tuple[list[float], ...] = ([], [], [], [])
         for r in by_app[app]:
-            key = (_arm_sig(r.current), _arm_sig(r.proposed), _route_changed(r))
+            key = (_arm_sig(r.current, r.row, posteriors),
+                   _arm_sig(r.proposed, r.row, posteriors), _route_changed(r))
             cls.append(classes.setdefault(key, len(classes)))
             current = (r.current.cost_usd or 0.0) if r.current.outcome == "routed" else 0.0
             proposed = (r.proposed.cost_usd or 0.0) if r.proposed.outcome == "routed" else 0.0
@@ -173,10 +302,13 @@ def _put(out: dict, name: str, current: float | None, proposed: float | None) ->
 
 
 def _figures(n: int, counts: dict[int, int], costs: list[float], classes: list[tuple],
-             days: float | None) -> dict[str, float | None]:
+             days: float | None, qdraw: dict[PosteriorKey, float] | None = None
+             ) -> dict[str, float | None]:
     """Every figure of one bucket, from its class counts and four cost sums.
     The same definitions as `simulate._bucket_stats`, stated on counts;
-    `tests/eval/test_uncertainty.py` pins the two against each other."""
+    `tests/eval/test_uncertainty.py` pins the two against each other.
+    `qdraw` is this replicate's quality draw per posterior key; without it
+    (the point estimate) every quality is the score the arm carries."""
     tally = dict.fromkeys(_COUNTS, 0)
     routed, onprem, q_n = [0, 0], [0, 0], [0, 0]
     q_sum = [0.0, 0.0]
@@ -189,7 +321,7 @@ def _figures(n: int, counts: dict[int, int], costs: list[float], classes: list[t
             tally["newly_rejected"] += m
         if proposed[0] and not current[0]:
             tally["newly_served"] += m
-        for a, (is_routed, is_onprem, lat, quality) in enumerate((current, proposed)):
+        for a, (is_routed, is_onprem, lat, quality, post) in enumerate((current, proposed)):
             if not is_routed:
                 continue
             routed[a] += m
@@ -198,7 +330,7 @@ def _figures(n: int, counts: dict[int, int], costs: list[float], classes: list[t
             if lat is not None:
                 latency[a][lat] = latency[a].get(lat, 0) + m
             if quality is not None:
-                q_sum[a] += m * quality
+                q_sum[a] += m * (quality if post is None or qdraw is None else qdraw[post])
                 q_n[a] += m
 
     out: dict[str, float | None] = {}
@@ -229,6 +361,15 @@ def draw_indices(seed: int, replicate: int, app: str, n: int) -> list[int]:
     return random.Random(f"{seed}:{replicate}:app:{app}").choices(range(n), k=n)
 
 
+def draw_quality(seed: int, replicate: int, key: PosteriorKey, post: Posterior) -> float:
+    """Replicate `replicate`'s quality for one calibrated unit: one draw from
+    its `Beta(alpha, beta)`, a function of the arguments alone, so both arms
+    and every scenario use the same value in the same replicate."""
+    set_id, model, workload = key
+    rng = random.Random(f"{seed}:{replicate}:quality:{set_id}:{model}:{workload or '*'}")
+    return rng.betavariate(post.alpha, post.beta)
+
+
 def _record(samples: dict[str, list], figures: dict[str, float | None]) -> None:
     for name, value in figures.items():
         samples.setdefault(name, []).append(value)
@@ -241,14 +382,17 @@ def run_bootstrap(results: list[RowResult], plan: UncertaintyPlan, *,
     the totals. A bucket with fewer than `plan.min_rows` rows gets no
     samples; its rows are still drawn, because they are part of the totals.
     With fewer than `min_rows` rows in all, nothing is drawn."""
-    strata, classes = _strata(results)
+    strata, classes = _strata(results, plan.posteriors)
     n_total = len(results)
+    used = sorted({sig[4] for cls in classes for sig in cls[:2] if sig[4] is not None},
+                  key=lambda k: (k[0], k[1], k[2] or ""))
     if n_total < plan.min_rows:
         return Bootstrap(n_total, None, {s.app: (s.n, None) for s in strata})
     totals: dict[str, list] = {}
     by_app: dict[str, tuple[int, dict[str, list] | None]] = {
         s.app: (s.n, {} if s.n >= plan.min_rows else None) for s in strata}
     for b in range(plan.replicates):
+        qdraw = {key: draw_quality(plan.seed, b, key, plan.posteriors[key]) for key in used}
         total_counts: Counter = Counter()
         total_costs = [0.0, 0.0, 0.0, 0.0]
         for s in strata:
@@ -258,11 +402,11 @@ def run_bootstrap(results: list[RowResult], plan: UncertaintyPlan, *,
             costs = [sums[i] for i in s.col_of]
             app_samples = by_app[s.app][1]
             if app_samples is not None:
-                _record(app_samples, _figures(s.n, counts, costs, classes, None))
+                _record(app_samples, _figures(s.n, counts, costs, classes, None, qdraw))
             total_counts.update(counts)
             for j, value in enumerate(costs):
                 total_costs[j] += value
-        _record(totals, _figures(n_total, total_counts, total_costs, classes, days))
+        _record(totals, _figures(n_total, total_counts, total_costs, classes, days, qdraw))
     return Bootstrap(n_total, totals, by_app)
 
 
@@ -366,12 +510,30 @@ def _count_outside(node) -> int:
     return 0
 
 
+def quality_coverage(results: list[RowResult], plan: UncertaintyPlan) -> dict:
+    """Per arm: `routed` rows that carry a quality, how many of those have a
+    `calibrated` quality, and how many of THOSE have a loaded posterior. The
+    quality range carries label uncertainty for `posterior` rows only; the
+    rest contribute a point."""
+    out = {}
+    for name in ARMS:
+        arms = [(getattr(r, name), r.row) for r in results]
+        routed = [(a, row) for a, row in arms if a.outcome == "routed" and a.quality is not None]
+        out[name] = {
+            "routed": len(routed),
+            "calibrated": sum(1 for a, _row in routed if a.quality_source == "calibrated"),
+            "posterior": sum(1 for a, row in routed
+                             if posterior_key(a, row, plan.posteriors) is not None)}
+    return out
+
+
 def results_block(results: list[RowResult], plan: UncertaintyPlan, *,
                   days: float | None = None) -> dict:
     """The ranges for ONE proposal's rows: `totals`, `by_app` (sorted),
-    `per_day` (None without a window of at least a day) and
-    `points_outside_interval`. Points come from `simulate._bucket_stats`,
-    so a leaf's `point` is the figure the document already carries."""
+    `per_day` (None without a window of at least a day), `quality_posterior`
+    (`quality_coverage`) and `points_outside_interval`. Points come from
+    `simulate._bucket_stats`, so a leaf's `point` is the figure the document
+    already carries."""
     boot = run_bootstrap(results, plan, days=days)
     totals = _bucket(boot.n, _flatten(_bucket_stats(results), boot.n, days), boot.totals, plan)
     per_day = totals.pop("per_day", None)
@@ -382,17 +544,21 @@ def results_block(results: list[RowResult], plan: UncertaintyPlan, *,
               for app, (n, samples) in boot.by_app.items()}
     block = {"totals": totals, "by_app": by_app, "per_day": per_day}
     block["points_outside_interval"] = _count_outside(block)
+    block["quality_posterior"] = quality_coverage(results, plan)
     return block
 
 
 def plan_inputs(plan: UncertaintyPlan) -> dict:
     """What enters a document's `inputs` (and so its id) with `--uncertainty`."""
     return {"replicates": plan.replicates, "interval": plan.interval, "seed": plan.seed,
-            "min_rows": plan.min_rows}
+            "min_rows": plan.min_rows,
+            "quality_posteriors_sha256": [sha for _set_id, sha in plan.posterior_files]}
 
 
 def plan_block(plan: UncertaintyPlan) -> dict:
     """The head of every `uncertainty` block: how the ranges were made and
     what they leave out."""
-    return {"method": METHOD, "strata": STRATA, **plan_inputs(plan),
+    return {"method": METHOD, "strata": STRATA, "replicates": plan.replicates,
+            "interval": plan.interval, "seed": plan.seed, "min_rows": plan.min_rows,
+            "quality_posterior_sets": [set_id for set_id, _sha in plan.posterior_files],
             "not_covered": list(NOT_COVERED)}
````

- [ ] **Step 5: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_quality.py tests/eval/test_uncertainty.py tests/eval/test_uncertainty_golden.py -p no:warnings -rf
```

Expected: `73 passed`.

- [ ] **Step 6: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `937 passed`, no `FAILED` line.

- [ ] **Step 7: Commit**

```bash
git add app/eval/simulate.py app/eval/uncertainty.py tests/eval/test_uncertainty.py tests/eval/test_uncertainty_quality.py
git commit -m "$(cat <<'EOF'
feat(eval): quality-label uncertainty from the calibration file (P3c-3)

The snapshot carries a calibrated quality's mean and set id, not its
Beta parameters. They are read from the P2b-3 recommendation file, which
authenticates itself by its content hash. One draw per unit per
replicate, shared by both arms. A published or tier-default quality
stays a point, and the block reports how many rows have a posterior.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: The caveat and the report text (spec §8, §10.1; U7, U9, U11)

**Files:**
- Modify: `app/eval/uncertainty.py` (one import line; everything else is appended)
- Create: `tests/eval/test_uncertainty_report.py`

**Interfaces:**
- Consumes:
  - Task 3's `app/eval/uncertainty.py`: `plan_block(plan)`, `results_block(results, plan, *, days=None)` (keys `totals`, `by_app`, `per_day`, `points_outside_interval`, `quality_posterior`), `UncertaintyPlan`, `NOT_COVERED`, `ARMS`, `_COUNTS`.
  - `app/eval/outcomes.py::_fmt(value) -> str` (`None` → `"n/a"`, a float → `f"{value:.6g}"`).
  - From `tests/eval/test_uncertainty.py`: `BANNED`, `REJECTED`, `_arm`, `_result`, `_synthetic`.
- Produces (`app/eval/uncertainty.py`):
  - `UNCERTAINTY_CAVEAT: str` — seven lines joined by `\n`, without a leading `"> "`.
  - `ZERO_COUNT_NOTE: str` — a `str.format` template with fields `n`, `level`, `bound`, `one_in`.
  - `level_text(level: float) -> str` (`0.9` → `"90%"`); `caveat_lines() -> list[str]` (each line with `"> "`).
  - `method_line(block: dict) -> str` — reads `interval`, `replicates`, `seed`, `min_rows`.
  - `quality_line(coverage: dict) -> str`; `insufficient_text(bucket: dict) -> str` (`"insufficient data (n = k)"`).
  - `range_text(node: dict) -> str` (`"lo to hi"`, …); `with_range(text: str, node: dict | None) -> str` (`"text [lo, hi]"`; `text` unchanged for `None`).
  - `zero_count_note(bucket: dict, level: float) -> str | None`; `bucket_lines(bucket: dict, level: float) -> list[str]`; `outside_line(count: int) -> str | None`.
  - `render_uncertainty_section(block: dict) -> str` — the whole `## Uncertainty ranges` section for a single-proposal report; `block` is `plan_block(...)` alone, or merged with a `results_block(...)`. Ends with one `\n`.

**Rules.**

- `UNCERTAINTY_CAVEAT` is spec §8's text, word for word and line for line. The test compares the whole string.
- No report sentence may contain `rank`, `recommend`, `best`, `winner`, `prefer` or `score`. That is why the quality line says "the file that set was applied from" and not the file's name.
- A bucket with `insufficient_data` prints `insufficient data (n = k)` and no table.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_uncertainty_report.py`:

````python
"""P3c-3: what a report says about a range (spec §8, §10; invariants U9, U11)."""
from __future__ import annotations

from app.eval.uncertainty import (NOT_COVERED, UNCERTAINTY_CAVEAT, ZERO_COUNT_NOTE,
                                  UncertaintyPlan, bucket_lines, caveat_lines,
                                  insufficient_text, level_text, method_line, outside_line,
                                  plan_block, quality_line, range_text,
                                  render_uncertainty_section, results_block, with_range,
                                  zero_count_note)
from tests.eval.test_uncertainty import BANNED, REJECTED, _arm, _result, _synthetic


def _block(results=None, *, days=3.0, **plan_kw) -> dict:
    plan = UncertaintyPlan(**{"replicates": 100, **plan_kw})
    return {**plan_block(plan),
            **results_block(_synthetic() if results is None else results, plan, days=days)}


def test_u9_caveat_is_the_spec_text():                                     # U9
    """The caveat is pinned word for word: it is the statement of what a
    range is. Editing it means editing spec §8 too."""
    assert UNCERTAINTY_CAVEAT == (
        "**Ranges show sampling variability only.** Each range is a percentile interval from\n"
        "resampling the recorded rows with replacement, within each application, under this replay\n"
        "model. It does not include replay model error (first-decision replay, frozen signals,\n"
        "predicted overflow), the error of the admission estimate for rows no backend answered,\n"
        "inputs that were not recorded, or future traffic shift. Replay gives each endpoint one\n"
        "latency, so a latency range reflects only which endpoints the resampled rows used. A narrow\n"
        "range on a biased replay is still biased.")
    assert all(line.startswith("> ") for line in caveat_lines())
    assert len(caveat_lines()) == 7


def test_u9_not_covered_names_every_excluded_source():                     # U9
    assert NOT_COVERED == ("replay_model_error", "admission_estimate_error",
                           "unrecorded_inputs", "traffic_shift", "per_request_latency")
    assert plan_block(UncertaintyPlan())["not_covered"] == list(NOT_COVERED)


def test_u9_section_opens_with_the_caveat_and_says_how_ranges_were_made():  # U9
    text = render_uncertainty_section(_block(replicates=100, seed=5))
    lines = text.split("\n")
    assert lines[0] == "## Uncertainty ranges" and lines[1] == ""
    assert lines[2:9] == caveat_lines()
    assert ("90% percentile intervals from 100 bootstrap resamples of the simulated rows, "
            "drawn with replacement within each application (seed 5).") in text
    assert "A bucket with fewer than 30 rows gets no range." in text
    assert text.endswith("\n") and not text.endswith("\n\n\n")


def test_section_structure_totals_per_day_and_apps():
    text = render_uncertainty_section(_block())
    order = [text.index(h) for h in ("### Totals (n = 240)", "### Per day", "### By app",
                                     "#### app-0 (n = 80)", "#### app-1 (n = 80)",
                                     "#### app-2 (n = 80)")]
    assert order == sorted(order)
    assert "| figure | point | 90% range |" in text
    assert "| cost_usd delta | " in text and "| newly_rejected_share | " in text
    assert "| cost_usd/day (both routed) delta | " in text
    assert "Quality uncertainty covers 0 of 240 routed rows in the current arm" in text
    assert "the rest use a point prior." in text


def test_no_per_day_table_without_a_window_of_a_day():
    assert "### Per day" not in render_uncertainty_section(_block(days=None))


def test_u7_report_prints_insufficient_data_instead_of_a_range():          # U7
    results = _synthetic() + [_result(5000 + i, "small", _arm("local-mid", 0.01), REJECTED)
                              for i in range(5)]
    text = render_uncertainty_section(_block(results))
    section = text[text.index("#### small (n = 5)"):]
    assert section.strip().split("\n")[-1] == "insufficient data (n = 5)"
    assert "| figure |" not in section
    assert insufficient_text({"insufficient_data": True, "n": 5}) == "insufficient data (n = 5)"
    # Too few rows in all: no table anywhere, and no per-day section.
    tiny = render_uncertainty_section(_block(_synthetic(12)))
    assert "### Totals (n = 12)\n\ninsufficient data (n = 12)\n" in tiny
    assert "| figure |" not in tiny and "### Per day" not in tiny


def test_zero_newly_rejected_is_never_presented_as_no_risk():
    block = _block()
    app0 = block["by_app"]["app-0"]
    note = zero_count_note(app0, 0.90)
    assert note == ZERO_COUNT_NOTE.format(n=80, level="90%", bound="2.84%", one_in="35")
    assert "That is not zero risk" in note and "independent draws" in note
    assert note in bucket_lines(app0, 0.90)
    assert zero_count_note(block["by_app"]["app-1"], 0.90) is None        # it has rejections
    assert zero_count_note({"insufficient_data": True, "n": 3}, 0.90) is None
    text = render_uncertainty_section(block)
    assert text.count("That is not zero risk") == 1


def test_range_text_and_with_range():
    assert range_text({"point": 1.5, "lo": 1.25, "hi": 2.0}) == "1.25 to 2"
    assert range_text({"point": 9.0, "lo": 1.0, "hi": 2.0, "point_outside": True}) == \
        "1 to 2 (point outside range)"
    assert range_text({"point": 800.0, "lo": None, "hi": None, "undefined_replicates": 7}) == \
        "n/a (undefined in 7 resamples)"
    assert with_range("-0.39", {"point": -0.39, "lo": -0.52, "hi": -0.21}) == \
        "-0.39 [-0.52, -0.21]"
    assert with_range("3", {"point": 3, "lo": 0, "hi": 6}) == "3 [0, 6]"
    assert with_range("n/a", {"point": None, "lo": None, "hi": None,
                              "undefined_replicates": 100}) == "n/a [n/a]"
    assert with_range("5", None) == "5"
    assert level_text(0.9) == "90%" and level_text(0.95) == "95%" and level_text(0.5) == "50%"


def test_point_outside_is_said_in_the_report():
    block = _block(replicates=1)
    assert block["points_outside_interval"] > 0
    text = render_uncertainty_section(block)
    assert outside_line(block["points_outside_interval"]) in text
    assert "(point outside range)" in text
    assert outside_line(0) is None
    assert "outside their own range" not in render_uncertainty_section(_block())


def test_quality_line_names_calibrated_rows_without_a_posterior():
    full = {"routed": 60, "calibrated": 60, "posterior": 60}
    assert quality_line({"current": full, "proposed": full}) == (
        "Quality uncertainty covers 60 of 60 routed rows in the current arm and 60 of 60 in "
        "the proposed arm; the rest use a point prior.")
    partial = {"routed": 60, "calibrated": 40, "posterior": 10}
    text = quality_line({"current": partial, "proposed": full})
    assert "covers 10 of 60 routed rows in the current arm" in text
    assert "Up to 30 row(s) per arm have a calibrated quality with no posterior loaded" in text
    assert "--quality-posteriors" in text


def test_a_block_with_no_figures_renders_the_caveat_and_method_only():
    """`--shadow-prices` without `--proposal`: there are no totals."""
    text = render_uncertainty_section(plan_block(UncertaintyPlan(replicates=50)))
    assert text.split("\n")[2:9] == caveat_lines()
    assert method_line(plan_block(UncertaintyPlan(replicates=50))) in text
    assert "### Totals" not in text and "Quality uncertainty" not in text


def test_u11_report_text_does_not_rank_or_recommend():                     # U11
    results = _synthetic() + [_result(5000 + i, "small", _arm("local-mid", 0.01), REJECTED)
                              for i in range(5)]
    partial = {"routed": 60, "calibrated": 40, "posterior": 10}
    text = (render_uncertainty_section(_block(results, replicates=1))
            + quality_line({"current": partial, "proposed": partial})).lower()
    assert not [word for word in BANNED if word in text]
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_report.py -p no:warnings -rf
```

Expected: a collection error, `ImportError: cannot import name 'UNCERTAINTY_CAVEAT' from 'app.eval.uncertainty'`.

- [ ] **Step 3: Add the report text**

Patch `app/eval/uncertainty.py`:

````diff
diff --git a/app/eval/uncertainty.py b/app/eval/uncertainty.py
index 49790b7..36df18c 100644
--- a/app/eval/uncertainty.py
+++ b/app/eval/uncertainty.py
@@ -44,7 +44,7 @@ from collections import Counter
 from dataclasses import dataclass, field
 
 from app.eval.calibration import recommendation_set_id
-from app.eval.outcomes import _round6
+from app.eval.outcomes import _fmt, _round6
 from app.eval.simulate import ArmResult, RowResult, _bucket_stats, _parse_ts, _route_changed
 
 METHOD = "stratified_paired_row_bootstrap"
@@ -562,3 +562,178 @@ def plan_block(plan: UncertaintyPlan) -> dict:
             "interval": plan.interval, "seed": plan.seed, "min_rows": plan.min_rows,
             "quality_posterior_sets": [set_id for set_id, _sha in plan.posterior_files],
             "not_covered": list(NOT_COVERED)}
+
+
+# --- report text (spec §10) ---------------------------------------------------
+
+#: Printed in every report that shows a range, verbatim (spec §8), one string
+#: per source line without the leading "> " -- the same convention as
+#: `simulate.REPORT_CAVEAT`. Pinned by tests/eval/test_uncertainty_report.py.
+UNCERTAINTY_CAVEAT = (
+    "**Ranges show sampling variability only.** Each range is a percentile interval from\n"
+    "resampling the recorded rows with replacement, within each application, under this replay\n"
+    "model. It does not include replay model error (first-decision replay, frozen signals,\n"
+    "predicted overflow), the error of the admission estimate for rows no backend answered,\n"
+    "inputs that were not recorded, or future traffic shift. Replay gives each endpoint one\n"
+    "latency, so a latency range reflects only which endpoints the resampled rows used. A narrow\n"
+    "range on a biased replay is still biased."
+)
+
+#: Why a zero count is not "no risk" (spec §7). `{}` fields: n, level, bound, one_in.
+ZERO_COUNT_NOTE = (
+    "newly_rejected is 0 in all {n} rows. That is not zero risk: at the {level} level these "
+    "rows are consistent with a newly-rejected rate of up to {bound} (about 1 request in "
+    "{one_in}), if the rows are independent draws from the same traffic. Correlated rows, "
+    "such as the calls of one agent loop, make the true bound larger."
+)
+
+# (label, path into a bucket) for the range table, in `_metrics_table`'s order.
+_FIGURE_ROWS = tuple(
+    [(f"cost_usd {k}", ("cost_usd", k)) for k in ("current", "proposed", "delta")]
+    + [(f"cost_usd (both routed) {k}", ("cost_usd_both_routed", k))
+       for k in ("current", "proposed", "delta")]
+    + [(name, (name,)) for count in _COUNTS for name in (count, f"{count}_share")]
+    + [(f"latency_ms {p} {k}", ("latency_ms", p, k))
+       for p in ("p50", "p95") for k in ("current", "proposed", "delta")]
+    + [(f"quality_mean {k}", ("quality_mean", k)) for k in ("current", "proposed", "delta")]
+    + [(f"onprem_share {k}", ("onprem_share", k)) for k in ARMS])
+
+_PER_DAY_ROWS = (
+    ("cost_usd/day current", ("cost_usd", "current")),
+    ("cost_usd/day proposed", ("cost_usd", "proposed")),
+    ("cost_usd/day delta", ("cost_usd", "delta")),
+    ("cost_usd/day (both routed) delta", ("cost_usd_both_routed", "delta")),
+)
+
+
+def level_text(level: float) -> str:
+    """`0.9` as `90%`."""
+    return f"{level * 100:g}%"
+
+
+def caveat_lines() -> list[str]:
+    return ["> " + line for line in UNCERTAINTY_CAVEAT.split("\n")]
+
+
+def method_line(block: dict) -> str:
+    """One sentence saying how the ranges were made, from the block's head."""
+    return (f"{level_text(block['interval'])} percentile intervals from {block['replicates']} "
+            "bootstrap resamples of the simulated rows, drawn with replacement within each "
+            f"application (seed {block['seed']}). Both arms, and every scenario or relaxation, "
+            f"use the same resampled rows. A bucket with fewer than {block['min_rows']} rows "
+            "gets no range.")
+
+
+def quality_line(coverage: dict) -> str:
+    """How much of the quality range is label uncertainty (spec §6)."""
+    cur, prop = coverage["current"], coverage["proposed"]
+    text = (f"Quality uncertainty covers {cur['posterior']} of {cur['routed']} routed rows in "
+            f"the current arm and {prop['posterior']} of {prop['routed']} in the proposed arm; "
+            "the rest use a point prior.")
+    missing = max(cur["calibrated"] - cur["posterior"], prop["calibrated"] - prop["posterior"])
+    if missing:
+        text += (f" Up to {missing} row(s) per arm have a calibrated quality with no posterior "
+                 "loaded: pass the file that set was applied from with --quality-posteriors.")
+    return text
+
+
+def insufficient_text(bucket: dict) -> str:
+    return f"insufficient data (n = {bucket['n']})"
+
+
+def range_text(node: dict) -> str:
+    """A leaf's range for a table cell: `lo to hi`, with what is unusual
+    about it said in words."""
+    if node["lo"] is None:
+        return f"n/a (undefined in {node.get('undefined_replicates', 0)} resamples)"
+    text = f"{_fmt(node['lo'])} to {_fmt(node['hi'])}"
+    if node.get("point_outside"):
+        text += " (point outside range)"
+    return text
+
+
+def with_range(text: str, node: dict | None) -> str:
+    """`text [lo, hi]` for a cell of a table that already shows the point;
+    `text` unchanged when there is no leaf for it."""
+    if node is None:
+        return text
+    if node["lo"] is None:
+        return f"{text} [n/a]"
+    return f"{text} [{_fmt(node['lo'])}, {_fmt(node['hi'])}]"
+
+
+def _dig(bucket: dict, path: tuple[str, ...]) -> dict:
+    node = bucket
+    for part in path:
+        node = node[part]
+    return node
+
+
+def _table(bucket: dict, rows: tuple, level: float) -> list[str]:
+    lines = [f"| figure | point | {level_text(level)} range |", "|---|---|---|"]
+    for label, path in rows:
+        node = _dig(bucket, path)
+        lines.append(f"| {label} | {_fmt(node['point'])} | {range_text(node)} |")
+    return lines
+
+
+def zero_count_note(bucket: dict, level: float) -> str | None:
+    """`ZERO_COUNT_NOTE` for a bucket whose `newly_rejected` is 0, else None."""
+    if bucket.get("insufficient_data"):
+        return None
+    bound = bucket["newly_rejected_share"].get("zero_bound")
+    if bound is None:
+        return None
+    return ZERO_COUNT_NOTE.format(n=bucket["n"], level=level_text(level),
+                                  bound=f"{bound * 100:.3g}%",
+                                  one_in=f"{max(1, round(1 / bound)):,}")
+
+
+def bucket_lines(bucket: dict, level: float) -> list[str]:
+    """One bucket for a report: its range table and, when `newly_rejected`
+    is 0, the zero-count note -- or the insufficient-data line alone."""
+    if bucket.get("insufficient_data"):
+        return [insufficient_text(bucket)]
+    lines = _table(bucket, _FIGURE_ROWS, level)
+    note = zero_count_note(bucket, level)
+    if note:
+        lines += ["", note]
+    return lines
+
+
+def outside_line(count: int) -> str | None:
+    if not count:
+        return None
+    return (f"{count} figure(s) have a point estimate outside their own range (marked). A "
+            "percentile interval does not guarantee otherwise; nothing was widened.")
+
+
+def render_uncertainty_section(block: dict) -> str:
+    """The "Uncertainty ranges" section of a single-proposal `report.md`,
+    from `simulation.json`'s `uncertainty` block alone: caveat, method, then
+    (when the block has them -- a shadow-prices-only run does not) the
+    quality coverage, the totals, the per-day figures and each app."""
+    lines: list[str] = ["## Uncertainty ranges", ""]
+    lines += caveat_lines()
+    lines += ["", method_line(block), ""]
+    if "totals" not in block:
+        return "\n".join(lines)
+    level = block["interval"]
+    lines += [quality_line(block["quality_posterior"]), ""]
+    outside = outside_line(block["points_outside_interval"])
+    if outside:
+        lines += [outside, ""]
+    lines += [f"### Totals (n = {block['totals']['n']})", ""]
+    lines += bucket_lines(block["totals"], level)
+    lines.append("")
+    if block["per_day"] is not None:
+        lines += ["### Per day", ""]
+        lines += _table(block["per_day"], _PER_DAY_ROWS, level)
+        lines.append("")
+    lines += ["### By app", ""]
+    for app_id in sorted(block["by_app"]):
+        bucket = block["by_app"][app_id]
+        lines += [f"#### {app_id} (n = {bucket['n']})", ""]
+        lines += bucket_lines(bucket, level)
+        lines.append("")
+    return "\n".join(lines)
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_report.py tests/eval/test_uncertainty.py tests/eval/test_uncertainty_quality.py -p no:warnings -rf
```

Expected: `80 passed`.

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `949 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add app/eval/uncertainty.py tests/eval/test_uncertainty_report.py
git commit -m "$(cat <<'EOF'
feat(eval): the uncertainty caveat and report section (P3c-3)

Every report that shows a range opens the section with a fixed caveat:
sampling variability only, and what that leaves out. Insufficient data
prints instead of a table; a zero count prints a bound, not "no risk".

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Ranges in the scenario comparison (spec §5.4, §9.4, §10.2; U1, U4, U7, U11, U13)

**Files:**
- Modify: `app/eval/uncertainty.py` (add `table_ranges`, before `plan_inputs`)
- Modify: `app/eval/scenarios.py`
- Create: `tests/eval/test_uncertainty_scenarios.py`

**Interfaces:**
- Consumes:
  - Task 4's `app/eval/uncertainty.py`: `UncertaintyPlan`, `results_block`, `plan_block`, `window_days`, `caveat_lines`, `method_line`, `quality_line`, `insufficient_text`, `outside_line`, `bucket_lines`, `with_range`.
  - `app/eval/scenarios.py` as it is: `async compare(records, snapshot, scenarios_doc, *, base, since=None) -> Comparison`; `Comparison(rows, baseline, window, scenarios)` (frozen dataclass); `comparison_doc(comparison, *, inputs) -> dict`; `_table_row(entry, window) -> dict` with keys `name`, `cost_per_day_delta_usd`, `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`, `overflow_hops`, `affected_apps`; `_comparison_row(row) -> str`; `render_comparison(doc) -> str`; `NO_RANKING_NOTE`. Inside `compare`'s loop, `results` (the scenario's `list[RowResult]`) and `sim` (its `summarize` output; `sim["inputs"]["window"]` is the window) are in scope.
  - From `tests/eval/test_scenarios_compare.py`: `_INPUTS`, `_TABLE_KEYS`, `_TRIAD` (three `(name, proposal body)` pairs), `_compare(records, snap, doc, **kw)`, `_doc(*scenarios, title=...)`, `_keys(value)`, `_snap()`.
- Produces:
  - `app/eval/uncertainty.py::table_ranges(block: dict) -> dict` — the ten leaves behind a comparison-table row, keyed `cost_per_day_delta_usd` (`None` without a per-day window), `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`; or `{"insufficient_data": True, "n"}`.
  - `compare(..., uncertainty: UncertaintyPlan | None = None)`; `Comparison.uncertainty: dict | None = None` — `{**plan_block(plan), "scenarios": [{"name", **results_block(...), "table": table_ranges(...)}, ...]}` in file order.
  - `comparison_doc` adds the top-level key `uncertainty` when `comparison.uncertainty` is not `None`, and nothing otherwise.
  - `RANGE_NOT_RANKING_NOTE: str`; `_comparison_row(row, ranges=None)`; `render_comparison` prints ranges when `doc` has `uncertainty`.

**Rules.**

- Without a plan, `compare`, `comparison_doc` and `render_comparison` return exactly what they return today. Task 1's `scenarios` golden must pass.
- Scenarios stay in file order everywhere. Do not sort by any range, and do not add a column or key that compares scenarios.
- Each scenario is bootstrapped inside the loop, from the `results` it was just replayed over, before the next scenario is built. Do not collect `results` across scenarios.
- The table's on-prem share cell (`current→proposed`) keeps its two numbers and gets no brackets.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_uncertainty_scenarios.py`:

````python
"""P3c-3: ranges in the scenario comparison (spec §9.2, §10.2; U1, U4, U11)."""
from __future__ import annotations

import json

from app.eval.scenarios import (NO_RANKING_NOTE, RANGE_NOT_RANKING_NOTE, comparison_doc,
                                render_comparison)
from app.eval.uncertainty import METHOD, UNCERTAINTY_CAVEAT, UncertaintyPlan
from tests.eval.sim_fixtures import make_row
from tests.eval.test_scenarios_compare import (_INPUTS, _TABLE_KEYS, _TRIAD, _compare, _doc,
                                               _keys, _snap)
from tests.eval.test_uncertainty import BANNED

PLAN = UncertaintyPlan(replicates=120)


def _rows(n: int = 90) -> list[dict]:
    """Two apps, three sizes, three days; every row a different prompt size
    so costs differ row by row."""
    return [make_row(f"r{i:03d}", "claims" if i % 2 else "chat",
                     complexity=[0.05, 0.6, 1.0][i % 3],
                     prompt_tokens=[200, 1200, 3000][i % 3] + 7 * i,
                     ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:{i % 50:02d}+00:00")
            for i in range(n)]


def _document(doc, *, rows=None, plan=PLAN):
    kw = {} if plan is None else {"uncertainty": plan}
    return comparison_doc(_compare(_rows() if rows is None else rows, _snap(), doc, **kw),
                          inputs=_INPUTS)


def test_u1_a_plan_adds_one_key_and_changes_no_other():                    # U1
    """With a plan the document is the plan-less document plus `uncertainty`.
    Every other key -- the table, each scenario's summary, the baseline, the
    id -- is the same object, value for value."""
    plain = _document(_doc(*_TRIAD), plan=None)
    ranged = _document(_doc(*_TRIAD))
    assert "uncertainty" not in plain
    assert set(ranged) == set(plain) | {"uncertainty"}
    without = {k: v for k, v in ranged.items() if k != "uncertainty"}
    assert json.dumps(without, sort_keys=True) == json.dumps(plain, sort_keys=True)
    for row in ranged["table"]:
        assert set(row) == _TABLE_KEYS


def test_block_head_and_scenarios_in_file_order():
    block = _document(_doc(*_TRIAD))["uncertainty"]
    assert (block["method"], block["replicates"], block["interval"], block["seed"],
            block["min_rows"]) == (METHOD, 120, 0.9, 0, 30)
    assert [entry["name"] for entry in block["scenarios"]] == [n for n, _ in _TRIAD]
    for entry in block["scenarios"]:
        assert set(entry) == {"name", "totals", "by_app", "per_day", "quality_posterior",
                              "points_outside_interval", "table"}
        assert set(entry["by_app"]) == {"chat", "claims"} and entry["totals"]["n"] == 90


def test_every_table_range_is_about_the_figure_in_that_column():
    """`uncertainty.scenarios[i].table[key].point` is `table[i][key]`."""
    doc = _document(_doc(*_TRIAD))
    for row, entry in zip(doc["table"], doc["uncertainty"]["scenarios"]):
        assert entry["name"] == row["name"]
        ranges = entry["table"]
        assert set(ranges) == _TABLE_KEYS - {"name", "overflow_hops", "affected_apps"}
        for key, node in ranges.items():
            assert node["point"] == row[key], (row["name"], key)


def test_u4_an_empty_scenario_has_every_range_at_zero():                   # U4
    doc = _document(_doc(("empty", {}), _TRIAD[0]))
    empty, changed = doc["uncertainty"]["scenarios"]
    for key in ("cost_per_day_delta_usd", "cost_delta_usd", "route_changed", "newly_rejected",
                "newly_served", "latency_p50_delta_ms", "latency_p95_delta_ms",
                "quality_mean_delta"):
        node = empty["table"][key]
        assert (node["point"], node["lo"], node["hi"]) == (0, 0, 0), key
    cost = changed["table"]["cost_delta_usd"]
    assert cost["lo"] < cost["hi"]


def test_u4_a_scenario_gets_the_same_ranges_alone_and_among_others():      # U4
    """Every scenario is resampled with the same draws, so a scenario's
    block does not depend on which other scenarios are in the file, or on
    their order."""
    name, body = _TRIAD[1]
    alone = _document(_doc((name, body)))["uncertainty"]["scenarios"][0]
    forward = _document(_doc(*_TRIAD))["uncertainty"]["scenarios"]
    backward = _document(_doc(*reversed(_TRIAD)))["uncertainty"]["scenarios"]
    assert alone == forward[1] == backward[1]
    assert {e["name"]: e for e in forward} == {e["name"]: e for e in backward}
    # The shared current arm has one range, whatever the scenario.
    currents = [e["totals"]["cost_usd"]["current"] for e in forward]
    assert currents[0] == currents[1] == currents[2]


def test_u11_ranges_add_no_ranking_key_and_keep_file_order():              # U11
    doc = _document(_doc(*_TRIAD))
    assert not [k for k in _keys(doc) if any(word in k.lower() for word in BANNED)]
    names = [entry["name"] for entry in doc["uncertainty"]["scenarios"]]
    assert names == [n for n, _ in _TRIAD] == [row["name"] for row in doc["table"]]
    lows = [e["table"]["cost_delta_usd"]["lo"] for e in doc["uncertainty"]["scenarios"]]
    assert lows != sorted(lows) and lows != sorted(lows, reverse=True)    # not in cost order


def test_report_puts_ranges_in_the_table_and_explains_them():
    doc = _document(_doc(*_TRIAD))
    report = render_comparison(doc)
    for row, entry in zip(doc["table"], doc["uncertainty"]["scenarios"]):
        line = next(l for l in report.split("\n") if l.startswith(f"| {row['name']} |"))
        node = entry["table"]["cost_delta_usd"]
        assert f"[{node['lo']:.6g}, {node['hi']:.6g}]" in line
        assert line.count("[") == 8                     # the eight figures with a range
    table, section = report.index("## Comparison"), report.index("## Uncertainty ranges")
    first_scenario = report.index("## Scenario: ")
    assert table < report.index(NO_RANKING_NOTE) < section < first_scenario
    for line in UNCERTAINTY_CAVEAT.split("\n"):
        assert "> " + line in report
    assert RANGE_NOT_RANKING_NOTE in report
    assert report.count("### Ranges") == 3
    assert report.count("Quality uncertainty covers") == 3
    assert "Per-app ranges are in `comparison.json`." in report
    positions = [report.index(f"- **{n}** -- ") for n, _ in _TRIAD]
    assert positions == sorted(positions)               # file order


def test_report_without_a_plan_is_unchanged():
    report = render_comparison(_document(_doc(*_TRIAD), plan=None))
    assert "Uncertainty" not in report and "### Ranges" not in report
    assert "[" not in report.split("## Comparison")[1].split("## Scenario:")[0]


def test_u7_too_few_rows_prints_insufficient_data_and_no_brackets():       # U7
    doc = _document(_doc(*_TRIAD), rows=_rows(12))
    for entry in doc["uncertainty"]["scenarios"]:
        assert entry["totals"] == {"insufficient_data": True, "n": 12}
        assert entry["table"] == {"insufficient_data": True, "n": 12}
    report = render_comparison(doc)
    assert "[" not in report.split("## Comparison")[1].split("## Uncertainty ranges")[0]
    assert report.count("-- insufficient data (n = 12)") == 3
    assert report.count("### Ranges\n\ninsufficient data (n = 12)") == 3


def test_a_small_app_is_named_under_each_scenario():
    plan = UncertaintyPlan(replicates=40, min_rows=46)       # 45 rows per app
    report = render_comparison(_document(_doc(_TRIAD[0]), plan=plan))
    assert ("Per-app ranges are in `comparison.json`. Insufficient data: "
            "chat (n = 45), claims (n = 45).") in report
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_scenarios.py -p no:warnings -rf
```

Expected: a collection error, `ImportError: cannot import name 'RANGE_NOT_RANKING_NOTE' from 'app.eval.scenarios'`.

- [ ] **Step 3: Add `table_ranges`**

Patch `app/eval/uncertainty.py`:

````diff
diff --git a/app/eval/uncertainty.py b/app/eval/uncertainty.py
index 36df18c..6bb56b5 100644
--- a/app/eval/uncertainty.py
+++ b/app/eval/uncertainty.py
@@ -548,6 +548,31 @@ def results_block(results: list[RowResult], plan: UncertaintyPlan, *,
     return block
 
 
+def table_ranges(block: dict) -> dict:
+    """The leaves behind one scenario's comparison-table row, under the
+    table's own column keys (`scenarios._table_row`): the like-for-like cost
+    deltas, the three counts, the latency and quality deltas and the two
+    on-prem shares. `{"insufficient_data": true, "n"}` when the totals have
+    no ranges. `overflow_hops` and `affected_apps` have no range."""
+    totals = block["totals"]
+    if totals.get("insufficient_data"):
+        return dict(totals)
+    per_day = block["per_day"]
+    return {
+        "cost_per_day_delta_usd": (per_day["cost_usd_both_routed"]["delta"]
+                                   if per_day is not None else None),
+        "cost_delta_usd": totals["cost_usd_both_routed"]["delta"],
+        "route_changed": totals["route_changed"],
+        "newly_rejected": totals["newly_rejected"],
+        "newly_served": totals["newly_served"],
+        "latency_p50_delta_ms": totals["latency_ms"]["p50"]["delta"],
+        "latency_p95_delta_ms": totals["latency_ms"]["p95"]["delta"],
+        "quality_mean_delta": totals["quality_mean"]["delta"],
+        "onprem_share_current": totals["onprem_share"]["current"],
+        "onprem_share_proposed": totals["onprem_share"]["proposed"],
+    }
+
+
 def plan_inputs(plan: UncertaintyPlan) -> dict:
     """What enters a document's `inputs` (and so its id) with `--uncertainty`."""
     return {"replicates": plan.replicates, "interval": plan.interval, "seed": plan.seed,
````

- [ ] **Step 4: Thread the plan through the comparison**

Patch `app/eval/scenarios.py`:

````diff
diff --git a/app/eval/scenarios.py b/app/eval/scenarios.py
index 391cf83..435a39f 100644
--- a/app/eval/scenarios.py
+++ b/app/eval/scenarios.py
@@ -8,6 +8,11 @@ recorded traffic against the same `current` arm (`app/eval/simulate.py`).
 RouteForge never picks, ranks or recommends a scenario: they are reported in
 file order, and nothing is applied (handoff §28).
 
+With an `UncertaintyPlan` (P3c-3) every scenario also gets ranges
+(`app/eval/uncertainty.py`), under one `uncertainty` key. Every scenario is
+resampled with the same draws, so the ranges of two scenarios are over the
+same resampled traffic. A range is not a ranking either.
+
 Never imported by serving code (`tests/eval/test_isolation.py`).
 """
 from __future__ import annotations
@@ -28,6 +33,10 @@ from app.eval.simulate import (CACHED_SHARE_NOTE, REPORT_CAVEAT, _assumption_ite
                                _excluded_lines, _fidelity_notes, _metrics_table,
                                _move_lines, _new_rejection_lines, _per_day, arm_results,
                                run_rows, summarize)
+from app.eval.uncertainty import (UncertaintyPlan, bucket_lines, caveat_lines,
+                                  insufficient_text, method_line, outside_line, plan_block,
+                                  quality_line, results_block, table_ranges, window_days,
+                                  with_range)
 
 SCHEMA = "routeforge.policy-scenarios/v1"
 COMPARISON_SCHEMA = "routeforge.scenario-comparison/v1"
@@ -177,23 +186,31 @@ class Comparison:
     baseline: dict         # the `current` arm's fidelity block
     window: dict | None    # {"start", "end"} over the simulated rows' `ts`
     scenarios: list[dict]  # file order: {"name", "edits", "assumptions", "summary"}
+    # P3c-3: `plan_block` plus `scenarios`, one `{"name", ...results_block,
+    # "table"}` per scenario in file order. None without a plan.
+    uncertainty: dict | None = None
 
 
 async def compare(records: list[dict], snapshot: dict, scenarios_doc: dict, *,
-                  base: Settings, since: datetime | None = None) -> Comparison:
+                  base: Settings, since: datetime | None = None,
+                  uncertainty: UncertaintyPlan | None = None) -> Comparison:
     """Replay every scenario against ONE shared `current` arm (spec §7).
 
     Every proposal is validated and applied before anything is replayed
     (`apply_scenarios`, all-or-nothing). The `current` scenario is built and
     simulated once; each proposed scenario is then built, replayed and
     closed in file order, so at most one proposed scenario is open at a
-    time. Nothing is ranked, and nothing is applied to a deployment."""
+    time. Nothing is ranked, and nothing is applied to a deployment.
+
+    `uncertainty` (P3c-3) adds each scenario's ranges, bootstrapped from the
+    rows it was just replayed over -- no row is simulated a second time."""
     applied = apply_scenarios(snapshot, scenarios_doc, base=base)
     current = await build_scenario(snapshot, base=base)
     try:
         current_results = arm_results(records, current, since=since)
         shared: tuple[dict, dict, dict | None] | None = None
         entries: list[dict] = []
+        ranges: list[dict] = []
         for item in applied:
             proposed = await build_scenario(item.doc, base=base)
             try:
@@ -221,10 +238,17 @@ async def compare(records: list[dict], snapshot: dict, scenarios_doc: dict, *,
                 "assumptions": sim["assumptions"],
                 "summary": {k: v for k, v in sim.items() if k not in _LIFTED},
             })
+            if uncertainty is not None:
+                block = results_block(results, uncertainty,
+                                      days=window_days(sim["inputs"]["window"]))
+                ranges.append({"name": item.name, **block, "table": table_ranges(block)})
     finally:
         await close_scenario(current)
     rows, baseline, window = shared      # a file always holds at least one scenario
-    return Comparison(rows=rows, baseline=baseline, window=window, scenarios=entries)
+    return Comparison(
+        rows=rows, baseline=baseline, window=window, scenarios=entries,
+        uncertainty=(None if uncertainty is None
+                     else {**plan_block(uncertainty), "scenarios": ranges}))
 
 
 # --- output (spec §8) ----------------------------------------------------------
@@ -271,7 +295,7 @@ def comparison_doc(comparison: Comparison, *, inputs: dict) -> dict:
     full_inputs["window"] = comparison.window
     comparison_id = hashlib.sha256(
         json.dumps(full_inputs, sort_keys=True).encode("utf-8")).hexdigest()[:16]
-    return _round6({
+    doc = {
         "schema": COMPARISON_SCHEMA,
         "comparison_id": comparison_id,
         "inputs": full_inputs,
@@ -279,7 +303,11 @@ def comparison_doc(comparison: Comparison, *, inputs: dict) -> dict:
         "baseline": comparison.baseline,
         "scenarios": comparison.scenarios,
         "table": [_table_row(entry, comparison.window) for entry in comparison.scenarios],
-    })
+    }
+    if comparison.uncertainty is not None:
+        # The ONE key `--uncertainty` adds; every key above is unchanged.
+        doc["uncertainty"] = comparison.uncertainty
+    return _round6(doc)
 
 
 _TABLE_COLUMNS = ("scenario", "Δcost/day USD", "Δcost USD", "route changed", "newly rejected",
@@ -350,17 +378,26 @@ def _agreement_lines(doc: dict) -> list[str]:
     return lines
 
 
-def _comparison_row(row: dict) -> str:
+def _comparison_row(row: dict, ranges: dict | None = None) -> str:
+    """One table row. `ranges` (P3c-3, `uncertainty.table_ranges`) puts
+    `[lo, hi]` after each figure that has one; without it, or when the
+    scenario has too few rows for a range, the cells are the points alone."""
+    if ranges is None or ranges.get("insufficient_data"):
+        ranges = {}
+
+    def cell(key: str, text: str) -> str:
+        return with_range(text, ranges.get(key))
+
     cells = [
         row["name"],
-        _fmt(row["cost_per_day_delta_usd"]),
-        _fmt(row["cost_delta_usd"]),
-        str(row["route_changed"]),
-        str(row["newly_rejected"]),
-        str(row["newly_served"]),
-        _fmt(row["latency_p50_delta_ms"]),
-        _fmt(row["latency_p95_delta_ms"]),
-        _fmt(row["quality_mean_delta"]),
+        cell("cost_per_day_delta_usd", _fmt(row["cost_per_day_delta_usd"])),
+        cell("cost_delta_usd", _fmt(row["cost_delta_usd"])),
+        cell("route_changed", str(row["route_changed"])),
+        cell("newly_rejected", str(row["newly_rejected"])),
+        cell("newly_served", str(row["newly_served"])),
+        cell("latency_p50_delta_ms", _fmt(row["latency_p50_delta_ms"])),
+        cell("latency_p95_delta_ms", _fmt(row["latency_p95_delta_ms"])),
+        cell("quality_mean_delta", _fmt(row["quality_mean_delta"])),
         f"{_fmt(row['onprem_share_current'])}→{_fmt(row['onprem_share_proposed'])}",
         str(row["overflow_hops"]),
         ", ".join(row["affected_apps"]) or "none",
@@ -368,6 +405,32 @@ def _comparison_row(row: dict) -> str:
     return "| " + " | ".join(cells) + " |"
 
 
+#: Printed with the ranges, verbatim (P3c-3 spec §10, invariant U11).
+RANGE_NOT_RANKING_NOTE = (
+    "In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking: "
+    "overlapping ranges do not make two scenarios equal, and separate ranges do not make "
+    "one of them the one to choose."
+)
+
+
+def _uncertainty_lines(block: dict) -> list[str]:
+    """The comparison report's "Uncertainty ranges" section: the caveat, how
+    the ranges were made, then one line per scenario in FILE order -- its
+    quality coverage, or that it has too few rows."""
+    lines = ["## Uncertainty ranges", ""]
+    lines += caveat_lines()
+    lines += ["", method_line(block), "", RANGE_NOT_RANKING_NOTE, ""]
+    for entry in block["scenarios"]:
+        if entry["totals"].get("insufficient_data"):
+            lines.append(f"- **{entry['name']}** -- {insufficient_text(entry['totals'])}")
+            continue
+        text = quality_line(entry["quality_posterior"])
+        outside = outside_line(entry["points_outside_interval"])
+        lines.append(f"- **{entry['name']}** -- {text}" + (f" {outside}" if outside else ""))
+    lines.append("")
+    return lines
+
+
 def render_comparison(doc: dict) -> str:
     """`report.md` for a comparison (spec §8): the caveat, the agreement
     banner, ONE table in file order, the assumptions each scenario makes,
@@ -386,14 +449,19 @@ def render_comparison(doc: dict) -> str:
 
     lines.append("## Comparison")
     lines.append("")
+    uncertainty = doc.get("uncertainty")
+    ranges = {entry["name"]: entry for entry in uncertainty["scenarios"]} if uncertainty else {}
     lines.append("| " + " | ".join(_TABLE_COLUMNS) + " |")
     lines.append("|" + "---|" * len(_TABLE_COLUMNS))
-    lines += [_comparison_row(row) for row in doc["table"]]
+    lines += [_comparison_row(row, ranges[row["name"]]["table"] if ranges else None)
+              for row in doc["table"]]
     lines.append("")
     lines.append(NO_RANKING_NOTE)
     lines.append("")
     lines.append(LIKE_FOR_LIKE_NOTE)
     lines.append("")
+    if uncertainty:
+        lines += _uncertainty_lines(uncertainty)
 
     assuming = [(entry["name"], _assumption_items(entry["assumptions"]))
                 for entry in doc["scenarios"]]
@@ -414,6 +482,20 @@ def render_comparison(doc: dict) -> str:
         lines.append("")
         lines += _metrics_table(summary["totals"])
         lines.append("")
+        if uncertainty:
+            lines.append("### Ranges")
+            lines.append("")
+            lines += bucket_lines(ranges[entry["name"]]["totals"], uncertainty["interval"])
+            lines.append("")
+            small = [f"{app_id} (n = {bucket['n']})"
+                     for app_id, bucket in sorted(ranges[entry["name"]]["by_app"].items())
+                     if bucket.get("insufficient_data")]
+            if small:
+                lines.append("Per-app ranges are in `comparison.json`. Insufficient data: "
+                             + ", ".join(small) + ".")
+            else:
+                lines.append("Per-app ranges are in `comparison.json`.")
+            lines.append("")
         lines.append("### Moves")
         lines.append("")
         lines += _move_lines(summary["moves"])
````

- [ ] **Step 5: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_scenarios.py tests/eval/test_scenarios_compare.py tests/eval/test_uncertainty_golden.py -p no:warnings -rf
```

Expected: `43 passed`.

- [ ] **Step 6: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `959 passed`, no `FAILED` line.

- [ ] **Step 7: Commit**

```bash
git add app/eval/uncertainty.py app/eval/scenarios.py tests/eval/test_uncertainty_scenarios.py
git commit -m "$(cat <<'EOF'
feat(eval): ranges in the scenario comparison (P3c-3)

Every scenario is bootstrapped with the same draws, so a scenario's
ranges do not depend on the others in the file. The table stays in file
order and shows [lo, hi] beside each figure; a range is not a ranking,
and the report says so.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Ranges on shadow prices (spec §5.3, §9.3, §10.3; U1–U4, U7, U11, U13)

**Files:**
- Modify: `app/eval/shadow.py`
- Create: `tests/eval/test_uncertainty_shadow.py`

**Interfaces:**
- Consumes:
  - `app/eval/uncertainty.py`: `UncertaintyPlan`, `run_bootstrap(results, plan, *, days=None) -> Bootstrap` (`.totals` is `None` below `min_rows`, else a map from flat figure name to one value per replicate; this task reads `route_changed`, `newly_served`, `newly_rejected`, `cost_usd_both_routed.delta`), `leaf(point, values, plan, *, zero_n=None) -> dict`, `insufficient_text(bucket)`, `level_text(level)`, `with_range(text, node)`.
  - `app/eval/shadow.py` as it is: `async shadow_prices(records, baseline_doc, *, base, cap=50, baseline_label="snapshot", since=None) -> dict` (keys `baseline`, `evaluated`, `skipped_by_cap`, `non_binding`, `constraints`); inside its loop `rel`, `results`, `stats` and `days` are in scope and a binding entry is `_round6(_entry(rel, results, stats, days))`; an entry has `requirement`, `scope`, `security`, `rows_in_scope`, `route_changed`, `rejections_avoided`, `newly_rejected`, `cost_usd_both_routed` (`{baseline, relaxed, delta}`), `cost_saved_per_day` (`None` under a one-day window); `_rank_key(entry)`; `render_shadow_section(block) -> str`; `_scope_str(scope)`; `_PROTECTION_LABEL`; `_fmt`.
  - From `tests/eval/test_uncertainty.py`: `BANNED`, `_keys`, `_leaves`.
- Produces:
  - `shadow_prices(..., uncertainty: UncertaintyPlan | None = None)`. With a plan the block gains ONE key: `uncertainty = {"interval": plan.interval, "constraints": [...]}`, the list parallel to `constraints` (same order). Each element: `requirement`, `scope`, `security`, `n`, and either `insufficient_data: True` or the leaves `route_changed`, `rejections_avoided`, `newly_rejected`, `cost_usd_both_routed_delta`, `cost_saved_per_day` (`None` when `days` is `None`).
  - `_entry_ranges(entry: dict, results: list[RowResult], plan: UncertaintyPlan, days: float | None) -> dict`.
  - `render_shadow_section` prints a `### Ranges` table when the block has `uncertainty`.

**Rules.**

- Without a plan the block and its rendered section are exactly what they are today. Task 1's `shadow` and `proposal_shadow` goldens must pass.
- Only binding relaxations get ranges; non-binding ones stay a count.
- The ranges do not enter `_rank_key`. The existing order is kept, and the ranges list is built **after** the sort so the two stay parallel.
- Every security row in the ranges table carries `_PROTECTION_LABEL`. Never print a security requirement's range without it.
- `cost_saved_per_day` is minus the like-for-like delta divided by `days`, per replicate, from the **unrounded** delta: that is the entry's own arithmetic. Do not use the engine's `per_day.*` figure here (it mirrors the comparison table, which rounds first).

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_uncertainty_shadow.py`:

````python
"""P3c-3: ranges on shadow prices (spec §5.3, §9.3, §10.3; U1, U4, U7, U11)."""
from __future__ import annotations

import asyncio
import json

from app.eval.shadow import _PROTECTION_LABEL, render_shadow_section, shadow_prices
from app.eval.simulate import simulate
from app.eval.uncertainty import UncertaintyPlan, results_block
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)
from tests.eval.test_uncertainty import BANNED, _keys, _leaves

P = "routeforge.policy-proposal/v1"
PLAN = UncertaintyPlan(replicates=150)


def _run(records, doc, **kw):
    return asyncio.run(shadow_prices(records, doc, base=BASE_SETTINGS, **kw))


def _rows(app: str, n: int, days: int = 3) -> list[dict]:
    return [make_row(f"{app}-{i:03d}", app, complexity=[0.05, 0.6, 1.0][i % 3],
                     prompt_tokens=[200, 1200, 3000][i % 3] + 5 * i,
                     ts=f"2026-09-{20 + i % days:02d}T12:{i % 60:02d}:00+00:00")
            for i in range(n)]


def _snap():
    """On-prem has a cheap and a mid endpoint; the frontier tier is cloud
    only. `big` is pinned to the mid tier (relaxing it saves money: an
    operational constraint). `safe` is on-prem only, so its largest requests
    are held at the mid tier (relaxing it moves them to the cloud: a
    security constraint). `few` has too few rows for a range."""
    return make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid")],
        apps=[app_entry("big", allowed_tiers=["mid"]),
              app_entry("few", allowed_tiers=["mid"]),
              app_entry("safe", residency="onprem_only")],
        providers={"azure": {"enabled": True}})


def _records():
    return _rows("big", 60) + _rows("few", 6) + _rows("safe", 45)


def _find(entries, requirement, app):
    return next(e for e in entries
                if e["requirement"] == requirement and e["scope"] == {"app": app})


def test_u1_a_plan_adds_one_key_to_the_block():                            # U1
    plain = _run(_records(), _snap())
    ranged = _run(_records(), _snap(), uncertainty=PLAN)
    assert "uncertainty" not in plain
    assert set(ranged) == set(plain) | {"uncertainty"}
    without = {k: v for k, v in ranged.items() if k != "uncertainty"}
    assert json.dumps(without, sort_keys=True) == json.dumps(plain, sort_keys=True)


def test_ranges_are_parallel_to_constraints_and_about_the_same_figures():
    block = _run(_records(), _snap(), uncertainty=PLAN)
    ranges = block["uncertainty"]
    assert ranges["interval"] == 0.9
    assert len(ranges["constraints"]) == len(block["constraints"]) >= 3
    for entry, r in zip(block["constraints"], ranges["constraints"]):
        assert (r["requirement"], r["scope"], r["security"], r["n"]) == (
            entry["requirement"], entry["scope"], entry["security"], entry["rows_in_scope"])
        if r.get("insufficient_data"):
            continue
        assert r["route_changed"]["point"] == entry["route_changed"]
        assert r["rejections_avoided"]["point"] == entry["rejections_avoided"]
        assert r["newly_rejected"]["point"] == entry["newly_rejected"]
        assert r["cost_usd_both_routed_delta"]["point"] == entry["cost_usd_both_routed"]["delta"]
        assert r["cost_saved_per_day"]["point"] == entry["cost_saved_per_day"]
    big = _find(ranges["constraints"], "allowed_tiers", "big")
    assert big["n"] == 60
    saved = big["cost_saved_per_day"]
    assert 0 < saved["lo"] < saved["point"] < saved["hi"]
    moved = big["route_changed"]
    assert moved["point"] == 20 and moved["lo"] < 20 < moved["hi"]


def test_u7_a_relaxation_with_few_rows_in_scope_gets_no_range():           # U7
    block = _run(_records(), _snap(), uncertainty=PLAN)
    few = _find(block["uncertainty"]["constraints"], "allowed_tiers", "few")
    assert few == {"requirement": "allowed_tiers", "scope": {"app": "few"},
                   "security": False, "n": 6, "insufficient_data": True}
    # The relaxation itself is still listed with its point figures.
    assert _find(block["constraints"], "allowed_tiers", "few")["route_changed"] == 2


def test_cost_saved_per_day_is_null_under_a_one_day_window():
    rows = _rows("big", 60, days=1)
    block = _run(rows, _snap(), uncertainty=PLAN)
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")
    assert big["cost_saved_per_day"] is None
    assert big["cost_usd_both_routed_delta"]["lo"] < big["cost_usd_both_routed_delta"]["hi"] < 0


def test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal():  # U4
    """Relaxing `allowed_tiers` for `big` is the proposal "set big's
    allowed_tiers to null". Its range must be that proposal's `big` bucket,
    figure for figure: the same rows, drawn the same way."""
    records = _records()
    block = _run(records, _snap(), uncertainty=PLAN)
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")
    results, _excluded = asyncio.run(simulate(
        records, _snap(), {"schema": P, "apps": {"big": {"allowed_tiers": None}}},
        base=BASE_SETTINGS))
    bucket = results_block(results, PLAN)["by_app"]["big"]
    assert big["cost_usd_both_routed_delta"] == bucket["cost_usd_both_routed"]["delta"]
    assert big["route_changed"] == bucket["route_changed"]
    assert big["newly_rejected"] == bucket["newly_rejected"]
    assert big["rejections_avoided"] == bucket["newly_served"]


def test_u2_shadow_ranges_are_deterministic():                             # U2
    first = json.dumps(_run(_records(), _snap(), uncertainty=PLAN), sort_keys=True)
    assert json.dumps(_run(_records(), _snap(), uncertainty=PLAN), sort_keys=True) == first


def test_u3_every_shadow_point_is_inside_its_range_or_flagged():           # U3
    block = _run(_records(), _snap(), uncertainty=PLAN)
    nodes = list(_leaves(block["uncertainty"]))
    assert nodes
    for path, node in nodes:
        assert node["lo"] <= node["point"] <= node["hi"] or node.get("point_outside"), path


def test_u11_a_security_range_keeps_the_protection_label():                # U11
    """A range on the cost of a protection is still the cost of a
    protection: the row is labelled, in the ranges table too, and nothing in
    the block ranks or recommends."""
    block = _run(_records(), _snap(), uncertainty=PLAN)
    safe = _find(block["uncertainty"]["constraints"], "residency", "safe")
    assert safe["security"] is True
    assert not [k for k in _keys(block) if any(word in k.lower() for word in BANNED)]
    report = render_shadow_section(block)
    section = report[report.index("### Ranges"):]
    line = next(l for l in section.split("\n") if l.startswith("| residency | app:safe |"))
    assert line.endswith(f"| {_PROTECTION_LABEL} |")
    operational = next(l for l in section.split("\n")
                       if l.startswith("| allowed_tiers | app:big |"))
    assert operational.endswith("|  |") and _PROTECTION_LABEL not in operational
    assert "sampling variability only" in section


def test_report_ranges_table():
    block = _run(_records(), _snap(), uncertainty=PLAN)
    report = render_shadow_section(block)
    assert report.index("### Cost of protection") < report.index("### Ranges") \
        < report.index("Non-binding:")
    section = report[report.index("### Ranges"):]
    assert "`[lo, hi]` is the 90% range of the figure before it" in section
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")["cost_saved_per_day"]
    assert f"/day [{big['lo']:.6g}, {big['hi']:.6g}]" in section
    few = next(l for l in section.split("\n") if l.startswith("| allowed_tiers | app:few |"))
    assert "| insufficient data (n = 6) |" in few and "[" not in few
    # One row per binding relaxation, in the block's order.
    rows = [l for l in section.split("\n") if l.startswith("| ") and "requirement" not in l]
    assert len(rows) == len(block["constraints"])


def test_report_without_a_plan_is_unchanged():
    report = render_shadow_section(_run(_records(), _snap()))
    assert "### Ranges" not in report and "[" not in report


def test_no_binding_relaxation_gives_an_empty_ranges_list():
    snap = make_snapshot(apps=[app_entry("big")])
    block = _run(_rows("big", 40), snap, uncertainty=PLAN)
    assert block["constraints"] == [] and block["uncertainty"] == {
        "interval": 0.9, "constraints": []}
    assert "### Ranges\n\n" in render_shadow_section(block)
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_shadow.py -p no:warnings -rf
```

Expected: `10 failed, 1 passed`. The failures are `TypeError: shadow_prices() got an unexpected keyword argument 'uncertainty'`; `test_report_without_a_plan_is_unchanged` passes already, as it should.

- [ ] **Step 3: Bootstrap each binding relaxation and print its ranges**

Patch `app/eval/shadow.py`:

````diff
diff --git a/app/eval/shadow.py b/app/eval/shadow.py
index c10a96c..97e6697 100644
--- a/app/eval/shadow.py
+++ b/app/eval/shadow.py
@@ -49,6 +49,8 @@ from app.eval.simulate import (
     select_rows,
     simulate_row,
 )
+from app.eval.uncertainty import (UncertaintyPlan, insufficient_text, leaf, level_text,
+                                  run_bootstrap, with_range)
 
 from app.policy.core import (
     REQ_ALLOWED_MODELS,
@@ -424,6 +426,36 @@ def _entry(rel: Relaxation, results: list[RowResult], stats: dict,
     }
 
 
+def _entry_ranges(entry: dict, results: list[RowResult], plan: UncertaintyPlan,
+                   days: float | None) -> dict:
+    """P3c-3: the ranges of one binding relaxation's figures (spec §5.3),
+    bootstrapped over the rows it re-simulated: `route_changed`,
+    `rejections_avoided`, `newly_rejected`, the like-for-like cost delta
+    and, with a window of at least a day, `cost_saved_per_day`. Each leaf's
+    `point` is the entry's own figure. Fewer than `plan.min_rows` rows in
+    scope: `insufficient_data` and no range.
+
+    The draws are the ones every other part of the run uses for those apps,
+    so a relaxation's range and the proposal's are over the same resamples."""
+    head = {"requirement": entry["requirement"], "scope": entry["scope"],
+            "security": entry["security"], "n": entry["rows_in_scope"]}
+    boot = run_bootstrap(results, plan)
+    if boot.totals is None:
+        return {**head, "insufficient_data": True}
+    both = boot.totals["cost_usd_both_routed.delta"]
+    return {
+        **head,
+        "route_changed": leaf(entry["route_changed"], boot.totals["route_changed"], plan),
+        "rejections_avoided": leaf(entry["rejections_avoided"],
+                                   boot.totals["newly_served"], plan),
+        "newly_rejected": leaf(entry["newly_rejected"], boot.totals["newly_rejected"], plan),
+        "cost_usd_both_routed_delta": leaf(entry["cost_usd_both_routed"]["delta"], both, plan),
+        # The entry's own arithmetic: minus the like-for-like delta, per day.
+        "cost_saved_per_day": (leaf(entry["cost_saved_per_day"], [-v / days for v in both], plan)
+                               if days is not None else None),
+    }
+
+
 def _rank_key(entry: dict) -> tuple:
     saved = entry["cost_saved_per_day"]
     if saved is None:
@@ -435,7 +467,8 @@ def _rank_key(entry: dict) -> tuple:
 
 async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settings,
                         cap: int = 50, baseline_label: str = "snapshot",
-                        since: datetime | None = None) -> dict:
+                        since: datetime | None = None,
+                        uncertainty: UncertaintyPlan | None = None) -> dict:
     """The spec §5 `shadow_prices` block for `baseline_doc` (the snapshot,
     or the snapshot with a proposal already applied -- the caller decides and
     says which in `baseline_label`).
@@ -448,7 +481,12 @@ async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settin
     (`newly_rejected`, `newly_rejected_reasons`), not raised. A relaxation
     is binding when it changes a route, avoids a rejection or newly rejects
     a row; the rest are counted as `non_binding`, not listed. `since` cuts
-    the export exactly as `simulate.run_rows` does (P3c-2)."""
+    the export exactly as `simulate.run_rows` does (P3c-2).
+
+    `uncertainty` (P3c-3) adds ONE key to the block, `uncertainty`:
+    `{"interval", "constraints"}`, the second a list parallel to
+    `constraints` (same order) of `_entry_ranges`. Without a plan the block
+    is exactly what it was."""
     observed = observed_latencies([r for r in records if not _before_since(r, since)])
     baseline = await build_scenario(baseline_doc, base=base)
     try:
@@ -461,6 +499,7 @@ async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settin
         rels, skipped = enumerate_relaxations(baseline_doc, rows, cap=cap)
 
         constraints: list[dict] = []
+        ranges: dict[int, dict] = {}        # id(entry) -> its ranges
         non_binding = 0
         for rel in rels:
             doc_r, scoped = rel.apply(baseline_doc, rows)
@@ -477,14 +516,23 @@ async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settin
 
             stats = _bucket_stats(results)
             if stats["route_changed"] + stats["newly_served"] + stats["newly_rejected"] > 0:
-                constraints.append(_round6(_entry(rel, results, stats, days)))
+                entry = _round6(_entry(rel, results, stats, days))
+                constraints.append(entry)
+                if uncertainty is not None:
+                    ranges[id(entry)] = _entry_ranges(entry, results, uncertainty, days)
             else:
                 non_binding += 1
 
         constraints.sort(key=_rank_key)
-        return _round6({"baseline": baseline_label, "evaluated": len(rels),
-                        "skipped_by_cap": skipped, "non_binding": non_binding,
-                        "constraints": constraints})
+        block = {"baseline": baseline_label, "evaluated": len(rels),
+                 "skipped_by_cap": skipped, "non_binding": non_binding,
+                 "constraints": constraints}
+        if uncertainty is not None:
+            # Looked up by identity AFTER the sort, so the two lists stay
+            # parallel whatever the order is.
+            block["uncertainty"] = {"interval": uncertainty.interval,
+                                    "constraints": [ranges[id(e)] for e in constraints]}
+        return _round6(block)
     finally:
         await close_scenario(baseline)
 
@@ -557,11 +605,48 @@ def _shadow_table(entries: list[dict], *, security: bool) -> list[str]:
     return lines
 
 
+_RANGE_COLUMNS = ("requirement", "scope", "rows", "cost saved/day (or Δcost both-routed)",
+                  "rejections avoided", "newly rejected", "route changed", "label")
+
+
+def _range_row(entry: dict) -> str:
+    """One relaxation's ranges. A security requirement keeps its label here
+    too: a range on the cost of a protection is still not a recommendation."""
+    cells = [entry["requirement"], _scope_str(entry["scope"]), str(entry["n"])]
+    if entry.get("insufficient_data"):
+        cells += [insufficient_text(entry), "", "", ""]
+    else:
+        per_day = entry["cost_saved_per_day"]
+        if per_day is not None:
+            cost = with_range(f"{_fmt(per_day['point'])}/day", per_day)
+        else:
+            both = entry["cost_usd_both_routed_delta"]
+            cost = with_range(f"{_fmt(both['point'])} (both-routed)", both)
+        cells += [cost] + [with_range(str(entry[key]["point"]), entry[key])
+                           for key in ("rejections_avoided", "newly_rejected", "route_changed")]
+    cells.append(_PROTECTION_LABEL if entry["security"] else "")
+    return "| " + " | ".join(cells) + " |"
+
+
+def _range_lines(ranges: dict) -> list[str]:
+    """The "Ranges" subsection, in the block's own order."""
+    lines = ["### Ranges", "",
+             f"`[lo, hi]` is the {level_text(ranges['interval'])} range of the figure before "
+             "it: sampling variability only (see \"Uncertainty ranges\").", ""]
+    if not ranges["constraints"]:
+        return lines + ["None.", ""]
+    lines += ["| " + " | ".join(_RANGE_COLUMNS) + " |", "|" + "---|" * len(_RANGE_COLUMNS)]
+    lines += [_range_row(entry) for entry in ranges["constraints"]]
+    lines.append("")
+    return lines
+
+
 def render_shadow_section(block: dict) -> str:
     """The `report.md` "Shadow prices" section (spec §5): the caveat, then
     the operational-constraints table, then the cost-of-protection table
     (every row labelled, never a recommendation), then the newly-rejected
-    note, then a non-binding/skipped footer."""
+    note, then (P3c-3, only when the block carries ranges) the ranges, then
+    a non-binding/skipped footer."""
     lines: list[str] = ["## Shadow prices", ""]
     lines += ["> " + line for line in SHADOW_CAVEAT.split("\n")]
     lines.append("")
@@ -583,6 +668,9 @@ def render_shadow_section(block: dict) -> str:
     lines.append(NEWLY_REJECTED_NOTE)
     lines.append("")
 
+    if block.get("uncertainty") is not None:
+        lines += _range_lines(block["uncertainty"])
+
     lines.append(f"Non-binding: {block['non_binding']} · "
                 f"skipped by cap: {block['skipped_by_cap']}")
 
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_shadow.py tests/eval/test_shadow.py tests/eval/test_uncertainty_golden.py -p no:warnings -rf
```

Expected: `56 passed`.

- [ ] **Step 5: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `970 passed`, no `FAILED` line.

- [ ] **Step 6: Commit**

```bash
git add app/eval/shadow.py tests/eval/test_uncertainty_shadow.py
git commit -m "$(cat <<'EOF'
feat(eval): ranges on shadow prices (P3c-3)

Each binding relaxation is bootstrapped over the rows it re-simulated,
with the same draws the rest of the run uses for those apps. A security
requirement's range keeps the price-of-a-protection label. The order of
the block is unchanged.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The CLI (spec §3, §9, §10; U1, U2, U10)

**Files:**
- Modify: `scripts/policy_simulate.py`
- Create: `tests/eval/test_uncertainty_cli.py`

**Interfaces:**
- Consumes:
  - `app/eval/uncertainty.py`: `MAX_REPLICATES`, `UncertaintyPlan(replicates, interval, seed, min_rows, posteriors, posterior_files)` (raises `ValueError`), `load_posteriors(paths) -> (posteriors, files)` (raises `UncertaintyError`, a `ValueError`, or `OSError`), `plan_block(plan)`, `plan_inputs(plan)`, `render_uncertainty_section(block) -> str` (ends with `\n`), `results_block(results, plan, *, days=None)`, `window_days(window)`.
  - `app/eval/scenarios.py::compare(..., uncertainty=None)` (Task 5); `app/eval/shadow.py::shadow_prices(..., uncertainty=None)` (Task 6).
  - `app/eval/simulate.py`: `baseline_check(records, snapshot, *, base, since=None) -> dict`, `agreement_ok(baseline, minimum) -> bool`.
  - `scripts/policy_simulate.py` as it is: `_parser()`, `_below_min_agreement(baseline, minimum) -> int` (returns `4`), `_compare_scenarios(args, *, records, warnings, snapshot, scenarios_doc, inputs, base, since) -> int`, `main(argv)`. In `main`, after the proposal branch runs, `results`, `sim` and `baseline` are in scope; `sim["inputs"]["window"]` is the window.
  - From `tests/eval/test_uncertainty_golden.py`: `EXPECTED`, `EXPORT`, `MODES`, `PROPOSAL`, `ROOT`, `SCRIPT`, `SNAPSHOT`, `load_cli`, `run_mode`. From `tests/eval/test_uncertainty.py`: `_leaves`.
- Produces:
  - Flags `--uncertainty`, `--bootstrap N`, `--interval P`, `--seed S`, `--min-rows K`, `--quality-posteriors FILE` (repeatable). The five tuning flags default to `None` so "given" can be told from "default".
  - `_uncertainty_plan(args) -> UncertaintyPlan | None`.
  - With the flag: `inputs["uncertainty"] = plan_inputs(plan)` in every mode; `simulation.json` gains `uncertainty` (`plan_block` merged with `results_block` when there is a proposal; `plan_block` alone otherwise) and, with shadow prices, `shadow_prices.uncertainty`; `comparison.json` gains `uncertainty`; `report.md` gains the section(s).
  - `_compare_scenarios(..., plan: UncertaintyPlan | None)`.

**Rules.**

- Without `--uncertainty` nothing changes: `inputs` gains no key (not even a null one), so the ids are unchanged. Task 1's golden test is the check.
- A tuning flag without `--uncertainty` is exit `2`; so is a value out of range and a posterior file that does not load. Print `error: …` on stderr, write nothing, never a traceback.
- The bootstrap starts only after the `--min-agreement` gate has passed. In `--scenarios` mode, when both `--min-agreement` and `--uncertainty` are given, gate on `baseline_check` before calling `compare`.
- Report order with a proposal: today's report, then `## Uncertainty ranges`, then `## Shadow prices` (which now ends with its own `### Ranges`). Without a proposal: the caveat, then `## Uncertainty ranges` (caveat and method only), then `## Shadow prices`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_uncertainty_cli.py`:

````python
"""P3c-3: `scripts/policy_simulate.py --uncertainty`, end to end, in every
mode (spec §3, §9; invariants U1, U2, U10).

Runs the CLI in-process over the committed golden inputs
(`tests/eval/test_uncertainty_golden.py`), so "the flag only adds" is
checked against the outputs pinned from before P3c-3.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.eval.calibration import build_recommendation, dump_recommendation, estimate
from app.eval.uncertainty import METHOD, NOT_COVERED, UNCERTAINTY_CAVEAT
from tests.eval.test_uncertainty import _leaves
from tests.eval.test_uncertainty_golden import (EXPECTED, EXPORT, MODES, PROPOSAL, ROOT,
                                                SCRIPT, SNAPSHOT, load_cli, run_mode)

FAST = ("--uncertainty", "--bootstrap", "60")
_ID = {"proposal": "simulation_id", "proposal_shadow": "simulation_id",
       "scenarios": "comparison_id"}          # the shadow-only document has no id


def _doc(mode: str, out) -> dict:
    return json.loads((out / MODES[mode][1][0]).read_text())


def _expected(mode: str) -> dict:
    return json.loads((EXPECTED / mode / MODES[mode][1][0]).read_text())


def _strip(doc: dict) -> dict:
    """`doc` without everything `--uncertainty` adds, and without its id
    (the id is a hash of `inputs`, which the flag extends on purpose)."""
    doc = json.loads(json.dumps(doc))
    doc.pop("uncertainty", None)
    doc["inputs"].pop("uncertainty", None)
    if isinstance(doc.get("shadow_prices"), dict):
        doc["shadow_prices"].pop("uncertainty", None)
    doc.pop("simulation_id", None)
    doc.pop("comparison_id", None)
    return doc


# --- U1: the flag only adds -----------------------------------------------------

@pytest.mark.parametrize("mode", sorted(MODES))
def test_u1_the_flag_adds_keys_and_changes_no_existing_value(mode, tmp_path):   # U1
    """With the flag, the document is the pinned pre-P3c-3 document plus
    `uncertainty`, `inputs.uncertainty` and (with shadow prices)
    `shadow_prices.uncertainty`. No existing key moves."""
    out = tmp_path / "out"
    assert run_mode(mode, out, *FAST) == 0
    doc = _doc(mode, out)
    assert _strip(doc) == _strip(_expected(mode))
    assert "uncertainty" in doc and "uncertainty" in doc["inputs"]
    assert ("uncertainty" in doc.get("shadow_prices", {})) == ("shadow" in mode)
    assert sorted(p.name for p in out.iterdir()) == sorted(MODES[mode][1])


# --- U2: deterministic ----------------------------------------------------------

@pytest.mark.parametrize("mode", sorted(MODES))
def test_u2_the_flagged_run_is_byte_identical_twice(mode, tmp_path):       # U2
    first, second = tmp_path / "a", tmp_path / "b"
    assert run_mode(mode, first, *FAST) == 0 and run_mode(mode, second, *FAST) == 0
    for name in MODES[mode][1]:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


# --- U10: the plan is an input ----------------------------------------------------

def test_u10_plan_values_are_in_inputs_with_their_defaults(tmp_path):      # U10
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--bootstrap", "40") == 0
    doc = _doc("proposal", out)
    assert doc["inputs"]["uncertainty"] == {
        "replicates": 40, "interval": 0.9, "seed": 0, "min_rows": 30,
        "quality_posteriors_sha256": []}
    block = doc["uncertainty"]
    assert (block["method"], block["strata"], block["replicates"], block["interval"],
            block["seed"], block["min_rows"]) == (METHOD, "application_id", 40, 0.9, 0, 30)
    assert block["not_covered"] == list(NOT_COVERED)
    assert block["quality_posterior_sets"] == []


@pytest.mark.parametrize("mode", sorted(_ID))
def test_u10_the_id_tells_plans_apart(mode, tmp_path):                     # U10
    """Same files, different plan: a different id. And a flagged run never
    shares an id with the unflagged one."""
    ids = {}
    for label, extra in (("base", FAST), ("seed", (*FAST, "--seed", "1")),
                         ("n", ("--uncertainty", "--bootstrap", "61")),
                         ("level", (*FAST, "--interval", "0.8")),
                         ("min", (*FAST, "--min-rows", "5"))):
        out = tmp_path / label
        assert run_mode(mode, out, *extra) == 0
        ids[label] = _doc(mode, out)[_ID[mode]]
    ids["unflagged"] = _expected(mode)[_ID[mode]]
    assert len(set(ids.values())) == len(ids), ids


def test_the_seed_moves_the_ranges_and_nothing_else(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert run_mode("proposal", a, *FAST) == 0
    assert run_mode("proposal", b, *FAST, "--seed", "7") == 0
    one, two = _doc("proposal", a), _doc("proposal", b)
    assert _strip(one) == _strip(two)
    assert one["uncertainty"]["totals"] != two["uncertainty"]["totals"]
    assert ([(path, node["point"]) for path, node in _leaves(one["uncertainty"])]
            == [(path, node["point"]) for path, node in _leaves(two["uncertainty"])])


# --- what each mode's documents carry -------------------------------------------

def test_proposal_block_has_totals_apps_and_per_day(tmp_path):
    out = tmp_path / "out"
    assert run_mode("proposal", out, *FAST) == 0
    doc = _doc("proposal", out)
    block = doc["uncertainty"]
    assert block["totals"]["n"] == doc["rows"]["simulated"] == 111
    assert set(block["by_app"]) == set(doc["by_app"]) == {"big", "few", "safe"}
    assert block["by_app"]["few"] == {"insufficient_data": True, "n": 6}
    for arm in ("current", "proposed", "delta"):
        assert block["totals"]["cost_usd"][arm]["point"] == doc["totals"]["cost_usd"][arm]
        assert block["per_day"]["cost_usd"][arm]["point"] == doc["per_day"]["cost_usd"][arm]
    for name in ("route_changed", "newly_rejected", "newly_served"):
        assert block["totals"][name]["point"] == doc["totals"][name]
    for path, node in _leaves(block):
        assert node["lo"] is None or node["lo"] <= node["point"] <= node["hi"] \
            or node.get("point_outside"), path


def test_proposal_report_gets_the_section_between_the_report_and_shadow_prices(tmp_path):
    out = tmp_path / "out"
    assert run_mode("proposal_shadow", out, *FAST) == 0
    report = (out / "report.md").read_text()
    expected = (EXPECTED / "proposal" / "report.md").read_text()
    assert report.startswith(expected)                 # the P3a report, untouched
    order = [report.index(h) for h in ("## Excluded rows", "## Uncertainty ranges",
                                       "### Totals (n = 111)", "## Shadow prices",
                                       "### Ranges")]
    assert order == sorted(order)
    for line in UNCERTAINTY_CAVEAT.split("\n"):
        assert "> " + line in report
    assert "#### few (n = 6)\n\ninsufficient data (n = 6)" in report
    assert "90% percentile intervals from 60 bootstrap resamples" in report


def test_shadow_only_run_has_the_block_head_and_shadow_ranges(tmp_path):
    out = tmp_path / "out"
    assert run_mode("shadow", out, *FAST) == 0
    doc = _doc("shadow", out)
    assert set(doc) == {"schema", "inputs", "shadow_prices", "uncertainty"}
    assert "totals" not in doc["uncertainty"] and doc["uncertainty"]["replicates"] == 60
    ranges = doc["shadow_prices"]["uncertainty"]
    assert len(ranges["constraints"]) == len(doc["shadow_prices"]["constraints"]) > 0
    report = (out / "report.md").read_text()
    assert report.startswith("> **Simulation only.**")
    assert report.index("## Uncertainty ranges") < report.index("## Shadow prices") \
        < report.index("### Ranges")
    assert "### Totals" not in report


def test_scenarios_documents_carry_one_block_per_scenario(tmp_path):
    out = tmp_path / "out"
    assert run_mode("scenarios", out, *FAST) == 0
    doc = _doc("scenarios", out)
    names = [entry["name"] for entry in doc["uncertainty"]["scenarios"]]
    assert names == [row["name"] for row in doc["table"]] == [
        "no-change", "drop-mid", "half-tokens"]
    report = (out / "report.md").read_text()
    assert "## Uncertainty ranges" in report and report.count("### Ranges") == 3
    assert "RouteForge does not rank or recommend scenarios." in report


# --- flags ------------------------------------------------------------------------

@pytest.mark.parametrize("extra,needle", [
    (("--uncertainty", "--bootstrap", "0"), "replicates"),
    (("--uncertainty", "--bootstrap", "20001"), "replicates"),
    (("--uncertainty", "--interval", "1"), "interval"),
    (("--uncertainty", "--interval", "0"), "interval"),
    (("--uncertainty", "--interval", "nan"), "interval"),
    (("--uncertainty", "--seed", "-1"), "seed"),
    (("--uncertainty", "--min-rows", "0"), "min_rows"),
    (("--bootstrap", "100"), "--bootstrap given without --uncertainty"),
    (("--interval", "0.8"), "--interval given without --uncertainty"),
    (("--seed", "3"), "--seed given without --uncertainty"),
    (("--min-rows", "5"), "--min-rows given without --uncertainty"),
    (("--seed", "3", "--min-rows", "5"), "--seed, --min-rows given without --uncertainty"),
])
def test_bad_or_orphan_flags_exit_2_and_write_nothing(extra, needle, tmp_path, capsys):
    out = tmp_path / "out"
    assert run_mode("proposal", out, *extra) == 2
    assert needle in capsys.readouterr().err
    assert not out.exists()


def test_limits_are_accepted(tmp_path):
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--bootstrap", "1", "--interval",
                    "0.99", "--min-rows", "1", "--seed", "0") == 0
    block = _doc("proposal", out)["uncertainty"]
    assert "insufficient_data" not in block["by_app"]["few"]     # --min-rows 1
    assert block["points_outside_interval"] > 0                  # one replicate


def test_help_names_the_flags():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                          text=True, cwd=ROOT,
                          env={**__import__("os").environ,
                               "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    assert proc.returncode == 0
    for flag in ("--uncertainty", "--bootstrap", "--interval", "--seed", "--min-rows",
                 "--quality-posteriors"):
        assert flag in proc.stdout
    assert "Sampling variability only" in " ".join(proc.stdout.split())


# --- the gate runs before the bootstrap ---------------------------------------------

@pytest.mark.parametrize("mode", sorted(MODES))
def test_min_agreement_refuses_before_any_bootstrap(mode, tmp_path, monkeypatch, capsys):
    """The golden export's model agreement is far below 0.99. The run must
    exit 4, write nothing and never start a bootstrap -- in `--scenarios`
    mode too, where the gate otherwise runs after `compare`."""
    import app.eval.uncertainty as uncertainty_mod

    def boom(*_a, **_kw):
        raise AssertionError("bootstrap started before the --min-agreement gate")

    monkeypatch.setattr(uncertainty_mod, "run_bootstrap", boom)
    out = tmp_path / "out"
    assert run_mode(mode, out, *FAST, "--min-agreement", "0.99") == 4
    assert "below --min-agreement" in capsys.readouterr().err
    assert not out.exists()


# --- --quality-posteriors -------------------------------------------------------------

def _recommendation(tmp_path, model="local-cheap"):
    scoring = {"candidates": [{"name": model, "quality": {"v": 0.5, "src": "tier_default"}}]}
    records = [{"request_id": f"q{i}", "ts": "2026-09-20T00:00:00+00:00", "status": "ok",
                "endpoint_registry_version": "test", "workload": None, "chosen": model,
                "cache_hit": False, "scoring": scoring,
                "feedback": [{"score_name": "route_correct", "value": 1 if i < 36 else 0,
                              "source": "admin", "ts": "2026-09-20T00:00:00+00:00"}]}
               for i in range(40)]
    rec = build_recommendation(estimate(records, max_ci_width=1.0), export_sha256="e" * 64)
    path = tmp_path / "recommendation.json"
    path.write_text(dump_recommendation(rec), encoding="utf-8")
    return rec, path


def _run_with_snapshot(snapshot: dict, tmp_path, *extra) -> tuple[int, object]:
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(snapshot), encoding="utf-8")
    out = tmp_path / "out"
    code = load_cli().main(["--export", str(EXPORT), "--snapshot", str(snap),
                            "--proposal", str(PROPOSAL), "--out", str(out),
                            "--config-dir", str(ROOT / "config"), *extra])
    return code, out


def test_quality_posteriors_end_to_end(tmp_path, capsys):
    rec, path = _recommendation(tmp_path)
    snapshot = json.loads(SNAPSHOT.read_text())
    snapshot["quality"]["calibrated"] = [{"model": "local-cheap", "workload": None,
                                          "score": 0.82, "set_id": rec["set_id"]}]
    code, out = _run_with_snapshot(snapshot, tmp_path, *FAST, "--quality-posteriors", str(path))
    assert code == 0 and "warning" not in capsys.readouterr().err
    doc = json.loads((out / "simulation.json").read_text())
    assert doc["uncertainty"]["quality_posterior_sets"] == [rec["set_id"]]
    assert len(doc["inputs"]["uncertainty"]["quality_posteriors_sha256"]) == 1
    coverage = doc["uncertainty"]["quality_posterior"]["proposed"]
    assert coverage["posterior"] == coverage["calibrated"] > 0
    quality = doc["uncertainty"]["totals"]["quality_mean"]["proposed"]
    assert quality["hi"] - quality["lo"] > 0.05
    report = (out / "report.md").read_text()
    covered = doc["uncertainty"]["quality_posterior"]["current"]["posterior"]
    assert f"Quality uncertainty covers {covered} of" in report
    assert "the rest use a point prior." in report


def test_a_posterior_file_for_another_set_warns_and_adds_nothing(tmp_path, capsys):
    rec, path = _recommendation(tmp_path)
    out = tmp_path / "out"
    assert run_mode("proposal", out, *FAST, "--quality-posteriors", str(path)) == 0
    err = capsys.readouterr().err
    assert f"--quality-posteriors set {rec['set_id']} is not a calibration set" in err
    coverage = _doc("proposal", out)["uncertainty"]["quality_posterior"]
    assert coverage["current"]["posterior"] == coverage["proposed"]["posterior"] == 0


@pytest.mark.parametrize("text,needle", [("not json", "invalid JSON"),
                                         ('{"schema": "x"}', "schema")])
def test_a_bad_posterior_file_exits_2_naming_it(text, needle, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(text)
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--quality-posteriors", str(bad)) == 2
    err = capsys.readouterr().err
    assert needle in err and str(bad) in err and "Traceback" not in err
    assert not out.exists()


def test_a_missing_posterior_file_exits_2(tmp_path, capsys):
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--quality-posteriors",
                    str(tmp_path / "absent.json")) == 2
    assert "absent.json" in capsys.readouterr().err and not out.exists()


def test_quality_posteriors_without_uncertainty_exits_2(tmp_path, capsys):
    _rec, path = _recommendation(tmp_path)
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--quality-posteriors", str(path)) == 2
    assert "--quality-posteriors given without --uncertainty" in capsys.readouterr().err
````

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_cli.py -p no:warnings -rf
```

Expected: every test fails. Most fail with `SystemExit: 2` (argparse: `unrecognized arguments: --uncertainty`); `test_help_names_the_flags` fails on its first assertion about a flag.

- [ ] **Step 3: Add the flags and wire every mode**

Patch `scripts/policy_simulate.py`:

````diff
diff --git a/scripts/policy_simulate.py b/scripts/policy_simulate.py
index 6c7f48e..2894bf9 100644
--- a/scripts/policy_simulate.py
+++ b/scripts/policy_simulate.py
@@ -25,6 +25,12 @@ and does not model.
 File-based only: no DB, no `app.api`, no serving import
 (`tests/eval/test_isolation.py`).
 
+With `--uncertainty` (P3c-3) every figure also gets a range: a percentile
+interval from a bootstrap over the simulated rows, written under one new
+`uncertainty` key (and `shadow_prices.uncertainty`). It is sampling
+variability only -- see `app/eval/uncertainty.py::UNCERTAINTY_CAVEAT`.
+Without the flag every output is byte-identical to what it was before.
+
 Exit codes: 0 output written; 2 an input file failed to load, validate or
 apply, or the flags do not combine; 3 the proposal (or any scenario)
 introduces new rejections and `--fail-on-new-rejections` is set (for
@@ -64,6 +70,10 @@ from app.eval.simulate import (  # noqa: E402
     REPORT_CAVEAT, agreement_ok, baseline_check, parse_since, render_report, simulate,
     summarize,
 )
+from app.eval.uncertainty import (  # noqa: E402
+    MAX_REPLICATES, UncertaintyPlan, load_posteriors, plan_block, plan_inputs,
+    render_uncertainty_section, results_block, window_days,
+)
 
 
 def _parser() -> argparse.ArgumentParser:
@@ -98,9 +108,52 @@ def _parser() -> argparse.ArgumentParser:
                         "rates is compared: model agreement (first pick vs the recorded "
                         "first attempt) and outcome agreement (routed/rejected vs the "
                         "recorded status)")
+    p.add_argument("--uncertainty", action="store_true",
+                   help="add a range to every figure (P3c-3): a percentile interval from "
+                        "a bootstrap over the simulated rows, resampled within each "
+                        "application. Sampling variability only. Applies to every mode; "
+                        "off by default")
+    p.add_argument("--bootstrap", type=int, default=None, metavar="N",
+                   help=f"bootstrap resamples, 1..{MAX_REPLICATES} (default: 1000). "
+                        "Needs --uncertainty")
+    p.add_argument("--interval", type=float, default=None, metavar="P",
+                   help="interval level, strictly between 0 and 1 (default: 0.90). "
+                        "Needs --uncertainty")
+    p.add_argument("--seed", type=int, default=None, metavar="S",
+                   help="bootstrap seed, an integer >= 0 (default: 0). Needs --uncertainty")
+    p.add_argument("--min-rows", type=int, default=None, metavar="K",
+                   help="a bucket with fewer rows gets \"insufficient data\" instead of a "
+                        "range (default: 30). Needs --uncertainty")
+    p.add_argument("--quality-posteriors", action="append", default=None, metavar="FILE",
+                   help="a P2b-3 recommendation.json that was applied; repeatable. Its "
+                        "Beta posteriors add quality-label uncertainty for the calibrated "
+                        "qualities of that set. Needs --uncertainty")
     return p
 
 
+def _uncertainty_plan(args: argparse.Namespace) -> UncertaintyPlan | None:
+    """The `--uncertainty` plan, or None without the flag. Raises
+    `ValueError` (`UncertaintyError` is one) or `OSError` for a tuning flag
+    given without `--uncertainty`, a value out of range, or a posterior file
+    that does not load."""
+    tuning = (("--bootstrap", args.bootstrap), ("--interval", args.interval),
+              ("--seed", args.seed), ("--min-rows", args.min_rows),
+              ("--quality-posteriors", args.quality_posteriors))
+    if not args.uncertainty:
+        given = [flag for flag, value in tuning if value is not None]
+        if given:
+            raise ValueError(f"{', '.join(given)} given without --uncertainty")
+        return None
+    defaults = UncertaintyPlan()
+    posteriors, files = load_posteriors(args.quality_posteriors or [])
+    return UncertaintyPlan(
+        replicates=defaults.replicates if args.bootstrap is None else args.bootstrap,
+        interval=defaults.interval if args.interval is None else args.interval,
+        seed=defaults.seed if args.seed is None else args.seed,
+        min_rows=defaults.min_rows if args.min_rows is None else args.min_rows,
+        posteriors=posteriors, posterior_files=files)
+
+
 def _shown(rate: float | None) -> str:
     return "n/a" if rate is None else f"{rate:.6g}"
 
@@ -123,12 +176,19 @@ def _below_min_agreement(baseline: dict, minimum: float) -> int:
 
 def _compare_scenarios(args: argparse.Namespace, *, records: list[dict], warnings: list[str],
                        snapshot: dict, scenarios_doc: dict, inputs: dict, base,
-                       since) -> int:
+                       since, plan: UncertaintyPlan | None) -> int:
     """`--scenarios` mode (P3c-2): replay every scenario against one shared
     `current` arm, then write `comparison.json` and `report.md`."""
     try:
+        if plan is not None and args.min_agreement is not None:
+            # P3c-3: `compare` bootstraps every scenario before it returns the
+            # baseline the gate reads. One extra replay of the snapshot keeps
+            # a run the gate refuses from paying for those bootstraps.
+            early = asyncio.run(baseline_check(records, snapshot, base=base, since=since))
+            if not agreement_ok(early, args.min_agreement):
+                return _below_min_agreement(early, args.min_agreement)
         comparison = asyncio.run(compare(records, snapshot, scenarios_doc, base=base,
-                                         since=since))
+                                         since=since, uncertainty=plan))
     except (SnapshotError, ProposalError, ScenarioError) as exc:
         # All-or-nothing (spec §3): one invalid scenario fails the whole
         # file, named `scenarios[<i>](<name>).proposal.<path>`.
@@ -189,6 +249,12 @@ def main(argv: list[str] | None = None) -> int:
                   file=sys.stderr)
             return 2
 
+    try:
+        plan = _uncertainty_plan(args)
+    except (ValueError, OSError) as exc:
+        print(f"error: {exc}", file=sys.stderr)
+        return 2
+
     try:
         records, warnings = load_export(args.export)
     except (ValueError, OSError, json.JSONDecodeError) as exc:
@@ -223,6 +289,21 @@ def main(argv: list[str] | None = None) -> int:
         print(f"error: --config-dir {args.config_dir}: {exc}", file=sys.stderr)
         return 2
 
+    uncertainty_inputs = {}
+    if plan is not None:
+        # Only with the flag: without it `inputs`, and so the document's id,
+        # are exactly what they were before P3c-3.
+        uncertainty_inputs = {"uncertainty": plan_inputs(plan)}
+        # A posterior file for a set the snapshot does not use is probably
+        # the wrong file: it loads, matches nothing, and the report's coverage
+        # line will say 0 rows. Say so now.
+        applied = {entry["set_id"] for entry in snapshot["quality"]["calibrated"]}
+        for set_id, _sha in plan.posterior_files:
+            if set_id not in applied:
+                print(f"policy_simulate: warning: --quality-posteriors set {set_id} is not "
+                      "a calibration set in the snapshot; it adds no quality uncertainty",
+                      file=sys.stderr)
+
     # The deployment posture every app was compiled under -- the SNAPSHOT's
     # values, never this machine's local config.
     serving_policy = {k: snapshot["settings"]["serving_policy"][k]
@@ -230,7 +311,7 @@ def main(argv: list[str] | None = None) -> int:
     if args.scenarios:
         return _compare_scenarios(
             args, records=records, warnings=warnings, snapshot=snapshot,
-            scenarios_doc=scenarios_doc, base=base, since=since,
+            scenarios_doc=scenarios_doc, base=base, since=since, plan=plan,
             inputs={
                 "export_sha256": export_sha256,
                 "snapshot_sha256": snapshot_sha256,
@@ -241,6 +322,7 @@ def main(argv: list[str] | None = None) -> int:
                 "snapshot_generated_at": snapshot["generated_at"],
                 "serving_policy": serving_policy,
                 "since": since.isoformat() if since is not None else None,
+                **uncertainty_inputs,
             })
 
     inputs = {
@@ -253,6 +335,7 @@ def main(argv: list[str] | None = None) -> int:
         "snapshot_generated_at": snapshot["generated_at"],
         "serving_policy": serving_policy,
         "since": since.isoformat() if since is not None else None,
+        **uncertainty_inputs,
     }
 
     try:
@@ -273,6 +356,15 @@ def main(argv: list[str] | None = None) -> int:
         if args.min_agreement is not None and not agreement_ok(baseline, args.min_agreement):
             return _below_min_agreement(baseline, args.min_agreement)
 
+        # P3c-3: after the gate, so a refused run never pays for a bootstrap.
+        # `results` are the rows `summarize` just read; nothing is re-simulated.
+        uncertainty_block = None
+        if plan is not None:
+            uncertainty_block = plan_block(plan)
+            if args.proposal:
+                uncertainty_block.update(results_block(
+                    results, plan, days=window_days(sim["inputs"]["window"])))
+
         shadow_block = None
         if args.shadow_prices:
             # P3c-1: the CALLER applies any proposal first -- shadow_prices
@@ -286,7 +378,7 @@ def main(argv: list[str] | None = None) -> int:
                 baseline_label = "snapshot"
             shadow_block = asyncio.run(shadow_prices(
                 records, baseline_doc, base=base, cap=args.max_relaxations,
-                baseline_label=baseline_label, since=since))
+                baseline_label=baseline_label, since=since, uncertainty=plan))
     except (SnapshotError, ProposalError) as exc:
         # Spec §8 governs over the narrower "catch ProposalError" reading:
         # exit 2 is "an input file failed to load OR VALIDATE" -- a
@@ -310,6 +402,9 @@ def main(argv: list[str] | None = None) -> int:
 
     if args.proposal:
         report_text = render_report(sim)
+        if uncertainty_block is not None:
+            sim["uncertainty"] = uncertainty_block
+            report_text += "\n" + render_uncertainty_section(uncertainty_block)
         if shadow_block is not None:
             sim["shadow_prices"] = shadow_block
             report_text += "\n" + render_shadow_section(shadow_block)
@@ -324,7 +419,11 @@ def main(argv: list[str] | None = None) -> int:
         sim = {"schema": "routeforge.simulation/v1", "inputs": inputs,
               "shadow_prices": shadow_block}
         caveat = "\n".join("> " + line for line in REPORT_CAVEAT.split("\n"))
-        report_text = caveat + "\n\n" + render_shadow_section(shadow_block)
+        report_text = caveat + "\n\n"
+        if uncertainty_block is not None:
+            sim["uncertainty"] = uncertainty_block
+            report_text += render_uncertainty_section(uncertainty_block) + "\n"
+        report_text += render_shadow_section(shadow_block)
         newly_rejected = 0
         print(f"policy_simulate: shadow prices only "
               f"({len(warnings)} export warnings) -> {out}")
````

- [ ] **Step 4: Run the tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_cli.py tests/eval/test_uncertainty_golden.py tests/eval/test_policy_simulate_cli.py -p no:warnings -rf
```

Expected: `105 passed`. About three minutes: `test_policy_simulate_cli.py` runs the CLI in subprocesses.

- [ ] **Step 5: Look at one real report**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run python scripts/policy_simulate.py \
  --export tests/eval/fixtures/uncertainty_golden/inputs/export.ndjson \
  --snapshot tests/eval/fixtures/uncertainty_golden/inputs/snapshot.json \
  --scenarios tests/eval/fixtures/uncertainty_golden/inputs/scenarios.json \
  --out /tmp/p3c3_cmp --uncertainty --bootstrap 200
sed -n '/^## Comparison/,/^## Assumptions/p' /tmp/p3c3_cmp/report.md
```

Expected: exit `0`. The table has three rows in the order `no-change`, `drop-mid`, `half-tokens`. Every bracket in the `no-change` row is `[0, 0]`. The `drop-mid` row's "Δcost USD" cell reads `-0.02895 [-0.034024, -0.023268]` (200 replicates, seed 0). Below the table: the two existing notes, then `## Uncertainty ranges` with the seven-line caveat.

- [ ] **Step 6: Run the offline suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -rf
```

Expected: `1011 passed`, no `FAILED` line.

- [ ] **Step 7: Commit**

```bash
git add scripts/policy_simulate.py tests/eval/test_uncertainty_cli.py
git commit -m "$(cat <<'EOF'
feat(eval): policy_simulate.py --uncertainty (P3c-3)

Opt-in ranges in every mode. The plan enters inputs, so the document id
tells two plans apart; without the flag inputs and outputs are exactly
what they were. Tuning flags without --uncertainty are refused, and the
--min-agreement gate still runs before any bootstrap.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Documentation (spec §14)

**Files:**
- Modify: `docs/reference/policy-simulator.md`, `docs/research/feasible-route-bench.md`, `docs/research/arxiv.md`, `docs/roadmap/priorities-1-3-status.md`

**Interfaces:**
- Consumes: the names and behaviour Tasks 1–7 built. Every test name these documents cite must exist; Step 5 checks that.
- Produces: no code.

**Rules.**

- **Never invent a number.** The only figures in research §20.6 and in `arxiv.md` §4.2 are the synthetic timing measured while this plan was validated (100,000 rows × 1000 replicates in 25.2 s, and 27.5 s on a second run; 1,000,000 rows × 50 in 36.0 s; Python 3.12.3, one core of an Intel Core Ultra 7 165U) and two assertions the test suite makes. They are labelled synthetic. Every measurement on recorded traffic is written **TO MEASURE**. If Task 9's timing on your machine differs, do not edit these numbers: they describe the validation machine, and the documents say which one.
- The docs gate (`scripts/check_docs_links.py`, run by `tests/test_docs_links.py`) refuses the purged identifier for the old bundled test model in any file under `docs/` outside `docs/archive/` and `docs/superpowers/`. The patches below do not use it; do not add it.
- The roadmap row's PR column says `pending`. The pull request does not exist yet. Whoever opens it replaces `pending` with its number in a follow-up docs commit. Leave the "As of" line at the top of that file alone until then.
- Apply each patch with `git apply`. If one does not apply because a file moved under you, make the same edit by hand and keep the wording.

- [ ] **Step 1: `docs/reference/policy-simulator.md`** — the header pointers, the usage line and two flag rows and the exit-2 list in §3, `inputs` and a new `uncertainty` row in §4.1, a sentence in §4.2, the out-of-scope sentence in §6, one bullet each in §8.7 and §9.6, and the new §10.

Patch `docs/reference/policy-simulator.md`:

````diff
diff --git a/docs/reference/policy-simulator.md b/docs/reference/policy-simulator.md
index e2fb0cb..a39485a 100644
--- a/docs/reference/policy-simulator.md
+++ b/docs/reference/policy-simulator.md
@@ -15,7 +15,8 @@ human still makes the change through the normal admin path (`PUT`ing app policy,
 removing an endpoint, editing prices).
 
 - Design: `docs/superpowers/specs/2026-09-24-policy-simulator-design.md`. Shadow prices (§8)
-  are `docs/superpowers/specs/2026-09-26-shadow-prices-design.md` (P3c-1).
+  are `docs/superpowers/specs/2026-09-26-shadow-prices-design.md` (P3c-1). Uncertainty ranges
+  (§10) are `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md` (P3c-3).
 - Research notes (the model, the two-arm design, the invariants):
   `docs/research/feasible-route-bench.md` §15; shadow prices §17.
 - Prerequisite datasets: `docs/reference/routing-outcomes.md` (P2b-2, the export this reads)
@@ -24,7 +25,8 @@ removing an endpoint, editing prices).
 Code: `app/apps/snapshot.py` (snapshot builder), `app/api/admin_routes.py`
 (`GET /admin/config-snapshot`), `app/eval/scenario.py` (snapshot/proposal → scenario),
 `app/eval/proposal.py` (proposal load/validate/apply), `app/eval/simulate.py` (replay +
-report), `app/eval/shadow.py` (shadow prices, P3c-1), `scripts/policy_simulate.py` (CLI).
+report), `app/eval/shadow.py` (shadow prices, P3c-1), `app/eval/uncertainty.py` (ranges,
+P3c-3), `scripts/policy_simulate.py` (CLI).
 
 ## 1. Three inputs
 
@@ -228,7 +230,9 @@ assumptions into the `current` arm.
 uv run python scripts/policy_simulate.py \
   --export outcomes.ndjson --snapshot snapshot.json --proposal proposal.json \
   --out results/sim \
-  [--config-dir config] [--fail-on-new-rejections]
+  [--config-dir config] [--fail-on-new-rejections] \
+  [--uncertainty [--bootstrap N] [--interval P] [--seed S] [--min-rows K] \
+                 [--quality-posteriors FILE ...]]
 ```
 
 | Flag | Default | Meaning |
@@ -244,6 +248,8 @@ uv run python scripts/policy_simulate.py \
 | `--scenarios` | off | a `routeforge.policy-scenarios/v1` file; compares its scenarios side by side (§9). Cannot be combined with `--proposal` or `--shadow-prices` (exit `2`) |
 | `--since` | off | ISO-8601 UTC timestamp. Rows with `ts` before it are excluded and counted as `before_since`. Applies to every mode. A value with no offset is read as UTC |
 | `--min-agreement` | off | `0 ≤ X ≤ 1`. Compared with the **lower** of the two baseline agreements (§5): model agreement and outcome agreement. If either is not computable, or the lower one is below `X`, print both and exit `4` before any file or directory is written. Applies to every mode |
+| `--uncertainty` | off | add a range to every headline figure (P3c-3, §10): a percentile interval from a bootstrap over the simulated rows. Sampling variability only. Applies to every mode |
+| `--bootstrap`, `--interval`, `--seed`, `--min-rows`, `--quality-posteriors` | `1000`, `0.90`, `0`, `30`, none | tuning for `--uncertainty` (§10.3); each is exit `2` without it |
 
 `--proposal` is required unless `--shadow-prices` or `--scenarios` is given — with none of
 the three, the CLI exits `2` (an argparse usage error, since there is nothing to simulate).
@@ -253,7 +259,7 @@ the three, the CLI exits `2` (an argparse usage error, since there is nothing to
 | Code | Meaning |
 |---|---|
 | `0` | `simulation.json` and `report.md` written (`--proposal`, or `--shadow-prices` alone); `comparison.json` and `report.md` written (`--scenarios`, §9.4) |
-| `2` | an input failed to load, validate or apply — a bad export, an invalid proposal (unknown key, bad value, a container of the wrong type), a malformed snapshot (a missing or mistyped nested key, named by its dotted path), a snapshot that loads structurally but fails **deep** validation during `simulate()`/`shadow_prices()` (for example a provider the running deployment's own `config/` never declared), a `--config-dir` that cannot load (missing key, bad YAML, a failed config lint), neither `--proposal` nor `--shadow-prices` given, `--max-relaxations < 1`, or a shadow-prices eligible-superset invariant breach (`ShadowInvariantError`, printed as `error: internal invariant: ...` — spec §3; a relaxation narrowed some row's eligible set, which should never happen since every relaxation only widens eligibility, so it means a bug, not a reportable finding). A relaxation that newly **rejects** a baseline-routed row is *not* a breach — it is reported as `newly_rejected` (§8.4) and exits `0`. `--scenarios` combined with `--proposal` or `--shadow-prices` (an argparse usage error), neither `--proposal` nor `--shadow-prices` nor `--scenarios` given (also an argparse usage error), an invalid scenarios file (named `scenarios[<i>](<name>).proposal.<path>`), a `--since` that is not a timestamp, a `--min-agreement` outside `[0, 1]`, and a `--scenarios` comparison invariant breach (`ComparisonInvariantError`, printed as `error: internal invariant: ...` — two scenarios disagreed about the rows, excluded counts or baseline they share by construction, which means a bug, never a reportable result) are also exit `2`. Nothing is written; the offending location is on stderr — never a traceback. |
+| `2` | an input failed to load, validate or apply — a bad export, an invalid proposal (unknown key, bad value, a container of the wrong type), a malformed snapshot (a missing or mistyped nested key, named by its dotted path), a snapshot that loads structurally but fails **deep** validation during `simulate()`/`shadow_prices()` (for example a provider the running deployment's own `config/` never declared), a `--config-dir` that cannot load (missing key, bad YAML, a failed config lint), neither `--proposal` nor `--shadow-prices` given, `--max-relaxations < 1`, or a shadow-prices eligible-superset invariant breach (`ShadowInvariantError`, printed as `error: internal invariant: ...` — spec §3; a relaxation narrowed some row's eligible set, which should never happen since every relaxation only widens eligibility, so it means a bug, not a reportable finding). A relaxation that newly **rejects** a baseline-routed row is *not* a breach — it is reported as `newly_rejected` (§8.4) and exits `0`. `--scenarios` combined with `--proposal` or `--shadow-prices` (an argparse usage error), neither `--proposal` nor `--shadow-prices` nor `--scenarios` given (also an argparse usage error), an invalid scenarios file (named `scenarios[<i>](<name>).proposal.<path>`), a `--since` that is not a timestamp, a `--min-agreement` outside `[0, 1]`, and a `--scenarios` comparison invariant breach (`ComparisonInvariantError`, printed as `error: internal invariant: ...` — two scenarios disagreed about the rows, excluded counts or baseline they share by construction, which means a bug, never a reportable result) are also exit `2`. So are an `--uncertainty` tuning flag given without `--uncertainty`, a tuning value out of range, and a `--quality-posteriors` file that does not load or does not match its own `set_id` (§10.3). Nothing is written; the offending location is on stderr — never a traceback. |
 | `3` | the proposal (or any scenario) introduces new rejections and `--fail-on-new-rejections` was passed |
 | `4` | the lower of model agreement and outcome agreement (§5) is below `--min-agreement`, or there is no comparable row. Nothing is written. The message names both rates and both outcome-disagreement counts |
 
@@ -275,7 +281,7 @@ the export window).
 |---|---|
 | `schema` | `"routeforge.simulation/v1"` |
 | `simulation_id` | first 16 hex characters of the sha256 over the canonical `inputs` block |
-| `inputs` | `export_sha256`, `snapshot_sha256`, `proposal_sha256`, `proposal_title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy` (the four posture values every app was compiled under — the snapshot's), `window` (`{start, end}` over the simulated rows' `ts`, or `null`), `since` (the `--since` cut as an ISO string, or `null`) |
+| `inputs` | `export_sha256`, `snapshot_sha256`, `proposal_sha256`, `proposal_title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy` (the four posture values every app was compiled under — the snapshot's), `window` (`{start, end}` over the simulated rows' `ts`, or `null`), `since` (the `--since` cut as an ISO string, or `null`). With `--uncertainty` only: `uncertainty` (§10.7); the key is absent otherwise |
 | `edits` | the proposal, minus `schema`/`title`, keys sorted — exactly what was asked for |
 | `rows` | `simulated` (count), `estimated_tokens` (simulated rows with no backend token count, sized and priced from `routing_tokens` — §6) and `excluded` (count per reason, first match wins: `before_since`, `cache_hit`, `unknown_status`, `unknown_app`, `missing_features`, `missing_routing_tokens`). `missing_routing_tokens` counts a well-formed row that records no size: not `ok`, no `routing_tokens`, and `prompt_tokens` zero — a rejected or failed row from before B60. A mistyped `routing_tokens` (anything but an integer ≥ 0 or null) is `missing_features`. `missing_features` counts a row whose `request_id`, `complexity`, `prompt_tokens` (numeric), `completion_tokens` (numeric or null), `detected_entity_types` (list of strings or null; the key must be present), `workload`, `attempts` or `ts` is missing or mistyped, or whose `requested_model` (non-empty string or null) or `max_tokens` (integer > 0 or null) is mistyped — these two may be absent (an older export) — a malformed row is counted, never a crash. A non-object export line is a skipped-line warning. |
 | `baseline` | the fidelity check (§5). Model agreement: `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`, `overflow_contradicted`. Outcome agreement: `outcome_rows_compared`, `outcome_agreement`, `outcome_warning`, `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected`, and `outcome_by_app.<id>` (`rows`, `agreement` and the same three counts) |
@@ -286,6 +292,7 @@ the export window).
 | `sources` | per arm, a count of `latency_source`/`quality_source` values (`observed`/`signal`/`tier_default`/`proposal`, `published`/`tier_default`/`calibrated`/`customer`) across routed rows |
 | `per_day` | `{cost_usd: {current, proposed, delta}}` divided by the export window in days; `null` when the window is under 1 day |
 | `assumptions` | always present: `token_scale`, `cached_share`, `latency_ms` (each a map, empty when the proposal sets none) and `saturated_complexity_estimated` (rows whose scaled complexity is an estimate) |
+| `uncertainty` | only with `--uncertainty`: a range for every figure of `totals`, `by_app` and `per_day` (§10.7). No other field changes when it is present |
 
 ### 4.2 `report.md`
 
@@ -305,7 +312,8 @@ metrics table), **New rejections** (by app, by code, then **by conflicting core*
 `| core | count |` table, sorted by count descending then core key — or "None." when there
 are none), **Moves** (from → to → count — or "None."), **By app** (one subsection per app,
 same metrics table), **By workload** (same, per workload label), **Latency and quality
-sources**, **Excluded rows** (reason → count).
+sources**, **Excluded rows** (reason → count). With `--uncertainty`, an **Uncertainty ranges**
+section follows (§10.8); everything before it is unchanged.
 
 **Read the two cost rows together.** `cost_usd` sums each arm over the rows *that arm*
 routed, so a row the proposal newly rejects simply disappears from the proposed sum — it
@@ -399,8 +407,9 @@ From spec §2, plus what the build settled during P3a:
 - **No explanation of why a proposal rejects a request** and **no shadow prices** were P3a's
   limits. The minimal conflicting core (P3b) now explains a rejection — see
   `docs/reference/conflicting-core.md`. Shadow prices (P3c-1, §8 below) now price what each
-  individual constraint costs. Alternative scenarios are §9 (P3c-2). **A capacity model and
-  uncertainty ranges** remain out of scope (P3c-3, P3c-4).
+  individual constraint costs. Alternative scenarios are §9 (P3c-2). Uncertainty ranges are
+  §10 (P3c-3): sampling variability only, and none of the limits in this section is inside
+  them. **A capacity model** remains out of scope (P3c-4).
 - **Two token counts, used for two things (B60, fixed 2026-10-06).** The router is given the
   row's `routing_tokens` — RouteForge's own count, the number the live router was given — so
   the per-request budget check and the cost term of the score see what they saw live. The
@@ -704,8 +713,9 @@ From spec §2, deliberate non-goals for this sub-project:
 - **One constraint at a time.** Every relaxation is evaluated independently, against the
   same baseline. It says nothing about relaxing two constraints together — that interaction
   (joint relaxations) is P3c-2, scenarios.
-- **No uncertainty.** Every figure is a point estimate from replaying the recorded rows
-  once; there is no confidence interval or sensitivity range. That is P3c-3.
+- **A point estimate unless asked.** Every figure is a point estimate from replaying the
+  recorded rows once. `--uncertainty` adds a range for each binding relaxation's cost and
+  counts (§10); it is sampling variability only, not a sensitivity analysis.
 - **No capacity effects.** A relaxation that would route far more traffic onto one endpoint
   is priced as if that endpoint has unlimited headroom — there is no queueing, saturation
   or capacity model. That is P3c-4.
@@ -803,8 +813,199 @@ excluded rows.
 
 ### 9.6 Limits
 
-- No uncertainty ranges (P3c-3) and no capacity or overload model (P3c-4).
+- No capacity or overload model (P3c-4). Ranges are opt-in (`--uncertainty`, §10) and are
+  sampling variability only.
 - `--scenarios` cannot be combined with `--shadow-prices`.
 - `token_scale` on a saturated row and every `cached_share` are assumptions; the report
   labels both.
 - Every §6 limit applies to every scenario.
+
+## 10. Uncertainty ranges (P3c-3)
+
+Design: `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md`. Research notes:
+`docs/research/feasible-route-bench.md` §20. Code: `app/eval/uncertainty.py`
+(`UncertaintyPlan`, `run_bootstrap`, `results_block`, `load_posteriors`,
+`render_uncertainty_section`), wired into `app/eval/scenarios.py`, `app/eval/shadow.py` and
+`scripts/policy_simulate.py`.
+
+### 10.1 What it answers
+
+Every figure in §4, §8 and §9 is a point estimate from one replay of one export. "Cost −39%"
+reads the same on 24 rows and on 2 million. `--uncertainty` puts a range beside each headline
+figure, so a reader can see how much the figure would move if the same traffic had produced a
+different sample of the same size.
+
+It is **off by default**. Without the flag every output file is byte-identical to what it was
+before this feature (pinned by `tests/eval/test_uncertainty_golden.py`). With it, the
+documents gain keys and no existing key or value changes.
+
+### 10.2 What the range is — and is not
+
+Every report that shows a range prints this, verbatim:
+
+> **Ranges show sampling variability only.** Each range is a percentile interval from
+> resampling the recorded rows with replacement, within each application, under this replay
+> model. It does not include replay model error (first-decision replay, frozen signals,
+> predicted overflow), the error of the admission estimate for rows no backend answered,
+> inputs that were not recorded, or future traffic shift. Replay gives each endpoint one
+> latency, so a latency range reflects only which endpoints the resampled rows used. A narrow
+> range on a biased replay is still biased.
+
+The block's `not_covered` list carries the same five exclusions as ids:
+`replay_model_error`, `admission_estimate_error`, `unrecorded_inputs`, `traffic_shift`,
+`per_request_latency`. Check the baseline agreements (§5) for bias; the range says nothing
+about it.
+
+### 10.3 Running it
+
+```bash
+uv run python scripts/policy_simulate.py \
+  --export outcomes.ndjson --snapshot snapshot.json --proposal proposal.json \
+  --out results/sim --uncertainty \
+  [--bootstrap 1000] [--interval 0.90] [--seed 0] [--min-rows 30] \
+  [--quality-posteriors recommendation.json ...]
+```
+
+The same flags work with `--scenarios` and with `--shadow-prices`.
+
+| Flag | Default | Valid | Meaning |
+|---|---|---|---|
+| `--uncertainty` | off | | compute and write ranges |
+| `--bootstrap` | `1000` | integer, 1 to 20000 | bootstrap resamples |
+| `--interval` | `0.90` | strictly between 0 and 1 | level of the interval |
+| `--seed` | `0` | integer ≥ 0 | seed; the same seed gives the same ranges |
+| `--min-rows` | `30` | integer ≥ 1 | a bucket with fewer rows gets "insufficient data" (§10.6) |
+| `--quality-posteriors` | none | repeatable | a P2b-3 `recommendation.json` that was applied (§10.5) |
+
+A tuning flag given without `--uncertainty`, a value out of range, or a posterior file that
+does not load is exit `2`; nothing is written. `--min-agreement` is checked before any
+resampling starts, in every mode.
+
+### 10.4 The method
+
+- **Rows are resampled, never re-simulated.** Each row is replayed once, as always. A
+  resample draws row positions with replacement and recomputes the figures.
+- **Within each application.** An app with `n` rows is resampled to exactly `n` rows from its
+  own rows, so a per-app figure is defined in every resample and the mix of traffic between
+  apps is held at what was recorded.
+- **Paired.** The rows drawn for an app in resample `b` depend only on the seed, `b`, the app
+  and its row count. Both arms, every scenario and every shadow-price relaxation therefore see
+  the same resampled rows, and a delta's range is the range of the difference over the same
+  rows. A proposal that changes nothing has every delta range at exactly `[0, 0]`.
+- **Percentile interval.** With `N` resamples and level `P`, `lo` and `hi` are the
+  `floor((1 − P)/2 · N) + 1`-th smallest and largest values.
+
+It assumes the rows of one application are exchangeable and independent over the export
+window. Neither is true of real traffic: the calls of one agent loop are related to each
+other, and traffic shifts within a window. Both make the true uncertainty larger than the
+range shown. There is no day-block resampling.
+
+### 10.5 Quality labels (`--quality-posteriors`)
+
+A quality that came from P2b-3 calibration (`docs/reference/quality-calibration.md`) is the
+mean of a Beta posterior. The snapshot carries the mean and the `set_id`, not the Beta's
+parameters. They are in the `recommendation.json` the set was applied from, so pass that
+file:
+
+```bash
+--uncertainty --quality-posteriors results/quality/recommendation.json
+```
+
+- The file must match its own `set_id` (a hash of its content — the check the admin API ran
+  at apply time). An edited file is refused.
+- In each resample, each calibrated `(model, workload)` unit of that set gets one quality
+  drawn from its posterior, used for every row that resolves to that unit, in both arms.
+- A published or tier-default quality has no stated uncertainty and gets none: it stays a
+  point.
+- The report says how much is covered: "Quality uncertainty covers k of n routed rows …; the
+  rest use a point prior." `uncertainty.quality_posterior` has the same counts per arm
+  (`routed`, `calibrated`, `posterior`).
+- A file for a set the snapshot does not hold is ignored with a warning on stderr.
+
+### 10.6 Insufficient data, zero counts, flags on a leaf
+
+- **`insufficient data (n = k)`.** A bucket with fewer than `--min-rows` rows has no range:
+  the JSON carries `{"insufficient_data": true, "n": k}`. That applies to an app, to the
+  totals, to a scenario and to a shadow-price relaxation's rows in scope.
+- **A zero count is not "no risk".** If no row is newly rejected, no resample has one: the
+  range is `[0, 0]` for 24 rows and for 2 million. So the share's leaf also carries
+  `zero_bound = 1 − (1 − P)^(1/n)` — the largest rate at which `n` independent rows would
+  still show none with probability `1 − P` — and the report prints it in a sentence. It is
+  about `2.3 / n` at the default level (9.2% for 24 rows).
+- **`point_outside: true`.** A percentile interval does not promise to contain the point
+  estimate. When it does not, the leaf says so and the report marks the row. Nothing is
+  widened.
+- **`undefined_replicates: k`.** A figure that does not exist in some resample (an arm with
+  no routed row there) has no range: `lo` and `hi` are null.
+
+### 10.7 Output
+
+The added keys, and the only ones: `uncertainty`, `inputs.uncertainty`, and — with shadow
+prices — `shadow_prices.uncertainty`.
+
+**A leaf** is `{"point", "lo", "hi"}`, plus the flags of §10.6 when they apply. `point` is the
+figure the document already carries at the same path.
+
+**`inputs.uncertainty`** is `{replicates, interval, seed, min_rows,
+quality_posteriors_sha256}`, so `simulation_id` / `comparison_id` differ between two runs that
+differ in any of them, and between a run with the flag and one without.
+
+**`simulation.json` → `uncertainty`:**
+
+| Field | Contents |
+|---|---|
+| `method`, `strata` | `"stratified_paired_row_bootstrap"`, `"application_id"` |
+| `replicates`, `interval`, `seed`, `min_rows` | the plan |
+| `quality_posterior_sets` | the `set_id` of each posterior file |
+| `not_covered` | the five ids of §10.2 |
+| `totals`, `by_app.<id>` | a bucket: `n` and one leaf per figure — `cost_usd` and `cost_usd_both_routed` (`current`, `proposed`, `delta`), `route_changed`, `newly_rejected`, `newly_served` and each one's `_share`, `latency_ms.p50`/`.p95` (`current`, `proposed`, `delta`), `quality_mean` (same), `onprem_share` (`current`, `proposed`) — or `{insufficient_data, n}` |
+| `per_day` | `cost_usd` (`current`, `proposed`, `delta`) and `cost_usd_both_routed.delta`, each a leaf; `null` when the window is under a day |
+| `quality_posterior` | per arm: `routed`, `calibrated`, `posterior` (§10.5) |
+| `points_outside_interval` | how many leaves carry `point_outside` |
+
+With `--shadow-prices` and no `--proposal` the block holds only its first four rows.
+`by_workload` and `overflow_hops` have no range.
+
+**`shadow_prices.uncertainty`** is `{interval, constraints}`. `constraints` is parallel to
+`shadow_prices.constraints` (same order); each entry has `requirement`, `scope`, `security`,
+`n` and leaves for `route_changed`, `rejections_avoided`, `newly_rejected`,
+`cost_usd_both_routed_delta` and `cost_saved_per_day` (`null` under a one-day window), or
+`insufficient_data`.
+
+**`comparison.json` → `uncertainty`** has the same head and `scenarios`, in file order: each
+entry is `name` plus the `simulation.json` fields above for that scenario, plus `table` — the
+leaves behind that scenario's comparison-table row, under the table's own column keys.
+
+### 10.8 Reports
+
+- **Single proposal.** The report is unchanged, followed by an **Uncertainty ranges** section:
+  the caveat, one sentence on the method, the quality coverage, then a `figure | point |
+  range` table for the totals, the per-day figures and each app.
+- **Scenario comparison.** In the comparison table each figure is followed by its range,
+  `-0.02895 [-0.034024, -0.023268]`. An **Uncertainty ranges** section follows the table, and
+  each scenario's section gains a **Ranges** table. The table stays in file order. A range is
+  not a ranking: overlapping ranges do not make two scenarios equal, and separate ranges do
+  not make one of them the one to choose.
+- **Shadow prices.** A **Ranges** table follows the two existing tables. A security
+  constraint's row keeps its `price of a protection — not a recommendation` label there too.
+
+### 10.9 Cost
+
+Pure Python, proportional to resamples × rows. Measured on synthetic rows (Python 3.12, one
+core of a laptop CPU): 100,000 rows × 1000 resamples in about 25 seconds; 1,000,000 rows at
+about 0.7 seconds per resample, so about 12 minutes at the default. Each scenario and each
+binding relaxation is resampled separately. For a very large export lower `--bootstrap` or cut
+the export with `--since`.
+
+### 10.10 Limits
+
+- Everything in §10.2 and §10.4. In particular the range is too narrow when rows are
+  correlated, and it knows nothing about a traffic shift.
+- Thirty rows is a convention for `--min-rows`, not a guarantee that a range is reliable at
+  31.
+- No ranges per workload, on `overflow_hops`, on the baseline agreements, or on non-binding
+  relaxations.
+- Quality-label uncertainty needs the recommendation file; it is not read from the snapshot.
+- Results are reproducible on the same Python minor version; the standard library's random
+  streams are not guaranteed identical across versions.
+- No capacity or overload model (P3c-4).
````

- [ ] **Step 2: `docs/research/feasible-route-bench.md`** — the new §20, appended.

Patch `docs/research/feasible-route-bench.md`:

````diff
diff --git a/docs/research/feasible-route-bench.md b/docs/research/feasible-route-bench.md
index 6491965..3854696 100644
--- a/docs/research/feasible-route-bench.md
+++ b/docs/research/feasible-route-bench.md
@@ -2705,3 +2705,190 @@ classes are exercised only by the test suite.
 - Nothing here replays failover, escalation, breaker state or daily-spend and RPM admission.
   A request refused for its daily budget or its rate limit writes no outcome row at all.
 - A `backend_failed` row is costed as if the call had succeeded.
+
+## 20. Uncertainty ranges: a paired, stratified bootstrap over replayed rows (P3c-3)
+
+Every figure in §15–§19 is a point estimate from one replay of one export. §19.4.1 reports
+"cost −39.0%" from 24 rows; nothing on the page distinguishes that from the same number on
+two million. This section records the range that now goes beside each figure, and what it
+does and does not mean. Design: `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md`.
+Reference: [`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §10. Code:
+`app/eval/uncertainty.py`. **Not deployed anywhere and not run on real records**: the only
+numbers below are synthetic, and every measurement on recorded traffic is to be made (§20.6).
+
+### 20.1 The model
+
+Let the export hold rows `r_1 … r_n`, partitioned by application into strata `S_a` of size
+`n_a`. Replay is a deterministic map `r ↦ (c(r), p(r))`: the row's result under the current
+config and under the proposal. A figure is a statistic `T` of the multiset
+`{(c(r), p(r))}` — a sum (cost), a count, a ratio of counts (a share, the on-prem share), a
+ratio of sums (the quality mean), a quantile (latency p50, p95), or a difference of two of
+these between the arms.
+
+A bootstrap replicate `b` draws, for each stratum, `n_a` positions with replacement from
+`S_a`, and evaluates `T` on the union. The reported range is the equal-tailed percentile
+interval of `T` over `N` replicates.
+
+- **Rows are resampled, not re-simulated.** `(c(r), p(r))` is computed once per row. A
+  replicate costs a draw and a sum, not a routing decision.
+- **Stratified.** Each `n_a` is fixed, so a per-app figure is defined in every replicate and
+  the between-app mix is conditioned on.
+- **Paired.** A drawn row contributes `c(r)` and `p(r)` together. So `T_p − T_c` in a
+  replicate is a difference over the same rows, and its range is the range of the paired
+  difference. If `c(r) = p(r)` for every row, the difference is exactly `0` in every
+  replicate.
+- **Common random numbers across counterfactuals.** The positions drawn for stratum `a` in
+  replicate `b` are a function of `(seed, b, a, n_a)` alone. Every scenario of a comparison,
+  and every shadow-price relaxation, is replayed over the same rows in the same order, so all
+  of them are evaluated on the same replicates without any of them being held in memory with
+  another. Two consequences that are pinned by tests: a scenario's ranges do not depend on
+  which other scenarios are in the file, and a relaxation's range equals the range of the
+  proposal that makes the same edit.
+
+**What it estimates.** The sampling variability of `T` if the rows of each application are
+exchangeable, independent draws from a fixed distribution. Neither holds for LLM traffic. An
+agent loop sends a sequence of related calls, each its own row; traffic changes with releases
+and time of day. Both make the true variability larger than the range. The design does not
+estimate by how much (§20.5).
+
+### 20.2 What it deliberately is not
+
+The range is conditional on the replay being right. Four things are outside it, named in
+every report (`UNCERTAINTY_CAVEAT`) and in the output (`not_covered`):
+
+| Outside the range | Evidence that it matters here |
+|---|---|
+| replay model error — first decision only, frozen signals, predicted overflow | §15.2, §18.2 |
+| the admission estimate for rows no backend answered | §19.4.1: RouteForge's count was 1.54× the backend's on 17 served rows, and the seven rescued rows are priced from it |
+| inputs the row does not record | B55 (§15.4) and B60 (§19): each was invisible until a figure was plainly wrong |
+| traffic shift | not measured |
+
+A fifth follows from the replay itself: a routed row is given the latency of its endpoint
+(asserted, observed median, signal or tier default), so latency has as many distinct values
+as there are endpoints. The p50 and p95 are computed exactly in every replicate, and their
+range reflects only which endpoints the resampled rows used. On a one-endpoint deployment a
+latency range is a single value. That is a correct statement about the replay, and an
+uninformative one about latency.
+
+The point worth making in the paper: **a narrow range on a biased replay is still biased.**
+The range and the baseline agreement answer different questions, and a report needs both.
+
+### 20.3 Quality labels
+
+A calibrated quality (§14) is the mean of a `Beta(α, β)` posterior. Using the mean as a point
+discards the one stated uncertainty the system has about quality.
+
+- The snapshot exports the mean and the `set_id`, not `α` and `β`; nor does the database hold
+  them. They are in the P2b-3 recommendation file, whose `set_id` is a hash of its own content
+  — the hash the admin API verified before applying the set. So the file authenticates itself
+  against the snapshot, and no serving change is needed.
+- In replicate `b`, each calibrated `(model, workload)` unit in use gets **one** draw from its
+  posterior, shared by every row that resolves to it and by both arms. The unknown is the
+  unit's quality, not each request's.
+- Because the draw is shared between arms, a row that stays on the same model contributes
+  exactly `0` to the quality delta. Label uncertainty enters a quality **delta** only through
+  rows that change model.
+- A published or tier-default quality has no stated uncertainty and is given none. The output
+  reports the coverage (`quality_posterior`: routed, calibrated, with a posterior) so a reader
+  can see how much of a quality range is labels and how much is only the route mix.
+
+This is a partial answer to handoff open question 10 ("how will the simulator estimate quality
+for routes not historically selected"): it does not estimate it better, it says how uncertain
+the existing estimate is — and for a route with no calibration, that the answer is a prior
+with no interval at all.
+
+### 20.4 Zero counts, and two honest non-answers
+
+**Zero counts.** If no row is newly rejected, no resample contains one, and the bootstrap
+range is `[0, 0]` at any sample size. The bootstrap cannot express "we looked at 24 rows and
+saw none". The output therefore adds an analytic bound for a zero count:
+`u = 1 − (1 − P)^(1/n)`, the largest rate at which `n` independent rows would all be clean
+with probability `1 − P`. For a stratified sample with per-app rates `p_a`, the chance of an
+all-clean sample is `∏(1 − p_a)^(n_a) ≤ (1 − p̄)^n` by Jensen's inequality (`ln(1 − x)` is
+concave), where `p̄` is the mix-weighted rate; so the bound holds for the totals as it does
+for one app. At `P = 0.90` it is about `2.3/n`: 9.2% for 24 rows.
+
+**A point outside its own range.** A percentile interval need not contain the point estimate
+(a very skewed posterior, a quantile of a discrete distribution, one replicate). When it does
+not, the leaf is flagged and counted; the range is not widened to hide it.
+
+**A figure that does not always exist.** An arm with one routed row has no latency in a
+replicate that does not draw it. An interval over the replicates where the figure exists
+would be conditional on that; so the figure gets no range and the output says in how many
+replicates it was undefined.
+
+**Too few rows.** Below `--min-rows` (default 30) a bucket reports `insufficient data (n = k)`
+and nothing else. Thirty is a convention.
+
+### 20.5 Invariants, and the test that pins each
+
+| # | Invariant | Pinned by |
+|---|---|---|
+| U1 | Without `--uncertainty` every output file of every mode is byte-identical to the tree before this slice; with it, only `uncertainty`, `inputs.uncertainty` and `shadow_prices.uncertainty` are added | `tests/eval/test_uncertainty_golden.py::test_u1_output_without_the_flag_is_byte_identical` (four modes, files written before any product code existed); `tests/eval/test_uncertainty_cli.py::test_u1_the_flag_adds_keys_and_changes_no_existing_value`; `tests/eval/test_uncertainty_scenarios.py::test_u1_a_plan_adds_one_key_and_changes_no_other`; `tests/eval/test_uncertainty_shadow.py::test_u1_a_plan_adds_one_key_to_the_block` |
+| U2 | Deterministic with the flag; the global `random` state is not read or moved | `tests/eval/test_uncertainty.py::test_u2_same_plan_same_block_and_the_seed_changes_it`, `::test_u2_the_global_random_state_is_not_read_or_moved`; `tests/eval/test_uncertainty_cli.py::test_u2_the_flagged_run_is_byte_identical_twice`; `tests/eval/test_uncertainty_shadow.py::test_u2_shadow_ranges_are_deterministic` |
+| U3 | Every point is inside its range, or the leaf is flagged; nothing is widened | `tests/eval/test_uncertainty.py::test_u3_every_point_is_inside_its_range_or_is_flagged`, `::test_u3_one_replicate_can_leave_the_point_outside_and_says_so`; `tests/eval/test_uncertainty_shadow.py::test_u3_every_shadow_point_is_inside_its_range_or_flagged` |
+| U4 | Paired: a no-op proposal has every delta range exactly `[0, 0]`; the same draws across scopes | `tests/eval/test_uncertainty.py::test_u4_noop_proposal_has_every_delta_range_exactly_zero`, `::test_u4_an_app_is_drawn_the_same_way_alone_and_inside_the_whole_export`; `tests/eval/test_uncertainty_quality.py::test_u4_one_unit_in_both_arms_cancels_in_the_delta`; `tests/eval/test_uncertainty_scenarios.py::test_u4_an_empty_scenario_has_every_range_at_zero`, `::test_u4_a_scenario_gets_the_same_ranges_alone_and_among_others`; `tests/eval/test_uncertainty_shadow.py::test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal` |
+| U5 | Ranges narrow as the rows are replicated 1×, 4×, 16× | `tests/eval/test_uncertainty.py::test_u5_ranges_narrow_as_the_rows_are_replicated` |
+| U6 | A replicate draws exactly `n` rows for an app with `n` rows; a one-row app is in every replicate | `tests/eval/test_uncertainty.py::test_u6_every_replicate_draws_exactly_the_apps_row_count`, `::test_u6_a_one_row_app_is_present_in_every_replicate` |
+| U7 | Below `--min-rows`: `insufficient_data` and `n`, no range; the reports print `insufficient data (n = k)` | `tests/eval/test_uncertainty.py::test_u7_a_small_app_gets_no_range_and_says_how_many_rows`, `::test_u7_too_few_rows_in_all_draws_nothing`, `::test_u7_no_rows_at_all`; `tests/eval/test_uncertainty_report.py::test_u7_report_prints_insufficient_data_instead_of_a_range`; `tests/eval/test_uncertainty_scenarios.py::test_u7_too_few_rows_prints_insufficient_data_and_no_brackets`; `tests/eval/test_uncertainty_shadow.py::test_u7_a_relaxation_with_few_rows_in_scope_gets_no_range` |
+| U8 | Quality-label uncertainty exists only where a posterior does | `tests/eval/test_uncertainty_quality.py::test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`, `::test_u8_without_the_file_a_calibrated_quality_stays_a_point`, `::test_u8_a_tier_default_quality_has_no_label_uncertainty`, `::test_u8_coverage_counts_a_mixed_export`, `::test_a_file_never_attaches_to_a_score_it_did_not_produce` |
+| U9 | The caveat is the spec's text and opens every ranges section; `not_covered` has the five ids | `tests/eval/test_uncertainty_report.py::test_u9_caveat_is_the_spec_text`, `::test_u9_not_covered_names_every_excluded_source`, `::test_u9_section_opens_with_the_caveat_and_says_how_ranges_were_made` |
+| U10 | The plan is in `inputs`; ids tell plans apart | `tests/eval/test_uncertainty_cli.py::test_u10_plan_values_are_in_inputs_with_their_defaults`, `::test_u10_the_id_tells_plans_apart` |
+| U11 | No key ranks; file order is kept; a security range keeps the protection label | `tests/eval/test_uncertainty.py::test_u11_no_key_ranks_or_recommends`; `tests/eval/test_uncertainty_report.py::test_u11_report_text_does_not_rank_or_recommend`; `tests/eval/test_uncertainty_scenarios.py::test_u11_ranges_add_no_ranking_key_and_keep_file_order`; `tests/eval/test_uncertainty_shadow.py::test_u11_a_security_range_keeps_the_protection_label` |
+| U12 | No serving module imports `app.eval` | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` |
+| U13 | The engine's figures are the document's figures | `tests/eval/test_uncertainty.py::test_engine_figures_equal_bucket_stats`, `::test_leaf_point_equals_the_figure_the_document_carries`; `tests/eval/test_uncertainty_scenarios.py::test_every_table_range_is_about_the_figure_in_that_column` |
+
+U1 is pinned against files generated from the tree **before** any code of this slice
+existed, not against a second run of the new code. A test that compares two runs of the same
+build cannot see a change both runs share.
+
+### 20.6 Results
+
+#### 20.6.1 Synthetic only (2026-10-06)
+
+Nothing below is a measurement of real traffic. The rows are built in memory; the purpose is
+to show the method costs what it should and reproduces a known interval.
+
+**Cost of the bootstrap.** Rows built directly as replay results (three apps, one untouched
+by the proposal, about 30% of the others' rows moved and 2% newly rejected); Python 3.12.3,
+one core of an Intel Core Ultra 7 165U; `results_block`, which includes the one-off setup.
+
+| rows | replicates | seconds |
+|---|---|---|
+| 100,000 | 1000 | 25.2 (27.5 on a second run) |
+| 1,000,000 | 50 | 36.0 |
+
+About 25 ms per replicate at 100,000 rows and 0.7 s at 1,000,000; so about 12 minutes for a
+million rows at the default 1000 replicates, per proposal or scenario. No `numpy`.
+
+**A known interval is reproduced** (a test assertion, not a study): with every row served by
+one calibrated unit whose posterior is `Beta(41, 9)`, row resampling cannot move the quality
+mean, and 4000 replicates give a 90% range within 0.01 of the Beta's own 5% and 95% quantiles
+(`test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`).
+
+**Width against sample size** (a test assertion): on the suite's synthetic export, the
+relative width of the cost delta's range at 16× the rows is under half its width at 1×
+(`test_u5_ranges_narrow_as_the_rows_are_replicated`); `1/√16 = 0.25` is the expected ratio.
+
+#### 20.6.2 Still to measure
+
+| measurement | value |
+|---|---|
+| range of each figure of the §19.4.1 scenarios on the DGX lab export (24 rows: expect `insufficient data` at the default `--min-rows`, and wide ranges at `--min-rows 1`) | TO MEASURE |
+| the same scenarios on a persona-generated export of 500, 5,000 and 50,000 rows: range width against `1/√n` | TO MEASURE |
+| empirical coverage: split a large export into disjoint halves many times; how often does the half-A 90% range contain the half-B point estimate | TO MEASURE |
+| coverage under dependence: the same, on traffic generated by agent loops (many related calls per task) | TO MEASURE |
+| quality range with and without `--quality-posteriors` after a real calibration round | TO MEASURE |
+| wall time of `--uncertainty` on a real export, per mode | TO MEASURE |
+
+### 20.7 Limits
+
+- The range is sampling variability under an independence assumption that LLM traffic does
+  not satisfy. A cluster bootstrap over client sessions or agent tasks would be the right
+  unit; the record has no session identifier (the same gap R3 in `arxiv.md` §5 needs closed).
+- A day-block bootstrap was considered and left out: exports are short, blocks would be few,
+  and `ts` is optional.
+- The percentile interval is neither bias-corrected nor studentized.
+- `--min-rows 30` is a convention.
+- Latency ranges carry no per-request variation (§20.2).
+- Reproducibility is per Python minor version.
````

- [ ] **Step 3: `docs/research/arxiv.md`** — §4.2 in place of its "spec pending" line, four experiment rows, one threat to validity.

Patch `docs/research/arxiv.md`:

````diff
diff --git a/docs/research/arxiv.md b/docs/research/arxiv.md
index 89e3259..c0bff41 100644
--- a/docs/research/arxiv.md
+++ b/docs/research/arxiv.md
@@ -316,7 +316,75 @@ literature doesn't model".
   traffic mix with realistic shares (60/25/10/5). Report the comparison table. Also: static
   4-request test vs traffic-weighted replay — the HO §33 "static tests underestimate" claim.
 
-### 4.2 P3c-3 uncertainty ranges — *spec pending*
+### 4.2 P3c-3 uncertainty ranges (spec `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md`)
+
+- **Claim:** a counterfactual policy report over recorded traffic can carry a range on every
+  figure at the cost of a resample, not a re-simulation — and the range must be published
+  with an explicit statement of what it excludes, because the dominant error in such a
+  simulator is replay bias, which no resampling of the same rows can see.
+- **Method (FB §20.1):** replay each row once under both arms; bootstrap the **rows**,
+  stratified by application, with replacement; percentile interval. The draw for an
+  application in a replicate is a function of (seed, replicate, application, row count) only,
+  which gives common random numbers across the two arms, across every scenario of a
+  comparison and across every shadow-price relaxation without holding them in memory together.
+  A delta's range is the range of the paired difference.
+- **What the interval covers:** sampling variability of the recorded rows, per application,
+  under the replay model. Where a route's quality is a P2b-3 calibrated score, the Beta
+  posterior is drawn once per unit per replicate (shared by both arms), so the quality range
+  also carries label uncertainty (FB §20.3).
+- **What it does not cover (state this in the paper, FB §20.2):** replay model error
+  (first-decision replay, frozen signals, predicted overflow); the admission estimate for
+  rows no backend answered (1.54× the backend's count on the one run that measured it, FB
+  §19.4.1); unrecorded inputs (B55, B60); traffic shift; per-request latency variation (replay
+  assigns one latency per endpoint). **A narrow range on a biased replay is still biased** —
+  the range and the baseline agreement answer different questions.
+- **Findings from the design, usable as paper points:**
+  - *The bootstrap cannot express a zero count.* "0 newly rejected" has range `[0, 0]` on 24
+    rows and on 2 million. An analytic bound `1 − (1 − P)^(1/n)` is reported beside it; by
+    Jensen's inequality it holds for the stratified totals as well as per application (FB
+    §20.4). 9.2% for 24 rows.
+  - *Label uncertainty cancels in a paired delta.* A request that stays on the same model
+    contributes exactly zero to the quality delta in every replicate; label uncertainty
+    enters only through the rows that change model.
+  - *Quality with no calibration has no interval at all.* The output reports the share of
+    rows whose quality has a posterior, rather than inventing a spread for a published score
+    or a tier default. This is the honest state of HO open question 10.
+  - *A percentile interval need not contain its point.* It is flagged, not widened.
+  - *The posterior parameters were recoverable without a serving change*, because the
+    calibration file is content-addressed by the same hash the admin API verified at apply
+    time.
+- **Invariants to cite, with the test that pins each** (full table: FB §20.5):
+  U1 `test_u1_output_without_the_flag_is_byte_identical`; U2
+  `test_u2_same_plan_same_block_and_the_seed_changes_it`; U3
+  `test_u3_every_point_is_inside_its_range_or_is_flagged`; U4
+  `test_u4_noop_proposal_has_every_delta_range_exactly_zero`,
+  `test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal`; U5
+  `test_u5_ranges_narrow_as_the_rows_are_replicated`; U6
+  `test_u6_a_one_row_app_is_present_in_every_replicate`; U7
+  `test_u7_a_small_app_gets_no_range_and_says_how_many_rows`; U8
+  `test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`; U9
+  `test_u9_caveat_is_the_spec_text`; U10 `test_u10_the_id_tells_plans_apart`; U11
+  `test_u11_ranges_add_no_ranking_key_and_keep_file_order`; U12
+  `test_no_serving_module_imports_app_eval`; U13 `test_engine_figures_equal_bucket_stats`.
+- **Synthetic numbers only so far (FB §20.6.1) — do not present as an evaluation:** 100,000
+  rows × 1000 replicates in about 25 s, 1,000,000 rows at about 0.7 s per replicate, pure Python;
+  a `Beta(41, 9)` posterior's 90% interval reproduced to within 0.01.
+- **Experiments for the DGX lab (all TO MEASURE):**
+  - **E9a — width against sample size.** Persona-generated traffic at 500, 5,000 and 50,000
+    rows through the same six scenarios as E7b; report the width of each delta's range and
+    fit against `1/√n`. Include the 24-row export as the "insufficient data" case.
+  - **E9b — empirical coverage.** Split a large export into disjoint halves, repeatedly; how
+    often does the 90% range from one half contain the other half's point estimate. This is
+    the test of the method on real records.
+  - **E9c — coverage under dependence.** The same on traffic produced by agent loops (many
+    related calls per task). Expected: under-coverage, by an amount worth reporting; it
+    motivates a cluster bootstrap over tasks, which needs a session identifier (the same
+    missing field as R3, §5).
+  - **E9d — label uncertainty.** After one real calibration round, the quality range with and
+    without `--quality-posteriors`, and the share of routed rows covered.
+- **Limits:** independence within an application is assumed and false; no block or cluster
+  bootstrap; percentile interval, not BCa; `--min-rows 30` is a convention; reproducible per
+  Python minor version.
 ### 4.3 P3c-4 capacity model — *spec pending* (HO §25 "230% of capacity" example)
 ### 4.4 Replay fidelity
 
@@ -417,7 +485,11 @@ literature doesn't model".
 | E7b | Rejected rows replay as routed (B60) | FB §18.4.1, §19.4.1 | done: defect observed (7 of 7 rejections replayed as routed), fixed, re-run on post-upgrade rows — 7 of 7 rejected in replay, outcome agreement 1.0 over 24, raise-budget newly serves 7 |
 | E7c | Token-estimate gap and over-rejection (B64) | FB §19.4.1 | first measurement: routing/backend = 1.54 on 17 served rows (synthetic prose); real traffic **TO MEASURE** |
 | E8 | Healthcare scenario triad, traffic-weighted vs static | P3c-2 | **TO MEASURE** |
-| E9+ | uncertainty, capacity, fidelity, P2 metrics, R2/R3/R4 | §4–§5 | specs pending |
+| E9a | Range width against sample size (500 / 5,000 / 50,000 rows; 24-row case) | §4.2, FB §20.6.2 | **TO MEASURE** |
+| E9b | Empirical coverage of the 90% range on split halves of a real export | §4.2, FB §20.6.2 | **TO MEASURE** |
+| E9c | Coverage under dependence (agent-loop traffic) | §4.2, FB §20.6.2 | **TO MEASURE** |
+| E9d | Quality range with and without calibration posteriors | §4.2, FB §20.6.2 | **TO MEASURE** |
+| E10+ | capacity, fidelity, P2 metrics, R2/R3/R4 | §4–§5 | specs pending |
 
 **Data caveat for every live number:** the Spark serves one app (`kit-gtm`, Qwen Code), all
 models priced $0 (FB §17.4). The DGX lab (the test target from 2026-10-06) has one priced
@@ -435,6 +507,9 @@ before writing the evaluation section.
 - Simulated quality is an input (FB §6.1); deterministic graders; synthetic single-turn cases.
 - Single tenant, single seed live runs; $0 prices on the Spark.
 - Replay is first-decision only (until §4.4), with frozen signals; cached tokens not recorded.
+- Uncertainty ranges (§4.2) are sampling variability under an independence assumption that
+  agent traffic violates; they exclude replay bias by construction. Coverage on real records
+  is unmeasured (E9b, E9c).
 - Endpoint declarations (region, retention) are operator-declared; RouteForge enforces the
   declaration and cannot verify a provider's contractual claim (CLAUDE.md).
 - Patent/prior-art scan incomplete; arXiv leads unverified.
````

- [ ] **Step 4: `docs/roadmap/priorities-1-3-status.md`** — the slice in Built, §2.1, §3.

Patch `docs/roadmap/priorities-1-3-status.md`:

````diff
diff --git a/docs/roadmap/priorities-1-3-status.md b/docs/roadmap/priorities-1-3-status.md
index 118e537..29d7670 100644
--- a/docs/roadmap/priorities-1-3-status.md
+++ b/docs/roadmap/priorities-1-3-status.md
@@ -61,6 +61,7 @@ Handoff §37 Priority 2 criteria met:
 | B56 / B57 | #78 | The exact app scope is enforced at the router; the `serving` logger actually prints in deployment | |
 | **P3c-1** shadow prices | #79, #80 | `policy_simulate.py --shadow-prices` relaxes one constraint at a time, per app, and prices it (cost/day, rejections avoided, newly rejected, quality, latency, on-prem share). Security constraints appear only as "cost of protection", never as a recommendation. | `docs/reference/policy-simulator.md` §8, research §17 |
 | **P3c-2** alternative scenarios | #84 | `policy_simulate.py --scenarios` replays several administrator-written scenarios against one shared baseline and writes one comparison table, in file order, never ranked. Proposals can assume a token scale, a cached-prompt share and a per-endpoint latency. Replay mirrors the gateway's context-window overflow re-selection. `--since` cuts old rows; `--min-agreement` refuses a run whose baseline disagrees with the record. | `docs/reference/policy-simulator.md` §9, research §18 |
+| **P3c-3** uncertainty ranges | pending | `policy_simulate.py --uncertainty` adds a range to every headline figure of all three modes: a percentile interval from a bootstrap over the replayed rows, resampled within each application and paired across arms, scenarios and relaxations. Opt-in; without the flag every output is byte-identical. `--quality-posteriors` adds label uncertainty for calibrated qualities from the P2b-3 recommendation file. Below `--min-rows` a bucket says "insufficient data"; a zero count carries a bound instead of reading as no risk. The range is sampling variability only and every report says so. | `docs/reference/policy-simulator.md` §10, research §20 |
 | B60 replayable rejections | #86 | Every outcome row records `routing_tokens`, the token count the router was given, so a request rejected for its size replays as rejected and a proposal that raises a budget shows what it rescues. The baseline check reports outcome agreement (routed or rejected against the recorded status) next to model agreement, and `--min-agreement` gates on the lower. Rejected rows from before the fix are excluded and counted. One `ALTER` before deploy. | `docs/reference/policy-simulator.md` §5–§6, `docs/reference/routing-outcomes.md` §1, research §19 |
 
 Handoff §37 Priority 3 criteria met:
@@ -93,16 +94,18 @@ partly met: nothing is ever applied, but there is no approval workflow (see §2.
 
 ## 2. Still to build
 
-### 2.1 Priority 3 (in progress: P3c-3 is next)
+### 2.1 Priority 3 (in progress: P3c-4 is next)
 
 The owner-approved order is scenarios → uncertainty → capacity. Each needs its own
-brainstorm, spec, plan and build.
-
-1. **P3c-3 uncertainty ranges** (handoff §36, open question 11). Today every figure is a
-   point estimate from one replay. Candidates:
-   - bootstrap over recorded rows for cost, latency and rejections;
-   - propagation of the P2b-3 quality confidence intervals;
-   - a clear "insufficient data" state when an app has too few rows.
+brainstorm, spec, plan and build. Scenarios (P3c-2) and uncertainty (P3c-3) are built.
+
+1. **Follow-ups to P3c-3** (research §20.7), none blocking:
+   - a cluster or day-block bootstrap. Rows are resampled as if independent; the calls of one
+     agent loop are not. A cluster bootstrap needs a session or task identifier on the record;
+   - calibration posteriors in the snapshot. Today the operator passes the P2b-3
+     recommendation file; exporting `alpha`/`beta` would need two `model_quality` columns and
+     a snapshot schema bump;
+   - measured coverage on real records (`docs/research/arxiv.md` E9a–E9d).
 2. **P3c-4 capacity model** (handoff §26, §31). Nothing today says "on-prem demand reaches
    230% of capacity". This needs a declared per-endpoint capacity (requests/s or tokens/s),
    either operator-declared or measured from the observation log. The simulator would then
@@ -170,8 +173,9 @@ brainstorm, spec, plan and build.
 
 1. Read the handoff §28–§31, then `docs/reference/policy-simulator.md` §1–§8 and research
    §15–§17.
-2. Start P3c-3 (uncertainty ranges) with a design brainstorm, then a spec, plan and subagent
+2. Start P3c-4 (the capacity model) with a design brainstorm, then a spec, plan and subagent
    build.
 3. Before quoting any simulator number from the Spark, check both baseline agreements, and
    exclude rows recorded before B55. Rejected rows recorded before B60 are excluded
-   automatically (`missing_routing_tokens`).
+   automatically (`missing_routing_tokens`). A range from `--uncertainty` is sampling
+   variability only: quote it with the agreements, never instead of them.
````

- [ ] **Step 5: Check the docs gate and every cited test name**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_docs_links.py -p no:warnings -rf
```

Expected: `2 passed`.

```bash
python3 - <<'EOF'
import glob
import re
import subprocess

names = set()
for path in glob.glob("tests/**/*.py", recursive=True):
    names |= set(re.findall(r"^def (test_\w+)", open(path, encoding="utf-8").read(), re.M))
modules = {p.rsplit("/", 1)[1][:-3] for p in glob.glob("tests/**/test_*.py", recursive=True)}
missing = []
for doc in ("docs/reference/policy-simulator.md", "docs/research/feasible-route-bench.md",
            "docs/research/arxiv.md", "docs/roadmap/priorities-1-3-status.md"):
    diff = subprocess.run(["git", "diff", "HEAD", "--", doc], capture_output=True,
                          text=True, check=True).stdout
    added = "\n".join(line[1:] for line in diff.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    for name in sorted(set(re.findall(r"\b(test_\w+)", added))):
        if name not in names and name not in modules:
            missing.append((doc, name))
print("missing:", missing)
EOF
```

Expected: `missing: []`. (Run it before committing: it reads the uncommitted diff.)

- [ ] **Step 6: Commit**

```bash
git add docs/reference/policy-simulator.md docs/research/feasible-route-bench.md docs/research/arxiv.md docs/roadmap/priorities-1-3-status.md
git commit -m "$(cat <<'EOF'
docs: P3c-3 uncertainty ranges

Reference §10 (flags, method, output, the caveat), research §20 (the
model, the zero-count bound, invariants and tests; synthetic numbers
only, real measurements TO MEASURE), arxiv §4.2 with experiments
E9a-E9d, and the roadmap.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: The whole suite, the timing, and the hand-off note

**Files:** none modified, unless a failure caused by this branch is found.

**Interfaces:**
- Consumes: everything above.
- Produces: a clean suite run, a timing figure for this machine, and the note for whoever opens the pull request.

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
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_uncertainty_golden.py tests/eval/test_uncertainty.py tests/eval/test_uncertainty_quality.py tests/eval/test_uncertainty_report.py tests/eval/test_uncertainty_scenarios.py tests/eval/test_uncertainty_shadow.py tests/eval/test_uncertainty_cli.py tests/eval/test_isolation.py -p no:warnings -rf
```

Expected: `149 passed`. This run is U1–U13.

- [ ] **Step 3: Time the bootstrap on a synthetic 100,000-row export**

Nothing is committed in this step. The script builds replay results in memory, so it times the bootstrap alone.

```bash
cat > /tmp/p3c3_timing.py <<'EOF'
"""Synthetic timing for the P3c-3 bootstrap. Not committed."""
import random
import sys
import time

from app.eval.simulate import ArmResult, RowResult
from app.eval.uncertainty import UncertaintyPlan, results_block


def arm(model: str, cost: float, latency: float, quality: float) -> ArmResult:
    return ArmResult("routed", model, "onprem", "cheap", (model,), (), cost, latency,
                     "observed", quality, "tier_default")


REJECTED = ArmResult("rejected", None, None, None, (), ("over_budget",),
                     None, None, None, None, None)


def synthetic(n: int, apps: int = 3, seed: int = 1) -> list[RowResult]:
    """`apps` applications; `app-0` is untouched; in the others about 30% of
    rows move to a cheaper model and about 2% are newly rejected."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        app = f"app-{i % apps}"
        cost = rng.uniform(0.0001, 0.02)
        current = arm("local-mid", cost, 800.0, 0.75)
        roll = rng.random()
        if app == "app-0" or roll >= 0.32:
            proposed = current
        elif roll < 0.30:
            proposed = arm("local-cheap", cost * 0.2, 300.0, 0.5)
        else:
            proposed = REJECTED
        rows.append(RowResult({"application_id": app, "request_id": f"r{i}",
                               "workload": None}, current, proposed))
    return rows


n, replicates = int(sys.argv[1]), int(sys.argv[2])
rows = synthetic(n)
start = time.perf_counter()
block = results_block(rows, UncertaintyPlan(replicates=replicates), days=3.0)
print(f"rows={n} replicates={replicates} seconds={time.perf_counter() - start:.1f}")
print("cost delta:", block["totals"]["cost_usd"]["delta"])
print("newly rejected:", block["totals"]["newly_rejected"])
EOF
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=. uv run python /tmp/p3c3_timing.py 100000 1000
```

Expected: one line `rows=100000 replicates=1000 seconds=…`, then

```
cost delta: {'point': -175.028985, 'lo': -176.928876, 'hi': -173.078684}
newly rejected: {'point': 1300, 'lo': 1242, 'hi': 1357}
```

The two result lines are deterministic and must match exactly. The time on the machine this plan was validated on was **25.2 seconds, and 27.5 on a second run** (Python 3.12.3, one core of an Intel Core Ultra 7 165U); anything under two minutes is unremarkable. Report your figure with the branch. If it is over five minutes, report that as a finding and do not optimise in this plan.

- [ ] **Step 4: Confirm the branch is clean and nothing was pushed**

```bash
git status --short
git log --oneline main..HEAD
```

Expected: `git status --short` prints nothing. The log lists, newest first, the eight task commits (Tasks 8 down to 1), then the plan commit and the spec commit. Do not push.

- [ ] **Step 5: Hand the note to whoever opens the pull request**

Nothing to run. Report these facts with the branch:

1. **Nothing to migrate and nothing to deploy.** No column, route, setting or dependency changed. `policy_simulate.py --uncertainty` works against any export and snapshot the simulator already reads.
2. **The roadmap row says PR `pending`.** Replace it with the number in a follow-up docs commit.
3. **The golden files** under `tests/eval/fixtures/uncertainty_golden/` pin the unflagged output. A later slice that deliberately changes that output regenerates them with `uv run python -m tests.eval.test_uncertainty_golden` and reviews the diff.
4. **The experiments that fill research §20.6.2** are `docs/research/arxiv.md` E9a–E9d. None has been run. The 24-row DGX lab export will report `insufficient data` at the default `--min-rows`; E9a needs persona-generated traffic at 500, 5,000 and 50,000 rows.
5. **The timing from Step 3** on this machine.

---

## Open risks

- **The range is too narrow on real traffic, by an unknown amount.** It assumes the rows of an application are independent. The calls of one agent loop are not, and every one of them is a row. Nothing in this plan measures the gap; `arxiv.md` E9b and E9c are the experiments, and a cluster bootstrap needs a session identifier the record does not have.
- **A range can be mistaken for accuracy.** It excludes replay bias by construction. The caveat is printed with every range and pinned by a test, but a reader who quotes `[lo, hi]` without the baseline agreements has lost the more important number.
- **`--min-rows 30` is a convention.** A heavily skewed cost distribution can need far more rows before a percentile interval is trustworthy. There is no diagnostic for that.
- **The zero-count bound rests on the same independence assumption** and is printed as a rate. It is labelled, and the sentence says correlated rows make the true bound larger, but it is the one number in the output that is not a property of the resamples.
- **Quality-label uncertainty depends on the operator keeping the recommendation file.** Without it a calibrated quality is a point and the report says how many rows that affects. A deployment whose file is lost cannot recover the posterior: the database does not hold it.
- **Latency ranges are nearly always degenerate** on a deployment with one or two endpoints, because replay assigns one latency per endpoint. That is stated, but a row of identical `lo`, `point`, `hi` may still be read as "latency is certain".
- **Cost is linear in resamples × rows, per scenario and per binding relaxation.** A 20-scenario comparison over a million rows at the default is about four hours of single-core Python. `--bootstrap` and `--since` are the remedies; there is no parallelism.
- **Reproducibility is per Python minor version.** `random.Random(str)` seeding and `choices` are stable in practice, but the standard library does not promise identical streams across versions. A document's id does not record the Python version.
- **The golden pin breaks on any deliberate change to the default output**, and on a change to the base `config/` that reaches a routing decision. That is its job, but the next slice (P3c-4) will have to regenerate it if it adds a default key.
- **`--scenarios` with `--min-agreement` and `--uncertainty` replays the snapshot one extra time** to gate early. On a very large export that is the cost of one more current-arm replay.
- **The comparison table is wider.** Eight cells per row gain a bracketed pair; with many scenarios the table is hard to read in a terminal.
