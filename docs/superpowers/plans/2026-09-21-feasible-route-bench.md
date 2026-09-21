# FeasibleRouteBench (P2b-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An offline, operator-run harness that sends each benchmark case to every endpoint the case is *allowed* to use (through RouteForge's real pipeline), computes the feasible oracle, and scores routing strategies (baselines and the real router, cold/warm, weight variants) by feasible-oracle regret.

**Architecture:** New package `app/eval/` (never imported by the serving path) + CLI `scripts/feasible_bench.py`. Pure modules for cases/graders, endpoint files, the simulated backend, the oracle/metrics, and strategies; one runner that assembles a real `Pipeline` over a scratch in-memory registry and drives the sweep.

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, `jsonschema` (check it is already a dependency; if not, implement `json_schema` grading with the `jsonschema` package added to `pyproject.toml` under the main deps — record the decision), pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md` — binding. Read it before any task.

## Global Constraints

- **Nothing under `app/` except `app/eval/` may import `app.eval`.** The harness is operator-run, outside the serving process.
- **The harness never touches a deployment database.** Registry = scratch `Database(url="sqlite+aiosqlite://")` + `store.upsert_onboarded` + `ModelRegistry.refresh`.
- **No eligibility bypass.** Every swept endpoint came from `feasible_set(...).eligible` for that case; the router strategies use the real `Router.select`.
- **Policy-violation rate must be 0; otherwise CLI exit code 2.** Empty-set mismatch also exits 2. Load/usage errors exit 1.
- **Cost/latency regret are never averaged over floor misses.**
- **Completion text is not persisted unless `--keep-text`.**
- **Simulated mode is deterministic for a given `--seed`** (byte-identical `metrics.json`).
- **Synthetic PII only** in suites: the loader rejects a case the regex detector flags unless `tags.synthetic` is `true`.
- `api_key_env` is a variable NAME; reject values that look like keys (contain `sk-`, or are ≥ 32 chars of `[A-Za-z0-9_-]` with lowercase letters, or contain whitespace).
- Tests: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest ...`; no pytest-asyncio (use `asyncio.run`). Shell: `/usr/bin/grep`, `rtk proxy git`.
- Commits end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Key existing interfaces (verified 2026-09-21)

- `AppPolicyView` (`app/apps/policy.py:23-59`): frozen dataclass; fields `residency, allowed_tiers, allowed_providers, allowed_models, max_spend_usd_per_day, max_cost_usd_per_request, max_rpm, compression_enabled, capture_io, policy_mode, pii_detection, stream_mode, pii_entity_denylist, pii_score_threshold, pii_exempt_patterns, model_input, client_output, content_integrity, escalate_residency_on_detection, allowed_regions, require_no_retention` (set-typed fields are `frozenset`).
- `AppPrivacyProfile(recognizers=(), rules=())` (`app/apps/privacy.py:23`).
- `Pipeline(settings, gate, detector, gateway, co_cfg_provider=lambda: settings.serving.context_optimization, ccr=CCRStore(ttl_seconds=...), registry=registry, memory=None, cache=None)`; `await pipeline.run(request_id, tenant_id, ChatRequest, app_policy=..., profile=...)` → dict with `model`, `usage` (`prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens`), `choices[0]["message"]["content"]`, `trace` (DecisionRecord dicts; the step=`"router"` record has `actual_cost_usd`, `latency_ms`), `cost.total_usd`.
- Construction order (from `app/main.py`): `settings`; `detector = make_detector(settings.serving.pii_detection)`; `registry = ModelRegistry(settings)`; `gate = Gate(settings, detector, registry=registry)`; `signals = SignalStore(settings.routing.signals)`; `router = Router(settings, registry=registry, signals=signals)`; `gateway = Gateway(settings, backend, router, registry, signals=signals)`.
- Pinning: `ChatRequest(model=slug, messages=...)`; a non-permitted pin raises `PipelineRejected`.
- `feasible_set(settings, registry, ctx_or_policy, *, tenant_id)` accepts an `AppPolicyView` directly → `FeasibleSet(eligible: tuple[PoolCandidate], rejected: tuple[Rejection])`, `Rejection(name, provider, tier, display_name, reason, code)`.
- `gate.evaluate(request_id, tenant_id, request, app_policy=...)` → `(ctx, reps, eff)`.
- `Router.select(ctx, tokens_after, complexity, min_tier=None, exclude=frozenset(), output_tokens=None)` → `RoutingDecision` (`.chosen.name`). Weights are read live from `settings.routing.weights` — **a weight variant needs its own deep-copied `Settings`**.
- `Router._est_cost` = `price_call(registry.price_for(name), prompt_tokens=tokens_after, completion_tokens=output_tokens)`.
- Providers: a cloud provider is allowed only if `settings.serving.providers[<name>].enabled`; onboarded rows may carry any provider string. The harness enables every provider named in the endpoint file on its deep-copied `Settings`.
- `store.upsert_onboarded(session, slug, **fields)` with fields `display_name, provider, tier, quality, price_input, price_output, price_cached_input, endpoint, api_key_env, context_window, enabled, allowed_app_ids, probe_status, probe_error, notes, region, prompt_retention, accepts_raw_sensitive`; `ModelRegistry.refresh(db)`; `registry.registry_version()`.
- `InferenceBackend` (`app/gateway/backends/base.py:59-77`): `name`, `async generate(model, messages, price, tier, hint, tools=None, tool_choice=None, params=None) -> GenerationResult(model, text, tokens_in, tokens_out, latency_ms, cost_usd, confidence=None, tool_calls=None, cached_prompt_tokens=0)`, `generate_stream(...)`, `async health()`. Helper `count_tokens` in the same module.
- `price_call(price, *, prompt_tokens, completion_tokens, cached_prompt_tokens=0)` (`app/routing/pricing.py`).
- `Observation(ts, model, outcome, latency_ms, streamed, ttft_ms)`; `SignalStore(cfg, sink=None, wall_offset=None).replay(obs)`.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/eval/__init__.py` | package docstring: offline harness, never imported by serving |
| `app/eval/graders.py` | the five graders |
| `app/eval/cases.py` | `Case` model, suite loader + validation, suite hash |
| `app/eval/endpoints.py` | endpoint file loader + validation, harness `Settings`, scratch registry builder |
| `app/eval/simulated.py` | deterministic `SimulatedBackend` |
| `app/eval/oracle.py` | `MatrixRow`, `Choice`, oracle, metrics, worst group, headline — pure |
| `app/eval/strategies.py` | baselines + router strategies (cold/warm/variants) |
| `app/eval/runner.py` | assembles the real pipeline, gate + feasible set, sweep, strategies, writes results |
| `scripts/feasible_bench.py` | CLI, exit codes, live confirmation |
| `eval/suites/starter.jsonl`, `eval/endpoints/healthcare-sim.yaml`, `eval/endpoints/spark-live.example.yaml`, `eval/variants/b45.yaml` | shipped data |
| `tests/eval/test_*.py` | one test file per module + `test_end_to_end.py` |

---

### Task 1: Graders and case suite

**Files:** create `app/eval/__init__.py`, `app/eval/graders.py`, `app/eval/cases.py`, `tests/eval/__init__.py` (if the repo's tests dirs use them — check `tests/routing/`), `tests/eval/test_cases.py`.

**Interfaces — produces:**
- `graders.grade(spec: GraderSpec, text: str) -> float` (0.0 or 1.0); `GraderSpec` pydantic model: `kind: Literal["exact","contains","regex","json_schema","numeric"]`, `expected: str | dict | float`, `tol: float = 0.0`.
- `cases.Case` pydantic model: `id: str`, `messages: list[dict]` (role/content), `app_policy: dict = {}`, `grader: GraderSpec`, `quality_floor: float = 1.0`, `tags: dict[str, str | bool] = {}`, `expect_empty: bool = False`; method `policy_view() -> AppPolicyView` (convert list-valued fields to `frozenset`; unknown keys → `ValueError`).
- `cases.load_suite(path: str) -> tuple[list[Case], str]` (cases, sha256 of the file's normalised content: each line JSON-parsed and re-dumped with `sort_keys=True`, joined by `\n`). Raises `SuiteError(ValueError)` with the case id for every §4.1 load error.

- [ ] **Step 1: failing tests** — graders: `exact` strips; `contains` case-insensitive; `regex` full-match (and an invalid regex rejected at load, not at grade); `json_schema` valid/invalid/non-JSON; `numeric` first number within `tol` (`"about 41.9 units"` vs 42 ± 0.2 → 1.0; ± 0.05 → 0.0). Loader: duplicate id; unknown grader kind; missing expected; invalid schema; unknown `app_policy` key; a case containing `078-05-1120` without `tags.synthetic: true` → `SuiteError`, with it → loads; hash stable under key reordering within a line.
- [ ] **Step 2: run, expect FAIL.**
- [ ] **Step 3: implement.** Regexes are compiled at load (store compiled pattern on a private attr). The synthetic-PII check uses `make_detector("regex")` over every message's content; any detected entity without `tags.get("synthetic") is True` → `SuiteError`. `policy_view()` builds `AppPolicyView(**converted)`.
- [ ] **Step 4: run, expect PASS.**
- [ ] **Step 5: commit** — `feat(eval): graders and case-suite loader`.

---

### Task 2: Endpoint files, harness Settings, scratch registry

**Files:** create `app/eval/endpoints.py`, `tests/eval/test_endpoints.py`.

**Interfaces — produces:**
- `EndpointSpec` pydantic: `slug, provider, tier, price: {input, output, cached_input: float|None=None}, quality: float|None=None, region: str|None, prompt_retention: str|None, accepts_raw_sensitive: bool|None, simulated: SimProfile|None, live: LiveTarget|None`. `SimProfile`: `quality: dict[str, float]` (must include `default`, values in [0,1]), `latency_ms: {mean: float, jitter: float = 0}`. `LiveTarget`: `endpoint: str`, `api_key_env: str|None`.
- `load_endpoints(path, *, live: bool) -> tuple[list[EndpointSpec], str]` (specs, sha256 of the raw file bytes). Errors → `EndpointFileError(ValueError)`: both/neither of simulated/live; `live` entry when `live=False`; `simulated` entry when `live=True` (a run is all one mode); key-looking `api_key_env` (see Global Constraints); tier not in the canonical tiers; duplicate slug.
- `harness_settings(base: Settings, specs) -> Settings` — `copy.deepcopy(base)`; set `serving.providers[p].enabled = True` for every provider named (create the provider entry if the settings model allows; if a provider name is unknown to `serving.providers`, raise `EndpointFileError` — record which providers exist); disable semantic cache and memory (`serving.semantic_cache.enabled=False`, `serving.memory.enabled=False`) so the sweep measures endpoints, not caches.
- `async build_registry(settings, specs) -> tuple[ModelRegistry, Database]` — scratch in-memory DB, `ensure_ready()`, one `upsert_onboarded` per spec with `enabled=True, probe_status="healthy"`, endpoint = `live.endpoint` or `f"sim://{slug}"`, prices, declarations; commit; `ModelRegistry(settings).refresh(db)`.

- [ ] **Step 1: failing tests** for every load error; `harness_settings` enables named providers and does not mutate `base`; `build_registry` → `effective_pool()` contains every slug with its declared provider/tier/region, `price_for(slug)` matches, and a spec with `provider: azure` survives `feasible_set(settings, registry, AppPolicyView(), tenant_id="t")` (proves the provider enablement works) while `AppPolicyView(residency="onprem_only")` excludes it.
- [ ] **Step 2–4:** run FAIL → implement → PASS.
- [ ] **Step 5: commit** — `feat(eval): endpoint files, harness settings, scratch registry`.

---

### Task 3: Simulated backend

**Files:** create `app/eval/simulated.py`, `tests/eval/test_simulated.py`.

**Interfaces — produces:** `SimulatedBackend(specs: list[EndpointSpec], seed: int)` implementing `InferenceBackend` (`name = "simulated"`). It needs to know the current case: `backend.set_case(case: Case)` is called by the runner before each case's sweep. `generate(model, messages, price, tier, hint, ...)`:
- `u = random.Random(f"{seed}:{case.id}:{model}").random()`;
- `q = profile.quality.get(str(case.tags.get("workload")), profile.quality["default"])`;
- text = the case's expected answer rendered for its grader (`exact`/`contains`/`regex` → a string that passes: `exact`/`contains` → `str(expected)`; `regex` → the case must carry `grader.sample` for simulation — add optional `sample: str|None` to `GraderSpec` and make the loader require it for `regex`/`json_schema` cases only when the runner is in simulated mode; simplest: require `sample` on regex/json_schema cases always, validated to pass its own grader at load; `numeric` → `str(expected)`) if `u < q`, else `"SIMULATED-WRONG"`;
- `latency_ms = max(1.0, mean + jitter * (2*u2 - 1))` with `u2 = Random(f"{seed}:{case.id}:{model}:lat").random()`;
- tokens: `tokens_in = sum(count_tokens(m.content) for m in messages)`, `tokens_out = count_tokens(text)`; `cost_usd = price_call(price, prompt_tokens=tokens_in, completion_tokens=tokens_out)`; `confidence=1.0` (so the gateway's confidence escalation never fires in simulation; document this);
- it **records the messages it received** per `(case.id, model)` in `self.received` (for the redaction test).
- `generate_stream` raises `NotImplementedError("simulated backend does not stream")`; `health()` returns a healthy `BackendHealth` (check its constructor).

- [ ] **Step 1: failing tests** — same seed ⇒ same text/latency; different seed changes at least one outcome over 50 draws; `u<q` semantics at q=0 (always wrong) and q=1 (always right); cost equals `price_call` of the counted tokens; `received` captured.
- [ ] **Step 2–4.** Also update `Task 1`'s loader for `sample` (this task owns that change; add its tests here: a regex case without `sample` → `SuiteError`; a `sample` that fails its own grader → `SuiteError`).
- [ ] **Step 5: commit** — `feat(eval): deterministic simulated backend`.

---

### Task 4: Oracle and metrics (pure)

**Files:** create `app/eval/oracle.py`, `tests/eval/test_oracle.py`.

**Interfaces — produces:**

```python
@dataclass(frozen=True)
class MatrixRow:
    case_id: str
    endpoint: str
    quality: float
    passed: bool
    cost_usd: float
    latency_ms: float | None      # None when error
    error: str | None = None

@dataclass(frozen=True)
class CaseInfo:
    case_id: str
    eligible: tuple[str, ...]
    expect_empty: bool
    rejected_ok: bool             # True if an empty case was rejected with >=1 reason and no calls
    tags: dict

@dataclass(frozen=True)
class Choice:
    case_id: str
    strategy: str
    endpoint: str | None          # None when the strategy made no choice (empty set)

def case_oracle(rows: list[MatrixRow]) -> dict   # {"q_star", "c_star", "l_star", "passing": [...], "infeasible_at_floor": bool}
def strategy_metrics(cases: list[CaseInfo], rows: list[MatrixRow], choices: list[Choice],
                     strategy: str) -> dict
def grouped(cases, rows, choices, strategy, tag: str) -> dict[str, dict]
def worst_groups(cases, rows, choices, strategy) -> dict   # per metric -> (tag, value, metric_value)
def headline(metrics_by_strategy: dict, *, router: str, reference: str, parity_tol: float) -> dict
def violations(cases, choices, sweep_served: list[tuple[str, str]]) -> int
```

`strategy_metrics` returns: `n_cases`, `violation_rate`, `pass_rate`, `quality_regret` (mean `q* − q(choice)` over non-empty cases), `cost_regret` (mean over cases where the choice passed and `c*` defined), `latency_regret` (same), `floor_miss_rate` (cases with passing set non-empty and choice not passing, over cases with passing set non-empty), `infeasible_at_floor` count, `empty_set_mismatches` (expect_empty vs actual emptiness, plus empty cases not `rejected_ok`). A choice's missing matrix row (should not happen) counts as a violation. All floats rounded to 12 dp before serialisation (determinism).

`headline`: `{"comparable": bool, "router_pass_rate", "reference_pass_rate", "saving_pct": (ref_cost − router_cost)/ref_cost*100 or None}` where costs are mean chosen cost over all non-empty cases; `comparable` iff `abs(pass_rate diff) <= parity_tol` and `ref_cost > 0`.

- [ ] **Step 1: failing tests**, with hand-computed fixtures:
  1. **§14 fixture**: endpoints local-llama (q .92 ⇒ with floor 0.9 passes, $0.08, 900 ms), azure-us ($0.03, 600, passes), european ($0.02, 500 — not eligible), general-us (retains — not eligible). Eligible = {local-llama, azure-us}. `c*` = azure-us 0.03; a strategy choosing local-llama has cost regret 0.05; choosing european ⇒ violation.
  2. Floor miss: cheapest endpoint fails, a pricier one passes; strategy picks the cheap failing one ⇒ `floor_miss_rate` 1.0 and **cost_regret not negative / not computed for that case**.
  3. All fail ⇒ `infeasible_at_floor`; only quality regret counts.
  4. Error rows (quality 0, latency None) excluded from latency aggregates.
  5. Empty-set mismatch counting both directions.
  6. `grouped`/`worst_groups` on two workloads.
  7. `headline` comparable vs not.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): feasible oracle and regret metrics`.

---

### Task 5: Strategies

**Files:** create `app/eval/strategies.py`, `tests/eval/test_strategies.py`.

**Interfaces — produces:** a strategy is `Callable[[StrategyInput], str | None]` where

```python
@dataclass(frozen=True)
class StrategyInput:
    case: Case
    ctx: ExecutionPolicyContext        # from gate.evaluate for the case with model="auto"
    eligible: tuple[PoolCandidate, ...]
    tokens_after: int                  # prompt tokens the sweep saw (first successful row), else count_tokens of the sanitized prompt
    output_tokens: int                 # expected_output_tokens(settings, params) — reuse the router's helper
```

Constructors:
- `cheapest(registry)` → min over eligible of `price_call(registry.price_for(name), prompt_tokens=tokens_after, completion_tokens=output_tokens)`, tie by slug.
- `best_published_quality(registry, settings)` → max published quality (registry quality or the tier default from the quality resolver the router uses — reuse, do not re-derive), tie by the same cost estimate then slug.
- `fixed(slug, fallback)` → slug if eligible else fallback(input) (the runner records `fallback_used`).
- `router(settings, registry, signals)` → `Router(settings, registry=registry, signals=signals).select(inp.ctx, inp.tokens_after, min(1.0, inp.tokens_after / 2000), output_tokens=inp.output_tokens).chosen.name`; `NoEligibleCandidate` → `None`. (The `/2000` complexity mirrors `app/pipeline.py`; find and reuse the pipeline's expression/helper if one exists rather than copying the constant.)
- `router_variant(base_settings, weights, registry, signals)` → deep-copies `base_settings`, sets `routing.weights = weights`, returns `router(...)`.
- `warm_signals(settings, rows, wall_offset=0.0) -> SignalStore` → builds `Observation`s from matrix rows in sweep order (`ok` → latency, `error` → error; ts = `i` seconds apart) and replays them into a fresh store; it must use `SignalStore.replay`, not direct observe calls.

- [ ] **Step 1: failing tests** — each baseline on a small registry built with Task 2's `build_registry`; `router` matches a direct `Router.select` call; a variant with `availability` weight 0 vs 5 changes the choice between a slow-available and a fast-flaky candidate in the same tier after `warm_signals` (proves variants and warm state reach the router); `fixed` fallback path.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): baseline and router strategies`.

---

### Task 6: Runner and CLI

**Files:** create `app/eval/runner.py`, `scripts/feasible_bench.py`, `tests/eval/test_runner.py`, `tests/eval/test_isolation.py`.

**Interfaces — produces:** `async run_bench(suite_path, endpoints_path, *, out_dir, seed=0, live=False, keep_text=False, variants_path=None, parity_tol=0.02, fixed=None) -> RunResult(exit_code: int, out_dir: str, metrics: dict)`.

Per run: load suite + endpoints (errors → exit 1), `settings = harness_settings(load_settings(), specs)`, registry, backend (`SimulatedBackend` or the real LiteLLM backend via `make_backend(settings, registry)` — check `app/gateway/backends/factory.py`), gate/router/gateway/pipeline per the Key Interfaces block (the pipeline's own signal store is a fresh one and is **not** reused by the strategies).

Per case:
1. `app_policy = case.policy_view()`; `profile = AppPrivacyProfile(recognizers=(), rules=())`; tenant id `"feasible-bench"`.
2. `fs = feasible_set(settings, registry, app_policy, tenant_id=...)` for the pre-sweep eligible list, and `ctx, reps, eff = gate.evaluate(rid, tenant, ChatRequest(model="auto", ...), app_policy=app_policy)`, then the **post-gate** `feasible_set(settings, registry, ctx, tenant_id=...)` — the post-gate set is authoritative (the gate may tighten residency); record both, and assert post ⊆ pre.
3. Empty post-gate set: record `CaseInfo(rejected_ok = len(fs.rejected) >= 1 and no calls made)`; no sweep.
4. Sweep: for each eligible `e`, `backend.set_case(case)` (simulated), `resp = await pipeline.run(rid, tenant, ChatRequest(model=e, messages=...), app_policy=app_policy, profile=profile)`; `served = resp["model"]`; quality via grader on `resp["choices"][0]["message"]["content"]`; cost = `resp["cost"]["total_usd"]`; latency = the `step == "router"` trace record's `latency_ms`; exceptions → error row. Append a JSONL line to `matrix.jsonl` immediately (text only with `keep_text`). Record `(case_id, served)` for the violation check.
5. Strategies over `StrategyInput` → `choices.jsonl`: `cheapest`, `best_published_quality`, `fixed:<slug>` when `--fixed` given, `router_cold` (fresh `SignalStore`), `router_warm` (`warm_signals` over the rows swept **so far including this case** — document the choice), and per variant `router_cold:<name>` / `router_warm:<name>`.
6. After all cases: metrics per strategy + grouped per tag + worst groups + headline (`router_warm` vs `best_published_quality`) → `metrics.json`; `report.md`; `run.json` stamp (suite hash, endpoints hash, `settings.routing_policy_version`, `registry.registry_version()`, seed, `git rev-parse HEAD` if available else `"unknown"`, mode, args). Exit 2 if any strategy's violations > 0 or any empty-set mismatch; else 0.

CLI (`scripts/feasible_bench.py`, argparse): `--suite`, `--endpoints`, `--out` (default `results/<utc timestamp>`), `--seed`, `--live`, `--yes`, `--keep-text`, `--variants`, `--fixed`, `--parity-tol`. In `--live` mode print `N cases × M endpoints ≤ K calls` and the endpoint list; without `--yes` prompt `Proceed? [y/N]` on stdin. `sys.exit(result.exit_code)`. Add `results/` to `.gitignore`.

- [ ] **Step 1: failing tests** (simulated, tiny inline suite + endpoint file written to `tmp_path`):
  1. End-to-end run writes all five files; `metrics.json` has every strategy; exit 0.
  2. **Determinism:** two runs, same seed ⇒ byte-identical `metrics.json`.
  3. **Violation guard falsified:** monkeypatch one strategy to return an ineligible slug ⇒ exit 2 and its `violation_rate > 0`.
  4. **Empty set:** an onprem_only case with no onprem endpoint and `expect_empty: true` ⇒ no `matrix.jsonl` rows for it, exit 0; the same with `expect_empty: false` ⇒ exit 2.
  5. **Model-facing view:** an `enforced` case with synthetic SSN `078-05-1120` ⇒ `SimulatedBackend.received` for that case contains no `078-05-1120` (placeholders); a `passthrough` case ⇒ it does.
  6. **Pinned sweep serves the pin:** every matrix row's recorded served model equals its endpoint.
  7. **Keep-text:** without `--keep-text` no row has a `text` key; with it, rows do.
  8. **Isolation** (`test_isolation.py`): walk `app/**/*.py` excluding `app/eval/`; none imports `app.eval` (AST scan).
  9. **CLI:** `subprocess` run of `scripts/feasible_bench.py` on the tiny suite exits 0; a `live` endpoint without `--live` exits 1.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): feasible_bench runner and CLI`.

---

### Task 7: Shipped suite, profiles, variants

**Files:** create `eval/suites/starter.jsonl`, `eval/endpoints/healthcare-sim.yaml`, `eval/endpoints/spark-live.example.yaml`, `eval/variants/b45.yaml`, `tests/eval/test_starter_suite.py`.

- **Starter suite (~24 cases)**, synthetic PII only (`tags.synthetic: true` wherever an entity appears; SSNs from the reserved `078-05-1120` / `219-09-9999` style), with `workload`, `placement`, `protection` tags, covering §15:
  - 8 public FAQ (`residency: any`, `policy_mode: enforced`) — `contains`/`exact`/`numeric` graders;
  - 8 patient claims with direct PII (`allowed_regions: ["us"]`, `require_no_retention: true`, `policy_mode: enforced`) — `contains`/`regex`/`json_schema`;
  - 4 on-prem-required (`residency: onprem_only`);
  - 4 on-prem-required against an endpoint file with no on-prem endpoint → covered by a second small endpoint file `eval/endpoints/cloud-only-sim.yaml` and a separate test run with `expect_empty: true` cases in `eval/suites/empty-set.jsonl` (keep the starter suite's `expect_empty` cases in their own file so the main run's exit code stays meaningful).
- **`healthcare-sim.yaml`** = the §14 table: `local-llama` (onprem, cheap?, $0.08 flat per the table → express as per-1M prices that reproduce roughly $0.08 per case at the suite's token sizes; document the conversion in a YAML comment), `azure-us` (azure, region us, retention none, accepts_raw_sensitive false), `european` (azure or bedrock, region eu), `general-us` (region us, retention retained). Quality per workload from the §14 table (as probabilities).
- **`spark-live.example.yaml`** — qwen3.8-27b, nemotron-3.5-lightning, llama3.2:latest with `live: {endpoint: "http://<spark>:8000/v1" ...}` placeholders and a header comment: copy, replace `<spark>`, run with `--live`.
- **`b45.yaml`** — variants: `default` (current weights), `availability_0`, `availability_1`, `availability_2` (other weights unchanged).

- [ ] **Step 1: failing test** — the starter suite loads; a simulated run of `starter.jsonl` × `healthcare-sim.yaml` with `b45.yaml` exits 0 with violation rate 0 for every strategy, and `european`/`general-us` never appear in any claim case's eligible list; `empty-set.jsonl` × `cloud-only-sim.yaml` exits 0 with all cases `rejected_ok`.
- [ ] **Step 2–4.**
- [ ] **Step 5: commit** — `feat(eval): starter suite, healthcare simulation, B45 variants`.

---

### Task 8: Documentation and first results

**Files:** create `docs/research/feasible-route-bench.md`, `docs/guides/feasible-route-bench.md`; modify `docs/backlog/bugs.md` (B45), `README.md` or `scripts/README.md` (one line pointing at the CLI — whichever lists scripts).

- [ ] **Step 1:** run `uv run python scripts/feasible_bench.py --suite eval/suites/starter.jsonl --endpoints eval/endpoints/healthcare-sim.yaml --variants eval/variants/b45.yaml --seed 0 --out /tmp/<scratch>/fb-starter` and the empty-set run; keep `report.md` / `metrics.json` for the docs (do not commit results).
- [ ] **Step 2: research notes** — formal definitions (spec §6); invariants and the test that pins each (violation guard incl. its falsification, determinism, model-facing view, isolation); the §14 worked example **as computed by the harness**; cold vs warm router results; the **first B45 result** (the variants' pass rate / cost regret / floor-miss rate), stated as a simulated result with its seed and suite hash; "what is NOT demonstrated": simulated quality is an input not a measurement; deterministic graders only; synthetic cases; no calibration/churn; router_warm uses observations from the same sweep it is evaluated on (optimistic — say so).
- [ ] **Step 3: guide** — writing cases (graders, `sample`, synthetic tag), endpoint files (simulated vs live, `api_key_env` is a name), running, reading `report.md`, exit codes, the live-mode cost warning.
- [ ] **Step 4: backlog B45** — add a dated update pointing at the harness and the first simulated result; keep it open (a live run is still needed).
- [ ] **Step 5: commit** — `docs(eval): FeasibleRouteBench research notes, guide, first B45 result`.
