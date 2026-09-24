# P3a — Policy counterfactual simulator (core)

**Date:** 2026-09-24 · **Status:** approved design, not built
**Priority:** 3a (handoff doc `docs/new/RouteForge-Priorities-1-3-LLM-Handoff.md`)
**Follows:** P2b-2 outcome records (`docs/reference/routing-outcomes.md`), P2b-3 quality
calibration (`docs/reference/quality-calibration.md`), FeasibleRouteBench (`app/eval/`).
**Next:** P3b (minimal conflicting core), P3c (shadow prices, scenarios, capacity, ranges).

## 1. Purpose

An operator is about to change policy — tighten an app's residency, require no-retention,
onboard or remove an endpoint, change a price — and wants to know **before** changing it:

- which real requests would route differently, and where they would go;
- what that does to cost, latency and quality;
- which requests would have **no eligible model** and be rejected;
- which apps and workloads are affected.

P3a answers that offline, from the operator's own recorded traffic, using the **real**
eligibility (`feasible_set`) and routing (`Router`) code. **It never activates anything.**
The review artifact is the report. A human then makes the change through the normal admin
path.

## 2. Non-goals (P3a)

- No live "dry-run" of an admin write. There is no new serving behaviour except a read-only
  snapshot endpoint.
- No minimal conflicting core or explanation of why a proposal rejects (P3b).
- No shadow prices, alternative scenarios, capacity model or uncertainty ranges (P3c).
- No re-running PII detection. Detected entity **types** are replayed as recorded, so a
  proposal cannot change `pii_detection`, denylists, exempt patterns or recognizers.
- No daily-spend or RPM admission simulation. Those are admission limits, not routing.
- No circuit-breaker state. Open circuits are transient; both arms run with none open.
- No confidence-escalation or failover replay. Only the request's **first routing
  decision** is simulated.

## 3. Components

```
GET /admin/config-snapshot ──► snapshot.json ─┐
GET /admin/outcomes/export ──► outcomes.ndjson ┼─► scripts/policy_simulate.py ─► simulation.json
operator-written            ──► proposal.json ─┘      (app/eval/simulate.py)      report.md
```

| Unit | File | Role |
|---|---|---|
| Snapshot export | `app/api/admin_routes.py` (+ builder in `app/apps/snapshot.py`) | Admin-only, read-only, content-free config snapshot |
| Snapshot/proposal loading | `app/eval/scenario.py` | Parse + validate both files; build a scenario's `Settings`, scratch `ModelRegistry`, `QualityResolver`, frozen signals, per-app policies |
| Replay + diff | `app/eval/simulate.py` | Rebuild each row's context, run both arms, cost/latency/quality, aggregate, render |
| CLI | `scripts/policy_simulate.py` | `--export --snapshot --proposal --out` |

`app/eval` may import serving code; serving never imports `app/eval`
(`tests/eval/test_isolation.py` keeps holding). The snapshot builder lives in `app/apps/`
because the serving process builds it.

## 4. Snapshot export — `GET /admin/config-snapshot`

Admin-authenticated (same dependency as `/admin/outcomes/export`), `GET` only, no query
parameters. Returns one JSON document:

```json
{
  "schema": "routeforge.config-snapshot/v1",
  "generated_at": "2026-09-24T21:00:00Z",
  "routing_policy_version": "…",
  "registry_version": "…",
  "settings": {
    "routing": { … RoutingCfg.model_dump(mode="json") … },
    "pricing": { … PricingCfg.model_dump(mode="json") … },
    "policy":  { … PolicyCfg.model_dump(mode="json") … },
    "providers": { "<name>": {"enabled": bool, "region": …, "prompt_retention": …,
                              "accepts_raw_sensitive": …} }
  },
  "onboarded": [ {"slug", "display_name", "provider", "tier", "quality",
                  "price_input", "price_output", "price_cached_input",
                  "enabled", "allowed_app_ids", "region", "prompt_retention",
                  "accepts_raw_sensitive", "context_window"} ],
  "apps": [ {"application_id", "name", "policy": { … AppPolicyView fields … },
             "sensitivity_rules": [ … ]} ],
  "quality": { "customer": [ … ], "calibrated": [ … ] },
  "signals": { "<model>": {"latency_ms", "latency_n", "availability", "availability_n"} }
}
```

- **Content-free and secret-free by construction:**
  - Excluded: endpoint URLs, `api_key_env`, Langfuse keys (anything under
    `serving.observability`), `pii_exempt_patterns`, custom recognizer definitions, and every
    per-app Langfuse/credential column.
  - Only the four settings sections shown are included.
  - `providers` carries only the four listed fields.
  - A test asserts that none of the excluded keys appear anywhere in the document, and that
    a seeded secret value (an API key env value, a Langfuse secret) appears nowhere in the
    serialised JSON.
- `apps[].policy` is every `AppPolicyView` field except `pii_exempt_patterns`. The
  simulator runs `compile_policy` itself on it, so the snapshot never carries a second,
  pre-compiled truth.
- `apps[].sensitivity_rules` is the app's own resolved rule list (the same list the gate
  passes as `app_rules`): entity **types** → sensitivity / residency / risk, never
  patterns.
- `quality` is the live resolver's override maps (`QualityResolver.override_maps()`):
  customer and applied-calibration entries, keyed by model and workload.
- `signals` is the live `SignalStore.snapshot()`.
- Onboarded rows include disabled ones (`enabled: false`), so a proposal can re-enable them.
- `generated_at` is the only non-deterministic field. The simulator ignores it except when
  echoing it in the report.

## 5. Proposal file — `routeforge.policy-proposal/v1`

Operator-written JSON. **Only the keys below exist.** Any other key, at any level, is a load
error naming the key, so a typo can never silently simulate "no change".

```json
{
  "schema": "routeforge.policy-proposal/v1",
  "title": "EU apps: no-retention endpoints only",
  "apps": {
    "<application_id>": {
      "residency": "onprem_only",
      "allowed_regions": ["eu-west"],
      "require_no_retention": true,
      "allowed_tiers": ["cheap", "mid"],
      "allowed_providers": ["onprem"],
      "allowed_models": null,
      "max_cost_usd_per_request": 0.02,
      "escalate_residency_on_detection": true,
      "sensitivity_rules": [ … same shape as the snapshot's … ]
    }
  },
  "providers": { "bedrock": {"enabled": true} },
  "endpoints": {
    "add":     [ {"slug", "provider", "tier", "price": {"input", "output", "cached_input"},
                  "region", "prompt_retention", "accepts_raw_sensitive",
                  "context_window", "quality", "allowed_app_ids"} ],
    "remove":  ["slug"],
    "disable": ["slug"],
    "enable":  ["slug"],
    "update":  { "slug": {"tier", "region", "prompt_retention",
                          "accepts_raw_sensitive", "context_window", "allowed_app_ids"} }
  },
  "prices": { "slug": {"input": 0.1, "output": 0.4, "cached_input": null} }
}
```

**App edits:**
- An app edit **replaces** the named fields and leaves the others as they are.
- `null` means "set to null" (inherit), which is the same meaning the admin `PUT` gives it.
- Set-valued fields are lists.
- An `application_id` that is not in the snapshot is an error.

**Providers and endpoints:**
- `providers` may only toggle `enabled`, and only for a provider declared in the snapshot.
  Enabling a cloud provider brings its YAML `model_pool` entries into the pool, exactly as it
  would live.
- Endpoints:
  - `add`: the slug must be new, and its provider must be declared.
  - `remove`, `disable`, `enable`, `update`: the slug must exist, as an onboarded row or,
    for `prices`, also a YAML pool model.
  - The same slug in two endpoint operations is an error.
- Every value is validated with the same rules the admin write path applies. For example,
  the tier must be canonical and prices non-negative and finite.

**Loading rules:**
- An empty proposal (`{"schema": …}` only) is valid and must predict zero changes (§9).
- Loading is all-or-nothing: a proposal either loads completely or fails, and is never
  partially applied. Any error exits 2 before any simulation runs.

## 6. Scenario construction (`app/eval/scenario.py`)

A **scenario** is everything a routing decision reads. There are two:
- `current` is built from the snapshot;
- `proposed` is built from the snapshot plus the proposal.

Each is built the same way:

1. **Settings.** Load the repo's default config through the normal loader as a base.
   **Replace** `routing`, `pricing` and `policy` wholesale with the snapshot's sections, and
   overlay the `providers` fields. The result goes through the same Pydantic validation and
   cross-file lint as a boot. A snapshot that fails lint is a load error.
2. **Registry.** A scratch in-memory SQLite `ModelRegistry`, the same pattern as
   `app/eval/endpoints.py::build_registry`, with one `upsert_onboarded` row per onboarded
   entry. `endpoint` is the placeholder `sim://<slug>`; `probe_status` is `healthy` for
   enabled rows. After refresh, every enabled slug must be in the effective pool, or it is a
   load error.
3. **Quality.** A `QualityResolver(settings, registry=…)` with `set_overrides(...)` loaded
   from the snapshot's `quality`.
   - An endpoint added by the proposal gets its declared `quality`, or the tier default.
   - A removed endpoint's overrides are dropped.
4. **Signals.** A `FrozenSignals` object in `app/eval`, passed to `Router(signals=…)`. It
   answers `latency(model, tier)` and `availability(model)` from the snapshot's values with
   the same `SignalValue` shapes and sources the live store would give. A model with no
   snapshot entry (for example, a newly added endpoint) gets exactly what a fresh
   `SignalStore` gives: tier default / neutral. **Both arms share one signals snapshot**, so
   a signal difference can never masquerade as a policy effect.
5. **Apps.** For each app, an `AppPolicyView` (built the way `app/eval/cases.py::policy_view`
   builds one: lists become frozensets) plus its `sensitivity_rules`, then
   `compile_policy(view, settings)`.

## 7. Replay (`app/eval/simulate.py`)

**Rows.**
- Every export row is one real request.
- A row is **simulated** when all of these hold:
  - `status` is one of the P2b-2 outcome statuses: `ok`, `backend_failed`, `verify_failed`,
    `no_eligible` or `context_overflow`;
  - `complexity` and `prompt_tokens` are present;
  - `application_id` is in the snapshot;
  - `cache_hit` is false.
- Every other row is **excluded**, with a counted reason: `cache_hit`, `unknown_status`,
  `unknown_app`, `missing_features`. It is never silently dropped.

**Context rebuild (per row, per arm).**
- Call `build_context(settings, request_id, tenant_id=application_id, entities, None,
  app_policy=view, app_rules=rules, registry=registry, escalate_residency=eff.escalate_residency_on_detection,
  content_integrity=eff.protection.content_integrity, model_input=eff.protection.model_input,
  workload=row.workload)`.
- `entities` holds one placeholder `Entity(type=t, start=0, end=0, value="")` per recorded
  `detected_entity_types` entry. Only `.type` is read on this path, and a test pins that.
- This re-applies the **scenario's** sensitivity rules to the recorded entity types. A
  proposal that makes `US_SSN` escalate to `onprem_only` therefore shows its effect. The
  recorded `sensitivity`, `risk_class` and `onprem_only` are used only for the baseline
  check, never as inputs.

**Decision.**
- Call `Router(settings, quality_resolver, registry, signals=frozen).select(ctx,
  tokens_after=row.prompt_tokens, complexity=row.complexity)`.
- `output_tokens` is left to the deployment default (`expected_output_tokens(settings,
  None)`), because a request's `max_tokens` is not recorded.
- The outcome is either:
  - `routed`: the chosen model and tier, plus the eligible names; or
  - `rejected`: `NoEligibleCandidate`, with the rejection codes.

**Scoring a routed pick:**
- **Cost:** `price_call(registry.price_for(model), prompt_tokens=row.prompt_tokens,
  completion_tokens=row.completion_tokens)`. The recorded tokens are post-compression, and
  cached tokens are taken as zero, the same assumption the router makes. The price comes
  from the **scenario's** registry, so a price change moves cost exactly.
- **Latency:** looked up in this order, and the source is labelled on every value:
  1. the model's observed median `latency_ms` over `ok` rows in the export where it served
     the first attempt (`observed`);
  2. otherwise the snapshot signal (`signal`);
  3. otherwise the tier default (`tier_default`).
- **Quality:** the scenario resolver's `resolve(model, workload)` value and its source
  (the P2b-3 ladder).

**Baseline check (fidelity, not correctness).**
- For every simulated `ok` row with a recorded first attempt, compare the `current` arm's
  pick with `attempts[0]`.
- Report the agreement rate overall and per app, plus the top disagreement pairs
  (`recorded → simulated`).
- Agreement below 90% prints a warning at the top of the report. The main causes are drift
  since the export window (onboarding, prices, signals), the unrecorded pin and
  `max_tokens`, and breaker state.
- **The headline diff is always `current` vs `proposed`, both simulated.** It is never
  logged vs simulated, so drift affects both arms equally.

## 8. Output

`--out DIR` writes two files.

**`simulation.json`** is deterministic: the same inputs give byte-identical output.
- Floats are rounded to 6 places; `sort_keys=True`.
- There is no wall-clock field except the snapshot's `generated_at`, echoed verbatim.
- It contains:
  - `inputs`: the sha256 of the export, snapshot and proposal; the proposal title; the
    snapshot's `routing_policy_version` / `registry_version`; and the export window (min/max
    `ts`).
  - `simulation_id`: sha256 over the canonical `inputs`.
  - `edits`: the normalised proposal, echoed.
  - `rows`: counts of simulated rows and of excluded rows by reason.
  - `baseline`: the agreement rate, per-app rates and the top 10 disagreement pairs.
  - `totals`, and the same block `by_app` and `by_workload`:
    - `requests`;
    - `route_changed`;
    - `newly_rejected`, `newly_served`;
    - `cost_usd`: current, proposed and delta;
    - `latency_ms` p50 and p95: current, proposed, delta;
    - `quality_mean`: current, proposed, delta;
    - `onprem_share`: current and proposed.
  - `moves`: the top 20 `(current_model → proposed_model)` pairs with counts, where
    `(rejected)` is a model value.
  - `new_rejections`: counts by app and rejection code.
  - `affected_apps`: the apps with any `route_changed`, `newly_rejected` or `newly_served`.
  - `per_day`: each cost figure divided by the export window in days. It is omitted when the
    window is under 1 day.

**`report.md`** opens with:

> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, using the real eligibility and routing
> code and a frozen snapshot of signals. It does not model failover, escalation, breaker
> state, request pins or caching.

It continues with:
- the baseline warning, if any;
- the headline table;
- new rejections, which come first because they break clients;
- moves;
- per-app and per-workload tables;
- the latency-source and quality-source breakdown;
- the exclusion counts.

**CLI exit codes:**
- 0: simulation written.
- 2: an input file failed to load or validate.
- 3: the proposal introduces rejections and `--fail-on-new-rejections` is set, for use in
  CI or GitOps.

## 9. Invariants and tests

The tests go in `tests/eval/test_simulate.py`, `tests/eval/test_scenario.py` and
`tests/api/test_config_snapshot.py`.

1. **Empty proposal ⇒ zero change.** Every row routes identically in both arms, and every
   delta is exactly 0.
2. **Stricter never widens.** This is a Hypothesis property. For a random snapshot and row,
   a proposal that only tightens residency, narrows allow-lists, adds `allowed_regions`,
   sets `require_no_retention: true` or removes/disables endpoints must leave every row's
   `proposed` eligible set a **subset** of its `current` eligible set.
3. **Residency is absolute in simulation too.** Tightening an app to `onprem_only` means no
   `proposed` pick for that app has a cloud provider. When no on-prem model exists, its rows
   become `newly_rejected`.
4. **Sole-model removal ⇒ rejections.** Removing the only eligible model for an app turns
   exactly those rows into `newly_rejected`.
5. **Price moves cost exactly.** Changing one model's prices without changing any route
   changes `cost_usd.delta` by exactly Σ(Δprice × tokens) over the rows routed to it.
6. **Sensitivity rules replay.** A proposed rule that escalates a recorded entity type to
   `onprem_only` moves exactly the rows carrying that type.
7. **Baseline fidelity.** When the snapshot is taken from the same config that produced a
   synthetic export (a round trip through a real pipeline run in-test), agreement is 100%.
8. **Determinism.** Two runs over the same inputs give byte-identical `simulation.json`.
9. **Load errors.** Each of these exits 2 with the offending key, slug or app named:
   - an unknown proposal key;
   - an unknown app or slug;
   - a duplicate slug op;
   - an undeclared provider;
   - an invalid tier or price;
   - a bad schema major.
10. **Snapshot.** The endpoint is admin-only (401 without auth, 403 for a non-admin token).
    The document has none of the excluded keys and none of the seeded secret values. The
    round trip holds: a scenario built from a snapshot of a test app reproduces that app's
    live `feasible_set`.
11. **Isolation.** Serving never imports `app/eval`, which is the existing test extended to
    the new modules.

## 10. Documentation

- `docs/reference/policy-simulator.md`: operator guide covering the snapshot, the proposal
  schema, the CLI, reading the report, and the caveats (§7 baseline, §2 non-goals).
- `docs/research/feasible-route-bench.md` §15: the simulator as a counterfactual over
  recorded traffic, its fidelity model, the invariants (§9) and results on the Spark's real
  export.
- `docs/backlog/bugs.md`: add **B55**, "outcome rows don't record the request's pinned model
  or `max_tokens`, so the simulator replays pinned requests unpinned and budgets with the
  default output estimate". Recording both would need an ALTER, and that change is deferred.

## 11. Deployment

- The snapshot endpoint is new code on the serving image, so it follows the standard Spark
  deploy.
- It needs **no schema change and no ALTER**.
- After deploy, run the simulator once against the Spark's own export with an empty proposal
  and one real proposal, and record the results in research §15.
