# P3c-2 — Alternative scenarios, compared side by side

**Date:** 2026-10-05 · **Status:** approved design, not built
**Priority:** 3c, second sub-project. The order is shadow prices (P3c-1, shipped) → scenarios →
uncertainty (P3c-3) → capacity (P3c-4). Handoff §28 ("compare only administrator-approved
alternatives") and §30 ("other changes the simulator should test").
**Follows:** P3a simulator (`app/eval/{scenario,proposal,simulate}.py`,
`scripts/policy_simulate.py`), P3b conflicting core, P3c-1 shadow prices.
**Paper:** this slice feeds `docs/research/arxiv.md` §P3c-2 (the R1/Priority-3 paper and R2).

## 1. Purpose

An administrator has several candidate policy changes — e.g. "all healthcare on-prem", "only
raw PII on-prem", "all healthcare on-prem plus a second GPU endpoint" — and wants their
predicted effects next to each other, over the same recorded traffic and the same baseline.
P3c-2 replays each scenario against one shared `current` arm and reports one row per scenario.

RouteForge **never picks, ranks or recommends** a scenario (handoff §28). Scenarios are listed
in file order and nothing is applied.

## 2. Non-goals

- No serving-code change. Everything is offline in `app/eval` + the CLI. The one read-only
  admin endpoint (`GET /admin/config-snapshot`) is unchanged.
- No uncertainty ranges (P3c-3), no capacity/overload or rate limits (P3c-4), no endpoint-health
  degradation or loss-of-fallback replay (replay-fidelity slice).
- No recording of cached tokens on outcome rows (a serving change); `cached_share` is an
  assumption, labelled as one.
- No vLLM-vs-Dynamo modelling beyond an assumed per-endpoint latency: RouteForge is a pure
  router and cannot see the serving engine.

## 3. Input: `routeforge.policy-scenarios/v1`

```json
{
  "schema": "routeforge.policy-scenarios/v1",
  "title": "Healthcare on-prem options",
  "scenarios": [
    {"name": "all-onprem",      "proposal": {"apps": {"claims": {"residency": "onprem_only"}}}},
    {"name": "raw-pii-onprem",  "proposal": {"apps": {"claims": {"escalate_residency_on_detection": true}}}},
    {"name": "onprem+2nd-gpu",  "proposal": {"apps": {"claims": {"residency": "onprem_only"}},
                                             "endpoints": {"add": [ ... ]}}}
  ]
}
```

- Top-level keys: exactly `schema`, `title`, `scenarios`. Unknown key → load error naming it.
- `scenarios`: a list of 1–20 objects with exactly `name`, `proposal`.
- `name`: non-empty, ≤ 64 chars, `[A-Za-z0-9._+-]+`, unique within the file.
- `proposal`: a v1 proposal **body** — the same keys as `routeforge.policy-proposal/v1`
  (`title` optional, `schema` optional and if present must be `routeforge.policy-proposal/v1`).
  It is validated by the existing `apply_proposal` against the snapshot; every error is
  re-raised with the prefix `scenarios[<i>](<name>).proposal.`.
- Loading is all-or-nothing: one invalid scenario fails the whole file (exit 2).
- An empty proposal body is a valid scenario and must predict zero change (§9 invariant S1).

New module `app/eval/scenarios.py::load_scenarios(path) -> (doc, sha256)`.

## 4. New proposal change types (added to v1)

These are additive keys on `routeforge.policy-proposal/v1`, so P3a single-proposal runs and
P3c-1 shadow prices (via a `--proposal` baseline) get them too. Unknown-key rejection still
applies everywhere; every new value is validated with a dotted-path `ProposalError`.

### 4.1 `workload` — assumptions about the traffic itself

```json
"workload": {
  "token_scale": 0.6,
  "cached_share": 0.0,
  "apps": {"<application_id>": {"token_scale": 0.5, "cached_share": 0.4}}
}
```

- `token_scale`: finite, `0 < x ≤ 10`. Per-app value overrides the global; default `1.0`.
  Applied to a row before eligibility, routing and pricing:
  - `prompt_tokens' = max(1, round(prompt_tokens × scale))`; `completion_tokens` unchanged.
  - `complexity'`: the live pipeline computes `min(1, tokens_after / 2000)`
    (`app/pipeline.py`). If the recorded `complexity < 1`, `complexity' = min(1, complexity × scale)`
    (exact: it is linear in `tokens_after`). If it is saturated (`== 1.0`), `tokens_after` is
    unknown, so `complexity' = min(1, prompt_tokens' / 2000)` and the row is counted in
    `assumptions.saturated_complexity_estimated`. The report shows that count.
  - Models: a stricter or looser redaction policy, Headroom on/off (handoff §30 "changes to
    post-redaction token count"); the R2 lever.
- `cached_share`: finite, `0 ≤ x ≤ 1`, global and per app; default `0`. Fraction of the
  (scaled) prompt tokens priced at the endpoint's `cached_input` price via the existing
  `price_call(..., cached_prompt_tokens=...)`; an endpoint with no declared cached price bills
  those tokens at input price, as live billing does. Outcome rows do not record cached tokens,
  so **both arms default to 0** and any non-zero value is an assumption; the report says so.
- The `current` arm is never given these assumptions: they are part of a scenario, and the
  diff therefore isolates their effect.

### 4.2 `endpoints.update.<slug>.latency_ms` and `endpoints.add[].latency_ms`

- Finite, `> 0`, or `null` (no override). Applies to onboarded rows, added rows, and also to a
  priced YAML pool model via `endpoints.update` (the only `update` field allowed on a YAML
  pool model).
- Effect: the scenario's `FrozenSignals` returns this value as the endpoint's latency, so it
  feeds **both** the router's latency scoring term and the replayed latency. It wins over
  observed, signal and tier-default latency; `latency_source = "proposal"`.
- Models: switching an endpoint between vLLM and NVIDIA Dynamo, or slower/faster hardware,
  as an operator-asserted latency (handoff §30).

### 4.3 Where the assumptions live in the applied doc

`apply_proposal` already returns a new snapshot document. It now also writes a top-level
`assumptions` block: `{"token_scale": {"*": x, "<app>": y}, "cached_share": {...},
"latency_ms": {"<slug>": ms}}`, absent when the proposal sets none. `build_scenario` reads
it into the `Scenario` (`assumptions` field) and into `FrozenSignals` (latency overrides). A
snapshot document that already contains `assumptions` is a load error — the block is only
ever produced by `apply_proposal`, so a hand-edited snapshot can never smuggle one in.

## 5. Context-window replay (both arms)

Today a proposed `context_window` change has no visible effect (reference
`policy-simulator.md` §6): enforcement is the backend's overflow error at call time, which the
gateway handles by excluding the overflowing model and re-selecting
(`app/gateway/gateway.py`, bounded by `routing.max_failover_attempts`).

Replay now mirrors that loop deterministically, for both arms:

1. `select(..., exclude=excluded)`.
2. If the pick has a declared `context_window` and
   `prompt_tokens' + output_tokens > context_window`, add it to `excluded`, count one overflow
   hop, and go to 1 — up to `max_failover_attempts` extra selects, exactly the live bound.
3. If every reachable candidate overflowed (or the bound is hit) → rejected with code
   `context_window` (live: 400 `ContextWindowExceeded`). If `select` raises
   `NoEligibleCandidate` after ≥ 1 overflow exclusion → also `context_window` (the live
   gateway reports exhausted-by-overflow the same way).
4. Undeclared `context_window` (`null`) never overflows — replay cannot invent a limit.

`ArmResult` gains `overflow_hops: int`. The served endpoint after overflow hops is the one
costed. This is the first non-first-decision behaviour in replay; it is restricted to
overflow, which is a deterministic function of declared data, and **not** general failover
(that is the replay-fidelity slice).

**Consequence:** a P3a/P3c-1 run over rows that would overflow under the *current* snapshot
now differs from before. Baseline agreement should rise, not fall (recorded `context_overflow`
rows and overflow failovers are now reproduced). This is recorded in research §18 with the
before/after agreement on the Spark export.

## 6. CLI

`scripts/policy_simulate.py` gains:

| flag | meaning |
|---|---|
| `--scenarios FILE` | scenario comparison mode; mutually exclusive with `--proposal` and with `--shadow-prices` (shadow prices of N scenarios is out of scope); either combination is a usage error, exit 2 |
| `--since TS` | ISO-8601 UTC; rows with `ts < TS` are excluded under a new reason `before_since`. Applies to every mode. |
| `--min-agreement X` | `0 ≤ X ≤ 1`. If the baseline's overall agreement is `None` or `< X`, print the agreement and exit **4** before writing any output files. |

Exit codes: 0 written; 2 bad input; 3 `--fail-on-new-rejections` and any scenario (or the
single proposal) newly rejects ≥ 1 row; 4 agreement below `--min-agreement`.

`EXCLUSION_REASONS` becomes `("before_since", "cache_hit", "unknown_status", "unknown_app",
"missing_features")` — `before_since` is checked first so old rows never reach the others.

## 7. Engine

`app/eval/scenarios.py`:

```python
async def compare(records, snapshot, scenarios_doc, *, base) -> Comparison
```

1. Build `current` once from the snapshot.
2. For each scenario in file order: `apply_proposal` → `build_scenario` → `run_rows(records,
   current, proposed)` → `summarize(...)` → `close_scenario(proposed)`. Only one proposed
   scenario is open at a time. The `current` arm is recomputed per scenario by `run_rows`
   today; to keep the single-proposal path untouched, `run_rows` gains an optional
   precomputed `current_results` map (request_id → ArmResult) that `compare` fills once.
3. Baseline agreement is computed once from the `current` arm (it is identical across
   scenarios) and lifted out of the per-scenario blocks into the top level.
4. Apps simulated = apps present in the snapshot **and** in every scenario's applied doc (a
   scenario cannot remove an app today, so this is all snapshot apps; asserted).

## 8. Output

`--out` gets `comparison.json` and `report.md`.

`comparison.json` (schema `routeforge.scenario-comparison/v1`, deterministic, `sort_keys`,
`_round6`):

```json
{
  "schema": "routeforge.scenario-comparison/v1",
  "comparison_id": "<sha256(inputs)[:16]>",
  "inputs": {"export_sha256", "snapshot_sha256", "scenarios_sha256", "title",
             "routing_policy_version", "registry_version", "snapshot_generated_at",
             "serving_policy", "since", "window"},
  "rows": {"simulated": N, "excluded": {...}},
  "baseline": { ...as P3a _baseline... },
  "scenarios": [ {"name": "...", "edits": {...}, "assumptions": {...},
                  "summary": { ...P3a simulation/v1 body minus inputs/baseline/rows... }} ],
  "table": [ {"name", "cost_per_day_delta_usd", "cost_delta_usd", "route_changed",
              "newly_rejected", "newly_served", "latency_p50_delta_ms",
              "latency_p95_delta_ms", "quality_mean_delta", "onprem_share_current",
              "onprem_share_proposed", "overflow_hops", "affected_apps"} ]
}
```

`report.md`: the P3a caveat (amended: "first routing decision, plus deterministic
context-window overflow re-selection"), an agreement banner (overall, per app, ⚠ below 0.90,
the `--since` cut), the one-table comparison in file order with the sentence "Listed in file
order. RouteForge does not rank or recommend scenarios.", an "Assumptions" line per scenario
that sets `workload` or `latency_ms`, then a short section per scenario (moves, new rejections
by core, affected apps) reusing `render_report` pieces.

## 9. Invariants (each pinned by a named test)

- **S1 empty scenario is a no-op:** an empty proposal body yields zero in every delta column.
- **S2 equivalence:** a scenarios file with one scenario `P` yields exactly the per-scenario
  summary that `--proposal P` yields (same rows, same numbers).
- **S3 order independence:** permuting scenarios permutes the table rows and changes nothing
  else.
- **S4 neutral assumptions:** `token_scale: 1`, `cached_share: 0`, and `latency_ms` equal to
  the value replay would otherwise use change no route and no cost.
- **S5 monotone token scale (pricing):** with routing held fixed (single eligible endpoint),
  cost is non-decreasing in `token_scale`.
- **S6 cached share:** with `cached_input < input`, cost is non-increasing in `cached_share`;
  with no declared cached price, cost is unchanged.
- **S7 overflow:** a pick whose declared window is exceeded is never the served endpoint; an
  undeclared window never overflows; all-overflow ⇒ `context_window` rejection.
- **S8 no recommendation:** the report and JSON contain no ranking field and list scenarios
  in file order.
- **S9 security monotonicity carries over:** the existing proposal invariants (tightening
  never enlarges the eligible set; protection never moves placement) still hold per scenario
  — the Hypothesis harness from P1a/P3a is run with scenario-generated proposals.
- **S10 since:** rows before `--since` are excluded and counted; none reach replay.
- **S11 agreement gate:** `--min-agreement` above the computed agreement exits 4 and writes no
  files.

Plus: `tests/eval/test_isolation.py` still passes (no serving import from `app/eval`).

## 10. Testing

TDD per task, in `tests/eval/`: `test_scenarios_load.py` (schema, names, nested error paths),
`test_proposal_workload.py` (validation + application of `workload`/`latency_ms`),
`test_replay_overflow.py`, `test_scenarios_compare.py` (S1–S8), `test_policy_simulate_cli.py`
additions (`--scenarios`, `--since`, `--min-agreement`, exit codes). Fixtures reuse the P3a
snapshot/export fixtures.

## 11. Docs

- `docs/reference/policy-simulator.md`: new §9 "Scenario comparison", §2.1 rows for
  `workload` and `latency_ms`, §6 updated (context-window now replayed; cached share is an
  assumption).
- `docs/research/feasible-route-bench.md` §18 (model, invariants table with test names,
  Spark results).
- `docs/research/arxiv.md` §P3c-2 (claims, findings, experiments to run).
- `docs/roadmap/priorities-1-3-status.md`: move P3c-2 to Built.
