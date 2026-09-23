# P2b-2 Routing Outcome Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record one content-free, versioned outcome row per routed request, plus local
feedback rows. Export them as NDJSON, and report on them from FeasibleRouteBench tooling. This
is the substrate P2b-3 learns from.

**Architecture:**
- Two new tables (`routing_outcomes`, `outcome_feedback`), created by `create_all`, so there is
  no ALTER.
- A pure builder (`app/apps/outcomes.py`) turns a finished `resp` or a failure context into a
  row dict. The routes call it from the same places metering already happens, as a
  best-effort write.
- The pipeline publishes a content-free `outcome_ctx` into the existing `internals`
  out-parameter, so failure paths have policy features too.
- Feedback is stored locally, scoped to the calling app.
- An admin NDJSON export feeds an offline loader/report in `app/eval`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (SQLite/Postgres), Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-09-23-routing-outcome-records-design.md`

## Global Constraints

- Run tests with `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest <tests> -p no:cacheprovider -W ignore`. Use that absolute venv path from any worktree. Do not use `-qq`, which hides the summary. The full suite takes about 6 minutes; read its result with `2>&1 | tail -3`.
- `grep` may get rewritten to `rg` by the shell wrapper, so use `/usr/bin/grep`.
- **Privacy invariant (spec §4.3).** Both tables hold ids, enums, numbers, entity **types** and
  version hashes only. They never hold prompt or completion text, tool arguments, reason
  strings, upstream error text, `comment` or `corrected_output`. `rejected` stores `{name, code}`
  and never `reason`.
- **Workload tags** are lower-cased and must match `^[a-z0-9][a-z0-9._-]{0,63}$`. An invalid
  tag is dropped (`workload_source="invalid"`) and **never rejected**. The header
  `x-routeforge-workload` wins over `metadata.workload`. Workload is never forwarded upstream.
- **`value_cat`** must match `^[A-Za-z0-9._:-]{1,64}$`, otherwise the feedback row is refused.
- **`status`** must be exactly one of: `ok`, `backend_failed`, `verify_failed`, `no_eligible`,
  `context_overflow`. Never write a row for 401, 429, a gate error, or a cancelled (499) request.
- **Prompt-token buckets** are exactly `<1k`, `1k-4k`, `4k-16k`, `16k-64k`, `>64k`. Bucket
  post-compression prompt tokens, with the upper bound exclusive (1000 → `1k-4k`).
- **`verify_mode`** must be one of: `not_required`, `verified`, `failed`, `waived_stream`,
  `incremental`.
- **Export schema** tag is exactly `routeforge.outcome/v1`, the media type is
  `application/x-ndjson`, and `limit` is capped at 100000.
- **Config** lives in `config/serving.yaml` as `outcomes: {enabled: true, retention_days: 90}`,
  and `serving.yaml` is its only owner. With `enabled: false` nothing is written and nothing
  else changes.
- **Outcome writes are best effort.** A failed write is logged with the request_id and never
  fails or delays the response. Metering (`usage_records`) is unchanged.
- **Routing decisions must be byte-identical.** This slice writes and reads records and never
  feeds them back into routing.
- `app/eval` must never be imported from serving code. `tests/eval/test_isolation.py` enforces
  this. The serving-side writer lives in `app/apps`.
- Every new `DecisionRecord` or `RoutingDecision` field must be optional with a default.
- Match the surrounding comment density. Commit messages end with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

**Deviations from the spec, decided while planning (Task 8 amends the spec):**
1. **No `routeforge_outcome_write_failures_total` metric.** A failed write is logged at warning
   with the request_id. `/metrics` stays at five families, and a write failure is an operator
   log concern, not a routing signal.
2. **`circuit_bypass` is recorded now.** The breaker merged in PR #67, so the column is `True`
   when any router hop's `DecisionRecord.circuit_bypass` was true.

---

### Task 1: Tables, store functions, config switch

**Files:**
- Modify: `app/apps/models.py` (add `RoutingOutcome` and `OutcomeFeedback` after `EndpointObservation`)
- Modify: `app/apps/store.py` (add `write_outcome`, `get_outcome`, `find_outcome`, `write_feedback`, `prune_outcomes`, `iter_outcomes`)
- Modify: `app/config/schema.py` (add `OutcomesCfg`, and `outcomes` on `ServingCfg`)
- Modify: `config/serving.yaml` (add an `outcomes:` block)
- Modify: `app/config/lint.py` (require `retention_days >= 1`)
- Test: `tests/test_outcome_store.py` (new) and `tests/test_config_lint.py`

**Interfaces:**
- Produces:
  - **ORM classes:** `RoutingOutcome` and `OutcomeFeedback`, with the columns below.
  - **Store functions**, all taking an `AsyncSession`; the caller commits:
    - `write_outcome(s, row: dict) -> None`
    - `get_outcome(s, request_id: str, *, application_id: str) -> RoutingOutcome | None`
    - `find_outcome(s, *, application_id: str, request_id: str | None, trace_id: str | None) -> RoutingOutcome | None`
    - `write_feedback(s, *, request_id, application_id, score_name, value_num, value_cat, source) -> None`
    - `prune_outcomes(s, *, older_than_days: float) -> int`
    - `iter_outcomes(s, *, since, until, application_id=None, after=None, page=1000) -> list[RoutingOutcome]`
      pages by keyset `(ts, request_id) > after`.
    - `feedback_for(s, request_ids: list[str]) -> dict[str, list[OutcomeFeedback]]`
  - **Config:** `Settings.serving.outcomes.enabled: bool` and `.retention_days: float`.

- [ ] **Step 1: Write the failing tests** in `tests/test_outcome_store.py`. Use an in-memory
  DB (`Database(url="sqlite+aiosqlite://")` and `await db.ensure_ready()`), as
  `tests/test_gateway_lateral_failover.py::_build_reg` does. The tests:
  1. `write_outcome` then `get_outcome` round-trips every column, including JSON lists and
     the nullable ones.
  2. `get_outcome` with a different `application_id` returns None.
  3. `find_outcome` works by `trace_id` and by `request_id`, and is app-scoped.
  4. `write_feedback` rows are returned by `feedback_for`.
  5. `prune_outcomes(older_than_days=90)` deletes outcome **and** feedback rows whose `ts` is
     older than the cutoff (write rows with explicit old `ts`), keeps newer ones, and returns
     the count of deleted outcome rows.
  6. `iter_outcomes` returns rows ordered by `(ts, request_id)`. With `page=2` over 5 rows,
     three successive calls passing the last `(ts, request_id)` as `after` return 2, 2 and 1
     rows, with no duplicates and none missing.
  7. **Schema privacy:** no column name in either table contains `prompt_text`, `completion`,
     `content`, `message`, `comment`, `corrected`, `reason` or `error`. `prompt_tokens` and
     `prompt_tokens_bucket` are allowed, so match against that exact forbidden list.

  In `tests/test_config_lint.py`:
  - `load_settings().serving.outcomes` equals `enabled=True, retention_days=90`.
  - `retention_days=0` produces lint code `OUTCOMES_RETENTION_NOT_POSITIVE`.

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement.** Models, following the file's conventions (`Mapped`,
  `mapped_column`, `utcnow`):

```python
class RoutingOutcome(Base):
    """One routed request's outcome (P2b-2): what was eligible, what was tried,
    what was served, what it cost, and how it verified -- the substrate P2b-3
    learns from. CONTENT-FREE BY CONTRACT: ids, enums, numbers, entity TYPES
    and version hashes only; never prompt/completion text, reason strings or
    upstream error text (spec §4.3). A NEW table -> create_all, no migration."""

    __tablename__ = "routing_outcomes"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20))
    app_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    endpoint_registry_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    routing_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workload: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    workload_source: Mapped[str] = mapped_column(String(8), default="none")
    tier_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    complexity: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_tokens_bucket: Mapped[str | None] = mapped_column(String(8), nullable=True)
    sensitivity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    risk_class: Mapped[str | None] = mapped_column(String(8), nullable=True)
    onprem_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    detected_entity_types: Mapped[list] = mapped_column(JSON, default=list)
    eligible: Mapped[list] = mapped_column(JSON, default=list)
    rejected: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[list] = mapped_column(JSON, default=list)
    chosen: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scoring: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    circuit_bypass: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    verify_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_performed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


class OutcomeFeedback(Base):
    """A score attached to one routed request (P2b-2 §4.2). `request_id` is a
    LOGICAL link to routing_outcomes, not an FK, so pruning order never
    orphans or blocks. `application_id` is always the AUTHENTICATED caller's.
    `comment`/`corrected_output` text is never stored here -- it goes to the
    app's own Langfuse project exactly as before."""

    __tablename__ = "outcome_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    application_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    score_name: Mapped[str] = mapped_column(String(32))
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_cat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(8))   # app | admin
```

Implement the store functions in `store.py`, next to the observation functions:
- `find_outcome` filters by `application_id`, plus `request_id` when given, otherwise
  `trace_id`.
- `prune_outcomes` deletes from both tables with `ts < cutoff`.
- `iter_outcomes` orders by `(RoutingOutcome.ts, RoutingOutcome.request_id)`. With `after`
  given, it filters with
  `or_(RoutingOutcome.ts > after_ts, and_(RoutingOutcome.ts == after_ts, RoutingOutcome.request_id > after_id))`
  and applies `.limit(page)`.

Add `OutcomesCfg` to the schema:

```python
class OutcomesCfg(BaseModel):
    """P2b-2 routing outcome records (app/apps/outcomes.py). Infra switch, so
    serving.yaml owns it. `enabled: false` writes nothing and changes nothing
    else."""
    enabled: bool = True
    retention_days: float = 90.0
```

Add `outcomes: OutcomesCfg = OutcomesCfg()` to `ServingCfg`. In `config/serving.yaml`:

```yaml
# P2b-2 routing outcome records: one content-free row per routed request
# (ids, enums, numbers, versions -- never text) plus local feedback scores,
# kept `retention_days` then pruned hourly. Exported via
# GET /admin/outcomes/export. See docs/reference/routing-outcomes.md.
outcomes:
  enabled: true
  retention_days: 90
```

Add the lint check `OUTCOMES_RETENTION_NOT_POSITIVE` for `retention_days <= 0`. If `lint.py`
has a single-owner or duplicate-concern check that enumerates keys, add `outcomes` there.

- [ ] **Step 4: Run the tests and see them pass.** Then run the full suite once.
- [ ] **Step 5: Commit** `feat(outcomes): routing_outcomes + outcome_feedback tables and store (P2b-2)`

---

### Task 2: Pure outcome builder and workload parsing

**Files:**
- Create: `app/apps/outcomes.py`
- Test: `tests/test_outcome_builder.py` (new)

**Interfaces:**
- Consumes: nothing from Task 1 beyond the column names (it returns plain dicts).
- Produces:
  - `parse_workload(header: str | None, metadata: object) -> tuple[str | None, str]`
    returns `(workload, workload_source)`, where the source is `client`, `none` or `invalid`.
  - `prompt_bucket(tokens: int) -> str`
  - `outcome_context(ctx) -> dict` holds the content-free policy features, taken from an
    `ExecutionPolicyContext`: `sensitivity`, `risk_class`, `onprem_only`,
    `detected_entity_types` (sorted unique types) and `app_policy_version`.
  - `build_ok_outcome(resp: dict, *, request_id: str, application_id: str, outcome_ctx: dict, routing_policy_version: str, workload: str | None, workload_source: str) -> dict`
  - `build_failure_outcome(*, status: str, request_id: str, application_id: str, outcome_ctx: dict, routing_policy_version: str, endpoint_registry_version: str | None, workload: str | None, workload_source: str, attempts: tuple[str, ...] = (), rejected: tuple = (), latency_ms: float = 0.0) -> dict`
  - `STATUSES`, `VERIFY_MODES`, `BUCKETS` constants.

`build_ok_outcome` reads only these content-free keys of `resp` (read the pipeline to confirm
each key path before relying on it; `resp["trace"]` is a list of dicts):
- `trace_id`, `usage.prompt_tokens`, `usage.completion_tokens`, `cost.total_usd`;
- `policy.endpoint_registry_version`;
- from `trace`:
  - **router hops:** the entries with `step == "router"`. `chosen` is the **last** router hop
    with `chosen` set. `scoring` is the **first** router hop's `scoring`. `eligible` is
    `[c["name"] for c in first_router["scoring"]["candidates"]]` when scoring exists, else
    `[chosen]`. `tier_target` is the first router hop's `tier_target` (Task 3 adds it).
    `circuit_bypass` is true if any router hop has it.
  - **gateway hops:** failed hops (`step == "gateway"` and `chosen is None` with `scoring`)
    contribute `scoring["candidates"][0]["name"]` to `attempts`, in hop order, interleaved
    with the router hops' `chosen`. The result is every model tried, in order.
  - **rejected:** `[{"name": r["name"], "code": r.get("code")} for r in first_router["rejected"]]`.
    Never include `reason`.
  - **verify:** the entry with `step == "verify"` gives `verify_required`, `verify_performed`
    and `verify_passed`. Map it to `verify_mode`:
    - `required` false → `not_required`;
    - `reason` starting `"waived"` → `waived_stream`;
    - `reason == "incremental"` → `incremental`;
    - `passed` false → `failed`;
    - otherwise → `verified`.
  - **latency:** `latency_ms` is the sum of the trace entries' `latency_ms` (the same sum the
    usage row uses).
- `prompt_tokens_bucket` is `prompt_bucket(usage.prompt_tokens)`.
- `status` is `"ok"`.

`build_failure_outcome` fills what it is given and leaves the rest null or empty.

- [ ] **Step 1: Write the failing tests.** Use fixture `resp` dicts shaped like real pipeline
  output: copy a real one by running the existing pipeline test helper once, or build a minimal
  one by hand from the key paths above.
  - `parse_workload`:
    - `("Claims.Summary", None)` → `("claims.summary", "client")`;
    - header beats metadata;
    - `(None, {"workload": "faq"})` → `("faq", "client")`;
    - `(None, None)` → `(None, "none")`;
    - a header with a space, `<script>`, 65 characters or an empty string → `(None, "invalid")`;
    - metadata that is not a dict, or a `workload` that is not a string → `(None, "invalid")`
      when present, `(None, "none")` when absent.
  - `prompt_bucket` boundaries: 0, 999, 1000, 3999, 4000, 15999, 16000, 63999, 64000.
  - `build_ok_outcome` on:
    - a single-hop response;
    - a failover response (a gateway failed hop then a router hop): `attempts == ["a", "b"]`,
      `chosen == "b"`;
    - an escalation response (two router hops): the last chosen wins;
    - one response per verify mode.
  - **Privacy:** build from a `resp` whose `choices`, `trace` reasons and `rejected` reasons
    contain `"123-45-6789"` and `"jane@example.com"`. Assert that
    `json.dumps(row, default=str)` contains neither.
  - `build_failure_outcome("no_eligible", rejected=(...))` stores only name and code.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `app/apps/outcomes.py`** with a module docstring stating the
  content-free contract. Put the regexes in module constants.
- [ ] **Step 4: Run the tests and see them pass.**
- [ ] **Step 5: Commit** `feat(outcomes): pure outcome builder and workload parsing (P2b-2)`

---

### Task 3: Surface the missing fields (`tier_target`, `outcome_ctx`, `NoEligibleCandidate.rejected`)

**Files:**
- Modify: `app/routing/router.py` (`RoutingDecision.tier_target: str | None = None`, set to the
  final target tier)
- Modify: `app/models.py` (`DecisionRecord.tier_target: str | None = None`, with a comment)
- Modify: `app/gateway/gateway.py` (pass `tier_target=decision.tier_target` on router records
  in `run` and `run_stream`)
- Modify: `app/errors.py` (`NoEligibleCandidate.__init__(self, *args, rejected: tuple = ())`,
  stored as `.rejected`)
- Modify: `app/routing/router.py` (both `NoEligibleCandidate` raises pass the list of rejected
  candidates as `(name, code)` tuples)
- Modify: `app/pipeline.py`:
  - in `run` and in `run_stream`, right after the policy context `ctx` exists, set
    `internals["outcome_ctx"] = outcome_context(ctx)` when `internals is not None`;
  - after `complexity` is computed (around lines 905 and 1210), set
    `internals["outcome_ctx"]["complexity"] = complexity`.
- Test: `tests/test_router.py` or `tests/test_router_breaker.py`, `tests/test_errors.py` if it
  exists, and a pipeline-level test (find the existing pipeline tests with
  `/usr/bin/grep -ln "Pipeline(" tests`).

**Interfaces:**
- Consumes: `outcome_context` (Task 2).
- Produces:
  - `DecisionRecord.tier_target`
  - `RoutingDecision.tier_target`
  - `NoEligibleCandidate.rejected: tuple[tuple[str, str | None], ...]`
  - `internals["outcome_ctx"]: dict` with the keys `sensitivity`, `risk_class`,
    `onprem_only`, `detected_entity_types`, `app_policy_version`, and `complexity` once
    routing ran.

- [ ] **Step 1: Write the failing tests.**
  - A router decision carries `tier_target` equal to the tier in its reason string.
  - A `NoEligibleCandidate` raised for an `onprem_only` context with only cloud models carries
    `rejected` with code `residency`.
  - `Pipeline.run` with an `internals={}` dict leaves `internals["outcome_ctx"]` populated,
    including `complexity`. It also stays populated when the request then fails at routing:
    use an `onprem_only` context with no on-prem model and assert that `outcome_ctx` exists
    after the `NoEligibleCandidate`.
  - `outcome_ctx` contains no text: its JSON has no SSN when the prompt has one.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Keep `DecisionRecord.tier_target` optional so nothing breaks on
  the wire. Check whether `app/obs/export.py` enumerates `DecisionRecord` fields for span
  attributes. It is fine not to export `tier_target`; do not add span attributes in this task.
- [ ] **Step 4: Run the tests and see them pass.** Then run the router, gateway, pipeline and
  the full suite.
- [ ] **Step 5: Commit** `feat(outcomes): surface tier_target, outcome_ctx and no-eligible rejections (P2b-2)`

---

### Task 4: Write-path hooks in `routes.py`

**Files:**
- Modify: `app/api/routes.py`:
  - parse the workload once in `chat_completions`;
  - add `_record_outcome(request, row)`;
  - call it from `_finalize` (ok), `_record_failed` (backend_failed), and the non-stream
    `except Exception` branch plus the keepalive error branch, for verify_failed, no_eligible
    and context_overflow.
- Test: `tests/test_outcome_hooks.py` (new). Follow the HTTP-boundary style of
  `tests/test_openai_compat.py` and the consumer-contract-tests skill: a real app via the
  project's test app factory, and a deterministic backend.

**Interfaces:**
- Consumes: Task 1 store functions, Task 2 builders, Task 3 `outcome_ctx`, `tier_target` and
  `NoEligibleCandidate.rejected`.
- Produces: `routing_outcomes` rows on every routed request when
  `settings.serving.outcomes.enabled`.

Details:
- **Workload.** In `chat_completions`, after auth, call
  `workload, workload_source = parse_workload(request.headers.get("x-routeforge-workload"), (body.model_extra or {}).get("metadata"))`
  and pass them down to `_finalize` as keyword arguments, and to the failure recorders.
  Confirm that `metadata` is not in `FORWARDED_PARAMS` (`app/models.py`). If it is, stop and
  report.
- **`_record_outcome(request, row: dict)`.** Returns immediately if
  `not request.app.state.settings.serving.outcomes.enabled`. Otherwise it writes the row in
  its own session and commits. Wrap it in `try`/`except Exception` and log with
  `logging.getLogger("serving.obs").warning("outcome record failed request=%s", row.get("request_id"), exc_info=True)`.
  It never raises.
- **`ok`.** In `_finalize`, after the usage row block, build with
  `build_ok_outcome(resp, request_id=..., application_id=tenant, outcome_ctx=(internals or {}).get("outcome_ctx", {}), routing_policy_version=s.routing_policy_version, workload=..., workload_source=...)`
  and record it. `_finalize` is shared by every stream mode, so they are covered. The
  streaming finalize (`_finalize_stream`) overwrites the verify record **before** calling
  `_finalize`; check the order, so `verify_mode` gets `waived_stream` or `incremental`.
- **`backend_failed`.** `_record_failed` gains keyword arguments `workload`,
  `workload_source`, `internals`, `attempts` (from `exc.attempted_models`) and `s`, then writes
  `build_failure_outcome(status="backend_failed", ...)`. Update every caller.
- **Other failures.** In the non-stream `except Exception as e` branch (routes.py ~526), after
  mapping:
  - `ContextWindowExceeded` → `context_overflow`;
  - `NoEligibleCandidate` → `no_eligible`, with `rejected=e.rejected`;
  - `PipelineRejected` whose message does **not** start with `"requested model"` →
    `verify_failed`. A pinned-model rejection is not a routing outcome, so write nothing.
  - Do the same in the keepalive stream's error branch (~972-992).
  `endpoint_registry_version` comes from `request.app.state.registry.registry_version()`, in a
  guarded call.
- **Nothing** for cancelled, 401, 429 or `PolicyGateError`.

- [ ] **Step 1: Write the failing tests** (HTTP boundary):
  1. A plain request gives one row with `status="ok"` and the right `chosen`, `eligible`,
     `attempts`, versions, bucket and `verify_mode`.
  2. The `x-routeforge-workload: claims.summary` header is stored. An invalid header still
     gives 200 with `workload_source="invalid"`. `metadata.workload` in the body is stored when
     there is no header, and is not sent upstream: check the fake backend's received params.
  3. A backend that always fails → 502 and a row with `backend_failed` and non-empty
     `attempts`.
  4. `onprem_only` with no on-prem model → 422 and a row with `no_eligible` whose `rejected`
     codes include `residency`.
  5. Context overflow → 400 and a row with `context_overflow`.
  6. Step-9 failure → 422 and a row with `verify_failed`. Find how existing tests force a
     verify failure (`/usr/bin/grep -rn "failed closed" tests`).
  7. A cancelled request, 401 and 429 write no row.
  8. With `outcomes.enabled=False`, no row is written and the response is identical.
  9. If `store.write_outcome` is monkeypatched to raise, the response is still 200 and the
     `usage_records` row still exists.
  10. **Privacy:** a request containing `"My SSN is 123-45-6789, email jane@example.com"` gives
      a row where no column value contains `123-45-6789` or `jane@example.com`. Check with
      `json.dumps` over all columns.
  11. **Streaming modes:** a `stream_unverified` app gives a row with
      `verify_mode="waived_stream"`, and a `verified_then_stream` app gives `verified` or
      `not_required`. Reuse the fixtures of the existing stream-mode tests.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, `tests/test_openai_compat.py`, and every
  `tests/test_*stream*` and `tests/test_*usage*` file, then the full suite.**
- [ ] **Step 5: Commit** `feat(outcomes): record an outcome row on every routed request (P2b-2)`

---

### Task 5: Local feedback storage, request-id linkage and app scoping

**Files:**
- Modify: `app/api/routes.py` (`FeedbackIn` gains `request_id: str | None = None`, and
  `trace_id` becomes `str | None = None`; one of the two is required)
- Modify: `app/api/ui_routes.py` (`admin_feedback` records locally with `source="admin"`,
  including when there is no trace)
- Modify: `app/apps/outcomes.py` (`feedback_values(score_name, value) -> tuple[float | None, str | None] | None`,
  which returns None when a categorical value fails the regex)
- Test: `tests/test_outcome_feedback.py` (new); update any existing `/v1/feedback` or admin
  feedback tests whose response shape changes

**Interfaces:**
- Consumes: Task 1 (`find_outcome`, `write_feedback`) and Task 4 (outcome rows exist).
- Produces: the `/v1/feedback` response
  `{"stored": <langfuse bool>, "recorded": <local bool>}`, and admin feedback's
  `{"stored": ..., "recorded": ...}`, which keeps `"reason": "no trace"` only for the Langfuse
  side.

Behaviour:
- **Validation.** If both `trace_id` and `request_id` are missing, return a 422 in the OpenAI
  error envelope (use the existing `ApiError` / `CODE_INVALID_VALUE`). Keep the check on
  `score_name`.
- **Local record.** Look up `find_outcome(application_id=tenant, request_id=..., trace_id=...)`.
  When it is found, and `feedback_values` accepts the value, call `write_feedback(source="app")`
  and set `recorded = True`. Otherwise `recorded = False`, including for another app's
  request: never reveal whether that request exists.
- **Langfuse** stays exactly as today, and runs only when `trace_id` is known. With only
  `request_id`, use the outcome row's `trace_id` if it has one. `stored` keeps its meaning.
- **`corrected_output`.** Record a local `corrected_output` row with `value_num=1.0`, and never
  store the text.
- **`comment`** is never stored locally.
- **Admin feedback.** Resolve the outcome by the snapshot's `request_id` and `application_id`,
  and record it with `source="admin"`. When the snapshot has no `trace_id`, still record
  locally and return `{"stored": false, "reason": "no trace", "recorded": <bool>}`.
- **Local gating.** The local record needs `outcomes.enabled`. When it is disabled,
  `recorded=False` and Langfuse behaviour is unchanged.

- [ ] **Step 1: Write the failing tests.**
  1. Feedback by `request_id` is recorded.
  2. Feedback by `trace_id` is recorded, using an outcome row with a known `trace_id`.
  3. App B scoring app A's request gives `recorded: false` and writes no row.
  4. A `value` of `"bad value!"` for `failure_type` gives `recorded: false`.
  5. Neither id given → 422.
  6. `comment` and `corrected_output` text never appear in `outcome_feedback`: dump all rows
     as JSON and assert that neither string is present.
  7. Admin feedback with no trace still returns `recorded: true`, `source == "admin"`.
  8. Existing Langfuse-path tests still pass: `stored` semantics are unchanged.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and see them pass.** Run `tests/test_*feedback*` and
  `tests/test_ui_routes.py`, then the full suite.
- [ ] **Step 5: Commit** `feat(outcomes): store feedback locally, app-scoped, linkable by request_id (P2b-2)`

---

### Task 6: Retention prune and admin NDJSON export

**Files:**
- Modify: `app/main.py`. In `_observation_loop`'s hourly prune block, also run
  `prune_outcomes(older_than_days=settings.serving.outcomes.retention_days)` in its own
  guarded session. Skip it when outcomes are disabled.
- Modify: `app/api/admin_routes.py`: `GET /admin/outcomes/export` with
  `Depends(require_admin)`.
- Test: `tests/test_outcome_export.py` (new), and a prune-in-loop test next to the existing
  observation-loop tests (`/usr/bin/grep -rln "_observation_loop\|prune_observations" tests`).

**Interfaces:**
- Consumes: Task 1 (`iter_outcomes`, `feedback_for`, `prune_outcomes`).
- Produces: `GET /admin/outcomes/export?since=&until=&application_id=&limit=`, which streams
  lines of the form
  `{"schema":"routeforge.outcome/v1","outcome":{...all columns, ts as ISO-8601...},"feedback":[{"score_name","value","source","ts"}]}`.
  In each feedback entry, `value` is `value_num` if it is not null, otherwise `value_cat`.

Behaviour:
- **Time window.** `since` and `until` are ISO-8601 (`datetime.fromisoformat`); a bad value
  → 422. The defaults are `until = now` and `since = until - 7 days`.
- **`limit`** defaults to 100000 and is capped at 100000; a value below 1 → 422.
- **Paging.** `StreamingResponse(gen(), media_type="application/x-ndjson")`, where `gen()`
  loops over `iter_outcomes` pages of 1000 in a fresh session per page, stops at `limit`, and
  joins feedback per page with `feedback_for`.
- **Line format.** Each line is `json.dumps(obj, separators=(",", ":"), default=str)` + `"\n"`.
- **Auth.** Without an admin session → 401.

- [ ] **Step 1: Write the failing tests.**
  1. With 3 outcome rows (2 for app A, 1 for B) and feedback on one, the export returns 3
     NDJSON lines, each with `schema == "routeforge.outcome/v1"` and the feedback joined.
  2. `application_id=A` filter → 2 lines.
  3. `since`/`until` filter.
  4. `limit=2` → 2 lines.
  5. Paging across a boundary: monkeypatch the page size to 2 over 5 rows → 5 unique lines.
  6. No admin cookie → 401.
  7. A bad `since` → 422.
  8. `outcomes` retention: the loop's prune deletes an old row. Drive the prune helper
     directly if the loop is hard to drive, following the existing observation-prune test
     pattern.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and see them pass, then the full suite.**
- [ ] **Step 5: Commit** `feat(outcomes): hourly retention prune and admin NDJSON export (P2b-2)`

---

### Task 7: Offline loader and outcome report

**Files:**
- Create: `app/eval/outcomes.py` (loader and aggregation, pure, and it reads a file)
- Create: `scripts/outcome_report.py` (CLI: `--export FILE --out DIR`)
- Test: `tests/eval/test_outcomes_report.py` (new), with a fixture export file under
  `tests/eval/fixtures/` (create the directory if it doesn't exist)

**Interfaces:**
- Consumes: the Task 6 export line format.
- Produces:
  - `load_export(path) -> tuple[list[dict], list[str]]` returns `(records, warnings)`. It
    raises `ValueError` on an unknown schema major such as `routeforge.outcome/v2`.
  - `summarize(records) -> dict` returns the deterministic summary.
  - `render_report(summary) -> str` returns markdown.
  - The script writes `summary.json` (with `sort_keys`, `indent=2`, and no timestamps) and
    `report.md`.

Summary content:
- **Grouping by registry version.** Records are grouped by `endpoint_registry_version` first.
  With more than one version, each group is reported separately, and the header of each lists
  the version. Never pool across versions.
- **Per `(workload or "(untagged)", chosen)`** within a version group:
  - `n`;
  - `served_ok_rate` (status ok / n);
  - `verify_pass_rate` over rows with `verify_performed` true (null when there are none);
  - `quality_mean` and `quality_n` from feedback `score_name == "quality"` numeric values;
  - `route_correct_rate` and `route_correct_n`;
  - `cost_mean`;
  - `latency_p50` and `latency_p95`, using nearest-rank over ok rows;
  - `failover_rate` (rows with `len(attempts) > 1` / n).
- **Per workload:** `n`, `no_eligible_rate`, `backend_failed_rate`, and `label_coverage` (the
  share of rows with any feedback).
- **`report.md`** starts with the caveat, verbatim:
  "Production observes only the served endpoint for each request, so oracle regret is not
  computable from these records and is not reported. Endpoint comparisons are confounded by
  the router's own choices; treat them as descriptive, not causal."
- **Ordering.** Keys are sorted, and floats are rounded to 6 decimal places, so the output is
  deterministic.

- [ ] **Step 1: Write the failing tests.**
  - A golden `summary.json` for the fixture export. Hand-build about 12 lines across two
    workloads, three endpoints and one untagged workload, with feedback, one `no_eligible`,
    one failover, and one `verify_failed`. Compute the expected values by hand in the test.
  - Mixed registry versions produce two groups.
  - Schema `routeforge.outcome/v2` raises `ValueError`.
  - The caveat text is in `report.md`.
  - Running the script twice gives byte-identical `summary.json`.
  - `app/eval/outcomes.py` does not import `app.api` or the DB. It is file-based, and the
    existing isolation test still passes.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `tests/eval` in full.**
- [ ] **Step 5: Commit** `feat(eval): outcome export loader and report (P2b-2)`

---

### Task 8: Docs

**Files:**
- Create: `docs/reference/routing-outcomes.md`. Cover:
  - the schema (both tables, every column, what each is and what it never contains);
  - the statuses and when each is written;
  - workload tagging (header, metadata, validation, "don't put personal data in it");
  - feedback linkage (request_id or trace_id, app scoping, `stored` vs `recorded`, text never
    stored);
  - the export (parameters, line format, example);
  - retention;
  - the `outcomes` config;
  - the privacy invariant and the tests that pin it;
  - the served-arm-only caveat.
- Modify: `docs/reference/api-reference.md`:
  - `/v1/feedback`: the `request_id` field and the `recorded` field;
  - the `x-routeforge-workload` header and `metadata.workload`;
  - `GET /admin/outcomes/export`.
- Modify: `docs/guides/feasible-route-bench.md` (an "Outcome report" section:
  `scripts/outcome_report.py` usage and how it differs from the bench).
- Modify: `docs/research/feasible-route-bench.md` (§13, the outcome records as a dataset):
  - the record model;
  - the invariants (content-free, app-scoped feedback, versions stamped, never pooled across
    registry versions);
  - what it can and cannot support (no counterfactuals, confounded by the router, no
    propensities, since the router is deterministic);
  - what P2b-3 needs from it.
  Follow the research-documentation convention used by §9–§12.
- Modify: `docs/backlog/bugs.md`:
  - mark P2b-2 shipped, wherever the backlog or roadmap tracks it (`/usr/bin/grep -rn "P2b-2" docs/backlog docs/superpowers/specs`);
  - update B51's "durable fix" pointer.
- Modify: `docs/superpowers/specs/2026-09-23-routing-outcome-records-design.md`: amend it,
  dated 2026-09-23, with this plan's two deviations (no write-failure metric; `circuit_bypass`
  recorded now) and anything the build changed.
- Modify: `CLAUDE.md` only if a short pointer fits naturally in "Routing observability &
  signals". Keep it to one sentence, or leave it untouched.

- [ ] **Step 1: Write the docs.** Check every claim against the code, citing file:line in your
  report, not in the docs.
- [ ] **Step 2: Run the full suite once** (docs-only, as a sanity check).
- [ ] **Step 3: Commit** `docs: routing outcome records reference, research §13, API and guide (P2b-2)`
