# P3c-2 Alternative Scenarios Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an administrator write several candidate policy changes into one file and see their predicted effects side by side, replayed over the same recorded traffic against one shared baseline, with nothing ranked and nothing applied.

**Architecture:** A new offline module `app/eval/scenarios.py` loads a `routeforge.policy-scenarios/v1` file, applies each scenario with the existing `apply_proposal`, and replays each one against a single precomputed `current` arm using the P3a engine (`app/eval/simulate.py`). The proposal format gains three assumption levers (`workload.token_scale`, `workload.cached_share`, per-endpoint `latency_ms`) carried in an `assumptions` block of the applied document, and replay gains deterministic context-window overflow re-selection in both arms. The CLI gains `--scenarios`, `--since` and `--min-agreement`.

**Tech Stack:** Python 3.12, pytest, Hypothesis, `uv`. No new dependency. No serving-code change.

**Spec:** docs/superpowers/specs/2026-10-05-p3c2-scenarios-design.md

## Deviations from spec

Each of these follows the code as it is, keeps the spec's intent, and is pinned by a test.

1. **`ArmResult` gains `attempted`, and `overflow_hops` is derived from it (spec §5 names only `overflow_hops`).** `attempted` is every model the arm selected, in order. `_baseline` compares the *first* pick with the recorded first attempt. Without this, an overflow failover that replay reproduces correctly (recorded `attempts: [m1, m2]`, replay serves `m2`) would count as a disagreement, and agreement would fall.
2. **Spec §5 expects baseline agreement to rise. By construction it should not move.** The baseline check compares first decisions over `ok` rows, and the first decision is unchanged by overflow replay. Recorded `context_overflow` rows also cannot be reproduced: every failure row records `prompt_tokens: 0` (`app/apps/outcomes.py::build_failure_outcome`), so replay sees a zero-token prompt that fits any window. Fixing that needs a serving change, which is out of scope. Research §18 records this and keeps the before/after measurement as TO MEASURE.
3. **`token_scale` on a zero-token row leaves it at zero (spec §4.1 says `max(1, round(p × scale))`).** Failure rows record 0 prompt tokens. Flooring them to 1 would make cost fall as the scale rises (1 token at 0.5x, 0 tokens at 1x), which breaks S5, and would make `token_scale: 1` a non-identity, which breaks S4.
4. **Saturated complexity is bounded below by `scale` (spec §4.1 says `min(1, prompt_tokens' / 2000)`).** A saturated row had at least 2000 routing tokens, so its scaled complexity is at least `scale`. The recorded `prompt_tokens` is the backend's count and can be below 2000 on such a row, so the spec formula can produce an impossible value, including a *lower* complexity when scaling up. Replay uses `min(1, max(scale, prompt_tokens' / 2000))`. Scaling up therefore gives exactly 1 and is not counted as an estimate; only scaling down is counted in `saturated_complexity_estimated`.
5. **An asserted `latency_ms` is scored as a measurement, and is ignored by scoring when `routing.signals.enabled` is false (spec §4.2 says it feeds both).** `FrozenSignals.latency` returns it with source `measured`, so it also acts as the same-tier peer prior, as a live measurement would. Otherwise re-asserting the current value could remove a peer prior and move a route, which breaks S4. With signals disabled the live router ignores measured latency, so the assertion feeds the replayed latency only. `ArmResult.latency_source` is `"proposal"` in both cases.
6. **S4's latency clause holds where the router's term and the replayed latency are the same number.** Replay's latency ladder is observed → signal → tier default; the router's is signal → peer prior → tier default. "The value replay would otherwise use" is therefore not always the router's term. The S4 test uses the case where they coincide (a measured snapshot signal, no observed rows), and a second test pins the peer-prior case.
7. **`--since` cuts the export, including observed latencies (spec §6 only says rows are excluded).** A run with `--since X` equals a run over an export that never held the older rows, apart from the `before_since` count. A row with no `ts` cannot be placed, so under `--since` it is counted `missing_features`.
8. **`simulation.json` gains `inputs.since` and a top-level `assumptions` block (spec lists `since` only for `comparison.json`).** Two runs with different cuts must not share a `simulation_id`, and spec §4.1 requires the report to show the saturated-row count for single-proposal runs too.
9. **`--min-agreement` also gates `--shadow-prices` without `--proposal`.** That mode has no summary to read agreement from, so a new `baseline_check` replays the snapshot once. This closes the P3c-1 limit noted in `docs/research/arxiv.md` §3.9.
10. **The comparison table's two cost columns are like-for-like (spec §8 does not say which delta).** `cost_delta_usd` is `totals.cost_usd_both_routed.delta`, and `cost_per_day_delta_usd` is that divided by the window in days. A scenario that newly rejects rows therefore never reads as cheaper for it. The all-routed delta stays in each scenario's `summary`.
11. **Loading is two functions.** `load_scenarios(path)` checks shape and has no snapshot. `apply_scenarios(snapshot, doc, base=)` validates every proposal against the snapshot, all-or-nothing, before any replay. `title` is optional.
12. **Test placement differs from spec §10.** S4–S6 live in `tests/eval/test_proposal_workload.py` and S7 in `tests/eval/test_replay_overflow.py`, in the tasks that build those behaviours. S10 and S11 live in the existing P3a test files.
13. **A context-window rejection is keyed `(context_window)` in `new_rejections.by_core`.** It has no conflicting core; without this it would read `(unknown)`, which means "the core computation failed".

## Global Constraints

- **Offline only.** Everything lives in `app/eval/` and `scripts/policy_simulate.py`. No serving module changes. `GET /admin/config-snapshot` is unchanged.
- **No serving import of `app.eval`.** `tests/eval/test_isolation.py` must keep passing.
- **RouteForge never ranks or recommends a scenario.** Scenarios are reported in file order. No output field may be named rank, score, best, winner, preferred or recommended.
- **Deterministic JSON.** `json.dumps(..., sort_keys=True, indent=2)`, floats through `_round6` (`app/eval/outcomes.py`).
- **Unknown key = load error naming its dotted path.** Inside a scenario the path is prefixed `scenarios[<i>](<name>).proposal.`.
- **Exit codes:** `0` written; `2` bad input or flags that do not combine; `3` `--fail-on-new-rejections` and the proposal or any scenario newly rejects ≥ 1 row; `4` baseline agreement is `None` or below `--min-agreement`, nothing written.
- **Exact values from the spec:** `token_scale` finite, `0 < x ≤ 10`, default `1.0`. `cached_share` finite, `0 ≤ x ≤ 1`, default `0`. `latency_ms` finite `> 0` or `null`. Scenarios per file: 1–20. Name: 1–64 characters of `[A-Za-z0-9._+-]`, unique. Schemas: `routeforge.policy-scenarios/v1`, `routeforge.scenario-comparison/v1`. `EXCLUSION_REASONS = ("before_since", "cache_hit", "unknown_status", "unknown_app", "missing_features")`. Overflow rule: `prompt_tokens' + output_tokens > context_window`, at most `routing.max_failover_attempts` extra selects, undeclared window never overflows.
- **The `current` arm never carries assumptions.** They belong to a scenario.
- **Tests:** always run pytest with `LITELLM_LOCAL_MODEL_COST_MAP=True` or it hangs on import. Use `uv run`. There is no pytest-asyncio: use `asyncio.run`. Shared fixtures are in `tests/eval/sim_fixtures.py` (`BASE_SETTINGS`, `make_snapshot`, `app_entry`, `onboarded_row`, `make_row`).
- **Code style:** type-hint everything. Match the surrounding comment density and idiom. `app/eval` modules import each other's underscore-private helpers (as `shadow.py` already does); keep that idiom.
- **How to read the code steps.** A step that says "Create" shows the whole new file. A step that says "Patch" shows a real patch against the tree as the previous task left it. Save the block to a file and run `git apply <file>`, or make the same edits by hand. Every patch in this plan was applied in order to a copy of the repo and the listed tests were run against it.
- **Git:** stage files by name, never `git add -A`. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

Five input classes the spec implies, that are likely to bite a user, and that an obvious task test would miss. Each has a named test in the owning task.

1. **Timestamps under `--since`.** `Z` against `+00:00`, a value with no offset, a non-UTC offset, on the flag and on rows; a row with no `ts`. Comparing strings instead of instants gets these wrong. Task 1: `test_since_compares_instants_not_strings`, `test_since_row_without_ts_is_missing_features_not_simulated`.
2. **Degenerate workload numbers.** A bool, a string, `NaN` (which `json.loads` accepts) or infinity as a scale; a zero-token failure row; a saturated row whose backend token count is under 2000. Task 3: `test_workload_errors_name_the_dotted_path`, `test_json_nan_literal_is_refused`. Task 4: `test_scaled_request`.
3. **An asserted latency that should be neutral.** Re-asserting the value on the tier's only measured endpoint; signals disabled; `latency_ms: null`; any other field on a YAML pool model. Task 5: `test_neutral_latency_keeps_the_peer_prior_it_supplies`, `test_signals_disabled_assertion_feeds_latency_but_not_routing`, `test_latency_ms_null_is_no_override`, `test_yaml_pool_model_accepts_only_latency_ms`.
4. **Overflow boundaries.** `prompt + output == window` must fit; `max_failover_attempts: 0`; more overflowing endpoints than the bound with a fitting one behind them (rejected, as live); the baseline still compares first decisions. Task 6: `test_boundary_prompt_plus_output_equal_to_window_fits`, `test_bound_is_max_failover_attempts_exactly_as_live`, `test_baseline_compares_the_first_decision_not_the_served_model`.
5. **Scenario files and exports as people really make them.** A name with a trailing newline or look-alike Unicode; a duplicate name; an error in the third scenario; two overlapping exports concatenated (repeated `request_id`s) under the precomputed current arm; `--min-agreement` with zero comparable rows. Task 7: `test_illegal_names_are_refused`, `test_duplicate_name_names_the_second_occurrence`, `test_value_errors_carry_index_name_and_dotted_path`. Task 8: `test_precomputed_current_arm_equals_recomputing_it`, `test_invalid_scenario_fails_before_any_replay`. Task 2: `test_min_agreement_with_no_comparable_rows_exits_4`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `app/eval/simulate.py` | modify | `--since` row cut; agreement gate helpers; workload assumptions in replay; overflow re-selection; `arm_results` and the precomputed current arm; report helpers shared with the comparison |
| `app/eval/proposal.py` | modify | `workload` and `latency_ms` validation; `assumptions_block`; writes the `assumptions` block |
| `app/eval/scenario.py` | modify | `ALL_APPS`, `Assumptions`, `Scenario.assumptions`, `FrozenSignals` latency overrides; refuses a snapshot file that carries `assumptions` |
| `app/eval/shadow.py` | modify | `shadow_prices(..., since=)` |
| `app/eval/scenarios.py` | create | scenarios file load and validation; `compare`; `comparison.json`; `report.md` |
| `scripts/policy_simulate.py` | modify | `--since`, `--min-agreement`, `--scenarios`, exit 4 |
| `tests/eval/test_simulate.py` | modify | `--since` and gate-helper tests; one updated exclusion-count expectation |
| `tests/eval/test_shadow.py` | modify | `since` reaches shadow prices |
| `tests/eval/test_policy_simulate_cli.py` | modify | CLI tests for the three flags and the new mode |
| `tests/eval/test_proposal_workload.py` | create | `workload` and `latency_ms`: validation and replay (S4, S5, S6) |
| `tests/eval/test_replay_overflow.py` | create | overflow re-selection (S7) |
| `tests/eval/test_scenarios_load.py` | create | scenarios file schema, names, nested error paths |
| `tests/eval/test_scenarios_compare.py` | create | engine and output (S1, S2, S3, S8, S9) |
| `docs/reference/policy-simulator.md` | modify | §2.1 rows, §3 flags and exit codes, §4, §6, new §9 |
| `docs/research/feasible-route-bench.md` | modify | new §18 |
| `docs/research/arxiv.md` | modify | §4.1 invariant → test mapping |
| `docs/roadmap/priorities-1-3-status.md` | modify | P3c-2 moved to Built |

## Spec invariants → tests

| # | Invariant | Test | Task |
|---|---|---|---|
| S1 | empty scenario is a no-op | `tests/eval/test_scenarios_compare.py::test_s1_empty_scenario_is_a_noop` | 9 |
| S2 | one scenario ≡ `--proposal` | `tests/eval/test_scenarios_compare.py::test_s2_single_scenario_equals_single_proposal`; end to end `tests/eval/test_policy_simulate_cli.py::test_cli_single_scenario_matches_the_proposal_run` | 8, 10 |
| S3 | order independence | `tests/eval/test_scenarios_compare.py::test_s3_order_independence` | 9 |
| S4 | neutral assumptions | `tests/eval/test_proposal_workload.py::test_s4_neutral_workload_changes_nothing`, `::test_s4_neutral_latency_changes_no_route_or_cost` | 4, 5 |
| S5 | cost monotone in `token_scale` | `tests/eval/test_proposal_workload.py::test_s5_cost_monotone_in_token_scale` | 4 |
| S6 | cached share | `tests/eval/test_proposal_workload.py::test_s6_cached_share_never_raises_cost`, `::test_s6_cached_share_without_declared_price_is_neutral` | 4 |
| S7 | overflow | `tests/eval/test_replay_overflow.py::test_s7_overflowing_pick_is_never_served`, `::test_s7_undeclared_window_never_overflows`, `::test_s7_all_overflow_rejects_context_window` | 6 |
| S8 | no recommendation, file order | `tests/eval/test_scenarios_compare.py::test_s8_no_ranking_and_file_order` | 9 |
| S9 | security monotonicity per scenario | `tests/eval/test_scenarios_compare.py::test_s9_scenario_proposals_never_widen_eligibility` | 8 |
| S10 | `--since` | `tests/eval/test_simulate.py::test_s10_since_excludes_and_counts_old_rows`; CLI `tests/eval/test_policy_simulate_cli.py::test_cli_since_cuts_the_export_and_records_the_cut` | 1 |
| S11 | agreement gate | `tests/eval/test_policy_simulate_cli.py::test_s11_min_agreement_exits_4_and_writes_nothing` | 2 |
| — | serving isolation | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` (unchanged, run in Tasks 7 and 11) | 7, 11 |

---

## Tasks

### Task 1: `--since` row cut and the `before_since` exclusion reason (spec §6, S10)

**Files:**
- Modify: `app/eval/simulate.py` (`EXCLUSION_REASONS` line 52, `select_rows` lines 119–146, `run_rows` lines 239–250, `simulate` lines 253–265)
- Modify: `app/eval/shadow.py` (`shadow_prices` lines 434–485)
- Modify: `scripts/policy_simulate.py` (`_parser` lines 51–69, `main` lines 72–208)
- Test: `tests/eval/test_simulate.py`, `tests/eval/test_shadow.py`, `tests/eval/test_policy_simulate_cli.py`

Line ranges throughout this plan are as of the branch's base commit; later tasks shift them.

**Interfaces:**
- Consumes: the existing `select_rows(records, app_ids)`, `run_rows(records, current, proposed)`, `simulate(records, snapshot, proposal, *, base)`, `shadow_prices(records, baseline_doc, *, base, cap, baseline_label)`, `_parse_ts(ts: str) -> datetime`.
- Produces:
  - `parse_since(text: str) -> datetime` — aware UTC; raises `ValueError`.
  - `_before_since(row: dict, since: datetime | None) -> bool`.
  - `select_rows(records, app_ids, *, since: datetime | None = None) -> tuple[list[dict], dict[str, int]]`.
  - `run_rows(records, current, proposed, *, since: datetime | None = None)`.
  - `simulate(records, snapshot, proposal, *, base, since: datetime | None = None)`.
  - `shadow_prices(..., since: datetime | None = None)`.
  - `EXCLUSION_REASONS` with `before_since` first.
  - CLI flag `--since TS`; `simulation.json` `inputs.since` (ISO string or `null`).

**Rules.** `before_since` is checked first. The comparison is between instants: a `ts` with no offset is read as UTC. The boundary row (`ts == since`) is kept. `--since` is a cut of the export: observed latencies come only from rows at or after it. With a `since`, a row whose `ts` is null is counted `missing_features`. The one existing expectation that legitimately changes is `test_select_rows_exclusions`, which now also expects `"before_since": 0`.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_simulate.py`:

````diff
diff --git a/tests/eval/test_simulate.py b/tests/eval/test_simulate.py
index 02c8e39..dd351ae 100644
--- a/tests/eval/test_simulate.py
+++ b/tests/eval/test_simulate.py
@@ -6,8 +6,9 @@ import asyncio
 import pytest
 from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
 
-from app.eval.simulate import (REPORT_CAVEAT, observed_latencies, render_report, select_rows,
-                               simulate, simulate_row, summarize)
+from app.eval.simulate import (EXCLUSION_REASONS, REPORT_CAVEAT, observed_latencies,
+                               parse_since, render_report, select_rows, simulate,
+                               simulate_row, summarize)
 from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                      onboarded_row)
 
@@ -92,8 +93,8 @@ def test_select_rows_exclusions():
             make_row("e", "app-a", complexity=None)]
     rows, excluded = select_rows(recs, {"app-a"})
     assert [r["request_id"] for r in rows] == ["a"]
-    assert excluded == {"cache_hit": 1, "unknown_status": 1, "unknown_app": 1,
-                        "missing_features": 1}
+    assert excluded == {"before_since": 0, "cache_hit": 1, "unknown_status": 1,
+                        "unknown_app": 1, "missing_features": 1}
 
 
 def test_observed_latency_median_single_attempt_ok_only():
@@ -459,3 +460,70 @@ def test_recorded_max_tokens_reaches_the_budget_check():
     assert by["nc"].current.outcome == "routed"
     assert by["bc"].current.outcome == "rejected"
     assert "over_budget" in by["bc"].current.reject_codes
+
+
+# --- P3c-2 Task 1: --since row cut (spec §6, S10) -----------------------------
+
+def _since_rows():
+    return [make_row("old1", "app-a", ts="2026-09-20T11:59:59+00:00"),
+            make_row("old2", "app-a", ts="2026-09-19T00:00:00+00:00", cache_hit=True),
+            make_row("edge", "app-a", ts="2026-09-20T12:00:00+00:00"),
+            make_row("new1", "app-a", ts="2026-09-21T00:00:00+00:00")]
+
+
+def test_s10_since_excludes_and_counts_old_rows():                         # S10
+    since = parse_since("2026-09-20T12:00:00Z")
+    results, excluded = asyncio.run(simulate(
+        _since_rows(), make_snapshot(), {"schema": P}, base=BASE_SETTINGS, since=since))
+    # none before the cut reaches replay; the boundary row (ts == since) does
+    assert [r.row["request_id"] for r in results] == ["edge", "new1"]
+    # before_since is checked FIRST: old2 is also a cache hit, counted once, here
+    assert excluded["before_since"] == 2 and excluded["cache_hit"] == 0
+    assert EXCLUSION_REASONS[0] == "before_since"
+
+
+def test_since_equals_a_precut_export():
+    """`--since` is a cut of the export: every number equals a run over an
+    export that never held the old rows -- including the observed-latency
+    medians, which an old row must not feed."""
+    old = make_row("old", "app-a", ts="2026-09-01T00:00:00+00:00", latency_ms=99999.0)
+    new = [make_row(f"n{i}", "app-a", ts=f"2026-09-20T12:00:0{i}+00:00") for i in range(3)]
+    since = parse_since("2026-09-10T00:00:00+00:00")
+    cut, cut_excluded = asyncio.run(simulate(
+        [old] + new, make_snapshot(), {"schema": P}, base=BASE_SETTINGS, since=since))
+    pre, _ = _run(new, make_snapshot(), {"schema": P})
+    assert [(r.row["request_id"], r.current, r.proposed) for r in cut] == \
+           [(r.row["request_id"], r.current, r.proposed) for r in pre]
+    assert all(r.current.latency_ms == 120.0 for r in cut)      # not the old row's 99999
+    assert cut_excluded["before_since"] == 1
+
+
+@pytest.mark.parametrize("flag", ["2026-09-20T12:00:00Z", "2026-09-20T12:00:00+00:00",
+                                  "2026-09-20T12:00:00", "2026-09-20T14:00:00+02:00"])
+@pytest.mark.parametrize("row_ts,kept", [
+    ("2026-09-20T12:00:00Z", True),             # Z suffix, on the boundary
+    ("2026-09-20T12:00:00", True),              # naive is read as UTC
+    ("2026-09-20T13:59:59+02:00", False),       # 11:59:59 UTC
+    ("2026-09-20T07:00:00-05:00", True),        # 12:00:00 UTC
+    ("2026-09-20T11:59:59.999999+00:00", False),
+])
+def test_since_compares_instants_not_strings(flag, row_ts, kept):          # Review Focus 1
+    rows, excluded = select_rows([make_row("r", "app-a", ts=row_ts)], {"app-a"},
+                                 since=parse_since(flag))
+    assert (len(rows) == 1) is kept
+    assert excluded["before_since"] == (0 if kept else 1)
+
+
+def test_since_row_without_ts_is_missing_features_not_simulated():         # Review Focus 1
+    """A row with no `ts` cannot be placed in the window. Without a cut it
+    is simulated as before; with one it is counted, never guessed at."""
+    row = make_row("r", "app-a", ts=None)
+    assert len(select_rows([row], {"app-a"})[0]) == 1
+    rows, excluded = select_rows([row], {"app-a"}, since=parse_since("2026-01-01"))
+    assert rows == [] and excluded["missing_features"] == 1 and excluded["before_since"] == 0
+
+
+@pytest.mark.parametrize("bad", ["yesterday", "", "2026-13-01", "12:00"])
+def test_parse_since_rejects_non_timestamps(bad):
+    with pytest.raises(ValueError):
+        parse_since(bad)
````

Patch `tests/eval/test_shadow.py`:

````diff
diff --git a/tests/eval/test_shadow.py b/tests/eval/test_shadow.py
index e814b67..2619391 100644
--- a/tests/eval/test_shadow.py
+++ b/tests/eval/test_shadow.py
@@ -622,3 +622,15 @@ def test_zero_saving_never_renders_as_negative_zero():
     from app.eval.outcomes import _round6
     assert math.copysign(1.0, _round6(-0.0)) == 1.0
     assert math.copysign(1.0, _round6({"x": [-1e-9]})["x"][0]) == 1.0
+
+
+# --- P3c-2 Task 1: --since reaches shadow prices too (spec §6) ----------------
+
+def test_shadow_prices_respects_since():
+    from app.eval.simulate import parse_since
+    snap = make_snapshot(apps=[app_entry("a", allowed_tiers=["frontier"])])
+    rows = [make_row("old", "a", ts="2026-09-01T00:00:00+00:00"),
+            make_row("new", "a", ts="2026-09-20T12:00:00+00:00")]
+    assert _entry(_run(rows, snap), "allowed_tiers", app="a")["rows_in_scope"] == 2
+    cut = _run(rows, snap, since=parse_since("2026-09-10T00:00:00Z"))
+    assert _entry(cut, "allowed_tiers", app="a")["rows_in_scope"] == 1
````

Patch `tests/eval/test_policy_simulate_cli.py`:

````diff
diff --git a/tests/eval/test_policy_simulate_cli.py b/tests/eval/test_policy_simulate_cli.py
index 3102385..c99a420 100644
--- a/tests/eval/test_policy_simulate_cli.py
+++ b/tests/eval/test_policy_simulate_cli.py
@@ -350,3 +350,46 @@ def test_cli_malformed_export_rows_are_counted(tmp_path):                 # Impo
     assert sim["rows"]["simulated"] == 1
     assert sim["rows"]["excluded"]["missing_features"] == 3
     assert "line 5" in proc.stderr          # the non-object line is a skipped-line warning
+
+
+# --- P3c-2 Task 1: --since (spec §6, S10) -------------------------------------
+
+def _two_eras():
+    return [_line(make_row("old", "app-a", ts="2026-09-01T00:00:00+00:00")),
+            _line(make_row("new", "app-a", ts="2026-09-20T12:00:00+00:00"))]
+
+
+def test_cli_since_cuts_the_export_and_records_the_cut(tmp_path):         # S10
+    proc, out = _run_cli(tmp_path, _two_eras(), make_snapshot(), {"schema": P},
+                         "--since", "2026-09-10T00:00:00Z")
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    assert sim["rows"]["simulated"] == 1
+    assert sim["rows"]["excluded"]["before_since"] == 1
+    assert sim["inputs"]["since"] == "2026-09-10T00:00:00+00:00"
+    assert "| before_since | 1 |" in (out / "report.md").read_text()
+    # a different cut is a different simulation
+    proc2, _ = _run_cli(tmp_path, _two_eras(), make_snapshot(), {"schema": P})
+    sim2 = json.loads((out / "simulation.json").read_text())
+    assert sim2["inputs"]["since"] is None and sim2["simulation_id"] != sim["simulation_id"]
+
+
+def test_cli_since_applies_to_shadow_prices_mode(tmp_path):
+    from tests.eval.sim_fixtures import app_entry
+    snap = make_snapshot(apps=[app_entry("app-a", allowed_tiers=["frontier"])])
+    proc, out = _run_cli_no_proposal(tmp_path, _two_eras(), snap, "--shadow-prices",
+                                     "--since", "2026-09-10T00:00:00Z")
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    (entry,) = [e for e in sim["shadow_prices"]["constraints"]
+                if e["requirement"] == "allowed_tiers"]
+    assert entry["rows_in_scope"] == 1
+    assert sim["inputs"]["since"] == "2026-09-10T00:00:00+00:00"
+
+
+def test_cli_bad_since_exits_2(tmp_path):
+    proc, out = _run_cli(tmp_path, _two_eras(), make_snapshot(), {"schema": P},
+                         "--since", "last tuesday")
+    assert proc.returncode == 2, proc.stderr
+    assert "--since" in proc.stderr and "Traceback" not in proc.stderr
+    assert not out.exists()
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_simulate.py tests/eval/test_shadow.py tests/eval/test_policy_simulate_cli.py -p no:warnings
```

Expected: collection error in `tests/eval/test_simulate.py`: `ImportError: cannot import name 'EXCLUSION_REASONS'`… then, once that import exists, `cannot import name 'parse_since'`. The new CLI tests fail with exit 2 (`unrecognized arguments: --since`).

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 21ade12..53f38e7 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -33,7 +33,7 @@ import json
 import statistics
 from collections import Counter
 from dataclasses import dataclass
-from datetime import datetime
+from datetime import datetime, timezone
 
 from app.config.schema import Settings
 from app.errors import ContextWindowExceeded, NoEligibleCandidate
@@ -49,7 +49,10 @@ from app.routing.router import expected_output_tokens
 SIMULATED_STATUSES = frozenset({"ok", "backend_failed", "verify_failed",
                                 "no_eligible", "context_overflow"})
 
-EXCLUSION_REASONS = ("cache_hit", "unknown_status", "unknown_app", "missing_features")
+# `before_since` is checked first, so a row older than `--since` never reaches
+# the other checks (P3c-2 spec §6).
+EXCLUSION_REASONS = ("before_since", "cache_hit", "unknown_status", "unknown_app",
+                     "missing_features")
 
 
 @dataclass(frozen=True)
@@ -116,10 +119,41 @@ def _has_features(row: dict) -> bool:
             and _valid_ts(row.get("ts")))
 
 
-def select_rows(records: list[dict], app_ids: set[str]) -> tuple[list[dict], dict[str, int]]:
+def parse_since(text: str) -> datetime:
+    """A `--since` value as an aware UTC datetime. Accepts what
+    `datetime.fromisoformat` accepts plus a trailing `Z`; a value with no
+    offset is read as UTC. Raises `ValueError` on anything else."""
+    return _utc(_parse_ts(text))
+
+
+def _utc(ts: datetime) -> datetime:
+    """`ts` as an aware UTC datetime; a naive value is read as UTC."""
+    if ts.tzinfo is None:
+        return ts.replace(tzinfo=timezone.utc)
+    return ts.astimezone(timezone.utc)
+
+
+def _before_since(row: dict, since: datetime | None) -> bool:
+    """True when `row` carries a parseable `ts` strictly before `since`. A
+    row with no usable `ts` is never `before_since` -- `select_rows` counts
+    it under `missing_features` instead when a cut was asked for."""
+    ts = row.get("ts")
+    if since is None or not isinstance(ts, str):
+        return False
+    try:
+        return _utc(_parse_ts(ts)) < since
+    except ValueError:
+        return False
+
+
+def select_rows(records: list[dict], app_ids: set[str], *,
+                since: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
     """The rows to simulate, sorted by `(ts, request_id)`, plus a count per
     exclusion reason. Every excluded row is counted under exactly one reason
     (first match, in `EXCLUSION_REASONS` order) -- never silently dropped.
+    `before_since` (P3c-2) is a row whose `ts` is strictly before `since`;
+    with a `since`, a row that has no `ts` cannot be placed in the window and
+    is counted under `missing_features`.
     `missing_features` covers a missing or mistyped `request_id`,
     `complexity`, `prompt_tokens`, `completion_tokens`, `workload`,
     `attempts`, `ts`, or `detected_entity_types` (list of strings, or null
@@ -132,13 +166,15 @@ def select_rows(records: list[dict], app_ids: set[str]) -> tuple[list[dict], dic
     for row in records:
         status = row.get("status")
         app_id = row.get("application_id")
-        if row.get("cache_hit"):
+        if _before_since(row, since):
+            excluded["before_since"] += 1
+        elif row.get("cache_hit"):
             excluded["cache_hit"] += 1
         elif not isinstance(status, str) or status not in SIMULATED_STATUSES:
             excluded["unknown_status"] += 1
         elif not isinstance(app_id, str) or app_id not in app_ids:
             excluded["unknown_app"] += 1
-        elif not _has_features(row):
+        elif not _has_features(row) or (since is not None and row.get("ts") is None):
             excluded["missing_features"] += 1
         else:
             kept.append(row)
@@ -236,14 +272,17 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
                      cost, latency, latency_source, q.score, q.source)
 
 
-def run_rows(records: list[dict], current: Scenario,
-             proposed: Scenario) -> tuple[list[RowResult], dict[str, int]]:
+def run_rows(records: list[dict], current: Scenario, proposed: Scenario, *,
+             since: datetime | None = None) -> tuple[list[RowResult], dict[str, int]]:
     """Simulate every selectable row under both scenarios. Observed latencies
-    come from ALL records (not only the selected ones) and are shared by both
-    arms; only apps present in both scenarios are simulated."""
-    observed = observed_latencies(records)
+    come from every record in the window (not only the selected ones) and
+    are shared by both arms; only apps present in both scenarios are
+    simulated. `since` cuts the export first: a row before it is counted
+    `before_since` and feeds nothing, so the result equals a run over an
+    export that never held those rows."""
+    observed = observed_latencies([r for r in records if not _before_since(r, since)])
     app_ids = set(current.apps) & set(proposed.apps)
-    rows, excluded = select_rows(records, app_ids)
+    rows, excluded = select_rows(records, app_ids, since=since)
     results = [RowResult(row, simulate_row(current, row, observed),
                          simulate_row(proposed, row, observed))
                for row in rows]
@@ -251,14 +290,15 @@ def run_rows(records: list[dict], current: Scenario,
 
 
 async def simulate(records: list[dict], snapshot: dict, proposal: dict, *,
-                   base: Settings) -> tuple[list[RowResult], dict[str, int]]:
+                   base: Settings, since: datetime | None = None
+                   ) -> tuple[list[RowResult], dict[str, int]]:
     """Build both scenarios from one base `Settings`, replay, close both."""
     proposed_doc = apply_proposal(snapshot, proposal, base=base)
     current = await build_scenario(snapshot, base=base)
     try:
         proposed = await build_scenario(proposed_doc, base=base)
         try:
-            return run_rows(records, current, proposed)
+            return run_rows(records, current, proposed, since=since)
         finally:
             await close_scenario(proposed)
     finally:
````

Patch `app/eval/shadow.py`:

````diff
diff --git a/app/eval/shadow.py b/app/eval/shadow.py
index b92a32f..c10a96c 100644
--- a/app/eval/shadow.py
+++ b/app/eval/shadow.py
@@ -28,6 +28,7 @@ from __future__ import annotations
 
 import copy
 from dataclasses import dataclass
+from datetime import datetime
 
 from collections import Counter
 
@@ -36,6 +37,7 @@ from app.eval.outcomes import _fmt, _round6
 from app.eval.scenario import app_rules, build_scenario, close_scenario
 from app.eval.simulate import (
     RowResult,
+    _before_since,
     _bucket_stats,
     _moves,
     _newly_rejected,
@@ -432,7 +434,8 @@ def _rank_key(entry: dict) -> tuple:
 
 
 async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settings,
-                        cap: int = 50, baseline_label: str = "snapshot") -> dict:
+                        cap: int = 50, baseline_label: str = "snapshot",
+                        since: datetime | None = None) -> dict:
     """The spec §5 `shadow_prices` block for `baseline_doc` (the snapshot,
     or the snapshot with a proposal already applied -- the caller decides and
     says which in `baseline_label`).
@@ -444,11 +447,12 @@ async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settin
     A baseline-routed row the relaxation rejects is reported
     (`newly_rejected`, `newly_rejected_reasons`), not raised. A relaxation
     is binding when it changes a route, avoids a rejection or newly rejects
-    a row; the rest are counted as `non_binding`, not listed."""
-    observed = observed_latencies(records)
+    a row; the rest are counted as `non_binding`, not listed. `since` cuts
+    the export exactly as `simulate.run_rows` does (P3c-2)."""
+    observed = observed_latencies([r for r in records if not _before_since(r, since)])
     baseline = await build_scenario(baseline_doc, base=base)
     try:
-        rows, _excluded = select_rows(records, set(baseline.apps))
+        rows, _excluded = select_rows(records, set(baseline.apps), since=since)
         base_arm = {row["request_id"]: simulate_row(baseline, row, observed) for row in rows}
         # The export window spans every selected row, whichever app sent it
         # (spec §4); one value serves every relaxation's per-day figure.
````

Patch `scripts/policy_simulate.py`:

````diff
diff --git a/scripts/policy_simulate.py b/scripts/policy_simulate.py
index bbc8f10..a570eef 100644
--- a/scripts/policy_simulate.py
+++ b/scripts/policy_simulate.py
@@ -45,7 +45,9 @@ from app.eval.proposal import (  # noqa: E402
 )
 from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
 from app.eval.shadow import ShadowInvariantError, render_shadow_section, shadow_prices  # noqa: E402
-from app.eval.simulate import REPORT_CAVEAT, render_report, simulate, summarize  # noqa: E402
+from app.eval.simulate import (  # noqa: E402
+    REPORT_CAVEAT, parse_since, render_report, simulate, summarize,
+)
 
 
 def _parser() -> argparse.ArgumentParser:
@@ -66,6 +68,9 @@ def _parser() -> argparse.ArgumentParser:
                    help="price what each constraint costs (P3c-1); makes --proposal optional")
     p.add_argument("--max-relaxations", type=int, default=50,
                    help="cap on relaxations evaluated with --shadow-prices (default: 50)")
+    p.add_argument("--since", default=None, metavar="TS",
+                   help="ISO-8601 UTC timestamp; rows before it are excluded "
+                        "(counted as before_since). Applies to every mode.")
     return p
 
 
@@ -80,6 +85,15 @@ def main(argv: list[str] | None = None) -> int:
         print("error: --max-relaxations must be >= 1", file=sys.stderr)
         return 2
 
+    since = None
+    if args.since is not None:
+        try:
+            since = parse_since(args.since)
+        except ValueError:
+            print(f"error: --since {args.since!r}: not an ISO-8601 timestamp",
+                  file=sys.stderr)
+            return 2
+
     try:
         records, warnings = load_export(args.export)
     except (ValueError, OSError, json.JSONDecodeError) as exc:
@@ -113,7 +127,8 @@ def main(argv: list[str] | None = None) -> int:
     try:
         results = excluded = None
         if args.proposal:
-            results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base))
+            results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base,
+                                                     since=since))
 
         shadow_block = None
         if args.shadow_prices:
@@ -128,7 +143,7 @@ def main(argv: list[str] | None = None) -> int:
                 baseline_label = "snapshot"
             shadow_block = asyncio.run(shadow_prices(
                 records, baseline_doc, base=base, cap=args.max_relaxations,
-                baseline_label=baseline_label))
+                baseline_label=baseline_label, since=since))
     except (SnapshotError, ProposalError) as exc:
         # Spec §8 governs over the narrower "catch ProposalError" reading:
         # exit 2 is "an input file failed to load OR VALIDATE" -- a
@@ -165,6 +180,7 @@ def main(argv: list[str] | None = None) -> int:
             "registry_version": snapshot["registry_version"],
             "snapshot_generated_at": snapshot["generated_at"],
             "serving_policy": serving_policy,
+            "since": since.isoformat() if since is not None else None,
         }
         sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal))
         report_text = render_report(sim)
@@ -188,6 +204,7 @@ def main(argv: list[str] | None = None) -> int:
             "registry_version": snapshot["registry_version"],
             "snapshot_generated_at": snapshot["generated_at"],
             "serving_policy": serving_policy,
+            "since": since.isoformat() if since is not None else None,
         }
         sim = {"schema": "routeforge.simulation/v1", "inputs": inputs,
               "shadow_prices": shadow_block}
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_simulate.py tests/eval/test_shadow.py tests/eval/test_policy_simulate_cli.py -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_simulate.py tests/eval/test_shadow.py tests/eval/test_policy_simulate_cli.py app/eval/simulate.py app/eval/shadow.py scripts/policy_simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): --since row cut with a before_since exclusion reason (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 2: `--min-agreement` gate and exit code 4 (spec §6, S11)

**Files:**
- Modify: `app/eval/simulate.py` (add `baseline_check` and `agreement_ok` after `simulate`)
- Modify: `scripts/policy_simulate.py` (`_parser`, `main`; the summary is now built before the output directory)
- Test: `tests/eval/test_simulate.py`, `tests/eval/test_policy_simulate_cli.py`

**Interfaces:**
- Consumes: Task 1's `select_rows(..., since=)`, `_before_since`, `simulate(..., since=)`; the existing `_baseline(results) -> dict` with keys `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`.
- Produces:
  - `async baseline_check(records: list[dict], snapshot: dict, *, base: Settings, since: datetime | None = None) -> dict` — the `_baseline` block for the snapshot as exported, rounded.
  - `agreement_ok(baseline: dict, minimum: float) -> bool` — False when `agreement` is `None` or `< minimum`.
  - CLI flag `--min-agreement X` (`0 ≤ X ≤ 1`, else exit 2); exit 4 with nothing written.
  - `_below_min_agreement(baseline: dict, minimum: float) -> int` in the script (prints to stderr, returns 4). Task 10 reuses it.

**Rules.** The gate runs before the shadow-price pass and before the output directory is created. With `--proposal`, agreement is read from `summarize()`'s `baseline`. With `--shadow-prices` alone, `baseline_check` replays the snapshot once. Exit 4 takes precedence over exit 3. The stderr line is `policy_simulate: baseline agreement <x> over <n> compared rows is below --min-agreement <X>; nothing written`, with `n/a` when agreement is `None`.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_simulate.py`:

````diff
diff --git a/tests/eval/test_simulate.py b/tests/eval/test_simulate.py
index dd351ae..5511a00 100644
--- a/tests/eval/test_simulate.py
+++ b/tests/eval/test_simulate.py
@@ -6,9 +6,10 @@ import asyncio
 import pytest
 from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
 
-from app.eval.simulate import (EXCLUSION_REASONS, REPORT_CAVEAT, observed_latencies,
-                               parse_since, render_report, select_rows, simulate,
-                               simulate_row, summarize)
+from app.eval.simulate import (EXCLUSION_REASONS, REPORT_CAVEAT, agreement_ok,
+                               baseline_check, observed_latencies, parse_since,
+                               render_report, select_rows, simulate, simulate_row,
+                               summarize)
 from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                      onboarded_row)
 
@@ -527,3 +528,22 @@ def test_since_row_without_ts_is_missing_features_not_simulated():         # Rev
 def test_parse_since_rejects_non_timestamps(bad):
     with pytest.raises(ValueError):
         parse_since(bad)
+
+
+# --- P3c-2 Task 2: the agreement gate's inputs (spec §6) ----------------------
+
+def test_baseline_check_equals_the_summarized_baseline():
+    """`baseline_check` (no proposal) and `summarize()["baseline"]` (empty
+    proposal) are the same block: both replay the snapshot as exported."""
+    rows = _rows(4) + [make_row("d", "app-a", attempts=["local-big"], chosen="local-big")]
+    sim = summarize(*_run(rows, make_snapshot(), {"schema": P}), inputs=_INPUTS, edits={})
+    assert asyncio.run(baseline_check(rows, make_snapshot(), base=BASE_SETTINGS)) \
+        == sim["baseline"]
+
+
+@pytest.mark.parametrize("agreement,minimum,ok", [
+    (1.0, 1.0, True), (0.9, 0.9, True), (0.899999, 0.9, False),
+    (0.0, 0.0, True), (None, 0.0, False),
+])
+def test_agreement_ok(agreement, minimum, ok):
+    assert agreement_ok({"agreement": agreement, "rows_compared": 0}, minimum) is ok
````

Patch `tests/eval/test_policy_simulate_cli.py`:

````diff
diff --git a/tests/eval/test_policy_simulate_cli.py b/tests/eval/test_policy_simulate_cli.py
index c99a420..db938d5 100644
--- a/tests/eval/test_policy_simulate_cli.py
+++ b/tests/eval/test_policy_simulate_cli.py
@@ -393,3 +393,65 @@ def test_cli_bad_since_exits_2(tmp_path):
     assert proc.returncode == 2, proc.stderr
     assert "--since" in proc.stderr and "Traceback" not in proc.stderr
     assert not out.exists()
+
+
+# --- P3c-2 Task 2: --min-agreement (spec §6, S11) -----------------------------
+
+def _disagreeing_lines(n=4):
+    """Unpinned rows the record says local-big served; replay sends a
+    0.1-complexity row to local-cheap, so baseline agreement is 0.0."""
+    return [_line(make_row(f"r{i}", "app-a", attempts=["local-big"], chosen="local-big"))
+            for i in range(n)]
+
+
+def test_s11_min_agreement_exits_4_and_writes_nothing(tmp_path):          # S11
+    proc, out = _run_cli(tmp_path, _disagreeing_lines(), make_snapshot(), {"schema": P},
+                         "--min-agreement", "0.9")
+    assert proc.returncode == 4, proc.stderr
+    assert "baseline agreement 0 over 4 compared rows" in proc.stderr
+    assert "--min-agreement 0.9" in proc.stderr
+    assert not out.exists()                       # no directory, no files
+
+
+def test_min_agreement_met_writes_as_usual(tmp_path):
+    lines = [_line(make_row(f"r{i}", "app-a")) for i in range(3)]      # agreement 1.0
+    proc, out = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P},
+                         "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    assert (out / "simulation.json").exists()
+
+
+def test_min_agreement_with_no_comparable_rows_exits_4(tmp_path):         # Review Focus 5
+    """No `ok` row with a recorded attempt: agreement is None. Nothing
+    vouches for the replay, so the gate refuses -- it never reads None as 1."""
+    lines = [_line(make_row("r1", "app-a", status="backend_failed"))]
+    proc, out = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P},
+                         "--min-agreement", "0")
+    assert proc.returncode == 4, proc.stderr
+    assert "baseline agreement n/a over 0 compared rows" in proc.stderr
+    assert not out.exists()
+
+
+def test_min_agreement_gates_shadow_prices_without_a_proposal(tmp_path):
+    proc, out = _run_cli_no_proposal(tmp_path, _disagreeing_lines(), make_snapshot(),
+                                     "--shadow-prices", "--min-agreement", "0.5")
+    assert proc.returncode == 4, proc.stderr
+    assert not out.exists()
+    ok, out2 = _run_cli_no_proposal(tmp_path, _disagreeing_lines(), make_snapshot(),
+                                    "--shadow-prices", "--min-agreement", "0")
+    assert ok.returncode == 0, ok.stderr
+
+
+def test_min_agreement_wins_over_fail_on_new_rejections(tmp_path):
+    prop = {"schema": P, "endpoints": {"remove": ["local-cheap"]}}
+    proc, out = _run_cli(tmp_path, _disagreeing_lines(), make_snapshot(), prop,
+                         "--min-agreement", "0.9", "--fail-on-new-rejections")
+    assert proc.returncode == 4 and not out.exists()
+
+
+@pytest.mark.parametrize("bad", ["-0.1", "1.5", "nan"])
+def test_min_agreement_out_of_range_exits_2(tmp_path, bad):
+    proc, out = _run_cli(tmp_path, [], make_snapshot(), {"schema": P},
+                         f"--min-agreement={bad}")
+    assert proc.returncode == 2, proc.stderr
+    assert "--min-agreement" in proc.stderr and not out.exists()
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_simulate.py tests/eval/test_policy_simulate_cli.py -p no:warnings
```

Expected: `ImportError: cannot import name 'agreement_ok'` in `tests/eval/test_simulate.py`; the new CLI tests fail with exit 2 (`unrecognized arguments: --min-agreement`).

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 53f38e7..5e7cb5a 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -305,6 +305,31 @@ async def simulate(records: list[dict], snapshot: dict, proposal: dict, *,
         await close_scenario(current)
 
 
+async def baseline_check(records: list[dict], snapshot: dict, *, base: Settings,
+                         since: datetime | None = None) -> dict:
+    """The fidelity block (`_baseline`) for the snapshot AS EXPORTED, with no
+    proposal: build the one scenario, replay each selectable row once, close.
+    For a caller that has no `summarize()` output to read the block from
+    (`--shadow-prices` without `--proposal`) but still has to gate on
+    agreement (P3c-2 spec §6, `--min-agreement`)."""
+    current = await build_scenario(snapshot, base=base)
+    try:
+        observed = observed_latencies([r for r in records if not _before_since(r, since)])
+        rows, _excluded = select_rows(records, set(current.apps), since=since)
+        arms = [simulate_row(current, row, observed) for row in rows]
+    finally:
+        await close_scenario(current)
+    return _round6(_baseline([RowResult(row, arm, arm) for row, arm in zip(rows, arms)]))
+
+
+def agreement_ok(baseline: dict, minimum: float) -> bool:
+    """The `--min-agreement` gate: False when the baseline has no comparable
+    row (`agreement` is None -- nothing vouches for the replay) or agrees
+    with the record less often than `minimum`."""
+    agreement = baseline["agreement"]
+    return agreement is not None and agreement >= minimum
+
+
 # --- summary and report (spec §8) -------------------------------------------
 
 #: The report's opening blockquote, verbatim from spec §8, one string per
````

Patch `scripts/policy_simulate.py`:

````diff
diff --git a/scripts/policy_simulate.py b/scripts/policy_simulate.py
index a570eef..c094a6f 100644
--- a/scripts/policy_simulate.py
+++ b/scripts/policy_simulate.py
@@ -19,7 +19,8 @@ File-based only: no DB, no `app.api`, no serving import
 
 Exit codes: 0 simulation written; 2 an input file failed to load, validate
 or apply; 3 the proposal introduces new rejections and
-`--fail-on-new-rejections` is set (for CI/GitOps gating).
+`--fail-on-new-rejections` is set (for CI/GitOps gating); 4 the baseline's
+agreement with the record is below `--min-agreement` (nothing is written).
 """
 from __future__ import annotations
 
@@ -46,7 +47,8 @@ from app.eval.proposal import (  # noqa: E402
 from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
 from app.eval.shadow import ShadowInvariantError, render_shadow_section, shadow_prices  # noqa: E402
 from app.eval.simulate import (  # noqa: E402
-    REPORT_CAVEAT, parse_since, render_report, simulate, summarize,
+    REPORT_CAVEAT, agreement_ok, baseline_check, parse_since, render_report, simulate,
+    summarize,
 )
 
 
@@ -71,9 +73,22 @@ def _parser() -> argparse.ArgumentParser:
     p.add_argument("--since", default=None, metavar="TS",
                    help="ISO-8601 UTC timestamp; rows before it are excluded "
                         "(counted as before_since). Applies to every mode.")
+    p.add_argument("--min-agreement", type=float, default=None, metavar="X",
+                   help="0..1; exit 4 and write nothing when the baseline's agreement "
+                        "with the recorded first attempts is below X (or not computable)")
     return p
 
 
+def _below_min_agreement(baseline: dict, minimum: float) -> int:
+    """Print why the `--min-agreement` gate refused the run; return exit 4."""
+    agreement = baseline["agreement"]
+    shown = "n/a" if agreement is None else f"{agreement:.6g}"
+    print(f"policy_simulate: baseline agreement {shown} over "
+          f"{baseline['rows_compared']} compared rows is below --min-agreement "
+          f"{minimum:g}; nothing written", file=sys.stderr)
+    return 4
+
+
 def main(argv: list[str] | None = None) -> int:
     parser = _parser()
     args = parser.parse_args(argv)
@@ -85,6 +100,11 @@ def main(argv: list[str] | None = None) -> int:
         print("error: --max-relaxations must be >= 1", file=sys.stderr)
         return 2
 
+    # `not (0 <= x <= 1)` also refuses NaN, which compares false both ways.
+    if args.min_agreement is not None and not (0.0 <= args.min_agreement <= 1.0):
+        print("error: --min-agreement must be between 0 and 1", file=sys.stderr)
+        return 2
+
     since = None
     if args.since is not None:
         try:
@@ -124,11 +144,38 @@ def main(argv: list[str] | None = None) -> int:
         print(f"error: --config-dir {args.config_dir}: {exc}", file=sys.stderr)
         return 2
 
+    # The deployment posture every app was compiled under -- the SNAPSHOT's
+    # values, never this machine's local config.
+    serving_policy = {k: snapshot["settings"]["serving_policy"][k]
+                      for k in SERVING_POLICY_FIELDS}
+    inputs = {
+        "export_sha256": export_sha256,
+        "snapshot_sha256": snapshot_sha256,
+        "proposal_sha256": proposal_sha256,
+        "proposal_title": proposal.get("title") if proposal else None,
+        "routing_policy_version": snapshot["routing_policy_version"],
+        "registry_version": snapshot["registry_version"],
+        "snapshot_generated_at": snapshot["generated_at"],
+        "serving_policy": serving_policy,
+        "since": since.isoformat() if since is not None else None,
+    }
+
     try:
-        results = excluded = None
+        sim = baseline = None
         if args.proposal:
             results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base,
                                                      since=since))
+            sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal))
+            baseline = sim["baseline"]
+        elif args.min_agreement is not None:
+            # `--shadow-prices` alone has no `summarize()` output to read the
+            # fidelity block from, so the gate replays the snapshot once.
+            baseline = asyncio.run(baseline_check(records, snapshot, base=base, since=since))
+
+        # The gate runs before the (expensive) shadow pass and before any
+        # output directory or file exists (P3c-2 spec §6).
+        if args.min_agreement is not None and not agreement_ok(baseline, args.min_agreement):
+            return _below_min_agreement(baseline, args.min_agreement)
 
         shadow_block = None
         if args.shadow_prices:
@@ -160,29 +207,12 @@ def main(argv: list[str] | None = None) -> int:
         print(f"error: internal invariant: {exc}", file=sys.stderr)
         return 2
 
-    # The deployment posture every app was compiled under -- the SNAPSHOT's
-    # values, never this machine's local config.
-    serving_policy = {k: snapshot["settings"]["serving_policy"][k]
-                      for k in SERVING_POLICY_FIELDS}
-
     out = Path(args.out)
     out.mkdir(parents=True, exist_ok=True)
     sim_path = out / "simulation.json"
     report_path = out / "report.md"
 
     if args.proposal:
-        inputs = {
-            "export_sha256": export_sha256,
-            "snapshot_sha256": snapshot_sha256,
-            "proposal_sha256": proposal_sha256,
-            "proposal_title": proposal.get("title"),
-            "routing_policy_version": snapshot["routing_policy_version"],
-            "registry_version": snapshot["registry_version"],
-            "snapshot_generated_at": snapshot["generated_at"],
-            "serving_policy": serving_policy,
-            "since": since.isoformat() if since is not None else None,
-        }
-        sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal))
         report_text = render_report(sim)
         if shadow_block is not None:
             sim["shadow_prices"] = shadow_block
@@ -195,17 +225,6 @@ def main(argv: list[str] | None = None) -> int:
         # proposal sections do not exist (spec §5), so `simulation.json` is
         # just the inputs (proposal fields null) plus the shadow block, and
         # `report.md` is the P3a caveat followed by the shadow section.
-        inputs = {
-            "export_sha256": export_sha256,
-            "snapshot_sha256": snapshot_sha256,
-            "proposal_sha256": None,
-            "proposal_title": None,
-            "routing_policy_version": snapshot["routing_policy_version"],
-            "registry_version": snapshot["registry_version"],
-            "snapshot_generated_at": snapshot["generated_at"],
-            "serving_policy": serving_policy,
-            "since": since.isoformat() if since is not None else None,
-        }
         sim = {"schema": "routeforge.simulation/v1", "inputs": inputs,
               "shadow_prices": shadow_block}
         caveat = "\n".join("> " + line for line in REPORT_CAVEAT.split("\n"))
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_simulate.py tests/eval/test_policy_simulate_cli.py -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_simulate.py tests/eval/test_policy_simulate_cli.py app/eval/simulate.py scripts/policy_simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): --min-agreement gate, exit 4 before any file is written (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 3: Proposal `workload` block and the `assumptions` block (spec §4.1, §4.3)

**Files:**
- Modify: `app/eval/proposal.py` (`_TOP_KEYS` line 33, `_validate_keys` lines 114–153, `apply_proposal` lines 416–586; new `_validate_workload_fields`, `assumptions_block`)
- Modify: `app/eval/scenario.py` (`ALL_APPS` after `SCHEMA_MAJOR`; `load_snapshot` lines 59–72)
- Test: create `tests/eval/test_proposal_workload.py`

**Interfaces:**
- Consumes: the existing `ProposalError(where, message)`, `SnapshotError(where, message)`, `_finite_number`, `_opt_dict`, `_check_keys`, `apply_proposal(snapshot, proposal, *, base=None) -> dict`.
- Produces:
  - `app.eval.scenario.ALL_APPS = "*"` — the key a global workload value is stored under.
  - `app.eval.proposal.assumptions_block(proposal: dict) -> dict | None` — `{"token_scale": {...}, "cached_share": {...}, "latency_ms": {...}}` with all three keys always present, values `float`, or `None` when the proposal sets none. Task 5 fills `latency_ms`.
  - `apply_proposal` writes that block as the applied document's top-level `assumptions`, absent when `None`.
  - `apply_proposal` raises `SnapshotError("assumptions", ...)` when the input already has the block; `load_snapshot` raises the same for a file that carries it.
  - `normalized_edits` echoes `workload` unchanged (no code change needed).

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_proposal_workload.py`:

````python
"""P3c-2: the `workload` and `latency_ms` proposal change types (spec §4)."""
from __future__ import annotations

import copy
import json

import pytest

from app.eval.proposal import (ALL_APPS, ProposalError, apply_proposal, assumptions_block,
                               load_proposal, normalized_edits)
from app.eval.scenario import SnapshotError, load_snapshot
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_snapshot

P = "routeforge.policy-proposal/v1"


def _apply(prop, snap=None):
    return apply_proposal(snap or make_snapshot(), prop, base=BASE_SETTINGS)


# --- Task 3: validation and the assumptions block (spec §4.1, §4.3) ----------

def test_workload_writes_the_assumptions_block():
    snap = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")])
    out = _apply({"schema": P, "workload": {
        "token_scale": 0.6, "cached_share": 0.25,
        "apps": {"app-b": {"token_scale": 2, "cached_share": 0.5}}}}, snap)
    assert out["assumptions"] == {
        "token_scale": {ALL_APPS: 0.6, "app-b": 2.0},
        "cached_share": {ALL_APPS: 0.25, "app-b": 0.5},
        "latency_ms": {}}
    # nothing else moved, and the input was not mutated
    del out["assumptions"]
    assert out == snap and "assumptions" not in snap


def test_no_workload_means_no_block():
    assert "assumptions" not in _apply({"schema": P})
    assert "assumptions" not in _apply({"schema": P, "workload": {}})
    assert "assumptions" not in _apply({"schema": P, "workload": {"apps": {"app-a": {}}}})
    assert assumptions_block({"schema": P}) is None


def test_per_app_only_and_global_only_blocks():
    out = _apply({"schema": P, "workload": {"apps": {"app-a": {"cached_share": 1}}}})
    assert out["assumptions"] == {"token_scale": {}, "cached_share": {"app-a": 1.0},
                                  "latency_ms": {}}
    out = _apply({"schema": P, "workload": {"token_scale": 10}})
    assert out["assumptions"]["token_scale"] == {ALL_APPS: 10.0}


def test_block_matches_assumptions_block_and_edits_echo_workload():
    prop = {"schema": P, "title": "t", "workload": {"token_scale": 0.5}}
    assert _apply(prop)["assumptions"] == assumptions_block(prop)
    assert normalized_edits(prop) == {"workload": {"token_scale": 0.5}}


@pytest.mark.parametrize("workload,where", [
    ({"token_scale": 0}, "workload.token_scale"),
    ({"token_scale": -1}, "workload.token_scale"),
    ({"token_scale": 10.0001}, "workload.token_scale"),
    ({"token_scale": "0.5"}, "workload.token_scale"),
    ({"token_scale": True}, "workload.token_scale"),          # a bool is not a number
    ({"token_scale": None}, "workload.token_scale"),
    ({"token_scale": float("nan")}, "workload.token_scale"),
    ({"token_scale": float("inf")}, "workload.token_scale"),
    ({"cached_share": -0.01}, "workload.cached_share"),
    ({"cached_share": 1.01}, "workload.cached_share"),
    ({"cached_share": False}, "workload.cached_share"),
    ({"cached_share": [0.5]}, "workload.cached_share"),
    ({"cached_share": float("nan")}, "workload.cached_share"),
    ({"tokenscale": 0.5}, "workload.tokenscale"),              # unknown key
    ({"apps": []}, "workload.apps"),
    ({"apps": {"app-a": 0.5}}, "workload.apps.app-a"),
    ({"apps": {"app-a": {"scale": 0.5}}}, "workload.apps.app-a.scale"),
    ({"apps": {"app-a": {"token_scale": 11}}}, "workload.apps.app-a.token_scale"),
    ({"apps": {"app-a": {"cached_share": 2}}}, "workload.apps.app-a.cached_share"),
    ({"apps": {"ghost": {"token_scale": 0.5}}}, "workload.apps.ghost"),
])
def test_workload_errors_name_the_dotted_path(workload, where):           # Review Focus 2
    with pytest.raises(ProposalError) as exc:
        _apply({"schema": P, "workload": workload})
    assert exc.value.where == where, str(exc.value)


def test_workload_not_an_object_is_named():
    with pytest.raises(ProposalError) as exc:
        _apply({"schema": P, "workload": 0.5})
    assert exc.value.where == "workload"


def test_boundary_values_are_accepted():
    _apply({"schema": P, "workload": {"token_scale": 10, "cached_share": 0}})
    _apply({"schema": P, "workload": {"token_scale": 1e-9, "cached_share": 1}})


def test_load_proposal_rejects_unknown_workload_key(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"schema": P, "workload": {"cache_share": 0.4}}))
    with pytest.raises(ProposalError) as exc:
        load_proposal(str(path))
    assert exc.value.where == "workload.cache_share"


def test_json_nan_literal_is_refused(tmp_path):                           # Review Focus 2
    """`json.loads` accepts the bare literal NaN; the proposal must not."""
    path = tmp_path / "p.json"
    path.write_text('{"schema": "%s", "workload": {"token_scale": NaN}}' % P)
    doc, _sha = load_proposal(str(path))
    with pytest.raises(ProposalError) as exc:
        _apply(doc)
    assert exc.value.where == "workload.token_scale"


def test_snapshot_file_with_assumptions_is_a_load_error(tmp_path):
    snap = make_snapshot()
    snap["assumptions"] = {"token_scale": {"*": 0.1}, "cached_share": {}, "latency_ms": {}}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(snap))
    with pytest.raises(SnapshotError) as exc:
        load_snapshot(str(path))
    assert exc.value.where == "assumptions"


def test_applying_a_proposal_twice_is_refused():
    once = _apply({"schema": P, "workload": {"token_scale": 0.5}})
    before = copy.deepcopy(once)
    with pytest.raises(SnapshotError) as exc:
        _apply({"schema": P, "workload": {"cached_share": 0.5}}, once)
    assert exc.value.where == "assumptions" and once == before
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_proposal_workload.py -p no:warnings
```

Expected: collection error: `ImportError: cannot import name 'ALL_APPS' from 'app.eval.proposal'`.

- [ ] **Step 3: Implement**

Patch `app/eval/proposal.py`:

````diff
diff --git a/app/eval/proposal.py b/app/eval/proposal.py
index b9eee45..3334708 100644
--- a/app/eval/proposal.py
+++ b/app/eval/proposal.py
@@ -21,7 +21,7 @@ import math
 
 from app.config.loader import load_settings
 from app.config.schema import Settings
-from app.eval.scenario import _app_policy_view, build_settings
+from app.eval.scenario import ALL_APPS, SnapshotError, _app_policy_view, build_settings
 from app.policy.compile import (_OPERATOR_NAME, PolicyViolation, compile_policy,
                                 resolve_residency)
 from app.policy.enums import PROMPT_RETENTION_VALUES
@@ -30,7 +30,8 @@ SCHEMA = "routeforge.policy-proposal/v1"
 _SCHEMA_PREFIX = "routeforge.policy-proposal/"
 _SCHEMA_MAJOR = "v1"
 
-_TOP_KEYS = frozenset({"schema", "title", "apps", "providers", "endpoints", "prices"})
+_TOP_KEYS = frozenset({"schema", "title", "apps", "providers", "endpoints", "prices",
+                       "workload"})
 _APP_EDIT_KEYS = frozenset({
     "residency", "allowed_regions", "require_no_retention", "allowed_tiers",
     "allowed_providers", "allowed_models", "max_cost_usd_per_request",
@@ -53,6 +54,9 @@ _ENDPOINT_UPDATE_KEYS = frozenset({
     "context_window", "allowed_app_ids",
 })
 _PRICE_KEYS = frozenset({"input", "output", "cached_input"})
+# P3c-2 (spec §4.1): assumptions about the traffic itself, global and per app.
+_WORKLOAD_KEYS = frozenset({"token_scale", "cached_share", "apps"})
+_WORKLOAD_APP_KEYS = frozenset({"token_scale", "cached_share"})
 _RULE_KEYS = frozenset(
     {"entities", "sensitivity", "risk_class", "residency", "require_verification"})
 
@@ -152,6 +156,11 @@ def _validate_keys(doc: dict) -> None:
     for slug, fields in _opt_dict(doc, "prices", "prices").items():
         _check_keys(fields, _PRICE_KEYS, f"prices.{slug}")
 
+    workload = _opt_dict(doc, "workload", "workload")
+    _check_keys(workload, _WORKLOAD_KEYS, "workload")
+    for app_id, fields in _opt_dict(workload, "apps", "workload.apps").items():
+        _check_keys(fields, _WORKLOAD_APP_KEYS, f"workload.apps.{app_id}")
+
 
 def load_proposal(path: str) -> tuple[dict, str]:
     """Read a policy-proposal JSON file. Returns `(doc, sha256 hex)` -- the
@@ -397,6 +406,24 @@ def _validate_endpoint_update(slug: str, fields: dict, canonical_tiers: set[str]
             f"{loc}.allowed_app_ids", fields["allowed_app_ids"], app_ids)
 
 
+# --- workload validation (P3c-2 spec §4.1) -------------------------------------
+
+def _validate_workload_fields(loc: str, fields: dict) -> None:
+    """`token_scale` in (0, 10] and `cached_share` in [0, 1], each only when
+    present. A bool, a string, NaN and +/-inf are all refused: an assumption
+    that silently reads as "no change" is worse than a load error."""
+    if "token_scale" in fields:
+        value = fields["token_scale"]
+        if not (_finite_number(value) and 0 < value <= 10):
+            raise ProposalError(f"{loc}.token_scale",
+                                "must be a finite number > 0 and <= 10")
+    if "cached_share" in fields:
+        value = fields["cached_share"]
+        if not (_finite_number(value) and 0 <= value <= 1):
+            raise ProposalError(f"{loc}.cached_share",
+                                "must be a finite number in [0, 1]")
+
+
 # --- application -----------------------------------------------------------
 
 def normalized_edits(proposal: dict) -> dict:
@@ -406,6 +433,25 @@ def normalized_edits(proposal: dict) -> dict:
     return {k: proposal[k] for k in sorted(proposal) if k not in ("schema", "title")}
 
 
+def assumptions_block(proposal: dict) -> dict | None:
+    """The `assumptions` block a VALIDATED proposal sets (P3c-2 spec §4.3):
+    `{"token_scale": {"*": x, "<app>": y}, "cached_share": {...},
+    "latency_ms": {"<slug>": ms}}`, or None when it sets none. All three keys
+    are always present in a block, so a reader never has to probe for one.
+    `apply_proposal` writes exactly this into the applied document; the CLI
+    and the scenario comparison echo it, like `normalized_edits`."""
+    workload = proposal.get("workload") or {}
+    block: dict[str, dict[str, float]] = {"token_scale": {}, "cached_share": {},
+                                          "latency_ms": {}}
+    for key in ("token_scale", "cached_share"):
+        if key in workload:
+            block[key][ALL_APPS] = float(workload[key])
+        for app_id, fields in sorted((workload.get("apps") or {}).items()):
+            if key in fields:
+                block[key][app_id] = float(fields[key])
+    return block if any(block.values()) else None
+
+
 def _load_base_settings() -> Settings:
     global _BASE_SETTINGS
     if _BASE_SETTINGS is None:
@@ -419,6 +465,14 @@ def apply_proposal(snapshot: dict, proposal: dict, *, base: Settings | None = No
     applied. Never mutates `snapshot` or `proposal`."""
     _validate_schema(proposal)
     _validate_keys(proposal)
+    # The block is only ever written below, so a document that already has
+    # one is a snapshot a proposal was already applied to (or a hand edit):
+    # applying a second proposal on top would silently merge two sets of
+    # assumptions (P3c-2 spec §4.3).
+    if "assumptions" in snapshot:
+        raise SnapshotError(
+            "assumptions", "is written only by apply_proposal; this snapshot already "
+            "has a proposal applied")
 
     settings = build_settings(snapshot, base if base is not None else _load_base_settings())
 
@@ -504,6 +558,14 @@ def apply_proposal(snapshot: dict, proposal: dict, *, base: Settings | None = No
                 f"prices.{slug}", "not an onboarded endpoint or a priced model")
         _validate_price_fields(f"prices.{slug}", fields)
 
+    # -- workload --
+    workload = proposal.get("workload") or {}
+    _validate_workload_fields("workload", workload)
+    for app_id, fields in (workload.get("apps") or {}).items():
+        if app_id not in apps_by_id:
+            raise ProposalError(f"workload.apps.{app_id}", "unknown application")
+        _validate_workload_fields(f"workload.apps.{app_id}", fields)
+
     # -- everything validated; apply on a deep copy, in spec order --
     out = copy.deepcopy(snapshot)
 
@@ -583,4 +645,10 @@ def apply_proposal(snapshot: dict, proposal: dict, *, base: Settings | None = No
     if add_list or remove_list:
         out["onboarded"] = sorted(onboarded_out.values(), key=lambda o: o["slug"])
 
+    # Absent when the proposal sets none, so an empty proposal stays a true
+    # identity.
+    assumptions = assumptions_block(proposal)
+    if assumptions is not None:
+        out["assumptions"] = assumptions
+
     return out
````

Patch `app/eval/scenario.py`:

````diff
diff --git a/app/eval/scenario.py b/app/eval/scenario.py
index 2f7a779..56f186a 100644
--- a/app/eval/scenario.py
+++ b/app/eval/scenario.py
@@ -33,6 +33,10 @@ from app.routing.signals import SignalValue
 SCHEMA_PREFIX = "routeforge.config-snapshot/"
 SCHEMA_MAJOR = "v1"
 
+#: The key a global workload value is stored under in an applied document's
+#: `assumptions` block (P3c-2 spec §4.3); any other key is an application id.
+ALL_APPS = "*"
+
 _REQUIRED_TOP_KEYS = (
     "schema", "generated_at", "routing_policy_version", "registry_version",
     "settings", "onboarded", "apps", "quality", "signals",
@@ -69,6 +73,14 @@ def load_snapshot(path: str) -> tuple[dict, str]:
     except json.JSONDecodeError as exc:
         raise SnapshotError("file", f"invalid JSON: {exc}") from exc
     validate_snapshot(doc)
+    # `assumptions` is written only by `apply_proposal` (P3c-2 spec §4.3). A
+    # snapshot FILE that carries one was edited by hand: refuse it, so
+    # workload or latency assumptions can never be smuggled into the
+    # `current` arm.
+    if "assumptions" in doc:
+        raise SnapshotError(
+            "assumptions", "not allowed in a snapshot file; it is written only by "
+            "apply_proposal")
     return doc, digest
 
 
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_proposal_workload.py tests/eval/test_proposal.py tests/eval/test_scenario.py -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_proposal_workload.py app/eval/proposal.py app/eval/scenario.py
git commit -m "$(cat <<'MSG'
feat(eval): proposal workload assumptions and the applied assumptions block (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 4: Replay applies `token_scale` and `cached_share` (spec §4.1, S4, S5, S6)

**Files:**
- Modify: `app/eval/scenario.py` (new `Assumptions`, `_assumptions`; `Scenario` lines 337–344; `build_scenario` lines 378–450)
- Modify: `app/eval/simulate.py` (`ArmResult` lines 56–69, `_rejected` lines 168–171, `simulate_row` lines 174–236, `summarize` lines 448–485, `render_report` lines 515–603; new `scaled_request`, `_assumptions_summary`, `_assumption_items`, `_assumption_lines`, `CACHED_SHARE_NOTE`)
- Modify: `scripts/policy_simulate.py` (pass `assumptions=` to `summarize`)
- Test: `tests/eval/test_proposal_workload.py` (append)

**Interfaces:**
- Consumes: Task 3's `ALL_APPS`, `assumptions_block`, and the applied document's `assumptions` block; the existing `price_call(price, *, prompt_tokens, completion_tokens, cached_prompt_tokens=0) -> float`.
- Produces:
  - `app.eval.scenario.Assumptions` — frozen dataclass with `token_scale`, `cached_share`, `latency_ms` (each `dict[str, float]`, default empty), `scale_for(app_id) -> float` (default 1.0) and `cached_for(app_id) -> float` (default 0.0).
  - `Scenario.assumptions: Assumptions` (default empty); `build_scenario` fills it from the document. A malformed block is a `SnapshotError` naming `assumptions...`.
  - `app.eval.simulate.scaled_request(prompt_tokens: int, complexity: float, scale: float) -> tuple[int, float, bool]`.
  - `ArmResult.complexity_estimated: bool = False`.
  - `summarize(results, excluded, *, inputs, edits, assumptions: dict | None = None)`; the output gains `"assumptions"`: the three maps plus `saturated_complexity_estimated: int`, always present.
  - `_assumption_items(block: dict) -> list[str]` and `CACHED_SHARE_NOTE: str` — Task 9 reuses both.
  - `report.md` gains an `## Assumptions` section before `## Headline`, only when the proposal assumes something.

**Rules (see Deviations 3 and 4).** `scale == 1.0` is an exact identity. Tokens: `max(1, round(p × scale))` when `p > 0`, else `0`. Complexity below 1: `min(1, c × scale)`, exact. Complexity at or above 1 (saturated): `min(1, max(scale, p' / 2000))`, counted as an estimate only when `scale < 1`. The cached share is `round(p' × share)` tokens passed to `price_call`; the router still estimates with zero cached tokens, so a share moves cost and never a route. The literal `2000` mirrors `app/pipeline.py`, and a test pins it against that source.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_proposal_workload.py`:

````diff
diff --git a/tests/eval/test_proposal_workload.py b/tests/eval/test_proposal_workload.py
index e78cc0c..acf0171 100644
--- a/tests/eval/test_proposal_workload.py
+++ b/tests/eval/test_proposal_workload.py
@@ -128,3 +128,168 @@ def test_applying_a_proposal_twice_is_refused():
     with pytest.raises(SnapshotError) as exc:
         _apply({"schema": P, "workload": {"cached_share": 0.5}}, once)
     assert exc.value.where == "assumptions" and once == before
+
+
+# --- Task 4: replay applies token_scale and cached_share (spec §4.1) ---------
+
+import asyncio  # noqa: E402
+from pathlib import Path  # noqa: E402
+
+from app.eval.simulate import (CACHED_SHARE_NOTE, render_report, scaled_request,  # noqa: E402
+                               simulate, summarize)
+from tests.eval.sim_fixtures import make_row, onboarded_row  # noqa: E402
+
+_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
+           "proposal_title": "t", "routing_policy_version": "v",
+           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}
+
+
+def _run(records, snap, prop):
+    return asyncio.run(simulate(records, snap, prop, base=BASE_SETTINGS))
+
+
+def _mixed_rows():
+    """Small, mid and saturated complexity; a failure row that recorded no
+    prompt tokens; a long prompt."""
+    return [make_row("small", "app-a", complexity=0.1, prompt_tokens=200),
+            make_row("mid", "app-a", complexity=0.6, prompt_tokens=1200),
+            make_row("sat", "app-a", complexity=1.0, prompt_tokens=3000),
+            make_row("zero", "app-a", complexity=0.2, prompt_tokens=0,
+                     status="backend_failed"),
+            make_row("long", "app-a", complexity=1.0, prompt_tokens=9000)]
+
+
+def test_complexity_literal_matches_the_pipeline():
+    """`scaled_request` mirrors `app/pipeline.py`'s complexity formula. If
+    the pipeline's divisor ever changes, this fails before a replay lies."""
+    root = Path(__file__).resolve().parents[2]
+    assert "min(1.0, comp.tokens_after / 2000)" in (root / "app" / "pipeline.py").read_text()
+
+
+@pytest.mark.parametrize("args,expected", [
+    ((200, 0.1, 1.0), (200, 0.1, False)),            # identity, exactly
+    ((0, 1.5, 1.0), (0, 1.5, False)),                # identity even on odd recorded values
+    ((200, 0.1, 0.5), (100, 0.05, False)),           # linear below saturation: exact
+    ((200, 0.4, 5.0), (1000, 1.0, False)),           # capped at 1
+    ((1, 0.0005, 0.1), (1, 0.00005, False)),         # tokens floor at 1
+    ((0, 0.2, 0.5), (0, 0.1, False)),                # an unrecorded size stays 0
+    ((0, 0.2, 5.0), (0, 1.0, False)),
+    ((3000, 1.0, 0.5), (1500, 0.75, True)),          # saturated, scaled down: estimate
+    ((9000, 1.0, 0.5), (4500, 1.0, True)),           # still saturated, still an estimate
+    ((500, 1.0, 0.9), (450, 0.9, True)),             # never below `scale` (Review Focus 2)
+    ((500, 1.0, 2.0), (1000, 1.0, False)),           # scaled up: exactly 1, not an estimate
+])
+def test_scaled_request(args, expected):                                   # Review Focus 2
+    tokens, complexity, estimated = scaled_request(*args)
+    assert (tokens, estimated) == (expected[0], expected[2])
+    assert complexity == pytest.approx(expected[1])
+
+
+def test_s4_neutral_workload_changes_nothing():                            # S4 (workload)
+    rows = _mixed_rows() + [make_row("odd", "app-a", complexity=1.5)]
+    results, excluded = _run(rows, make_snapshot(), {"schema": P, "workload": {
+        "token_scale": 1, "cached_share": 0,
+        "apps": {"app-a": {"token_scale": 1.0, "cached_share": 0.0}}}})
+    assert len(results) == len(rows)
+    for r in results:
+        assert r.proposed == r.current
+    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
+    assert sim["totals"]["cost_usd"]["delta"] == 0
+    assert sim["assumptions"]["saturated_complexity_estimated"] == 0
+
+
+def test_s5_cost_monotone_in_token_scale():                                # S5
+    """One eligible endpoint, so routing is fixed and only pricing moves."""
+    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid")])
+    rows = _mixed_rows()
+    costs = []
+    for scale in (0.01, 0.1, 0.5, 1, 1.5, 3, 10):
+        results, _ = _run(rows, snap, {"schema": P, "workload": {"token_scale": scale}})
+        assert all(r.proposed.model == "local-mid" for r in results)
+        costs.append({r.row["request_id"]: r.proposed.cost_usd for r in results})
+    for lower, higher in zip(costs, costs[1:]):
+        for request_id in lower:
+            assert lower[request_id] <= higher[request_id], request_id
+    assert costs[0]["sat"] < costs[-1]["sat"]                # and it really moves
+    assert costs[0]["zero"] == costs[-1]["zero"]             # an unrecorded size never does
+
+
+def test_token_scale_moves_tier_and_counts_estimated_rows():
+    """At 0.4x the mid-complexity row drops to the cheap tier and the
+    saturated 3000-token row to mid (max(0.4, 1200/2000) = 0.6); the
+    9000-token row stays saturated. Both saturated rows are estimates."""
+    results, excluded = _run(_mixed_rows(), make_snapshot(),
+                             {"schema": P, "workload": {"token_scale": 0.4}})
+    by = {r.row["request_id"]: r for r in results}
+    assert (by["mid"].current.tier, by["mid"].proposed.tier) == ("mid", "cheap")
+    assert (by["sat"].current.tier, by["sat"].proposed.tier) == ("frontier", "mid")
+    assert (by["long"].current.tier, by["long"].proposed.tier) == ("frontier", "frontier")
+    assert by["sat"].proposed.complexity_estimated and by["long"].proposed.complexity_estimated
+    assert not by["mid"].proposed.complexity_estimated
+    assert not any(r.current.complexity_estimated for r in results)   # current: no assumptions
+    sim = summarize(results, excluded, inputs=_INPUTS, edits={},
+                    assumptions=assumptions_block({"workload": {"token_scale": 0.4}}))
+    assert sim["assumptions"] == {"token_scale": {ALL_APPS: 0.4}, "cached_share": {},
+                                  "latency_ms": {}, "saturated_complexity_estimated": 2}
+    report = render_report(sim)
+    assert "## Assumptions" in report and "- token_scale: *=0.4" in report
+    assert "estimated from scaled prompt tokens: 2" in report
+
+
+def test_per_app_token_scale_overrides_the_global_one():
+    snap = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")])
+    rows = [make_row("a", "app-a", prompt_tokens=1000), make_row("b", "app-b", prompt_tokens=1000)]
+    results, _ = _run(rows, snap, {"schema": P, "workload": {
+        "token_scale": 2, "apps": {"app-b": {"token_scale": 0.5}}}})
+    by = {r.row["request_id"]: r for r in results}
+    assert by["a"].proposed.cost_usd > by["a"].current.cost_usd      # global 2x
+    assert by["b"].proposed.cost_usd < by["b"].current.cost_usd      # its own 0.5x
+
+
+def test_s6_cached_share_never_raises_cost():                              # S6
+    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid", price_cached_input=0.05)])
+    rows = _mixed_rows()
+    costs = []
+    for share in (0, 0.25, 0.5, 1):
+        results, _ = _run(rows, snap, {"schema": P, "workload": {"cached_share": share}})
+        for r in results:
+            assert r.proposed.model == r.current.model           # a share never moves a route
+        costs.append({r.row["request_id"]: r.proposed.cost_usd for r in results})
+    for lower_share, higher_share in zip(costs, costs[1:]):
+        for request_id in lower_share:
+            assert higher_share[request_id] <= lower_share[request_id], request_id
+    # 3000 prompt tokens, all cached: (3000 * 0.05 + 100 * 1.0) / 1e6
+    assert costs[-1]["sat"] == pytest.approx((3000 * 0.05 + 100 * 1.0) / 1_000_000)
+    assert costs[-1]["sat"] < costs[0]["sat"]
+
+
+def test_s6_cached_share_without_declared_price_is_neutral():              # S6
+    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid")])   # cached price: None
+    results, excluded = _run(_mixed_rows(), snap,
+                             {"schema": P, "workload": {"cached_share": 1}})
+    for r in results:
+        assert r.proposed.cost_usd == r.current.cost_usd
+    sim = summarize(results, excluded, inputs=_INPUTS, edits={},
+                    assumptions=assumptions_block({"workload": {"cached_share": 1}}))
+    assert CACHED_SHARE_NOTE in render_report(sim)
+
+
+def test_report_has_no_assumptions_section_without_assumptions():
+    results, excluded = _run(_mixed_rows(), make_snapshot(), {"schema": P})
+    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
+    assert sim["assumptions"] == {"token_scale": {}, "cached_share": {}, "latency_ms": {},
+                                  "saturated_complexity_estimated": 0}
+    assert "## Assumptions" not in render_report(sim)
+
+
+def test_malformed_assumptions_block_is_a_named_snapshot_error():
+    from app.eval.scenario import build_scenario
+    for block, where in (([], "assumptions"),
+                         ({"token_scale": {}, "bogus": {}}, "assumptions.bogus"),
+                         ({"token_scale": {"*": "big"}}, "assumptions.token_scale.*"),
+                         ({"cached_share": 3}, "assumptions.cached_share")):
+        doc = make_snapshot()
+        doc["assumptions"] = block
+        with pytest.raises(SnapshotError) as exc:
+            asyncio.run(build_scenario(doc, base=BASE_SETTINGS))
+        assert exc.value.where == where
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_proposal_workload.py -p no:warnings
```

Expected: collection error: `ImportError: cannot import name 'CACHED_SHARE_NOTE' from 'app.eval.simulate'`.

- [ ] **Step 3: Implement**

Patch `app/eval/scenario.py`:

````diff
diff --git a/app/eval/scenario.py b/app/eval/scenario.py
index 56f186a..54ca88c 100644
--- a/app/eval/scenario.py
+++ b/app/eval/scenario.py
@@ -12,7 +12,7 @@ from __future__ import annotations
 import copy
 import hashlib
 import json
-from dataclasses import dataclass, fields as dc_fields
+from dataclasses import dataclass, field, fields as dc_fields
 
 from pydantic import ValidationError
 
@@ -336,6 +336,52 @@ class FrozenSignals:
         return float(row["latency_ms"])
 
 
+_ASSUMPTION_KEYS = ("token_scale", "cached_share", "latency_ms")
+
+
+@dataclass(frozen=True)
+class Assumptions:
+    """What a proposal ASSUMES rather than configures (P3c-2 spec §4): a
+    token scale and a cached-prompt share per app (`ALL_APPS` holds the
+    global value), and an asserted latency per endpoint. Empty for a
+    scenario built from a plain snapshot, so the `current` arm never
+    carries any."""
+
+    token_scale: dict[str, float] = field(default_factory=dict)
+    cached_share: dict[str, float] = field(default_factory=dict)
+    latency_ms: dict[str, float] = field(default_factory=dict)
+
+    def scale_for(self, app_id: str) -> float:
+        """The app's own `token_scale`, else the global one, else 1.0."""
+        return self.token_scale.get(app_id, self.token_scale.get(ALL_APPS, 1.0))
+
+    def cached_for(self, app_id: str) -> float:
+        """The app's own `cached_share`, else the global one, else 0.0."""
+        return self.cached_share.get(app_id, self.cached_share.get(ALL_APPS, 0.0))
+
+
+def _assumptions(doc: dict) -> Assumptions:
+    """The applied document's `assumptions` block as an `Assumptions`; an
+    absent block is the empty one. The block is machine-written
+    (`apply_proposal`), so this only checks its shape -- the ranges were
+    validated where the proposal was."""
+    block = doc.get("assumptions")
+    if block is None:
+        return Assumptions()
+    _need_dict(block, "assumptions")
+    for key in block:
+        if key not in _ASSUMPTION_KEYS:
+            raise SnapshotError(f"assumptions.{key}", "unknown key")
+    maps: dict[str, dict[str, float]] = {}
+    for key in _ASSUMPTION_KEYS:
+        values = _need_dict(block.get(key, {}), f"assumptions.{key}")
+        for name, value in values.items():
+            if not _is_number(value):
+                raise SnapshotError(f"assumptions.{key}.{name}", "must be a number")
+        maps[key] = {name: float(value) for name, value in values.items()}
+    return Assumptions(**maps)
+
+
 @dataclass(frozen=True)
 class AppScenario:
     application_id: str
@@ -354,6 +400,7 @@ class Scenario:
     apps: dict[str, AppScenario]
     router: Router
     db: Database
+    assumptions: Assumptions = field(default_factory=Assumptions)
 
 
 def _app_policy_view(policy: dict, app_id: str) -> AppPolicyView:
@@ -395,6 +442,7 @@ async def build_scenario(doc: dict, *, base: Settings) -> Scenario:
             raise SnapshotError(
                 f"onboarded.{row['slug']}.tier",
                 f"unknown tier {row['tier']!r}; legal tiers: {sorted(canonical_tiers)}")
+    assumptions = _assumptions(doc)   # before the scratch DB: a bad block leaks nothing
 
     db = Database(url="sqlite+aiosqlite://")
     await db.ensure_ready()
@@ -456,7 +504,8 @@ async def build_scenario(doc: dict, *, base: Settings) -> Scenario:
         router = Router(settings, quality_resolver=quality, registry=registry, signals=signals)
 
         return Scenario(settings=settings, registry=registry, quality=quality,
-                        signals=signals, apps=apps, router=router, db=db)
+                        signals=signals, apps=apps, router=router, db=db,
+                        assumptions=assumptions)
     except BaseException:
         await db.dispose()
         raise
````

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 5e7cb5a..5986fd9 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -70,6 +70,9 @@ class ArmResult:
     quality_source: str | None
     core: tuple[str, ...] = ()       # P3b: NoEligibleCandidate.core.ids(), () when routed
     core_state: tuple[str, ...] = () # P3b: NoEligibleCandidate.core.state_ids(), () when routed
+    # P3c-2: True when `workload.token_scale` shrank a row whose recorded
+    # complexity was saturated, so its new complexity is an estimate.
+    complexity_estimated: bool = False
 
 
 @dataclass(frozen=True)
@@ -202,9 +205,42 @@ def observed_latencies(records: list[dict]) -> dict[str, float]:
 
 
 def _rejected(eligible: tuple[str, ...], codes: tuple[str, ...], *,
-              core: tuple[str, ...] = (), core_state: tuple[str, ...] = ()) -> ArmResult:
+              core: tuple[str, ...] = (), core_state: tuple[str, ...] = (),
+              complexity_estimated: bool = False) -> ArmResult:
     return ArmResult("rejected", None, None, None, eligible, codes,
-                     None, None, None, None, None, core, core_state)
+                     None, None, None, None, None, core, core_state,
+                     complexity_estimated=complexity_estimated)
+
+
+# app/pipeline.py: `complexity = min(1.0, comp.tokens_after / 2000)`. The
+# literal has no name there to import; tests/eval/test_proposal_workload.py
+# pins it against the pipeline source so the two cannot drift apart.
+_COMPLEXITY_SATURATION_TOKENS = 2000
+
+
+def scaled_request(prompt_tokens: int, complexity: float,
+                   scale: float) -> tuple[int, float, bool]:
+    """`(prompt_tokens', complexity', estimated)` for one row under
+    `workload.token_scale` (P3c-2 spec §4.1).
+
+    - `scale == 1.0` is the identity, exactly.
+    - Tokens: `max(1, round(prompt_tokens * scale))`. A row that recorded no
+      prompt tokens (every failure row records 0) stays at 0: its size is
+      unknown, and flooring it to 1 would make cost fall as the scale rises.
+    - Complexity below 1 is linear in the token count, so `complexity *
+      scale` (capped at 1) is exact.
+    - A saturated complexity (>= 1) hides the real token count, which was at
+      least the saturation point. So the scaled complexity is at least
+      `scale`, and otherwise estimated from the scaled prompt tokens.
+      Scaling UP therefore keeps it at exactly 1 (known, not an estimate);
+      scaling DOWN is an estimate, and `estimated` says so."""
+    if scale == 1.0:
+        return prompt_tokens, complexity, False
+    scaled = max(1, round(prompt_tokens * scale)) if prompt_tokens > 0 else 0
+    if complexity < 1.0:
+        return scaled, min(1.0, complexity * scale), False
+    estimate = max(scale, scaled / _COMPLEXITY_SATURATION_TOKENS)
+    return scaled, min(1.0, estimate), scale < 1.0
 
 
 def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmResult:
@@ -228,7 +264,12 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
     eligible = tuple(sorted(
         c.name for c in feasible_set(scn.settings, scn.registry, ctx,
                                      tenant_id=row["application_id"]).eligible))
-    prompt_tokens = int(row["prompt_tokens"])
+    # P3c-2: the scenario's workload assumptions, applied before routing and
+    # pricing. A scenario built from a plain snapshot has none (scale 1.0,
+    # share 0.0), so the `current` arm replays the row as recorded.
+    prompt_tokens, complexity, estimated = scaled_request(
+        int(row["prompt_tokens"]), float(row["complexity"]),
+        scn.assumptions.scale_for(row["application_id"]))
     try:
         # B55: price at the client's recorded output cap, as the live
         # gateway does (`expected_output_tokens(settings, params)`); no cap
@@ -236,7 +277,7 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
         output_tokens = expected_output_tokens(
             scn.settings, {"max_tokens": row["max_tokens"]} if row.get("max_tokens") else None)
         decision = scn.router.select(ctx, tokens_after=prompt_tokens,
-                                     complexity=float(row["complexity"]),
+                                     complexity=complexity,
                                      output_tokens=output_tokens)
     except NoEligibleCandidate as exc:
         # P3b: the router attaches the minimal conflicting core to every
@@ -246,18 +287,26 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
         core = exc.core.ids() if exc.core else ()
         core_state = exc.core.state_ids() if exc.core else ()
         if not exc.rejected:
-            return _rejected(eligible, ("empty_pool",), core=core, core_state=core_state)
+            return _rejected(eligible, ("empty_pool",), core=core, core_state=core_state,
+                             complexity_estimated=estimated)
         fallback = "over_budget" if "budget filter" in str(exc) else "unspecified"
         codes = tuple(sorted({code or fallback for _name, code in exc.rejected}))
-        return _rejected(eligible, codes, core=core, core_state=core_state)
+        return _rejected(eligible, codes, core=core, core_state=core_state,
+                         complexity_estimated=estimated)
     except ContextWindowExceeded:
-        return _rejected(eligible, ("context_window",))
+        return _rejected(eligible, ("context_window",), complexity_estimated=estimated)
 
     pick = decision.chosen
+    # P3c-2: `workload.cached_share` of the (scaled) prompt is billed at the
+    # endpoint's cached-input price. The ROUTER still estimates at zero
+    # cached tokens, as it does live, so the share moves cost and never a
+    # route. `price_call` bills cached tokens at the input price when the
+    # endpoint declares no cached price.
+    cached_tokens = round(prompt_tokens * scn.assumptions.cached_for(row["application_id"]))
     cost = price_call(scn.registry.price_for(pick.name),
                       prompt_tokens=prompt_tokens,
                       completion_tokens=int(row.get("completion_tokens") or 0),
-                      cached_prompt_tokens=0)
+                      cached_prompt_tokens=cached_tokens)
     if pick.name in observed:
         latency, latency_source = observed[pick.name], "observed"
     else:
@@ -269,7 +318,8 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
             latency_source = "tier_default"
     q = scn.quality.resolve(pick.name, row.get("workload"))
     return ArmResult("routed", pick.name, pick.provider, pick.tier, eligible, (),
-                     cost, latency, latency_source, q.score, q.source)
+                     cost, latency, latency_source, q.score, q.source,
+                     complexity_estimated=estimated)
 
 
 def run_rows(records: list[dict], current: Scenario, proposed: Scenario, *,
@@ -510,8 +560,20 @@ def _per_day(window: dict | None, cost_usd: dict) -> dict | None:
     return {"cost_usd": {k: (v / days if v is not None else None) for k, v in cost_usd.items()}}
 
 
+def _assumptions_summary(results: list[RowResult], assumptions: dict | None) -> dict:
+    """The `assumptions` block of `simulation.json` (P3c-2 spec §4): what the
+    proposal assumed (`proposal.assumptions_block`'s three maps, empty when
+    it assumed nothing) plus `saturated_complexity_estimated`, the number of
+    rows whose scaled complexity is an estimate."""
+    block = {key: dict((assumptions or {}).get(key) or {})
+             for key in ("token_scale", "cached_share", "latency_ms")}
+    block["saturated_complexity_estimated"] = sum(
+        1 for r in results if r.proposed.complexity_estimated)
+    return block
+
+
 def summarize(results: list[RowResult], excluded: dict[str, int], *, inputs: dict,
-             edits: dict) -> dict:
+             edits: dict, assumptions: dict | None = None) -> dict:
     """The deterministic `simulation.json` document (spec §8). `inputs` is
     the caller's partial input record (no `window` yet -- this function
     computes it from the simulated rows and adds it before hashing, so
@@ -530,6 +592,7 @@ def summarize(results: list[RowResult], excluded: dict[str, int], *, inputs: dic
         "simulation_id": simulation_id,
         "inputs": full_inputs,
         "edits": edits,
+        "assumptions": _assumptions_summary(results, assumptions),
         "rows": {"simulated": len(results), "excluded": dict(excluded)},
         "baseline": _baseline(results),
         "totals": totals,
@@ -577,6 +640,42 @@ def _metrics_table(stats: dict) -> list[str]:
     return lines
 
 
+#: Printed whenever a proposal sets `workload.cached_share` (spec §4.1).
+CACHED_SHARE_NOTE = (
+    "`cached_share` is an assumption: outcome rows do not record cached prompt tokens, so "
+    "the current arm prices none as cached."
+)
+
+
+def _assumption_items(block: dict) -> list[str]:
+    """One short string per assumption a proposal set, e.g.
+    `"token_scale: *=0.6, claims=0.5"`; empty when it assumed nothing. With
+    a `token_scale`, the last item says how many rows had a saturated
+    complexity estimated."""
+    items = []
+    for key in ("token_scale", "cached_share", "latency_ms"):
+        if block[key]:
+            shown = ", ".join(f"{name}={_fmt(value)}" for name, value in sorted(block[key].items()))
+            items.append(f"{key}: {shown}")
+    if block["token_scale"]:
+        items.append("rows whose saturated complexity was estimated from scaled prompt "
+                     f"tokens: {block['saturated_complexity_estimated']}")
+    return items
+
+
+def _assumption_lines(block: dict) -> list[str]:
+    """The report's "Assumptions" section for one `assumptions` block, or
+    nothing at all when the proposal assumed nothing."""
+    items = _assumption_items(block)
+    if not items:
+        return []
+    lines = ["## Assumptions", ""] + [f"- {item}" for item in items]
+    if block["cached_share"]:
+        lines.append(f"- {CACHED_SHARE_NOTE}")
+    lines.append("")
+    return lines
+
+
 def render_report(sim: dict) -> str:
     """The `report.md` document (spec §8), in the order: caveat, baseline
     warning, headline, new rejections, moves, by-app, by-workload,
@@ -592,6 +691,8 @@ def render_report(sim: dict) -> str:
             "Treat the headline diff as noisier than usual -- see spec §7 for causes.")
         lines.append("")
 
+    lines += _assumption_lines(sim["assumptions"])
+
     lines.append("## Headline")
     lines.append("")
     lines += _metrics_table(sim["totals"])
````

Patch `scripts/policy_simulate.py`:

````diff
diff --git a/scripts/policy_simulate.py b/scripts/policy_simulate.py
index c094a6f..aa34c34 100644
--- a/scripts/policy_simulate.py
+++ b/scripts/policy_simulate.py
@@ -42,7 +42,7 @@ from app.config.lint import ConfigLintError  # noqa: E402
 from app.config.loader import load_settings  # noqa: E402
 from app.eval.outcomes import load_export  # noqa: E402
 from app.eval.proposal import (  # noqa: E402
-    ProposalError, apply_proposal, load_proposal, normalized_edits,
+    ProposalError, apply_proposal, assumptions_block, load_proposal, normalized_edits,
 )
 from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
 from app.eval.shadow import ShadowInvariantError, render_shadow_section, shadow_prices  # noqa: E402
@@ -165,7 +165,8 @@ def main(argv: list[str] | None = None) -> int:
         if args.proposal:
             results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base,
                                                      since=since))
-            sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal))
+            sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal),
+                            assumptions=assumptions_block(proposal))
             baseline = sim["baseline"]
         elif args.min_agreement is not None:
             # `--shadow-prices` alone has no `summarize()` output to read the
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_proposal_workload.py app/eval/scenario.py app/eval/simulate.py scripts/policy_simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): replay applies workload token_scale and cached_share (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 5: Asserted endpoint `latency_ms` (spec §4.2, S4)

**Files:**
- Modify: `app/eval/proposal.py` (`_ENDPOINT_ADD_KEYS` / `_ENDPOINT_UPDATE_KEYS` lines 47–54, `_validate_endpoint_add` lines 356–378, `_validate_endpoint_update` lines 381–397, the `update` loops in `apply_proposal`, `assumptions_block`)
- Modify: `app/eval/scenario.py` (`FrozenSignals` lines 282–324, its construction in `build_scenario`)
- Modify: `app/eval/simulate.py` (the latency ladder in `simulate_row`)
- Test: `tests/eval/test_proposal_workload.py` (append)

**Interfaces:**
- Consumes: Task 3's `assumptions_block`; Task 4's `Assumptions.latency_ms` and `Scenario.assumptions`; `SignalValue(value, source, samples)` from `app/routing/signals.py`.
- Produces:
  - Proposal keys `endpoints.add[].latency_ms` and `endpoints.update.<slug>.latency_ms` (finite `> 0` or `null`). `endpoints.update` now also accepts a priced YAML pool model, with `latency_ms` as its only allowed field.
  - `assumptions_block` fills `latency_ms: {slug: float}`; `null` writes nothing. The value never lands on an onboarded row.
  - `FrozenSignals(cfg, values, overrides: dict[str, float] = {})`; `FrozenSignals.proposed_latency(model: str) -> float | None`; `FrozenSignals.latency` returns an override as `SignalValue(ms, "measured", n)` when signals are enabled.
  - `ArmResult.latency_source` may now be `"proposal"`.

**Rules (see Deviations 5 and 6).** The asserted value wins every rung of the replay latency ladder and is labelled `proposal`. In scoring it behaves exactly like a measurement, including as the same-tier peer prior. With `routing.signals.enabled: false` it does not affect scoring.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_proposal_workload.py`:

````diff
diff --git a/tests/eval/test_proposal_workload.py b/tests/eval/test_proposal_workload.py
index acf0171..55b3dc5 100644
--- a/tests/eval/test_proposal_workload.py
+++ b/tests/eval/test_proposal_workload.py
@@ -293,3 +293,160 @@ def test_malformed_assumptions_block_is_a_named_snapshot_error():
         with pytest.raises(SnapshotError) as exc:
             asyncio.run(build_scenario(doc, base=BASE_SETTINGS))
         assert exc.value.where == where
+
+
+# --- Task 5: asserted endpoint latency (spec §4.2) ----------------------------
+
+import dataclasses  # noqa: E402
+
+_ADD = {"slug": "new-mid", "provider": "onprem", "tier": "mid",
+        "price": {"input": 0.5, "output": 1.0, "cached_input": None}}
+_YAML_MODEL = "azure-gpt-mid"          # config/routing.yaml model_pool
+
+
+def _signal(ms, n=50):
+    return {"latency_ms": ms, "latency_n": n, "availability": 1.0, "availability_n": n}
+
+
+def _two_mids(**signals):
+    """Two mid-tier endpoints; `local-mid` is the cheaper, so it wins a tie."""
+    return make_snapshot(
+        onboarded=[onboarded_row("local-mid", "mid", price_input=0.4),
+                   onboarded_row("local-mid-2", "mid")],
+        signals=signals)
+
+
+def _mid_rows(n=3):
+    return [make_row(f"m{i}", "app-a", complexity=0.6) for i in range(n)]
+
+
+@pytest.mark.parametrize("value", [0, -5, "fast", True, [100], float("nan"), float("inf")])
+def test_latency_ms_must_be_positive_finite_or_null(value):
+    for prop, where in (
+            ({"endpoints": {"update": {"local-mid": {"latency_ms": value}}}},
+             "endpoints.update.local-mid.latency_ms"),
+            ({"endpoints": {"add": [{**_ADD, "latency_ms": value}]}},
+             "endpoints.add.new-mid.latency_ms"),
+            ({"endpoints": {"update": {_YAML_MODEL: {"latency_ms": value}}}},
+             f"endpoints.update.{_YAML_MODEL}.latency_ms")):
+        with pytest.raises(ProposalError) as exc:
+            _apply({"schema": P, **prop})
+        assert exc.value.where == where, str(exc.value)
+
+
+def test_latency_ms_goes_to_assumptions_never_onto_the_row():
+    snap = make_snapshot()
+    out = _apply({"schema": P, "endpoints": {
+        "add": [{**_ADD, "latency_ms": 40}],
+        "update": {"local-mid": {"latency_ms": 800, "region": "eu"},
+                   _YAML_MODEL: {"latency_ms": 250.5}}}}, snap)
+    assert out["assumptions"] == {"token_scale": {}, "cached_share": {}, "latency_ms": {
+        "new-mid": 40.0, "local-mid": 800.0, _YAML_MODEL: 250.5}}
+    by = {o["slug"]: o for o in out["onboarded"]}
+    assert by["local-mid"]["region"] == "eu"                 # the other update still lands
+    for row in out["onboarded"]:
+        assert "latency_ms" not in row
+    assert _YAML_MODEL not in by                             # a YAML model gains no row
+
+
+def test_latency_ms_null_is_no_override():                                # Review Focus 3
+    out = _apply({"schema": P, "endpoints": {
+        "add": [{**_ADD, "latency_ms": None}],
+        "update": {"local-mid": {"latency_ms": None}}}})
+    assert "assumptions" not in out
+
+
+@pytest.mark.parametrize("fields,where", [
+    ({"tier": "mid"}, f"endpoints.update.{_YAML_MODEL}.tier"),
+    ({"latency_ms": 100, "region": "eu"}, f"endpoints.update.{_YAML_MODEL}.region"),
+    ({"context_window": 4096}, f"endpoints.update.{_YAML_MODEL}.context_window"),
+])
+def test_yaml_pool_model_accepts_only_latency_ms(fields, where):          # Review Focus 3
+    with pytest.raises(ProposalError) as exc:
+        _apply({"schema": P, "endpoints": {"update": {_YAML_MODEL: fields}}})
+    assert exc.value.where == where
+
+
+def test_update_of_an_unknown_slug_is_still_refused():
+    with pytest.raises(ProposalError) as exc:
+        _apply({"schema": P, "endpoints": {"update": {"ghost": {"latency_ms": 100}}}})
+    assert exc.value.where == "endpoints.update.ghost"
+
+
+def test_asserted_latency_moves_the_route_and_the_replayed_latency():
+    """`local-mid` (300 ms measured) beats `local-mid-2` (600 ms). Asserting
+    5000 ms for it sends the mid rows to `local-mid-2`."""
+    snap = _two_mids(**{"local-mid": _signal(300.0), "local-mid-2": _signal(600.0)})
+    results, _ = _run(_mid_rows(), snap, {"schema": P, "endpoints": {
+        "update": {"local-mid": {"latency_ms": 5000}}}})
+    for r in results:
+        assert (r.current.model, r.proposed.model) == ("local-mid", "local-mid-2")
+        assert (r.proposed.latency_ms, r.proposed.latency_source) == (600.0, "signal")
+
+
+def test_asserted_latency_wins_over_observed_and_is_labelled_proposal():
+    """The export's own rows give `local-mid` an observed median (120 ms);
+    the assertion still wins, and the source says where it came from."""
+    rows = [make_row(f"m{i}", "app-a", complexity=0.6, attempts=["local-mid"],
+                     chosen="local-mid") for i in range(3)]
+    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid")])
+    results, excluded = _run(rows, snap, {"schema": P, "endpoints": {
+        "update": {"local-mid": {"latency_ms": 42.5}}}})
+    for r in results:
+        assert (r.current.latency_ms, r.current.latency_source) == (120.0, "observed")
+        assert (r.proposed.latency_ms, r.proposed.latency_source) == (42.5, "proposal")
+    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
+    assert sim["sources"]["proposed"]["latency"] == {"proposal": 3}
+    assert sim["totals"]["latency_ms"]["p50"]["delta"] == pytest.approx(42.5 - 120.0)
+
+
+def test_added_endpoint_with_latency_is_scored_and_replayed_at_it():
+    snap = _two_mids(**{"local-mid": _signal(300.0), "local-mid-2": _signal(600.0)})
+    results, _ = _run(_mid_rows(), snap, {"schema": P, "endpoints": {
+        "add": [{**_ADD, "price": {"input": 0.4, "output": 1.0, "cached_input": None},
+                 "latency_ms": 20}]}})
+    for r in results:
+        assert r.proposed.model == "new-mid"
+        assert (r.proposed.latency_ms, r.proposed.latency_source) == (20.0, "proposal")
+
+
+def test_s4_neutral_latency_changes_no_route_or_cost():                    # S4 (latency)
+    """Asserting the latency replay already uses -- here the snapshot's
+    measured signal, which is also the router's scoring term -- changes no
+    route, cost or latency. Only the source label differs."""
+    snap = _two_mids(**{"local-mid": _signal(300.0), "local-mid-2": _signal(600.0)})
+    results, _ = _run(_mid_rows(), snap, {"schema": P, "endpoints": {
+        "update": {"local-mid": {"latency_ms": 300.0}}}})
+    assert results
+    for r in results:
+        assert (r.current.model, r.current.latency_source) == ("local-mid", "signal")
+        assert r.proposed.latency_source == "proposal"
+        relabelled = dataclasses.replace(r.proposed, latency_source="signal")
+        assert relabelled == r.current
+
+
+def test_neutral_latency_keeps_the_peer_prior_it_supplies():              # Review Focus 3
+    """`local-mid` is the tier's only measured endpoint, so `local-mid-2`
+    is scored at its 3000 ms as a peer prior. Re-asserting 3000 ms must
+    leave that prior in place: were the assertion not scored as a
+    measurement, `local-mid-2` would fall back to the 500 ms tier default
+    and take every row."""
+    snap = _two_mids(**{"local-mid": _signal(3000.0)})
+    results, _ = _run(_mid_rows(), snap, {"schema": P, "endpoints": {
+        "update": {"local-mid": {"latency_ms": 3000.0}}}})
+    for r in results:
+        assert (r.current.model, r.proposed.model) == ("local-mid", "local-mid")
+
+
+def test_signals_disabled_assertion_feeds_latency_but_not_routing():      # Review Focus 3
+    """With `routing.signals.enabled: false` the live router ignores measured
+    latency, so an assertion must not move a route -- but the replayed
+    latency is still the asserted one."""
+    snap = _two_mids()
+    snap["settings"]["routing"]["signals"]["enabled"] = False
+    results, _ = _run(_mid_rows(), snap, {"schema": P, "endpoints": {
+        "update": {"local-mid": {"latency_ms": 60000}}}})
+    for r in results:
+        assert (r.current.model, r.proposed.model) == ("local-mid", "local-mid")
+        assert r.current.latency_source == "tier_default"
+        assert (r.proposed.latency_ms, r.proposed.latency_source) == (60000.0, "proposal")
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_proposal_workload.py -p no:warnings
```

Expected: the new tests fail with `ProposalError: endpoints.update.local-mid.latency_ms: unknown key` (and `endpoints.add.new-mid.latency_ms: unknown key`).

- [ ] **Step 3: Implement**

Patch `app/eval/proposal.py`:

````diff
diff --git a/app/eval/proposal.py b/app/eval/proposal.py
index 3334708..afe60f3 100644
--- a/app/eval/proposal.py
+++ b/app/eval/proposal.py
@@ -48,10 +48,11 @@ _ENDPOINTS_KEYS = frozenset({"add", "remove", "disable", "enable", "update"})
 _ENDPOINT_ADD_KEYS = frozenset({
     "slug", "provider", "tier", "price", "region", "prompt_retention",
     "accepts_raw_sensitive", "context_window", "quality", "allowed_app_ids",
+    "latency_ms",
 })
 _ENDPOINT_UPDATE_KEYS = frozenset({
     "tier", "region", "prompt_retention", "accepts_raw_sensitive",
-    "context_window", "allowed_app_ids",
+    "context_window", "allowed_app_ids", "latency_ms",
 })
 _PRICE_KEYS = frozenset({"input", "output", "cached_input"})
 # P3c-2 (spec §4.1): assumptions about the traffic itself, global and per app.
@@ -242,6 +243,13 @@ def _validate_context_window(loc: str, value) -> None:
         raise ProposalError(loc, "must be an int > 0 or null")
 
 
+def _validate_latency_ms(loc: str, value) -> None:
+    """P3c-2 spec §4.2: an operator-asserted latency. `null` means "no
+    override", so it is legal and writes nothing."""
+    if value is not None and not _finite_positive(value):
+        raise ProposalError(loc, "must be a finite number > 0, or null")
+
+
 def _validate_tier(loc: str, value, canonical_tiers: set[str]) -> None:
     if value not in canonical_tiers:
         raise ProposalError(
@@ -385,6 +393,7 @@ def _validate_endpoint_add(entry, onboarded_by_slug: dict, pricing_models: set[s
     if quality is not None and not (_finite_number(quality) and 0 <= quality <= 1):
         raise ProposalError(f"{loc}.quality", "must be null or a finite value in [0, 1]")
     _validate_allowed_app_ids(f"{loc}.allowed_app_ids", entry.get("allowed_app_ids"), app_ids)
+    _validate_latency_ms(f"{loc}.latency_ms", entry.get("latency_ms"))
 
 
 def _validate_endpoint_update(slug: str, fields: dict, canonical_tiers: set[str],
@@ -404,6 +413,8 @@ def _validate_endpoint_update(slug: str, fields: dict, canonical_tiers: set[str]
     if "allowed_app_ids" in fields:
         _validate_allowed_app_ids(
             f"{loc}.allowed_app_ids", fields["allowed_app_ids"], app_ids)
+    if "latency_ms" in fields:
+        _validate_latency_ms(f"{loc}.latency_ms", fields["latency_ms"])
 
 
 # --- workload validation (P3c-2 spec §4.1) -------------------------------------
@@ -449,6 +460,15 @@ def assumptions_block(proposal: dict) -> dict | None:
         for app_id, fields in sorted((workload.get("apps") or {}).items()):
             if key in fields:
                 block[key][app_id] = float(fields[key])
+    # An asserted latency is an assumption about an endpoint, never a field
+    # of its row: it lives here, and only here (spec §4.2). `null` = none.
+    endpoints = proposal.get("endpoints") or {}
+    for entry in endpoints.get("add") or []:
+        if entry.get("latency_ms") is not None:
+            block["latency_ms"][entry["slug"]] = float(entry["latency_ms"])
+    for slug, fields in (endpoints.get("update") or {}).items():
+        if fields.get("latency_ms") is not None:
+            block["latency_ms"][slug] = float(fields["latency_ms"])
     return block if any(block.values()) else None
 
 
@@ -544,9 +564,23 @@ def apply_proposal(snapshot: dict, proposal: dict, *, base: Settings | None = No
         if slug not in onboarded_by_slug:
             raise ProposalError(f"endpoints.enable.{slug}", "not an onboarded endpoint")
     for slug, fields in update_map.items():
-        if slug not in onboarded_by_slug:
-            raise ProposalError(f"endpoints.update.{slug}", "not an onboarded endpoint")
-        _validate_endpoint_update(slug, fields, canonical_tiers, app_ids)
+        if slug in onboarded_by_slug:
+            _validate_endpoint_update(slug, fields, canonical_tiers, app_ids)
+        elif slug in pricing_models:
+            # A YAML pool model's tier, region and the rest are declared in
+            # routing.yaml, not on an onboarded row -- there is nothing here
+            # to update. An asserted latency is the one per-endpoint
+            # assumption a proposal may set on it (spec §4.2).
+            for field in fields:
+                if field != "latency_ms":
+                    raise ProposalError(
+                        f"endpoints.update.{slug}.{field}",
+                        "only latency_ms may be updated on a YAML pool model")
+            _validate_endpoint_update(slug, fields, canonical_tiers, app_ids)
+        else:
+            raise ProposalError(
+                f"endpoints.update.{slug}",
+                "not an onboarded endpoint or a priced YAML pool model")
 
     # -- prices --
     removed = set(remove_list)
@@ -611,8 +645,10 @@ def apply_proposal(snapshot: dict, proposal: dict, *, base: Settings | None = No
     for slug in enable_list:
         onboarded_out[slug]["enabled"] = True
     for slug, fields in update_map.items():
-        row = onboarded_out[slug]
+        row = onboarded_out.get(slug)     # None for a YAML pool model: latency_ms only
         for field, value in fields.items():
+            if field == "latency_ms":
+                continue                  # an assumption, not a row field: assumptions_block
             row[field] = sorted(value) if field == "allowed_app_ids" and value is not None else value
 
     if removed:
````

Patch `app/eval/scenario.py`:

````diff
diff --git a/app/eval/scenario.py b/app/eval/scenario.py
index 54ca88c..f35fdb1 100644
--- a/app/eval/scenario.py
+++ b/app/eval/scenario.py
@@ -300,6 +300,10 @@ class FrozenSignals:
 
     cfg: SignalsCfg
     values: dict[str, dict]
+    # P3c-2 spec §4.2: operator-asserted latency per endpoint, from the
+    # applied document's `assumptions.latency_ms`. Empty for the `current`
+    # arm.
+    overrides: dict[str, float] = field(default_factory=dict)
 
     def _tier_default(self, tier: str) -> float:
         return self.cfg.tier_latency_ms.get(tier, self.cfg.latency_scale_ms)
@@ -310,6 +314,14 @@ class FrozenSignals:
             return SignalValue(default, "disabled", 0)
         row = self.values.get(model)
         n = int(row["latency_n"]) if row else 0
+        if model in self.overrides:
+            # Scored exactly as a measurement would be -- it also serves as
+            # the same-tier peer prior for unmeasured endpoints
+            # (`strategy._with_peer_prior`), as a live measurement of this
+            # value would. So asserting the value already in use changes no
+            # route. Below the `enabled` check on purpose: a deployment with
+            # signals off ignores measured latency when scoring, live.
+            return SignalValue(self.overrides[model], "measured", n)
         if row is None or n < self.cfg.min_samples:
             return SignalValue(default, "tier_default", n)
         return SignalValue(float(row["latency_ms"]), "measured", n)
@@ -335,6 +347,12 @@ class FrozenSignals:
             return None
         return float(row["latency_ms"])
 
+    def proposed_latency(self, model: str) -> float | None:
+        """The proposal's asserted latency for `model`, or None. The replay
+        ladder's first rung: it wins over observed, signal and tier-default
+        latency, whether or not signals are enabled."""
+        return self.overrides.get(model)
+
 
 _ASSUMPTION_KEYS = ("token_scale", "cached_share", "latency_ms")
 
@@ -490,7 +508,8 @@ async def build_scenario(doc: dict, *, base: Settings) -> Scenario:
                 QualityScore(entry["score"], "calibrated", entry["set_id"]))
         quality.set_overrides(overrides)
 
-        signals = FrozenSignals(settings.routing.signals, doc["signals"])
+        signals = FrozenSignals(settings.routing.signals, doc["signals"],
+                                assumptions.latency_ms)
 
         apps: dict[str, AppScenario] = {}
         for entry in doc["apps"]:
````

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 5986fd9..bb56489 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -65,7 +65,7 @@ class ArmResult:
     reject_codes: tuple[str, ...] # sorted, empty when routed
     cost_usd: float | None
     latency_ms: float | None
-    latency_source: str | None    # "observed" | "signal" | "tier_default"
+    latency_source: str | None    # "proposal" | "observed" | "signal" | "tier_default"
     quality: float | None
     quality_source: str | None
     core: tuple[str, ...] = ()       # P3b: NoEligibleCandidate.core.ids(), () when routed
@@ -307,7 +307,11 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
                       prompt_tokens=prompt_tokens,
                       completion_tokens=int(row.get("completion_tokens") or 0),
                       cached_prompt_tokens=cached_tokens)
-    if pick.name in observed:
+    asserted = scn.signals.proposed_latency(pick.name)
+    if asserted is not None:
+        # P3c-2 spec §4.2: the proposal's asserted latency wins every rung.
+        latency, latency_source = asserted, "proposal"
+    elif pick.name in observed:
         latency, latency_source = observed[pick.name], "observed"
     else:
         snap = scn.signals.snapshot_latency(pick.name)
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_proposal_workload.py app/eval/proposal.py app/eval/scenario.py app/eval/simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): operator-asserted endpoint latency in proposals (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 6: Context-window overflow re-selection in both arms (spec §5, S7)

**Files:**
- Modify: `app/eval/simulate.py` (module docstring lines 1–28, `ArmResult`, `_rejected`, `simulate_row`, `REPORT_CAVEAT` lines 273–278, `_bucket_stats` lines 303–342, `_baseline` lines 345–378, `_new_rejections` lines 404–414, `_metrics_table` lines 488–512; new `_first_pick`)
- Test: create `tests/eval/test_replay_overflow.py`

**Interfaces:**
- Consumes: `Router.select(ctx, tokens_after, complexity, min_tier=None, exclude: frozenset[str] = frozenset(), output_tokens=None, open_circuits=frozenset()) -> RoutingDecision`; `decision.chosen.context_window: int | None` (set by `ModelRegistry.effective_pool()` for onboarded rows, always `None` for YAML pool models); `settings.routing.max_failover_attempts: int`; Task 4's `scaled_request` output.
- Produces:
  - `ArmResult.attempted: tuple[str, ...] = ()` — every model the arm selected, in order; the last one is the served model when routed.
  - `ArmResult.overflow_hops` (property, `int`) — the number of picks excluded for overflowing.
  - A row whose reachable candidates all overflow, or that exceeds the bound, is rejected with `reject_codes == ("context_window",)`.
  - `_bucket_stats` output gains `"overflow_hops": {"current": int, "proposed": int}` (in `totals`, `by_app.*`, `by_workload.*`); `_metrics_table` prints it.
  - `_first_pick(arm: ArmResult) -> str | None`; `_baseline` compares it with `attempts[0]`.
  - `new_rejections.by_core` uses the key `(context_window)` for these rejections.
  - `REPORT_CAVEAT` amended: "…the first routing decision of recorded requests under the proposal, plus deterministic context-window overflow re-selection…".

**Rules.** This mirrors `Gateway.run` (`app/gateway/gateway.py`): on overflow the pick is added to `exclude` and the router re-selects; the request fails once the number of overflowed picks exceeds `max_failover_attempts`. `prompt + output == window` fits. A `None` window never overflows. The served endpoint is the one costed. See Deviations 1, 2 and 13.

**Existing tests.** No existing P3a or P3c-1 expectation changes: every fixture row has `context_window: None`. The whole `tests/eval` directory is run in Step 4 to confirm it. The reference doc still quotes the old caveat; Task 11 updates it.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_replay_overflow.py`:

````python
"""P3c-2 Task 6: context-window overflow re-selection in replay (spec §5, S7)."""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.eval.simulate import REPORT_CAVEAT, render_report, simulate, summarize
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"
DEFAULT_OUTPUT = int(BASE_SETTINGS.policy.budgets["default_estimated_output_tokens"])   # 512

_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": "t", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}


def _run(records, snap, prop=None):
    return asyncio.run(simulate(records, snap, prop or {"schema": P}, base=BASE_SETTINGS))


def _mids(*windows, max_failover=None, extra=()):
    """Mid-tier endpoints `m1..mN`, cheapest first, so the router prefers them
    in that order; `windows[i]` is `m{i+1}`'s declared context window."""
    rows = [onboarded_row(f"m{i + 1}", "mid", price_input=0.1 * (i + 1), context_window=w)
            for i, w in enumerate(windows)]
    snap = make_snapshot(onboarded=rows + list(extra),
                         apps=[app_entry("app-a", max_cost_usd_per_request=100.0)])
    if max_failover is not None:
        snap["settings"]["routing"]["max_failover_attempts"] = max_failover
    return snap


def _row(request_id="r", prompt_tokens=900, **over):
    return make_row(request_id, "app-a", complexity=0.6, prompt_tokens=prompt_tokens, **over)


def test_fixture_order_is_the_routers_preference():
    (r,), _ = _run([_row(prompt_tokens=10)], _mids(None, None, None))
    assert r.current.model == "m1" and r.current.attempted == ("m1",)
    assert r.current.overflow_hops == 0


def test_overflowing_pick_is_excluded_and_the_next_one_serves():
    """900 prompt + 512 output > m1's 1000-token window: m1 is excluded, m2
    serves, and m2 is the endpoint costed."""
    (r,), _ = _run([_row()], _mids(1000, None))
    assert r.current.model == "m2"
    assert r.current.attempted == ("m1", "m2") and r.current.overflow_hops == 1
    assert r.current.cost_usd == pytest.approx((900 * 0.2 + 100 * 1.0) / 1_000_000)


@pytest.mark.parametrize("prompt_tokens,hops", [(1000 - DEFAULT_OUTPUT, 0),
                                                (1000 - DEFAULT_OUTPUT + 1, 1)])
def test_boundary_prompt_plus_output_equal_to_window_fits(prompt_tokens, hops):  # Review Focus 4
    (r,), _ = _run([_row(prompt_tokens=prompt_tokens)], _mids(1000, None))
    assert r.current.overflow_hops == hops
    assert r.current.model == ("m1" if hops == 0 else "m2")


def test_the_clients_recorded_max_tokens_is_the_output_counted():
    snap = _mids(1000, None)
    (small,), _ = _run([_row(max_tokens=64)], snap)            # 900 + 64 fits
    (large,), _ = _run([_row(max_tokens=101)], snap)           # 900 + 101 does not
    assert (small.current.model, large.current.model) == ("m1", "m2")


def test_s7_undeclared_window_never_overflows():                           # S7
    (r,), _ = _run([_row(prompt_tokens=5_000_000)], _mids(None, None))
    assert r.current.outcome == "routed" and r.current.model == "m1"
    assert r.current.overflow_hops == 0


def test_s7_all_overflow_rejects_context_window():                         # S7
    """Both reachable endpoints overflow: the third select finds nothing left
    and the row is rejected `context_window` (live: 400), never `no_eligible`."""
    (r,), _ = _run([_row()], _mids(1000, 1200))
    assert (r.current.outcome, r.current.reject_codes) == ("rejected", ("context_window",))
    assert r.current.attempted == ("m1", "m2") and r.current.overflow_hops == 2
    assert (r.current.model, r.current.cost_usd, r.current.core) == (None, None, ())
    assert r.current.eligible == ("m1", "m2")       # policy still admits both


def test_bound_is_max_failover_attempts_exactly_as_live():                # Review Focus 4
    """Three overflowing endpoints ahead of one that fits. With the shipped
    bound (2 extra selects) the row is rejected although `m4` would fit --
    which is what the live gateway does. Raising the bound to 3 reaches it;
    a bound of 0 makes the first overflow terminal."""
    windows = (1000, 1000, 1000, None)
    (default,), _ = _run([_row()], _mids(*windows))
    assert BASE_SETTINGS.routing.max_failover_attempts == 2
    assert default.current.reject_codes == ("context_window",)
    assert default.current.attempted == ("m1", "m2", "m3")
    (raised,), _ = _run([_row()], _mids(*windows, max_failover=3))
    assert raised.current.model == "m4" and raised.current.overflow_hops == 3
    (zero,), _ = _run([_row()], _mids(1000, None, max_failover=0))
    assert zero.current.reject_codes == ("context_window",)
    assert zero.current.attempted == ("m1",)


def test_overflow_can_leave_the_tier_when_it_empties():
    """The only mid endpoint overflows; excluding it leaves the frontier tier."""
    snap = _mids(1000, extra=[onboarded_row("big", "frontier", context_window=None)])
    (r,), _ = _run([_row()], snap)
    assert (r.current.model, r.current.tier, r.current.overflow_hops) == ("big", "frontier", 1)


def test_a_proposed_context_window_change_now_moves_the_route():
    """Reference doc §6 used to say this had no visible effect."""
    results, excluded = _run([_row("a"), _row("b")], _mids(1000, None), {"schema": P,
        "endpoints": {"update": {"m1": {"context_window": 8000}}}})
    for r in results:
        assert (r.current.model, r.proposed.model) == ("m2", "m1")
        assert (r.current.overflow_hops, r.proposed.overflow_hops) == (1, 0)
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["route_changed"] == 2
    assert sim["totals"]["overflow_hops"] == {"current": 2, "proposed": 0}
    assert "| overflow_hops | 2 | 0 | -2 |" in render_report(sim)


def test_token_scale_is_applied_before_the_window_check():
    """At 0.5x the 900-token prompt is 450: 450 + 512 fits m1's window."""
    (r,), _ = _run([_row()], _mids(1000, None),
                   {"schema": P, "workload": {"token_scale": 0.5}})
    assert (r.current.model, r.proposed.model) == ("m2", "m1")


def test_shrinking_a_window_newly_rejects_and_is_keyed_context_window():
    results, excluded = _run([_row()], _mids(None), {"schema": P,
        "endpoints": {"update": {"m1": {"context_window": 1000}}}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_rejected"] == 1
    assert sim["new_rejections"]["by_code"] == {"context_window": 1}
    assert sim["new_rejections"]["by_core"] == {"(context_window)": 1}
    assert sim["totals"]["overflow_hops"] == {"current": 0, "proposed": 1}


def test_baseline_compares_the_first_decision_not_the_served_model():     # Review Focus 4
    """The record says m1 was tried first and m2 served. Replay reproduces
    exactly that; the fidelity check must call it agreement, and a rejected
    arm must still count as a disagreement."""
    failover = _row("f", attempts=["m1", "m2"], chosen="m2")
    results, excluded = _run([failover], _mids(1000, None))
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert results[0].current.model == "m2"
    assert sim["baseline"]["agreement"] == 1.0 and sim["baseline"]["top_disagreements"] == []
    served_live = _row("s", attempts=["m1"], chosen="m1")
    results, excluded = _run([served_live], _mids(1000, 1200))     # replay: all overflow
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["baseline"]["agreement"] == 0.0
    assert sim["baseline"]["top_disagreements"] == [
        {"recorded": "m1", "simulated": "(rejected)", "count": 1}]


def test_caveat_names_the_overflow_re_selection():
    assert "plus deterministic context-window overflow" in REPORT_CAVEAT
    assert "first routing" in REPORT_CAVEAT


_WINDOW = st.sampled_from([None, 600, 1000, 4000])


@hsettings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(windows=st.lists(_WINDOW, min_size=1, max_size=4),
       prompt_tokens=st.integers(1, 6000),
       max_tokens=st.sampled_from([None, 64, 2000]),
       max_failover=st.integers(0, 3))
def test_s7_overflowing_pick_is_never_served(windows, prompt_tokens, max_tokens,  # S7
                                             max_failover):
    snap = _mids(*windows, max_failover=max_failover)
    declared = {f"m{i + 1}": w for i, w in enumerate(windows)}
    needed = prompt_tokens + (max_tokens or DEFAULT_OUTPUT)
    (r,), _ = _run([_row(prompt_tokens=prompt_tokens, max_tokens=max_tokens)], snap)
    arm = r.current

    def overflows(name):
        return declared[name] is not None and needed > declared[name]

    assert arm.overflow_hops <= max_failover + 1
    if arm.outcome == "routed":
        assert not overflows(arm.model)                      # the served pick always fits
        assert arm.attempted[-1] == arm.model
        assert all(overflows(name) for name in arm.attempted[:-1])
        assert arm.overflow_hops == len(arm.attempted) - 1 <= max_failover
    else:
        assert arm.reject_codes == ("context_window",)
        assert arm.attempted and all(overflows(name) for name in arm.attempted)
    assert len(set(arm.attempted)) == len(arm.attempted)     # never re-selects an excluded pick
    assert r.proposed == arm                                 # empty proposal: both arms agree
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_overflow.py -p no:warnings
```

Expected: the tests fail with `AttributeError: 'ArmResult' object has no attribute 'attempted'` (and `overflow_hops`).

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index bb56489..42e8cca 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -1,8 +1,9 @@
 """Policy simulator replay engine (P3a, spec §7): offline, operator-run.
 
-Replays each recorded P2b-2 outcome row's **FIRST routing decision only**
-through the real serving code -- `build_context` -> `feasible_set` ->
-`Router.select` -- under **both** arms:
+Replays each recorded P2b-2 outcome row's **first routing decision** through
+the real serving code -- `build_context` -> `feasible_set` ->
+`Router.select` -- plus the gateway's deterministic context-window overflow
+re-selection (P3c-2), under **both** arms:
 
 - `current`  = the config snapshot as exported;
 - `proposed` = the snapshot with a policy proposal applied.
@@ -20,7 +21,8 @@ Deliberate non-goals (spec §2):
 - no re-running PII detection -- recorded entity TYPES are replayed as-is;
 - no daily-spend / RPM admission simulation;
 - no circuit-breaker state (both arms run with no open circuits);
-- no confidence-escalation or failover replay -- only the first decision.
+- no confidence-escalation or failover replay -- only the first decision,
+  and the overflow re-selection that is a pure function of declared data.
 
 Only `NoEligibleCandidate` and `ContextWindowExceeded` are recorded as
 rejections. Any other exception propagates: a simulator bug must surface,
@@ -73,6 +75,16 @@ class ArmResult:
     # P3c-2: True when `workload.token_scale` shrank a row whose recorded
     # complexity was saturated, so its new complexity is an estimate.
     complexity_estimated: bool = False
+    # P3c-2 spec §5: every model this arm selected, in order -- the picks
+    # that overflowed their declared context window, then (when routed) the
+    # one served. `attempted[0]` is the first routing decision.
+    attempted: tuple[str, ...] = ()
+
+    @property
+    def overflow_hops(self) -> int:
+        """Picks excluded because prompt + output exceeded their window."""
+        served = 1 if self.outcome == "routed" else 0
+        return max(0, len(self.attempted) - served)
 
 
 @dataclass(frozen=True)
@@ -206,10 +218,11 @@ def observed_latencies(records: list[dict]) -> dict[str, float]:
 
 def _rejected(eligible: tuple[str, ...], codes: tuple[str, ...], *,
               core: tuple[str, ...] = (), core_state: tuple[str, ...] = (),
-              complexity_estimated: bool = False) -> ArmResult:
+              complexity_estimated: bool = False,
+              attempted: tuple[str, ...] = ()) -> ArmResult:
     return ArmResult("rejected", None, None, None, eligible, codes,
                      None, None, None, None, None, core, core_state,
-                     complexity_estimated=complexity_estimated)
+                     complexity_estimated=complexity_estimated, attempted=attempted)
 
 
 # app/pipeline.py: `complexity = min(1.0, comp.tokens_after / 2000)`. The
@@ -244,7 +257,9 @@ def scaled_request(prompt_tokens: int, complexity: float,
 
 
 def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmResult:
-    """One row's first routing decision under one scenario."""
+    """One row's routing decision under one scenario: the first decision,
+    then -- only when the pick's declared context window is too small -- the
+    same exclude-and-reselect the live gateway does (P3c-2 spec §5)."""
     app = scn.apps[row["application_id"]]
     # Placeholder entities: only `.type` is read on the context path.
     entities = [Entity(type=t, start=0, end=0, value="")
@@ -270,33 +285,56 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
     prompt_tokens, complexity, estimated = scaled_request(
         int(row["prompt_tokens"]), float(row["complexity"]),
         scn.assumptions.scale_for(row["application_id"]))
-    try:
-        # B55: price at the client's recorded output cap, as the live
-        # gateway does (`expected_output_tokens(settings, params)`); no cap
-        # recorded -> the deployment's default estimate.
-        output_tokens = expected_output_tokens(
-            scn.settings, {"max_tokens": row["max_tokens"]} if row.get("max_tokens") else None)
-        decision = scn.router.select(ctx, tokens_after=prompt_tokens,
-                                     complexity=complexity,
-                                     output_tokens=output_tokens)
-    except NoEligibleCandidate as exc:
-        # P3b: the router attaches the minimal conflicting core to every
-        # NoEligibleCandidate it raises (None only if computing the core
-        # itself failed) -- carry it through so the summary can group
-        # rejections by why, not just by code.
-        core = exc.core.ids() if exc.core else ()
-        core_state = exc.core.state_ids() if exc.core else ()
-        if not exc.rejected:
-            return _rejected(eligible, ("empty_pool",), core=core, core_state=core_state,
+    # B55: price at the client's recorded output cap, as the live gateway
+    # does (`expected_output_tokens(settings, params)`); no cap recorded ->
+    # the deployment's default estimate.
+    output_tokens = expected_output_tokens(
+        scn.settings, {"max_tokens": row["max_tokens"]} if row.get("max_tokens") else None)
+
+    # P3c-2 spec §5: mirror `Gateway.run`'s overflow handling. A pick whose
+    # DECLARED window cannot hold prompt + output is excluded and the router
+    # re-selects, at most `max_failover_attempts` extra times -- the live
+    # bound. An undeclared window (None) never overflows: replay cannot
+    # invent a limit. This is the only later-hop behaviour replayed.
+    max_failover = int(scn.settings.routing.max_failover_attempts)
+    overflowed: list[str] = []          # picks that overflowed, in order
+    while True:
+        try:
+            decision = scn.router.select(ctx, tokens_after=prompt_tokens,
+                                         complexity=complexity,
+                                         output_tokens=output_tokens,
+                                         exclude=frozenset(overflowed))
+        except NoEligibleCandidate as exc:
+            if overflowed:
+                # Every reachable candidate overflowed. The live gateway
+                # reports that as ContextWindowExceeded (400), not a 422.
+                return _rejected(eligible, ("context_window",),
+                                 complexity_estimated=estimated,
+                                 attempted=tuple(overflowed))
+            # P3b: the router attaches the minimal conflicting core to every
+            # NoEligibleCandidate it raises (None only if computing the core
+            # itself failed) -- carry it through so the summary can group
+            # rejections by why, not just by code.
+            core = exc.core.ids() if exc.core else ()
+            core_state = exc.core.state_ids() if exc.core else ()
+            if not exc.rejected:
+                return _rejected(eligible, ("empty_pool",), core=core, core_state=core_state,
+                                 complexity_estimated=estimated)
+            fallback = "over_budget" if "budget filter" in str(exc) else "unspecified"
+            codes = tuple(sorted({code or fallback for _name, code in exc.rejected}))
+            return _rejected(eligible, codes, core=core, core_state=core_state,
                              complexity_estimated=estimated)
-        fallback = "over_budget" if "budget filter" in str(exc) else "unspecified"
-        codes = tuple(sorted({code or fallback for _name, code in exc.rejected}))
-        return _rejected(eligible, codes, core=core, core_state=core_state,
-                         complexity_estimated=estimated)
-    except ContextWindowExceeded:
-        return _rejected(eligible, ("context_window",), complexity_estimated=estimated)
-
-    pick = decision.chosen
+        except ContextWindowExceeded:
+            return _rejected(eligible, ("context_window",), complexity_estimated=estimated,
+                             attempted=tuple(overflowed))
+        pick = decision.chosen
+        if pick.context_window is None or prompt_tokens + output_tokens <= pick.context_window:
+            break
+        overflowed.append(pick.name)
+        if len(overflowed) > max_failover:
+            return _rejected(eligible, ("context_window",), complexity_estimated=estimated,
+                             attempted=tuple(overflowed))
+
     # P3c-2: `workload.cached_share` of the (scaled) prompt is billed at the
     # endpoint's cached-input price. The ROUTER still estimates at zero
     # cached tokens, as it does live, so the share moves cost and never a
@@ -321,9 +359,11 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
             latency = scn.signals.latency(pick.name, pick.tier).value
             latency_source = "tier_default"
     q = scn.quality.resolve(pick.name, row.get("workload"))
+    # The endpoint served AFTER any overflow hops is the one costed.
     return ArmResult("routed", pick.name, pick.provider, pick.tier, eligible, (),
                      cost, latency, latency_source, q.score, q.source,
-                     complexity_estimated=estimated)
+                     complexity_estimated=estimated,
+                     attempted=(*overflowed, pick.name))
 
 
 def run_rows(records: list[dict], current: Scenario, proposed: Scenario, *,
@@ -391,9 +431,9 @@ def agreement_ok(baseline: dict, minimum: float) -> bool:
 #: every line so a multi-line quote stays a single blockquote).
 REPORT_CAVEAT = (
     "**Simulation only.** Nothing in this report has been applied. It predicts the first routing\n"
-    "decision of recorded requests under the proposal, using the real eligibility and routing\n"
-    "code and a frozen snapshot of signals. It does not model failover, escalation, breaker\n"
-    "state or caching."
+    "decision of recorded requests under the proposal, plus deterministic context-window overflow\n"
+    "re-selection, using the real eligibility and routing code and a frozen snapshot of signals.\n"
+    "It does not model other failover, escalation, breaker state or caching."
 )
 
 
@@ -421,13 +461,14 @@ def _delta(current: float | None, proposed: float | None) -> dict:
 
 def _bucket_stats(rows: list[RowResult]) -> dict:
     """The `{requests, route_changed, newly_rejected, newly_served, cost_usd,
-    cost_usd_both_routed, latency_ms, quality_mean, onprem_share}` block shared by `totals`,
+    cost_usd_both_routed, latency_ms, quality_mean, onprem_share, overflow_hops}` block shared by `totals`,
     `by_app.<app>` and `by_workload.<workload>` (spec §8)."""
     cost = {}
     lat_p50 = {}
     lat_p95 = {}
     quality = {}
     onprem_share = {}
+    overflow_hops = {}
     for arm in ("current", "proposed"):
         routed = [getattr(r, arm) for r in rows if getattr(r, arm).outcome == "routed"]
         cost[arm] = sum(a.cost_usd or 0.0 for a in routed)
@@ -438,6 +479,9 @@ def _bucket_stats(rows: list[RowResult]) -> dict:
         quality[arm] = _mean(q_vals)
         onprem_share[arm] = ((sum(1 for a in routed if a.provider == "onprem") / len(routed))
                               if routed else None)
+        # Over every row, routed or not: a row rejected for overflowing every
+        # reachable endpoint spent hops too.
+        overflow_hops[arm] = sum(getattr(r, arm).overflow_hops for r in rows)
 
     # Like-for-like cost: only rows routed in BOTH arms. `cost_usd` above
     # sums each arm's routed rows, so a newly rejected row simply drops out
@@ -458,9 +502,20 @@ def _bucket_stats(rows: list[RowResult]) -> dict:
                        "p95": _delta(lat_p95["current"], lat_p95["proposed"])},
         "quality_mean": _delta(quality["current"], quality["proposed"]),
         "onprem_share": {"current": onprem_share["current"], "proposed": onprem_share["proposed"]},
+        "overflow_hops": {"current": overflow_hops["current"],
+                          "proposed": overflow_hops["proposed"]},
     }
 
 
+def _first_pick(arm: ArmResult) -> str | None:
+    """The model the arm's FIRST select chose, before any overflow
+    re-selection; None when the arm rejected the row. An `ArmResult` built
+    without `attempted` (older callers) reads as its served model."""
+    if arm.outcome != "routed":
+        return None
+    return arm.attempted[0] if arm.attempted else arm.model
+
+
 def _baseline(results: list[RowResult]) -> dict:
     """Fidelity check (spec §7): does the `current` arm's pick agree with the
     recorded first attempt, over simulated `ok` rows with a recorded
@@ -473,15 +528,18 @@ def _baseline(results: list[RowResult]) -> dict:
     by_app: dict[str, float] = {}
     disagreements: Counter = Counter()
     if rows_compared:
-        agree_count = sum(1 for r in compared if r.current.model == r.row["attempts"][0])
+        # First decision against first recorded attempt: an overflow hop
+        # replay reproduces (P3c-2) must not read as a disagreement.
+        agree_count = sum(1 for r in compared
+                          if _first_pick(r.current) == r.row["attempts"][0])
         agreement = agree_count / rows_compared
         for app_id in sorted({r.row["application_id"] for r in compared}):
             sub = [r for r in compared if r.row["application_id"] == app_id]
             by_app[app_id] = sum(
-                1 for r in sub if r.current.model == r.row["attempts"][0]) / len(sub)
+                1 for r in sub if _first_pick(r.current) == r.row["attempts"][0]) / len(sub)
         for r in compared:
             recorded = r.row["attempts"][0]
-            simulated = r.current.model or "(rejected)"
+            simulated = _first_pick(r.current) or "(rejected)"
             if recorded != simulated:
                 disagreements[(recorded, simulated)] += 1
 
@@ -529,7 +587,13 @@ def _new_rejections(results: list[RowResult]) -> dict:
             by_app[r.row["application_id"]] += 1
             for code in r.proposed.reject_codes:
                 by_code[code] += 1
-            by_core[_core_key(r.proposed.core, r.proposed.core_state)] += 1
+            # A context-window rejection has no conflicting core -- no
+            # requirement refused it, the request is too large -- so it gets
+            # its own key instead of reading as "(unknown)".
+            if r.proposed.reject_codes == ("context_window",):
+                by_core["(context_window)"] += 1
+            else:
+                by_core[_core_key(r.proposed.core, r.proposed.core_state)] += 1
     return {"by_app": dict(by_app), "by_code": dict(by_code), "by_core": dict(by_core)}
 
 
@@ -641,6 +705,9 @@ def _metrics_table(stats: dict) -> list[str]:
     lines.append(f"| quality_mean | {_fmt(q['current'])} | {_fmt(q['proposed'])} | {_fmt(q['delta'])} |")
     o = stats["onprem_share"]
     lines.append(f"| onprem_share | {_fmt(o['current'])} | {_fmt(o['proposed'])} | n/a |")
+    h = stats["overflow_hops"]
+    lines.append(f"| overflow_hops | {h['current']} | {h['proposed']} | "
+                 f"{h['proposed'] - h['current']} |")
     return lines
 
 
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_replay_overflow.py app/eval/simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): replay mirrors the gateway's context-window overflow re-selection (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 7: `app/eval/scenarios.py`: load and validate a scenarios file (spec §3)

**Files:**
- Create: `app/eval/scenarios.py`
- Test: create `tests/eval/test_scenarios_load.py`

**Interfaces:**
- Consumes: `app.eval.proposal.SCHEMA` (`"routeforge.policy-proposal/v1"`), `ProposalError` (`.where`, `.message`), `_validate_schema(doc)`, `_validate_keys(doc)`, `apply_proposal(snapshot, proposal, *, base) -> dict`.
- Produces:
  - `SCHEMA = "routeforge.policy-scenarios/v1"`, `MAX_SCENARIOS = 20`, `MAX_NAME_LEN = 64`.
  - `ScenarioError(where: str, message: str)` — `ValueError` with `.where`, `.message`; `str()` is `"<where>: <message>"`.
  - `validate_scenarios(doc) -> None` — shape only.
  - `load_scenarios(path: str) -> tuple[dict, str]` — `(doc, sha256 hex of the raw bytes)`.
  - `AppliedScenario` — frozen dataclass `name: str`, `proposal: dict` (the body as a full v1 proposal), `doc: dict` (the applied snapshot).
  - `apply_scenarios(snapshot: dict, doc: dict, *, base: Settings) -> list[AppliedScenario]` — file order, all-or-nothing.

**Rules.** Names are matched with `re.fullmatch`, never `match` with `$` (which accepts a trailing newline). A proposal body's `schema` is optional; when present it is checked exactly as a proposal file's is. An error inside a proposal is re-raised as `ScenarioError` with `where` = `scenarios[<i>](<name>).proposal.<the ProposalError's where>`. A `SnapshotError` is a snapshot problem and propagates unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_scenarios_load.py`:

````python
"""P3c-2 Task 7: the `routeforge.policy-scenarios/v1` file (spec §3)."""
from __future__ import annotations

import copy
import json

import pytest

from app.eval.scenario import SnapshotError
from app.eval.scenarios import (MAX_SCENARIOS, SCHEMA, ScenarioError, apply_scenarios,
                                load_scenarios, validate_scenarios)
from tests.eval.sim_fixtures import BASE_SETTINGS, make_snapshot

P = "routeforge.policy-proposal/v1"


def _doc(*scenarios, **top):
    return {"schema": SCHEMA, "title": "t",
            "scenarios": [{"name": n, "proposal": p} for n, p in scenarios], **top}


def _write(tmp_path, doc):
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_loads_and_hashes_the_file(tmp_path):
    doc = _doc(("all-onprem", {"apps": {"app-a": {"residency": "onprem_only"}}}),
               ("empty", {}))
    loaded, sha = load_scenarios(_write(tmp_path, doc))
    assert loaded == doc and len(sha) == 64


def test_title_is_optional_and_proposal_schema_is_optional():
    validate_scenarios({"schema": SCHEMA, "scenarios": [{"name": "a", "proposal": {}}]})
    validate_scenarios(_doc(("a", {"schema": P, "title": "with schema"})))


@pytest.mark.parametrize("name", ["a", "onprem+2nd-gpu", "raw.pii_only", "A-1", "x" * 64])
def test_legal_names(name):
    validate_scenarios(_doc((name, {})))


@pytest.mark.parametrize("name", [
    "", "x" * 65, "two words", "tab\tname", "a/b", "name\n", "\nname",   # trailing newline
    "naïve", "ｆｕｌｌｗｉｄｔｈ", "٣", "a,b", "a(b)", None, 7, ["a"],
])
def test_illegal_names_are_refused(name):                                 # Review Focus 5
    with pytest.raises(ScenarioError) as exc:
        validate_scenarios(_doc((name, {})))
    assert exc.value.where == "scenarios[0].name"


def test_duplicate_name_names_the_second_occurrence():                    # Review Focus 5
    with pytest.raises(ScenarioError) as exc:
        validate_scenarios(_doc(("a", {}), ("b", {}), ("a", {})))
    assert exc.value.where == "scenarios[2].name" and "duplicate" in str(exc.value)


@pytest.mark.parametrize("doc,where", [
    ([], "<root>"),
    ({"scenarios": [{"name": "a", "proposal": {}}]}, "schema"),
    ({"schema": "routeforge.policy-scenarios/v2", "scenarios": []}, "schema"),
    ({"schema": P, "scenarios": []}, "schema"),                # a proposal schema is not ours
    (_doc(("a", {}), extra=1), "extra"),
    (_doc(("a", {}), title=5), "title"),
    ({"schema": SCHEMA, "title": "t"}, "scenarios"),
    ({"schema": SCHEMA, "scenarios": {}}, "scenarios"),
    ({"schema": SCHEMA, "scenarios": []}, "scenarios"),
    (_doc(*[(f"s{i}", {}) for i in range(MAX_SCENARIOS + 1)]), "scenarios"),
    ({"schema": SCHEMA, "scenarios": ["a"]}, "scenarios[0]"),
    ({"schema": SCHEMA, "scenarios": [{"name": "a", "proposal": {}, "rank": 1}]},
     "scenarios[0].rank"),
    ({"schema": SCHEMA, "scenarios": [{"name": "a"}]}, "scenarios[0](a).proposal"),
    (_doc(("a", [])), "scenarios[0](a).proposal"),
    (_doc(("a", {}), ("b", {"bogus": 1})), "scenarios[1](b).proposal.bogus"),
    (_doc(("a", {"schema": "routeforge.policy-proposal/v2"})), "scenarios[0](a).proposal.schema"),
    (_doc(("a", {"apps": {"app-a": {"residencyy": "any"}}})),
     "scenarios[0](a).proposal.apps.app-a.residencyy"),
    (_doc(("a", {"workload": {"tokenscale": 1}})), "scenarios[0](a).proposal.workload.tokenscale"),
    (_doc(("a", {"endpoints": {"update": {"m": {"latency": 5}}}})),
     "scenarios[0](a).proposal.endpoints.update.m.latency"),
])
def test_shape_errors_name_the_location(doc, where):
    with pytest.raises(ScenarioError) as exc:
        validate_scenarios(doc)
    assert exc.value.where == where, str(exc.value)


def test_twenty_scenarios_is_the_limit_not_past_it():
    validate_scenarios(_doc(*[(f"s{i}", {}) for i in range(MAX_SCENARIOS)]))


def test_invalid_json_is_named(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    with pytest.raises(ScenarioError) as exc:
        load_scenarios(str(path))
    assert exc.value.where == "file"


def test_apply_scenarios_applies_each_in_file_order():
    doc = _doc(("tight", {"apps": {"app-a": {"residency": "onprem_only"}}}),
               ("empty", {}),
               ("scaled", {"schema": P, "workload": {"token_scale": 0.5}}))
    snap = make_snapshot()
    before_snap, before_doc = copy.deepcopy(snap), copy.deepcopy(doc)
    applied = apply_scenarios(snap, doc, base=BASE_SETTINGS)
    assert [a.name for a in applied] == ["tight", "empty", "scaled"]
    assert applied[0].doc["apps"][0]["policy"]["residency"] == "onprem_only"
    assert applied[1].doc == snap                              # an empty body is an identity
    assert applied[2].doc["assumptions"]["token_scale"] == {"*": 0.5}
    assert all(a.proposal["schema"] == P for a in applied)     # bodies become full proposals
    assert (snap, doc) == (before_snap, before_doc)            # inputs never mutated


@pytest.mark.parametrize("body,where", [
    ({"apps": {"ghost": {"residency": "any"}}}, "scenarios[2](third).proposal.apps.ghost"),
    ({"apps": {"app-a": {"residency": "moon"}}},
     "scenarios[2](third).proposal.apps.app-a.residency"),
    ({"endpoints": {"remove": ["ghost"]}}, "scenarios[2](third).proposal.endpoints.remove.ghost"),
    ({"workload": {"token_scale": 0}}, "scenarios[2](third).proposal.workload.token_scale"),
    ({"workload": {"apps": {"ghost": {"cached_share": 0.5}}}},
     "scenarios[2](third).proposal.workload.apps.ghost"),
    ({"endpoints": {"update": {"local-mid": {"latency_ms": -1}}}},
     "scenarios[2](third).proposal.endpoints.update.local-mid.latency_ms"),
])
def test_value_errors_carry_index_name_and_dotted_path(body, where):      # Review Focus 5
    """All-or-nothing: scenarios 0 and 1 are valid, the third is not -- the
    whole file fails, naming exactly where."""
    doc = _doc(("first", {}), ("second", {"workload": {"token_scale": 2}}), ("third", body))
    with pytest.raises(ScenarioError) as exc:
        apply_scenarios(make_snapshot(), doc, base=BASE_SETTINGS)
    assert exc.value.where == where, str(exc.value)
    assert str(exc.value).startswith(where + ": ")


def test_a_snapshot_problem_is_not_blamed_on_a_scenario():
    snap = make_snapshot()
    snap["settings"]["providers"]["mystery"] = {
        "enabled": True, "region": None, "prompt_retention": None,
        "accepts_raw_sensitive": None}
    with pytest.raises(SnapshotError) as exc:
        apply_scenarios(snap, _doc(("a", {})), base=BASE_SETTINGS)
    assert exc.value.where == "settings.providers.mystery"
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_scenarios_load.py -p no:warnings
```

Expected: collection error: `ModuleNotFoundError: No module named 'app.eval.scenarios'`.

- [ ] **Step 3: Implement**

Create `app/eval/scenarios.py`:

````python
"""P3c-2: alternative scenarios, compared side by side (offline, operator-run).

An administrator writes several candidate policy changes into one
`routeforge.policy-scenarios/v1` file. Each scenario is a named
`routeforge.policy-proposal/v1` BODY; every one is replayed over the same
recorded traffic against the same `current` arm (`app/eval/simulate.py`).

RouteForge never picks, ranks or recommends a scenario: they are reported in
file order, and nothing is applied (handoff §28).

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.config.schema import Settings
from app.eval.proposal import (SCHEMA as PROPOSAL_SCHEMA, ProposalError, _validate_keys,
                               _validate_schema, apply_proposal)

SCHEMA = "routeforge.policy-scenarios/v1"
_SCHEMA_PREFIX = "routeforge.policy-scenarios/"
_SCHEMA_MAJOR = "v1"

_TOP_KEYS = frozenset({"schema", "title", "scenarios"})
_SCENARIO_KEYS = frozenset({"name", "proposal"})
MAX_SCENARIOS = 20
MAX_NAME_LEN = 64
# Matched with `fullmatch`, never `match` + `$`: `$` also matches before a
# trailing newline, which would let "name\n" through.
_NAME = re.compile(r"[A-Za-z0-9._+-]+")


class ScenarioError(ValueError):
    """A scenarios-file load/validate error. Always names the offending
    location -- `scenarios[<i>](<name>).proposal.<dotted path>` for an error
    inside a scenario's proposal -- mirroring `ProposalError`."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class AppliedScenario:
    name: str
    proposal: dict      # the scenario's proposal body as a full v1 proposal
    doc: dict           # the snapshot with that proposal applied


def _scenario_where(index: int, name: str) -> str:
    return f"scenarios[{index}]({name})"


def validate_scenarios(doc) -> None:
    """Shape-only checks (no snapshot needed): the schema, the closed key
    sets, 1..20 scenarios, legal unique names, and every proposal body's own
    keys. Values are checked against the snapshot by `apply_scenarios`."""
    if not isinstance(doc, dict):
        raise ScenarioError("<root>", "must be an object")
    schema = doc.get("schema")
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise ScenarioError("schema", f"must start with {_SCHEMA_PREFIX!r}; got {schema!r}")
    major = schema[len(_SCHEMA_PREFIX):].split(".", 1)[0]
    if major != _SCHEMA_MAJOR:
        raise ScenarioError("schema", f"major version must be {_SCHEMA_MAJOR!r}; got {major!r}")
    for key in doc:
        if key not in _TOP_KEYS:
            raise ScenarioError(key, "unknown key")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        raise ScenarioError("title", "must be a string")

    scenarios = doc.get("scenarios")
    if not isinstance(scenarios, list):
        raise ScenarioError("scenarios", "must be a list")
    if not 1 <= len(scenarios) <= MAX_SCENARIOS:
        raise ScenarioError(
            "scenarios", f"must list 1 to {MAX_SCENARIOS} scenarios; got {len(scenarios)}")

    seen: set[str] = set()
    for i, entry in enumerate(scenarios):
        if not isinstance(entry, dict):
            raise ScenarioError(f"scenarios[{i}]", "must be an object")
        for key in entry:
            if key not in _SCENARIO_KEYS:
                raise ScenarioError(f"scenarios[{i}].{key}", "unknown key")
        name = entry.get("name")
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or len(name) > MAX_NAME_LEN):
            raise ScenarioError(
                f"scenarios[{i}].name",
                f"must be 1 to {MAX_NAME_LEN} characters of [A-Za-z0-9._+-]; got {name!r}")
        if name in seen:
            raise ScenarioError(f"scenarios[{i}].name", f"duplicate scenario name {name!r}")
        seen.add(name)

        where = f"{_scenario_where(i, name)}.proposal"
        body = entry.get("proposal")
        if not isinstance(body, dict):
            raise ScenarioError(where, "must be an object")
        try:
            # `schema` is optional on a body; when present it must be a v1
            # proposal schema, checked exactly as a proposal file's is.
            if "schema" in body:
                _validate_schema(body)
            _validate_keys(body)
        except ProposalError as exc:
            raise ScenarioError(f"{where}.{exc.where}", exc.message) from exc


def load_scenarios(path: str) -> tuple[dict, str]:
    """Read a scenarios JSON file. Returns `(doc, sha256 hex)` -- the hash is
    over the raw file bytes. Raises `ScenarioError` on invalid JSON, a bad
    schema, an unknown key anywhere, or a bad scenario name."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScenarioError("file", f"invalid JSON: {exc}") from exc
    validate_scenarios(doc)
    return doc, digest


def apply_scenarios(snapshot: dict, doc: dict, *, base: Settings) -> list[AppliedScenario]:
    """Validate EVERY scenario's proposal against `snapshot` and apply it,
    in file order. All-or-nothing: the first invalid scenario raises
    `ScenarioError` naming its index, name and dotted path, before any
    replay starts. Never mutates `snapshot` or `doc`."""
    validate_scenarios(doc)
    applied: list[AppliedScenario] = []
    for i, entry in enumerate(doc["scenarios"]):
        proposal = {"schema": PROPOSAL_SCHEMA, **entry["proposal"]}
        try:
            applied_doc = apply_proposal(snapshot, proposal, base=base)
        except ProposalError as exc:
            raise ScenarioError(
                f"{_scenario_where(i, entry['name'])}.proposal.{exc.where}",
                exc.message) from exc
        applied.append(AppliedScenario(entry["name"], proposal, applied_doc))
    return applied
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_scenarios_load.py tests/eval/test_isolation.py -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_scenarios_load.py app/eval/scenarios.py
git commit -m "$(cat <<'MSG'
feat(eval): routeforge.policy-scenarios/v1 loader with nested error paths (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 8: `compare()` engine and the precomputed current arm (spec §7, S2, S9)

**Files:**
- Modify: `app/eval/simulate.py` (`run_rows`; new `arm_results` just above it)
- Modify: `app/eval/scenarios.py` (imports; new engine section at the end)
- Test: create `tests/eval/test_scenarios_compare.py`

**Interfaces:**
- Consumes:
  - Task 7's `apply_scenarios(snapshot, doc, *, base) -> list[AppliedScenario]` (`.name`, `.proposal`, `.doc`).
  - Task 1's `select_rows(..., since=)`, `_before_since`, `run_rows(records, current, proposed, *, since=None)`.
  - Task 4's `summarize(results, excluded, *, inputs, edits, assumptions=None) -> dict` (keys: `schema`, `simulation_id`, `inputs` (with `window`), `edits`, `assumptions`, `rows`, `baseline`, `totals`, `by_app`, `by_workload`, `moves`, `new_rejections`, `affected_apps`, `sources`, `per_day`).
  - `normalized_edits(proposal) -> dict`, `assumptions_block(proposal) -> dict | None`, `build_scenario(doc, *, base) -> Scenario`, `close_scenario(scn)`.
- Produces:
  - `app.eval.simulate.arm_results(records: list[dict], scn: Scenario, *, since: datetime | None = None) -> dict[str, ArmResult]` — keyed by `request_id`.
  - `run_rows(records, current, proposed, *, since=None, current_results: dict[str, ArmResult] | None = None)`.
  - `app.eval.scenarios.Comparison` — frozen dataclass `rows: dict`, `baseline: dict`, `window: dict | None`, `scenarios: list[dict]` (file order; each `{"name", "edits", "assumptions", "summary"}`, where `summary` is the `summarize()` body minus `schema`, `simulation_id`, `inputs`, `rows`, `baseline`, `edits`, `assumptions`).
  - `async compare(records: list[dict], snapshot: dict, scenarios_doc: dict, *, base: Settings, since: datetime | None = None) -> Comparison`.
  - `ComparisonInvariantError(RuntimeError)`.

**Rules.** Every scenario is validated and applied before anything is built. `current` is built and simulated once. Each proposed scenario is built, replayed and closed in file order, so at most two scenarios are open at once. `rows`, `baseline` and `window` are taken from the first scenario's summary and must be identical for the rest; a difference raises `ComparisonInvariantError`. The single-proposal path (`simulate`) is untouched: without `current_results`, `run_rows` behaves as before.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_scenarios_compare.py`:

````python
"""P3c-2: the scenario comparison (spec §7-§9).

Task 8: the engine -- one shared `current` arm, every scenario replayed
against it. Task 9 (below): `comparison.json` and `report.md`."""
from __future__ import annotations

import asyncio
import copy

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

import app.eval.scenarios as scenarios_mod
from app.eval.proposal import assumptions_block, normalized_edits
from app.eval.scenario import build_scenario, close_scenario
from app.eval.scenarios import SCHEMA, ScenarioError, apply_scenarios, compare
from app.eval.simulate import arm_results, parse_since, run_rows, simulate, summarize
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot
from tests.eval.test_simulate import _TIGHTEN

P = "routeforge.policy-proposal/v1"


def _doc(*scenarios, title="Healthcare on-prem options"):
    return {"schema": SCHEMA, "title": title,
            "scenarios": [{"name": n, "proposal": p} for n, p in scenarios]}


def _compare(records, snap, doc, **kw):
    return asyncio.run(compare(records, snap, doc, base=BASE_SETTINGS, **kw))


def _snap():
    """Two apps; azure enabled so a residency tightening has somewhere to
    move traffic FROM."""
    return make_snapshot(apps=[app_entry("claims"), app_entry("chat")],
                         providers={"azure": {"enabled": True}})


def _rows(days=3):
    """Twelve rows over `days` days, two apps, three complexities."""
    rows = []
    for i in range(12):
        rows.append(make_row(
            f"r{i:02d}", "claims" if i % 2 else "chat",
            complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=[200, 1200, 3000][i % 3],
            ts=f"2026-09-{20 + i % days:02d}T12:00:{i:02d}+00:00"))
    return rows


_TRIAD = (
    ("all-onprem", {"apps": {"claims": {"residency": "onprem_only"}}}),
    ("smaller-prompts", {"workload": {"apps": {"claims": {"token_scale": 0.4}}}}),
    ("onprem+2nd-gpu", {"apps": {"claims": {"residency": "onprem_only"}},
                        "endpoints": {"add": [{
                            "slug": "local-mid-2", "provider": "onprem", "tier": "mid",
                            "price": {"input": 0.05, "output": 0.1, "cached_input": None},
                            "latency_ms": 90}]}}),
)


@pytest.mark.parametrize("name,body", _TRIAD)
def test_s2_single_scenario_equals_single_proposal(name, body):            # S2
    """A scenarios file holding one scenario P gives exactly what
    `--proposal P` gives: same rows, same baseline, same numbers."""
    proposal = {"schema": P, **body}
    results, excluded = asyncio.run(simulate(_rows(), _snap(), proposal, base=BASE_SETTINGS))
    sim = summarize(results, excluded, inputs={}, edits=normalized_edits(proposal),
                    assumptions=assumptions_block(proposal))
    cmp = _compare(_rows(), _snap(), _doc((name, body)))
    (entry,) = cmp.scenarios
    assert (cmp.rows, cmp.baseline, cmp.window) == (
        sim["rows"], sim["baseline"], sim["inputs"]["window"])
    assert (entry["name"], entry["edits"], entry["assumptions"]) == (
        name, sim["edits"], sim["assumptions"])
    assert set(entry["summary"]) == set(sim) - {
        "schema", "simulation_id", "inputs", "rows", "baseline", "edits", "assumptions"}
    for key, value in entry["summary"].items():
        assert value == sim[key], key
    totals = entry["summary"]["totals"]
    assert totals["route_changed"] > 0                         # not vacuous


def test_scenarios_come_back_in_file_order_with_shared_rows_and_baseline():
    cmp = _compare(_rows(), _snap(), _doc(*_TRIAD))
    assert [e["name"] for e in cmp.scenarios] == [n for n, _ in _TRIAD]
    assert cmp.rows["simulated"] == 12 and "before_since" in cmp.rows["excluded"]
    assert cmp.window == {"start": "2026-09-20T12:00:00+00:00",
                          "end": "2026-09-22T12:00:11+00:00"}
    assert set(cmp.baseline) == {"rows_compared", "agreement", "warning", "by_app",
                                 "top_disagreements"}
    for entry in cmp.scenarios:
        assert set(entry) == {"name", "edits", "assumptions", "summary"}
        assert set(entry["summary"]) == {"totals", "by_app", "by_workload", "moves",
                                         "new_rejections", "affected_apps", "sources",
                                         "per_day"}


def test_since_reaches_every_scenario():
    cmp = _compare(_rows(), _snap(), _doc(*_TRIAD), since=parse_since("2026-09-21T00:00:00Z"))
    assert cmp.rows["simulated"] == 8 and cmp.rows["excluded"]["before_since"] == 4
    assert all(e["summary"]["totals"]["requests"] == 8 for e in cmp.scenarios)


@hsettings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tightenings=st.lists(_TIGHTEN, min_size=1, max_size=3),
       disable=st.lists(st.sampled_from(["local-cheap", "local-mid", "local-big"]),
                        unique=True, max_size=2),
       etypes=st.lists(st.sampled_from(["US_SSN", "EMAIL_ADDRESS", "PERSON"]),
                       unique=True, max_size=2),
       complexity=st.floats(0.0, 1.0))
def test_s9_scenario_proposals_never_widen_eligibility(tightenings, disable, etypes,  # S9
                                                       complexity):
    """The P3a stricter-never-widens property, per scenario of a generated
    scenarios file -- and `onprem_only` stays absolute in every scenario."""
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    bodies = []
    for tighten in tightenings:
        body = {"apps": {"app-a": tighten}}
        if disable:
            body["endpoints"] = {"disable": disable}
        bodies.append(body)
    doc = _doc(*[(f"s{i}", body) for i, body in enumerate(bodies)])
    rows = [make_row("h", "app-a", detected_entity_types=etypes, complexity=complexity)]
    cmp = _compare(rows, snap, doc)
    for item, entry in zip(apply_scenarios(snap, doc, base=BASE_SETTINGS), cmp.scenarios):
        (r,), _ = asyncio.run(simulate(rows, snap, item.proposal, base=BASE_SETTINGS))
        assert set(r.proposed.eligible) <= set(r.current.eligible)
        if item.proposal["apps"]["app-a"].get("residency") == "onprem_only":
            assert r.proposed.provider in (None, "onprem")
        # a tightening can never newly serve a row, and compare() agrees with the row
        totals = entry["summary"]["totals"]
        assert totals["newly_served"] == 0
        assert totals["newly_rejected"] == int(
            r.current.outcome == "routed" and r.proposed.outcome == "rejected")


# --- the shared current arm -----------------------------------------------------

def test_current_arm_is_simulated_once_per_row(monkeypatch):
    """Three scenarios over 12 rows: 12 `current` simulations, not 36."""
    import app.eval.simulate as sim_mod
    seen: dict[int, int] = {}
    real = sim_mod.simulate_row

    def counting(scn, row, observed):
        seen[id(scn)] = seen.get(id(scn), 0) + 1
        return real(scn, row, observed)
    monkeypatch.setattr(sim_mod, "simulate_row", counting)
    _compare(_rows(), _snap(), _doc(*_TRIAD))
    # four scenarios were built (current + three proposed), each simulated 12 times
    assert sum(seen.values()) == 48
    assert all(count % 12 == 0 for count in seen.values())


def test_precomputed_current_arm_equals_recomputing_it():
    async def both(records):
        current = await build_scenario(_snap(), base=BASE_SETTINGS)
        (item,) = apply_scenarios(_snap(), _doc(_TRIAD[0]), base=BASE_SETTINGS)
        proposed = await build_scenario(item.doc, base=BASE_SETTINGS)
        try:
            plain = run_rows(records, current, proposed)
            pre = run_rows(records, current, proposed,
                           current_results=arm_results(records, current))
        finally:
            await close_scenario(proposed)
            await close_scenario(current)
        return plain, pre
    plain, pre = asyncio.run(both(_rows()))
    assert plain == pre
    # Review Focus 5: two overlapping exports concatenated repeat request ids.
    doubled = _rows() + copy.deepcopy(_rows()[:5])
    plain, pre = asyncio.run(both(doubled))
    assert plain == pre and len(plain[0]) == 17


def test_only_one_proposed_scenario_is_open_at_a_time(monkeypatch):
    opened = {"now": 0, "max": 0}
    real_build, real_close = scenarios_mod.build_scenario, scenarios_mod.close_scenario

    async def build(doc, *, base):
        opened["now"] += 1
        opened["max"] = max(opened["max"], opened["now"])
        return await real_build(doc, base=base)

    async def close(scn):
        opened["now"] -= 1
        await real_close(scn)
    monkeypatch.setattr(scenarios_mod, "build_scenario", build)
    monkeypatch.setattr(scenarios_mod, "close_scenario", close)
    _compare(_rows(), _snap(), _doc(*_TRIAD))
    assert opened == {"now": 0, "max": 2}            # current + one proposed


def test_invalid_scenario_fails_before_any_replay(monkeypatch):           # Review Focus 5
    """The third scenario is invalid: nothing is built, nothing is replayed."""
    async def boom(*_a, **_k):
        raise AssertionError("build_scenario must not be reached")
    monkeypatch.setattr(scenarios_mod, "build_scenario", boom)
    doc = _doc(_TRIAD[0], _TRIAD[1], ("bad", {"workload": {"cached_share": 7}}))
    with pytest.raises(ScenarioError) as exc:
        _compare(_rows(), _snap(), doc)
    assert exc.value.where == "scenarios[2](bad).proposal.workload.cached_share"


def test_scenarios_are_closed_when_a_replay_raises(monkeypatch):
    closed = []
    real_close = scenarios_mod.close_scenario

    async def close(scn):
        closed.append(scn)
        await real_close(scn)

    def boom(*_a, **_k):
        raise RuntimeError("replay failed")
    monkeypatch.setattr(scenarios_mod, "close_scenario", close)
    monkeypatch.setattr(scenarios_mod, "run_rows", boom)
    with pytest.raises(RuntimeError, match="replay failed"):
        _compare(_rows(), _snap(), _doc(*_TRIAD))
    assert len(closed) == 2                          # the one proposed scenario, then current
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_scenarios_compare.py -p no:warnings
```

Expected: collection error: `ImportError: cannot import name 'compare' from 'app.eval.scenarios'`.

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 42e8cca..f2c1867 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -366,18 +366,37 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
                      attempted=(*overflowed, pick.name))
 
 
+def arm_results(records: list[dict], scn: Scenario, *,
+                since: datetime | None = None) -> dict[str, ArmResult]:
+    """One scenario's `ArmResult` for every selectable row, keyed by
+    `request_id` -- the `current` arm, computed once and handed to
+    `run_rows(current_results=...)` when several proposals are replayed
+    against the same snapshot (P3c-2 spec §7). Rows that repeat a
+    `request_id` (two overlapping exports concatenated) are the same
+    recorded request, so they share one entry."""
+    observed = observed_latencies([r for r in records if not _before_since(r, since)])
+    rows, _excluded = select_rows(records, set(scn.apps), since=since)
+    return {row["request_id"]: simulate_row(scn, row, observed) for row in rows}
+
+
 def run_rows(records: list[dict], current: Scenario, proposed: Scenario, *,
-             since: datetime | None = None) -> tuple[list[RowResult], dict[str, int]]:
+             since: datetime | None = None,
+             current_results: dict[str, ArmResult] | None = None
+             ) -> tuple[list[RowResult], dict[str, int]]:
     """Simulate every selectable row under both scenarios. Observed latencies
     come from every record in the window (not only the selected ones) and
     are shared by both arms; only apps present in both scenarios are
     simulated. `since` cuts the export first: a row before it is counted
     `before_since` and feeds nothing, so the result equals a run over an
-    export that never held those rows."""
+    export that never held those rows. `current_results` is an optional
+    precomputed `current` arm (`arm_results(records, current, since=since)`);
+    without it the arm is simulated here, row by row, as before."""
     observed = observed_latencies([r for r in records if not _before_since(r, since)])
     app_ids = set(current.apps) & set(proposed.apps)
     rows, excluded = select_rows(records, app_ids, since=since)
-    results = [RowResult(row, simulate_row(current, row, observed),
+    results = [RowResult(row,
+                         (current_results[row["request_id"]] if current_results is not None
+                          else simulate_row(current, row, observed)),
                          simulate_row(proposed, row, observed))
                for row in rows]
     return results, excluded
````

Patch `app/eval/scenarios.py`:

````diff
diff --git a/app/eval/scenarios.py b/app/eval/scenarios.py
index f593ed0..4e09935 100644
--- a/app/eval/scenarios.py
+++ b/app/eval/scenarios.py
@@ -16,10 +16,14 @@ import hashlib
 import json
 import re
 from dataclasses import dataclass
+from datetime import datetime
 
 from app.config.schema import Settings
 from app.eval.proposal import (SCHEMA as PROPOSAL_SCHEMA, ProposalError, _validate_keys,
-                               _validate_schema, apply_proposal)
+                               _validate_schema, apply_proposal, assumptions_block,
+                               normalized_edits)
+from app.eval.scenario import build_scenario, close_scenario
+from app.eval.simulate import arm_results, run_rows, summarize
 
 SCHEMA = "routeforge.policy-scenarios/v1"
 _SCHEMA_PREFIX = "routeforge.policy-scenarios/"
@@ -145,3 +149,74 @@ def apply_scenarios(snapshot: dict, doc: dict, *, base: Settings) -> list[Applie
                 exc.message) from exc
         applied.append(AppliedScenario(entry["name"], proposal, applied_doc))
     return applied
+
+
+# --- engine (spec §7) ----------------------------------------------------------
+
+# The parts of a P3a `simulation.json` body that are NOT per scenario: the
+# first three belong to the document as a whole, `rows` and `baseline` are
+# the shared `current` arm's and are lifted to the top level, and `edits` /
+# `assumptions` sit beside each scenario's `summary`.
+_LIFTED = ("schema", "simulation_id", "inputs", "rows", "baseline", "edits", "assumptions")
+
+
+class ComparisonInvariantError(RuntimeError):
+    """Two scenarios disagreed about something they share by construction --
+    the selected rows, the excluded counts or the `current` arm's baseline.
+    Never a reportable result: it means a bug, and the CLI exits 2."""
+
+
+@dataclass(frozen=True)
+class Comparison:
+    rows: dict             # {"simulated": N, "excluded": {...}} -- the same for every scenario
+    baseline: dict         # the `current` arm's fidelity block
+    window: dict | None    # {"start", "end"} over the simulated rows' `ts`
+    scenarios: list[dict]  # file order: {"name", "edits", "assumptions", "summary"}
+
+
+async def compare(records: list[dict], snapshot: dict, scenarios_doc: dict, *,
+                  base: Settings, since: datetime | None = None) -> Comparison:
+    """Replay every scenario against ONE shared `current` arm (spec §7).
+
+    Every proposal is validated and applied before anything is replayed
+    (`apply_scenarios`, all-or-nothing). The `current` scenario is built and
+    simulated once; each proposed scenario is then built, replayed and
+    closed in file order, so at most one proposed scenario is open at a
+    time. Nothing is ranked, and nothing is applied to a deployment."""
+    applied = apply_scenarios(snapshot, scenarios_doc, base=base)
+    current = await build_scenario(snapshot, base=base)
+    try:
+        current_results = arm_results(records, current, since=since)
+        shared: tuple[dict, dict, dict | None] | None = None
+        entries: list[dict] = []
+        for item in applied:
+            proposed = await build_scenario(item.doc, base=base)
+            try:
+                # A proposal only edits apps in place (spec §7.4), so every
+                # scenario simulates exactly the snapshot's apps.
+                if set(proposed.apps) != set(current.apps):
+                    raise ComparisonInvariantError(
+                        f"scenario {item.name!r} changed the set of applications")
+                results, excluded = run_rows(records, current, proposed, since=since,
+                                             current_results=current_results)
+            finally:
+                await close_scenario(proposed)
+            sim = summarize(results, excluded, inputs={},
+                            edits=normalized_edits(item.proposal),
+                            assumptions=assumptions_block(item.proposal))
+            head = (sim["rows"], sim["baseline"], sim["inputs"]["window"])
+            if shared is None:
+                shared = head
+            elif head != shared:
+                raise ComparisonInvariantError(
+                    f"scenario {item.name!r} saw different rows or a different baseline")
+            entries.append({
+                "name": item.name,
+                "edits": sim["edits"],
+                "assumptions": sim["assumptions"],
+                "summary": {k: v for k, v in sim.items() if k not in _LIFTED},
+            })
+    finally:
+        await close_scenario(current)
+    rows, baseline, window = shared      # a file always holds at least one scenario
+    return Comparison(rows=rows, baseline=baseline, window=window, scenarios=entries)
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_scenarios_compare.py tests/eval/test_scenarios_load.py tests/eval/test_simulate.py tests/eval/test_shadow.py -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_scenarios_compare.py app/eval/simulate.py app/eval/scenarios.py
git commit -m "$(cat <<'MSG'
feat(eval): scenario comparison engine over one shared current arm (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 9: `comparison.json` and `report.md` (spec §8, S1, S3, S8)

**Files:**
- Modify: `app/eval/simulate.py` (`render_report` split into `_new_rejection_lines`, `_move_lines`, `_excluded_lines`; output byte-identical)
- Modify: `app/eval/scenarios.py` (imports; `COMPARISON_SCHEMA`; new output section at the end)
- Test: `tests/eval/test_scenarios_compare.py` (append)

**Interfaces:**
- Consumes: Task 8's `Comparison`; Task 4's `_assumption_items(block) -> list[str]`, `CACHED_SHARE_NOTE`; Task 6's `REPORT_CAVEAT` and the `overflow_hops` key of `totals`; the existing `_metrics_table(stats) -> list[str]`, `_per_day(window, cost_usd) -> dict | None` (returns `{"cost_usd": {current, proposed, delta}}`, `None` under a one-day window), `_fmt`, `_round6`.
- Produces:
  - `app.eval.simulate._new_rejection_lines(new_rej: dict) -> list[str]`, `_move_lines(moves: list[dict]) -> list[str]`, `_excluded_lines(excluded: dict) -> list[str]` — section bodies without headings.
  - `COMPARISON_SCHEMA = "routeforge.scenario-comparison/v1"`.
  - `comparison_doc(comparison: Comparison, *, inputs: dict) -> dict` — keys `schema`, `comparison_id` (16 hex), `inputs` (the caller's plus `window`), `rows`, `baseline`, `scenarios`, `table`.
  - Each `table` row has exactly: `name`, `cost_per_day_delta_usd`, `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`, `overflow_hops`, `affected_apps`.
  - `render_comparison(doc: dict) -> str` — built from the `comparison.json` document alone.
  - `NO_RANKING_NOTE = "Listed in file order. RouteForge does not rank or recommend scenarios."`, `LIKE_FOR_LIKE_NOTE`.

**Rules.** The table is in file order and has no ranking field. Its cost columns are like-for-like (Deviation 10). `overflow_hops` is the proposed arm's total. The report order is: caveat, title, baseline agreement banner (overall, ⚠ under 90%, per app, the `--since` cut), the one table, `NO_RANKING_NOTE`, `LIKE_FOR_LIKE_NOTE`, an assumptions line per scenario that assumes something, a section per scenario (metrics, moves, new rejections, affected apps), excluded rows. The `render_report` refactor must not change a byte of the P3a report; `tests/eval/test_simulate.py` and `tests/eval/test_policy_simulate_cli.py` pin it.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_scenarios_compare.py`:

````diff
diff --git a/tests/eval/test_scenarios_compare.py b/tests/eval/test_scenarios_compare.py
index 5b8900b..4560d08 100644
--- a/tests/eval/test_scenarios_compare.py
+++ b/tests/eval/test_scenarios_compare.py
@@ -218,3 +218,213 @@ def test_scenarios_are_closed_when_a_replay_raises(monkeypatch):
     with pytest.raises(RuntimeError, match="replay failed"):
         _compare(_rows(), _snap(), _doc(*_TRIAD))
     assert len(closed) == 2                          # the one proposed scenario, then current
+
+
+# --- Task 9: comparison.json and report.md (spec §8) ---------------------------
+
+import json  # noqa: E402
+
+from app.eval.scenarios import (COMPARISON_SCHEMA, LIKE_FOR_LIKE_NOTE,  # noqa: E402
+                                NO_RANKING_NOTE, comparison_doc, render_comparison)
+from app.eval.simulate import REPORT_CAVEAT  # noqa: E402
+
+_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "scenarios_sha256": "c",
+           "title": "Healthcare on-prem options", "routing_policy_version": "v",
+           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z",
+           "serving_policy": {}, "since": None}
+
+_TABLE_KEYS = {"name", "cost_per_day_delta_usd", "cost_delta_usd", "route_changed",
+               "newly_rejected", "newly_served", "latency_p50_delta_ms",
+               "latency_p95_delta_ms", "quality_mean_delta", "onprem_share_current",
+               "onprem_share_proposed", "overflow_hops", "affected_apps"}
+
+
+def _document(records, snap, doc, inputs=None, **kw):
+    return comparison_doc(_compare(records, snap, doc, **kw), inputs=inputs or _INPUTS)
+
+
+def test_s1_empty_scenario_is_a_noop():                                    # S1
+    doc = _document(_rows(), _snap(), _doc(("empty", {}), ("also-empty", {"title": "x"})))
+    assert len(doc["table"]) == 2
+    for row in doc["table"]:
+        assert row["cost_per_day_delta_usd"] == 0 and row["cost_delta_usd"] == 0
+        assert (row["route_changed"], row["newly_rejected"], row["newly_served"]) == (0, 0, 0)
+        assert row["latency_p50_delta_ms"] == 0 and row["latency_p95_delta_ms"] == 0
+        assert row["quality_mean_delta"] == 0
+        assert row["onprem_share_current"] == row["onprem_share_proposed"]
+        assert row["overflow_hops"] == 0 and row["affected_apps"] == []
+    for entry in doc["scenarios"]:
+        assert entry["edits"] == {} and entry["summary"]["moves"] == []
+        assert entry["summary"]["totals"]["cost_usd"]["delta"] == 0
+
+
+def test_s3_order_independence():                                          # S3
+    """Permuting the scenarios permutes the table rows and changes nothing
+    else: each scenario's block, the rows and the baseline are identical."""
+    forward = _document(_rows(), _snap(), _doc(*_TRIAD))
+    backward = _document(_rows(), _snap(), _doc(*reversed(_TRIAD)))
+    assert [e["name"] for e in forward["scenarios"]] == [n for n, _ in _TRIAD]
+    assert [e["name"] for e in backward["scenarios"]] == [n for n, _ in reversed(_TRIAD)]
+    assert {e["name"]: e for e in forward["scenarios"]} == \
+           {e["name"]: e for e in backward["scenarios"]}
+    assert forward["table"] == list(reversed(backward["table"]))
+    for key in ("rows", "baseline", "inputs", "comparison_id"):
+        assert forward[key] == backward[key], key
+
+
+def _keys(value):
+    if isinstance(value, dict):
+        for key, inner in value.items():
+            yield key
+            yield from _keys(inner)
+    elif isinstance(value, list):
+        for inner in value:
+            yield from _keys(inner)
+
+
+def test_s8_no_ranking_and_file_order():                                   # S8
+    """The file is not in cost order, and the output must not put it in one.
+    No field anywhere ranks, scores or recommends."""
+    doc = _document(_rows(), _snap(), _doc(*_TRIAD))
+    assert [row["name"] for row in doc["table"]] == [n for n, _ in _TRIAD]
+    assert [e["name"] for e in doc["scenarios"]] == [n for n, _ in _TRIAD]
+    costs = [row["cost_delta_usd"] for row in doc["table"]]
+    assert costs != sorted(costs) and costs != sorted(costs, reverse=True)   # not sorted by cost
+    for row in doc["table"]:
+        assert set(row) == _TABLE_KEYS
+    banned = ("rank", "recommend", "best", "winner", "prefer", "score")
+    assert not [k for k in _keys(doc) if any(word in k.lower() for word in banned)]
+    report = render_comparison(doc)
+    assert NO_RANKING_NOTE in report
+    positions = [report.index(f"| {n} |") for n, _ in _TRIAD]
+    assert positions == sorted(positions)
+    sections = [report.index(f"## Scenario: {n}") for n, _ in _TRIAD]
+    assert sections == sorted(sections)
+
+
+def test_comparison_doc_shape():
+    doc = _document(_rows(), _snap(), _doc(*_TRIAD))
+    assert set(doc) == {"schema", "comparison_id", "inputs", "rows", "baseline",
+                        "scenarios", "table"}
+    assert doc["schema"] == COMPARISON_SCHEMA and len(doc["comparison_id"]) == 16
+    assert set(doc["inputs"]) == set(_INPUTS) | {"window"}
+    assert doc["inputs"]["window"] == {"start": "2026-09-20T12:00:00+00:00",
+                                       "end": "2026-09-22T12:00:11+00:00"}
+    assert doc["rows"]["simulated"] == 12
+    assert "agreement" in doc["baseline"]                      # lifted out of every scenario
+    assert all("baseline" not in e["summary"] for e in doc["scenarios"])
+
+
+def test_table_values_and_like_for_like_cost():
+    """The table reads the scenario's own totals. Its cost columns are the
+    both-routed (like-for-like) delta, so a scenario that newly rejects rows
+    does not look cheaper for it."""
+    snap = make_snapshot(apps=[app_entry("claims", allowed_models=["local-mid"]),
+                               app_entry("chat")])
+    doc = _document(_rows(), snap, _doc(("drop-mid", {"endpoints": {"remove": ["local-mid"]}})))
+    (row,), (entry,) = doc["table"], doc["scenarios"]
+    totals = entry["summary"]["totals"]
+    assert row["newly_rejected"] == totals["newly_rejected"] == 6
+    assert totals["cost_usd"]["delta"] < 0                     # the misleading "saving"
+    assert row["cost_delta_usd"] == totals["cost_usd_both_routed"]["delta"]
+    assert row["cost_delta_usd"] != totals["cost_usd"]["delta"]    # not the all-routed delta
+    assert row["affected_apps"] == entry["summary"]["affected_apps"]
+    assert row["overflow_hops"] == totals["overflow_hops"]["proposed"]
+    assert row["onprem_share_proposed"] == totals["onprem_share"]["proposed"]
+    assert row["latency_p95_delta_ms"] == totals["latency_ms"]["p95"]["delta"]
+
+
+def test_cost_per_day_divides_the_like_for_like_delta_by_the_window():
+    """The window is 2 days and 11 seconds: 2.000127 days."""
+    doc = _document(_rows(), _snap(), _doc(*_TRIAD))
+    for row in doc["table"]:
+        assert row["cost_delta_usd"] != 0
+        assert row["cost_per_day_delta_usd"] == pytest.approx(
+            row["cost_delta_usd"] / (2 + 11 / 86400), abs=1e-6)
+
+
+def test_cost_per_day_is_null_under_a_one_day_window():
+    doc = _document(_rows(days=1), _snap(), _doc(*_TRIAD))
+    assert all(row["cost_per_day_delta_usd"] is None for row in doc["table"])
+    assert "| all-onprem | n/a |" in render_comparison(doc)
+
+
+def test_comparison_is_deterministic_and_id_follows_inputs():
+    one = _document(_rows(), _snap(), _doc(*_TRIAD))
+    two = _document(_rows(), _snap(), _doc(*_TRIAD))
+    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
+    assert render_comparison(one) == render_comparison(two)
+    other = _document(_rows(), _snap(), _doc(*_TRIAD),
+                      inputs={**_INPUTS, "since": "2026-09-01T00:00:00+00:00"})
+    assert other["comparison_id"] != one["comparison_id"]
+
+
+# --- report.md -------------------------------------------------------------------
+
+def test_report_structure():
+    report = render_comparison(_document(_rows(), _snap(), _doc(*_TRIAD)))
+    assert report.startswith("> " + REPORT_CAVEAT.split("\n")[0])
+    assert "plus deterministic context-window overflow" in report
+    order = ["# Scenario comparison: Healthcare on-prem options", "## Baseline agreement",
+             "## Comparison", NO_RANKING_NOTE, LIKE_FOR_LIKE_NOTE, "## Assumptions",
+             "## Scenario: all-onprem", "## Scenario: smaller-prompts",
+             "## Scenario: onprem+2nd-gpu", "## Excluded rows"]
+    positions = [report.index(marker) for marker in order]
+    assert positions == sorted(positions)
+    assert report.count("## Comparison") == 1                  # ONE table
+    assert "- **smaller-prompts** -- token_scale: claims=0.4; rows whose saturated" in report
+    assert "- **onprem+2nd-gpu** -- latency_ms: local-mid-2=90" in report
+    assert "- **all-onprem**" not in report                    # it assumes nothing
+    assert "### Moves" in report and "### New rejections" in report
+    assert "Affected apps: claims" in report
+
+
+def test_report_names_the_since_cut():
+    since = parse_since("2026-09-21T00:00:00Z")
+    doc = _document(_rows(), _snap(), _doc(*_TRIAD), since=since,
+                    inputs={**_INPUTS, "since": since.isoformat()})
+    report = render_comparison(doc)
+    assert "Rows before `--since 2026-09-21T00:00:00+00:00` excluded: 4." in report
+    assert "| before_since | 4 |" in report
+
+
+def test_report_agreement_banner_warns_and_marks_apps():
+    """`chat` rows record the model replay picks; `claims` rows record one it
+    never would. Overall 50%: a warning, and `claims` is marked."""
+    rows = []
+    for row in _rows():
+        if row["application_id"] == "claims":
+            row.update(attempts=["local-big"], chosen="local-big", complexity=0.05)
+        else:
+            row.update(complexity=0.05, attempts=["local-cheap"], chosen="local-cheap")
+        rows.append(row)
+    doc = _document(rows, _snap(), _doc(("empty", {})))
+    assert doc["baseline"]["by_app"] == {"chat": 1.0, "claims": 0.0}
+    assert doc["baseline"]["warning"] is True
+    report = render_comparison(doc)
+    assert "**Baseline agreement: 50.0%** over 12 compared rows." in report
+    assert "⚠ **Warning:** below the 90% threshold" in report
+    assert "Per app: chat 100.0% · claims 0.0% ⚠" in report
+    assert "Rows before `--since" not in report
+
+
+def test_report_agreement_banner_without_comparable_rows():
+    rows = [make_row("r", "chat", status="backend_failed")]
+    doc = _document(rows, _snap(), _doc(("empty", {})))
+    assert doc["baseline"]["agreement"] is None
+    assert "**Baseline agreement: n/a**" in render_comparison(doc)
+
+
+def test_report_cached_share_note_and_no_title():
+    doc = _document(_rows(), _snap(),
+                    _doc(("cached", {"workload": {"cached_share": 0.5}}), title=None),
+                    inputs={**_INPUTS, "title": None})
+    report = render_comparison(doc)
+    assert "# Scenario comparison\n" in report
+    assert "- **cached** -- cached_share: *=0.5" in report
+    assert "`cached_share` is an assumption" in report
+
+
+def test_report_has_no_assumptions_section_when_nothing_is_assumed():
+    report = render_comparison(_document(_rows(), _snap(), _doc(("empty", {}))))
+    assert "## Assumptions" not in report
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_scenarios_compare.py -p no:warnings
```

Expected: collection error: `ImportError: cannot import name 'COMPARISON_SCHEMA' from 'app.eval.scenarios'`.

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index f2c1867..823ed1b 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -766,6 +766,47 @@ def _assumption_lines(block: dict) -> list[str]:
     return lines
 
 
+def _new_rejection_lines(new_rej: dict) -> list[str]:
+    """The body of a "New rejections" section: by app, by code, then by
+    conflicting core -- or "None."."""
+    if not new_rej["by_app"] and not new_rej["by_code"]:
+        return ["None."]
+    lines = ["| app | count |", "|---|---|"]
+    for app_id in sorted(new_rej["by_app"]):
+        lines.append(f"| {app_id} | {new_rej['by_app'][app_id]} |")
+    lines.append("")
+    lines.append("| code | count |")
+    lines.append("|---|---|")
+    for code in sorted(new_rej["by_code"]):
+        lines.append(f"| {code} | {new_rej['by_code'][code]} |")
+    lines.append("")
+    lines.append("**By conflicting core**")
+    lines.append("")
+    lines.append("| core | count |")
+    lines.append("|---|---|")
+    for key, count in sorted(new_rej["by_core"].items(), key=lambda kv: (-kv[1], kv[0])):
+        lines.append(f"| {key} | {count} |")
+    return lines
+
+
+def _move_lines(moves: list[dict]) -> list[str]:
+    """The body of a "Moves" section: from -> to -> count, or "None."."""
+    if not moves:
+        return ["None."]
+    lines = ["| from | to | count |", "|---|---|---|"]
+    for move in moves:
+        lines.append(f"| {move['from']} | {move['to']} | {move['count']} |")
+    return lines
+
+
+def _excluded_lines(excluded: dict) -> list[str]:
+    """The body of an "Excluded rows" section: reason -> count."""
+    lines = ["| reason | count |", "|---|---|"]
+    for reason in sorted(excluded):
+        lines.append(f"| {reason} | {excluded[reason]} |")
+    return lines
+
+
 def render_report(sim: dict) -> str:
     """The `report.md` document (spec §8), in the order: caveat, baseline
     warning, headline, new rejections, moves, by-app, by-workload,
@@ -790,37 +831,12 @@ def render_report(sim: dict) -> str:
 
     lines.append("## New rejections")
     lines.append("")
-    new_rej = sim["new_rejections"]
-    if not new_rej["by_app"] and not new_rej["by_code"]:
-        lines.append("None.")
-    else:
-        lines.append("| app | count |")
-        lines.append("|---|---|")
-        for app_id in sorted(new_rej["by_app"]):
-            lines.append(f"| {app_id} | {new_rej['by_app'][app_id]} |")
-        lines.append("")
-        lines.append("| code | count |")
-        lines.append("|---|---|")
-        for code in sorted(new_rej["by_code"]):
-            lines.append(f"| {code} | {new_rej['by_code'][code]} |")
-        lines.append("")
-        lines.append("**By conflicting core**")
-        lines.append("")
-        lines.append("| core | count |")
-        lines.append("|---|---|")
-        for key, count in sorted(new_rej["by_core"].items(), key=lambda kv: (-kv[1], kv[0])):
-            lines.append(f"| {key} | {count} |")
+    lines += _new_rejection_lines(sim["new_rejections"])
     lines.append("")
 
     lines.append("## Moves")
     lines.append("")
-    if not sim["moves"]:
-        lines.append("None.")
-    else:
-        lines.append("| from | to | count |")
-        lines.append("|---|---|---|")
-        for move in sim["moves"]:
-            lines.append(f"| {move['from']} | {move['to']} | {move['count']} |")
+    lines += _move_lines(sim["moves"])
     lines.append("")
 
     lines.append("## By app")
@@ -850,10 +866,6 @@ def render_report(sim: dict) -> str:
 
     lines.append("## Excluded rows")
     lines.append("")
-    excluded = sim["rows"]["excluded"]
-    lines.append("| reason | count |")
-    lines.append("|---|---|")
-    for reason in sorted(excluded):
-        lines.append(f"| {reason} | {excluded[reason]} |")
+    lines += _excluded_lines(sim["rows"]["excluded"])
 
     return "\n".join(lines) + "\n"
````

Patch `app/eval/scenarios.py`:

````diff
diff --git a/app/eval/scenarios.py b/app/eval/scenarios.py
index 4e09935..3272745 100644
--- a/app/eval/scenarios.py
+++ b/app/eval/scenarios.py
@@ -19,13 +19,18 @@ from dataclasses import dataclass
 from datetime import datetime
 
 from app.config.schema import Settings
+from app.eval.outcomes import _fmt, _round6
 from app.eval.proposal import (SCHEMA as PROPOSAL_SCHEMA, ProposalError, _validate_keys,
                                _validate_schema, apply_proposal, assumptions_block,
                                normalized_edits)
 from app.eval.scenario import build_scenario, close_scenario
-from app.eval.simulate import arm_results, run_rows, summarize
+from app.eval.simulate import (CACHED_SHARE_NOTE, REPORT_CAVEAT, _assumption_items,
+                               _excluded_lines, _metrics_table, _move_lines,
+                               _new_rejection_lines, _per_day, arm_results, run_rows,
+                               summarize)
 
 SCHEMA = "routeforge.policy-scenarios/v1"
+COMPARISON_SCHEMA = "routeforge.scenario-comparison/v1"
 _SCHEMA_PREFIX = "routeforge.policy-scenarios/"
 _SCHEMA_MAJOR = "v1"
 
@@ -220,3 +225,178 @@ async def compare(records: list[dict], snapshot: dict, scenarios_doc: dict, *,
         await close_scenario(current)
     rows, baseline, window = shared      # a file always holds at least one scenario
     return Comparison(rows=rows, baseline=baseline, window=window, scenarios=entries)
+
+
+# --- output (spec §8) ----------------------------------------------------------
+
+#: Printed under the comparison table, verbatim (spec §8, invariant S8).
+NO_RANKING_NOTE = "Listed in file order. RouteForge does not rank or recommend scenarios."
+
+#: Why the table's two cost columns can be read across scenarios.
+LIKE_FOR_LIKE_NOTE = (
+    "Cost columns compare only the rows routed in both arms. A row a scenario newly rejects "
+    "is counted under \"newly rejected\", never as a saving."
+)
+
+
+def _table_row(entry: dict, window: dict | None) -> dict:
+    """One comparison-table row. Exactly these keys -- there is no rank,
+    score or recommendation field, by design (S8)."""
+    summary = entry["summary"]
+    totals = summary["totals"]
+    like_for_like = totals["cost_usd_both_routed"]
+    per_day = _per_day(window, like_for_like)
+    return {
+        "name": entry["name"],
+        "cost_per_day_delta_usd": per_day["cost_usd"]["delta"] if per_day else None,
+        "cost_delta_usd": like_for_like["delta"],
+        "route_changed": totals["route_changed"],
+        "newly_rejected": totals["newly_rejected"],
+        "newly_served": totals["newly_served"],
+        "latency_p50_delta_ms": totals["latency_ms"]["p50"]["delta"],
+        "latency_p95_delta_ms": totals["latency_ms"]["p95"]["delta"],
+        "quality_mean_delta": totals["quality_mean"]["delta"],
+        "onprem_share_current": totals["onprem_share"]["current"],
+        "onprem_share_proposed": totals["onprem_share"]["proposed"],
+        "overflow_hops": totals["overflow_hops"]["proposed"],
+        "affected_apps": summary["affected_apps"],
+    }
+
+
+def comparison_doc(comparison: Comparison, *, inputs: dict) -> dict:
+    """The deterministic `comparison.json` document (spec §8). `inputs` is
+    the caller's partial input record; `window` is added here, before
+    hashing, exactly as `simulate.summarize` does for `simulation_id`."""
+    full_inputs = dict(inputs)
+    full_inputs["window"] = comparison.window
+    comparison_id = hashlib.sha256(
+        json.dumps(full_inputs, sort_keys=True).encode("utf-8")).hexdigest()[:16]
+    return _round6({
+        "schema": COMPARISON_SCHEMA,
+        "comparison_id": comparison_id,
+        "inputs": full_inputs,
+        "rows": comparison.rows,
+        "baseline": comparison.baseline,
+        "scenarios": comparison.scenarios,
+        "table": [_table_row(entry, comparison.window) for entry in comparison.scenarios],
+    })
+
+
+_TABLE_COLUMNS = ("scenario", "Δcost/day USD", "Δcost USD", "route changed", "newly rejected",
+                  "newly served", "Δp50 ms", "Δp95 ms", "Δquality",
+                  "on-prem share (current→proposed)", "overflow hops", "affected apps")
+
+
+def _pct(value: float) -> str:
+    return f"{value * 100:.1f}%"
+
+
+def _agreement_lines(doc: dict) -> list[str]:
+    """The agreement banner: overall, per app, a warning under 90%, and the
+    `--since` cut when one was applied."""
+    baseline = doc["baseline"]
+    lines = ["## Baseline agreement", ""]
+    if baseline["agreement"] is None:
+        lines.append("**Baseline agreement: n/a** -- no served row with a recorded first "
+                     "attempt to compare. Nothing vouches for this replay.")
+    else:
+        lines.append(f"**Baseline agreement: {_pct(baseline['agreement'])}** over "
+                     f"{baseline['rows_compared']} compared rows.")
+        if baseline["warning"]:
+            lines.append("")
+            lines.append("⚠ **Warning:** below the 90% threshold. Every scenario row below "
+                         "rests on a replay that disagrees with the record this often.")
+        if baseline["by_app"]:
+            lines.append("")
+            lines.append("Per app: " + " · ".join(
+                f"{app_id} {_pct(rate)}" + (" ⚠" if rate < 0.90 else "")
+                for app_id, rate in sorted(baseline["by_app"].items())))
+    since = doc["inputs"].get("since")
+    if since is not None:
+        lines.append("")
+        lines.append(f"Rows before `--since {since}` excluded: "
+                     f"{doc['rows']['excluded'].get('before_since', 0)}.")
+    lines.append("")
+    return lines
+
+
+def _comparison_row(row: dict) -> str:
+    cells = [
+        row["name"],
+        _fmt(row["cost_per_day_delta_usd"]),
+        _fmt(row["cost_delta_usd"]),
+        str(row["route_changed"]),
+        str(row["newly_rejected"]),
+        str(row["newly_served"]),
+        _fmt(row["latency_p50_delta_ms"]),
+        _fmt(row["latency_p95_delta_ms"]),
+        _fmt(row["quality_mean_delta"]),
+        f"{_fmt(row['onprem_share_current'])}→{_fmt(row['onprem_share_proposed'])}",
+        str(row["overflow_hops"]),
+        ", ".join(row["affected_apps"]) or "none",
+    ]
+    return "| " + " | ".join(cells) + " |"
+
+
+def render_comparison(doc: dict) -> str:
+    """`report.md` for a comparison (spec §8): the caveat, the agreement
+    banner, ONE table in file order, the assumptions each scenario makes,
+    then a short section per scenario, then the excluded rows. Built from
+    the `comparison.json` document alone."""
+    lines: list[str] = ["> " + line for line in REPORT_CAVEAT.split("\n")]
+    lines.append("")
+    title = doc["inputs"].get("title")
+    lines.append(f"# Scenario comparison: {title}" if title else "# Scenario comparison")
+    lines.append("")
+    lines.append(f"{doc['rows']['simulated']} rows simulated, "
+                 f"{len(doc['scenarios'])} scenario(s), one shared current arm.")
+    lines.append("")
+
+    lines += _agreement_lines(doc)
+
+    lines.append("## Comparison")
+    lines.append("")
+    lines.append("| " + " | ".join(_TABLE_COLUMNS) + " |")
+    lines.append("|" + "---|" * len(_TABLE_COLUMNS))
+    lines += [_comparison_row(row) for row in doc["table"]]
+    lines.append("")
+    lines.append(NO_RANKING_NOTE)
+    lines.append("")
+    lines.append(LIKE_FOR_LIKE_NOTE)
+    lines.append("")
+
+    assuming = [(entry["name"], _assumption_items(entry["assumptions"]))
+                for entry in doc["scenarios"]]
+    assuming = [(name, items) for name, items in assuming if items]
+    if assuming:
+        lines.append("## Assumptions")
+        lines.append("")
+        for name, items in assuming:
+            lines.append(f"- **{name}** -- " + "; ".join(items))
+        if any(entry["assumptions"]["cached_share"] for entry in doc["scenarios"]):
+            lines.append("")
+            lines.append(CACHED_SHARE_NOTE)
+        lines.append("")
+
+    for entry in doc["scenarios"]:
+        summary = entry["summary"]
+        lines.append(f"## Scenario: {entry['name']}")
+        lines.append("")
+        lines += _metrics_table(summary["totals"])
+        lines.append("")
+        lines.append("### Moves")
+        lines.append("")
+        lines += _move_lines(summary["moves"])
+        lines.append("")
+        lines.append("### New rejections")
+        lines.append("")
+        lines += _new_rejection_lines(summary["new_rejections"])
+        lines.append("")
+        lines.append("Affected apps: " + (", ".join(summary["affected_apps"]) or "none"))
+        lines.append("")
+
+    lines.append("## Excluded rows")
+    lines.append("")
+    lines += _excluded_lines(doc["rows"]["excluded"])
+
+    return "\n".join(lines) + "\n"
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_scenarios_compare.py app/eval/simulate.py app/eval/scenarios.py
git commit -m "$(cat <<'MSG'
feat(eval): comparison.json and the one-table scenario report (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 10: CLI `--scenarios` mode (spec §6)

**Files:**
- Modify: `scripts/policy_simulate.py` (module docstring, imports, `_parser`, `main`; new `_compare_scenarios`)
- Test: `tests/eval/test_policy_simulate_cli.py` (append)

**Interfaces:**
- Consumes: Task 7's `load_scenarios(path) -> (doc, sha256)`, `ScenarioError`; Task 8's `compare(...)`, `ComparisonInvariantError`, `Comparison.baseline`; Task 9's `comparison_doc(comparison, *, inputs)`, `render_comparison(doc)`; Task 2's `agreement_ok`, `_below_min_agreement`; Task 1's `since`.
- Produces:
  - CLI flag `--scenarios FILE`. Combining it with `--proposal` or `--shadow-prices` is an argparse usage error (exit 2).
  - Output files `comparison.json` and `report.md` in `--out` (no `simulation.json`).
  - `comparison.json` `inputs`: `export_sha256`, `snapshot_sha256`, `scenarios_sha256`, `title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy`, `since`, `window`.
  - Exit 3 when `--fail-on-new-rejections` and any table row has `newly_rejected > 0` (files are written first, as for a single proposal). Exit 4 from `--min-agreement`, nothing written.

**Determinism** is pinned by `test_cli_scenarios_deterministic` (two runs, byte-identical `comparison.json` and `report.md`).

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_policy_simulate_cli.py`:

````diff
diff --git a/tests/eval/test_policy_simulate_cli.py b/tests/eval/test_policy_simulate_cli.py
index db938d5..a1046c1 100644
--- a/tests/eval/test_policy_simulate_cli.py
+++ b/tests/eval/test_policy_simulate_cli.py
@@ -455,3 +455,165 @@ def test_min_agreement_out_of_range_exits_2(tmp_path, bad):
                          f"--min-agreement={bad}")
     assert proc.returncode == 2, proc.stderr
     assert "--min-agreement" in proc.stderr and not out.exists()
+
+
+# --- P3c-2 Task 10: --scenarios (spec §6, §8) ---------------------------------
+
+S = "routeforge.policy-scenarios/v1"
+
+
+def _scenarios(*pairs, title="Options"):
+    return {"schema": S, "title": title,
+            "scenarios": [{"name": n, "proposal": p} for n, p in pairs]}
+
+
+def _run_cli_scenarios(tmp_path, export_lines, snapshot, scenarios_doc, *extra):
+    exp, snap, scn = tmp_path / "e.ndjson", tmp_path / "s.json", tmp_path / "c.json"
+    exp.write_text("".join(json.dumps(line) + "\n" for line in export_lines))
+    snap.write_text(json.dumps(snapshot))
+    scn.write_text(json.dumps(scenarios_doc))
+    out = tmp_path / "out"
+    proc = subprocess.run([sys.executable, str(SCRIPT), "--export", str(exp),
+                           "--snapshot", str(snap), "--scenarios", str(scn),
+                           "--out", str(out), *extra],
+                          capture_output=True, text=True, cwd=ROOT,
+                          env={**__import__("os").environ,
+                               "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
+    return proc, out
+
+
+def _mixed_lines(n=6):
+    return [_line(make_row(f"r{i}", "app-a", complexity=[0.05, 0.6, 1.0][i % 3],
+                           ts=f"2026-09-2{i % 3}T12:00:0{i}+00:00")) for i in range(n)]
+
+
+_PAIR = (("drop-mid", {"endpoints": {"remove": ["local-mid"]}}),
+         ("half-tokens", {"workload": {"token_scale": 0.5}}))
+
+
+def test_cli_scenarios_writes_comparison_and_report(tmp_path):
+    proc, out = _run_cli_scenarios(tmp_path, _mixed_lines(), make_snapshot(),
+                                   _scenarios(*_PAIR))
+    assert proc.returncode == 0, proc.stderr
+    assert sorted(p.name for p in out.iterdir()) == ["comparison.json", "report.md"]
+    doc = json.loads((out / "comparison.json").read_text())
+    assert doc["schema"] == "routeforge.scenario-comparison/v1"
+    assert doc["inputs"]["title"] == "Options" and len(doc["inputs"]["scenarios_sha256"]) == 64
+    assert doc["inputs"]["since"] is None
+    assert [row["name"] for row in doc["table"]] == ["drop-mid", "half-tokens"]   # file order
+    assert doc["rows"]["simulated"] == 6
+    report = (out / "report.md").read_text()
+    assert report.startswith("> **Simulation only.**")
+    assert "RouteForge does not rank or recommend scenarios." in report
+    assert "6 rows simulated under 2 scenario(s)" in proc.stdout
+
+
+def test_cli_scenarios_deterministic(tmp_path):
+    args = (tmp_path, _mixed_lines(), make_snapshot(), _scenarios(*_PAIR))
+    proc, out = _run_cli_scenarios(*args)
+    assert proc.returncode == 0, proc.stderr
+    first = ((out / "comparison.json").read_bytes(), (out / "report.md").read_bytes())
+    proc2, _ = _run_cli_scenarios(*args)
+    assert proc2.returncode == 0
+    assert ((out / "comparison.json").read_bytes(), (out / "report.md").read_bytes()) == first
+
+
+def test_cli_single_scenario_matches_the_proposal_run(tmp_path):          # S2, end to end
+    body = {"endpoints": {"remove": ["local-mid"]}, "workload": {"cached_share": 0.5}}
+    proc, out = _run_cli(tmp_path, _mixed_lines(), make_snapshot(), {"schema": P, **body})
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    proc2, out2 = _run_cli_scenarios(tmp_path, _mixed_lines(), make_snapshot(),
+                                     _scenarios(("only", body)))
+    assert proc2.returncode == 0, proc2.stderr
+    doc = json.loads((out2 / "comparison.json").read_text())
+    (entry,) = doc["scenarios"]
+    assert (doc["rows"], doc["baseline"]) == (sim["rows"], sim["baseline"])
+    assert (entry["edits"], entry["assumptions"]) == (sim["edits"], sim["assumptions"])
+    for key, value in entry["summary"].items():
+        assert value == sim[key], key
+
+
+@pytest.mark.parametrize("extra", [["--shadow-prices"], ["--proposal", "PROPOSAL"]])
+def test_cli_scenarios_excludes_proposal_and_shadow_prices(tmp_path, extra):
+    prop = tmp_path / "p.json"
+    prop.write_text(json.dumps({"schema": P}))
+    extra = [str(prop) if x == "PROPOSAL" else x for x in extra]
+    proc, out = _run_cli_scenarios(tmp_path, _mixed_lines(), make_snapshot(),
+                                   _scenarios(*_PAIR), *extra)
+    assert proc.returncode == 2, proc.stderr
+    assert "--scenarios cannot be combined" in proc.stderr
+    assert not out.exists()
+
+
+def test_cli_scenarios_invalid_scenario_exits_2_naming_it(tmp_path):
+    bad = _scenarios(_PAIR[0], ("bad-one", {"workload": {"token_scale": 0}}))
+    proc, out = _run_cli_scenarios(tmp_path, _mixed_lines(), make_snapshot(), bad)
+    assert proc.returncode == 2, proc.stderr
+    assert "scenarios[1](bad-one).proposal.workload.token_scale" in proc.stderr
+    assert "Traceback" not in proc.stderr and not out.exists()
+
+
+def test_cli_scenarios_unknown_key_exits_2(tmp_path):
+    bad = _scenarios(*_PAIR)
+    bad["scenarios"][0]["recommended"] = True
+    proc, out = _run_cli_scenarios(tmp_path, _mixed_lines(), make_snapshot(), bad)
+    assert proc.returncode == 2 and "scenarios[0].recommended" in proc.stderr
+    assert not out.exists()
+
+
+def test_cli_scenarios_exit_3_when_any_scenario_newly_rejects(tmp_path):
+    from tests.eval.sim_fixtures import app_entry
+    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
+    doc = _scenarios(("harmless", {}), ("drop-mid", {"endpoints": {"remove": ["local-mid"]}}))
+    proc, out = _run_cli_scenarios(tmp_path, _mixed_lines(), snap, doc,
+                                   "--fail-on-new-rejections")
+    assert proc.returncode == 3, proc.stderr
+    assert (out / "comparison.json").exists()             # written, then gated
+    proc2, _ = _run_cli_scenarios(tmp_path, _mixed_lines(), snap, doc)
+    assert proc2.returncode == 0
+    proc3, _ = _run_cli_scenarios(tmp_path, _mixed_lines(), snap,
+                                  _scenarios(("harmless", {})), "--fail-on-new-rejections")
+    assert proc3.returncode == 0
+
+
+def test_cli_scenarios_min_agreement_exits_4_and_writes_nothing(tmp_path):
+    proc, out = _run_cli_scenarios(tmp_path, _disagreeing_lines(), make_snapshot(),
+                                   _scenarios(*_PAIR), "--min-agreement", "0.9")
+    assert proc.returncode == 4, proc.stderr
+    assert "baseline agreement 0 over 4 compared rows" in proc.stderr
+    assert not out.exists()
+
+
+def test_cli_scenarios_since(tmp_path):
+    proc, out = _run_cli_scenarios(tmp_path, _two_eras(), make_snapshot(), _scenarios(*_PAIR),
+                                   "--since", "2026-09-10T00:00:00Z")
+    assert proc.returncode == 0, proc.stderr
+    doc = json.loads((out / "comparison.json").read_text())
+    assert doc["rows"]["simulated"] == 1 and doc["rows"]["excluded"]["before_since"] == 1
+    assert doc["inputs"]["since"] == "2026-09-10T00:00:00+00:00"
+    assert "Rows before `--since 2026-09-10T00:00:00+00:00` excluded: 1." in \
+        (out / "report.md").read_text()
+
+
+def test_live_round_trip_scenarios_empty_scenario_is_a_perfect_noop(tmp_path):
+    """Live export + live snapshot, one empty scenario: the shared baseline
+    still agrees 100% with what was served (the overflow re-selection and
+    the first-decision baseline change nothing on real rows), and every
+    delta is zero."""
+    with TestClient(create_app()) as c:
+        for _ in range(4):
+            r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
+                       json={"messages": [{"role": "user", "content": "hello"}]})
+            assert r.status_code == 200, r.text
+        _login(c)
+        snapshot = c.get("/admin/config-snapshot").json()
+        export = [json.loads(x) for x in
+                  c.get("/admin/outcomes/export").text.strip().splitlines()]
+    proc, out = _run_cli_scenarios(tmp_path, export, snapshot, _scenarios(("noop", {})),
+                                   "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    doc = json.loads((out / "comparison.json").read_text())
+    assert doc["rows"]["simulated"] == 4 and doc["baseline"]["agreement"] == 1.0
+    (row,) = doc["table"]
+    assert (row["route_changed"], row["newly_rejected"], row["cost_delta_usd"]) == (0, 0, 0)
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_policy_simulate_cli.py -p no:warnings
```

Expected: the new tests fail with exit 2: `error: unrecognized arguments: --scenarios`.

- [ ] **Step 3: Implement**

Patch `scripts/policy_simulate.py`:

````diff
diff --git a/scripts/policy_simulate.py b/scripts/policy_simulate.py
index aa34c34..e33ed1f 100644
--- a/scripts/policy_simulate.py
+++ b/scripts/policy_simulate.py
@@ -3,6 +3,9 @@
     uv run python scripts/policy_simulate.py --export outcomes.ndjson \
         --snapshot snapshot.json --proposal proposal.json --out results/sim
 
+    uv run python scripts/policy_simulate.py --export outcomes.ndjson \
+        --snapshot snapshot.json --scenarios scenarios.json --out results/cmp
+
 Reads a `routing_outcomes` NDJSON export (`GET /admin/outcomes/export`), a
 config snapshot (`GET /admin/config-snapshot`) and an operator-written policy
 proposal (`routeforge.policy-proposal/v1`); replays every simulated row's
@@ -10,6 +13,11 @@ first routing decision under the snapshot as exported (`current`) and the
 snapshot with the proposal applied (`proposed`); and writes a deterministic
 `simulation.json` plus a human-readable `report.md` into `--out`.
 
+With `--scenarios` (P3c-2) it instead replays every scenario of a
+`routeforge.policy-scenarios/v1` file against one shared `current` arm and
+writes `comparison.json` plus `report.md`. Scenarios are reported in file
+order; nothing ranks or recommends one.
+
 **Simulation only.** It never applies the proposal to a live deployment --
 see `app/eval/simulate.py::REPORT_CAVEAT` and spec §2/§7 for what this does
 and does not model.
@@ -17,10 +25,11 @@ and does not model.
 File-based only: no DB, no `app.api`, no serving import
 (`tests/eval/test_isolation.py`).
 
-Exit codes: 0 simulation written; 2 an input file failed to load, validate
-or apply; 3 the proposal introduces new rejections and
-`--fail-on-new-rejections` is set (for CI/GitOps gating); 4 the baseline's
-agreement with the record is below `--min-agreement` (nothing is written).
+Exit codes: 0 output written; 2 an input file failed to load, validate or
+apply, or the flags do not combine; 3 the proposal (or any scenario)
+introduces new rejections and `--fail-on-new-rejections` is set (for
+CI/GitOps gating); 4 the baseline's agreement with the record is below
+`--min-agreement` (nothing is written).
 """
 from __future__ import annotations
 
@@ -45,6 +54,10 @@ from app.eval.proposal import (  # noqa: E402
     ProposalError, apply_proposal, assumptions_block, load_proposal, normalized_edits,
 )
 from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
+from app.eval.scenarios import (  # noqa: E402
+    ComparisonInvariantError, ScenarioError, compare, comparison_doc, load_scenarios,
+    render_comparison,
+)
 from app.eval.shadow import ShadowInvariantError, render_shadow_section, shadow_prices  # noqa: E402
 from app.eval.simulate import (  # noqa: E402
     REPORT_CAVEAT, agreement_ok, baseline_check, parse_since, render_report, simulate,
@@ -60,12 +73,17 @@ def _parser() -> argparse.ArgumentParser:
     p.add_argument("--snapshot", required=True, help="GET /admin/config-snapshot JSON file")
     p.add_argument("--proposal", default=None,
                    help="routeforge.policy-proposal/v1 JSON file "
-                        "(required unless --shadow-prices is given)")
+                        "(required unless --shadow-prices or --scenarios is given)")
+    p.add_argument("--scenarios", default=None,
+                   help="routeforge.policy-scenarios/v1 JSON file: compare its scenarios "
+                        "side by side (P3c-2). Not combinable with --proposal or "
+                        "--shadow-prices.")
     p.add_argument("--out", required=True, help="output directory")
     p.add_argument("--config-dir", default="config",
                    help="base config directory the snapshot is overlaid onto (default: config)")
     p.add_argument("--fail-on-new-rejections", action="store_true",
-                   help="exit 3 if the proposal newly rejects any simulated row")
+                   help="exit 3 if the proposal (or any scenario) newly rejects any "
+                        "simulated row")
     p.add_argument("--shadow-prices", action="store_true",
                    help="price what each constraint costs (P3c-1); makes --proposal optional")
     p.add_argument("--max-relaxations", type=int, default=50,
@@ -89,12 +107,55 @@ def _below_min_agreement(baseline: dict, minimum: float) -> int:
     return 4
 
 
+def _compare_scenarios(args: argparse.Namespace, *, records: list[dict], warnings: list[str],
+                       snapshot: dict, scenarios_doc: dict, inputs: dict, base,
+                       since) -> int:
+    """`--scenarios` mode (P3c-2): replay every scenario against one shared
+    `current` arm, then write `comparison.json` and `report.md`."""
+    try:
+        comparison = asyncio.run(compare(records, snapshot, scenarios_doc, base=base,
+                                         since=since))
+    except (SnapshotError, ProposalError, ScenarioError) as exc:
+        # All-or-nothing (spec §3): one invalid scenario fails the whole
+        # file, named `scenarios[<i>](<name>).proposal.<path>`.
+        print(f"error: {exc}", file=sys.stderr)
+        return 2
+    except ComparisonInvariantError as exc:
+        print(f"error: internal invariant: {exc}", file=sys.stderr)
+        return 2
+
+    if args.min_agreement is not None and not agreement_ok(comparison.baseline,
+                                                           args.min_agreement):
+        return _below_min_agreement(comparison.baseline, args.min_agreement)
+
+    doc = comparison_doc(comparison, inputs=inputs)
+    out = Path(args.out)
+    out.mkdir(parents=True, exist_ok=True)
+    comparison_path = out / "comparison.json"
+    report_path = out / "report.md"
+    comparison_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n",
+                               encoding="utf-8")
+    report_path.write_text(render_comparison(doc), encoding="utf-8")
+
+    print(f"policy_simulate: {doc['rows']['simulated']} rows simulated under "
+          f"{len(doc['scenarios'])} scenario(s) ({len(warnings)} export warnings) -> {out}")
+    print(str(comparison_path))
+    print(str(report_path))
+
+    if args.fail_on_new_rejections and any(row["newly_rejected"] > 0 for row in doc["table"]):
+        return 3
+    return 0
+
+
 def main(argv: list[str] | None = None) -> int:
     parser = _parser()
     args = parser.parse_args(argv)
 
-    if not args.shadow_prices and not args.proposal:
-        parser.error("--proposal is required unless --shadow-prices is given")
+    if args.scenarios and (args.proposal or args.shadow_prices):
+        # Shadow prices of N scenarios is out of scope (P3c-2 spec §6).
+        parser.error("--scenarios cannot be combined with --proposal or --shadow-prices")
+    if not (args.shadow_prices or args.proposal or args.scenarios):
+        parser.error("--proposal is required unless --shadow-prices or --scenarios is given")
 
     if args.max_relaxations < 1:
         print("error: --max-relaxations must be >= 1", file=sys.stderr)
@@ -131,7 +192,11 @@ def main(argv: list[str] | None = None) -> int:
         proposal, proposal_sha256 = (None, None)
         if args.proposal:
             proposal, proposal_sha256 = load_proposal(args.proposal)
-    except (SnapshotError, ProposalError, ValueError, OSError, json.JSONDecodeError) as exc:
+        scenarios_doc, scenarios_sha256 = (None, None)
+        if args.scenarios:
+            scenarios_doc, scenarios_sha256 = load_scenarios(args.scenarios)
+    except (SnapshotError, ProposalError, ScenarioError, ValueError, OSError,
+            json.JSONDecodeError) as exc:
         print(f"error: {exc}", file=sys.stderr)
         return 2
 
@@ -148,6 +213,22 @@ def main(argv: list[str] | None = None) -> int:
     # values, never this machine's local config.
     serving_policy = {k: snapshot["settings"]["serving_policy"][k]
                       for k in SERVING_POLICY_FIELDS}
+    if args.scenarios:
+        return _compare_scenarios(
+            args, records=records, warnings=warnings, snapshot=snapshot,
+            scenarios_doc=scenarios_doc, base=base, since=since,
+            inputs={
+                "export_sha256": export_sha256,
+                "snapshot_sha256": snapshot_sha256,
+                "scenarios_sha256": scenarios_sha256,
+                "title": scenarios_doc.get("title"),
+                "routing_policy_version": snapshot["routing_policy_version"],
+                "registry_version": snapshot["registry_version"],
+                "snapshot_generated_at": snapshot["generated_at"],
+                "serving_policy": serving_policy,
+                "since": since.isoformat() if since is not None else None,
+            })
+
     inputs = {
         "export_sha256": export_sha256,
         "snapshot_sha256": snapshot_sha256,
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_policy_simulate_cli.py scripts/policy_simulate.py
git commit -m "$(cat <<'MSG'
feat(cli): policy_simulate --scenarios writes a side-by-side comparison (P3c-2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 11: Documentation and the full suite (spec §11)

**Files:**
- Modify: `docs/reference/policy-simulator.md`
- Modify: `docs/research/feasible-route-bench.md` (append §18)
- Modify: `docs/research/arxiv.md` (§4.1)
- Modify: `docs/roadmap/priorities-1-3-status.md`

**Interfaces:**
- Consumes: the names every earlier task produced. Nothing later depends on this task.
- Produces: documentation only.

There is no failing test for prose. Each step is one file. Insert the text exactly as given.

- [ ] **Step 1: `docs/reference/policy-simulator.md`**

(a) In §2.1, replace the line that starts `**Top level (`_TOP_KEYS`):**` so the key list reads `` `schema`, `title`, `apps`, `providers`, `endpoints`, `prices`, `workload` `` and add `workload` and `workload.apps` to the list of objects in the sentence that follows.

(b) In the §2.1 `endpoints` table, add `latency_ms` to the key list of both the `add` row and the `update` row, and append this sentence to the `update` row: "`update` also accepts a priced YAML pool model; `latency_ms` is the only field allowed on one."

(c) After the `**`prices.<slug>`**` paragraph at the end of §2.1, add:

````markdown
**`workload`** (P3c-2) — assumptions about the traffic itself, not configuration. They apply
to the `proposed` arm only; the `current` arm always replays rows as recorded.

| Key | Validation | Effect in replay |
|---|---|---|
| `token_scale` | Finite, `0 < x ≤ 10`. Default `1.0`. | Before routing and pricing: `prompt_tokens' = max(1, round(prompt_tokens × scale))` (a row that recorded 0 prompt tokens stays 0); `completion_tokens` unchanged. A recorded `complexity` below 1 becomes `min(1, complexity × scale)`, which is exact. A saturated complexity (1.0) hides the real token count, so it becomes `min(1, max(scale, prompt_tokens' / 2000))`; when the scale is below 1 that is an estimate, and the row is counted in `assumptions.saturated_complexity_estimated`. |
| `cached_share` | Finite, `0 ≤ x ≤ 1`. Default `0`. | That fraction of the (scaled) prompt tokens is priced at the endpoint's cached-input price. An endpoint with no declared cached price bills them at the input price. The router still estimates with zero cached tokens, so this moves cost, never a route. |
| `apps.<application_id>` | The app must be in the snapshot. Same two keys. | Overrides the global value for that app. |

Outcome rows do not record cached tokens, so any non-zero `cached_share` is an assumption
and the report says so.

**`endpoints.add[].latency_ms` and `endpoints.update.<slug>.latency_ms`** (P3c-2) — an
operator-asserted latency in milliseconds: finite `> 0`, or `null` for "no override". Use it
to model a serving-engine or hardware change; RouteForge cannot see the serving engine, so
this is an assertion, not a measurement. It becomes the replayed latency of that endpoint
(`latency_source: "proposal"`, ahead of observed, signal and tier-default latency) and the
router's latency scoring term, where it is treated exactly as a measurement would be —
including as the same-tier peer prior for unmeasured endpoints. When
`routing.signals.enabled` is false the live router ignores measured latency, so the assertion
then changes the replayed latency only.

**Where assumptions live.** `apply_proposal` writes them into the applied document as a
top-level `assumptions` block: `{"token_scale": {"*": x, "<app>": y}, "cached_share": {...},
"latency_ms": {"<slug>": ms}}`, absent when the proposal sets none. A snapshot **file** that
already contains `assumptions` is a load error, so a hand-edited snapshot cannot put
assumptions into the `current` arm.
````

(d) In §3, add these rows to the flag table, change the `--proposal` row's default to "required unless `--shadow-prices` or `--scenarios`", and change the `--fail-on-new-rejections` meaning to "…when the proposal (or any scenario) newly rejects…":

````markdown
| `--scenarios` | off | a `routeforge.policy-scenarios/v1` file; compares its scenarios side by side (§9). Cannot be combined with `--proposal` or `--shadow-prices` (exit `2`) |
| `--since` | off | ISO-8601 UTC timestamp. Rows with `ts` before it are excluded and counted as `before_since`. Applies to every mode. A value with no offset is read as UTC |
| `--min-agreement` | off | `0 ≤ X ≤ 1`. If baseline agreement (§5) is not computable or is below `X`, print it and exit `4` before any file or directory is written. Applies to every mode |
````

Add this row to the exit-code table, and add "`--scenarios` combined with `--proposal` or `--shadow-prices`, an invalid scenarios file (named `scenarios[<i>](<name>).proposal.<path>`), a `--since` that is not a timestamp, a `--min-agreement` outside `[0, 1]`" to the list in the `2` row:

````markdown
| `4` | baseline agreement is below `--min-agreement`, or there is no comparable row. Nothing is written |
````

(e) In §4.1's field table: add `since` to the `inputs` row ("the `--since` cut as an ISO string, or `null`"); add `before_since` as the first reason in the `rows` row; add `overflow_hops` (`{current, proposed}`) to the `totals` row; add `proposal` to the latency sources in the `sources` row; and add this row after `edits`:

````markdown
| `assumptions` | always present: `token_scale`, `cached_share`, `latency_ms` (each a map, empty when the proposal sets none) and `saturated_complexity_estimated` (rows whose scaled complexity is an estimate) |
````

(f) In §4.2 replace the quoted caveat with the new text, and add "an **Assumptions** section (only when the proposal sets `workload` or `latency_ms`)" between the baseline warning and **Headline** in the ordered list:

````markdown
> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection, using the real eligibility and routing code and a frozen snapshot of signals.
> It does not model other failover, escalation, breaker state or caching.
````

(g) In §5, after the paragraph that starts "For every simulated `ok` row", add:

````markdown
The comparison is between **first decisions**: the model the `current` arm selected first,
before any context-window re-selection (§6), against the recorded first attempt. A row the
`current` arm rejects always counts as a disagreement. `--min-agreement X` turns this check
into a gate (exit `4`), and `--since TS` is the way to cut an export at the first row a
replay can be trusted for — for example the first row recorded after B55.
````

(h) In §6, replace the bullet that starts "**A proposed change to an endpoint's `context_window` has no visible effect.**" with the two bullets below, and in the bullet about shadow prices replace "**Alternative scenarios, a capacity model and uncertainty ranges** remain out of scope (P3c-2 through P3c-4)" with "Alternative scenarios are §9 (P3c-2). **A capacity model and uncertainty ranges** remain out of scope (P3c-3, P3c-4)":

````markdown
- **Context-window overflow is replayed; other failover is not.** Both arms mirror the
  gateway's overflow handling: if the pick declares a `context_window` and
  `prompt_tokens + output_tokens` exceeds it, the pick is excluded and the router selects
  again, at most `routing.max_failover_attempts` extra times — the live bound. If every
  reachable candidate overflows, the row is rejected with code `context_window` (live: a 400).
  An undeclared window (`null`) never overflows. The endpoint served after the hops is the one
  costed, and `overflow_hops` counts the hops. So a proposed `context_window` change now has a
  visible effect.
- **Recorded `context_overflow` rows are still not reproduced.** A failure row records
  `prompt_tokens: 0`, so replay sees a prompt that fits any window. Rows that were served
  carry their real token count and are replayed faithfully.
- **Cached tokens are an assumption, not a record.** Outcome rows do not record cached
  prompt tokens. Both arms price none as cached unless a proposal sets `workload.cached_share`.
````

(i) Append a new section at the end of the file:

````markdown
## 9. Scenario comparison (P3c-2)

Design: `docs/superpowers/specs/2026-10-05-p3c2-scenarios-design.md`. Research notes:
`docs/research/feasible-route-bench.md` §18. Code: `app/eval/scenarios.py`
(`load_scenarios`, `apply_scenarios`, `compare`, `comparison_doc`, `render_comparison`),
wired into `scripts/policy_simulate.py` by `--scenarios`.

### 9.1 What it answers

An administrator has several candidate changes and wants their predicted effects next to
each other, over the same recorded traffic and the same baseline. Each scenario is replayed
against one shared `current` arm.

**RouteForge never picks, ranks or recommends a scenario.** Scenarios are listed in file
order, no output field ranks or scores them, and nothing is applied.

### 9.2 The scenarios file

```json
{
  "schema": "routeforge.policy-scenarios/v1",
  "title": "Healthcare on-prem options",
  "scenarios": [
    {"name": "all-onprem",     "proposal": {"apps": {"claims": {"residency": "onprem_only"}}}},
    {"name": "raw-pii-onprem", "proposal": {"apps": {"claims": {"escalate_residency_on_detection": true}}}},
    {"name": "smaller-prompts", "proposal": {"workload": {"apps": {"claims": {"token_scale": 0.6}}}}}
  ]
}
```

- Top-level keys: `schema`, `title` (optional string), `scenarios`. Any other key is a load
  error naming it.
- `scenarios`: 1 to 20 objects, each with exactly `name` and `proposal`.
- `name`: 1 to 64 characters of `[A-Za-z0-9._+-]`, unique within the file.
- `proposal`: a v1 proposal body (§2). `schema` is optional inside it.
- Loading is all-or-nothing. Every scenario is validated against the snapshot before any
  replay starts. An error is named `scenarios[<i>](<name>).proposal.<dotted path>` and the
  CLI exits `2` with nothing written.
- An empty body `{}` is a valid scenario and predicts zero change.

### 9.3 Running it

```bash
uv run python scripts/policy_simulate.py \
  --export outcomes.ndjson --snapshot snapshot.json --scenarios scenarios.json \
  --out results/cmp [--since 2026-09-25T01:25:00Z] [--min-agreement 0.9] \
  [--fail-on-new-rejections]
```

`--out` receives `comparison.json` and `report.md`. With `--fail-on-new-rejections` the CLI
exits `3` if any scenario newly rejects a row (the files are still written).

### 9.4 `comparison.json`

Schema `routeforge.scenario-comparison/v1`. Deterministic: sorted keys, floats rounded to 6
places.

| Field | Contents |
|---|---|
| `comparison_id` | first 16 hex characters of the sha256 over `inputs` |
| `inputs` | `export_sha256`, `snapshot_sha256`, `scenarios_sha256`, `title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy`, `since`, `window` |
| `rows` | `simulated` and `excluded` — the same for every scenario |
| `baseline` | the fidelity block of §5, computed once from the shared `current` arm |
| `scenarios` | file order. Each: `name`, `edits`, `assumptions`, and `summary` — the §4.1 body (`totals`, `by_app`, `by_workload`, `moves`, `new_rejections`, `affected_apps`, `sources`, `per_day`) |
| `table` | file order. One row per scenario: `name`, `cost_per_day_delta_usd`, `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`, `overflow_hops`, `affected_apps` |

**The table's cost columns are like-for-like.** `cost_delta_usd` is
`totals.cost_usd_both_routed.delta` — only rows routed in both arms — and
`cost_per_day_delta_usd` is that divided by the export window in days (`null` under one day).
A scenario that newly rejects rows therefore never looks cheaper because of it; read
`newly_rejected` for the service loss. The all-routed delta is in each scenario's
`summary.totals.cost_usd`. `overflow_hops` is the scenario arm's total.

A scenarios file with one scenario gives exactly the per-scenario numbers that `--proposal`
gives for the same body.

### 9.5 `report.md`

In order: the §4.2 caveat; the title; a **baseline agreement** banner (overall rate, a ⚠
warning under 90%, the rate per app, and the `--since` cut when one was given); **one
comparison table** in file order, followed by the sentence "Listed in file order. RouteForge
does not rank or recommend scenarios." and the like-for-like note; an **Assumptions** line
for each scenario that sets `workload` or `latency_ms`; a short section per scenario
(metrics, moves, new rejections by app, code and conflicting core, affected apps); and the
excluded rows.

### 9.6 Limits

- No uncertainty ranges (P3c-3) and no capacity or overload model (P3c-4).
- `--scenarios` cannot be combined with `--shadow-prices`.
- `token_scale` on a saturated row and every `cached_share` are assumptions; the report
  labels both.
- Every §6 limit applies to every scenario.
````

- [ ] **Step 2: `docs/research/feasible-route-bench.md` — append §18**

````markdown
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
- **Context-window overflow.** Both arms mirror the gateway: exclude the overflowing pick,
  select again, at most `max_failover_attempts` extra times. This is the first later-hop
  behaviour replayed. It is safe to replay because it is a pure function of declared data.

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

**TO MEASURE.** No live run has been made; nothing below is a result yet. These numbers are
legitimately unmeasured, not omitted.

| measurement | value |
|---|---|
| Spark export, baseline agreement before overflow replay | TO MEASURE |
| Spark export, baseline agreement after overflow replay (§18.2 predicts no change) | TO MEASURE |
| Spark export, rows with `overflow_hops > 0` in the current arm | TO MEASURE |
| Healthcare triad (all on-prem / raw-PII-only on-prem / all on-prem + second endpoint) on persona traffic: the comparison table | TO MEASURE |
| Static 4-request test vs traffic-weighted replay of the same triad | TO MEASURE |

Caveat for when they are measured: the Spark serves one app with every model priced at $0
(§17.4), so cost columns there will be zero. A priced, multi-app result needs persona traffic.
````

- [ ] **Step 3: `docs/research/arxiv.md` §4.1**

Replace the bullet that starts `- **Invariants to cite:**` with:

````markdown
- **Invariants to cite, with the test that pins each** (full table: FB §18.3):
  S1 `test_s1_empty_scenario_is_a_noop`; S2 `test_s2_single_scenario_equals_single_proposal`;
  S3 `test_s3_order_independence`; S4 `test_s4_neutral_workload_changes_nothing` and
  `test_s4_neutral_latency_changes_no_route_or_cost`; S5 `test_s5_cost_monotone_in_token_scale`;
  S6 `test_s6_cached_share_never_raises_cost`; S7 `test_s7_overflowing_pick_is_never_served`;
  S8 `test_s8_no_ranking_and_file_order`; S9
  `test_s9_scenario_proposals_never_widen_eligibility`; S10
  `test_s10_since_excludes_and_counts_old_rows`; S11
  `test_s11_min_agreement_exits_4_and_writes_nothing`.
- **Findings from the build (FB §18.2):** the saturated-complexity rule needs a lower bound of
  `scale`, or scaling tokens up can lower complexity; an asserted latency must be scored as a
  measurement, or re-asserting the current value can move a route through the peer prior.
````

Replace the bullet that starts `- **Expected finding to check:**` with:

````markdown
- **Expected finding, revised:** the design expected context-window replay to *raise* baseline
  agreement. By construction it should leave it unchanged: the baseline compares first
  decisions, and failure rows record `prompt_tokens: 0`, so recorded overflows cannot be
  reproduced (FB §18.2). **TO MEASURE** (before/after agreement on the Spark export).
````

Leave the `**Experiment E-P3c2 (TO MEASURE)**` bullet and the E7/E8 rows of §6 as they are.

- [ ] **Step 4: `docs/roadmap/priorities-1-3-status.md`**

(a) Add this row to the Priority 3 table in §1, after the P3c-1 row. Replace `#NN` with this branch's PR number once it exists; until then write "this branch".

````markdown
| **P3c-2** alternative scenarios | #NN | `policy_simulate.py --scenarios` replays several administrator-written scenarios against one shared baseline and writes one comparison table, in file order, never ranked. Proposals can assume a token scale, a cached-prompt share and a per-endpoint latency. Replay mirrors the gateway's context-window overflow re-selection. `--since` cuts old rows; `--min-agreement` refuses a run whose baseline disagrees with the record. | `docs/reference/policy-simulator.md` §9, research §18 |
````

(b) In §2.1 change the heading to `### 2.1 Priority 3 (in progress: P3c-3 is next)`, delete list item 1 ("**P3c-2 alternative scenarios**…" with its open design questions) and renumber the rest. In the replay-fidelity item, replace the bullet "`context_window` changes are invisible because they are enforced at call time;" with "recorded `context_overflow` rows are not reproduced, because failure rows record no prompt size (research §18.2);".

(c) In §3 replace step 2 with: "Start P3c-3 (uncertainty ranges) with a design brainstorm, then a spec, plan and subagent build."

(d) Update the "As of" line at the top to this branch's merge date and commit.

- [ ] **Step 5: Check the docs against the code**

```bash
grep -n "test_s[0-9]" docs/research/feasible-route-bench.md | sed 's/.*::\(test_[a-z0-9_]*\).*/\1/' | sort -u | while read t; do grep -rq "def $t" tests/eval || echo "MISSING $t"; done
```

Expected: no output.

- [ ] **Step 6: Run the full suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest -q
```

Expected: 0 failed. The suite takes more than ten minutes. In an environment without the
Presidio extra, `tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app`
fails with `ModuleNotFoundError` on the base commit too; that one is not caused by this
branch. Any other failure is.

- [ ] **Step 7: Commit**

```bash
git add docs/reference/policy-simulator.md docs/research/feasible-route-bench.md docs/research/arxiv.md docs/roadmap/priorities-1-3-status.md
git commit -m "$(cat <<'MSG'
docs: P3c-2 scenario comparison reference, research §18, roadmap status

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Open risks

- **The overflow rule uses the router's output estimate.** `prompt + output` uses the client's
  recorded `max_tokens`, else the deployment default (512). A live backend given no
  `max_tokens` may only overflow when the prompt alone exceeds the window, so replay can report
  an overflow hop for a prompt within 512 tokens of the limit that was served live.
- **Recorded prompt tokens are the backend's count.** The live router uses RouteForge's own
  count. They differ by tokenizer and chat template, which affects both the overflow check and
  `token_scale` on saturated rows.
- **`--min-agreement` in `--scenarios` mode runs after the replay.** The gate needs the shared
  baseline, which `compare` returns at the end. Nothing is written, but the time is spent.
- **An application whose id is literally `*`** would collide with the global key of the
  `assumptions` block. No check guards this.
