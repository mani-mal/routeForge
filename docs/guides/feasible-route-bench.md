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
  at import. The CLI sets it itself when it is unset, so an air-gapped run does not hang;
  passing it explicitly is harmless.
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
| `eval/suites/outage.jsonl` | 30 public FAQ cases for the outage scenario (B45) |
| `eval/endpoints/outage-sim.yaml` | two same-tier endpoints; the cheaper one is down for cases 10–19, the other flakes at 5% |

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
  never swept. Instead the harness sends **one unpinned probe** through the real pipeline,
  which must refuse it (raise) with no model call; the case must also have at least one
  exclusion reason. A probe that is served counts as a policy violation. A disagreement in
  either direction exits 2. Keep expected-empty cases in their own suite, as the shipped
  `empty-set.jsonl` does, so that the main run's exit code keeps its meaning.

**Load errors fail the whole run, before anything is called**: duplicate ids, unknown
grader kinds, invalid regex or schema, a missing `expected` or `sample`, unknown
`app_policy` keys, a list-valued `app_policy` field (`allowed_providers`,
`allowed_regions`, ...) given as anything but a list, and untagged PII.

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
      # optional failure injection (all three default to "never fails"):
      outages: [{from: 10, to: 19}]   # inclusive case indices (0-based, suite order): every call fails
      error_rate: 0.05                # independent per-(case, endpoint) failure probability
      failure_latency_ms: 1000        # latency charged for a failed attempt; default latency_ms.mean
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
  model.
  - **Failure injection (simulated only).** `outages` lists inclusive ranges of **case
    indices**, counted from 0 in suite file order, not times. Every call in a range fails
    the way a refused connection does (no status code). `error_rate` makes each
    `(case, endpoint)` call fail independently with that probability, as an HTTP 503,
    drawn from the seed. An outage is checked first, so it always fails. A failed attempt
    costs 0 and takes `failure_latency_ms` (default: `latency_ms.mean`). Both kinds count
    as endpoint unhealth, as production's `counts_as_endpoint_unhealth` decides. Load
    errors: `from > to`, a negative index, `error_rate` outside `[0, 1]`, or a
    non-positive `failure_latency_ms`. Without these fields a simulated endpoint never
    errors.
- **`live`**: `{endpoint: "http://host:8000/v1", api_key_env: SP_ONPREM_API_KEY}`.
  **`api_key_env` is the name of an environment variable, never the key itself.** The
  loader rejects values that look like keys: anything containing `sk-`, containing
  whitespace, or a long lowercase token. When it is omitted, the provider's own
  `api_key_env` from `config/serving.yaml` is used.

A run is all one mode. A `live` entry without `--live` is a load error, and so is a
`simulated` entry in a `--live` run. Other load errors: an unknown provider, a
non-canonical tier and a duplicate slug. A slug that is also one of
`config/pricing.yaml`'s model names (`azure-gpt-mid`, `bedrock-frontier`) is priced from
**your file**, not from `pricing.yaml`: the endpoint file is the whole universe of a run.

## 4. Live runs cost money

```bash
export SP_ONPREM_API_KEY=...          # the variable your file names
uv run python scripts/feasible_bench.py \
  --suite eval/suites/starter.jsonl --endpoints eval/endpoints/spark-live.yaml --live
```

A live run makes **one real call per (case, eligible endpoint)**: up to cases × endpoints.
The sweep disables confidence escalation so that bound holds (§5).
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
| `--variants FILE` | YAML mapping `name -> {quality, cost, latency, availability}`. Adds `router_cold:<name>`, `router_online:<name>` and `router_warm_prior:<name>`. Every weight key is required, and a bad file exits 1 |
| `--seconds-per-case S` | spacing of the synthetic case clock (default 30, must be > 0): case `k` happens at `k × S` seconds. It drives availability decay (900 s half-life) and latency staleness in the online and warm-prior arms, so their numbers depend on it (§6). Recorded in `run.json` |
| `--fixed SLUG` | adds `fixed:<slug>`: always that endpoint when it is eligible, otherwise `cheapest` (recorded as `fallback_used`) |
| `--parity-tol X` | maximum pass-rate gap for the headline cost claim (default 0.02) |
| `--keep-text` | stores completion text in `matrix.jsonl` |

**The sweep disables confidence escalation.** Each `(case, endpoint)` is a single-shot
measurement: one backend call. With a pinned endpoint, the gateway's confidence
escalation would clamp back to the same tier and call the **same** endpoint again,
doubling the spend and grading the second answer. The harness therefore runs with
`routing.max_escalations = 0` (the strategies do not read it), and the runner checks it
independently: a successful sweep call that made any number of backend calls other than
one becomes an error row (`GenerateCountMismatch`), counted as
`sweep_generate_mismatches`.

**The sweep also disables gateway failover** (`routing.max_failover_attempts = 0`), for
the same reason: one row is one attempt. Failover is modelled in scoring instead (§6).
The number of attempts a strategy may make is `1 +` **your deployment's**
`max_failover_attempts` (3 with the shipped `routing.yaml`), recorded as `attempt_cap` in
`run.json`.

## 6. Reading the output

`<out>/` contains five files:

- `run.json`: the **stamp**. It holds the suite hash, endpoint-file hash,
  `base_routing_policy_version` (the deployment's routing and pricing hash **before** the
  harness overrides it: emptied `model_pool`, file prices, `max_escalations` 0, variant
  weights; it does not cover your endpoint file or variants, and `notes` says so),
  `registry_version`, seed, git SHA, mode, CLI args, `seconds_per_case`, `attempt_cap`,
  the strategy list and `notes` explaining each arm, the case clock and the units. Quote
  the stamp, including `seconds_per_case`, with any number you cite.
- `matrix.jsonl`: one row per `(case, endpoint)`, with quality, pass, cost, latency,
  tokens, `served` and `error`. A failed row also has `counts_unhealth` (whether
  production would count it against the endpoint), `failure_latency_ms` and
  `error_class`. Written as it goes, so an interrupted run leaves a readable partial
  matrix and no `metrics.json`.
- `choices.jsonl`: one row per `(case, strategy)`: the `attempts` in order, the
  `endpoint` that served (null if none did), and the chain's `quality`, `passed`,
  `cost_usd`, `latency_ms`, `wasted_attempts` and `exhausted`.
- `metrics.json`: per-strategy metrics, per-group metrics, worst groups, the headline,
  policy counters and per-case eligibility (pre-gate and post-gate sets, exclusions with
  reason codes).
- `report.md`: the human summary.

In `report.md`:

- **How to read this.** A short block at the top that restates the rules below for this
  run, including its `seconds_per_case`.
- **Strategies.** `cheapest`, `best_published_quality`, `router_cold`, `router_online`,
  `router_warm_prior`, plus `fixed:<slug>` and the variant forms when requested. Each
  strategy is scored on an **attempt chain**, the way production fails over. It tries
  its first choice; if that call fails, it tries the next (the router re-selects with the
  failed endpoints excluded; the baselines take their own next-ranked endpoint), for up
  to `attempt_cap` attempts, and stops at the first call that succeeds. The chain's cost
  is the sum over its attempts (a failed attempt costs 0). Its latency is each failed
  attempt's failure latency plus the served call's latency. A chain where every attempt
  failed is **exhausted** (production's 502) and scores quality 0. A chain with no
  attempts at all is "no choice" (production's 422 for a policy-empty request).
  - `violation_rate` must be 0.
  - `pass_rate` is the share of cases where the choice met the floor.
  - `quality_regret` is the mean gap to the best quality any eligible endpoint achieved.
  - `cost_regret` and `latency_regret` are the extra cost and latency over the cheapest
    and fastest **passing** endpoint. They are averaged **only over cases the choice
    passed**, so a cheap wrong answer never looks like a saving.
  - `floor_miss_rate` is the share of cases that had a passing endpoint but where the
    choice missed it.

  - `wasted_attempts` is the mean number of failed attempts per case, and
    `exhausted_rate` is the share of cases where every attempt failed.

  Always read cost regret next to floor-miss rate. `n/a` means there was no data, not
  zero. A failed attempt whose latency is unknown (possible only in a live run) counts
  as 0 ms.
- **The three router arms.**
  - `router_cold` has an empty signal store: the no-measurement baseline.
  - `router_online` is **the reported arm**. Before scoring a case, its store holds only
    the attempts this arm itself made on *earlier* cases, including its failovers. That
    is what production would have learned, and nothing more.
  - `router_warm_prior` has replayed every matrix row of every earlier case, for every
    endpoint. It is a full-information reference, not a forecast, and not a guaranteed
    upper bound: it also sees the failures of endpoints the online arm is not using.

  Both warm arms count a failure only when production would (`counts_unhealth`), and
  both read their store at the case's clock time. Their numbers depend on the suite's
  case order and on `--seconds-per-case`. The old `router_warm` arm is gone: it replayed
  the scored case's own rows, which leaks the outcome it is scored on.
- **Headline.** `router_online` cost vs `best_published_quality` cost, shown only when their
  pass rates are within `--parity-tol`. Otherwise the report says "not comparable" and
  makes no claim. On small suites, whether the headline is comparable at all changes from
  seed to seed, so run several seeds before you believe it.
- **Worst groups.** For each strategy and metric, the worst `workload`, `placement` or
  `protection` group.
- **Policy.** `violations_total` counts **attempts** (every attempt outside the case's
  eligible set, plus sweep calls), while each strategy's `violation_rate` is per **case**.
  A chain that repeats an endpoint, or records attempts after the one that served, is
  also a violation. Also listed: `sweep_violations`, `empty_set_mismatches`,
  `post_gate_widened` (the gate may only tighten the eligible set),
  `pinned_mismatches` (a pinned call served a different model than requested) and
  `sweep_generate_mismatches` (a sweep call that made other than one backend call).
- **Empty-set cases.** Each case with no eligible endpoint, with its exclusions.
  `metrics.json` also records `empty_probe_error`, the exception type the real pipeline
  raised for the probe (for example `NoEligibleCandidate`).
- **Deferred metrics.** Calibration, churn robustness and noninterference. These are not
  measured by this tool.

### 6.1 Reading B45 from `report.md`

Backlog B45 asks whether `weights.availability` (0.2 in `config/routing.yaml`) is large
enough for a dead endpoint to lose. To read it:

1. Run a suite in which some endpoint fails, with the variants file and each spacing:

   ```bash
   export SP_PII_DETECTION=regex    # only if Presidio is not installed (see §1)
   for spc in 1 30 300; do
     uv run python scripts/feasible_bench.py --suite eval/suites/outage.jsonl \
       --endpoints eval/endpoints/outage-sim.yaml --variants eval/variants/b45.yaml \
       --seed 0 --seconds-per-case $spc --out results/b45-s0-$spc
   done
   ```

   Repeat for several seeds. On the starter suite nothing fails, so every availability
   variant is identical by construction.
2. In each report's **Strategies** table, compare the `router_online:<variant>` rows, and
   only those: `wasted_attempts` (lower means fewer requests sent to the dead endpoint),
   `mean_chosen_cost` and `cost_regret` (what the higher weight costs), and
   `latency_regret`. The report gives chains and totals, not scores. To see the router's
   per-case score terms, run `scripts/b45_score_probe.py` (its docstring gives the
   command).
3. If `router_online:default` equals `router_online:availability_0` on every column, the
   shipped weight changed nothing in that run. For per-case detail, `choices.jsonl`'s
   `attempts[0]` per case shows where each variant sent its first attempt, and so when it
   switched away from the failing endpoint and when it came back.
4. Quote `seconds_per_case` and the seed with every number. The same scenario gives
   different answers at 1 s, 30 s and 300 s per case, because availability recovers on
   clock time.

The first answer, simulated, is in
[`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §9. Changing
the production weight is a decision for the owner, not something this tool does.

## 7. Exit codes

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | load or usage error: a bad suite, endpoint file or variants file, an unknown `--fixed` slug, a missing detector, a declined `--live` prompt, a `--seconds-per-case` too small for this run's endpoint count or attempt cap, or **a gate exception on any case** |
| `2` | policy violation or integrity failure: any strategy with `violation_rate > 0`, any sweep call (or empty-set probe) that served an ineligible model, any post-gate eligible set that is not a subset of the pre-gate set, any `expect_empty` disagreement, any `pinned_mismatches` (a pinned call served another endpoint; impossible by construction, so any occurrence is a harness integrity bug), or a signal arm's own leak guard firing (`LeakageError` -- an arm was asked to score a case out of order; also a harness integrity bug) |

`sweep_generate_mismatches` does not by itself change the exit code: those rows are
already error rows, scored as failed calls rather than credited to the endpoint.

## 8. Outcome report (production records)

`scripts/outcome_report.py` summarises RouteForge's **production** routing outcome
records. It reads a file exported from a running deployment
(`GET /admin/outcomes/export`; see
[`docs/reference/routing-outcomes.md`](../reference/routing-outcomes.md) §7). It never
connects to a database or a deployment.

```bash
curl -s -b "rf_admin_session=<session>" \
  "http://<host>:8080/admin/outcomes/export?since=2026-09-16T00:00:00Z" > outcomes.ndjson

uv run python scripts/outcome_report.py --export outcomes.ndjson --out results/outcomes
```

It writes `summary.json` and `report.md` into `--out`. Re-running it on the same export
gives a byte-identical `summary.json`: keys are sorted, floats are rounded to 6 decimal
places, and nothing time-dependent is included.

**How it groups.** First by `endpoint_registry_version`. Records from different registry
versions are **never pooled**, because a registry change can change which endpoints exist.
A record with no version is grouped as `(unknown)`. Within each version:

- per `(workload, chosen)`: `n`, served-ok rate, verify pass rate (over records where
  verification was performed), mean `quality` feedback and its count, `route_correct`
  rate and its count, mean cost, latency p50/p95 (nearest rank), and failover rate
  (more than one attempt). Mean cost and the latency percentiles use `ok` records only,
  because failed records carry zero cost and a different latency definition;
- per workload: `n`, `no_eligible` rate, `backend_failed` rate, and label coverage (the
  share of records with any feedback).

A request with no workload tag is shown as `(untagged)`. A record with no served model
(a failure, or a semantic-cache hit) is shown under chosen `(none)`.

**Loading.** Blank lines are skipped. A line that is not JSON, or has no `outcome`, is
skipped with a warning on stderr. A line whose `schema` is missing or is not
`routeforge.outcome/v1` (a `v1.x` minor is accepted) stops the run with exit code `1`:
the loader does not guess at a format it does not know.

**How it differs from the bench.**

| | FeasibleRouteBench (§1–§7) | Outcome report |
|---|---|---|
| Input | a suite of cases and an endpoint file | a production export |
| Endpoints observed per request | **every** eligible endpoint | only the one that served it |
| Quality | graded against each case's expected answer | client and admin feedback, where given, and step-9 verification |
| Oracle regret | computed | **not computable, never printed** |
| Endpoint comparisons | same cases for every endpoint | different request mixes, chosen by the router |

`report.md` starts with this caveat, verbatim:

> Production observes only the served endpoint for each request, so oracle regret is not
> computable from these records and is not reported. Endpoint comparisons are confounded
> by the router's own choices; treat them as descriptive, not causal.

Use the report to see what production traffic looks like per workload: where requests
fail, which endpoints serve which workloads, and how much feedback exists. Use the bench
to compare endpoints on the same cases. The research notes on what the records can and
cannot support are in [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §13.

| Exit code | Meaning |
|---|---|
| `0` | ok |
| `1` | load error: the file cannot be read, or a line has an unrecognised or missing schema |
