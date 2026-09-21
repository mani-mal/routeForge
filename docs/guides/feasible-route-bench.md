# Benchmarking routing against the feasible oracle (FeasibleRouteBench)

FeasibleRouteBench is an operator-run CLI. It scores routing strategies against the
**feasible oracle**: for each test case, the best outcome among the endpoints that case was
*allowed* to use. It does not compare against the best model in the registry. It runs the
real policy gate, `feasible_set`, redaction and `Pipeline.run` in process, then calls
**every eligible endpoint** for each case. From that full matrix it computes regret for
each strategy.

It is not part of the serving path. It builds its own settings, an in-memory database and
a scratch registry from the endpoint file you give it. **It never reads or writes a
deployment's database.** Only the endpoints listed in your file exist in a run. The
deployment's YAML `model_pool` is emptied for the run.

Research notes, definitions and the first results are in
[`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md).

## 1. Quick start (simulated, no model calls)

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex \
  uv run python scripts/feasible_bench.py \
    --suite eval/suites/starter.jsonl \
    --endpoints eval/endpoints/healthcare-sim.yaml \
    --variants eval/variants/b45.yaml \
    --seed 0 --out results/fb-starter
```

- `SP_PII_DETECTION=regex` is needed only if Presidio is not installed. Without it, the
  CLI exits 1 and says so.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` stops LiteLLM fetching a cost map over the network
  at import.
- `--out` defaults to `results/<utc timestamp>`. `results/` is gitignored, so do not
  commit run output.

Shipped inputs:

| File | What it is |
|---|---|
| `eval/suites/starter.jsonl` | 22 cases: 8 public FAQ, 10 patient claims (synthetic PII), 4 `onprem_only` |
| `eval/suites/empty-set.jsonl` | 4 `onprem_only` cases that expect **no** eligible endpoint |
| `eval/endpoints/healthcare-sim.yaml` | the handoff's §14 table as four simulated endpoints |
| `eval/endpoints/cloud-only-sim.yaml` | two cloud endpoints and no on-prem, to pair with `empty-set.jsonl` |
| `eval/endpoints/spark-live.example.yaml` | example `--live` file (`<spark>` is a placeholder) |
| `eval/variants/b45.yaml` | routing-weight variants for backlog B45 |

## 2. Writing cases

A suite is JSONL, one case per line:

```json
{"id": "claim-001",
 "messages": [{"role": "user", "content": "Patient: Jane Doe. SSN 078-05-1120. ... state the approval decision."}],
 "app_policy": {"residency": "any", "policy_mode": "enforced", "pii_detection": "regex",
                "allowed_regions": ["us"], "require_no_retention": true,
                "escalate_residency_on_detection": false},
 "grader": {"kind": "contains", "expected": "approved"},
 "quality_floor": 1.0,
 "tags": {"workload": "claim_summary", "placement": "us_only", "protection": "enforced",
          "synthetic": true},
 "expect_empty": false}
```

- **`app_policy`** holds the fields of a per-app policy (`AppPolicyView`), such as
  `residency`, `policy_mode`, `pii_detection`, `allowed_regions`, `require_no_retention`,
  `allowed_providers` and `escalate_residency_on_detection`. The harness builds the view
  with the same constructor production uses. An unknown key fails the load. Settings mean
  what they mean in production. For example, a detected SSN escalates the case to
  `onprem_only` unless the case sets `escalate_residency_on_detection: false`, which is
  a declared per-app choice and not a bypass. The starter suite contains both kinds
  (`placement: us_only` and `placement: escalated`).
- **`grader`**: one of
  - `exact`: string equality after stripping whitespace;
  - `contains`: case-insensitive substring;
  - `regex`: full match on the stripped text, compiled at load;
  - `json_schema`: the text must parse as JSON and validate against `expected` (a schema);
  - `numeric`: the first number in the text is within `tol` of `expected`.

  Every grader returns 0 or 1.
- **`sample`** is required for `regex` and `json_schema` graders. It is a literal answer
  that passes the grader, and the loader checks that it does. The simulated backend
  returns it as the "right answer". Other graders use `expected` for this.
- **`quality_floor`** (default 1.0): an endpoint *passes* a case when its quality is at
  least the floor.
- **`tags`**: `workload`, `placement` and `protection` are the grouping dimensions for
  worst-group reporting. `workload` also selects the per-workload quality in a simulated
  endpoint profile.
- **`synthetic: true` is mandatory for any case whose text contains PII-shaped content.**
  The loader runs the regex detector over every message and **refuses the whole suite**
  if it finds an entity in a case that is not tagged synthetic. Use reserved values:
  SSNs `078-05-1120` or `219-09-9999`, and `@example.com` emails. Never paste real data.
- **`expect_empty`**: `true` if the case should have no eligible endpoint. An empty case is
  never swept. It must be rejected with at least one exclusion reason. A disagreement in
  either direction exits 2. Keep expected-empty cases in their own suite, as the shipped
  `empty-set.jsonl` does, so that the main run's exit code keeps its meaning.

**Load errors fail the whole run, before anything is called**: duplicate ids, unknown
grader kinds, invalid regex or schema, a missing `expected` or `sample`, unknown
`app_policy` keys, and untagged PII.

## 3. Writing endpoint files

```yaml
endpoints:
  - slug: local-llama
    provider: onprem            # must be a provider the deployment declares: onprem | azure | bedrock
    tier: cheap                 # cheap | mid | frontier
    price: {input: 80, output: 80}   # USD per 1M tokens; optional cached_input
    quality: 0.92               # published quality (used by best_published_quality and the router)
    region: us
    prompt_retention: none
    accepts_raw_sensitive: true
    simulated:
      quality: {claim_summary: 0.92, faq: 0.95, default: 0.90}   # 'default' is required
      latency_ms: {mean: 900, jitter: 100}
```

Each entry becomes an onboarded, enabled row in the scratch registry. The fields are the
same data-handling declarations as in
[`onboarding-models.md`](onboarding-models.md): `region`, `prompt_retention` and
`accepts_raw_sensitive`. Each entry needs **exactly one** of `simulated` and `live`.

- **`simulated`**: no network. For each `(case, endpoint)` pair, a seeded draw returns
  the right answer with probability `quality[workload]`, and `SIMULATED-WRONG`
  otherwise. Latency is drawn from `mean ± jitter`. Cost is the declared price over the
  pipeline's real token counts. **The quality numbers are your assumptions, and the pass
  rates you get back mostly reproduce them.** A simulated run tests eligibility, strategy
  wiring and cost/latency trade-offs *under those assumptions*. It does not measure any
  model. There is no failure injection: a simulated endpoint never errors.
- **`live`**: `{endpoint: "http://host:8000/v1", api_key_env: SP_ONPREM_API_KEY}`.
  **`api_key_env` is the name of an environment variable, never the key itself.** The
  loader rejects values that look like keys: anything containing `sk-`, containing
  whitespace, or a long lowercase token. When it is omitted, the provider's own
  `api_key_env` from `config/serving.yaml` is used.

A run is all one mode. A `live` entry without `--live` is a load error, and so is a
`simulated` entry in a `--live` run. Other load errors: an unknown provider, a
non-canonical tier and a duplicate slug. Also avoid slugs that collide with
`config/pricing.yaml`'s model names (`azure-gpt-mid`, `bedrock-frontier`).

## 4. Live runs cost money

```bash
export SP_ONPREM_API_KEY=...          # the variable your file names
uv run python scripts/feasible_bench.py \
  --suite eval/suites/starter.jsonl --endpoints eval/endpoints/spark-live.yaml --live
```

A live run makes **one real call per (case, eligible endpoint)**: up to cases × endpoints.
Before calling anything, the CLI prints `LIVE run: N cases × M endpoints ≤ K calls` and
the endpoint list, then asks `Proceed? [y/N]`. Declining, or end of input, exits 1 with no
calls made. `--yes` skips the prompt for non-interactive use, so only use it when you have
already checked the budget. Live completion text is **not** stored unless you pass
`--keep-text`. A live completion for a real (non-synthetic) case is exactly the data
the gateway is built to protect, so decide where `results/` lives before you keep it.

## 5. Other options

| Flag | Effect |
|---|---|
| `--seed N` | simulation seed (default 0). The same seed and inputs give a byte-identical `metrics.json` |
| `--variants FILE` | YAML mapping `name -> {quality, cost, latency, availability}`. Adds `router_cold:<name>` and `router_warm:<name>`. Every weight key is required, and a bad file exits 1 |
| `--fixed SLUG` | adds `fixed:<slug>`: always that endpoint when it is eligible, otherwise `cheapest` (recorded as `fallback_used`) |
| `--parity-tol X` | maximum pass-rate gap for the headline cost claim (default 0.02) |
| `--keep-text` | stores completion text in `matrix.jsonl` |

## 6. Reading the output

`<out>/` contains five files:

- `run.json`: the **stamp**. It holds the suite hash, endpoint-file hash,
  `routing_policy_version` (a hash of the *stock* routing and pricing config; it does not
  cover your endpoint file or variants), `registry_version`, seed, git SHA, mode, CLI
  args, the strategy list and the `router_warm` note. Quote the stamp with any number
  you cite.
- `matrix.jsonl`: one row per `(case, endpoint)`, with quality, pass, cost, latency,
  tokens, `served` and `error`. Written as it goes, so an interrupted run leaves a
  readable partial matrix and no `metrics.json`.
- `choices.jsonl`: one row per `(case, strategy)`.
- `metrics.json`: per-strategy metrics, per-group metrics, worst groups, the headline,
  policy counters and per-case eligibility (pre-gate and post-gate sets, exclusions with
  reason codes).
- `report.md`: the human summary.

In `report.md`:

- **Strategies.** `cheapest`, `best_published_quality`, `router_cold`, `router_warm`,
  plus `fixed:<slug>` and the variant forms when requested.
  - `violation_rate` must be 0.
  - `pass_rate` is the share of cases where the choice met the floor.
  - `quality_regret` is the mean gap to the best quality any eligible endpoint achieved.
  - `cost_regret` and `latency_regret` are the extra cost and latency over the cheapest
    and fastest **passing** endpoint. They are averaged **only over cases the choice
    passed**, so a cheap wrong answer never looks like a saving.
  - `floor_miss_rate` is the share of cases that had a passing endpoint but where the
    choice missed it.

  Always read cost regret next to floor-miss rate. `n/a` means there was no data, not
  zero.
- **`router_warm` is optimistic by construction.** Its signal store has replayed every row
  swept so far, *including the case being scored*. It is an upper bound on what the
  current signals could learn, not a forecast. The report says so at the top.
- **Headline.** `router_warm` cost vs `best_published_quality` cost, shown only when their
  pass rates are within `--parity-tol`. Otherwise the report says "not comparable" and
  makes no claim. On small suites, whether the headline is comparable at all changes from
  seed to seed, so run several seeds before you believe it.
- **Worst groups.** For each strategy and metric, the worst `workload`, `placement` or
  `protection` group.
- **Policy.** `violations_total`, `sweep_violations`, `empty_set_mismatches`,
  `post_gate_widened` (the gate may only tighten the eligible set) and
  `pinned_mismatches` (a pinned call served a different model than requested).
- **Empty-set cases.** Each case with no eligible endpoint, with its exclusions.
- **Deferred metrics.** Calibration, churn robustness and noninterference. These are not
  measured by this tool.

## 7. Exit codes

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | load or usage error: a bad suite, endpoint file or variants file, an unknown `--fixed` slug, a missing detector, a declined `--live` prompt, or **a gate exception on any case** |
| `2` | policy violation or empty-set mismatch: any strategy with `violation_rate > 0`, any sweep call that served an ineligible model, any post-gate eligible set that is not a subset of the pre-gate set, or any `expect_empty` disagreement |

`pinned_mismatches > 0` by itself does not change the exit code when the model served was
still eligible. Treat it as a benchmark-integrity failure anyway: the matrix row is an
error row, not a measurement of the endpoint you pinned.
