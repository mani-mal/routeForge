# Replayable Rejections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a request that RouteForge rejected replayable by the offline policy simulator, so a proposal that raises a budget shows the requests it would rescue, and make the simulator's baseline check notice when replay serves a request the record says was rejected (B60).

**Architecture:** One content-free serving change: every `routing_outcomes` row records `routing_tokens`, the token count the router was given, set on the outcome context next to `complexity` and copied onto the row by both outcome builders. Offline, `app/eval/simulate.py` gives that count to `Router.select`, keeps pricing on the backend's `prompt_tokens`, prices a row no backend answered at the router's own admission estimate, and excludes a rejected row that records no size. The baseline check gains outcome-level agreement (routed or rejected against the recorded status) beside model agreement; both reports show it and `--min-agreement` gates on the lower of the two.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), pytest, Hypothesis, `uv`. No new dependency. One new nullable column; deployed databases need one `ALTER`.

**Spec:** docs/superpowers/specs/2026-10-06-replayable-rejections-design.md

## Deviations from spec

The spec was amended while this plan was written, so the plan and the spec agree: there is no plan-versus-spec deviation. What follows are the places where the design departs from the brief the spec was written from, or where the brief was silent. The spec lists the same items in its §12. Each follows the code as it is, keeps the brief's intent, and is pinned by a test.

1. **The `missing_routing_tokens` exclusion covers every unsized row, not only rejected ones.** The brief said rows "recorded as rejected without `routing_tokens`". `build_failure_outcome` writes `prompt_tokens: 0` on every failure row built without a trace, so a pre-fix `backend_failed` row (and a `verify_failed` row whose trace stash failed) has the same empty-prompt defect. The rule is stated on the data: `status != "ok"`, no `routing_tokens`, `prompt_tokens == 0`. Task 3: `test_r6_unsized_rows_are_excluded_and_counted_never_routed` (four statuses), `test_rows_that_still_have_a_size_are_kept`.
2. **A gate rejection writes no outcome row, so the brief's null example does not occur.** The brief's example of a null column was "the gate itself rejects". A policy-gate error is a 503 and a pinned-model rejection a 422; neither writes a row (`app/api/routes.py::_outcome_failure_status`). Every status that writes a row is raised at or after the gateway call, which is after compression. A row written by this build is therefore null only on a semantic-cache hit. The counted exclusion is reached by rows from before the column existed. Task 2: `test_semantic_cache_hit_row_has_null_routing_tokens` and one test per status.
3. **The context-window check stays on the backend's count for a served row.** The brief assigned routing to `routing_tokens` and pricing to `prompt_tokens`, and did not assign the window check. Live overflow is the backend's own refusal on its own tokenisation, so the check goes with the backend's count and P3c-2's overflow results for served rows do not move. A row no backend answered uses its routing tokens. Task 3: `test_window_check_uses_the_backend_count_for_a_served_row`, `test_context_overflow_row_is_reproduced_when_the_window_is_declared`.
4. **A row no backend answered is priced with an output estimate as well as a prompt estimate.** The brief gave such a row a prompt count (`routing_tokens`). It has no completion count either. Pricing it at its prompt alone would understate what rescuing it costs, so it is priced at the pair the live budget check priced it at: its routing tokens and `expected_output_tokens` (the recorded `max_tokens`, else the deployment default). Task 3: `test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate`, `test_no_recorded_cap_prices_the_rescued_row_at_the_default_output_estimate`.
5. **`context_overflow` is classed "rejected" although its first decision was a route.** The brief's status vocabulary matches `app/apps/outcomes.py::STATUSES` exactly (checked; pinned by Task 4's `test_r11_every_status_has_a_recorded_outcome_class`). Live, a `context_overflow` request was routed and then refused by the backend on every attempt. Replay models that as a `context_window` rejection (P3c-2), so the class is the one the arm reports.
6. **The `--min-agreement` gate needs both rates to be computable.** The brief said "the minimum of the two agreements". With no served row, model agreement is `None`; that case already exits 4 and is pinned by the existing `test_min_agreement_with_no_comparable_rows_exits_4`, so it still does. Task 4: `test_r12_gate_takes_the_lower_of_the_two`.
7. **Additions the brief did not ask for.** (a) `recorded_rejected`, the denominator, and a report note for any non-zero disagreement count: a pooled outcome rate dilutes rejections, which are a small share of traffic (Task 4: `test_diluted_outcome_agreement_still_reports_every_rejection_wrong`; Task 5: `test_report_notes_a_diluted_disagreement_without_a_warning`). (b) `rows.estimated_tokens`. (c) One line stating both agreements on every single-proposal report, not only when one is low. (d) Invariants R9–R13. (e) A test for the unmigrated-table claims; B55 made the same claims without one.
8. **"Per app" is one map.** The brief asked for the outcome rate and the two counts "per app". They are reported together as `outcome_by_app.<id>` = `{rows, agreement, recorded_rejected, recorded_rejected_but_routed, recorded_routed_but_rejected}`.
9. **`token_scale` applies to both counts.** The brief mentioned the scale for the routing count only. The pricing count is scaled with the same rule, as it was before this change.
10. **`saturated_complexity_estimated` is left as it is.** With `routing_tokens` on the row a saturated row's scaled complexity could be computed instead of estimated. That is a refinement of P3c-2, not part of the defect, and is not made here.
11. **One existing fixture changes meaning.** `tests/eval/test_proposal_workload.py::_mixed_rows` carried a zero-token `backend_failed` row to pin "a zero-token row stays at zero under `token_scale`". That row is now excluded as unsized, so the fixture's zero-token row becomes a served row, the only kind still replayed at zero.

## Global Constraints

- **Content-free.** `routing_outcomes` holds ids, enum values, numbers, entity types and version hashes only. `routing_tokens` is an integer count. Never record request text, a hash of it, or anything derived from its content other than its length.
- **Nothing external before the gate; fail closed.** The value is read from `comp.tokens_after`, which exists only after the gate and compression have run. No ordering in `app/pipeline.py` changes. A failed outcome write is logged and swallowed, as today; it must never change a response.
- **`prompt_tokens` is not repurposed.** It stays the backend's billed count, `0` on a row no backend answered.
- **The wire does not change.** Nothing is added to a success body, an error envelope or an SSE frame.
- **No serving import of `app.eval`.** `tests/eval/test_isolation.py` must keep passing. `app/eval` may import serving modules; never the reverse.
- **No code tolerates a missing column.** The `ALTER` is a hard pre-deploy step, as for B55 and P3b. Do not add reflection, conditional inserts or a migration runner.
- **Exact values from the spec.** Column: `routing_tokens`, `Integer`, nullable. `EXCLUSION_REASONS = ("before_since", "cache_hit", "unknown_status", "unknown_app", "missing_features", "missing_routing_tokens")`. A valid recorded `routing_tokens` is an `int` (not `bool`) `>= 0`, or null/absent. Recorded outcome classes: `ok`, `verify_failed`, `backend_failed` → `routed`; `no_eligible`, `context_overflow` → `rejected`. Warning threshold for either agreement: `< 0.90`. Exit codes are unchanged: `0`, `2`, `3`, `4`.
- **Deterministic JSON.** `json.dumps(..., sort_keys=True, indent=2)`, floats through `_round6` (`app/eval/outcomes.py`).
- **Tests.** Always run pytest as `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest ...`; without the variable pytest hangs on import. There is no pytest-asyncio: use `asyncio.run` (offline code) or `client.portal.call` (inside a `TestClient`). Shared simulator fixtures are in `tests/eval/sim_fixtures.py` (`BASE_SETTINGS`, `make_snapshot`, `app_entry`, `onboarded_row`, `make_row`). HTTP-boundary helpers are in `tests/test_outcome_hooks.py` (`DEMO`, `HELLO`, `SSN`, `EMAIL`, `_client`, `_only`, `_outcomes`, `_columns`, `_failing_backend`, `_failing_verify`) and `tests/onboarding/harness.py` (`onboard`, `conformance_client`, `install_dual_fake`).
- **What each task runs.** The focused tests named in its steps, then `tests/eval` and the serving tests the task touched. The whole suite runs once, in Task 8, with `-rf`: it takes more than ten minutes and prints no summary line under `-q`, so `-rf` is what lists a failure. One failure is pre-existing and not caused by this branch: `tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app`.
- **Code style.** Type-hint everything. Match the surrounding comment density and idiom. `app/eval` modules import each other's underscore-private helpers (as `scenarios.py` and `shadow.py` already do); keep that idiom. No application name in a symbol, test id, fixture or comment (`.claude/skills/consumer-contract-tests`).
- **How to read the code steps.** A step that says "Create" shows the whole new file. A step that says "Patch" shows a real patch against the tree as the previous task left it. Save the block to a file and run `git apply <file>`, or make the same edits by hand. Every block in this plan was applied in order to a scratch copy of the branch and the listed tests were run against it.
- **Git.** Stage files by name, never `git add -A`. Every commit message ends with exactly this line; do not substitute your own model name:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- **Do not push.** Do not deploy. The `ALTER` on the Spark and the DGX lab is an operator step.

## Review Focus

Six input classes the spec implies, that are likely to bite a user, and that an obvious task test would miss. Each has a named test in the owning task.

1. **A rejection caused by size, not by the output cap.** A request over budget because of a large `max_tokens` is over budget with a zero-token prompt too, so a test built on it passes before the fix. The acceptance tests use a long prompt, a small `max_tokens` and a cap between the two estimates. Task 2: `test_budget_rejected_row_records_the_tokens_the_router_was_given`. Task 6: `test_live_round_trip_budget_rejections_replay_as_rejected`.
2. **Request shapes whose token count is not just the prompt.** A tool conversation (schemas and replayed arguments are counted), all three stream modes, a semantic-cache hit (no router ran: null), a `verify_failed` request (both counts are real and differ). Task 2: `test_tool_conversation_row_counts_what_the_router_priced`, `test_streamed_row_records_routing_tokens`, `test_semantic_cache_hit_row_has_null_routing_tokens`, `test_verify_failed_row_records_both_counts`.
3. **Degenerate values in an export.** `routing_tokens` as a string, a float, a bool, a negative number, a list; `0`; null; absent. Task 3: `test_mistyped_routing_tokens_is_missing_features`, `test_rows_that_still_have_a_size_are_kept`.
4. **Exports from two eras concatenated.** Pre-fix and post-fix rows in one file; an unsized row that is also before `--since`, of an unknown app, or malformed (it must be counted once, under the earlier reason). Task 3: `test_r9_export_without_the_field_still_loads_and_replays_served_rows`, `test_missing_routing_tokens_is_the_last_exclusion_reason`. Task 5: `test_cli_counts_prefix_rejected_rows_as_missing_routing_tokens`.
5. **Agreement on thin or lopsided data.** Two rejections among thirty rows (the rate stays above 90% while every rejection replays wrong); no served row; no row at all; one app wrong and one right. Task 4: `test_diluted_outcome_agreement_still_reports_every_rejection_wrong`, `test_r12_gate_takes_the_lower_of_the_two`, `test_outcome_agreement_is_null_with_no_simulated_row`, `test_a_recorded_route_that_replay_rejects_is_counted_per_app`.
6. **The upgraded image against an old table.** A fresh process, not a table altered under a running one; the documented statement run twice. Task 2: `test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `app/apps/models.py` | modify | `RoutingOutcome.routing_tokens` |
| `app/apps/outcomes.py` | modify | `_policy_features` copies `routing_tokens` onto every row |
| `app/pipeline.py` | modify | `run` and `run_stream` put `routing_tokens` on the outcome context |
| `app/eval/simulate.py` | modify | `replay_tokens`, `_scale_tokens`, the `missing_routing_tokens` exclusion, estimated pricing, `RECORDED_OUTCOME`, `_outcome_stats`, outcome fields in `_baseline`, `gate_agreement`, report lines |
| `app/eval/scenarios.py` | modify | outcome agreement in the comparison banner |
| `scripts/policy_simulate.py` | modify | the exit-4 message names both rates; help text |
| `tests/test_outcome_builder.py` | modify | builders copy the field |
| `tests/test_outcome_store.py` | modify | schema test: nullable integer |
| `tests/test_outcome_ctx_pipeline.py` | modify | the outcome context carries the field |
| `tests/test_outcome_routing_tokens.py` | create | HTTP boundary: one test per status (R10), content-free (R1), export, unmigrated table (R13) |
| `tests/test_openai_compat.py` | modify | consumer contract: the wire is unchanged (R8) |
| `tests/eval/test_replay_rejections.py` | create | replay and agreement (R2, R3, R4, R6, R9, R11, R12), report lines |
| `tests/eval/test_simulate.py` | modify | two expectations that legitimately change |
| `tests/eval/test_proposal_workload.py` | modify | the `_mixed_rows` fixture's zero-token row |
| `tests/eval/test_scenarios_compare.py` | modify | the baseline key set; the banner |
| `tests/eval/test_policy_simulate_cli.py` | modify | the gate; the live round trip with rejections (R3, R4, R5, R6) |
| `docs/reference/routing-outcomes.md` | modify | the column, both `ALTER`s, the content-free table, the export example |
| `docs/reference/policy-simulator.md` | modify | §1.1, §3, §4.1, §4.2, §5, §6, §9.4, §9.5 |
| `docs/backlog/bugs.md` | modify | B60 resolved |
| `docs/research/feasible-route-bench.md` | modify | new §19 |
| `docs/research/arxiv.md` | modify | §4.1, §4.4, experiment E7b |
| `docs/roadmap/priorities-1-3-status.md` | modify | the slice in Built; remaining fidelity gaps |

## Spec invariants → tests

| # | Invariant | Test | Task |
|---|---|---|---|
| R1 | content-free with the new column | `tests/test_outcome_store.py::test_schema_is_content_free`; `tests/test_outcome_routing_tokens.py::test_r1_rejected_row_with_pii_carries_a_count_and_no_content` | 1, 2 |
| R2 | a served row's replay is unchanged when `routing_tokens == prompt_tokens` | `tests/eval/test_replay_rejections.py::test_r2_served_row_replay_is_unchanged_when_the_counts_coincide` | 3 |
| R3 | a budget rejection is rejected in the `current` arm | `tests/eval/test_replay_rejections.py::test_r3_budget_rejected_row_is_rejected_in_the_current_arm`; live `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected` | 3, 6 |
| R4 | raising the cap newly serves it | `tests/eval/test_replay_rejections.py::test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate`; live: the same round-trip test | 3, 6 |
| R5 | outcome agreement 1.0 on a live round trip with rejections | `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected`, `::test_live_round_trip_scenarios_show_the_rescued_rejections` | 6 |
| R6 | unsized rows are excluded and counted, never routed | `tests/eval/test_replay_rejections.py::test_r6_unsized_rows_are_excluded_and_counted_never_routed`; `tests/eval/test_policy_simulate_cli.py::test_cli_counts_prefix_rejected_rows_as_missing_routing_tokens`, `::test_live_export_stripped_of_routing_tokens_excludes_the_rejections` | 3, 5, 6 |
| R7 | serving isolation | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` (unchanged; run in Tasks 2 and 8) | 2, 8 |
| R8 | the OpenAI wire shape is unchanged | `tests/test_openai_compat.py::test_outcome_recording_adds_nothing_to_the_success_wire`, `::test_outcome_recording_adds_nothing_to_the_rejection_wire`, and the rest of that file | 2 |
| R9 | an export without the field still loads and replays served rows | `tests/eval/test_replay_rejections.py::test_r9_export_without_the_field_still_loads_and_replays_served_rows` | 3 |
| R10 | the recorded value is the router's input, on every status | `tests/test_outcome_routing_tokens.py::test_ok_row_records_the_tokens_the_router_was_given`, `::test_budget_rejected_row_records_the_tokens_the_router_was_given`, `::test_backend_failed_row_records_routing_tokens`, `::test_context_overflow_row_records_routing_tokens`, `::test_verify_failed_row_records_both_counts`, `::test_streamed_row_records_routing_tokens`, `::test_tool_conversation_row_counts_what_the_router_priced`, `::test_semantic_cache_hit_row_has_null_routing_tokens` | 2 |
| R11 | every status has a recorded outcome class | `tests/eval/test_replay_rejections.py::test_r11_every_status_has_a_recorded_outcome_class` | 4 |
| R12 | the gate takes the lower of the two | `tests/eval/test_replay_rejections.py::test_r12_gate_takes_the_lower_of_the_two`; `tests/eval/test_policy_simulate_cli.py::test_r12_min_agreement_exits_4_on_outcome_disagreement` | 4, 5 |
| R13 | an unmigrated table does not break serving; the documented `ALTER` repairs it | `tests/test_outcome_routing_tokens.py::test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it` | 2 |

---

## Tasks

### Task 1: The `routing_tokens` column and the outcome builders (spec §3.1, §3.2, R1)

**Files:**
- Modify: `app/apps/models.py` (`RoutingOutcome`, after `conflicting_core`, the last column, line 319)
- Modify: `app/apps/outcomes.py` (`_policy_features` line 276; the `build_failure_outcome` docstring, from line 343)
- Test: `tests/test_outcome_builder.py`, `tests/test_outcome_store.py`

Line numbers throughout this plan are as of the branch's base commit; later tasks shift them.

**Interfaces:**
- Consumes (all unchanged):
  - `outcome_context(ctx, *, max_tokens: int | None = None) -> dict` with keys `sensitivity`, `risk_class`, `onprem_only`, `detected_entity_types`, `app_policy_version`, `requested_model`, `max_tokens`. The pipeline adds `complexity` to that dict after compression.
  - `build_ok_outcome(resp: dict, *, request_id, application_id, outcome_ctx: dict, routing_policy_version, workload, workload_source) -> dict`
  - `build_failure_outcome(*, status, request_id, application_id, outcome_ctx: dict, routing_policy_version, endpoint_registry_version, workload, workload_source, attempts=(), rejected=(), latency_ms=0.0, trace=None, prompt_tokens=0, completion_tokens=0, cost_usd=0.0, conflicting_core=None) -> dict`
- Produces:
  - `RoutingOutcome.routing_tokens: Mapped[int | None]` — `Integer`, nullable, no default.
  - Both builders' returned dict has the key `"routing_tokens"`, equal to `outcome_ctx.get("routing_tokens")` (so `None` when the key is absent). Task 2 is what puts the key on `outcome_ctx`.
  - `prompt_tokens` on the returned dict is unchanged in every case.

**Rules.** `outcome_context()` itself does not gain the key: it runs before compression, when the count does not exist. The value reaches the row only through `_policy_features`, which both builders already call, so neither builder's signature changes and no route handler changes. Null means "not recorded"; never write `0` for it.

- [ ] **Step 1: Write the failing tests**

Patch `tests/test_outcome_builder.py`:

````diff
diff --git a/tests/test_outcome_builder.py b/tests/test_outcome_builder.py
index 03ac4b3..5dd6e85 100644
--- a/tests/test_outcome_builder.py
+++ b/tests/test_outcome_builder.py
@@ -604,6 +604,64 @@ def test_build_failure_outcome_verify_failed_reads_the_stashed_trace():
     assert SSN not in dumped and EMAIL not in dumped
 
 
+# --- routing_tokens (B60) -------------------------------------------------
+
+def test_both_builders_copy_routing_tokens_from_the_outcome_ctx():
+    """The token count the router was given rides on `outcome_ctx` (the
+    pipeline sets it next to `complexity`) and lands on the row whatever the
+    status. `prompt_tokens` is untouched: it stays the backend's count."""
+    ctx = outcome_context(_ctx())
+    ctx["complexity"] = 1.0
+    ctx["routing_tokens"] = 5280
+    ok = build_ok_outcome(
+        _resp([_router_hop("m"), _verify()], prompt_tokens=5011),
+        request_id="req-rt", application_id="app-1", outcome_ctx=ctx,
+        routing_policy_version="rpv-1", workload=None, workload_source="none")
+    assert ok["routing_tokens"] == 5280
+    assert ok["prompt_tokens"] == 5011
+    for status in ("no_eligible", "context_overflow", "backend_failed"):
+        row = build_failure_outcome(
+            status=status, request_id="req-rt", application_id="app-1",
+            outcome_ctx=ctx, routing_policy_version="rpv-1",
+            endpoint_registry_version=None, workload=None, workload_source="none")
+        assert row["routing_tokens"] == 5280, status
+        assert row["prompt_tokens"] == 0, status
+
+
+def test_routing_tokens_is_null_when_the_request_never_reached_compression():
+    """`outcome_context` itself does not know the count -- compression has
+    not run yet. A row built from it alone (a semantic-cache hit; a failure
+    before step 5) records null, never 0."""
+    ctx = outcome_context(_ctx())
+    assert "routing_tokens" not in ctx
+    ok = build_ok_outcome(
+        _resp([_verify()]), request_id="req-rt0", application_id="app-1",
+        outcome_ctx=ctx, routing_policy_version="rpv-1", workload=None,
+        workload_source="none")
+    failed = build_failure_outcome(
+        status="no_eligible", request_id="req-rt0", application_id="app-1",
+        outcome_ctx={}, routing_policy_version="rpv-1",
+        endpoint_registry_version=None, workload=None, workload_source="none")
+    assert ok["routing_tokens"] is None
+    assert failed["routing_tokens"] is None
+
+
+def test_verify_failed_row_keeps_both_token_counts():
+    """A `verify_failed` row is built from the served attempt's trace: the
+    backend's `prompt_tokens` AND the router's `routing_tokens` are both
+    real, and they are different numbers."""
+    ctx = outcome_context(_ctx())
+    ctx["routing_tokens"] = 1620
+    row = build_failure_outcome(
+        status="verify_failed", request_id="req-rtv", application_id="app-1",
+        outcome_ctx=ctx, routing_policy_version="rpv-1",
+        endpoint_registry_version="erv-1", workload=None, workload_source="none",
+        trace=[_router_hop("m"), _verify(passed=False)],
+        prompt_tokens=1500, completion_tokens=40, cost_usd=0.003)
+    assert row["routing_tokens"] == 1620
+    assert row["prompt_tokens"] == 1500
+
+
 # --- feedback_values -----------------------------------------------------
 
 def test_feedback_values_accepts_a_slashed_model_slug():
````

Patch `tests/test_outcome_store.py`:

````diff
diff --git a/tests/test_outcome_store.py b/tests/test_outcome_store.py
index d8eb88f..de2d842 100644
--- a/tests/test_outcome_store.py
+++ b/tests/test_outcome_store.py
@@ -315,6 +315,12 @@ def test_schema_is_content_free():
     assert "prompt_tokens" in RoutingOutcome.__table__.columns
     assert "prompt_tokens_bucket" in RoutingOutcome.__table__.columns
     assert "completion_tokens" in RoutingOutcome.__table__.columns
+    # B60: the routing token count is a nullable integer COUNT -- no text can
+    # ride on it, and old rows (and cache hits) leave it null.
+    from sqlalchemy import Integer
+    routing_tokens = RoutingOutcome.__table__.columns["routing_tokens"]
+    assert isinstance(routing_tokens.type, Integer)
+    assert routing_tokens.nullable is True
 
 
 def test_outcome_feedback_has_the_app_request_composite_index():
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_outcome_builder.py tests/test_outcome_store.py -p no:warnings -q -rf
```

Expected: 4 failed, each with `KeyError: 'routing_tokens'` — `test_both_builders_copy_routing_tokens_from_the_outcome_ctx`, `test_routing_tokens_is_null_when_the_request_never_reached_compression`, `test_verify_failed_row_keeps_both_token_counts`, and `tests/test_outcome_store.py::test_schema_is_content_free`.

- [ ] **Step 3: Implement**

Patch `app/apps/models.py`:

````diff
diff --git a/app/apps/models.py b/app/apps/models.py
index 3df72a4..a669a3b 100644
--- a/app/apps/models.py
+++ b/app/apps/models.py
@@ -317,6 +317,14 @@ class RoutingOutcome(Base):
     # other status. Added after the table shipped: a deployed DB needs
     # `ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS conflicting_core JSON;`.
     conflicting_core: Mapped[dict | None] = mapped_column(JSON, nullable=True)
+    # B60: the token count the router was given (`tokens_after`: RouteForge's
+    # own post-gate, post-compression count), on EVERY status -- so the policy
+    # simulator can replay a rejected request at its real size. Not the
+    # backend's billed count; that stays `prompt_tokens`. Null on a
+    # semantic-cache hit and on rows written before the column existed. Added
+    # after the table shipped: a deployed DB needs the ALTER in
+    # docs/reference/routing-outcomes.md.
+    routing_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
 
 
 class OutcomeFeedback(Base):
````

Patch `app/apps/outcomes.py`:

````diff
diff --git a/app/apps/outcomes.py b/app/apps/outcomes.py
index 188b75a..91591a5 100644
--- a/app/apps/outcomes.py
+++ b/app/apps/outcomes.py
@@ -274,6 +274,10 @@ def build_ok_outcome(resp: dict, *, request_id: str, application_id: str,
 
 
 def _policy_features(outcome_ctx: dict) -> dict:
+    """The request features both builders copy off `outcome_ctx`, so a
+    served and a rejected row can never disagree about them. `routing_tokens`
+    (B60) is the token count the router was given -- a count, never text --
+    and is `None` when the request never reached compression."""
     return {
         "sensitivity": outcome_ctx.get("sensitivity"),
         "risk_class": outcome_ctx.get("risk_class"),
@@ -281,6 +285,7 @@ def _policy_features(outcome_ctx: dict) -> dict:
         "detected_entity_types": list(outcome_ctx.get("detected_entity_types") or []),
         "requested_model": outcome_ctx.get("requested_model"),
         "max_tokens": outcome_ctx.get("max_tokens"),
+        "routing_tokens": outcome_ctx.get("routing_tokens"),
     }
 
 
@@ -359,10 +364,11 @@ def build_failure_outcome(*, status: str, request_id: str, application_id: str,
     the caller collected before the failure -- and leaves everything that
     presumes a served response (`chosen`, `scoring`, `eligible`,
     `prompt_tokens`/`completion_tokens`, `cost_usd`, the `verify_*` fields,
-    `tier_target`, `prompt_tokens_bucket`) null or empty. `complexity` is
-    read off `outcome_ctx.get("complexity")` like `build_ok_outcome` --
-    known the moment the gate/compression ran, independent of whether the
-    request went on to fail. `rejected` is normalized through the same
+    `tier_target`, `prompt_tokens_bucket`) null or empty. `complexity` and
+    `routing_tokens` (B60) are read off `outcome_ctx` like `build_ok_outcome`
+    -- known the moment the gate/compression ran, independent of whether the
+    request went on to fail. `prompt_tokens` stays 0 here: it is the
+    backend's billed count, and no backend answered. `rejected` is normalized through the same
     `{name, code}`-only rule `build_ok_outcome` uses, regardless of what
     shape the caller's entries carry.
 
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_outcome_builder.py tests/test_outcome_store.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_outcome_builder.py tests/test_outcome_store.py app/apps/models.py app/apps/outcomes.py
git commit -m "$(cat <<'MSG'
feat(outcomes): routing_tokens column, copied onto every outcome row (B60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 2: The pipeline records the count; HTTP-boundary and consumer-contract tests (spec §3.2–§3.5, §4, R1, R7, R8, R10, R13)

**Files:**
- Modify: `app/pipeline.py` (`Pipeline.run` lines 917–919; `Pipeline.run_stream` lines 1252–1254)
- Create: `tests/test_outcome_routing_tokens.py`
- Test: `tests/test_outcome_ctx_pipeline.py`, `tests/test_openai_compat.py`

**Interfaces:**
- Consumes:
  - Task 1's `RoutingOutcome.routing_tokens` and the builders' `"routing_tokens"` key read from `outcome_ctx.get("routing_tokens")`.
  - In both `Pipeline.run` and `Pipeline.run_stream`: `comp` (a `CompressionResult` with `tokens_after: int`), and `internals: dict | None`, whose `"outcome_ctx"` entry is the dict `outcome_context(...)` returned.
  - `Router.select(self, ctx, tokens_after: int, complexity: float, min_tier=None, exclude=frozenset(), output_tokens=None, open_circuits=frozenset())` — the tests spy on it; the gateway passes `tokens_after` positionally.
  - Test helpers: `tests/test_outcome_hooks.py` (`DEMO`, `HELLO`, `SSN`, `EMAIL`, `_client`, `_columns`, `_failing_backend`, `_failing_verify`, `_only`, `_outcomes`); `tests/onboarding/harness.py` (`onboard(client, app_id, **policy) -> OnboardedApp` with `.chat(payload)` and `.headers`, `install_dual_fake(monkeypatch)`, `conformance_client()`); `tests/test_admin_api.py::_login`; `tests/conftest.py::v1_error`; `tests/_litellm_fake.py` (`CALLS`, `make_recording_acompletion`, `make_response`).
- Produces:
  - `internals["outcome_ctx"]["routing_tokens"] = comp.tokens_after` in `run` and `run_stream`, set on the line after `complexity`.
  - So every row written with status `ok`, `no_eligible`, `context_overflow`, `backend_failed` or `verify_failed` carries `routing_tokens`; a semantic-cache hit carries null.
  - The export line's `outcome` object gains `"routing_tokens"` with no exporter change (`_export_outcome_dict` dumps every column).

**Rules.** The only serving-code change is one line in two places. Do not touch the gateway, the router, the gate, the route handlers or any response shape. Test facts the tests rely on: `count_tokens` is `max(1, len(text) // 4)`, so `"word " * 2400` is 3000 routing tokens and `"hello"` is 1; the test backends always report `prompt_tokens: 10`; the seeded test models are priced $0.05 (cheap tier) and $0.10 (mid tier) per 1M tokens for both input and output, so a 3000-token prompt with `max_tokens: 16` is estimated at $0.0001508 / $0.0003016, over a $0.0001 cap on both tiers, while the same request with a zero-token prompt would be far under it.

This task changes a file on the serving path, so the consumer contract applies (`.claude/skills/consumer-contract-tests/SKILL.md`). Its required checks, and where each is met:

| Required check | Where |
|---|---|
| A test drives the full `/v1/chat/completions` stack (a token, a policy, a recording backend) | `tests/test_openai_compat.py::test_outcome_recording_adds_nothing_to_the_success_wire`, `::test_outcome_recording_adds_nothing_to_the_rejection_wire`; every test in `tests/test_outcome_routing_tokens.py` |
| Wire shape parsed with the real SDK (`ChatCompletion.model_validate`) | `_validate_openai(body)` in the success-wire test |
| What reached the model is asserted on `CALLS[-1]`, not on the trace | the success-wire test compares the recorded messages with the client's |
| A realistic payload, not `"hi"` | `_SUPPORT_TICKET`, a ticket with an order id and dates, about 2500 routing tokens |
| Both directions of the guarantee | success: valid `ChatCompletion`, nothing added; rejection: the B49 envelope with exactly its keys, nothing added |
| No application name in a symbol, test id, fixture or comment | checked: app ids are `rt-budget`, `wire-budget`, … |
| Extend the matching standing file | the two wire tests are added to `tests/test_openai_compat.py` |

The skill's signature gotcha does not apply: no `InferenceBackend` method and no gateway signature changes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outcome_routing_tokens.py`:

````python
"""B60: every outcome row records `routing_tokens` -- the token count the
router was given (`tokens_after`: RouteForge's own post-gate, post-compression
count) -- whether the request was served or rejected, so the offline policy
simulator can replay a rejected request at its real size.

Driven through the real HTTP boundary, like tests/test_outcome_hooks.py: a
real app from `create_app()`, a real `TestClient`, the litellm fakes. The
recorded value is checked against what the router was ACTUALLY called with
(a spy on `Router.select`), never against a re-derivation of it.

`prompt_tokens` is a different number and is untouched: it stays the
backend's billed count (the fakes always report 10), and 0 on a row no
backend answered.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from app.errors import ContextWindowExceeded
from app.gateway.backends import litellm_backend
from app.routing.router import Router
from tests.conftest import v1_error
from tests.onboarding.harness import install_dual_fake, onboard
from tests.test_admin_api import _login
from tests.test_outcome_hooks import (
    DEMO, EMAIL, HELLO, SSN, _client, _columns, _failing_backend, _failing_verify, _only,
    _outcomes,
)

MARKER = "ZZMARKER-b60"
# 12 000 characters -> 3000 routing tokens (`count_tokens` is len // 4): above
# the 2000-token complexity saturation point, so `complexity` alone (1.0)
# cannot tell this request's size -- only `routing_tokens` can.
LONG_PROMPT = "word " * 2400
LONG = {"messages": [{"role": "user", "content": LONG_PROMPT}], "max_tokens": 16}
# Seeded prices are $0.05 (local-8b) and $0.10 (local-32b) per 1M tokens, so a
# 3000-token prompt + 16 output tokens is estimated at $0.00015 / $0.0003:
# over this cap on BOTH tiers. A zero-token prompt would be $0.0000008 /
# $0.0000016 -- under it. The rejection is caused by the prompt's size.
TIGHT_CAP = 0.0001


def _spy_router(monkeypatch) -> list[int]:
    """Record the `tokens_after` every `Router.select` call is given."""
    seen: list[int] = []
    real = Router.select

    def select(self, ctx, tokens_after, *args, **kwargs):
        seen.append(tokens_after)
        return real(self, ctx, tokens_after, *args, **kwargs)

    monkeypatch.setattr(Router, "select", select)
    return seen


# --------------------------------------------------------------------------- #
# R10: the recorded value is the router's input, on every status
# --------------------------------------------------------------------------- #
def test_ok_row_records_the_tokens_the_router_was_given(monkeypatch):
    seen = _spy_router(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.status == "ok"
        assert row.routing_tokens == seen[0] == 3000
        assert row.complexity == 1.0                 # saturated: it hides the size
        assert row.prompt_tokens == r.json()["usage"]["prompt_tokens"] == 10


def test_budget_rejected_row_records_the_tokens_the_router_was_given(monkeypatch):
    """The B60 row itself: rejected for `max_cost_usd_per_request`, because of
    its size. Before the fix nothing on the row said how large it was."""
    seen = _spy_router(monkeypatch)
    with _client() as c:
        app = onboard(c, "rt-budget", max_cost_usd_per_request=TIGHT_CAP)
        r = app.chat(LONG)
        assert r.status_code == 422, r.text
        assert v1_error(r)["code"] == "no_eligible_model"
        row = _only(c)
        assert row.status == "no_eligible"
        assert [q["id"] for q in row.conflicting_core["requirements"]] == \
            ["max_cost_usd_per_request"]
        assert row.routing_tokens == seen[0] == 3000
        assert row.max_tokens == 16
        assert row.prompt_tokens == 0                # never billed: unchanged


def test_backend_failed_row_records_routing_tokens(monkeypatch):
    seen = _spy_router(monkeypatch)
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 502, r.text
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.routing_tokens == seen[0] == 3000
        assert row.prompt_tokens == 0


def test_context_overflow_row_records_routing_tokens(monkeypatch):
    seen = _spy_router(monkeypatch)

    async def _overflow(model, call):
        raise ContextWindowExceeded(f"{model}: maximum context length exceeded")

    monkeypatch.setattr(litellm_backend, "_acompletion", _overflow)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 400, r.text
        row = _only(c)
        assert row.status == "context_overflow"
        assert row.routing_tokens == seen[0] == 3000
        assert row.prompt_tokens == 0


def test_verify_failed_row_records_both_counts(monkeypatch):
    seen = _spy_router(monkeypatch)
    _failing_verify(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "verify_failed"
        assert row.routing_tokens == seen[0] == 3000
        assert row.prompt_tokens == 10               # the served attempt's, from the backend


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify",
                                  "verified_then_stream"])
def test_streamed_row_records_routing_tokens(monkeypatch, mode):
    seen = _spy_router(monkeypatch)
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, f"rt-stream-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**LONG, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
        assert row.status == "ok"
        assert row.routing_tokens == seen[0] == 3000


def test_tool_conversation_row_counts_what_the_router_priced(monkeypatch):
    """A tool-bearing request is priced on its messages PLUS its tool schemas
    (`app/pipeline.py`); the recorded count is that same number."""
    seen = _spy_router(monkeypatch)
    tools = [{"type": "function", "function": {
        "name": "read_file", "description": "Read a file from the workspace",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}}]
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={**LONG, "tools": tools})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.routing_tokens == seen[0]
        assert row.routing_tokens > 3000             # the schemas are counted too


def test_semantic_cache_hit_row_has_null_routing_tokens(monkeypatch):
    """A cache hit returns before compression: no router ran, so there is no
    count to record -- null, like `complexity` on the same row."""
    monkeypatch.setenv("SP_SEMANTIC_CACHE", "on")
    with _client() as c:
        first = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        second = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert first.status_code == 200 and second.status_code == 200
        rows = {r.request_id: r for r in _outcomes(c)}
        miss, hit = rows[first.json()["id"]], rows[second.json()["id"]]
        assert miss.routing_tokens == 1              # "hello": max(1, 5 // 4)
        assert hit.cache_hit is True
        assert hit.routing_tokens is None and hit.complexity is None


# --------------------------------------------------------------------------- #
# R1: still content-free -- a count, on a rejected request carrying PII
# --------------------------------------------------------------------------- #
def test_r1_rejected_row_with_pii_carries_a_count_and_no_content():
    with _client() as c:
        app = onboard(c, "rt-pii", max_cost_usd_per_request=TIGHT_CAP)
        r = app.chat({"max_tokens": 16, "messages": [{"role": "user", "content": (
            f"{MARKER} my SSN is {SSN}, email {EMAIL}. " + LONG_PROMPT)}]})
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "no_eligible"
        assert type(row.routing_tokens) is int and row.routing_tokens > 3000
        dumped = json.dumps(_columns(row), default=str)
        for secret in (MARKER, SSN, EMAIL, "my SSN", "word word"):
            assert secret not in dumped
        # The entity TYPES are the content-free signal that remains.
        assert "US_SSN" in row.detected_entity_types


# --------------------------------------------------------------------------- #
# the export carries the field (no exporter change: it dumps every column)
# --------------------------------------------------------------------------- #
def test_export_line_carries_routing_tokens():
    with _client() as c:
        app = onboard(c, "rt-export", max_cost_usd_per_request=TIGHT_CAP)
        assert app.chat(LONG).status_code == 422
        assert c.post("/v1/chat/completions", headers=DEMO, json=HELLO).status_code == 200
        _login(c)
        r = c.get("/admin/outcomes/export")
        assert r.status_code == 200, r.text
        by_status = {x["outcome"]["status"]: x["outcome"]
                     for x in map(json.loads, r.text.strip().splitlines())}
        assert by_status["no_eligible"]["routing_tokens"] == 3000
        assert by_status["no_eligible"]["prompt_tokens"] == 0
        assert by_status["ok"]["routing_tokens"] == 1


# --------------------------------------------------------------------------- #
# R13: an unmigrated table never breaks serving; the documented ALTER repairs it
# --------------------------------------------------------------------------- #
def test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it(
        tmp_path, monkeypatch, caplog):
    """`create_all` never adds a column to an existing table, so a deployed
    database needs the ALTER in docs/reference/routing-outcomes.md. Against a
    table that lacks the column the app still BOOTS and still SERVES; every
    outcome write fails and is swallowed, so no row is recorded. After the
    documented statement, rows are recorded again, with the new column.

    A real file database and three real boots: the upgraded image against an
    old table is a fresh process, not a table altered under a running one."""
    caplog.set_level(logging.WARNING, logger="serving.obs")
    path = tmp_path / "routeforge.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")

    def _db(statement: str) -> list:
        con = sqlite3.connect(path)
        try:
            rows = con.execute(statement).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    with _client() as c:                         # boot 1: create_all builds the table
        assert c.post("/v1/chat/completions", headers=DEMO, json=HELLO).status_code == 200
    # Put the table in the shape a deployed database has before the upgrade.
    _db("DELETE FROM routing_outcomes")
    _db("ALTER TABLE routing_outcomes DROP COLUMN routing_tokens")

    with _client() as c:                         # boot 2: this code, the old table
        assert c.get("/health").status_code == 200                # it boots
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text                       # it serves
    assert _db("SELECT COUNT(*) FROM routing_outcomes") == [(0,)]  # nothing recorded
    assert any("outcome record failed" in rec.getMessage() for rec in caplog.records)

    # The SQLite form of the documented statement, verbatim.
    _db("ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER")
    with pytest.raises(sqlite3.OperationalError, match="duplicate column name"):
        _db("ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER")

    with _client() as c:                         # boot 3: migrated
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.request_id == r.json()["id"] and row.routing_tokens == 1
````

Patch `tests/test_outcome_ctx_pipeline.py`:

````diff
diff --git a/tests/test_outcome_ctx_pipeline.py b/tests/test_outcome_ctx_pipeline.py
index d754f88..fd1c791 100644
--- a/tests/test_outcome_ctx_pipeline.py
+++ b/tests/test_outcome_ctx_pipeline.py
@@ -67,8 +67,12 @@ def test_outcome_ctx_populated_on_success():
     assert oc.keys() == {
         "sensitivity", "risk_class", "onprem_only",
         "detected_entity_types", "app_policy_version", "complexity",
-        "requested_model", "max_tokens"}
+        "requested_model", "max_tokens", "routing_tokens"}
     assert isinstance(oc["complexity"], float)
+    # B60: the count the router was given, set on the same line as the
+    # complexity derived from it.
+    assert type(oc["routing_tokens"]) is int and oc["routing_tokens"] > 0
+    assert oc["complexity"] == min(1.0, oc["routing_tokens"] / 2000)
 
 
 def test_outcome_ctx_survives_no_eligible_candidate_at_routing():
@@ -88,6 +92,8 @@ def test_outcome_ctx_survives_no_eligible_candidate_at_routing():
     # The failure happens in the gateway/router, strictly after complexity is
     # computed in run() -- so it must already be on the dict.
     assert "complexity" in oc
+    # B60: and so is the token count the router rejected the request on.
+    assert oc["routing_tokens"] == 1      # "hello": max(1, 5 // 4)
 
 
 def test_outcome_ctx_has_no_prompt_text_even_with_ssn():
````

Patch `tests/test_openai_compat.py`:

````diff
diff --git a/tests/test_openai_compat.py b/tests/test_openai_compat.py
index c5047c8..d28f1dc 100644
--- a/tests/test_openai_compat.py
+++ b/tests/test_openai_compat.py
@@ -368,3 +368,50 @@ def test_request_validation_error_does_not_echo_the_rejected_value(client) -> No
                           "max_tokens": "supersecretvalue"})
     assert r.status_code == 422
     assert "supersecretvalue" not in r.text
+
+
+# --- B60: recording the routing token count adds nothing to the wire ----------
+
+_SUPPORT_TICKET = (
+    "Ticket 48213 (priority: high). Customer reports that order ORD-2026-0917 "
+    "shipped to the wrong depot on 2026-09-30; the carrier scan shows it at "
+    "the Leeds hub. Summarise the ticket in two sentences and propose the "
+    "next action for the support agent. ") * 40      # ~2500 routing tokens
+
+
+def test_outcome_recording_adds_nothing_to_the_success_wire(client, monkeypatch):
+    """Serving-path change (B60: `app/pipeline.py` now publishes
+    `routing_tokens` for the outcome row). The consumer contract: the
+    response is still a valid OpenAI `ChatCompletion`, the new field appears
+    nowhere in it, and the model was sent exactly the client's message."""
+    monkeypatch.setattr(litellm_backend, "acompletion",
+                        make_recording_acompletion(make_response(content="Two sentences.")))
+    r = client.post("/v1/chat/completions", headers=_AUTH, json={
+        "messages": [{"role": "user", "content": _SUPPORT_TICKET}], "max_tokens": 64})
+    assert r.status_code == 200, r.text
+    body = r.json()
+    _validate_openai(body)
+    assert body["choices"][0]["message"]["content"] == "Two sentences."
+    assert "routing_tokens" not in r.text
+    wire = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
+    assert wire == _SUPPORT_TICKET                   # nothing redacted, nothing added
+
+
+def test_outcome_recording_adds_nothing_to_the_rejection_wire() -> None:
+    """The other direction: a request rejected for its size (per-request
+    budget) still returns the B49 envelope with exactly its existing keys --
+    the count that the outcome row now records is not echoed to the client."""
+    from tests.onboarding.harness import conformance_client, onboard
+
+    with conformance_client() as c:
+        app = onboard(c, "wire-budget", max_cost_usd_per_request=0.00001)
+        r = app.chat({"messages": [{"role": "user", "content": _SUPPORT_TICKET}],
+                      "max_tokens": 64})
+    assert r.status_code == 422, r.text
+    assert set(r.json()) == {"error"}
+    err = v1_error(r)
+    assert set(err) == {"message", "type", "param", "code", "conflicting_core"}
+    assert err["type"] == "invalid_request_error" and err["param"] is None
+    assert err["code"] == "no_eligible_model"
+    assert set(err["conflicting_core"]) == {"requirements", "state", "candidates_considered"}
+    assert "routing_tokens" not in r.text
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_outcome_routing_tokens.py tests/test_outcome_ctx_pipeline.py tests/test_openai_compat.py -p no:warnings -q -rf
```

Expected: 15 failed, 17 passed.

- Thirteen in `tests/test_outcome_routing_tokens.py`, which is every test in it: the row's `routing_tokens` is still null (`assert None == 3000`, `assert None == 1`, `assert (<class 'NoneType'> is int)`).
- Two in `tests/test_outcome_ctx_pipeline.py`: `test_outcome_ctx_populated_on_success` (the key set differs) and `test_outcome_ctx_survives_no_eligible_candidate_at_routing` (`KeyError: 'routing_tokens'`).
- The two new tests in `tests/test_openai_compat.py` pass already. They are guards: they pin the wire this task must not change, so they pass before and after.

- [ ] **Step 3: Implement**

Patch `app/pipeline.py`:

````diff
diff --git a/app/pipeline.py b/app/pipeline.py
index 34ff3bb..c791411 100644
--- a/app/pipeline.py
+++ b/app/pipeline.py
@@ -917,6 +917,11 @@ class Pipeline:
         complexity = min(1.0, comp.tokens_after / 2000)
         if internals is not None:
             internals["outcome_ctx"]["complexity"] = complexity
+            # B60: the count the router is about to be given, recorded on
+            # every outcome row -- a rejected one included -- so the policy
+            # simulator replays the request at its real size. A count, never
+            # text (app/apps/outcomes.py's content-free contract).
+            internals["outcome_ctx"]["routing_tokens"] = comp.tokens_after
 
         hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                 if hasattr(request, k)}
@@ -1252,6 +1257,11 @@ class Pipeline:
         complexity = min(1.0, comp.tokens_after / 2000)
         if internals is not None:
             internals["outcome_ctx"]["complexity"] = complexity
+            # B60: the count the router is about to be given, recorded on
+            # every outcome row -- a rejected one included -- so the policy
+            # simulator replays the request at its real size. A count, never
+            # text (app/apps/outcomes.py's content-free contract).
+            internals["outcome_ctx"]["routing_tokens"] = comp.tokens_after
         hint = {k: getattr(request, k) for k in ("mock_confidence", "mock_fail")
                 if hasattr(request, k)}
         stream, records = await self._gateway.run_stream(
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_outcome_routing_tokens.py tests/test_outcome_ctx_pipeline.py tests/test_openai_compat.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

Then the serving tests this task touches, and isolation (R7):

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_outcome_hooks.py tests/test_outcome_pin.py tests/test_outcome_export.py tests/test_outcome_builder.py tests/test_outcome_store.py tests/test_conflicting_core_api.py tests/test_run_stream_pipeline.py tests/test_pii_entity_filter.py tests/eval/test_isolation.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_outcome_routing_tokens.py tests/test_outcome_ctx_pipeline.py tests/test_openai_compat.py app/pipeline.py
git commit -m "$(cat <<'MSG'
feat(outcomes): record the routing token count on every outcome row (B60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 3: Replay routes on `routing_tokens`; unsized rows are excluded (spec §5, §6, R2, R3, R4, R6, R9)

**Files:**
- Modify: `app/eval/simulate.py` (`EXCLUSION_REASONS` line 56; `_has_features` line 113; `select_rows` line 164; `scaled_request` line 234; `simulate_row` line 259; `summarize` line 723)
- Create: `tests/eval/test_replay_rejections.py`
- Test: `tests/eval/test_simulate.py`, `tests/eval/test_proposal_workload.py`

**Interfaces:**
- Consumes:
  - Export rows as dicts. Task 2 added the optional key `routing_tokens` (int ≥ 0, or null, or absent on an export from before it).
  - Existing in `app/eval/simulate.py`: `select_rows(records, app_ids, *, since=None) -> tuple[list[dict], dict[str, int]]`; `scaled_request(prompt_tokens: int, complexity: float, scale: float) -> tuple[int, float, bool]`; `simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmResult`; `simulate(records, snapshot, proposal, *, base, since=None) -> tuple[list[RowResult], dict[str, int]]`; `summarize(results, excluded, *, inputs, edits, assumptions=None) -> dict`; `ArmResult` (fields `outcome`, `model`, `tier`, `reject_codes`, `cost_usd`, `core`, `overflow_hops`, …); `RowResult(row, current, proposed)`.
  - `expected_output_tokens(settings, params) -> int` and `price_call(price, *, prompt_tokens, completion_tokens, cached_prompt_tokens=0) -> float`, both already imported by `simulate.py`.
  - `app/eval/outcomes.py::load_export(path) -> tuple[list[dict], list[str]]`; `app/eval/shadow.py::shadow_prices(records, baseline_doc, *, base, cap=50, baseline_label="snapshot", since=None) -> dict`; `tests/eval/test_shadow.py::_entry(block, requirement, app=None, endpoint=None)`.
- Produces:
  - `EXCLUSION_REASONS` with `"missing_routing_tokens"` appended last.
  - `_unsized(row: dict) -> bool` — `row["status"] != "ok" and row.get("routing_tokens") is None and row["prompt_tokens"] == 0`. Call only after `_has_features`.
  - `_scale_tokens(tokens: int, scale: float) -> int` — `max(1, round(tokens * scale))`; returns `tokens` unchanged when `scale == 1.0` or `tokens <= 0`.
  - `replay_tokens(row: dict) -> tuple[int, int, bool]` — `(routing_tokens, prompt_tokens, estimated)`. Tasks 4 and 5 do not call it; `summarize` does.
  - `summarize()` output: `rows` gains `"estimated_tokens": int`.
  - `simulate_row` gives `Router.select` the routing count, prices and sizes at the prompt count, and prices an estimated row's completion at the output estimate.

**Rules.**

| Row | routing tokens (to `Router.select`) | prompt tokens (price, window check) | completion tokens (price) | `estimated` |
|---|---|---|---|---|
| `routing_tokens` recorded, `prompt_tokens > 0` | `routing_tokens` | `prompt_tokens` | recorded `completion_tokens` | no |
| `routing_tokens` recorded, `prompt_tokens == 0` | `routing_tokens` | `routing_tokens` | `expected_output_tokens(...)` for the row | yes |
| no `routing_tokens`, `prompt_tokens > 0` | `prompt_tokens` | `prompt_tokens` | recorded `completion_tokens` | no |
| no `routing_tokens`, `prompt_tokens == 0`, `status == "ok"` | `0` | `0` | recorded `completion_tokens` | no |
| no `routing_tokens`, `prompt_tokens == 0`, `status != "ok"` | excluded: `missing_routing_tokens` | | | |

Both counts are scaled by `workload.token_scale` with `_scale_tokens`. `scaled_request` is called with the routing count, so the recorded `complexity` is replayed and scaled exactly as before. A mistyped `routing_tokens` is `missing_features`. The fixture pool's prices, per 1M tokens (input / output): `local-cheap` 0.1 / 0.2, `local-mid` 0.5 / 1.0, `local-big` 2.0 / 6.0.

Two existing expectations change, and only these two: `tests/eval/test_simulate.py::test_select_rows_exclusions` now also expects the new reason (the test gains one unsized row); `tests/eval/test_proposal_workload.py::_mixed_rows`'s zero-token row becomes a served row (Deviation 11).

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_replay_rejections.py`:

````python
"""B60: replay sizes a row from the token count the router was given.

A rejected outcome row records `prompt_tokens: 0` (no backend billed it).
Before `routing_tokens` existed the simulator replayed such a row as an
EMPTY prompt, which fits any per-request budget and any context window, so
its `current` arm ROUTED a request the record says was rejected -- and a
proposal that raised the budget showed nothing to rescue.

Invariants pinned here (spec §9): R2, R3, R4, R6, R9.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

import app.eval.simulate as simulate_mod
from app.eval.outcomes import load_export
from app.eval.simulate import (EXCLUSION_REASONS, replay_tokens, select_rows, simulate,
                               summarize)
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"
_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": None, "routing_policy_version": "r", "registry_version": "v",
           "snapshot_generated_at": "2026-09-24T00:00:00Z"}

# The default fixture pool, USD per 1M tokens (input / output):
#   local-cheap 0.1 / 0.2 · local-mid 0.5 / 1.0 · local-big 2.0 / 6.0
# A 3000-token prompt with a 16-token output cap is estimated at
#   cheap 0.0003032 · mid 0.001516 · frontier 0.006096
# so a 0.0002 cap rejects it on every tier. The SAME request with a zero-token
# prompt is estimated at 0.000096 on the frontier tier -- under the cap. That
# is B60: without its size, the row routes.
CAP = 0.0002
FRONTIER_ESTIMATE = (3000 * 2.0 + 16 * 6.0) / 1_000_000


def _snap(cap=CAP, **kw):
    return make_snapshot(apps=[app_entry("app-a", max_cost_usd_per_request=cap)], **kw)


def _rejected_row(request_id="rej", **over):
    """A row as serving writes it for a request rejected at the per-request
    budget: no backend usage, no attempts, the router's token count."""
    row = dict(status="no_eligible", complexity=1.0, prompt_tokens=0, completion_tokens=0,
               routing_tokens=3000, max_tokens=16, attempts=[], chosen=None, latency_ms=3.0)
    row.update(over)
    return make_row(request_id, "app-a", **row)


def _without_size(row):
    """The same row as an export from before the column existed carries it."""
    return {k: v for k, v in row.items() if k != "routing_tokens"}


def _run(records, snap, prop=None, **kw):
    return asyncio.run(simulate(records, snap, prop or {"schema": P}, base=BASE_SETTINGS, **kw))


def _raise_cap(cap=1.0):
    return {"schema": P, "apps": {"app-a": {"max_cost_usd_per_request": cap}}}


# --- R3 / R4: the rejection is reproduced, and a higher cap rescues it --------

def test_r3_budget_rejected_row_is_rejected_in_the_current_arm():          # R3
    (r,), excluded = _run([_rejected_row()], _snap())
    assert r.current.outcome == "rejected"
    assert r.current.reject_codes == ("over_budget",)
    assert r.current.core == ("max_cost_usd_per_request",)
    assert r.current.cost_usd is None
    assert excluded["missing_routing_tokens"] == 0


def test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate():   # R4
    results, excluded = _run([_rejected_row()], _snap(), _raise_cap())
    (r,) = results
    assert (r.current.outcome, r.proposed.outcome) == ("rejected", "routed")
    assert r.proposed.model == "local-big"                    # complexity 1.0 -> frontier
    # Priced at the SAME pair the live budget check priced it at: the routing
    # tokens and the client's own output cap. Never at the prompt alone.
    assert r.proposed.cost_usd == pytest.approx(FRONTIER_ESTIMATE)
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_served"] == 1
    assert sim["totals"]["newly_rejected"] == 0
    assert sim["affected_apps"] == ["app-a"]
    assert sim["rows"]["estimated_tokens"] == 1


def test_a_cap_still_below_the_estimate_serves_nothing():
    """The cap that matters is the cheapest tier's estimate (the budget
    fallback steps down to it): just under it still rejects, just over
    serves -- on the cheap tier."""
    cheap = (3000 * 0.1 + 16 * 0.2) / 1_000_000
    (under,), _ = _run([_rejected_row()], _snap(), _raise_cap(cheap - 1e-9))
    (over,), _ = _run([_rejected_row()], _snap(), _raise_cap(cheap + 1e-9))
    assert under.proposed.outcome == "rejected"
    assert (over.proposed.outcome, over.proposed.model) == ("routed", "local-cheap")
    assert over.proposed.cost_usd == pytest.approx(cheap)


def test_no_recorded_cap_prices_the_rescued_row_at_the_default_output_estimate():
    default_out = int(BASE_SETTINGS.policy.budgets["default_estimated_output_tokens"])
    (r,), _ = _run([_rejected_row(max_tokens=None)], _snap(cap=1e-9), _raise_cap(100.0))
    assert r.current.outcome == "rejected"
    assert r.proposed.cost_usd == pytest.approx((3000 * 2.0 + default_out * 6.0) / 1_000_000)


def test_token_scale_reaches_the_budget_check_of_a_rejected_row():
    """Halving the prompt (a stricter redaction, Headroom on) brings the
    cheap tier's estimate under the cap: 1500 tokens -> 0.0001532."""
    (r,), _ = _run([_rejected_row()], _snap(),
                   {"schema": P, "workload": {"token_scale": 0.5}})
    assert (r.current.outcome, r.proposed.outcome) == ("rejected", "routed")
    assert r.proposed.model == "local-cheap"
    assert r.proposed.cost_usd == pytest.approx((1500 * 0.1 + 16 * 0.2) / 1_000_000)


def test_context_overflow_row_is_reproduced_when_the_window_is_declared():
    """A recorded `context_overflow` row replays at its routing tokens, so a
    DECLARED window it exceeds rejects it; an undeclared one cannot."""
    row = _rejected_row("ovf", status="context_overflow", attempts=["m"], complexity=1.0)
    declared = make_snapshot(onboarded=[onboarded_row("m", "frontier", context_window=2048)])
    undeclared = make_snapshot(onboarded=[onboarded_row("m", "frontier")])
    (a,), _ = _run([row], declared)
    (b,), _ = _run([row], undeclared)
    assert (a.current.outcome, a.current.reject_codes) == ("rejected", ("context_window",))
    assert b.current.outcome == "routed"


# --- the two counts: routed on one, priced and sized on the other -------------

def test_replay_tokens():
    served = make_row("s", "app-a", prompt_tokens=10, routing_tokens=3000)
    assert replay_tokens(served) == (3000, 10, False)          # route / bill / not estimated
    assert replay_tokens(_without_size(served)) == (10, 10, False)        # as before B60
    assert replay_tokens(_rejected_row()) == (3000, 3000, True)
    assert replay_tokens(make_row("z", "app-a", prompt_tokens=0)) == (0, 0, False)
    assert replay_tokens(make_row("n", "app-a", routing_tokens=None)) == (200, 200, False)


def test_served_row_is_routed_on_routing_tokens_and_priced_on_the_backend_count():
    """The backend billed 10 tokens; the router was given 3000. Under a cap
    of 0.002 the frontier estimate (0.006096) is over budget and the mid one
    (0.001516) fits, so the router falls back to mid -- as it did live. The
    10-token count alone would have stayed on frontier. The cost is the bill:
    10 prompt + 100 completion tokens at mid prices."""
    row = make_row("s", "app-a", complexity=1.0, prompt_tokens=10, completion_tokens=100,
                   routing_tokens=3000, max_tokens=16,
                   attempts=["local-mid"], chosen="local-mid")
    (new,), _ = _run([row], _snap(cap=0.002))
    (old,), _ = _run([_without_size(row)], _snap(cap=0.002))
    assert new.current.model == "local-mid"
    assert new.current.cost_usd == pytest.approx((10 * 0.5 + 100 * 1.0) / 1_000_000)
    assert old.current.model == "local-big"                    # what replay did before


def test_window_check_uses_the_backend_count_for_a_served_row():
    """Live, overflow is the backend's refusal on its own tokenisation, so a
    served row's window check stays on `prompt_tokens`: 900 + 16 fits a
    1000-token window although the routing count (3000) would not."""
    snap = make_snapshot(onboarded=[onboarded_row("m", "frontier", context_window=1000)])
    row = make_row("s", "app-a", complexity=1.0, prompt_tokens=900, routing_tokens=3000,
                   max_tokens=16, attempts=["m"], chosen="m")
    (r,), _ = _run([row], snap)
    assert (r.current.outcome, r.current.overflow_hops) == ("routed", 0)


# --- R2: a served row is unchanged when the two counts coincide ---------------

@hsettings(max_examples=60, deadline=None,
           suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(tokens=st.integers(min_value=1, max_value=20_000),
       complexity=st.sampled_from([0.0, 0.05, 0.3, 0.6, 0.99, 1.0]),
       max_tokens=st.sampled_from([None, 16, 4096]),
       scale=st.sampled_from([0.1, 0.5, 1.0, 2.0]),
       cap=st.sampled_from([0.0002, 0.002, 100.0]),
       status=st.sampled_from(["ok", "verify_failed"]))
def test_r2_served_row_replay_is_unchanged_when_the_counts_coincide(      # R2
        tokens, complexity, max_tokens, scale, cap, status):
    """`routing_tokens == prompt_tokens` must reproduce, field for field,
    the `ArmResult` the row produced before the column existed -- in both
    arms, under any `token_scale`."""
    legacy = make_row("r", "app-a", status=status, complexity=complexity,
                      prompt_tokens=tokens, max_tokens=max_tokens)
    recorded = {**legacy, "routing_tokens": tokens}
    prop = {"schema": P, "workload": {"token_scale": scale}}
    (a,), ex_a = _run([legacy], _snap(cap=cap), prop)
    (b,), ex_b = _run([recorded], _snap(cap=cap), prop)
    assert a.current == b.current and a.proposed == b.proposed
    assert ex_a == ex_b


# --- R6: a row with no size is excluded and counted, never routed -------------

@pytest.mark.parametrize("status", ["no_eligible", "context_overflow", "backend_failed",
                                    "verify_failed"])
def test_r6_unsized_rows_are_excluded_and_counted_never_routed(status, monkeypatch):  # R6
    simulated: list[str] = []
    real = simulate_mod.simulate_row

    def spy(scn, row, observed):
        simulated.append(row["request_id"])
        return real(scn, row, observed)

    monkeypatch.setattr(simulate_mod, "simulate_row", spy)
    unsized = _without_size(_rejected_row("old", status=status))
    results, excluded = _run([unsized, make_row("ok1", "app-a")], _snap(), _raise_cap())
    assert [r.row["request_id"] for r in results] == ["ok1"]
    assert excluded["missing_routing_tokens"] == 1
    assert "old" not in simulated                              # it reaches no arm at all
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_served"] == 0
    assert sim["rows"]["excluded"]["missing_routing_tokens"] == 1


def test_missing_routing_tokens_is_the_last_exclusion_reason():
    """First match wins, in `EXCLUSION_REASONS` order: an unsized row that is
    ALSO before `--since`, of an unknown app or malformed is counted there."""
    assert EXCLUSION_REASONS[-1] == "missing_routing_tokens"
    unsized = _without_size(_rejected_row("u"))
    rows, excluded = select_rows(
        [unsized, {**unsized, "request_id": "ghost", "application_id": "ghost"},
         {**unsized, "request_id": "bad", "complexity": None}], {"app-a"})
    assert rows == []
    assert (excluded["missing_routing_tokens"], excluded["unknown_app"],
            excluded["missing_features"]) == (1, 1, 1)


def test_rows_that_still_have_a_size_are_kept():
    """Not every row without `routing_tokens` is unsized: a served row, and a
    `verify_failed` row carrying its served attempt's backend count, replay
    exactly as before. A null value is the same as an absent key."""
    kept = [make_row("ok-old", "app-a"),
            make_row("ok-zero", "app-a", prompt_tokens=0),
            make_row("vf-old", "app-a", status="verify_failed", prompt_tokens=1500),
            make_row("ok-null", "app-a", routing_tokens=None),
            _rejected_row("rej-new"),
            _rejected_row("rej-empty", routing_tokens=0)]      # recorded, and really 0
    rows, excluded = select_rows(kept, {"app-a"})
    assert {r["request_id"] for r in rows} == {r["request_id"] for r in kept}
    assert excluded["missing_routing_tokens"] == 0
    null_rejected = _rejected_row("rej-null", routing_tokens=None)
    assert select_rows([null_rejected], {"app-a"})[1]["missing_routing_tokens"] == 1


@pytest.mark.parametrize("bad", ["3000", 3000.0, True, -1, [3000], {"n": 1}])
def test_mistyped_routing_tokens_is_missing_features(bad):
    """A present-but-mistyped count is a malformed row like any other --
    counted, never a crash, and never guessed at."""
    for row in (_rejected_row(routing_tokens=bad), make_row("s", "app-a", routing_tokens=bad)):
        rows, excluded = select_rows([row], {"app-a"})
        assert rows == []
        assert (excluded["missing_features"], excluded["missing_routing_tokens"]) == (1, 0)


# --- R9: an export from before the field existed still loads and replays ------

def test_r9_export_without_the_field_still_loads_and_replays_served_rows(tmp_path):  # R9
    served = make_row("s1", "app-a", prompt_tokens=200)
    old_rejected = _without_size(_rejected_row("r1"))
    new_rejected = _rejected_row("r2")
    path = tmp_path / "old.ndjson"
    with path.open("w") as f:
        for row in (served, old_rejected, new_rejected):
            outcome = {k: v for k, v in row.items() if k != "feedback"}
            f.write(json.dumps({"schema": "routeforge.outcome/v1", "outcome": outcome,
                                "feedback": []}) + "\n")
    records, warnings = load_export(str(path))
    assert warnings == [] and len(records) == 3
    assert "routing_tokens" not in records[0] and records[2]["routing_tokens"] == 3000

    results, excluded = _run(records, _snap())
    by = {r.row["request_id"]: r for r in results}
    assert set(by) == {"s1", "r2"} and excluded["missing_routing_tokens"] == 1
    # The served row replays exactly as a row that says its routing count is
    # its backend count -- i.e. exactly as it did before the field existed.
    (same,), _ = _run([{**served, "routing_tokens": 200}], _snap())
    assert by["s1"].current == same.current
    assert by["r2"].current.outcome == "rejected"


# --- shadow prices gain the fix through the same row path ---------------------

def test_shadow_price_of_the_budget_now_shows_the_rejections_it_would_avoid():
    from app.eval.shadow import shadow_prices
    from tests.eval.test_shadow import _entry

    rows = [_rejected_row(f"rej{i}", ts=f"2026-09-20T12:00:0{i}+00:00") for i in range(3)]
    rows.append(make_row("ok1", "app-a"))
    block = asyncio.run(shadow_prices(rows, _snap(), base=BASE_SETTINGS))
    entry = _entry(block, "max_cost_usd_per_request", app="app-a")
    assert entry is not None and entry["rejections_avoided"] == 3
    legacy = [_without_size(r) for r in rows[:3]] + rows[3:]
    assert _entry(asyncio.run(shadow_prices(legacy, _snap(), base=BASE_SETTINGS)),
                  "max_cost_usd_per_request", app="app-a") is None
````

Patch `tests/eval/test_simulate.py`:

````diff
diff --git a/tests/eval/test_simulate.py b/tests/eval/test_simulate.py
index 5511a00..2225743 100644
--- a/tests/eval/test_simulate.py
+++ b/tests/eval/test_simulate.py
@@ -91,11 +91,14 @@ def test_sensitivity_rule_replays_recorded_entity_types():                 # §9
 def test_select_rows_exclusions():
     recs = [make_row("a", "app-a"), make_row("b", "app-a", cache_hit=True),
             make_row("c", "app-a", status="weird"), make_row("d", "ghost"),
-            make_row("e", "app-a", complexity=None)]
+            make_row("e", "app-a", complexity=None),
+            # B60: a rejected row from before `routing_tokens` was recorded
+            make_row("f", "app-a", status="no_eligible", prompt_tokens=0)]
     rows, excluded = select_rows(recs, {"app-a"})
     assert [r["request_id"] for r in rows] == ["a"]
     assert excluded == {"before_since": 0, "cache_hit": 1, "unknown_status": 1,
-                        "unknown_app": 1, "missing_features": 1}
+                        "unknown_app": 1, "missing_features": 1,
+                        "missing_routing_tokens": 1}
 
 
 def test_observed_latency_median_single_attempt_ok_only():
````

Patch `tests/eval/test_proposal_workload.py`:

````diff
diff --git a/tests/eval/test_proposal_workload.py b/tests/eval/test_proposal_workload.py
index 9e79083..14f0896 100644
--- a/tests/eval/test_proposal_workload.py
+++ b/tests/eval/test_proposal_workload.py
@@ -164,13 +164,15 @@ def _run(records, snap, prop):
 
 
 def _mixed_rows():
-    """Small, mid and saturated complexity; a failure row that recorded no
-    prompt tokens; a long prompt."""
+    """Small, mid and saturated complexity; a served row whose backend
+    reported no prompt tokens; a long prompt. (The zero-token row used to be
+    a `backend_failed` row. Since B60 a failure row with no recorded size is
+    excluded as `missing_routing_tokens`, so the zero-token case is carried
+    by a served row -- the only kind still replayed at zero.)"""
     return [make_row("small", "app-a", complexity=0.1, prompt_tokens=200),
             make_row("mid", "app-a", complexity=0.6, prompt_tokens=1200),
             make_row("sat", "app-a", complexity=1.0, prompt_tokens=3000),
-            make_row("zero", "app-a", complexity=0.2, prompt_tokens=0,
-                     status="backend_failed"),
+            make_row("zero", "app-a", complexity=0.2, prompt_tokens=0),
             make_row("long", "app-a", complexity=1.0, prompt_tokens=9000)]
 
 
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_rejections.py tests/eval/test_simulate.py tests/eval/test_proposal_workload.py -p no:warnings -q -rf
```

Expected: a collection error, exit code 2 — `ImportError: cannot import name 'replay_tokens' from 'app.eval.simulate'` while importing `tests/eval/test_replay_rejections.py`. pytest stops there, so the other two files do not run yet.

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index e53c829..614f59c 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -52,9 +52,10 @@ SIMULATED_STATUSES = frozenset({"ok", "backend_failed", "verify_failed",
                                 "no_eligible", "context_overflow"})
 
 # `before_since` is checked first, so a row older than `--since` never reaches
-# the other checks (P3c-2 spec §6).
+# the other checks (P3c-2 spec §6). `missing_routing_tokens` (B60) is checked
+# last: only a well-formed row of a known app can be "well-formed but unsized".
 EXCLUSION_REASONS = ("before_since", "cache_hit", "unknown_status", "unknown_app",
-                     "missing_features")
+                     "missing_features", "missing_routing_tokens")
 
 
 @dataclass(frozen=True)
@@ -131,9 +132,26 @@ def _has_features(row: dict) -> bool:
                  or (isinstance(row["max_tokens"], int)
                      and not isinstance(row["max_tokens"], bool)
                      and row["max_tokens"] > 0))
+            # B60: absent (an export from before the column existed) or null
+            # (a cache hit) means "not recorded"; a recorded count is an int.
+            and (row.get("routing_tokens") is None
+                 or (isinstance(row["routing_tokens"], int)
+                     and not isinstance(row["routing_tokens"], bool)
+                     and row["routing_tokens"] >= 0))
             and _valid_ts(row.get("ts")))
 
 
+def _unsized(row: dict) -> bool:
+    """True for a well-formed row that records no size at all (B60): it was
+    not served (`status != "ok"`), it has no `routing_tokens`, and its
+    `prompt_tokens` is 0 because no backend answered. That is a rejected or
+    failed row written before `routing_tokens` existed. Replaying it would
+    replay an EMPTY prompt -- which fits any budget and any context window --
+    so it is excluded and counted instead. Call only after `_has_features`."""
+    return (row["status"] != "ok" and row.get("routing_tokens") is None
+            and row["prompt_tokens"] == 0)
+
+
 def parse_since(text: str) -> datetime:
     """A `--since` value as an aware UTC datetime. Accepts what
     `datetime.fromisoformat` accepts plus a trailing `Z`; a value with no
@@ -175,7 +193,10 @@ def select_rows(records: list[dict], app_ids: set[str], *,
     -- but the key must be present), and a mistyped `requested_model`
     (non-empty string or null) or `max_tokens` (int > 0 or null). Those two
     (B55) may be ABSENT: an export from before they were recorded replays
-    unpinned at the default output estimate."""
+    unpinned at the default output estimate. A mistyped `routing_tokens`
+    (B60: int >= 0, null or absent) is `missing_features` too.
+    `missing_routing_tokens` (B60) is a well-formed row that records no size
+    (`_unsized`): never simulated as an empty prompt."""
     excluded = {reason: 0 for reason in EXCLUSION_REASONS}
     kept: list[dict] = []
     for row in records:
@@ -191,6 +212,8 @@ def select_rows(records: list[dict], app_ids: set[str], *,
             excluded["unknown_app"] += 1
         elif not _has_features(row) or (since is not None and row.get("ts") is None):
             excluded["missing_features"] += 1
+        elif _unsized(row):
+            excluded["missing_routing_tokens"] += 1
         else:
             kept.append(row)
     kept.sort(key=lambda r: (r.get("ts") or "", r["request_id"]))
@@ -231,14 +254,50 @@ def _rejected(eligible: tuple[str, ...], codes: tuple[str, ...], *,
 _COMPLEXITY_SATURATION_TOKENS = 2000
 
 
+def _scale_tokens(tokens: int, scale: float) -> int:
+    """A token count under `workload.token_scale`: `max(1, round(tokens *
+    scale))`, the identity at exactly 1.0. A count of 0 stays 0: flooring it
+    to 1 would make cost fall as the scale rises."""
+    if scale == 1.0:
+        return tokens
+    return max(1, round(tokens * scale)) if tokens > 0 else 0
+
+
+def replay_tokens(row: dict) -> tuple[int, int, bool]:
+    """`(routing_tokens, prompt_tokens, estimated)` for one selected row (B60).
+
+    Two different counts, used for two different things:
+
+    - `routing_tokens` is what `Router.select` is given -- the per-request
+      budget check and the cost term of the score. It is the row's recorded
+      `routing_tokens` (RouteForge's own count, the number the live router
+      was given), else the row's `prompt_tokens` as before the column
+      existed.
+    - `prompt_tokens` is what the routed call is priced at and what the
+      context-window check adds the output estimate to. It is the backend's
+      billed count when the row has one (`prompt_tokens > 0`), else the
+      routing count.
+
+    `estimated` is True when the row has a recorded `routing_tokens` but no
+    backend usage: a request that was rejected, or failed before any
+    response. Its replayed size and cost are then RouteForge's estimate, not
+    a bill, and the caller says so."""
+    billed = int(row["prompt_tokens"])
+    recorded = row.get("routing_tokens")
+    routing = billed if recorded is None else int(recorded)
+    if billed > 0:
+        return routing, billed, False
+    return routing, routing, recorded is not None
+
+
 def scaled_request(prompt_tokens: int, complexity: float,
                    scale: float) -> tuple[int, float, bool]:
     """`(prompt_tokens', complexity', estimated)` for one row under
     `workload.token_scale` (P3c-2 spec §4.1).
 
     - `scale == 1.0` is the identity, exactly.
-    - Tokens: `max(1, round(prompt_tokens * scale))`. A row that recorded no
-      prompt tokens (every failure row records 0) stays at 0: its size is
+    - Tokens: `_scale_tokens`. A row that recorded no
+      prompt tokens (a legacy zero-token row) stays at 0: its size is
       unknown, and flooring it to 1 would make cost fall as the scale rises.
     - Complexity below 1 is linear in the token count, so `complexity *
       scale` (capped at 1) is exact.
@@ -249,7 +308,7 @@ def scaled_request(prompt_tokens: int, complexity: float,
       scaling DOWN is an estimate, and `estimated` says so."""
     if scale == 1.0:
         return prompt_tokens, complexity, False
-    scaled = max(1, round(prompt_tokens * scale)) if prompt_tokens > 0 else 0
+    scaled = _scale_tokens(prompt_tokens, scale)
     if complexity < 1.0:
         return scaled, min(1.0, complexity * scale), False
     estimate = max(scale, scaled / _COMPLEXITY_SATURATION_TOKENS)
@@ -279,12 +338,17 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
     eligible = tuple(sorted(
         c.name for c in feasible_set(scn.settings, scn.registry, ctx,
                                      tenant_id=row["application_id"]).eligible))
+    # B60: the count the ROUTER is given and the count the call is PRICED and
+    # sized at are two numbers (`replay_tokens`). On a row from before
+    # `routing_tokens` was recorded they are the same number, as before.
+    routing_base, prompt_base, estimated_tokens = replay_tokens(row)
     # P3c-2: the scenario's workload assumptions, applied before routing and
     # pricing. A scenario built from a plain snapshot has none (scale 1.0,
     # share 0.0), so the `current` arm replays the row as recorded.
-    prompt_tokens, complexity, estimated = scaled_request(
-        int(row["prompt_tokens"]), float(row["complexity"]),
-        scn.assumptions.scale_for(row["application_id"]))
+    scale = scn.assumptions.scale_for(row["application_id"])
+    routing_tokens, complexity, estimated = scaled_request(
+        routing_base, float(row["complexity"]), scale)
+    prompt_tokens = _scale_tokens(prompt_base, scale)
     # B55: price at the client's recorded output cap, as the live gateway
     # does (`expected_output_tokens(settings, params)`); no cap recorded ->
     # the deployment's default estimate.
@@ -300,7 +364,7 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
     overflowed: list[str] = []          # picks that overflowed, in order
     while True:
         try:
-            decision = scn.router.select(ctx, tokens_after=prompt_tokens,
+            decision = scn.router.select(ctx, tokens_after=routing_tokens,
                                          complexity=complexity,
                                          output_tokens=output_tokens,
                                          exclude=frozenset(overflowed))
@@ -341,9 +405,16 @@ def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmRes
     # route. `price_call` bills cached tokens at the input price when the
     # endpoint declares no cached price.
     cached_tokens = round(prompt_tokens * scn.assumptions.cached_for(row["application_id"]))
+    # B60: a row no backend answered has no completion count either. It is
+    # priced at the router's own output estimate -- with `prompt_tokens`
+    # above, the same pair the live per-request budget check priced it at --
+    # so a request a proposal rescues costs its admission estimate, not its
+    # prompt alone.
+    completion_tokens = (output_tokens if estimated_tokens
+                         else int(row.get("completion_tokens") or 0))
     cost = price_call(scn.registry.price_for(pick.name),
                       prompt_tokens=prompt_tokens,
-                      completion_tokens=int(row.get("completion_tokens") or 0),
+                      completion_tokens=completion_tokens,
                       cached_prompt_tokens=cached_tokens)
     asserted = scn.signals.proposed_latency(pick.name)
     if asserted is not None:
@@ -720,7 +791,10 @@ def summarize(results: list[RowResult], excluded: dict[str, int], *, inputs: dic
         "inputs": full_inputs,
         "edits": edits,
         "assumptions": _assumptions_summary(results, assumptions),
-        "rows": {"simulated": len(results), "excluded": dict(excluded)},
+        "rows": {"simulated": len(results), "excluded": dict(excluded),
+                 # B60: rows no backend answered, sized and priced from
+                 # RouteForge's own routing token count -- an estimate.
+                 "estimated_tokens": sum(1 for r in results if replay_tokens(r.row)[2])},
         "baseline": _baseline(results),
         "totals": totals,
         "by_app": {app_id: _bucket_stats([r for r in results
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_rejections.py tests/eval/test_simulate.py tests/eval/test_proposal_workload.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -q -rf
```

Expected: every test passes (0 failed). In particular the three existing live round trips in `tests/eval/test_policy_simulate_cli.py` still report `agreement == 1.0`.

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_replay_rejections.py tests/eval/test_simulate.py tests/eval/test_proposal_workload.py app/eval/simulate.py
git commit -m "$(cat <<'MSG'
fix(eval): replay routes on routing_tokens; unsized rows are excluded, not replayed empty (B60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 4: Outcome-level agreement and the gate (spec §7.1, §7.2, §7.4, R11, R12)

**Files:**
- Modify: `app/eval/simulate.py` (`SIMULATED_STATUSES` line 51; `agreement_ok` line 438; `_baseline` line 571)
- Test: `tests/eval/test_replay_rejections.py`, `tests/eval/test_simulate.py`, `tests/eval/test_scenarios_compare.py`

**Interfaces:**
- Consumes:
  - `RowResult(row, current, proposed)`; `ArmResult.outcome` is `"routed"` or `"rejected"`. Every selected row's `status` is in `SIMULATED_STATUSES`.
  - The existing `_baseline(results: list[RowResult]) -> dict` with keys `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`, `overflow_contradicted`; `baseline_check(records, snapshot, *, base, since=None) -> dict`, which returns `_round6(_baseline(...))`; `agreement_ok(baseline: dict, minimum: float) -> bool`.
  - `app/apps/outcomes.py::STATUSES` (a tuple of the five statuses).
  - Task 3's test helpers in `tests/eval/test_replay_rejections.py`: `_snap(cap=CAP, **kw)`, `_rejected_row(request_id="rej", **over)`, `_run(records, snap, prop=None, **kw)`, `_INPUTS`, `P`.
- Produces:
  - `RECORDED_OUTCOME: dict[str, str]` — status → `"routed"` | `"rejected"`.
  - `_outcome_stats(rows: list[RowResult]) -> dict` with keys `rows`, `agreement` (`None` when `rows == 0`), `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected`.
  - `_baseline()` output gains `outcome_rows_compared`, `outcome_agreement`, `outcome_warning`, `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected`, `outcome_by_app` (app id → an `_outcome_stats` dict). The six existing keys are unchanged.
  - `gate_agreement(baseline: dict) -> float | None` — `min(agreement, outcome_agreement)`, or `None` when either is `None`.
  - `agreement_ok(baseline, minimum)` — `gate_agreement(baseline)` is not `None` and `>= minimum`. It now reads `baseline["outcome_agreement"]`, so a hand-built baseline dict must carry that key.

**Rules.** Outcome agreement is over every simulated row; model agreement stays over `ok` rows with a recorded attempt. `outcome_by_app` has an entry only for an app with at least one simulated row, so its `agreement` is never `None`. Two existing expectations change: `test_agreement_ok` passes a baseline with `outcome_agreement: 1.0`, and `test_scenarios_come_back_in_file_order_with_shared_rows_and_baseline` pins the new key set.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_replay_rejections.py`:

````diff
diff --git a/tests/eval/test_replay_rejections.py b/tests/eval/test_replay_rejections.py
index 3eef244..b1ef226 100644
--- a/tests/eval/test_replay_rejections.py
+++ b/tests/eval/test_replay_rejections.py
@@ -6,7 +6,7 @@ EMPTY prompt, which fits any per-request budget and any context window, so
 its `current` arm ROUTED a request the record says was rejected -- and a
 proposal that raised the budget showed nothing to rescue.
 
-Invariants pinned here (spec §9): R2, R3, R4, R6, R9.
+Invariants pinned here (spec §9): R2, R3, R4, R6, R9, R11, R12.
 """
 from __future__ import annotations
 
@@ -18,8 +18,9 @@ from hypothesis import HealthCheck, given, settings as hsettings, strategies as
 
 import app.eval.simulate as simulate_mod
 from app.eval.outcomes import load_export
-from app.eval.simulate import (EXCLUSION_REASONS, replay_tokens, select_rows, simulate,
-                               summarize)
+from app.eval.simulate import (EXCLUSION_REASONS, RECORDED_OUTCOME, SIMULATED_STATUSES,
+                               agreement_ok, baseline_check, gate_agreement, replay_tokens,
+                               select_rows, simulate, summarize)
 from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                      onboarded_row)
 
@@ -298,3 +299,115 @@ def test_shadow_price_of_the_budget_now_shows_the_rejections_it_would_avoid():
     legacy = [_without_size(r) for r in rows[:3]] + rows[3:]
     assert _entry(asyncio.run(shadow_prices(legacy, _snap(), base=BASE_SETTINGS)),
                   "max_cost_usd_per_request", app="app-a") is None
+
+
+# --- outcome-level agreement (spec §7) ----------------------------------------
+
+def _baseline_of(records, snap, **kw):
+    results, excluded = _run(records, snap, **kw)
+    return summarize(results, excluded, inputs=_INPUTS, edits={})["baseline"]
+
+
+def test_r11_every_status_has_a_recorded_outcome_class():                  # R11
+    """The class table covers exactly the statuses serving can write. A
+    sixth status fails here until someone decides what its first routing
+    decision was."""
+    from app.apps.outcomes import STATUSES
+    assert set(RECORDED_OUTCOME) == set(STATUSES) == set(SIMULATED_STATUSES)
+    assert set(RECORDED_OUTCOME.values()) == {"routed", "rejected"}
+    assert {s for s, c in RECORDED_OUTCOME.items() if c == "rejected"} == \
+        {"no_eligible", "context_overflow"}
+
+
+def test_outcome_agreement_is_perfect_when_rejections_are_reproduced():
+    rows = [make_row(f"ok{i}", "app-a") for i in range(3)] + \
+           [_rejected_row(f"rej{i}") for i in range(2)]
+    b = _baseline_of(rows, _snap())
+    assert (b["agreement"], b["rows_compared"]) == (1.0, 3)        # model: `ok` rows only
+    assert (b["outcome_agreement"], b["outcome_rows_compared"]) == (1.0, 5)
+    assert b["outcome_warning"] is False
+    assert (b["recorded_rejected"], b["recorded_rejected_but_routed"],
+            b["recorded_routed_but_rejected"]) == (2, 0, 0)
+    assert b["outcome_by_app"] == {"app-a": {
+        "rows": 5, "agreement": 1.0, "recorded_rejected": 2,
+        "recorded_rejected_but_routed": 0, "recorded_routed_but_rejected": 0}}
+
+
+def test_a_recorded_rejection_that_replay_routes_is_counted():
+    """The B60 signature, made visible: the snapshot's cap is ABOVE the
+    estimate (it was raised after the traffic was recorded), so replay
+    routes rows the record says were rejected. Model agreement cannot see
+    it; outcome agreement and its count can."""
+    rows = [make_row(f"ok{i}", "app-a") for i in range(3)] + \
+           [_rejected_row(f"rej{i}") for i in range(2)]
+    b = _baseline_of(rows, _snap(cap=100.0))
+    assert b["agreement"] == 1.0 and b["warning"] is False         # blind to it
+    assert b["outcome_agreement"] == pytest.approx(3 / 5)
+    assert b["outcome_warning"] is True
+    assert (b["recorded_rejected"], b["recorded_rejected_but_routed"],
+            b["recorded_routed_but_rejected"]) == (2, 2, 0)
+
+
+def test_a_recorded_route_that_replay_rejects_is_counted_per_app():
+    """The reverse direction, and the per-app split: app-b's served rows are
+    over ITS cap in replay; app-a is untouched."""
+    snap = make_snapshot(apps=[app_entry("app-a"),
+                               app_entry("app-b", max_cost_usd_per_request=1e-9)])
+    rows = [make_row("a1", "app-a"), make_row("b1", "app-b"),
+            make_row("b2", "app-b", status="backend_failed", routing_tokens=200,
+                     prompt_tokens=0)]
+    b = _baseline_of(rows, snap)
+    assert b["recorded_routed_but_rejected"] == 2 and b["recorded_rejected_but_routed"] == 0
+    assert b["outcome_agreement"] == pytest.approx(1 / 3)
+    assert b["outcome_by_app"]["app-a"]["agreement"] == 1.0
+    assert b["outcome_by_app"]["app-b"] == {
+        "rows": 2, "agreement": 0.0, "recorded_rejected": 0,
+        "recorded_rejected_but_routed": 0, "recorded_routed_but_rejected": 2}
+
+
+def test_backend_failed_and_verify_failed_rows_are_recorded_as_routed():
+    """Their first decision was a route: replay routing them is agreement."""
+    rows = [make_row("ok1", "app-a"),
+            make_row("bf", "app-a", status="backend_failed", prompt_tokens=0,
+                     routing_tokens=200, chosen=None),
+            make_row("vf", "app-a", status="verify_failed", routing_tokens=200)]
+    b = _baseline_of(rows, make_snapshot())
+    assert (b["outcome_agreement"], b["outcome_rows_compared"]) == (1.0, 3)
+    assert b["recorded_rejected"] == 0
+
+
+def test_outcome_agreement_is_null_with_no_simulated_row():
+    b = _baseline_of([], make_snapshot())
+    assert b["outcome_agreement"] is None and b["outcome_rows_compared"] == 0
+    assert b["outcome_warning"] is False and b["outcome_by_app"] == {}
+
+
+def test_diluted_outcome_agreement_still_reports_every_rejection_wrong():
+    """Why the count is reported with its denominator: 2 of 2 rejections
+    replay wrong while outcome agreement is still above the 90% threshold."""
+    rows = [make_row(f"ok{i}", "app-a") for i in range(28)] + \
+           [_rejected_row(f"rej{i}") for i in range(2)]
+    b = _baseline_of(rows, _snap(cap=100.0))
+    assert b["outcome_agreement"] == pytest.approx(28 / 30) and b["outcome_warning"] is False
+    assert (b["recorded_rejected_but_routed"], b["recorded_rejected"]) == (2, 2)
+    assert agreement_ok(b, 0.9) is True and agreement_ok(b, 1.0) is False
+
+
+@pytest.mark.parametrize("agreement,outcome,gate", [
+    (1.0, 0.72, 0.72), (0.5, 1.0, 0.5), (1.0, 1.0, 1.0),
+    (None, 1.0, None), (1.0, None, None), (None, None, None),
+])
+def test_r12_gate_takes_the_lower_of_the_two(agreement, outcome, gate):    # R12
+    baseline = {"agreement": agreement, "outcome_agreement": outcome}
+    assert gate_agreement(baseline) == gate
+    assert agreement_ok(baseline, 0.9) is (gate is not None and gate >= 0.9)
+    assert agreement_ok(baseline, 0.0) is (gate is not None)   # n/a never passes
+
+
+def test_baseline_check_carries_the_outcome_fields():
+    """`--shadow-prices` without `--proposal` gates through `baseline_check`:
+    it must return the same block, outcome fields included."""
+    rows = [make_row("ok1", "app-a"), _rejected_row("rej1")]
+    snap = _snap(cap=100.0)
+    assert asyncio.run(baseline_check(rows, snap, base=BASE_SETTINGS)) == \
+        _baseline_of(rows, snap)
````

Patch `tests/eval/test_simulate.py`:

````diff
diff --git a/tests/eval/test_simulate.py b/tests/eval/test_simulate.py
index 2225743..87f09e6 100644
--- a/tests/eval/test_simulate.py
+++ b/tests/eval/test_simulate.py
@@ -549,4 +549,8 @@ def test_baseline_check_equals_the_summarized_baseline():
     (0.0, 0.0, True), (None, 0.0, False),
 ])
 def test_agreement_ok(agreement, minimum, ok):
-    assert agreement_ok({"agreement": agreement, "rows_compared": 0}, minimum) is ok
+    # Outcome agreement is perfect here, so model agreement alone decides
+    # (B60: the gate takes the lower of the two --
+    # tests/eval/test_replay_rejections.py::test_r12_gate_takes_the_lower_of_the_two).
+    assert agreement_ok({"agreement": agreement, "rows_compared": 0,
+                         "outcome_agreement": 1.0}, minimum) is ok
````

Patch `tests/eval/test_scenarios_compare.py`:

````diff
diff --git a/tests/eval/test_scenarios_compare.py b/tests/eval/test_scenarios_compare.py
index e0ed5e3..6337954 100644
--- a/tests/eval/test_scenarios_compare.py
+++ b/tests/eval/test_scenarios_compare.py
@@ -88,7 +88,12 @@ def test_scenarios_come_back_in_file_order_with_shared_rows_and_baseline():
     assert cmp.window == {"start": "2026-09-20T12:00:00+00:00",
                           "end": "2026-09-22T12:00:11+00:00"}
     assert set(cmp.baseline) == {"rows_compared", "agreement", "warning", "by_app",
-                                 "top_disagreements", "overflow_contradicted"}
+                                 "top_disagreements", "overflow_contradicted",
+                                 # B60: outcome-level agreement, shared like the rest
+                                 "outcome_rows_compared", "outcome_agreement",
+                                 "outcome_warning", "recorded_rejected",
+                                 "recorded_rejected_but_routed",
+                                 "recorded_routed_but_rejected", "outcome_by_app"}
     for entry in cmp.scenarios:
         assert set(entry) == {"name", "edits", "assumptions", "summary"}
         assert set(entry["summary"]) == {"totals", "by_app", "by_workload", "moves",
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_rejections.py tests/eval/test_simulate.py tests/eval/test_scenarios_compare.py -p no:warnings -q -rf
```

Expected: a collection error, exit code 2 — `ImportError: cannot import name 'RECORDED_OUTCOME' from 'app.eval.simulate'` while importing `tests/eval/test_replay_rejections.py`.

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 614f59c..6e473cd 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -51,6 +51,16 @@ from app.routing.router import expected_output_tokens
 SIMULATED_STATUSES = frozenset({"ok", "backend_failed", "verify_failed",
                                 "no_eligible", "context_overflow"})
 
+#: The outcome each status's FIRST routing decision had (B60), which is what an
+#: arm's `outcome` is compared with. `verify_failed` and `backend_failed` were
+#: routed -- a model was chosen, and step 9 or the backend failed afterwards.
+#: `no_eligible` is always a first-decision rejection. `context_overflow` was
+#: refused for size on every attempt; replay models that as a `context_window`
+#: rejection. tests/eval/test_replay_rejections.py pins this table to
+#: `app/apps/outcomes.py::STATUSES`, so a new status must be given a class.
+RECORDED_OUTCOME = {"ok": "routed", "verify_failed": "routed", "backend_failed": "routed",
+                    "no_eligible": "rejected", "context_overflow": "rejected"}
+
 # `before_since` is checked first, so a row older than `--since` never reaches
 # the other checks (P3c-2 spec §6). `missing_routing_tokens` (B60) is checked
 # last: only a well-formed row of a known app can be "well-formed but unsized".
@@ -506,12 +516,25 @@ async def baseline_check(records: list[dict], snapshot: dict, *, base: Settings,
     return _round6(_baseline([RowResult(row, arm, arm) for row, arm in zip(rows, arms)]))
 
 
+def gate_agreement(baseline: dict) -> float | None:
+    """The one rate `--min-agreement` is compared with: the LOWER of model
+    agreement and outcome agreement (B60). They catch different defects --
+    replay picked a different model (B55) / replay served a request the
+    record says was rejected (B60) -- and a replay is fit to use only if it
+    reproduces both, so neither may hide behind the other. None when either
+    is not computable: with no served row nothing vouches for the model
+    replay picks, however well the outcomes agree."""
+    agreement, outcome = baseline["agreement"], baseline["outcome_agreement"]
+    if agreement is None or outcome is None:
+        return None
+    return min(agreement, outcome)
+
+
 def agreement_ok(baseline: dict, minimum: float) -> bool:
-    """The `--min-agreement` gate: False when the baseline has no comparable
-    row (`agreement` is None -- nothing vouches for the replay) or agrees
-    with the record less often than `minimum`."""
-    agreement = baseline["agreement"]
-    return agreement is not None and agreement >= minimum
+    """The `--min-agreement` gate: False when `gate_agreement` is None
+    (nothing vouches for the replay) or below `minimum`."""
+    rate = gate_agreement(baseline)
+    return rate is not None and rate >= minimum
 
 
 # --- summary and report (spec §8) -------------------------------------------
@@ -639,12 +662,42 @@ def _agrees(r: RowResult) -> bool:
     return _first_pick(r.current) == r.row["attempts"][0]
 
 
+def _outcome_stats(rows: list[RowResult]) -> dict:
+    """Outcome-level agreement over `rows` (B60): does the `current` arm's
+    OUTCOME -- routed or rejected -- match the recorded status's class
+    (`RECORDED_OUTCOME`)? Over every simulated row, served or not.
+
+    `recorded_rejected_but_routed` is the B60 signature: replay serves a
+    request the record says was rejected, so a proposal can show nothing to
+    rescue. It is reported next to `recorded_rejected`, its denominator,
+    because rejections are usually a small share of traffic and `agreement`
+    alone would dilute them."""
+    recorded = [RECORDED_OUTCOME[r.row["status"]] for r in rows]
+    rejected_but_routed = sum(1 for r, rec in zip(rows, recorded)
+                              if rec == "rejected" and r.current.outcome == "routed")
+    routed_but_rejected = sum(1 for r, rec in zip(rows, recorded)
+                              if rec == "routed" and r.current.outcome == "rejected")
+    n = len(rows)
+    return {
+        "rows": n,
+        "agreement": (n - rejected_but_routed - routed_but_rejected) / n if n else None,
+        "recorded_rejected": sum(1 for rec in recorded if rec == "rejected"),
+        "recorded_rejected_but_routed": rejected_but_routed,
+        "recorded_routed_but_rejected": routed_but_rejected,
+    }
+
+
 def _baseline(results: list[RowResult]) -> dict:
     """Fidelity check (spec §7): does the `current` arm's pick agree with the
     recorded first attempt, over simulated `ok` rows with a recorded
     attempt. Never compares `proposed` -- the headline diff is always
     `current` vs `proposed`, both simulated.
 
+    Two measures (B60). `agreement` is MODEL agreement, as above. The
+    `outcome_*` fields are OUTCOME agreement (`_outcome_stats`), over every
+    simulated row: a recorded rejection the `current` arm routes is
+    invisible to model agreement, which only looks at `ok` rows.
+
     `overflow_contradicted` (final review) counts the subset of
     disagreements where the record proves the row's recorded first pick
     SERVED it alone, live, while replay's predicted context-window overflow
@@ -675,6 +728,7 @@ def _baseline(results: list[RowResult]) -> dict:
     top = sorted(disagreements.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[:10]
     top_disagreements = [{"recorded": rec, "simulated": sim, "count": n}
                          for (rec, sim), n in top]
+    outcome = _outcome_stats(results)
     return {
         "rows_compared": rows_compared,
         "agreement": agreement,
@@ -682,6 +736,15 @@ def _baseline(results: list[RowResult]) -> dict:
         "by_app": by_app,
         "top_disagreements": top_disagreements,
         "overflow_contradicted": overflow_contradicted,
+        "outcome_rows_compared": outcome["rows"],
+        "outcome_agreement": outcome["agreement"],
+        "outcome_warning": outcome["agreement"] is not None and outcome["agreement"] < 0.90,
+        "recorded_rejected": outcome["recorded_rejected"],
+        "recorded_rejected_but_routed": outcome["recorded_rejected_but_routed"],
+        "recorded_routed_but_rejected": outcome["recorded_routed_but_rejected"],
+        "outcome_by_app": {
+            app_id: _outcome_stats([r for r in results if r.row["application_id"] == app_id])
+            for app_id in sorted({r.row["application_id"] for r in results})},
     }
 
 
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_rejections.py tests/eval/test_simulate.py tests/eval/test_scenarios_compare.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_replay_rejections.py tests/eval/test_simulate.py tests/eval/test_scenarios_compare.py app/eval/simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): outcome-level baseline agreement; --min-agreement gates on the lower rate (B60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 5: Both reports and the exit-4 message show outcome agreement (spec §6.2, §7.3, §7.4, R6, R12)

**Files:**
- Modify: `app/eval/simulate.py` (`render_report` line 850)
- Modify: `app/eval/scenarios.py` (the `app.eval.simulate` import line 27; `_agreement_lines` line 294)
- Modify: `scripts/policy_simulate.py` (module docstring line 28; `_parser` line 94; `_below_min_agreement` line 100)
- Test: `tests/eval/test_replay_rejections.py`, `tests/eval/test_scenarios_compare.py`, `tests/eval/test_policy_simulate_cli.py`

**Interfaces:**
- Consumes:
  - Task 4's baseline keys: `agreement`, `rows_compared`, `warning`, `overflow_contradicted`, `outcome_agreement`, `outcome_rows_compared`, `outcome_warning`, `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected`, `outcome_by_app` (app id → `{rows, agreement, …}`).
  - Task 3's `rows.estimated_tokens` in `summarize()` output; the comparison document's `rows` is the same block (`app/eval/scenarios.py::compare` lifts it).
  - `render_report(sim: dict) -> str`; `app/eval/scenarios.py::_agreement_lines(doc: dict) -> list[str]` and its `_pct(value: float) -> str`; `scripts/policy_simulate.py::_below_min_agreement(baseline: dict, minimum: float) -> int`, called from `main` and `_compare_scenarios`.
  - Test helpers: `tests/eval/test_policy_simulate_cli.py` (`_run_cli(tmp_path, export_lines, snapshot, proposal, *extra)`, `_run_cli_no_proposal(tmp_path, export_lines, snapshot, *extra)`, `_run_cli_scenarios(tmp_path, export_lines, snapshot, scenarios_doc, *extra)`, `_scenarios(*pairs, title="Options")`, `_line(row)`, `P`); `tests/eval/test_scenarios_compare.py` (`_document(records, snap, doc, inputs=None, **kw)`, `_doc(*scenarios, title=...)`, `_snap()`, `render_comparison`).
- Produces:
  - `_rate(value: float | None) -> str` — `"87.5%"` or `"n/a"`.
  - `_agreement_line(baseline: dict) -> str` — `Baseline agreement: model <r> over <n> compared rows; outcome <r> over <m> simulated rows.`
  - `_fidelity_notes(baseline: dict, rows: dict) -> list[str]` — plain sentences, imported by `scenarios.py`.
  - `render_report` prints `_agreement_line` right after the caveat, on every report; an outcome warning below 90%; one `> **Note:** …` per fidelity note.
  - The comparison banner prints `**Outcome agreement: <r>** over <m> simulated rows (routed or rejected, against each row's recorded status).`, its warning, `Per app (outcome): …`, and one `**Note:** …` per fidelity note.
  - The exit-4 message: `policy_simulate: baseline agreement <a> over <n> compared rows, outcome agreement <o> over <m> simulated rows (<x> recorded rejected but routed, <y> recorded routed but rejected): below --min-agreement <X> (the lower of the two is compared; n/a never passes); nothing written`.

**Rules.** The existing banner strings stay byte-for-byte (`**Baseline agreement: 50.0%** over 12 compared rows.`, `⚠ **Warning:** below the 90% threshold`, `Per app: …`, `**Baseline agreement: n/a**`), and so do the two substrings the existing gate tests read from stderr (`baseline agreement 0 over 4 compared rows`, `--min-agreement 0.9`). The outcome banner is printed even when model agreement is `n/a`. A fidelity note is printed for any non-zero disagreement count, whatever the rate, and for any `estimated_tokens`.

- [ ] **Step 1: Write the failing tests**

Patch `tests/eval/test_replay_rejections.py`:

````diff
diff --git a/tests/eval/test_replay_rejections.py b/tests/eval/test_replay_rejections.py
index b1ef226..2e023fa 100644
--- a/tests/eval/test_replay_rejections.py
+++ b/tests/eval/test_replay_rejections.py
@@ -19,8 +19,8 @@ from hypothesis import HealthCheck, given, settings as hsettings, strategies as
 import app.eval.simulate as simulate_mod
 from app.eval.outcomes import load_export
 from app.eval.simulate import (EXCLUSION_REASONS, RECORDED_OUTCOME, SIMULATED_STATUSES,
-                               agreement_ok, baseline_check, gate_agreement, replay_tokens,
-                               select_rows, simulate, summarize)
+                               agreement_ok, baseline_check, gate_agreement, render_report,
+                               replay_tokens, select_rows, simulate, summarize)
 from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                      onboarded_row)
 
@@ -411,3 +411,63 @@ def test_baseline_check_carries_the_outcome_fields():
     snap = _snap(cap=100.0)
     assert asyncio.run(baseline_check(rows, snap, base=BASE_SETTINGS)) == \
         _baseline_of(rows, snap)
+
+
+# --- report.md shows both agreements (spec §7.3) ------------------------------
+
+def _report(records, snap, prop=None):
+    results, excluded = _run(records, snap, prop)
+    return render_report(summarize(results, excluded, inputs=_INPUTS, edits={}))
+
+
+def test_report_always_states_both_agreements():
+    """Printed on a clean run too: B60 went unnoticed because a report with
+    nothing wrong to warn about said nothing about agreement at all."""
+    rows = [make_row(f"ok{i}", "app-a") for i in range(3)] + [_rejected_row("rej")]
+    report = _report(rows, _snap())
+    assert ("Baseline agreement: model 100.0% over 3 compared rows; "
+            "outcome 100.0% over 4 simulated rows.") in report
+    assert "**Warning:**" not in report
+    assert "replay as routed" not in report and "replay as rejected" not in report
+    # ...but the estimate is always labelled when a row is sized from it.
+    assert "> **Note:** 1 row(s) had no backend token count" in report
+    assert "an estimate, not a bill" in report
+
+
+def test_report_without_estimated_rows_has_no_estimate_note():
+    report = _report([make_row("ok1", "app-a")], _snap())
+    assert "no backend token count" not in report
+    assert "outcome 100.0% over 1 simulated rows." in report
+
+
+def test_report_warns_and_counts_rejections_replay_routes():
+    rows = [make_row(f"ok{i}", "app-a") for i in range(3)] + \
+           [_rejected_row(f"rej{i}") for i in range(2)]
+    report = _report(rows, _snap(cap=100.0))
+    assert "model 100.0% over 3 compared rows; outcome 60.0% over 5 simulated rows." in report
+    assert ("> **Warning:** baseline outcome agreement is only 60.0% across 5 simulated "
+            "rows (below the 90% threshold).") in report
+    assert "> **Note:** 2 of 2 recorded rejection(s) replay as routed." in report
+    # the banner sits above the headline, like the model-agreement warning
+    assert report.index("recorded rejection(s) replay as routed") < report.index("## Headline")
+
+
+def test_report_notes_a_diluted_disagreement_without_a_warning():
+    rows = [make_row(f"ok{i}", "app-a") for i in range(28)] + \
+           [_rejected_row(f"rej{i}") for i in range(2)]
+    report = _report(rows, _snap(cap=100.0))
+    assert "outcome 93.3% over 30 simulated rows." in report
+    assert "baseline outcome agreement is only" not in report        # above 90%
+    assert "> **Note:** 2 of 2 recorded rejection(s) replay as routed." in report
+
+
+def test_report_counts_routes_replay_rejects():
+    snap = make_snapshot(apps=[app_entry("app-a", max_cost_usd_per_request=1e-9)])
+    report = _report([make_row("ok1", "app-a"), make_row("ok2", "app-a")], snap)
+    assert "> **Note:** 2 of 2 row(s) recorded as routed replay as rejected." in report
+
+
+def test_report_states_agreement_as_na_when_not_computable():
+    row = make_row("bf", "app-a", status="backend_failed", prompt_tokens=0, routing_tokens=200)
+    report = _report([row], make_snapshot())
+    assert "model n/a over 0 compared rows; outcome 100.0% over 1 simulated rows." in report
````

Patch `tests/eval/test_scenarios_compare.py`:

````diff
diff --git a/tests/eval/test_scenarios_compare.py b/tests/eval/test_scenarios_compare.py
index 6337954..b9c2e93 100644
--- a/tests/eval/test_scenarios_compare.py
+++ b/tests/eval/test_scenarios_compare.py
@@ -438,3 +438,58 @@ def test_report_cached_share_note_and_no_title():
 def test_report_has_no_assumptions_section_when_nothing_is_assumed():
     report = render_comparison(_document(_rows(), _snap(), _doc(("empty", {}))))
     assert "## Assumptions" not in report
+
+
+# --- B60: the banner shows outcome agreement next to model agreement -----------
+
+def _budget_rows():
+    """`chat` serves 4 rows; `claims` has 2 served rows and 2 the record says
+    were rejected at its per-request budget (3000 routing tokens, 16 output)."""
+    rows = [make_row(f"c{i}", "chat") for i in range(4)]
+    rows += [make_row(f"k{i}", "claims") for i in range(2)]
+    rows += [make_row(f"x{i}", "claims", status="no_eligible", complexity=1.0,
+                      prompt_tokens=0, completion_tokens=0, routing_tokens=3000,
+                      max_tokens=16, attempts=[], chosen=None) for i in range(2)]
+    return rows
+
+
+def _budget_snap(cap):
+    return make_snapshot(apps=[app_entry("claims", max_cost_usd_per_request=cap),
+                               app_entry("chat")])
+
+
+def test_report_banner_shows_outcome_agreement_when_rejections_are_reproduced():
+    doc = _document(_budget_rows(), _budget_snap(0.0002),
+                    _doc(("raise-cap", {"apps": {"claims": {"max_cost_usd_per_request": 1}}})))
+    assert doc["baseline"]["outcome_agreement"] == 1.0
+    assert doc["table"][0]["newly_served"] == 2
+    assert doc["rows"]["estimated_tokens"] == 2
+    report = render_comparison(doc)
+    assert "**Baseline agreement: 100.0%** over 6 compared rows." in report
+    assert ("**Outcome agreement: 100.0%** over 8 simulated rows (routed or rejected, "
+            "against each row's recorded status).") in report
+    assert "Per app (outcome): chat 100.0% · claims 100.0%" in report
+    assert "replay as routed" not in report
+    assert "**Note:** 2 row(s) had no backend token count" in report
+    assert report.index("**Outcome agreement") < report.index("## Comparison")
+
+
+def test_report_banner_warns_and_counts_rejections_replay_routes():
+    """The snapshot's cap is already above the estimate, so replay routes the
+    two recorded rejections: model agreement is blind to it, the outcome
+    banner is not, and `claims` is the app marked."""
+    doc = _document(_budget_rows(), _budget_snap(100.0), _doc(("empty", {})))
+    assert doc["baseline"]["agreement"] == 1.0 and doc["baseline"]["warning"] is False
+    report = render_comparison(doc)
+    assert "**Outcome agreement: 75.0%** over 8 simulated rows" in report
+    assert "⚠ **Warning:** outcome agreement is below the 90% threshold." in report
+    assert "Per app (outcome): chat 100.0% · claims 50.0% ⚠" in report
+    assert "**Note:** 2 of 2 recorded rejection(s) replay as routed." in report
+    assert "⚠ **Warning:** below the 90% threshold" not in report   # model agreement is fine
+
+
+def test_report_banner_shows_outcome_agreement_without_comparable_model_rows():
+    rows = [make_row("r", "chat", status="backend_failed")]
+    report = render_comparison(_document(rows, _snap(), _doc(("empty", {}))))
+    assert "**Baseline agreement: n/a**" in report
+    assert "**Outcome agreement: 100.0%** over 1 simulated rows" in report
````

Patch `tests/eval/test_policy_simulate_cli.py`:

````diff
diff --git a/tests/eval/test_policy_simulate_cli.py b/tests/eval/test_policy_simulate_cli.py
index 634fcc2..c715b47 100644
--- a/tests/eval/test_policy_simulate_cli.py
+++ b/tests/eval/test_policy_simulate_cli.py
@@ -636,3 +636,78 @@ def test_live_round_trip_scenarios_empty_scenario_is_a_perfect_noop(tmp_path):
     assert doc["rows"]["simulated"] == 4 and doc["baseline"]["agreement"] == 1.0
     (row,) = doc["table"]
     assert (row["route_changed"], row["newly_rejected"], row["cost_delta_usd"]) == (0, 0, 0)
+
+
+# --- B60: --min-agreement gates on the lower of model and outcome agreement ----
+
+def _rejected(request_id, **over):
+    """An export row for a request rejected at the per-request budget, as
+    serving writes it since B60: no backend usage, the router's token count."""
+    row = dict(status="no_eligible", complexity=1.0, prompt_tokens=0, completion_tokens=0,
+               routing_tokens=3000, max_tokens=16, attempts=[], chosen=None)
+    row.update(over)
+    return make_row(request_id, "app-a", **row)
+
+
+def _mixed_outcome_lines():
+    """3 served rows replay picks correctly + 2 recorded budget rejections."""
+    return [_line(make_row(f"ok{i}", "app-a")) for i in range(3)] + \
+           [_line(_rejected(f"rej{i}")) for i in range(2)]
+
+
+def _cap_snapshot(cap):
+    return make_snapshot(apps=[app_entry("app-a", max_cost_usd_per_request=cap)])
+
+
+def test_r12_min_agreement_exits_4_on_outcome_disagreement(tmp_path):      # R12
+    """Model agreement is 1.0 (every served row replays to its model), but
+    the snapshot's cap is above the estimate, so replay ROUTES the two
+    recorded rejections: outcome agreement 0.6. The gate takes the lower."""
+    proc, out = _run_cli(tmp_path, _mixed_outcome_lines(), _cap_snapshot(100.0),
+                         {"schema": P}, "--min-agreement", "0.9")
+    assert proc.returncode == 4, proc.stderr
+    assert "baseline agreement 1 over 3 compared rows" in proc.stderr
+    assert "outcome agreement 0.6 over 5 simulated rows" in proc.stderr
+    assert "2 recorded rejected but routed, 0 recorded routed but rejected" in proc.stderr
+    assert "--min-agreement 0.9" in proc.stderr
+    assert not out.exists()                       # no directory, no files
+
+
+def test_min_agreement_passes_when_rejections_are_reproduced(tmp_path):
+    proc, out = _run_cli(tmp_path, _mixed_outcome_lines(), _cap_snapshot(0.0002),
+                         {"schema": P, "apps": {"app-a": {"max_cost_usd_per_request": 1}}},
+                         "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    assert sim["baseline"]["agreement"] == sim["baseline"]["outcome_agreement"] == 1.0
+    assert sim["totals"]["newly_served"] == 2
+    assert sim["rows"]["estimated_tokens"] == 2
+    assert "outcome 100.0% over 5 simulated rows." in (out / "report.md").read_text()
+
+
+def test_min_agreement_outcome_gate_applies_to_scenarios_and_shadow_prices(tmp_path):
+    proc, out = _run_cli_scenarios(tmp_path, _mixed_outcome_lines(), _cap_snapshot(100.0),
+                                   _scenarios(("noop", {})), "--min-agreement", "0.9")
+    assert proc.returncode == 4, proc.stderr
+    assert "outcome agreement 0.6 over 5 simulated rows" in proc.stderr and not out.exists()
+    proc, out = _run_cli_no_proposal(tmp_path, _mixed_outcome_lines(), _cap_snapshot(100.0),
+                                     "--shadow-prices", "--min-agreement", "0.9")
+    assert proc.returncode == 4, proc.stderr
+    assert "outcome agreement 0.6 over 5 simulated rows" in proc.stderr and not out.exists()
+
+
+def test_cli_counts_prefix_rejected_rows_as_missing_routing_tokens(tmp_path):
+    """An export taken before the column existed: its rejected rows carry no
+    size, so they are excluded and counted -- never replayed as empty prompts
+    (which is what made a raised budget show nothing to rescue)."""
+    old = [_line({k: v for k, v in _rejected(f"rej{i}").items() if k != "routing_tokens"})
+           for i in range(2)]
+    lines = [_line(make_row(f"ok{i}", "app-a")) for i in range(3)] + old
+    proc, out = _run_cli(tmp_path, lines, _cap_snapshot(0.0002),
+                         {"schema": P, "apps": {"app-a": {"max_cost_usd_per_request": 1}}})
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    assert sim["rows"]["simulated"] == 3
+    assert sim["rows"]["excluded"]["missing_routing_tokens"] == 2
+    assert sim["totals"]["newly_served"] == 0
+    assert "| missing_routing_tokens | 2 |" in (out / "report.md").read_text()
````

- [ ] **Step 2: Run them — expected FAIL**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_rejections.py tests/eval/test_scenarios_compare.py tests/eval/test_policy_simulate_cli.py -p no:warnings -q -rf
```

Expected: 12 failed.

- Six in `tests/eval/test_replay_rejections.py` (`test_report_…`): the report has no agreement line and no notes yet.
- Three in `tests/eval/test_scenarios_compare.py` (`test_report_banner_…`): the banner has no outcome paragraph yet.
- Three in `tests/eval/test_policy_simulate_cli.py`: `test_r12_min_agreement_exits_4_on_outcome_disagreement` and `test_min_agreement_outcome_gate_applies_to_scenarios_and_shadow_prices` (the gate already exits 4 since Task 4, but the message does not name outcome agreement), and `test_min_agreement_passes_when_rejections_are_reproduced` (no agreement line in `report.md`).
- `test_cli_counts_prefix_rejected_rows_as_missing_routing_tokens` passes already: the exclusion is Task 3's, and this test pins it at the CLI.

- [ ] **Step 3: Implement**

Patch `app/eval/simulate.py`:

````diff
diff --git a/app/eval/simulate.py b/app/eval/simulate.py
index 6e473cd..06f8892 100644
--- a/app/eval/simulate.py
+++ b/app/eval/simulate.py
@@ -984,14 +984,64 @@ def _excluded_lines(excluded: dict) -> list[str]:
     return lines
 
 
+def _rate(value: float | None) -> str:
+    """An agreement rate for a report: `87.5%`, or `n/a` when not computable."""
+    return "n/a" if value is None else f"{value * 100:.1f}%"
+
+
+def _agreement_line(baseline: dict) -> str:
+    """Both baseline agreements on one line (B60) -- printed on every
+    report, not only when one of them is low: a replay nobody was told
+    about is how B60 went unnoticed."""
+    return (f"Baseline agreement: model {_rate(baseline['agreement'])} over "
+            f"{baseline['rows_compared']} compared rows; outcome "
+            f"{_rate(baseline['outcome_agreement'])} over "
+            f"{baseline['outcome_rows_compared']} simulated rows.")
+
+
+def _fidelity_notes(baseline: dict, rows: dict) -> list[str]:
+    """Plain sentences both reports print under the agreement banner (B60),
+    one per condition that holds; empty when none does.
+
+    The two outcome disagreement counts are printed with their own
+    denominators whenever they are non-zero, whatever `outcome_agreement`
+    is: rejections are usually a small share of traffic, so every one of
+    them can replay wrong while the rate stays above the 90% threshold."""
+    notes: list[str] = []
+    rejected = baseline["recorded_rejected"]
+    wrongly_routed = baseline["recorded_rejected_but_routed"]
+    if wrongly_routed:
+        notes.append(
+            f"{wrongly_routed} of {rejected} recorded rejection(s) replay as routed. Replay "
+            "does not reproduce them, so \"newly served\" is understated for these rows -- "
+            "check for drift since the export window (a budget, a declared context window "
+            "or an endpoint's state).")
+    wrongly_rejected = baseline["recorded_routed_but_rejected"]
+    if wrongly_rejected:
+        notes.append(
+            f"{wrongly_rejected} of {baseline['outcome_rows_compared'] - rejected} row(s) "
+            "recorded as routed replay as rejected. \"newly rejected\" is understated for "
+            "these rows -- check for drift since the export window.")
+    estimated = rows.get("estimated_tokens", 0)
+    if estimated:
+        notes.append(
+            f"{estimated} row(s) had no backend token count (rejected, or failed before a "
+            "response). Replay sizes and prices them from RouteForge's own routing token "
+            "count and output-token estimate -- an estimate, not a bill.")
+    return notes
+
+
 def render_report(sim: dict) -> str:
-    """The `report.md` document (spec §8), in the order: caveat, baseline
-    warning, headline, new rejections, moves, by-app, by-workload,
-    latency/quality sources, excluded rows."""
+    """The `report.md` document (spec §8), in the order: caveat, the two
+    baseline agreements, baseline warnings and notes, headline, new
+    rejections, moves, by-app, by-workload, latency/quality sources,
+    excluded rows."""
     lines: list[str] = ["> " + line for line in REPORT_CAVEAT.split("\n")]
     lines.append("")
 
     baseline = sim["baseline"]
+    lines.append(_agreement_line(baseline))
+    lines.append("")
     if baseline["warning"]:
         lines.append(
             f"> **Warning:** baseline agreement is only {baseline['agreement'] * 100:.1f}% "
@@ -1004,6 +1054,16 @@ def render_report(sim: dict) -> str:
             "replay predicts would overflow -- check that endpoint's declared "
             "context_window.")
         lines.append("")
+    if baseline["outcome_warning"]:
+        lines.append(
+            f"> **Warning:** baseline outcome agreement is only "
+            f"{baseline['outcome_agreement'] * 100:.1f}% across "
+            f"{baseline['outcome_rows_compared']} simulated rows (below the 90% threshold). "
+            "Replay serves or rejects requests differently from the record this often.")
+        lines.append("")
+    for note in _fidelity_notes(baseline, sim["rows"]):
+        lines.append(f"> **Note:** {note}")
+        lines.append("")
 
     lines += _assumption_lines(sim["assumptions"])
 
````

Patch `app/eval/scenarios.py`:

````diff
diff --git a/app/eval/scenarios.py b/app/eval/scenarios.py
index ec9446b..391cf83 100644
--- a/app/eval/scenarios.py
+++ b/app/eval/scenarios.py
@@ -25,9 +25,9 @@ from app.eval.proposal import (SCHEMA as PROPOSAL_SCHEMA, ProposalError, _valida
                                normalized_edits)
 from app.eval.scenario import build_scenario, close_scenario
 from app.eval.simulate import (CACHED_SHARE_NOTE, REPORT_CAVEAT, _assumption_items,
-                               _excluded_lines, _metrics_table, _move_lines,
-                               _new_rejection_lines, _per_day, arm_results, run_rows,
-                               summarize)
+                               _excluded_lines, _fidelity_notes, _metrics_table,
+                               _move_lines, _new_rejection_lines, _per_day, arm_results,
+                               run_rows, summarize)
 
 SCHEMA = "routeforge.policy-scenarios/v1"
 COMPARISON_SCHEMA = "routeforge.scenario-comparison/v1"
@@ -292,8 +292,10 @@ def _pct(value: float) -> str:
 
 
 def _agreement_lines(doc: dict) -> list[str]:
-    """The agreement banner: overall, per app, a warning under 90%, and the
-    `--since` cut when one was applied."""
+    """The agreement banner: model agreement (overall, per app, a warning
+    under 90%), outcome agreement (the same, B60) with a note for every
+    recorded outcome replay does not reproduce, and the `--since` cut when
+    one was applied."""
     baseline = doc["baseline"]
     lines = ["## Baseline agreement", ""]
     if baseline["agreement"] is None:
@@ -317,6 +319,28 @@ def _agreement_lines(doc: dict) -> list[str]:
                 f"**Note:** {baseline['overflow_contradicted']} row(s) served by an endpoint "
                 "replay predicts would overflow -- check that endpoint's declared "
                 "context_window.")
+    # B60: outcome agreement is over EVERY simulated row, so it is shown even
+    # when no served row makes model agreement computable.
+    lines.append("")
+    if baseline["outcome_agreement"] is None:
+        lines.append("**Outcome agreement: n/a** -- no simulated row.")
+    else:
+        lines.append(f"**Outcome agreement: {_pct(baseline['outcome_agreement'])}** over "
+                     f"{baseline['outcome_rows_compared']} simulated rows (routed or "
+                     "rejected, against each row's recorded status).")
+        if baseline["outcome_warning"]:
+            lines.append("")
+            lines.append("⚠ **Warning:** outcome agreement is below the 90% threshold. "
+                         "Replay serves or rejects requests differently from the record "
+                         "this often.")
+        if baseline["outcome_by_app"]:
+            lines.append("")
+            lines.append("Per app (outcome): " + " · ".join(
+                f"{app_id} {_pct(stats['agreement'])}" + (" ⚠" if stats["agreement"] < 0.90 else "")
+                for app_id, stats in sorted(baseline["outcome_by_app"].items())))
+    for note in _fidelity_notes(baseline, doc["rows"]):
+        lines.append("")
+        lines.append(f"**Note:** {note}")
     since = doc["inputs"].get("since")
     if since is not None:
         lines.append("")
````

Patch `scripts/policy_simulate.py`:

````diff
diff --git a/scripts/policy_simulate.py b/scripts/policy_simulate.py
index e33ed1f..6c7f48e 100644
--- a/scripts/policy_simulate.py
+++ b/scripts/policy_simulate.py
@@ -28,8 +28,9 @@ File-based only: no DB, no `app.api`, no serving import
 Exit codes: 0 output written; 2 an input file failed to load, validate or
 apply, or the flags do not combine; 3 the proposal (or any scenario)
 introduces new rejections and `--fail-on-new-rejections` is set (for
-CI/GitOps gating); 4 the baseline's agreement with the record is below
-`--min-agreement` (nothing is written).
+CI/GitOps gating); 4 the baseline's agreement with the record -- the lower
+of model agreement and outcome agreement -- is below `--min-agreement`
+(nothing is written).
 """
 from __future__ import annotations
 
@@ -93,17 +94,30 @@ def _parser() -> argparse.ArgumentParser:
                         "(counted as before_since). Applies to every mode.")
     p.add_argument("--min-agreement", type=float, default=None, metavar="X",
                    help="0..1; exit 4 and write nothing when the baseline's agreement "
-                        "with the recorded first attempts is below X (or not computable)")
+                        "with the record is below X (or not computable). The lower of two "
+                        "rates is compared: model agreement (first pick vs the recorded "
+                        "first attempt) and outcome agreement (routed/rejected vs the "
+                        "recorded status)")
     return p
 
 
+def _shown(rate: float | None) -> str:
+    return "n/a" if rate is None else f"{rate:.6g}"
+
+
 def _below_min_agreement(baseline: dict, minimum: float) -> int:
-    """Print why the `--min-agreement` gate refused the run; return exit 4."""
-    agreement = baseline["agreement"]
-    shown = "n/a" if agreement is None else f"{agreement:.6g}"
-    print(f"policy_simulate: baseline agreement {shown} over "
-          f"{baseline['rows_compared']} compared rows is below --min-agreement "
-          f"{minimum:g}; nothing written", file=sys.stderr)
+    """Print why the `--min-agreement` gate refused the run; return exit 4.
+    Both rates and both outcome-disagreement counts are named, so the
+    operator can tell a wrong-model replay (B55) from a wrong-outcome one
+    (B60) without re-running."""
+    print(f"policy_simulate: baseline agreement {_shown(baseline['agreement'])} over "
+          f"{baseline['rows_compared']} compared rows, outcome agreement "
+          f"{_shown(baseline['outcome_agreement'])} over "
+          f"{baseline['outcome_rows_compared']} simulated rows "
+          f"({baseline['recorded_rejected_but_routed']} recorded rejected but routed, "
+          f"{baseline['recorded_routed_but_rejected']} recorded routed but rejected): "
+          f"below --min-agreement {minimum:g} (the lower of the two is compared; n/a "
+          "never passes); nothing written", file=sys.stderr)
     return 4
 
 
````

- [ ] **Step 4: Run — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_replay_rejections.py tests/eval/test_scenarios_compare.py tests/eval/test_policy_simulate_cli.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_replay_rejections.py tests/eval/test_scenarios_compare.py tests/eval/test_policy_simulate_cli.py app/eval/simulate.py app/eval/scenarios.py scripts/policy_simulate.py
git commit -m "$(cat <<'MSG'
feat(eval): both reports and the exit-4 message show outcome agreement (B60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 6: The live round trip, with rejections (spec §10, R3, R4, R5, R6)

**Files:**
- Test: `tests/eval/test_policy_simulate_cli.py`

**Interfaces:**
- Consumes:
  - Tasks 1–2: a live `no_eligible` row carries `routing_tokens` and `prompt_tokens: 0`, and the export line carries both.
  - Tasks 3–5: `simulation.json` has `rows.simulated`, `rows.estimated_tokens`, `rows.excluded.missing_routing_tokens`, `baseline.{agreement, rows_compared, outcome_agreement, outcome_rows_compared, recorded_rejected, recorded_rejected_but_routed, recorded_routed_but_rejected}`, `totals.{newly_served, newly_rejected, route_changed}`, `by_app.<id>.{newly_served, cost_usd.delta}`, `affected_apps`; `comparison.json` has `baseline` and `table[].{name, newly_served}`.
  - Already in `tests/eval/test_policy_simulate_cli.py`: `TestClient`, `create_app`, `_login`, `_run_cli`, `_run_cli_scenarios`, `_scenarios`, `P`, `json`, `pytest`.
  - `tests/onboarding/harness.py::onboard(client, app_id, **policy)` — creates the app and a token through the admin API (it logs the admin in), writes the policy columns, and returns an object with `.chat(payload)`.
- Produces: three tests. No production code.

**Rules.** This task adds tests only; with Tasks 1–5 in place they pass at once, so Step 3 takes the replay fix away for a moment to watch the acceptance test fail for the right reason. They are the acceptance test for B60, so they are built on a rejection caused by size (Review Focus 1): `"word " * 2400` is 3000 routing tokens, `max_tokens` is 16, the cap is $0.0001, and the seeded models cost $0.05 and $0.10 per 1M tokens. `newly_served` is asserted first, because `0` there is the defect as the DGX lab saw it. The 6-decimal rounding of `simulation.json` is why the cost assertion uses an absolute tolerance.

- [ ] **Step 1: Write the tests**

Patch `tests/eval/test_policy_simulate_cli.py`:

````diff
diff --git a/tests/eval/test_policy_simulate_cli.py b/tests/eval/test_policy_simulate_cli.py
index c715b47..16b0b9f 100644
--- a/tests/eval/test_policy_simulate_cli.py
+++ b/tests/eval/test_policy_simulate_cli.py
@@ -711,3 +711,128 @@ def test_cli_counts_prefix_rejected_rows_as_missing_routing_tokens(tmp_path):
     assert sim["rows"]["excluded"]["missing_routing_tokens"] == 2
     assert sim["totals"]["newly_served"] == 0
     assert "| missing_routing_tokens | 2 |" in (out / "report.md").read_text()
+
+
+# --- B60: the live round trip, with rejections (R3, R4, R5) -------------------
+
+# 12 000 characters -> 3000 routing tokens (`count_tokens` is len // 4). With
+# the seeded prices ($0.05 and $0.10 per 1M) and a 16-token output cap the
+# request is estimated at $0.0001508 (local-8b) / $0.0003016 (local-32b): over
+# the $0.0001 cap on both tiers. The SAME request with a zero-token prompt is
+# estimated at $0.0000008 / $0.0000016 -- under it. So these requests are
+# rejected BECAUSE OF THEIR SIZE, which is the case B60 could not replay: a
+# test built on a large `max_tokens` instead would be over budget on its
+# output alone and would have passed before the fix.
+_LIVE_LONG = {"messages": [{"role": "user", "content": "word " * 2400}], "max_tokens": 16}
+_LIVE_CAP = 0.0001
+_LIVE_APP = "rr-budget"
+
+
+def _live_traffic_with_rejections():
+    """A real app, real requests: 3 served for the demo app, 1 served and 3
+    budget-rejected for a capped app. Returns `(export_lines, snapshot)`."""
+    from tests.onboarding.harness import onboard
+
+    with TestClient(create_app()) as c:
+        demo = {"Authorization": "Bearer demo-key"}
+        hello = {"messages": [{"role": "user", "content": "hello"}]}
+        for _ in range(3):
+            assert c.post("/v1/chat/completions", headers=demo, json=hello).status_code == 200
+        app = onboard(c, _LIVE_APP, max_cost_usd_per_request=_LIVE_CAP)   # logs the admin in
+        assert app.chat({**hello, "max_tokens": 16}).status_code == 200
+        for _ in range(3):
+            r = app.chat(_LIVE_LONG)
+            assert r.status_code == 422, r.text
+            assert r.json()["error"]["code"] == "no_eligible_model"
+        snapshot = c.get("/admin/config-snapshot").json()
+        export = [json.loads(x) for x in
+                  c.get("/admin/outcomes/export").text.strip().splitlines()]
+    return export, snapshot
+
+
+def _raise_live_cap():
+    return {"apps": {_LIVE_APP: {"max_cost_usd_per_request": 1.0}}}
+
+
+def test_live_round_trip_budget_rejections_replay_as_rejected(tmp_path):  # R3, R4, R5
+    """The B60 experiment, end to end on a real app. Requests rejected live
+    for `max_cost_usd_per_request` are REJECTED by replay's `current` arm
+    under the snapshot the same deployment exported (R3); a proposal raising
+    the cap newly serves exactly them (R4); and both agreements are 1.0, so
+    `--min-agreement 1.0` passes (R5)."""
+    export, snapshot = _live_traffic_with_rejections()
+    rejected = [e["outcome"] for e in export if e["outcome"]["status"] == "no_eligible"]
+    assert len(rejected) == 3
+    assert all(o["routing_tokens"] == 3000 and o["prompt_tokens"] == 0 for o in rejected)
+
+    proc, out = _run_cli(tmp_path, export, snapshot, {"schema": P, **_raise_live_cap()},
+                         "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    assert sim["rows"]["simulated"] == 7
+    # The B60 headline, asserted first: before the fix this was 0, because
+    # replay had already (wrongly) routed the three rejected requests.
+    assert sim["totals"]["newly_served"] == 3                                             # R4
+    assert sim["totals"]["newly_rejected"] == 0
+    assert sim["rows"]["excluded"]["missing_routing_tokens"] == 0
+    assert sim["rows"]["estimated_tokens"] == 3
+    base = sim["baseline"]
+    assert (base["agreement"], base["rows_compared"]) == (1.0, 4)
+    assert (base["outcome_agreement"], base["outcome_rows_compared"]) == (1.0, 7)       # R5
+    assert (base["recorded_rejected"], base["recorded_rejected_but_routed"],
+            base["recorded_routed_but_rejected"]) == (3, 0, 0)                           # R3
+    assert sim["by_app"][_LIVE_APP]["newly_served"] == 3
+    (demo_app,) = set(sim["by_app"]) - {_LIVE_APP}             # the seeded demo app
+    assert sim["by_app"][demo_app]["newly_served"] == 0
+    assert sim["affected_apps"] == [_LIVE_APP]
+    # Each rescued request costs its admission estimate on the tier it routes
+    # to (local-32b: 3000 prompt + 16 output tokens at $0.10 per 1M). The
+    # JSON is rounded to 6 places, hence the absolute tolerance.
+    assert sim["by_app"][_LIVE_APP]["cost_usd"]["delta"] == \
+        pytest.approx(3 * 0.0003016, abs=1e-6)
+    report = (out / "report.md").read_text()
+    assert "model 100.0% over 4 compared rows; outcome 100.0% over 7 simulated rows." in report
+
+    # No proposal change: the same rows stay rejected, and nothing moves.
+    proc, out = _run_cli(tmp_path, export, snapshot, {"schema": P}, "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    noop = json.loads((out / "simulation.json").read_text())
+    assert noop["baseline"] == base
+    assert (noop["totals"]["newly_served"], noop["totals"]["route_changed"]) == (0, 0)
+
+
+def test_live_round_trip_scenarios_show_the_rescued_rejections(tmp_path):
+    """The DGX lab's `raise-request-budget` scenario, which reported 0 newly
+    served: on a live export it now reports the rejections it rescues, next
+    to a no-op scenario that reports none."""
+    export, snapshot = _live_traffic_with_rejections()
+    proc, out = _run_cli_scenarios(
+        tmp_path, export, snapshot,
+        _scenarios(("no-change", {}), ("raise-request-budget", _raise_live_cap())),
+        "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    doc = json.loads((out / "comparison.json").read_text())
+    assert doc["baseline"]["outcome_agreement"] == 1.0
+    assert doc["baseline"]["recorded_rejected_but_routed"] == 0
+    assert [(row["name"], row["newly_served"]) for row in doc["table"]] == \
+        [("no-change", 0), ("raise-request-budget", 3)]
+    assert "**Outcome agreement: 100.0%** over 7 simulated rows" in \
+        (out / "report.md").read_text()
+
+
+def test_live_export_stripped_of_routing_tokens_excludes_the_rejections(tmp_path):  # R6
+    """The same live export as a pre-fix deployment would have written it
+    (no `routing_tokens` key): the three rejections are excluded and counted,
+    and the raised cap rescues nothing -- never a silent 0 over rows that
+    were quietly replayed as empty prompts."""
+    export, snapshot = _live_traffic_with_rejections()
+    for line in export:
+        del line["outcome"]["routing_tokens"]
+    proc, out = _run_cli(tmp_path, export, snapshot, {"schema": P, **_raise_live_cap()},
+                         "--min-agreement", "1.0")
+    assert proc.returncode == 0, proc.stderr
+    sim = json.loads((out / "simulation.json").read_text())
+    assert sim["rows"]["simulated"] == 4
+    assert sim["rows"]["excluded"]["missing_routing_tokens"] == 3
+    assert sim["baseline"]["recorded_rejected"] == 0
+    assert sim["totals"]["newly_served"] == 0
````

- [ ] **Step 2: Run them — expected PASS**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_policy_simulate_cli.py -k live -p no:warnings -q -rf
```

Expected: 7 passed (the four existing live round trips and the three new ones).

- [ ] **Step 3: Watch the acceptance test fail without the replay fix**

Put the three replay files back to how Task 2 left them (serving records the count, replay does not use it yet), run the acceptance test, then restore them. Do not commit the intermediate state.

```bash
TASK2="$(git log --format=%H --grep='record the routing token count on every outcome row' -1)"
git checkout "$TASK2" -- app/eval/simulate.py app/eval/scenarios.py scripts/policy_simulate.py
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval/test_policy_simulate_cli.py -k live_round_trip_budget_rejections -p no:warnings -q -rf
git checkout HEAD -- app/eval/simulate.py app/eval/scenarios.py scripts/policy_simulate.py
git status --short
```

Expected: 1 failed, `test_live_round_trip_budget_rejections_replay_as_rejected`, at `assert sim["totals"]["newly_served"] == 3` with `assert 0 == 3`. That is B60 as the DGX lab saw it: the raised cap rescues nothing, because replay had already routed the rejected requests. After the restore, `git status --short` lists only `tests/eval/test_policy_simulate_cli.py`.

- [ ] **Step 4: Run the eval suite and the serving outcome tests**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/eval tests/test_outcome_routing_tokens.py tests/test_outcome_hooks.py tests/test_openai_compat.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_policy_simulate_cli.py
git commit -m "$(cat <<'MSG'
test(eval): live round trip with budget rejections replays as rejected (B60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 7: Documentation (spec §11)

**Files:**
- Modify: `docs/reference/routing-outcomes.md`, `docs/reference/policy-simulator.md`, `docs/backlog/bugs.md`, `docs/research/feasible-route-bench.md`, `docs/research/arxiv.md`, `docs/roadmap/priorities-1-3-status.md`

**Interfaces:**
- Consumes: the names and behaviour Tasks 1–6 built. Every test name these documents cite must exist; Step 7 checks that.
- Produces: no code.

**Rules.**

- **Never invent a number.** Every result in research §19.4 and in `arxiv.md` is written **TO MEASURE**. The experiment is the DGX lab re-run of the 2026-10-06 scenarios on rows recorded after the upgrade. It is not part of this plan.
- The docs gate (`scripts/check_docs_links.py`, run by `tests/test_docs_links.py`) refuses the purged identifier for the old bundled test model in any file under `docs/` outside `docs/archive/` and `docs/superpowers/`. The patches below do not use it; do not add it.
- The roadmap row's PR column says `pending`. The pull request does not exist yet. Whoever opens it replaces `pending` with its number in a follow-up docs commit, as `dd79a26` did for P3c-2. Leave the "As of" line at the top of that file alone until then.
- Apply each patch with `git apply`. If one does not apply because a file moved under you, make the same edit by hand and keep the wording.

- [ ] **Step 1: `docs/reference/routing-outcomes.md`** — the upgrade note with both `ALTER`s (§1), two rows and a paragraph in the content-free section (§2), the column and the `prompt_tokens` row (§3.2), cache hits (§3.4), the export example (§7).

Patch `docs/reference/routing-outcomes.md`:

````diff
diff --git a/docs/reference/routing-outcomes.md b/docs/reference/routing-outcomes.md
index f2e1859..48460a2 100644
--- a/docs/reference/routing-outcomes.md
+++ b/docs/reference/routing-outcomes.md
@@ -81,6 +81,39 @@ ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS conflicting_core JSON;
 Rows written before the ALTER keep the column null, same as any `no_eligible` row the
 router's core computation itself failed to explain (`app/routing/router.py::_safe_core`).
 
+**Upgrade: `routing_tokens` (B60, 2026-10-06).** `routing_outcomes` gained one nullable
+integer column (§3.2). `create_all` never adds a column to a table that already exists, so a
+database that already has `routing_outcomes` (the Spark, the DGX lab) needs it added by hand.
+**Run this before rolling the image.** The application still boots against an unmigrated
+table and still serves: the ORM names the column in every `INSERT` and `SELECT` on the
+table, so every outcome write fails (swallowed, logged as `outcome record failed`, so serving
+continues, but no rows are recorded) and reads of the table fail until the ALTER runs. The
+admin export returns an empty stream: the error is raised after the `200` header has been
+sent, so it shows in the server log, not in the status code. The `/v1/feedback` outcome
+lookup errors.
+
+PostgreSQL:
+
+```sql
+ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS routing_tokens INTEGER;
+```
+
+SQLite (it has no `IF NOT EXISTS` for `ADD COLUMN`; a second run fails with
+`duplicate column name`, which is harmless):
+
+```sql
+ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER;
+```
+
+Rows written before the ALTER keep the column null. The policy simulator replays a served
+row from before it as it always did, and **excludes** a rejected or failed one, counted under
+`missing_routing_tokens` (`docs/reference/policy-simulator.md` §6).
+
+That the application boots and serves against the unmigrated table, records nothing, and
+records again after the SQLite statement is pinned by
+`tests/test_outcome_routing_tokens.py::test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it`
+(a SQLite file, three real boots). The PostgreSQL statement is not run by any test.
+
 ## 2. Content-free by contract
 
 Both tables hold **ids, enum values, numbers, entity types and version hashes only.** They
@@ -95,7 +128,8 @@ Enforced by:
 
 | What | Test |
 |---|---|
-| No column on either table has a name that suggests text (`prompt_text`, `completion`, `content`, `message`, `comment`, `corrected`, `reason`, `error`); token counts are the only exemptions | `tests/test_outcome_store.py::test_schema_is_content_free` |
+| No column on either table has a name that suggests text (`prompt_text`, `completion`, `content`, `message`, `comment`, `corrected`, `reason`, `error`); token counts are the only exemptions. `routing_tokens` is a nullable integer | `tests/test_outcome_store.py::test_schema_is_content_free` |
+| A request **rejected** for its size, carrying a marker, an SSN and an e-mail address, writes a row with none of them; `routing_tokens` is an integer count | `tests/test_outcome_routing_tokens.py::test_r1_rejected_row_with_pii_carries_a_count_and_no_content` |
 | A served request that contains an SSN and an e-mail address writes a row with neither value in any column; the entity **types** remain | `tests/test_outcome_hooks.py::test_outcome_row_carries_no_request_content` |
 | The builder copies no text from the response or the trace | `tests/test_outcome_builder.py::test_build_ok_outcome_is_content_free` |
 | `rejected` keeps `name` and `code` only, never `reason` | `tests/test_outcome_builder.py::test_build_failure_outcome_no_eligible_stores_only_name_and_code` |
@@ -103,9 +137,15 @@ Enforced by:
 | Feedback `comment` and `corrected_output` text is never stored locally | `tests/test_outcome_feedback.py::test_comment_and_corrected_output_text_never_stored_locally` |
 | `workload` and categorical feedback values are regex-validated slugs of at most 64 characters | `tests/test_outcome_builder.py::test_parse_workload_*`, `tests/test_outcome_feedback.py::test_bad_categorical_value_is_refused` |
 
-The SSN/e-mail test covers a served (`ok`) row. Failure rows are built from the same
-outcome context and from candidate names and codes only, and the builder tests cover them,
-but no end-to-end test sends PII through a failing request.
+Failure rows are built from the same outcome context as served ones, and from candidate
+names and codes only. End-to-end tests send PII through a served request, a `no_eligible`
+request and a `verify_failed` one.
+
+**Token counts are lengths, not content.** `prompt_tokens`, `completion_tokens`,
+`prompt_tokens_bucket`, `complexity` and `routing_tokens` say how large a request was and
+nothing about what it said. They are a side channel on length only. Since `routing_tokens`
+(B60) that length is recorded for rejected requests too, where before only the capped
+`complexity` was.
 
 The one field a client controls is `workload`. The slug format limits what fits there, but
 it cannot stop a client from encoding personal data in a slug. Clients must not do that
@@ -166,6 +206,7 @@ filled on it exactly as on an `ok` row.
 | `workload_source` | enum | `client` (a valid tag was supplied), `none` (nothing supplied) or `invalid` (supplied but rejected; `workload` is then null). |
 | `tier_target` | str, nullable | The **first routing decision's** own target tier (§3.5). A failed gateway hop records it too, so after a failover it is the failed attempt's target; it is never borrowed from a later hop (a trace from before 2026-09-23 whose failed hop lacks it reads null). The target is the tier after the complexity rule, risk floor, escalation and the clamp to the best residency-allowed tier, and **before** the budget fallback. The served tier can be lower when the budget fallback stepped down. Null on failure rows other than `verify_failed`. |
 | `complexity` | float, nullable | The router's complexity input, `tokens_after / 2000` (post-compression tokens). Written on failure rows too when compression ran before the failure. Null on a semantic-cache hit. |
+| `routing_tokens` | int, nullable | The token count the router was given: `tokens_after`, RouteForge's **own** count of the model-facing messages (and, on a tool conversation, the replayed tool-call arguments and the tool schemas) after the gate and after step-5 compression. It is the number `complexity` is derived from, the per-request budget is checked against and the cost term of the score is computed from. **Not the backend's count**: that is `prompt_tokens`, and the two differ on almost every request. Written on **every** status, rejected rows included, so the policy simulator can replay a rejected request at its real size. Null on a semantic-cache hit (no router ran) and on rows written before the column existed. A count, never text (§2). Added 2026-10-06 (B60). |
 | `prompt_tokens_bucket` | enum, nullable | `<1k`, `1k-4k`, `4k-16k`, `16k-64k` or `>64k`, over the response's `usage.prompt_tokens`. Lower bound inclusive: exactly 1000 tokens is `1k-4k`. Null on failure rows other than `verify_failed`. |
 | `sensitivity` | enum | `public`, `internal`, `confidential` or `restricted`, from the gate. |
 | `risk_class` | str | From the gate. |
@@ -182,7 +223,7 @@ filled on it exactly as on an `ok` row.
 | `cache_hit` | bool | True when a semantic-cache entry served the request (§3.4). False otherwise, and on every failure row. |
 | `cost_usd` | float | The total cost across hops (`cost.total_usd`). On `verify_failed` rows the served attempt's cost, which was incurred. `0` on other failure rows. |
 | `latency_ms` | float | **Two definitions.** `ok` rows: the sum of the trace records' `latency_ms` (the same figure the usage row carries). Failure rows: wall-clock time from the start of the request handler to the failure, which includes authentication and gate time. Do not pool the two. |
-| `prompt_tokens`, `completion_tokens` | int | From the response `usage` (on `verify_failed`, the served attempt's). `0` on other failure rows. |
+| `prompt_tokens`, `completion_tokens` | int | The **backend's** billed counts, from the response `usage` (on `verify_failed`, the served attempt's). `0` on other failure rows: no backend answered, so nothing was billed. For how large such a request was, read `routing_tokens`. |
 | `verify_required`, `verify_performed`, `verify_passed` | bool, nullable | From the trace's `verify` record. On `verify_failed` rows `true`, `true`, `false`. Null on other failure rows. |
 | `verify_mode` | enum, nullable | See below. `failed` on `verify_failed` rows; null on other failure rows. |
 | `conflicting_core` | JSON, nullable | A `no_eligible` row's minimal conflicting core (P3b): `ConflictingCore.to_record()` — requirement ids/`security`/`source`/`detail`, endpoint state, `candidates_considered`; no `label`, no `hints`. Content-free. Null on every other status, and null when the router's own core computation failed (`core=None`). See `docs/reference/conflicting-core.md`. Added 2026-09-25 (P3b). |
@@ -221,8 +262,8 @@ the budget case; see `docs/reference/conflicting-core.md`.
 
 A request served from the semantic cache is recorded as `ok`, but its trace has no router
 hop (only the `verify` and `cache` records). So a cache-hit row has `cache_hit: true`, and
-`chosen`, `tier_target`, `scoring` and `complexity` are null, and `eligible` and `attempts`
-are `[]`. The model that produced the cached answer is not recorded. The report puts these
+`chosen`, `tier_target`, `scoring`, `complexity` and `routing_tokens` are null (the hit
+returns before compression), and `eligible` and `attempts` are `[]`. The model that produced the cached answer is not recorded. The report puts these
 rows in their own chosen bucket, `(cache)`: never in an endpoint's bucket (that endpoint
 did not answer), and never in `(none)` (the request was served).
 
@@ -370,7 +411,7 @@ per page by `(application_id, request_id)`, never by `request_id` alone.
              "attempts": ["qwen3.8-27b"], "chosen": "qwen3.8-27b",
              "scoring": {"weights": {…}, "candidates": [{"name": "qwen3.8-27b", "score": …, …}, …], …},
              "circuit_bypass": false, "cache_hit": false, "cost_usd": 0.00021, "latency_ms": 3620.4,
-             "prompt_tokens": 812, "completion_tokens": 140,
+             "prompt_tokens": 812, "completion_tokens": 140, "routing_tokens": 820,
              "verify_required": true, "verify_performed": true, "verify_passed": true,
              "verify_mode": "verified"},
  "feedback": [{"score_name": "quality", "value": 0.8, "source": "app",
````

- [ ] **Step 2: `docs/reference/policy-simulator.md`** — the third per-row field (§1.1), `--min-agreement` and exit 4 (§3), `rows` and `baseline` (§4.1), the report order (§4.2), outcome agreement and the gate (§5), the B60 limit replaced by what is now true and what remains (§6), the comparison document and its report (§9.4, §9.5).

Patch `docs/reference/policy-simulator.md`:

````diff
diff --git a/docs/reference/policy-simulator.md b/docs/reference/policy-simulator.md
index 4e4d8d3..4067883 100644
--- a/docs/reference/policy-simulator.md
+++ b/docs/reference/policy-simulator.md
@@ -56,6 +56,16 @@ per-request budget check sees the real cap. Both are null for an unpinned or unc
 request. **Rows exported before these fields existed lack both keys**; such a row is still
 simulated, unpinned and at the deployment's default output-token estimate.
 
+A third per-row field makes a **rejected** request replayable (B60, 2026-10-06):
+`routing_tokens` — the token count the router was given (RouteForge's own post-gate,
+post-compression count). Replay hands it to `Router.select`, so the per-request budget check
+sees the size the live check saw. It is recorded on every status. `prompt_tokens` is a
+different number — the backend's billed count, zero on a row no backend answered — and replay
+still prices a served row from it (§6). **A rejected or failed row exported before
+`routing_tokens` existed has no record of its size at all**, and is excluded, counted as
+`missing_routing_tokens`, rather than replayed as an empty prompt. A served row from before
+it replays as it always did.
+
 ### 1.2 The config snapshot
 
 Everything a routing decision reads — settings, the onboarded pool, per-app policy,
@@ -233,7 +243,7 @@ uv run python scripts/policy_simulate.py \
 | `--max-relaxations` | `50` | cap on relaxations `--shadow-prices` evaluates, must be `>= 1` (exit `2` otherwise) |
 | `--scenarios` | off | a `routeforge.policy-scenarios/v1` file; compares its scenarios side by side (§9). Cannot be combined with `--proposal` or `--shadow-prices` (exit `2`) |
 | `--since` | off | ISO-8601 UTC timestamp. Rows with `ts` before it are excluded and counted as `before_since`. Applies to every mode. A value with no offset is read as UTC |
-| `--min-agreement` | off | `0 ≤ X ≤ 1`. If baseline agreement (§5) is not computable or is below `X`, print it and exit `4` before any file or directory is written. Applies to every mode |
+| `--min-agreement` | off | `0 ≤ X ≤ 1`. Compared with the **lower** of the two baseline agreements (§5): model agreement and outcome agreement. If either is not computable, or the lower one is below `X`, print both and exit `4` before any file or directory is written. Applies to every mode |
 
 `--proposal` is required unless `--shadow-prices` or `--scenarios` is given — with none of
 the three, the CLI exits `2` (an argparse usage error, since there is nothing to simulate).
@@ -245,7 +255,7 @@ the three, the CLI exits `2` (an argparse usage error, since there is nothing to
 | `0` | `simulation.json` and `report.md` written (`--proposal`, or `--shadow-prices` alone); `comparison.json` and `report.md` written (`--scenarios`, §9.4) |
 | `2` | an input failed to load, validate or apply — a bad export, an invalid proposal (unknown key, bad value, a container of the wrong type), a malformed snapshot (a missing or mistyped nested key, named by its dotted path), a snapshot that loads structurally but fails **deep** validation during `simulate()`/`shadow_prices()` (for example a provider the running deployment's own `config/` never declared), a `--config-dir` that cannot load (missing key, bad YAML, a failed config lint), neither `--proposal` nor `--shadow-prices` given, `--max-relaxations < 1`, or a shadow-prices eligible-superset invariant breach (`ShadowInvariantError`, printed as `error: internal invariant: ...` — spec §3; a relaxation narrowed some row's eligible set, which should never happen since every relaxation only widens eligibility, so it means a bug, not a reportable finding). A relaxation that newly **rejects** a baseline-routed row is *not* a breach — it is reported as `newly_rejected` (§8.4) and exits `0`. `--scenarios` combined with `--proposal` or `--shadow-prices` (an argparse usage error), neither `--proposal` nor `--shadow-prices` nor `--scenarios` given (also an argparse usage error), an invalid scenarios file (named `scenarios[<i>](<name>).proposal.<path>`), a `--since` that is not a timestamp, a `--min-agreement` outside `[0, 1]`, and a `--scenarios` comparison invariant breach (`ComparisonInvariantError`, printed as `error: internal invariant: ...` — two scenarios disagreed about the rows, excluded counts or baseline they share by construction, which means a bug, never a reportable result) are also exit `2`. Nothing is written; the offending location is on stderr — never a traceback. |
 | `3` | the proposal (or any scenario) introduces new rejections and `--fail-on-new-rejections` was passed |
-| `4` | baseline agreement is below `--min-agreement`, or there is no comparable row. Nothing is written |
+| `4` | the lower of model agreement and outcome agreement (§5) is below `--min-agreement`, or there is no comparable row. Nothing is written. The message names both rates and both outcome-disagreement counts |
 
 The CLI is file-based only: no DB, no `app.api` import, no network
 (`tests/eval/test_isolation.py` pins that `app/eval` is never imported by serving code, and
@@ -267,8 +277,8 @@ the export window).
 | `simulation_id` | first 16 hex characters of the sha256 over the canonical `inputs` block |
 | `inputs` | `export_sha256`, `snapshot_sha256`, `proposal_sha256`, `proposal_title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy` (the four posture values every app was compiled under — the snapshot's), `window` (`{start, end}` over the simulated rows' `ts`, or `null`), `since` (the `--since` cut as an ISO string, or `null`) |
 | `edits` | the proposal, minus `schema`/`title`, keys sorted — exactly what was asked for |
-| `rows` | `simulated` (count) and `excluded` (count per reason: `before_since` (first), `cache_hit`, `unknown_status`, `unknown_app`, `missing_features`). `missing_features` counts a row whose `request_id`, `complexity`, `prompt_tokens` (numeric), `completion_tokens` (numeric or null), `detected_entity_types` (list of strings or null; the key must be present), `workload`, `attempts` or `ts` is missing or mistyped, or whose `requested_model` (non-empty string or null) or `max_tokens` (integer > 0 or null) is mistyped — these two may be absent (an older export) — a malformed row is counted, never a crash. A non-object export line is a skipped-line warning. |
-| `baseline` | the fidelity check — `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`, `overflow_contradicted` (§5) |
+| `rows` | `simulated` (count), `estimated_tokens` (simulated rows with no backend token count, sized and priced from `routing_tokens` — §6) and `excluded` (count per reason, first match wins: `before_since`, `cache_hit`, `unknown_status`, `unknown_app`, `missing_features`, `missing_routing_tokens`). `missing_routing_tokens` counts a well-formed row that records no size: not `ok`, no `routing_tokens`, and `prompt_tokens` zero — a rejected or failed row from before B60. A mistyped `routing_tokens` (anything but an integer ≥ 0 or null) is `missing_features`. `missing_features` counts a row whose `request_id`, `complexity`, `prompt_tokens` (numeric), `completion_tokens` (numeric or null), `detected_entity_types` (list of strings or null; the key must be present), `workload`, `attempts` or `ts` is missing or mistyped, or whose `requested_model` (non-empty string or null) or `max_tokens` (integer > 0 or null) is mistyped — these two may be absent (an older export) — a malformed row is counted, never a crash. A non-object export line is a skipped-line warning. |
+| `baseline` | the fidelity check (§5). Model agreement: `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`, `overflow_contradicted`. Outcome agreement: `outcome_rows_compared`, `outcome_agreement`, `outcome_warning`, `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected`, and `outcome_by_app.<id>` (`rows`, `agreement` and the same three counts) |
 | `totals`, `by_app.<id>`, `by_workload.<label>` | each the same block: `requests`, `route_changed`, `newly_rejected`, `newly_served`, `cost_usd` (`{current, proposed, delta}` — each arm's routed rows), `cost_usd_both_routed` (same shape, over only the rows routed in **both** arms), `latency_ms.p50`/`.p95` (same shape), `quality_mean` (same shape), `onprem_share` (`{current, proposed}`), `overflow_hops` (`{current, proposed}`) |
 | `moves` | top 20 `{from, to, count}` model-pair moves; `(rejected)` is a legal `from`/`to` value |
 | `new_rejections` | `{by_app: {...}, by_code: {...}, by_core: {...}}` — rows that were `routed` under `current` and `rejected` under `proposed`. `by_core` (P3b) groups by the `proposed` arm's minimal conflicting core: requirement ids joined with `+` (e.g. `"residency+requested_model"`), in `REQUIREMENT_ORDER`; when the core itself is empty it falls back to `"(state:" + ",".join(core_state) + ")"`; when both are empty (the router's own core computation failed) it is `"(unknown)"`. See `docs/reference/conflicting-core.md`. |
@@ -287,7 +297,9 @@ Opens with the caveat, verbatim, as a blockquote:
 > eligibility and routing code and a frozen snapshot of signals. It does not model other
 > failover, escalation, breaker state or caching.
 
-Then, in order: a baseline warning (only if `agreement < 0.90`), an **Assumptions** section
+Then, in order: one line stating both baseline agreements (always printed), a baseline
+warning for each agreement that is below 0.90, a note for every recorded outcome replay
+does not reproduce and for rows sized from an estimate (§5), an **Assumptions** section
 (only when the proposal sets `workload` or `latency_ms`), **Headline** (the `totals`
 metrics table), **New rejections** (by app, by code, then **by conflicting core** — a
 `| core | count |` table, sorted by count descending then core key — or "None." when there
@@ -322,9 +334,39 @@ though the first picks still match. That subset is reported separately as
 `overflow_contradicted`: it is a fidelity check on the endpoint's declared `context_window`
 (or the default output-token estimate, when `max_tokens` was not recorded), not an ordinary
 routing disagreement, and `report.md` prints it next to the agreement banner whenever it is
-non-zero. `--min-agreement X` turns this check into a gate (exit `4`), and `--since TS` is the
-way to cut an export at the first row a replay can be trusted for — for example the first row
-recorded after B55.
+non-zero. `--since TS` is the way to cut an export at the first row a replay can be trusted for — for
+example the first row recorded after B55.
+
+**Outcome agreement (B60).** Model agreement looks only at `ok` rows, so it cannot see a
+request the record says was *rejected* and replay *routes*. A second measure covers that.
+Over **every simulated row**, the `current` arm's outcome — routed or rejected — is compared
+with the class of the recorded status:
+
+| Recorded `status` | Class | Why |
+|---|---|---|
+| `ok` | routed | served |
+| `verify_failed` | routed | a model was chosen and answered; step 9 refused the answer |
+| `backend_failed` | routed | a model was chosen; every attempt failed upstream |
+| `no_eligible` | rejected | the router found no candidate at the first decision |
+| `context_overflow` | rejected | every attempt was refused for size; replay models that as a `context_window` rejection |
+
+`outcome_agreement` is the share that match. Two counts say which way it went wrong:
+`recorded_rejected_but_routed` (out of `recorded_rejected`) and
+`recorded_routed_but_rejected`. All of it is also broken down per app (`outcome_by_app`).
+
+Every report states both agreements, on a clean run too. A rate below 90% prints a warning. **Any
+non-zero count prints a note with its denominator** ("2 of 7 recorded rejections replay as
+routed"), whatever the rate is: rejections are usually a small share of traffic, so every one
+of them can replay wrong while `outcome_agreement` stays above 90%. The count out of the
+rejected rows is the number that says whether a "newly served" figure can be trusted.
+
+**`--min-agreement X` is a gate on the lower of the two** (exit `4`, nothing written). Model
+agreement catches a replay that picks a different model (B55); outcome agreement catches one
+that serves a rejected request (B60). A replay is fit to answer "what would change" only if
+it reproduces both, so neither is allowed to hide behind the other, and they are not pooled.
+Both must be computable: with no served row, nothing vouches for which model replay picks,
+however well the outcomes agree. Outcome agreement is taken over all simulated rows, so it is
+diluted as described above; `--min-agreement 1.0` refuses any disagreement.
 
 **What lowers it:**
 
@@ -337,6 +379,11 @@ recorded after B55.
   replayed unpinned to a different model.
 - **Circuit-breaker state** — a request served while a circuit was open replays with no open
   circuits (§6).
+- **A budget, a declared context window or an endpoint's state changed since the traffic was
+  recorded** — a request rejected then can route now, and the reverse. This lowers *outcome*
+  agreement.
+- **A recorded `context_overflow` on an endpoint with no declared `context_window`** — replay
+  cannot predict an overflow it has no limit for, so the row replays as routed (§6).
 
 **The headline diff itself is never logged-vs-simulated.** Both `current` and `proposed` are
 simulated arms sharing one signals snapshot, so drift and fidelity loss affect both sides
@@ -354,12 +401,26 @@ From spec §2, plus what the build settled during P3a:
   `docs/reference/conflicting-core.md`. Shadow prices (P3c-1, §8 below) now price what each
   individual constraint costs. Alternative scenarios are §9 (P3c-2). **A capacity model and
   uncertainty ranges** remain out of scope (P3c-3, P3c-4).
-- **Rejected rows replay with zero prompt tokens (B60).** A row recorded `no_eligible` or
-  `context_overflow` carries `prompt_tokens: 0`, so replay prices and sizes it as an empty
-  prompt and the `current` arm routes it. A scenario that raises a per-request budget or a
-  context window therefore cannot show the requests it would rescue, and baseline agreement
-  (computed over `ok` rows) does not flag the mismatch. First observed live on 2026-10-06
-  (`docs/research/feasible-route-bench.md` §18.4.1).
+- **Two token counts, used for two things (B60, fixed 2026-10-06).** The router is given the
+  row's `routing_tokens` — RouteForge's own count, the number the live router was given — so
+  the per-request budget check and the cost term of the score see what they saw live. The
+  routed call is priced, and the context-window check sized, at `prompt_tokens`, the
+  backend's billed count. A row from before `routing_tokens` existed uses `prompt_tokens`
+  for both, as before.
+- **A row no backend answered is an estimate.** A rejected row, or one that failed before any
+  response, has `prompt_tokens: 0` and no completion count. When an arm routes it, replay
+  prices it at its `routing_tokens` and at the router's output estimate (its recorded
+  `max_tokens`, else `default_estimated_output_tokens`) — the same pair the live per-request
+  budget check priced it at. So a request a proposal rescues is costed at its admission
+  estimate, which can overstate what it would really have cost. `rows.estimated_tokens`
+  counts these rows and the report says so. A `backend_failed` row is routed in both arms and
+  costed as if the call had succeeded; the failure itself is not replayed.
+- **Rejected and failed rows recorded before B60 are excluded**, counted as
+  `missing_routing_tokens`. They record no size, and replaying them as empty prompts is what
+  made a raised budget show nothing to rescue (first observed live on 2026-10-06,
+  `docs/research/feasible-route-bench.md` §18.4.1). **This changes the output for an export
+  that contains such rows**: fewer rows are simulated than before, and totals move. To
+  simulate rejected requests, use rows recorded after the upgrade (`--since`).
 - **No re-running PII detection.** Recorded `detected_entity_types` are replayed as-is, so a
   proposal cannot change `pii_detection`, a denylist, exempt patterns or recognizers.
 - **No daily-spend or RPM admission simulation** — those are admission limits, not routing.
@@ -371,7 +432,10 @@ From spec §2, plus what the build settled during P3a:
   declared `context_window` is never enforced ahead of time — overflow is the backend's own
   refusal, on its real tokenisation and its real limit. Replay has no backend to ask, so it
   predicts that refusal instead, from data the row and the snapshot already carry:
-  1. `prompt_tokens` is the backend's own recorded count for the row — not an estimate.
+  1. `prompt_tokens` is the backend's own recorded count for the row — not an estimate. For
+     a row no backend answered it is the row's `routing_tokens`, which is RouteForge's count
+     and not the backend's, so a request the backend refused by a small margin can replay as
+     fitting.
   2. `output_tokens` is the row's recorded `max_tokens`, or the deployment's
      `default_estimated_output_tokens` when none was recorded. So an uncapped request that fit
      live can still overflow in replay, when the default estimate overstates what the client
@@ -393,9 +457,10 @@ From spec §2, plus what the build settled during P3a:
   that same pick as an overflow is a sign the endpoint's declared window understates its real
   one (or no `max_tokens` was recorded and point 2 above overstates the request) — §5 reports
   that subset separately as `overflow_contradicted`.
-- **Recorded `context_overflow` rows are still not reproduced.** A failure row records
-  `prompt_tokens: 0`, so replay sees a prompt that fits any window. Rows that were served
-  carry their real token count and are replayed faithfully.
+- **A recorded `context_overflow` row is reproduced only when the endpoint declares a
+  window.** The row now carries its size (`routing_tokens`), so a declared `context_window`
+  it exceeds rejects it in replay too. An undeclared window never overflows, so the row
+  replays as routed and is counted in `recorded_rejected_but_routed` (§5).
 - **Cached tokens are an assumption, not a record.** Outcome rows do not record cached
   prompt tokens. Both arms price none as cached unless a proposal sets `workload.cached_share`.
 - **Pinned-model rejections are not in the input.** A pin the app may not use is a
@@ -690,7 +755,7 @@ places.
 |---|---|
 | `comparison_id` | first 16 hex characters of the sha256 over `inputs` |
 | `inputs` | `export_sha256`, `snapshot_sha256`, `scenarios_sha256`, `title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy`, `since`, `window` |
-| `rows` | `simulated` and `excluded` — the same for every scenario |
+| `rows` | `simulated`, `estimated_tokens` and `excluded` — the same for every scenario |
 | `baseline` | the fidelity block of §5, computed once from the shared `current` arm |
 | `scenarios` | file order. Each: `name`, `edits`, `assumptions`, and `summary` — the §4.1 body (`totals`, `by_app`, `by_workload`, `moves`, `new_rejections`, `affected_apps`, `sources`, `per_day`) |
 | `table` | file order. One row per scenario: `name`, `cost_per_day_delta_usd`, `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`, `overflow_hops`, `affected_apps` |
@@ -707,8 +772,10 @@ gives for the same body.
 
 ### 9.5 `report.md`
 
-In order: the §4.2 caveat; the title; a **baseline agreement** banner (overall rate, a ⚠
-warning under 90%, the rate per app, and the `--since` cut when one was given); **one
+In order: the §4.2 caveat; the title; a **baseline agreement** banner (model agreement and
+outcome agreement, each with its overall rate, a ⚠ warning under 90% and the rate per app;
+a note for every recorded outcome replay does not reproduce and for rows sized from an
+estimate; and the `--since` cut when one was given); **one
 comparison table** in file order, followed by the sentence "Listed in file order. RouteForge
 does not rank or recommend scenarios." and the like-for-like note; an **Assumptions** line
 for each scenario that sets `workload` or `latency_ms`; a short section per scenario
````

- [ ] **Step 3: `docs/backlog/bugs.md`** — B60 resolved, with the evidence.

Patch `docs/backlog/bugs.md`:

````diff
diff --git a/docs/backlog/bugs.md b/docs/backlog/bugs.md
index 2ad4c9e..812c878 100644
--- a/docs/backlog/bugs.md
+++ b/docs/backlog/bugs.md
@@ -144,7 +144,7 @@ then stored in `onboarded_models.api_key_env` in clear text and displayed in the
 var set in the API pod?") instead of a bare "unreachable"; (4) say next to the slug field that
 it must equal the model id the endpoint serves.
 
-## B60 — Rejected outcome rows record `prompt_tokens: 0`, so the policy simulator replays live-rejected requests as routed  🔴 OPEN
+## B60 — Rejected outcome rows record `prompt_tokens: 0`, so the policy simulator replays live-rejected requests as routed  ✅ RESOLVED (2026-10-06)
 
 **Severity:** medium-high for the simulator (wrong answer for a whole scenario class; no
 effect on serving).
@@ -171,6 +171,37 @@ served all 7. Baseline agreement stayed 1.0, because `_baseline` compares `ok` r
 3. Until then: document in `docs/reference/policy-simulator.md` §6 that rejected rows replay
    with zero tokens and that budget and context-window scenarios cannot show rescued requests.
 
+**Resolution.** Design: `docs/superpowers/specs/2026-10-06-replayable-rejections-design.md`.
+
+- **Serving.** `RoutingOutcome` has `routing_tokens` (`INTEGER`, nullable): the `tokens_after`
+  the router was given. `Pipeline.run` and `run_stream` put it on the outcome context on the
+  same line as `complexity`, and `app/apps/outcomes.py::_policy_features` copies it onto
+  every row, served or rejected. `prompt_tokens` is unchanged: the backend's billed count,
+  zero when no backend answered. Nothing is added to any response. A deployed database needs
+  one `ALTER` before the image is rolled (`docs/reference/routing-outcomes.md` §1).
+- **Replay.** `app/eval/simulate.py::replay_tokens` gives the router the row's
+  `routing_tokens` and prices the call at the backend's `prompt_tokens`; a row no backend
+  answered is sized and priced at its routing tokens and the router's output estimate, and
+  counted in `rows.estimated_tokens`. A rejected or failed row with no recorded size (written
+  before the fix) is excluded as `missing_routing_tokens`, never replayed as an empty prompt.
+- **Agreement.** `_baseline` reports outcome agreement next to model agreement, with
+  `recorded_rejected_but_routed` and `recorded_routed_but_rejected`, overall and per app.
+  Both reports show it, and `--min-agreement` gates on the lower of the two.
+
+**Evidence.**
+`tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected`
+sends three requests that a real app rejects for `max_cost_usd_per_request` *because of their
+size*, exports them with the snapshot, and replays: the `current` arm rejects all three,
+outcome agreement is 1.0, and a proposal raising the cap reports 3 newly served.
+`tests/eval/test_replay_rejections.py::test_r6_unsized_rows_are_excluded_and_counted_never_routed`
+pins the exclusion of pre-fix rows. The DGX lab re-run of the 2026-10-06 scenarios is recorded
+in `docs/research/feasible-route-bench.md` §19.4 when it is done.
+
+**Still open, by design** (`docs/reference/policy-simulator.md` §6): a recorded
+`context_overflow` is reproduced only when the endpoint declares a context window; a rescued
+request's cost is the admission estimate; failover, escalation, breaker state and
+daily-spend/RPM admission are not replayed.
+
 ## B59 — FEATURE: per-app tokens-per-minute / concurrency limits, and a limiter shared across replicas  🔴 OPEN
 
 **Severity:** medium (fairness on a shared model; no effect on privacy or routing correctness).
````

- [ ] **Step 4: `docs/research/feasible-route-bench.md`** — new §19: the model, why the count sits beside the rate, invariants → tests, results TO MEASURE, limits.

Patch `docs/research/feasible-route-bench.md`:

````diff
diff --git a/docs/research/feasible-route-bench.md b/docs/research/feasible-route-bench.md
index 5dc44d5..e91d93e 100644
--- a/docs/research/feasible-route-bench.md
+++ b/docs/research/feasible-route-bench.md
@@ -2546,3 +2546,113 @@ a per-request model. Nothing here supports a claim about routing quality or savi
 The triad needs at least two eligible endpoints with different placement (an on-prem and a
 cloud or second on-prem endpoint) and persona traffic with PII; the DGX lab has one endpoint
 today.
+
+## 19. Replayable rejections: the routing token count on the record (B60)
+
+§18.4.1 found that the simulator replays a live-rejected request as routed, and that the
+baseline check did not notice. This section records the fix. Design:
+`docs/superpowers/specs/2026-10-06-replayable-rejections-design.md`. Reference:
+[`docs/reference/routing-outcomes.md`](../reference/routing-outcomes.md) §3.2 and
+[`docs/reference/policy-simulator.md`](../reference/policy-simulator.md) §5, §6. Code:
+`app/pipeline.py` and `app/apps/outcomes.py` (the record), `app/eval/simulate.py` (replay and
+agreement). **Not deployed**; this section reports what the code does. The results are to be
+measured (§19.4).
+
+### 19.1 The model
+
+A routing decision is a function of the request's features and the configuration. The
+simulator can replay a decision only if every feature the decision read is on the record.
+§13.1 lists what an outcome row holds. Two rounds of replay have each found a feature that
+was read by the decision and missing from the row:
+
+| Feature | Read by | Missing until | Symptom while missing |
+|---|---|---|---|
+| the client's model pin, its output cap | eligibility (the pin stage), the budget check | B55 (§15.4) | replay picked a different **model** |
+| the token count the router was given | the budget check, the cost term of the score | B60 (this section) | replay **served** a request the record says was rejected |
+
+The second gap hid behind a property of the record. `prompt_tokens` is the backend's billed
+count, so it is zero on a row no backend answered. For a served row it is a usable stand-in
+for the router's count. For a rejected row it says the prompt was empty, and an empty prompt
+fits any per-request budget and any context window.
+
+- **Two counts, two meanings.** `routing_tokens` is RouteForge's own count, made after the
+  gate and after compression and before any backend is called. It is what admission and
+  scoring read. `prompt_tokens` is what the backend counted and billed. Replay gives the
+  router the first and prices the call at the second.
+- **A row no backend answered is an estimate.** It has no billed count and no completion.
+  When an arm routes it, replay prices it at the pair the live budget check priced it at:
+  its routing tokens and the router's output estimate. That is the admission estimate, not a
+  bill, and the output counts such rows.
+- **A row with no size is not replayed.** A rejected or failed row from before the fix is
+  excluded and counted. Replaying it as an empty prompt is the defect, so there is no
+  degraded mode for it.
+- **Agreement has two parts.** Model agreement compares the first pick with the recorded
+  first attempt, over served rows. Outcome agreement compares routed-or-rejected with the
+  recorded status's class, over every simulated row. Each is blind to the other's failure:
+  B55 was invisible to an outcome check (every row still routed), and B60 was invisible to a
+  model check (it only looks at served rows).
+
+### 19.2 Why outcome agreement needs a count beside it
+
+Rejected requests are usually a small share of traffic. Outcome agreement is taken over all
+simulated rows, so every rejection can replay wrong while the rate stays high: 2 wrong out
+of 30 rows is 93%. The number that says whether a "newly served" figure can be trusted is
+the count out of the recorded rejections. So `recorded_rejected_but_routed` is reported with
+`recorded_rejected`, and a non-zero count prints a note whatever the rate is
+(`tests/eval/test_replay_rejections.py::test_diluted_outcome_agreement_still_reports_every_rejection_wrong`).
+`--min-agreement` gates on the lower of the two rates; it is diluted in the same way, and
+only `--min-agreement 1.0` refuses every disagreement. A per-class threshold was not added.
+
+### 19.3 Invariants, and the test that pins each
+
+| # | Invariant | Pinned by |
+|---|---|---|
+| R1 | The table is still content-free: `routing_tokens` is a nullable integer, and a rejected request carrying a marker, an SSN and an e-mail address writes none of them | `tests/test_outcome_store.py::test_schema_is_content_free`; `tests/test_outcome_routing_tokens.py::test_r1_rejected_row_with_pii_carries_a_count_and_no_content` |
+| R2 | A served row replays to an identical `ArmResult` when `routing_tokens == prompt_tokens`, in both arms, under any `token_scale` | `tests/eval/test_replay_rejections.py::test_r2_served_row_replay_is_unchanged_when_the_counts_coincide` (Hypothesis) |
+| R3 | A request rejected live for its per-request budget is rejected in the `current` arm under the same snapshot | `tests/eval/test_replay_rejections.py::test_r3_budget_rejected_row_is_rejected_in_the_current_arm`; live: `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected` |
+| R4 | Raising the cap in a proposal newly serves it, costed at the admission estimate | `tests/eval/test_replay_rejections.py::test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate`; live: the same round-trip test |
+| R5 | Outcome agreement is 1.0 on a live round trip that contains rejections | `tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected`, `::test_live_round_trip_scenarios_show_the_rescued_rejections` |
+| R6 | A rejected or failed row with no recorded size is excluded, counted, and reaches no arm | `tests/eval/test_replay_rejections.py::test_r6_unsized_rows_are_excluded_and_counted_never_routed`; live export: `tests/eval/test_policy_simulate_cli.py::test_live_export_stripped_of_routing_tokens_excludes_the_rejections` |
+| R7 | No serving module imports `app.eval` | `tests/eval/test_isolation.py::test_no_serving_module_imports_app_eval` |
+| R8 | The OpenAI wire shape is unchanged: success validates against the SDK model, the rejection envelope keeps exactly its keys, the new field appears in neither | `tests/test_openai_compat.py::test_outcome_recording_adds_nothing_to_the_success_wire`, `::test_outcome_recording_adds_nothing_to_the_rejection_wire` |
+| R9 | An export without the field loads, and its served rows replay as before | `tests/eval/test_replay_rejections.py::test_r9_export_without_the_field_still_loads_and_replays_served_rows` |
+| R10 | The recorded value is the `tokens_after` the router was called with, on every status; null on a semantic-cache hit | `tests/test_outcome_routing_tokens.py` (one test per status, each against a spy on `Router.select`), `::test_semantic_cache_hit_row_has_null_routing_tokens` |
+| R11 | Every status serving can write has a recorded outcome class | `tests/eval/test_replay_rejections.py::test_r11_every_status_has_a_recorded_outcome_class` |
+| R12 | `--min-agreement` takes the lower of model and outcome agreement | `tests/eval/test_replay_rejections.py::test_r12_gate_takes_the_lower_of_the_two`; `tests/eval/test_policy_simulate_cli.py::test_r12_min_agreement_exits_4_on_outcome_disagreement` |
+| R13 | An unmigrated table does not break serving, and the documented `ALTER` repairs it | `tests/test_outcome_routing_tokens.py::test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it` |
+
+R2 states exactly what "unchanged" means. When the two counts differ, a served row's route
+can legitimately differ from what replay produced before, because the router is now given
+the number it was given live.
+
+### 19.4 Results
+
+**TO MEASURE.** The experiment is the DGX lab re-run of the 2026-10-06 scenarios (§18.4.1):
+the same application, endpoint, prices and per-request cap, the same six scenarios, on rows
+recorded after the upgrade (`--since` at the first of them).
+
+| measurement | value |
+|---|---|
+| requests rejected live with core `max_cost_usd_per_request` | TO MEASURE |
+| of those, rejected by the `current` arm (`recorded_rejected` − `recorded_rejected_but_routed`) | TO MEASURE |
+| model agreement, rows compared | TO MEASURE |
+| outcome agreement, rows simulated | TO MEASURE |
+| `raise-request-budget` (cap 0.05 → 0.5): newly served | TO MEASURE |
+| `raise-request-budget`: cost of the rescued rows (admission estimate) | TO MEASURE |
+| `rows.estimated_tokens` | TO MEASURE |
+| the pre-upgrade export from §18.4.1 replayed with the new code: `missing_routing_tokens` | TO MEASURE |
+| `routing_tokens` against `prompt_tokens` on served rows (how far RouteForge's count is from the backend's) | TO MEASURE |
+
+The last row matters for the paper: replay sizes a rejected row with RouteForge's count, and
+the gap between the two counts on served rows is the only evidence of how good an estimate
+that is.
+
+### 19.5 Limits
+
+- A recorded `context_overflow` is reproduced only on an endpoint with a declared context
+  window, and with RouteForge's token count rather than the backend's.
+- A rescued request's cost is the admission estimate. Where the client's output cap is far
+  above what it would have generated, the estimate overstates the cost.
+- Nothing here replays failover, escalation, breaker state or daily-spend and RPM admission.
+  A request refused for its daily budget or its rate limit writes no outcome row at all.
+- A `backend_failed` row is costed as if the call had succeeded.
````

- [ ] **Step 5: `docs/research/arxiv.md`** — §4.4 gains §4.4.1 (the claim, the lesson, the invariants, the experiment); the §4.1 bullet and the E7b row point at it.

Patch `docs/research/arxiv.md`:

````diff
diff --git a/docs/research/arxiv.md b/docs/research/arxiv.md
index eb757c1..4ea8b17 100644
--- a/docs/research/arxiv.md
+++ b/docs/research/arxiv.md
@@ -310,8 +310,7 @@ literature doesn't model".
   it would have served 7, and baseline agreement stayed 1.0 because it only compares `ok`
   rows. Lesson, same family as B55 (§3.7): *every input the admission and routing decision
   read must be on the record for rejected requests too, and agreement must compare
-  outcomes (served vs rejected), not only the chosen model.* Fix is scoped into the
-  replay-fidelity slice (§4.4).
+  outcomes (served vs rejected), not only the chosen model.* The fix is §4.4.1.
 - **Experiment E-P3c2 (TO MEASURE):** the HO §25/§28 healthcare triad as three scenarios
   (all-on-prem / raw-PII-only on-prem / all-on-prem + second endpoint) on a persona-generated
   traffic mix with realistic shares (60/25/10/5). Report the comparison table. Also: static
@@ -319,7 +318,56 @@ literature doesn't model".
 
 ### 4.2 P3c-3 uncertainty ranges — *spec pending*
 ### 4.3 P3c-4 capacity model — *spec pending* (HO §25 "230% of capacity" example)
-### 4.4 Replay fidelity: failover/escalation, breaker state, spend/RPM admission — *spec pending*
+### 4.4 Replay fidelity
+
+#### 4.4.1 Replayable rejections (B60; spec `docs/superpowers/specs/2026-10-06-replayable-rejections-design.md`)
+
+- **Claim:** a request the gateway rejected is replayable from a content-free record, once
+  the record carries the one number the admission decision read: the token count the router
+  was given. A fidelity check must compare *outcomes* (routed or rejected), not only the
+  chosen model.
+- **What changed (FB §19.1):** every outcome row records `routing_tokens` (RouteForge's own
+  post-gate, post-compression count), separate from the backend's billed `prompt_tokens`.
+  Replay routes on the first and prices on the second. A rejected row is priced at the
+  admission estimate and counted as an estimate. A rejected row with no recorded size is
+  excluded and counted, never replayed as an empty prompt.
+- **Lesson to state (extends §3.7):** *every input the admission and routing decision read
+  must be on the record for rejected requests too; agreement must compare outcomes, not only
+  the chosen model.* B55 and B60 are the same defect found twice: a field the decision read
+  was missing from the row, and replay substituted a default that looked plausible. The two
+  agreement measures are each blind to the other's failure (FB §19.1), which is why both are
+  reported and the gate takes the lower.
+- **Second finding (FB §19.2):** a pooled outcome rate dilutes rejections, which are a small
+  share of traffic. The count of unreproduced rejections out of the recorded rejections is
+  the trustworthy number; report it beside the rate.
+- **Invariants to cite, with the test that pins each** (full table: FB §19.3):
+  R1 `test_schema_is_content_free` and
+  `test_r1_rejected_row_with_pii_carries_a_count_and_no_content`; R2
+  `test_r2_served_row_replay_is_unchanged_when_the_counts_coincide`; R3
+  `test_r3_budget_rejected_row_is_rejected_in_the_current_arm`; R4
+  `test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate`; R5
+  `test_live_round_trip_budget_rejections_replay_as_rejected`; R6
+  `test_r6_unsized_rows_are_excluded_and_counted_never_routed`; R7
+  `test_no_serving_module_imports_app_eval`; R8
+  `test_outcome_recording_adds_nothing_to_the_success_wire`; R9
+  `test_r9_export_without_the_field_still_loads_and_replays_served_rows`; R10
+  `tests/test_outcome_routing_tokens.py`; R11
+  `test_r11_every_status_has_a_recorded_outcome_class`; R12
+  `test_r12_gate_takes_the_lower_of_the_two`; R13
+  `test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it`.
+- **Privacy argument for the paper:** the added field is an integer length, measured after
+  the gate. It is the same class of value as the token counts already recorded, and for
+  requests under 2000 tokens it was already recoverable from `complexity`. It adds a length
+  side channel for rejected requests, and that is all it adds (reference
+  `routing-outcomes.md` §2).
+- **Experiment E7b (TO MEASURE, FB §19.4):** re-run the 2026-10-06 DGX lab scenarios on rows
+  recorded after the upgrade. Report outcome agreement, the unreproduced-rejection count,
+  newly served under `raise-request-budget`, and the gap between `routing_tokens` and
+  `prompt_tokens` on served rows. No number exists yet.
+- **Limits:** a rescued request is costed at its admission estimate; a recorded
+  `context_overflow` needs a declared window; RouteForge's token count is not the backend's.
+
+#### 4.4.2 Still pending: failover/escalation, breaker state, spend/RPM admission — *spec pending*
 ### 4.5 Administrator review workflow — *spec pending* (closes HO §37 P3 "admin approval")
 ### 4.6 P2 evaluation metrics: confidence calibration, churn robustness, worst-group, router-version promotion gate — *spec pending* (FB §6.5 lists these as not yet demonstrated)
 
@@ -349,7 +397,7 @@ literature doesn't model".
 | E5 | Replay agreement before/after B55 | FB §15.4 | done |
 | E6 | Shadow-price non-monotonicity counterexample, live | FB §17.3 | **TO MEASURE** (not exercised live) |
 | E7 | Context-window replay effect on agreement | P3c-2, FB §18.4.1 | first run done (no overflow in the export: agreement 1.0, 0 hops); with real overflow failovers **TO MEASURE** |
-| E7b | Rejected rows replay as routed (B60) | FB §18.4.1 | observed live: 7 of 7 budget rejections; fix pending |
+| E7b | Rejected rows replay as routed (B60) | FB §18.4.1, §19.4 | defect observed live: 7 of 7 budget rejections replayed as routed. Fix built (§4.4.1); the re-run on post-upgrade rows — outcome agreement, unreproduced rejections, newly served, `routing_tokens` vs `prompt_tokens` — is **TO MEASURE** |
 | E8 | Healthcare scenario triad, traffic-weighted vs static | P3c-2 | **TO MEASURE** |
 | E9+ | uncertainty, capacity, fidelity, P2 metrics, R2/R3/R4 | §4–§5 | specs pending |
 
````

- [ ] **Step 6: `docs/roadmap/priorities-1-3-status.md`** — the slice in the Built table; the remaining fidelity gaps; the finding.

Patch `docs/roadmap/priorities-1-3-status.md`:

````diff
diff --git a/docs/roadmap/priorities-1-3-status.md b/docs/roadmap/priorities-1-3-status.md
index 1f6fc08..4033538 100644
--- a/docs/roadmap/priorities-1-3-status.md
+++ b/docs/roadmap/priorities-1-3-status.md
@@ -60,6 +60,7 @@ Handoff §37 Priority 2 criteria met:
 | **P3b** minimal conflicting core | #76, #77 | An empty feasible set's 422, the outcome row, and the simulator name the smallest set of requirements no endpoint meets together. Endpoint state (disabled, circuit open, …) is kept separate. Remediation hints never relax a security requirement. | `docs/reference/conflicting-core.md`, research §16 |
 | B56 / B57 | #78 | The exact app scope is enforced at the router; the `serving` logger actually prints in deployment | |
 | **P3c-1** shadow prices | #79, #80 | `policy_simulate.py --shadow-prices` relaxes one constraint at a time, per app, and prices it (cost/day, rejections avoided, newly rejected, quality, latency, on-prem share). Security constraints appear only as "cost of protection", never as a recommendation. | `docs/reference/policy-simulator.md` §8, research §17 |
+| B60 replayable rejections | pending | Every outcome row records `routing_tokens`, the token count the router was given, so a request rejected for its size replays as rejected and a proposal that raises a budget shows what it rescues. The baseline check reports outcome agreement (routed or rejected against the recorded status) next to model agreement, and `--min-agreement` gates on the lower. Rejected rows from before the fix are excluded and counted. One `ALTER` before deploy. | `docs/reference/policy-simulator.md` §5–§6, `docs/reference/routing-outcomes.md` §1, research §19 |
 | **P3c-2** alternative scenarios | #84 | `policy_simulate.py --scenarios` replays several administrator-written scenarios against one shared baseline and writes one comparison table, in file order, never ranked. Proposals can assume a token scale, a cached-prompt share and a per-endpoint latency. Replay mirrors the gateway's context-window overflow re-selection. `--since` cuts old rows; `--min-agreement` refuses a run whose baseline disagrees with the record. | `docs/reference/policy-simulator.md` §9, research §18 |
 
 Handoff §37 Priority 3 criteria met:
@@ -82,6 +83,9 @@ partly met: nothing is ever applied, but there is no approval workflow (see §2.
 - **A replay is only as good as its baseline agreement.** Rows recorded before B55 replay
   unpinned. On the Spark they produced a fake shadow price (research §17.4). Cut exports at
   the first B55-era row: 2026-09-25 01:25 UTC on the Spark.
+- **Agreement over served rows is not enough.** A request rejected for its size replayed as
+  routed, because the row did not record its size, and model agreement stayed 1.0 (B60,
+  research §18.4.1). Agreement now also compares outcomes (research §19).
 - **Real Spark traffic today is one app (`kit-gtm`, Qwen Code).** Its only binding constraint
   is its own model pin: $0 difference, and about 26 s lower replayed p50 without it.
 
@@ -113,8 +117,9 @@ brainstorm, spec, plan and build.
    - no breaker state;
    - no re-running of PII detection;
    - no daily-spend or RPM admission;
-   - recorded `context_overflow` rows are not reproduced, because failure rows record no
-     prompt size (research §18.2);
+   - a recorded `context_overflow` row is reproduced only on an endpoint with a declared
+     context window (research §19.5). Rejected rows now carry their size (B60);
+   - a rescued request is costed at its admission estimate, not a measured cost;
    - quality for routes never historically served falls back to the tier default
      (handoff open question 10).
 
@@ -167,5 +172,6 @@ brainstorm, spec, plan and build.
    §15–§17.
 2. Start P3c-3 (uncertainty ranges) with a design brainstorm, then a spec, plan and subagent
    build.
-3. Before quoting any simulator number from the Spark, check baseline agreement, and exclude
-   rows recorded before B55.
+3. Before quoting any simulator number from the Spark, check both baseline agreements, and
+   exclude rows recorded before B55. Rejected rows recorded before B60 are excluded
+   automatically (`missing_routing_tokens`).
````

- [ ] **Step 7: Check the docs against the code**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_docs_links.py -p no:warnings -q -rf
```

Expected: every test passes (0 failed).

```bash
sed -n '/^## 19\./,$p' docs/research/feasible-route-bench.md | grep -o "::test_[a-z0-9_]*" | tr -d ':' | sort -u | while read t; do grep -rq "def $t" tests || echo "MISSING $t"; done
sed -n '/^### 19\.4/,/^### 19\.5/p' docs/research/feasible-route-bench.md | grep -c "TO MEASURE"
```

Expected: the first command prints nothing (every test §19 cites exists). The second prints `10`: the heading sentence and the nine result rows of §19.4, none of them a number.

- [ ] **Step 8: Commit**

```bash
git add docs/reference/routing-outcomes.md docs/reference/policy-simulator.md docs/backlog/bugs.md docs/research/feasible-route-bench.md docs/research/arxiv.md docs/roadmap/priorities-1-3-status.md
git commit -m "$(cat <<'MSG'
docs: replayable rejections -- routing_tokens, outcome agreement, B60 resolved, research §19

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 8: The whole suite, and the hand-off note

**Files:** none modified, unless a failure caused by this branch is found.

**Interfaces:**
- Consumes: everything above.
- Produces: a clean suite run, and the deploy note for the operator.

- [ ] **Step 1: Run the whole suite**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest -q -rf -p no:warnings 2>&1 | tee /tmp/rf_full_suite.log | tail -n 30
```

Expected: it takes more than ten minutes (about seventeen on the machine this plan was validated on). Under `-q` pytest prints no summary line in this repo, so read the `-rf` section: there is either no `FAILED` line at all, or exactly one, which is pre-existing and not caused by this branch:
`tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app`.
It fails with `ModuleNotFoundError` in an environment without the Presidio extra, on the base commit too.

```bash
grep -c "^FAILED" /tmp/rf_full_suite.log; grep "^FAILED" /tmp/rf_full_suite.log
```

Expected: `0`, or `1` and that test. Any other `FAILED` line is this branch's: fix it under the task that owns the file, re-run that task's tests, and re-run this step.

- [ ] **Step 2: Re-run the invariant tests by name**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_outcome_routing_tokens.py tests/test_outcome_store.py::test_schema_is_content_free tests/test_openai_compat.py tests/test_pii_entity_filter.py tests/eval/test_replay_rejections.py tests/eval/test_isolation.py "tests/eval/test_policy_simulate_cli.py::test_live_round_trip_budget_rejections_replay_as_rejected" "tests/eval/test_policy_simulate_cli.py::test_live_round_trip_scenarios_show_the_rescued_rejections" "tests/eval/test_policy_simulate_cli.py::test_live_export_stripped_of_routing_tokens_excludes_the_rejections" "tests/eval/test_policy_simulate_cli.py::test_r12_min_agreement_exits_4_on_outcome_disagreement" -p no:warnings -q -rf
```

Expected: every test passes (0 failed). Two tests in `tests/test_pii_entity_filter.py` are skipped when the Presidio extra is not installed; that is not this branch. This run is R1–R13 and both standing consumer-contract files.

- [ ] **Step 3: Confirm the branch is clean and nothing was pushed**

```bash
git status --short
git log --oneline main..HEAD
```

Expected: `git status --short` prints nothing. The log lists, newest first, the seven task commits (Tasks 7 down to 1), then the plan commit and the spec commit. Do not push.

- [ ] **Step 4: Hand the deploy note to whoever deploys**

Nothing to run. Report these three facts with the branch:

1. **Run before rolling the image**, on every database that already has `routing_outcomes` (the Spark, the DGX lab):
   - PostgreSQL: `ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS routing_tokens INTEGER;`
   - SQLite: `ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER;`
2. Without it the application still boots and serves, but writes no outcome rows and the admin export comes back empty.
3. The experiment that fills research §19.4 is the DGX lab re-run of the 2026-10-06 scenarios, with `--since` at the first row recorded after the upgrade. Old exports now report their rejected rows under `missing_routing_tokens` instead of simulating them.

---

## Open risks

- **Outcome agreement is diluted by served rows.** Rejections are a small share of traffic, so the rate can stay above any threshold below 1.0 while every rejection replays wrong. The reports print the count out of the recorded rejections for exactly this reason, but `--min-agreement` compares only the rate. A per-class gate was not added.
- **`routing_tokens` is RouteForge's count (`len(text) // 4`), not the backend's.** It is the right number for the budget check, which used it live. For a rejected row it is also used for the context-window check and for pricing, where the backend's count would be the right one and does not exist. Research §19.4 has a row to measure the gap on served rows.
- **A rescued request is costed at its admission estimate.** With a large client `max_tokens` (8192 is common for coding agents) that can be far above what the request would really have cost. It is labelled, counted in `rows.estimated_tokens`, and outside the like-for-like cost columns, but the proposed side of `cost_usd` includes it.
- **Served rows now route on a different number than before.** On a deployment whose rows carry `routing_tokens`, replay gives the router RouteForge's count where it used to give the backend's. Model agreement on existing deployments should rise or stay the same; that is not measured yet.
- **Old exports change their output.** Rejected and failed rows recorded before the upgrade leave the simulation. `simulation_id` is a hash of the inputs and does not change with the code, so two reports with the same id can now differ.
- **An unmigrated database loses outcome rows silently from the client's point of view.** Serving continues, the write failure is a `serving.obs` warning, and the admin export returns an empty `200`. There is no metric and no boot-time check, by the B55 precedent.
- **The PostgreSQL `ALTER` is not executed by any test.** The test suite runs on SQLite. The statement has the same form as the two `docs/reference/routing-outcomes.md` already gives for the B55 and P3b upgrades.
- **R13's test relies on SQLite's `ALTER TABLE … DROP COLUMN`,** which needs SQLite 3.35 or later.
- **A recorded `context_overflow` on an endpoint with no declared context window still replays as routed.** It is now counted (`recorded_rejected_but_routed`) rather than invisible, and on such a deployment it lowers outcome agreement and can trip `--min-agreement`. The remedy is to declare the window.
- **`--min-agreement` in `--scenarios` mode still runs after the replay,** as before: the gate needs the shared baseline, which `compare` returns at the end.
