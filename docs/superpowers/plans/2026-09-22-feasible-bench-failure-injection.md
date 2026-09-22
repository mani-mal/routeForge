# FeasibleRouteBench Failure Injection (B48) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FeasibleRouteBench able to answer B45 honestly: simulated endpoints can fail (outages + flakiness), strategies are scored with production-style failover, and the router's measured signals come from an online arm that sees only its own past attempts.

**Architecture:** Extend the existing `app/eval/` modules. The simulated backend raises production-shaped failures; matrix rows record whether each failure counts as endpoint unhealth; strategies return attempt orders; a pure oracle function scores an attempt chain against the matrix; the runner drives three router arms (cold / online / warm_prior) and weight variants. Nothing outside `app/eval/`, `eval/`, `tests/eval/`, `scripts/feasible_bench.py` and docs changes.

**Tech Stack:** Python 3.12, Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-feasible-bench-failure-injection-design.md` — binding. Also read the P2b-1 spec `docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md` for the existing definitions.

## Global Constraints

- **Serving untouched:** no edits under `app/` outside `app/eval/`, none in `config/`.
- **No leakage:** when scoring case `k`, no arm's signal store may contain any observation from case `k` or later.
- **Production parity:** injected failures must satisfy `app/gateway/gateway.py::counts_as_endpoint_unhealth`; router attempt chains use the real `Router.select(..., exclude=...)`; the attempt cap is `1 + settings.routing.max_failover_attempts`; warm/online stores count an error only when `counts_unhealth` is true.
- **Determinism:** simulated runs with the same seed give byte-identical `metrics.json` (no timings, paths or timestamps in it).
- **Violations cover every attempt:** any attempt outside `E(c)` is a violation → exit 2.
- **The oracle is unchanged** (best eligible endpoint's successful single call); regret is computed from the failover-aware strategy outcome.
- **B45's answer comes from `router_online:<variant>`.** No change to `config/routing.yaml` weights.
- Tests: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest ...`; no pytest-asyncio (`asyncio.run`). Shell: `/usr/bin/grep`, `rtk proxy git`.
- Commits end with exactly `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Current code (verified 2026-09-22)

- `app/eval/endpoints.py`: `SimProfile(quality: dict[str,float], latency_ms: LatencyProfile)` with a `model_validator`; `EndpointSpec`, `load_endpoints(path, *, live, tiers)`, `harness_settings` (empties model_pool, `max_escalations=0`, file prices win), `build_registry`.
- `app/eval/simulated.py`: `SimulatedBackend(specs, seed)`, `set_case(case)`, `generate(...)` returns `GenerationResult(confidence=1.0)`, `received`.
- `app/eval/oracle.py`: `MatrixRow(case_id, endpoint, quality, passed, cost_usd, latency_ms: float|None, error: str|None)`, `CaseInfo`, `Choice(case_id, strategy, endpoint)`, `_oracle_raw`, `case_oracle`, `strategy_metrics`, `grouped`, `worst_groups`, `headline`, `violations`.
- `app/eval/strategies.py`: `StrategyInput(case, ctx, eligible, tokens_after, output_tokens)`, `cheapest`, `best_published_quality`, `fixed`, `router`, `router_variant`, `_FrozenReadSignalStore`, `warm_signals(settings, rows, wall_offset=0.0)`.
- `app/eval/runner.py`: `_CountingBackend`, `_sweep_one`, `run_bench`, `_run_loaded`, `_exit_code(...)`, `_report`.

---

### Task 1: Failure model in the simulated backend

**Files:** `app/eval/endpoints.py`, `app/eval/simulated.py`, `tests/eval/test_simulated.py`, `tests/eval/test_endpoints.py`.

**Produces:**
- `SimProfile.outages: list[OutageSpan] = []` with `OutageSpan(from_: int = Field(alias="from"), to: int)` (inclusive, 0-based case index; validate `0 <= from_ <= to`); `SimProfile.error_rate: float = 0.0` (validate `[0,1]`); `SimProfile.failure_latency_ms: float | None = None` (validate `> 0` when set).
- `SimulatedBackend.set_case(case, index: int)` (the runner passes the case's suite position). `index` defaults are NOT allowed — make it required and update every caller in `app/eval/` and tests.
- `class SimulatedEndpointFailure(Exception)` with attributes `status_code: int | None` and `failure_latency_ms: float`. Outage → `status_code=None`; flake → `status_code=503`. Check order: outage first, then flake draw `random.Random(f"{seed}:{case.id}:{model}:err").random() < error_rate`. `failure_latency_ms = profile.failure_latency_ms or profile.latency_ms.mean`. A failed call is still recorded in `received` (the model was contacted).

- [ ] **Step 1: failing tests** — outage range raises on every in-range index and not outside; `error_rate` 0 never / 1 always; same seed ⇒ same failure pattern over 50 cases, different seed differs; `counts_as_endpoint_unhealth(exc)` is True for both failure kinds (import it from `app.gateway.gateway`); `failure_latency_ms` default = mean; validation errors for bad spans/rates.
- [ ] **Step 2–4:** FAIL → implement → PASS (`tests/eval -q`; existing simulated tests updated for the new `set_case` signature).
- [ ] **Step 5: commit** — `feat(eval): simulated outages and flaky failures (B48)`.

---

### Task 2: Matrix rows record unhealth; warm signals count only unhealth

**Files:** `app/eval/oracle.py` (MatrixRow fields only), `app/eval/runner.py` (`_sweep_one` + passing the case index), `app/eval/strategies.py` (`warm_signals` filter), tests.

**Produces:**
- `MatrixRow.counts_unhealth: bool | None = None` and `MatrixRow.failure_latency_ms: float | None = None`, serialised into `matrix.jsonl`.
- `_sweep_one` records, on a failed sweep call, `counts_unhealth = counts_as_endpoint_unhealth(exc)` for the ROOT exception (unwrap `BackendUnavailable.__cause__` / the pipeline's wrapping — find what `Pipeline.run` raises when the backend raises and walk `__cause__`/`__context__` to the original; record the class name found) and `failure_latency_ms = getattr(root_exc, "failure_latency_ms", None)`.
- `warm_signals` applies `observe_error` only for rows with `counts_unhealth is True`; other error rows are skipped (neither success nor error).

- [ ] **Step 1: failing tests** — a sweep over a simulated outage produces rows with `counts_unhealth True` and the profile's failure latency; a monkeypatched backend raising a 400-status exception yields `counts_unhealth False`; `warm_signals` over a mix leaves availability untouched by a `counts_unhealth False` row and lowered by a True row.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): matrix rows record endpoint-unhealth; warm signals align with production (B48c)`.

---

### Task 3: Failover-aware scoring (pure oracle)

**Files:** `app/eval/oracle.py`, `tests/eval/test_oracle.py`.

**Produces:**
- `Choice` gains `attempts: tuple[str, ...] = ()`. `endpoint` stays for compatibility = the **served** endpoint (first successful attempt) or `None`.
- `def attempt_outcome(rows_by_endpoint: dict[str, MatrixRow], attempts: tuple[str, ...]) -> dict` → `{"served": str|None, "quality": float, "cost_usd": float, "latency_ms": float|None, "attempts": int, "wasted_attempts": int, "exhausted": bool}` per spec §5: iterate attempts; a failed row adds `failure_latency_ms or 0.0` to latency and 0 to cost; the first successful row ends the chain and adds its cost/latency/quality; if none succeed → `served None`, quality 0, `exhausted True`, latency = Σ failure latencies (report it; excluded from latency regret like before).
- `strategy_metrics` scores from `attempt_outcome` (when `choice.attempts` is empty, treat `(choice.endpoint,)` as the chain — back-compat for tests); adds `wasted_attempts` (mean over non-empty cases) and `exhausted_rate`; `mean_chosen_cost` uses the failover-aware cost; a case is a **pass** iff served quality ≥ floor. Cost/latency regret still only over cases where the outcome passed and `c*`/`l*` exist.
- **Violations over every attempt:** in `strategy_metrics` and in `violations()`, any attempt not in `case.eligible` is a violation.
- Rounding/determinism rules as before.

- [ ] **Step 1: failing tests** (hand-computed): chain [dead, ok] → served ok, quality ok's, cost = ok's, latency = dead.failure_latency + ok.latency, wasted 1; chain [dead, dead, dead] → exhausted, quality 0; a cap is the caller's job (not enforced here) — test that `attempt_outcome` uses exactly the given list; violation when the 2nd attempt is ineligible; `mean_chosen_cost` of a strategy picking a dead endpoint first is not lower than one picking the live endpoint (B48 b); empty `attempts` falls back to `endpoint`.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): failover-aware strategy scoring (B48b,d)`.

---

### Task 4: Attempt orders and the three router arms

**Files:** `app/eval/strategies.py`, `tests/eval/test_strategies.py`.

**Produces:**
- Every strategy returns an **attempt order** `tuple[str, ...]` (empty tuple = no choice) capped at `max_attempts = 1 + settings.routing.max_failover_attempts`, via a new protocol `AttemptStrategy = Callable[[StrategyInput, Callable[[str], bool]], tuple[str, ...]]` where the second argument `failed(slug) -> bool` tells the strategy whether an attempt failed (looked up in the matrix by the runner). Baselines: rank all eligible by their key and take attempts in order until one does not fail or the cap is hit. Router: repeat `Router.select(ctx, tokens_after, complexity, output_tokens=..., exclude=frozenset(failed_so_far))` until a non-failing pick, the cap, or `NoEligibleCandidate`.
- `class OnlineArm`: holds a `_FrozenReadSignalStore` (or a variant that advances its frozen anchor per case); `choose(inp, failed) -> tuple[str, ...]` uses its current store for `Router.select`; `observe(case_index, attempts, rows_by_endpoint)` applies, in attempt order, `observe_error(slug)` for failed attempts with `counts_unhealth True` (skip False), `observe_success(slug, row.latency_ms)` for the served attempt, with `now` = a monotonic value derived from `case_index` (1 s per case, sub-steps within a case), and the frozen read anchor advanced to that case's last observation before the next `choose`.
- `warm_prior_signals(settings, rows_before_case) -> store` = `warm_signals` over rows of cases `< k` only (the runner passes only prior rows).
- Remove the old `router_warm` construction path from any helper that implied current-case replay; keep `warm_signals` itself.

- [ ] **Step 1: failing tests** — baseline attempt chains on a registry with one failing endpoint; router chain equals repeated direct `Router.select(... exclude=...)`; cap enforced; **leakage falsification**: an `OnlineArm` scoring case k has no observation with case index ≥ k (inject a leak by calling `observe` before `choose` and assert the test fails); online arm availability for an endpoint drops after its failed attempts and not after `counts_unhealth False` failures; with no failures, availability is exactly 1.0 and weight variants within the online arm give identical attempt chains.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): attempt orders, online router arm, prior-only warm arm (B48a)`.

---

### Task 5: Runner wiring, outputs, outage scenario

**Files:** `app/eval/runner.py`, `scripts/feasible_bench.py` (only if flags change), `eval/suites/outage.jsonl`, `eval/endpoints/outage-sim.yaml`, `tests/eval/test_runner.py`, `tests/eval/test_starter_suite.py`, new `tests/eval/test_outage_scenario.py`.

**Produces:**
- Runner passes the case index to `set_case`; strategies are called with `failed = lambda slug: rows_by_endpoint[slug].error is not None`; arms: `cheapest`, `best_published_quality`, `fixed:<slug>`, `router_cold`, `router_online`, `router_warm_prior`, and per variant `router_cold:<v>`, `router_online:<v>`, `router_warm_prior:<v>` (one `OnlineArm` per online arm/variant, observing after each case in suite order). `router_warm` is gone.
- `choices.jsonl` rows carry `attempts` and the attempt outcome; `metrics.json` gains `wasted_attempts`, `exhausted_rate` per strategy; `report.md` shows them and a line stating B45 is read from `router_online:<variant>`; `run.json` notes: arms' meaning, case-order dependence, 1 s synthetic spacing.
- Exit-2 rule covers violations on any attempt (via `violations()` over attempts and `strategy_metrics`).
- **Outage scenario**: `eval/suites/outage.jsonl` ~30 public FAQ cases (`residency: any`, graders with answers), `eval/endpoints/outage-sim.yaml` with two `cheap`-tier onprem endpoints — `fast-cheap` (lower price, outage over roughly cases 10–19, quality high) and `steady` (higher price, `error_rate` ≈ 0.05) — header comments explaining the design. Pick prices so the router's cost term prefers `fast-cheap` when healthy.
- Re-run the existing starter/empty-set runs: exit 0, violation rate 0; the no-failure invariant holds (all availability variants within each arm identical).

- [ ] **Step 1: failing tests** — e2e outage run: exit 0, byte-identical `metrics.json` across two runs with the same seed; `router_online:availability_0` has more `wasted_attempts` during the outage than `router_online:availability_2` (or, if the simulation shows otherwise, the test asserts the variants DIFFER and the report records the direction — do not tune the scenario to force a desired answer; report what it shows); `router_warm_prior` ≤ `router_online` in wasted attempts (upper bound); starter run still exits 0 with violation 0.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): wire failover scoring and router arms; outage scenario (B48)`.

---

### Task 6: Documentation and the B45 result

**Files:** `docs/research/feasible-route-bench.md`, `docs/guides/feasible-route-bench.md`, `docs/backlog/bugs.md`.

- [ ] **Step 1:** run the outage scenario with `eval/variants/b45.yaml` at seeds 0–9 (scratch dir outside the repo) and the starter + empty-set runs; collect metrics.
- [ ] **Step 2: research notes** — new section: failure model; failover-aware definitions (attempt outcome, wasted attempts, exhausted rate); the three arms and why the leaky arm was removed (with the falsification); **the B45 result** read from `router_online:<variant>` across seeds, with stamps; limits (simulated failures, case-indexed outages, 1 s spacing drives decay, the scenario is designed, a live run is pending). Re-cite starter-run numbers that changed because the arm set changed.
- [ ] **Step 3: guide** — new profile fields, arm names, new metrics, reading B45 from the report.
- [ ] **Step 4: backlog** — B48 `✅ RESOLVED (2026-09-22, <commit>)` with how; B45 dated update with the simulated answer (stays open pending a live run).
- [ ] **Step 5: commit** — `docs(eval): failure injection, failover scoring, first B45 answer`.
