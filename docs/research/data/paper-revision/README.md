# Paper revision figures (2026-11-05)

These scripts recompute figures added to `docs/research/paper/main.tex` in response to a
pre-arXiv review (2026-11-05) and in the `arxiv-v3` revision (2026-11-06). They read only
files already in this repository and need no GPU or provider account;
`matched_baseline.py` pulls the `openpolicyagent/opa:0.70.0` image and
`detection_and_record_overhead.py` starts a `postgres:16.4-alpine` container, both locally.

| Script | Recomputes |
|---|---|
| `replay_and_detector.py` | The replay-fidelity table (admission, first-attempt and final-endpoint agreement, runs 1–4). The 987-request breakdown in "Placement holds" (status, outbound attempts, cloud attempts). The detector table (against the generator's planted identifiers, runs 3–4). The empty-response rates in "Two measurements about tokens". |
| `policy_overhead.py` | The policy-step timings in "Overhead" (compile, eligibility, conflicting-core search). |
| `matched_baseline.py` | Table "Matched baseline": a plain filter (`baseline_filter.py`) and an OPA policy (`eligibility.rego`) against the shipped eligible set, with the same router. |
| `detection_and_record_overhead.py` | Table "Detection time per prompt", the outcome-record write and the in-process end-to-end time in "Overhead". |
| `version_check_null.py` | The null experiment of Appendix A (the exploratory version check). |

Run both from the repository root:

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay   # the lab's cloud provider + pool
uv run python docs/research/data/paper-revision/replay_and_detector.py
uv run python docs/research/data/paper-revision/policy_overhead.py
```

`*.out` holds the output these scripts produced for the paper. The replay figures are
deterministic. The timings vary between runs by tens of percent, and the machine was a
developer laptop under WSL2.

Ground truth for the detector is the generator's `pii` flag in each run's `client_log.ndjson`
(a planted SSN plus e-mail address). Rows join to `outcomes.ndjson` by request id. A row
counts as detected when any entity type was recorded.

## Matched baseline for eligibility (2026-11-06)

`matched_baseline.py` answers the paper's "Not measured" item: what does the shipped
eligibility filter (`app/policy/feasible.py`) do that a plain filter, or a generic policy
engine, applying the same predicates before the same scoring rule would not?

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
uv run python docs/research/data/paper-revision/matched_baseline.py
```

Needs docker (image `openpolicyagent/opa:0.70.0`, pulled on first use). No network is used
otherwise. `matched_baseline.out` is the captured output.

| File | What it is |
|---|---|
| `baseline_filter.py` | The plain-filter baseline (35 code lines), written from the documented semantics. Imports neither `app.policy.feasible` nor `app.policy.core`. Returns a set of endpoint names, nothing else. |
| `eligibility.rego` | The same predicates as an OPA/Rego policy (70 non-comment lines). Evaluated in ONE `opa eval` for every request of every input set; per-request cost is timed separately with `opa bench`. |
| `matched_baseline.py` | Builds inputs, runs the three filters, feeds each eligible set to the shipped `Router.select`, prints agreement, disagreements, core statistics, line counts and timings. |

What is held fixed. The gate, `compile_policy`, placement resolution (`allowed_providers`,
`onprem_only` after escalation) and `sends_raw_sensitive` are shipped code and give all
three filters the same request facts. Only the filter differs. The router is the shipped
`Router.select` with `app.routing.router.feasible_set` replaced by the baseline's set
(patched inside the script; nothing under `app/` is modified), so cost cap, risk floor,
tier targeting and scoring are identical and any decision difference comes from eligibility.

Input sets: the 22-case starter suite (healthcare-sim endpoints), the 108-case extended suite
on healthcare-sim and on the four DGX-lab endpoints, the 4-case empty-set suite (cloud-only-sim),
the replayed live rows of runs 1 to 4 (`outcomes.ndjson` + `config-snapshot.json`), and a
6,912-request synthetic grid over a ten-endpoint pool whose declarations include undeclared,
padded, upper-case and `unknown` values. A second hop is evaluated for every non-grid request
(`exclude` = the shipped first pick) to cover the prior-failure stage.

Limits stated in the output header of each section: the baselines were written by someone who
had read `feasible.py`, so agreement is an upper bound on what an independent author would
achieve; timings were taken on a shared WSL2 machine (load average is printed) and vary by
tens of percent.

Retries. `Gateway.run` (`app/gateway/gateway.py`) calls `Router.select` again after every failed
attempt with `exclude=` the failed names, and `select` calls `feasible_set` each time, so
eligibility (including the prior-failure stage and the breaker's `open_circuits`) is
re-evaluated per hop. A baseline used as a one-shot pre-filter would not do that unless the
caller re-ran it with the failed names removed; the script's second hop checks that both
baselines give the same sets when told the exclusion, not that a pre-filter deployment
would remember to ask.

## Version-check null experiment (`version_check_null.py`)

Rebuilds the synthetic null table in `docs/research/feasible-route-bench.md` section 22.7,
whose original script was never committed. Output: `version_check_null.out`.

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
uv run python docs/research/data/paper-revision/version_check_null.py --min-group 5   # shipped default
uv run python docs/research/data/paper-revision/version_check_null.py --min-group 2   # closest to the published figure counts
```

`version_check_null.out` is the two runs concatenated. Wall time 834 s (both runs, one laptop under
WSL2; about 4 min and 9.5 min).

Modelling choices (the original ones are not recoverable, so these are mine):

- **New rule** is the shipped code: `decide` per figure, `verdict_of` for the verdict, the shipped
  `draw_indices` for the resamples and `interval_of` for the percentile range. The per-resample
  means are computed with numpy for speed. The first 3 draws of every row are also run through the
  shipped `run_check`; the script asserts the same figure count, verdict and regressed count.
- **First rule** is `first_rule_regressed`: a figure is regressed when its point estimate is beyond
  its tolerance (range ignored). The first-rule column is the share of draws with at least one such figure.
- **Cases.** n=22 is the real starter suite (`eval/suites/starter.jsonl`) with its real group keys
  (`case_keys`/`partition`). n=200: case j takes the tags and `app_policy` of starter case j mod 22, so
  group proportions are the starter suite's.
- **Shared distribution.** A: each case's whole 8-metric vector (None pattern included) drawn uniformly
  from the 22 `router_online` `CaseScore`s of a simulated bench of the starter suite on
  `eval/endpoints/healthcare-sim.yaml` (captured from the runner in-process; pass rate 0.909). B (sensitivity):
  independent parametric draws (pass ~ Bern(0.8), lognormal cost, regrets only when passed, Poisson
  wasted attempts, Bern(0.05) exhausted). Costs are expressed in USD per 1,000 requests so that the
  0.25 cost tolerance is on a meaningful scale; the original unit is unknown.
- **Redraw share f.** Each draw makes a base set of n cases; the baseline arm is the base, the candidate
  arm is the base with a random `round(f*n)` cases independently redrawn from the same distribution. So the
  arms differ only in which cases came out differently, with no true difference.
- 300 draws and 400 resamples per row, interval 0.90 (shipped default), fixed seeds. The brackets are 95%
  Wilson intervals for 300 draws.
- **Figures judged.** With the shipped `min_group_size=5` only 7 groups plus overall are judged (8 figures per
  metric), not the published 14. `--min-group 2` gives 13 per metric, the nearest reachable. Published 14 is
  not reproducible from the starter suite's groups.
- **Bonferroni** (NOT shipped): interval 1 - 0.1/m with m the mean figures judged, resamples raised to what
  that level needs. In groups of 2-4 cases the percentile bootstrap is itself unreliable, so the corrected
  rule still reports regressed far above 5% in some rows at `--min-group 2`.

## Detection and record-write overhead (2026-11-06)

`detection_and_record_overhead.py` measures what `policy_overhead.py` leaves out: PII detection, the
`routing_outcomes` write, and an in-process end-to-end request. Output of the run quoted in the paper:
`detection_and_record_overhead.out`.

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
uv run python -u docs/research/data/paper-revision/detection_and_record_overhead.py   # ~35 min, mostly Presidio
RF_PARTS=2,3 uv run python -u docs/research/data/paper-revision/detection_and_record_overhead.py  # skip the Presidio sweep
```

1. **Detection.** The 1,800 prompt texts of live runs 3 and 4 are rebuilt by running each run's
   `traffic_generator.py` text-building code (seeds 4 and 5, recovered by matching every prompt length and PII flag
   against `client_log.ndjson`; asserted per text). Nothing is sent. `analyze()` is timed per text (regex: median of 5
   calls; Presidio: 1 call, after `warmup()`), then summarised by generator kind and by length bucket. The
   Presidio constructor, cold first call and `warmup()` are reported separately, as are `anonymize()` timings and the
   spaCy model version.
2. **Outcome write.** One real outcome row (captured from a request through the app) is written with a fresh
   `request_id`: session open, insert, commit. SQLite file DB and Postgres (`postgres:16.4-alpine` in a throwaway
   container on a free loopback port, removed afterwards). 2,000 writes after 100 warm-up.
3. **In-process request.** `TestClient(create_app())` posts to `/v1/chat/completions` with `litellm_backend.acompletion`
   replaced by a stub that returns a canned completion with no sleep and no network. The app DB is in-memory SQLite,
   the seeded test models (`SP_SEED_TEST_MODELS=1`) are the on-prem pool, Langfuse export is off (keys unset), semantic
   cache and compression are off. Includes the TestClient/ASGI harness (a `GET /health` baseline is printed).
   Real model latency, network and Langfuse export are excluded. A second pass sets `outcomes.enabled=false`.

Machine: Intel Core Ultra 7 165U laptop, 14 logical CPUs, WSL2, Python 3.12.3, presidio-analyzer 2.2.363,
spaCy 3.8.13 with `en_core_web_lg` 3.8.0. Timings vary by tens of percent between runs and part 3 is noisy
(the outcomes-on/off difference is inside the noise and is not a measured cost of the write).

## Matched baseline, second grid: endpoint allowlist, disabled model, open circuit (2026-11-06)

`matched_baseline.py` gained a second synthetic input set, `synthetic-state-grid`
(1920 requests: 3 tenants x 5 open-circuit sets x onprem_only x 2 provider sets x 4 pins x
2 tier scopes x 2 region scopes x require_no_retention). The first grid never reached three
stages of `feasible_set`: the endpoint-side `allowed_app_ids` allowlist, a disabled model, and an
open circuit. All endpoints in this grid are onboarded rows of a scratch in-memory registry
(`n0..n7` enabled, with and without allowlists; `d0..d2` disabled), the only place the allowlist
and the enabled flag exist. Open circuits are passed as `feasible_set(open_circuits=...)`.

What changed in the baselines. `baseline_filter.py` already took `app_denied` and `disabled`
per endpoint (the first grid and the runs fed them, never nonzero), so only the open circuit
was new: a request now carries `open_circuits` and the filter drops those names
(`eligibility.rego` gained `circuit_ok`, one line, and its `allowed` rule one conjunct).
`request_facts` emits `open_circuits`; the disabled rows are handed to both baselines with
`disabled: true`. Nothing else in the baselines changed.

Results are in `matched_baseline.out` under "Endpoint-state grid". The disabled stage cannot fire
through `feasible_set`: `registry.effective_pool()` already omits disabled onboarded rows, so a
disabled model leaves the set by absence (and shows up as `state: disabled` in the conflicting core).
`is_disabled` is also only true for `provider == "onprem"`. The script therefore also hands
`candidate_rejection` each disabled row directly (predicate level) and reports what it returns.
The open circuit empties the eligible set but never the router's: `Router.select` bypasses stage 7
within the policy-eligible set, and the conflicting core ignores circuits (state, not policy); the
script reports the router outcome on every request a circuit emptied.

## End-to-end time split (`e2e_breakdown.py`, 2026-11-06)

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
uv run python docs/research/data/paper-revision/e2e_breakdown.py > e2e_breakdown.out
python3 docs/research/data/paper-revision/e2e_breakdown_summarize.py > e2e_breakdown_summary.out
```

Splits the `POST /v1/chat/completions` in-process time of `detection_and_record_overhead.py` part 3
(zero-latency stub backend) into harness, database and application compute, for sqlite in-memory,
Postgres 16.4 (default fsync) and a Postgres with `fsync=off` as a diagnostic, regex and Presidio, a
62-char and a 4013-char prompt. Pass A wraps only the ASGI app (wall vs ASGI-call time; "own" = ASGI
call minus the stubbed backend). Pass B wraps functions at run time (nothing under `app/` is edited) and
records exclusive times per stage, with `Database.session()` timed as a whole and tagged by the store
function called inside. Needs docker for Postgres (throwaway containers, removed at the end).
`e2e_breakdown_summarize.py` only parses the `.out`.

Caveats. The machine was never idle: load average was 4.1-5.6 during these runs (other processes on
the host), so absolute numbers moved between two complete runs by tens of percent; the second run is
the one saved. The Postgres is a docker loopback port under WSL2, whose measured `SELECT 1` round trip
is about 2 ms, so its database times are an upper bound dominated by the network path. sqlite in-memory
time includes the busy-poll in `_patch_aiosqlite_wakeup` (about 1,200-1,500 event-loop turns per request
against about 65 on Postgres; the script prints both).

## Replay overhead (`replay_overhead.py`, 2026-11-06)

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
uv run python docs/research/data/paper-revision/replay_overhead.py > replay_overhead.out
```

Times `scripts/policy_simulate.py` over run 4 (900 rows, 5 scenarios) as a fresh process each time
(5 runs, median), and in process the load, scenario build and `simulate_row` per row (900 rows x 5
passes). Plain replay passes an empty proposal because the CLI requires one. Load average at start
was 4.8.

Machine for all three: Intel Core Ultra 7 165U, 14 threads, WSL2, Python 3.12.3.
