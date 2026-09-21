# FeasibleRouteBench — the feasible-oracle evaluation harness (P2b-1)

**Status:** approved design, 2026-09-21.
**Source:** Priority 2, §12–21 of `docs/new/RouteForge-Priorities-1-3-LLM-Handoff.md`.
**Builds on:** P1a (`compile_policy`, decision certificate), P1b (`app/policy/feasible.py`
`FeasibleSet` with exclusion reasons), P2a (scoring record, `RoutingStrategy`), signal
hardening (`SignalStore.replay`, `Observation`).

---

## 1. Why, and the problem this slice solves

Priority 2 asks RouteForge to be judged against the **feasible oracle**: the best endpoint the
request was *allowed* to use — not the best endpoint in the registry. A router that "loses" to a
cheaper European model it may never send US patient data to has not lost anything.

The obstacle: the oracle needs outcomes for **every eligible endpoint** of a request, and
production observes only the endpoint it chose. The oracle cannot be computed from live traffic.

P2b is therefore decomposed (user decision, 2026-09-21):

| Slice | What | Status |
|---|---|---|
| **P2b-1** | Offline harness: sweep every eligible endpoint per case, compute the oracle, score strategies | **this spec** |
| P2b-2 | Production outcome records (§16) exported as a dataset | later |
| P2b-3 | Learning / exploration bounded by the feasible set | later — needs P2b-1 to measure it |

## 2. Decisions (user answers, 2026-09-21)

1. **P2b-1 first.**
2. **Deterministic graders** (`exact`, `contains`, `regex`, `json_schema`, `numeric`). An LLM judge
   is out of scope; the grader interface admits one later.
3. **In-process real pipeline.** The harness imports RouteForge and runs the real gate,
   `compile_policy`, `feasible_set`, redaction and compression — not an HTTP client, not a
   re-implementation. Endpoints are real (`--live`) or simulated.

## 3. Boundary

- Lives in a new package **`app/eval/`** and a CLI **`scripts/feasible_bench.py`**. **Nothing on
  the serving path imports `app/eval/`** (asserted by test). It is operator-run, outside the
  serving process — the handoff's "thin runtime router" (§19) and CLAUDE.md rule 4.
- It **never reads or writes a deployment's database.** It builds its own `Settings`, an
  in-memory SQLite `Database`, and a `ModelRegistry` refreshed from rows it writes there from the
  endpoint file (the same pattern as `tests/_litellm_fake.py::make_test_registry`).
- It adds **no eligibility bypass**. Every endpoint it calls is one `feasible_set()` returned as
  eligible for that case.

## 4. Inputs

### 4.1 Case suite (JSONL, one case per line)

```json
{"id": "claim-001",
 "messages": [{"role": "user", "content": "Summarise claim ... SSN 078-05-1120 ..."}],
 "app_policy": {"residency": "any", "policy_mode": "enforced", "pii_detection": "regex",
                "allowed_regions": ["us"], "require_no_retention": true},
 "grader": {"kind": "contains", "expected": "approved"},
 "quality_floor": 1.0,
 "tags": {"workload": "claim_summary", "placement": "any", "protection": "enforced",
          "synthetic": true},
 "expect_empty": false}
```

- `app_policy` holds the fields an `AppPolicy` row holds; the harness builds the view the
  pipeline consumes (`AppPolicyView` / privacy profile) from it **via the same constructors
  production uses** — it does not hand-assemble an `ExecutionPolicyContext`.
- Graders: `exact` (string equality after strip), `contains` (substring, case-insensitive),
  `regex` (full-match on stripped text, compiled at load), `json_schema` (parses and validates),
  `numeric` (first number in the text within `tol`). Each returns quality in `[0,1]`
  (`numeric` and the others are 0/1 in this slice).
- `quality_floor` default `1.0`.
- **Load-time validation fails the whole run** (never mid-run): unknown grader kind, invalid regex
  or schema, missing expected value, duplicate id, or a case whose content the configured
  detector flags as containing PII while `tags.synthetic` is not `true`.
- The suite hash (sha256 of the normalised file) is stamped on every result.

### 4.2 Endpoint file (YAML)

```yaml
endpoints:
  - slug: local-llama
    provider: onprem
    tier: cheap
    price: {input: 0.08, output: 0.08}
    region: us
    prompt_retention: none
    accepts_raw_sensitive: true
    simulated:
      quality: {claim_summary: 0.92, faq: 0.95, default: 0.9}
      latency_ms: {mean: 900, jitter: 100}
  - slug: qwen3.8-27b
    provider: onprem
    tier: cheap
    price: {input: 0, output: 0}
    live: {endpoint: "http://<spark>:8000/v1", api_key_env: SP_ONPREM_API_KEY}
```

Each entry has exactly one of `simulated` / `live`. A run is either all-simulated or `--live`
(any `live` entry without `--live` is a load error). `api_key_env` is a variable **name**; the
loader rejects a value that looks like a key (the B32 lesson). Entries become onboarded rows in
the harness's scratch registry, with the declared provider enabled in the harness `Settings`.

## 5. The run

Per case:

1. **Gate + feasible set.** Real gate + `compile_policy` → context; real
   `feasible_set(settings, registry, ctx, tenant_id=...)` → eligible + rejected with reasons.
   - `expect_empty` and the actual emptiness must agree; an empty case is not swept.
2. **Sweep.** For each eligible endpoint `e`: real `Pipeline.run(..., request with model=e)`
   — a pinned request, so the model sees exactly the model-facing view (redacted or raw)
   production would show it. Record per `(case, e)`: grader quality, pass (`quality >= floor`),
   cost (`actual_cost_usd` from the response), backend latency, cached tokens, `error`
   (exception class + message, when the call fails). The completion text is **not** kept unless
   `--keep-text`.
   - The harness also asserts the pinned call **served `e`** (response `model == e`).
3. **Strategies** — each is a function `(case, feasible_set) -> slug`; its outcome is looked up in
   the matrix (no extra model calls):
   - `cheapest`: lowest estimated cost among eligible (the router's own estimate), ties by slug;
   - `best_published_quality`: highest registry quality (tier default when unset), ties by cost;
   - `fixed:<slug>`: that slug when eligible, else `cheapest` (recorded as a fallback);
   - `router_cold`: real `Router.select` with a **fresh, empty** `SignalStore`;
   - `router_warm`: real `Router.select` with a `SignalStore` that has **replayed the sweep's own
     observations** (`Observation`s built from the sweep rows, in sweep order);
   - `router_weights:<name>`: `router_cold` and `router_warm` with an alternative weights mapping
     from a `--variants` file (the instrument for backlog B45).
4. **Oracle and metrics** (§6).

Everything is deterministic in simulated mode for a fixed `--seed`.

### 5.1 Simulated backend

An `InferenceBackend` (`generate` only; streaming is out of scope). For `(case, endpoint)` it
draws `u = rng(seed, case.id, endpoint.slug)`; it returns the case's expected answer if
`u < quality[workload or default]`, else a fixed wrong answer (`"SIMULATED-WRONG"`), and reports
`latency_ms` drawn from the profile (no sleeping) and cost from `app/routing/pricing.py::price_call`
over the pipeline's token counts. Same seed ⇒ same answer ⇒ same metrics.

### 5.2 Live backend

The real LiteLLM backend. Before calling anything the CLI prints `N cases × M endpoints = K calls`
and the endpoint list, and requires `--live` (plus `--yes` for non-interactive use).

## 6. Oracle and metrics

For case `c` with eligible set `E(c)` and sweep outcomes:

- `q*(c) = max_{e∈E} q(c,e)`
- `P(c) = {e ∈ E : q(c,e) ≥ floor(c)}` (endpoints that pass)
- `c*(c) = min_{e∈P} cost(c,e)`, `l*(c) = min_{e∈P} latency(c,e)`; undefined if `P = ∅`
  (**infeasible-at-floor**).

For strategy `s` choosing `s(c)`:

| # | Metric | Definition |
|---|---|---|
| 1 | Policy-violation rate | share of cases with `s(c) ∉ E(c)` (a missing choice record or a choice with no matrix row also counts); separately, any sweep call that served a non-eligible slug. **Must be 0; any non-zero strategy violation rate or sweep violation makes the CLI exit 2.** |
| 2 | Quality regret | mean `q*(c) − q(c,s(c))`; plus pass rate |
| 3 | Cost regret | mean `cost(c,s(c)) − c*(c)` over cases where `s(c) ∈ P(c)`; plus **floor-miss rate** = among cases with `P ≠ ∅`, the share where `s(c) ∉ P` (denominator: cases with `P ≠ ∅` only) |
| 4 | Latency regret | same shape as cost regret |
| 5 | Empty-set handling | per `expect_empty` case: rejected with ≥1 exclusion reason and no model call; mismatches counted |
| 6 | Worst group | metrics 2–4 (incl. pass rate and floor-miss rate) per value of the tags `workload`, `placement`, `protection`; the worst group per metric named |

Cost and latency regret are **never averaged over floor misses** — a cheap wrong answer must not
look like a saving.

**Headline (§20)**: `router_warm` cost vs `best_published_quality` cost, as a percentage, printed
only when the two pass rates differ by ≤ `--parity-tol` (default 0.02); otherwise the report states
the pass rates and that the claim is not comparable.

**Deferred and named in the report:** calibration (the router emits no confidence), policy/endpoint
churn robustness, noninterference failure rate (P1a's harness owns it).

## 7. Output

`results/<run-id>/`:
- `matrix.jsonl` — one row per `(case, endpoint)`;
- `choices.jsonl` — one row per `(case, strategy)`;
- `metrics.json` — per strategy, per group;
- `report.md` — human summary: run stamp, headline, per-strategy table, worst groups,
  empty-set results, deferred metrics, the `--keep-text` state;
- `run.json` — stamp: suite hash, endpoint-file hash, `routing_policy_version`,
  `registry_version`, seed, git SHA, mode (simulated/live), CLI args.

Exit codes: `0` ok; `1` load/usage error; `2` policy violation or empty-set mismatch.

## 8. Error handling

- A sweep call that raises → row with `quality 0`, `pass false`, `error` set, cost 0, latency
  excluded from latency aggregates. Never dropped.
- All endpoints fail a case → infeasible-at-floor for that case.
- Anything invalid in inputs fails at load (§4).
- Rows are written incrementally; an interrupted run leaves a readable partial `matrix.jsonl`
  and no `metrics.json`.

## 9. Starter suite and profiles (shipped)

- `eval/suites/starter.jsonl` — ~24 cases across the handoff's §15 request types: public FAQ,
  patient claim with direct (synthetic) PII, on-prem-required, and on-prem-required with no
  eligible endpoint (`expect_empty`). Synthetic PII only.
- `eval/endpoints/healthcare-sim.yaml` — the §14 table as simulated endpoints (Local Llama, Azure
  US no-retention, European, General US retaining).
- `eval/endpoints/spark-live.example.yaml` — the Spark's endpoints, as an example for `--live`.

## 10. Tests

1. **Oracle arithmetic** against hand-computed matrices, incl. the §14 example as a fixture:
   European excluded for the US claim; Azure US chosen as `c*` when it passes the floor.
2. **Floor misses** never produce negative cost regret.
3. **Violation guard falsified:** a strategy injected to return an ineligible slug ⇒ metric 1 > 0
   and exit code 2.
4. **Empty-set:** an `expect_empty` case with no on-prem endpoint ⇒ rejected, no sweep calls;
   flipping `expect_empty` ⇒ mismatch, exit 2.
5. **Determinism:** two simulated runs with the same seed ⇒ byte-identical `metrics.json`.
6. **Pinned sweep serves the pinned endpoint** and uses the model-facing view (a `redact` case's
   simulated backend receives placeholders, not the synthetic SSN).
7. **Loader rejections:** each §4 load error, incl. non-synthetic PII and a key-looking
   `api_key_env`.
8. **Serving isolation:** no module under `app/` except `app/eval/` imports `app.eval`.
9. **End-to-end** simulated run of the starter suite through the CLI.

## 11. Documentation

- `docs/research/feasible-route-bench.md` (research notes, owner writes papers from it): formal
  definitions (§6), invariants with their tests, the §14 worked example as computed, the
  cold-vs-warm router comparison, a first **B45 result** from weight variants on the simulated
  suite, and a "what is NOT demonstrated" list (simulated quality is an input, not a measurement;
  deterministic graders only; no churn/calibration; results on synthetic cases).
- `docs/guides/feasible-route-bench.md`: writing cases and endpoint files, simulated vs live runs,
  reading the report.
- `docs/backlog/bugs.md` B45: point at the harness and the first result.

## 12. Out of scope

LLM-as-judge; production outcome records (P2b-2); learning/exploration (P2b-3); calibration and
churn metrics; a console UI; streaming in the sweep; running live against the Spark inside this
slice's build (offered separately).
