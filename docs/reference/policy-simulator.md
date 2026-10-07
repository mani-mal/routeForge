# Policy counterfactual simulator (P3a)

An operator is about to change policy — tighten an app's residency, require no-retention,
onboard or remove an endpoint, change a price — and wants to know **before** changing it:
which real requests would route differently, what that does to cost, latency and quality,
which requests would newly have no eligible model, and which apps and workloads are
affected.

The simulator answers that offline, from the operator's own recorded traffic, by replaying
each request's **first routing decision** through the real eligibility (`feasible_set`) and
routing (`Router`) code, once under the config as it is today and once under the config with
the proposal applied. **It never activates anything.** It writes no admin API call, opens no
database, and touches nothing on the running deployment. The review artifact is the report; a
human still makes the change through the normal admin path (`PUT`ing app policy, onboarding or
removing an endpoint, editing prices).

- Design: `docs/superpowers/specs/2026-09-24-policy-simulator-design.md`. Shadow prices (§8)
  are `docs/superpowers/specs/2026-09-26-shadow-prices-design.md` (P3c-1). Uncertainty ranges
  (§10) are `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md` (P3c-3). Capacity
  (§11) is `docs/superpowers/specs/2026-10-07-p3c4-capacity-design.md` (P3c-4).
- Research notes (the model, the two-arm design, the invariants):
  `docs/research/feasible-route-bench.md` §15; shadow prices §17.
- Prerequisite datasets: `docs/reference/routing-outcomes.md` (P2b-2, the export this reads)
  and `docs/reference/quality-calibration.md` (P2b-3, the quality ladder replay uses).

Code: `app/apps/snapshot.py` (snapshot builder), `app/api/admin_routes.py`
(`GET /admin/config-snapshot`), `app/eval/scenario.py` (snapshot/proposal → scenario),
`app/eval/proposal.py` (proposal load/validate/apply), `app/eval/simulate.py` (replay +
report), `app/eval/shadow.py` (shadow prices, P3c-1), `app/eval/uncertainty.py` (ranges,
P3c-3), `app/eval/capacity.py` (capacity, P3c-4), `scripts/policy_simulate.py` (CLI).

## 1. Three inputs

```
GET /admin/outcomes/export ──► outcomes.ndjson ┐
GET /admin/config-snapshot ──► snapshot.json    ┼─► scripts/policy_simulate.py ─► simulation.json
operator-written            ──► proposal.json  ┘                                  report.md
```

### 1.1 The outcomes export

The recorded traffic to replay. `docs/reference/routing-outcomes.md` §7 is the full
reference; the shape used here is the same `routeforge.outcome/v1` NDJSON.

```bash
curl -s -b "rf_admin_session=<session>" \
  "http://<host>:8080/admin/outcomes/export?since=2026-09-16T00:00:00Z&application_id=app-claims" \
  > outcomes.ndjson
```

`rf_admin_session` is the cookie the console sets at admin login.

Two per-row fields make a replay faithful to the request itself (B55, 2026-09-24):
`requested_model` — the client's model pin, re-applied to the rebuilt context exactly as the
gate applies it, so a pinned row replays to its pin (and a proposal that removes or
disables the pinned model rejects it, `not_requested`); and `max_tokens` — the client's
output-token cap, which the router prices the row at (`expected_output_tokens`), so the
per-request budget check sees the real cap. Both are null for an unpinned or uncapped
request. **Rows exported before these fields existed lack both keys**; such a row is still
simulated, unpinned and at the deployment's default output-token estimate.

A third per-row field makes a **rejected** request replayable (B60, 2026-10-06):
`routing_tokens` — the token count the router was given (RouteForge's own post-gate,
post-compression count). Replay hands it to `Router.select`, so the per-request budget check
sees the size the live check saw. It is recorded on every status. `prompt_tokens` is a
different number — the backend's billed count, zero on a row no backend answered — and replay
still prices a served row from it (§6). **A rejected or failed row exported before
`routing_tokens` existed has no record of its size at all**, and is excluded, counted as
`missing_routing_tokens`, rather than replayed as an empty prompt. A served row from before
it replays as it always did.

### 1.2 The config snapshot

Everything a routing decision reads — settings, the onboarded pool, per-app policy,
quality overrides and the current signals — as one content-free JSON document (§7).
That includes `settings.serving_policy`: the deployment's four posture values
`compile_policy` reads (`pii_detection`, `allow_app_pii_downgrade`, `default_policy_mode`,
`allow_app_passthrough`, with any `SP_*` env overrides already applied). Every app is
compiled under **the snapshot's** posture, never the posture of the machine running the
simulator — so an app that inherits `default_policy_mode` compiles to the same
`model_input` (and so the same raw-sensitive eligibility) offline as it does live. A
snapshot without this block (taken before it existed) is refused with
`settings.serving_policy: missing`; re-export it.

```bash
curl -s -b "rf_admin_session=<session>" \
  "http://<host>:8080/admin/config-snapshot" \
  > snapshot.json
```

`GET /admin/config-snapshot` is read-only and admin-only (`require_admin`; `401` without the
session cookie). `require_admin` is session-cookie only, so every non-admin caller — including
one presenting an application API token — gets `401`; there is no `403` case. It calls `state.registry.ensure_loaded(state.db)` before snapshotting
(`app/api/admin_routes.py:1288`), so a snapshot taken before any request has reached the
registry still lists onboarded models instead of an empty pool.

### 1.3 The proposal

An operator-written JSON file describing the policy edit to try — §2 below.

## 2. The proposal schema

Schema `routeforge.policy-proposal/v1`. **Only the keys below exist** — an unknown key
anywhere in the document is a load error naming its exact dotted location, so a typo can
never silently simulate "no change" (`app/eval/proposal.py::_validate_keys`).

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
      "sensitivity_rules": [
        {"entities": ["US_SSN"], "sensitivity": "restricted", "risk_class": "high",
         "residency": "onprem_only", "require_verification": true}
      ]
    }
  },
  "providers": {"bedrock": {"enabled": true}},
  "endpoints": {
    "add": [
      {"slug": "local-mid-2", "provider": "onprem", "tier": "mid",
       "price": {"input": 0.5, "output": 1.5, "cached_input": null},
       "region": "eu-west", "prompt_retention": "none", "accepts_raw_sensitive": false,
       "context_window": 32768, "quality": 0.8, "allowed_app_ids": null}
    ],
    "remove": ["old-slug"],
    "disable": [],
    "enable": [],
    "update": {"local-mid": {"tier": "mid"}}
  },
  "prices": {"local-mid": {"input": 0.4, "output": 1.2, "cached_input": null}}
}
```

An empty proposal — `{"schema": "routeforge.policy-proposal/v1"}` — is valid and must
predict zero change in every row (spec §9.1).

### 2.1 Allowed keys and their validation

`ProposalError` always names the offending dotted location (`app_id.field`,
`endpoints.add.<slug>`, …). Every value is validated against the snapshot **before**
anything is applied — loading is all-or-nothing.

**Top level (`_TOP_KEYS`):** `schema`, `title`, `apps`, `providers`, `endpoints`, `prices`,
`workload`, `capacity`. Every container must be the documented type — `apps`, `providers`,
`prices`, `endpoints`, `endpoints.update`, `workload`, `workload.apps`, `capacity` and each
`capacity.<slug>` objects; `endpoints.add` a
list of objects; `remove`/`disable`/`enable` lists of slug strings — or loading fails naming
the path.

**`apps.<application_id>`** — the app must already be in the snapshot. A field not named in
the edit is left as-is; `null` means "set to null" (inherit), the same meaning the admin
`PUT` gives it. After the per-field checks below, the merged policy is built into an
`AppPolicyView` and run through `compile_policy` — any resulting `PolicyViolation` is also
a load error, named `apps.<application_id>`, and so is any value `compile_policy` could not
parse and failed closed on (its `rejected` list — e.g. an empty or blank `allowed_regions`),
named `apps.<application_id>.<field>`. The rules mirror the admin write path
(`app/api/admin_routes.py::_validate_policy_values` plus the compiler's refusals): a value
the admin `PUT` would refuse is a proposal load error too. As on the admin path, the
allowlist checks apply only to a value the edit **changes** — re-sending the stored value
is never refused.

| Key | Validation |
|---|---|
| `residency` | Must parse via `resolve_residency` (`any`, `private_cloud_ok`, `onprem_only`, or `null`). |
| `allowed_regions`, `allowed_tiers`, `allowed_providers`, `allowed_models` | Must be a list of strings or `null`. `allowed_tiers`, `allowed_providers` and `allowed_models` may not be an empty list (the store reads `[]` as "no restriction"; use `null` to clear). `allowed_tiers` entries must be canonical tiers (`cheap`, `mid`, `frontier`); `allowed_providers` entries must be declared in the snapshot's `settings.providers`; `allowed_models` entries must be a YAML pool model, an onboarded row in the snapshot, or a slug this same proposal's `endpoints.add` onboards. `allowed_regions` must be non-empty with non-blank tokens (refused by `compile_policy`, as `ALLOWED_REGIONS_INVALID` is on the admin path). |
| `require_no_retention`, `escalate_residency_on_detection` | Must be a bool or `null`. |
| `max_cost_usd_per_request` | Must be a finite number `> 0`, or `null`. |
| `sensitivity_rules` | A list; each rule needs `entities` (list of strings), `sensitivity` (a value in the deployment's sensitivity scale), `residency` (parseable), `risk_class` (a configured risk class) and `require_verification` (bool). |

**`providers.<name>`** — `{"enabled": bool}` only; the provider must already be declared in
the snapshot's `settings.providers`.

**`endpoints`** — keys `add`, `remove`, `disable`, `enable`, `update`. The same slug may
appear in at most one of these operations.

| Operation | Validation |
|---|---|
| `add` (list of objects) | Keys: `slug`, `provider`, `tier`, `price`, `region`, `prompt_retention`, `accepts_raw_sensitive`, `context_window`, `quality`, `allowed_app_ids`, `latency_ms`. `slug` must be new — not an existing onboarded row or a priced YAML pool model. `provider` must be declared. `tier` must be canonical. `price.input`/`price.output` must be finite ≥ 0; `price.cached_input` null or finite ≥ 0. `prompt_retention` null or one of `none`/`retained`/`unknown`. `accepts_raw_sensitive` null or bool. `context_window` null or an int `> 0`. `quality` null or a finite value in `[0, 1]`. `allowed_app_ids` null or a list of application ids that exist in the snapshot. `region` must be a string or `null`; the token itself is customer vocabulary and is not checked against a list (admin parity). `latency_ms` null or finite `> 0`. |
| `remove`, `disable`, `enable` (lists of slugs) | Each slug must already be an onboarded row. |
| `update` (map slug → fields) | Same slug requirement. Fields: `tier`, `region`, `prompt_retention`, `accepts_raw_sensitive`, `context_window`, `allowed_app_ids`, `latency_ms` — validated the same way as `add`'s matching field, only when the field is present (`region`: string or `null`). `update` also accepts a priced YAML pool model; `latency_ms` is the only field allowed on one. |

**`prices.<slug>`** — `{"input", "output", "cached_input"}`, same price validation as
`endpoints.add[].price`. The slug must be an onboarded row not being removed in this same
proposal, or a priced YAML pool model.

**`workload`** (P3c-2) — assumptions about the traffic itself, not configuration. They apply
to the `proposed` arm only; the `current` arm always replays rows as recorded.

| Key | Validation | Effect in replay |
|---|---|---|
| `token_scale` | Finite, `0 < x ≤ 10`. Default `1.0`. | Before routing and pricing, the same rule scales TWO counts independently (`app/eval/simulate.py`: `scaled_request` for the routing count, `_scale_tokens` for the pricing count): `x' = max(1, round(x × scale))`, a row that recorded a `0` count stays `0`. The **routing** count — what `Router.select` is given, the per-request budget check and the cost term of the score, and what a saturated complexity is re-estimated from — and the **pricing/context-window** count — what the routed call is priced at and the context-window check sizes against a candidate's declared window — scale separately. On a row recorded before `routing_tokens` existed, the routing count equals `prompt_tokens`, so the two scale identically, as before B60. `completion_tokens` is unchanged. A recorded `complexity` below 1 becomes `min(1, complexity × scale)`, which is exact. A saturated complexity (1.0) hides the real token count, so it becomes `min(1, max(scale, routing_tokens' / 2000))`; when the scale is below 1 that is an estimate, and the row is counted in `assumptions.saturated_complexity_estimated`. |
| `cached_share` | Finite, `0 ≤ x ≤ 1`. Default `0`. | That fraction of the (scaled) prompt tokens is priced at the endpoint's cached-input price. An endpoint with no declared cached price bills them at the input price. The router still estimates with zero cached tokens, so this moves cost, never a route. |
| `volume_scale` (P3c-4) | Finite, `0 < x ≤ 1,000,000`. Default `1.0`. | Counts every routed request that many times **in the capacity section only** (§11.3). It moves no route, no cost and no other figure. Exit `2` when no capacity is stated anywhere. |
| `apps.<application_id>` | The app must be in the snapshot. The same three keys. | Overrides the global value for that app. |

Outcome rows do not record cached tokens, so any non-zero `cached_share` is an assumption
and the report says so.

`token_scale` changes the tokens priced and checked against a candidate's declared
`context_window` (§6); it never changes the replayed latency, which comes only from the
`endpoints.*.latency_ms` assertion below, an observed/signal/tier-default value, or the
router's own latency scoring term.

**`endpoints.add[].latency_ms` and `endpoints.update.<slug>.latency_ms`** (P3c-2) — an
operator-asserted latency in milliseconds: finite `> 0`, or `null` for "no override". Use it
to model a serving-engine or hardware change; RouteForge cannot see the serving engine, so
this is an assertion, not a measurement. It becomes the replayed latency of that endpoint
(`latency_source: "proposal"`, ahead of observed, signal and tier-default latency) and the
router's latency scoring term, where it is treated exactly as a measurement would be —
including as the same-tier peer prior for unmeasured endpoints. When
`routing.signals.enabled` is false the live router ignores measured latency, so the assertion
then changes the replayed latency only.

**`capacity.<slug>`** (P3c-4) — limits the operator **states** for an endpoint in the proposed
arm: any of `max_concurrency`, `requests_per_minute`, `tokens_per_minute`,
`output_tokens_per_minute`, each a finite number `> 0` (a `0` is refused; leave the key out
for "not stated"). The slug must be an onboarded row this proposal does not remove, a priced
YAML pool model, or an endpoint this proposal adds. It overrides a `--capacity` file's value
for that endpoint limit by limit. §11.

**Where assumptions live.** `apply_proposal` writes them into the applied document as a
top-level `assumptions` block: `{"token_scale": {"*": x, "<app>": y}, "cached_share": {...},
"latency_ms": {"<slug>": ms}}`, absent when the proposal sets none. A snapshot **file** that
already contains `assumptions` is a load error, so a hand-edited snapshot cannot put
assumptions into the `current` arm.

`capacity` and `workload.volume_scale` (P3c-4) are the exception. `apply_proposal` validates
them and writes them **nowhere**: they are read from the proposal itself after replay, are
echoed in `edits` and in the capacity block (§11.8), and are not in the `assumptions` block.
That is what keeps a run with no capacity stated byte-identical.

## 3. Running the CLI

```bash
uv run python scripts/policy_simulate.py \
  --export outcomes.ndjson --snapshot snapshot.json --proposal proposal.json \
  --out results/sim \
  [--config-dir config] [--fail-on-new-rejections] \
  [--uncertainty [--bootstrap N] [--interval P] [--seed S] [--min-rows K]
                 [--quality-posteriors FILE ...]] \
  [--capacity FILE [--capacity-bucket SECONDS] [--capacity-min-buckets K]]
```

| Flag | Default | Meaning |
|---|---|---|
| `--export` | required | `routing_outcomes` NDJSON export file |
| `--snapshot` | required | `GET /admin/config-snapshot` JSON file |
| `--proposal` | required unless `--shadow-prices` or `--scenarios` | `routeforge.policy-proposal/v1` JSON file |
| `--out` | required | output directory (created if missing) |
| `--config-dir` | `config` | the base config directory the snapshot is overlaid onto. The snapshot replaces `routing`/`pricing`/`policy` wholesale and overrides each declared provider's data-handling fields and the four `serving_policy` posture values, so the base only supplies what no routing decision reads (and the set of declared providers). |
| `--fail-on-new-rejections` | off | exit `3` instead of `0` when the proposal (or any scenario) newly rejects any simulated row (for CI/GitOps gating) |
| `--shadow-prices` | off | price what each constraint costs (P3c-1, §8 below); makes `--proposal` optional |
| `--max-relaxations` | `50` | cap on relaxations `--shadow-prices` evaluates, must be `>= 1` (exit `2` otherwise) |
| `--scenarios` | off | a `routeforge.policy-scenarios/v1` file; compares its scenarios side by side (§9). Cannot be combined with `--proposal` or `--shadow-prices` (exit `2`) |
| `--since` | off | ISO-8601 UTC timestamp. Rows with `ts` before it are excluded and counted as `before_since`. Applies to every mode. A value with no offset is read as UTC |
| `--min-agreement` | off | `0 ≤ X ≤ 1`. Compared with the **lower** of the two baseline agreements (§5): model agreement and outcome agreement. If either is not computable, or the lower one is below `X`, print both and exit `4` before any file or directory is written. Applies to every mode |
| `--uncertainty` | off | add a range to every headline figure (P3c-3, §10): a percentile interval from a bootstrap over the simulated rows. Sampling variability only. Applies to every mode |
| `--bootstrap`, `--interval`, `--seed`, `--min-rows`, `--quality-posteriors` | `1000`, `0.90`, `0`, `30`, none | tuning for `--uncertainty` (§10.3); each is exit `2` without it |
| `--capacity` | off | a `routeforge.capacity-assumptions/v1` file: the limits the operator states for endpoints as they are, for both arms (P3c-4, §11). Needs `--proposal` or `--scenarios` |
| `--capacity-bucket`, `--capacity-min-buckets` | `60`, `10` | tuning for the capacity section (§11.4); each is exit `2` when no capacity is stated |

`--proposal` is required unless `--shadow-prices` or `--scenarios` is given — with none of
the three, the CLI exits `2` (an argparse usage error, since there is nothing to simulate).

**Exit codes** (`scripts/policy_simulate.py`):

| Code | Meaning |
|---|---|
| `0` | `simulation.json` and `report.md` written (`--proposal`, or `--shadow-prices` alone); `comparison.json` and `report.md` written (`--scenarios`, §9.4) |
| `2` | an input failed to load, validate or apply — a bad export, an invalid proposal (unknown key, bad value, a container of the wrong type), a malformed snapshot (a missing or mistyped nested key, named by its dotted path), a snapshot that loads structurally but fails **deep** validation during `simulate()`/`shadow_prices()` (for example a provider the running deployment's own `config/` never declared), a `--config-dir` that cannot load (missing key, bad YAML, a failed config lint), neither `--proposal` nor `--shadow-prices` given, `--max-relaxations < 1`, or a shadow-prices eligible-superset invariant breach (`ShadowInvariantError`, printed as `error: internal invariant: ...` — spec §3; a relaxation narrowed some row's eligible set, which should never happen since every relaxation only widens eligibility, so it means a bug, not a reportable finding). A relaxation that newly **rejects** a baseline-routed row is *not* a breach — it is reported as `newly_rejected` (§8.4) and exits `0`. `--scenarios` combined with `--proposal` or `--shadow-prices` (an argparse usage error), neither `--proposal` nor `--shadow-prices` nor `--scenarios` given (also an argparse usage error), an invalid scenarios file (named `scenarios[<i>](<name>).proposal.<path>`), a `--since` that is not a timestamp, a `--min-agreement` outside `[0, 1]`, and a `--scenarios` comparison invariant breach (`ComparisonInvariantError`, printed as `error: internal invariant: ...` — two scenarios disagreed about the rows, excluded counts or baseline they share by construction, which means a bug, never a reportable result) are also exit `2`. So are an `--uncertainty` tuning flag given without `--uncertainty`, a tuning value out of range, and a `--quality-posteriors` file that does not load or does not match its own `set_id` (§10.3). So are a `--capacity` file that does not load or validate, a capacity flag out of range, a capacity tuning flag or a `workload.volume_scale` with no capacity stated anywhere, `--capacity` with `--shadow-prices` and no `--proposal`, and an asserted endpoint latency above 24 hours when a capacity is stated (§11.4) — each printed as `error: capacity: …`. Nothing is written; the offending location is on stderr — never a traceback. |
| `3` | the proposal (or any scenario) introduces new rejections and `--fail-on-new-rejections` was passed |
| `4` | the lower of model agreement and outcome agreement (§5) is below `--min-agreement`, or there is no comparable row. Nothing is written. The message names both rates and both outcome-disagreement counts |

The CLI is file-based only: no DB, no `app.api` import, no network
(`tests/eval/test_isolation.py` pins that `app/eval` is never imported by serving code, and
the reverse — this script only ever reads files and the base `config/` directory).

## 4. Reading the output

### 4.1 `simulation.json`

Deterministic: the same three inputs give byte-identical output (floats rounded to 6
places, `sort_keys=True`). `simulation_id` is the first 16 hex characters of a sha256 over
the canonical `inputs` block (export/snapshot/proposal hashes, the proposal title, the
snapshot's `routing_policy_version`/`registry_version`/`generated_at`/`serving_policy`, and
the export window).

| Field | Contents |
|---|---|
| `schema` | `"routeforge.simulation/v1"` |
| `simulation_id` | first 16 hex characters of the sha256 over the canonical `inputs` block |
| `inputs` | `export_sha256`, `snapshot_sha256`, `proposal_sha256`, `proposal_title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy` (the four posture values every app was compiled under — the snapshot's), `window` (`{start, end}` over the simulated rows' `ts`, or `null`), `since` (the `--since` cut as an ISO string, or `null`). With `--uncertainty` only: `uncertainty` (§10.7); the key is absent otherwise. With a stated capacity only: `capacity` (§11.8); absent otherwise |
| `edits` | the proposal, minus `schema`/`title`, keys sorted — exactly what was asked for |
| `rows` | `simulated` (count), `estimated_tokens` (simulated rows with no backend token count, sized and priced from `routing_tokens` — §6) and `excluded` (count per reason, first match wins: `before_since`, `cache_hit`, `unknown_status`, `unknown_app`, `missing_features`, `missing_routing_tokens`). `missing_routing_tokens` counts a well-formed row that records no size: not `ok`, no `routing_tokens`, and `prompt_tokens` zero — a rejected or failed row from before B60. A mistyped `routing_tokens` (anything but an integer ≥ 0 or null) is `missing_features`. `missing_features` counts a row whose `request_id`, `complexity`, `prompt_tokens` (numeric), `completion_tokens` (numeric or null), `detected_entity_types` (list of strings or null; the key must be present), `workload`, `attempts` or `ts` is missing or mistyped, or whose `requested_model` (non-empty string or null) or `max_tokens` (integer > 0 or null) is mistyped — these two may be absent (an older export) — a malformed row is counted, never a crash. A non-object export line is a skipped-line warning. |
| `baseline` | the fidelity check (§5). Model agreement: `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements`, `overflow_contradicted`. Outcome agreement: `outcome_rows_compared`, `outcome_agreement`, `outcome_warning`, `recorded_rejected`, `recorded_rejected_but_routed`, `recorded_routed_but_rejected`, and `outcome_by_app.<id>` (`rows`, `agreement` and the same three counts) |
| `totals`, `by_app.<id>`, `by_workload.<label>` | each the same block: `requests`, `route_changed`, `newly_rejected`, `newly_served`, `cost_usd` (`{current, proposed, delta}` — each arm's routed rows), `cost_usd_both_routed` (same shape, over only the rows routed in **both** arms), `latency_ms.p50`/`.p95` (same shape), `quality_mean` (same shape), `onprem_share` (`{current, proposed}`), `overflow_hops` (`{current, proposed}`) |
| `moves` | top 20 `{from, to, count}` model-pair moves; `(rejected)` is a legal `from`/`to` value |
| `new_rejections` | `{by_app: {...}, by_code: {...}, by_core: {...}}` — rows that were `routed` under `current` and `rejected` under `proposed`. `by_core` (P3b) groups by the `proposed` arm's minimal conflicting core: requirement ids joined with `+` (e.g. `"residency+requested_model"`), in `REQUIREMENT_ORDER`; when the core itself is empty it falls back to `"(state:" + ",".join(core_state) + ")"`; when both are empty (the router's own core computation failed) it is `"(unknown)"`. See `docs/reference/conflicting-core.md`. |
| `affected_apps` | apps with any `route_changed`, `newly_rejected` or `newly_served` row |
| `sources` | per arm, a count of `latency_source`/`quality_source` values (`observed`/`signal`/`tier_default`/`proposal`, `published`/`tier_default`/`calibrated`/`customer`) across routed rows |
| `per_day` | `{cost_usd: {current, proposed, delta}}` divided by the export window in days; `null` when the window is under 1 day |
| `assumptions` | always present: `token_scale`, `cached_share`, `latency_ms` (each a map, empty when the proposal sets none) and `saturated_complexity_estimated` (rows whose scaled complexity is an estimate) |
| `uncertainty` | only with `--uncertainty`: a range for every figure of `totals`, `by_app` and `per_day` (§10.7). No other field changes when it is present |
| `capacity` | only with a stated capacity: per endpoint and arm, replayed demand per time bucket against the stated limits, every peak with its average over the window and the share of buckets above the limit (§11.8). No other field changes when it is present |

### 4.2 `report.md`

Opens with the caveat, verbatim, as a blockquote:

> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, plus deterministic context-window overflow
> re-selection predicted from each endpoint's declared context window, using the real
> eligibility and routing code and a frozen snapshot of signals. It does not model other
> failover, escalation, breaker state or caching.

Then, in order: one line stating both baseline agreements (always printed), a baseline
warning for each agreement that is below 0.90, a note for every recorded outcome replay
does not reproduce and for rows sized from an estimate (§5), an **Assumptions** section
(only when the proposal sets `workload` or `latency_ms`), **Headline** (the `totals`
metrics table), **New rejections** (by app, by code, then **by conflicting core** — a
`| core | count |` table, sorted by count descending then core key — or "None." when there
are none), **Moves** (from → to → count — or "None."), **By app** (one subsection per app,
same metrics table), **By workload** (same, per workload label), **Latency and quality
sources**, **Excluded rows** (reason → count). With a stated capacity, a **Capacity** section
follows (§11.8). With `--uncertainty`, an **Uncertainty ranges** section follows (§10.8);
everything before them is unchanged.

**Read the two cost rows together.** `cost_usd` sums each arm over the rows *that arm*
routed, so a row the proposal newly rejects simply disappears from the proposed sum — it
reads as a saving when it is really a request that would no longer be served.
`cost_usd (both routed)` sums only the rows routed under **both** arms, so its delta is the
like-for-like cost change. When `newly_rejected` is non-zero, trust the both-routed delta
for cost, and read `newly_rejected` for the service loss.

## 5. Fidelity: the baseline check

Simulation reconstructs context and re-runs real code, so it can drift from what was
actually served. The baseline check (spec §7, `app/eval/simulate.py::_baseline`) is that
fidelity check — **not** the headline result.

For every simulated `ok` row that carries a recorded first attempt (`row["attempts"]`), the
`current` arm's pick is compared with `attempts[0]`. The report shows the overall agreement
rate, the rate per app, and the top 10 `recorded → simulated` disagreement pairs by count.
Agreement below 90% prints a warning at the top of `report.md`.

The comparison is between **first decisions**: the model the `current` arm selected first,
before any context-window re-selection (§6), against the recorded first attempt. A row the
`current` arm rejects always counts as a disagreement. So does a row whose record proves the
*recorded* first pick served it alone, live (`chosen == attempts[0]`, one recorded attempt),
while replay's `current` arm excludes that same pick as a predicted overflow (§6) — even
though the first picks still match. That subset is reported separately as
`overflow_contradicted`: it is a fidelity check on the endpoint's declared `context_window`
(or the default output-token estimate, when `max_tokens` was not recorded), not an ordinary
routing disagreement, and `report.md` prints it next to the agreement banner whenever it is
non-zero. `--since TS` is the way to cut an export at the first row a replay can be trusted for — for
example the first row recorded after B55.

**Outcome agreement (B60).** Model agreement looks only at `ok` rows, so it cannot see a
request the record says was *rejected* and replay *routes*. A second measure covers that.
Over **every simulated row**, the `current` arm's outcome — routed or rejected — is compared
with the class of the recorded status:

| Recorded `status` | Class | Why |
|---|---|---|
| `ok` | routed | served |
| `verify_failed` | routed | a model was chosen and answered; step 9 refused the answer |
| `backend_failed` | routed | a model was chosen; every attempt failed upstream |
| `no_eligible` | rejected | the router found no candidate at the first decision |
| `context_overflow` | rejected | every attempt was refused for size; replay models that as a `context_window` rejection |

`outcome_agreement` is the share that match. Two counts say which way it went wrong:
`recorded_rejected_but_routed` (out of `recorded_rejected`) and
`recorded_routed_but_rejected`. All of it is also broken down per app (`outcome_by_app`).

Every report states both agreements, on a clean run too. A rate below 90% prints a warning. **Any
non-zero count prints a note with its denominator** ("2 of 7 recorded rejections replay as
routed"), whatever the rate is: rejections are usually a small share of traffic, so every one
of them can replay wrong while `outcome_agreement` stays above 90%. The count out of the
rejected rows is the number that says whether a "newly served" figure can be trusted.

**`--min-agreement X` is a gate on the lower of the two** (exit `4`, nothing written). Model
agreement catches a replay that picks a different model (B55); outcome agreement catches one
that serves a rejected request (B60). A replay is fit to answer "what would change" only if
it reproduces both, so neither is allowed to hide behind the other, and they are not pooled.
Both must be computable: with no served row, nothing vouches for which model replay picks,
however well the outcomes agree. Outcome agreement is taken over all simulated rows, so it is
diluted as described above; `--min-agreement 1.0` refuses any disagreement.

**What lowers it:**

- **Drift since the export window** — onboarding, price or signal changes between when the
  traffic was served and when the snapshot was taken.
- **Rows exported before B55** (2026-09-24) — they carry no `requested_model` or
  `max_tokens`, so a pinned request among them replays unpinned and prices at the default
  output-token estimate. The first real Spark run (before the fix) measured 0.0206
  agreement for exactly this reason: Qwen Code pins its model, and 190 of 194 rows were
  replayed unpinned to a different model.
- **Circuit-breaker state** — a request served while a circuit was open replays with no open
  circuits (§6).
- **A budget, a declared context window or an endpoint's state changed since the traffic was
  recorded** — a request rejected then can route now, and the reverse. This lowers *outcome*
  agreement.
- **A recorded `context_overflow` on an endpoint with no declared `context_window`** — replay
  cannot predict an overflow it has no limit for, so the row replays as routed (§6).

**The headline diff itself is never logged-vs-simulated.** Both `current` and `proposed` are
simulated arms sharing one signals snapshot, so drift and fidelity loss affect both sides
equally — that is the reason the two-arm design was chosen over comparing the `proposed` arm
against the raw export (`docs/research/feasible-route-bench.md` §15.2).

## 6. What is not simulated

From spec §2, plus what the build settled during P3a:

- **No live dry-run of an admin write.** The simulator adds one read-only endpoint
  (`GET /admin/config-snapshot`) and no other serving behaviour.
- **No explanation of why a proposal rejects a request** and **no shadow prices** were P3a's
  limits. The minimal conflicting core (P3b) now explains a rejection — see
  `docs/reference/conflicting-core.md`. Shadow prices (P3c-1, §8 below) now price what each
  individual constraint costs. Alternative scenarios are §9 (P3c-2). Uncertainty ranges are
  §10 (P3c-3): sampling variability only, and none of the limits in this section is inside
  them. Endpoint load is §11 (P3c-4): stated limits against replayed demand, with **no
  queueing, overload or rejection model**.
- **Two token counts, used for two things (B60, fixed 2026-10-06).** The router is given the
  row's `routing_tokens` — RouteForge's own count, the number the live router was given — so
  the per-request budget check and the cost term of the score see what they saw live. The
  routed call is priced, and the context-window check sized, at `prompt_tokens`, the
  backend's billed count. A row from before `routing_tokens` existed uses `prompt_tokens`
  for both, as before.
- **A row with no backend token count is an estimate.** A rejected row, one that failed
  before any response, or one an endpoint served but reported no usage for (a stream whose
  endpoint sent no usage block) has `prompt_tokens: 0` and no completion count. When an arm
  routes it, replay prices it at its `routing_tokens` and at the router's output estimate
  (its recorded `max_tokens`, else `default_estimated_output_tokens`) — the same pair the live
  per-request budget check priced it at. So a request a proposal rescues, or an already-`ok`
  row an endpoint reported no usage for, is costed at its admission estimate, which can
  overstate what it would really have cost. `rows.estimated_tokens` counts all of these rows
  and the report says so. A `backend_failed` row is routed in both arms and costed as if the
  call had succeeded; the failure itself is not replayed.
- **Rejected and failed rows recorded before B60 are excluded**, counted as
  `missing_routing_tokens`. They record no size, and replaying them as empty prompts is what
  made a raised budget show nothing to rescue (first observed live on 2026-10-06,
  `docs/research/feasible-route-bench.md` §18.4.1). **This changes the output for an export
  that contains such rows**: fewer rows are simulated than before, and totals move. To
  simulate rejected requests, use rows recorded after the upgrade (`--since`).
  `simulation_id`/`comparison_id` hash the `inputs` block, not the code, so the same id can
  now name two different outputs for an export recorded before `routing_tokens` existed —
  its unsized rejected/failed rows are excluded where an earlier build of this tool replayed
  them as routed.
- **No re-running PII detection.** Recorded `detected_entity_types` are replayed as-is, so a
  proposal cannot change `pii_detection`, a denylist, exempt patterns or recognizers.
- **No daily-spend or RPM admission simulation** — those are admission limits, not routing.
- **No circuit-breaker state.** Both arms run with no circuits open.
- **Only the request's first routing decision is simulated, plus predicted context-window
  overflow re-selection (below).** No confidence-escalation and no other failover is
  replayed; later hops beyond the overflow re-selection are never replayed.
- **Context-window overflow is PREDICTED, not replayed from what happened live.** Live, the
  declared `context_window` is never enforced ahead of time — overflow is the backend's own
  refusal, on its real tokenisation and its real limit. Replay has no backend to ask, so it
  predicts that refusal instead, from data the row and the snapshot already carry:
  1. `prompt_tokens` is the backend's own recorded count for the row — not an estimate. For
     a row no backend answered it is the row's `routing_tokens`, which is RouteForge's count
     and not the backend's, so a request the backend refused by a small margin can replay as
     fitting.
  2. `output_tokens` is the row's recorded `max_tokens`, or the deployment's
     `default_estimated_output_tokens` when none was recorded. So an uncapped request that fit
     live can still overflow in replay, when the default estimate overstates what the client
     actually asked for (and the endpoint actually served).
  3. A pick is excluded when `prompt_tokens + output_tokens` exceeds its DECLARED
     `context_window`, and the router selects again, at most `routing.max_failover_attempts`
     extra times — the live bound. If every reachable candidate overflows, the row is rejected
     with code `context_window` (live: a 400). An undeclared window (`null`) never overflows.
  4. **The live streaming path has no re-selection at all.** `Gateway.run_stream` makes exactly
     one `Router.select` call and streams from whatever it returns; a request that overflows
     live on that path is a 400 with no retry. Replay re-selects for every row regardless of
     whether it was served streamed or not, so a streamed row's predicted overflow handling is
     strictly more forgiving than what the stream path actually does.

  The endpoint served after the hops is the one costed, and `overflow_hops` counts the hops. So
  a proposed `context_window` change now has a visible effect. And because this is a
  prediction, not a replay, it can be WRONG: an `ok` row whose record proves its recorded first
  pick served it alone (`chosen == attempts[0]`, one recorded attempt) but whose replay excludes
  that same pick as an overflow is a sign the endpoint's declared window understates its real
  one (or no `max_tokens` was recorded and point 2 above overstates the request) — §5 reports
  that subset separately as `overflow_contradicted`.
- **A recorded `context_overflow` row is reproduced only when the endpoint declares a
  window.** The row now carries its size (`routing_tokens`), so a declared `context_window`
  it exceeds rejects it in replay too. An undeclared window never overflows, so the row
  replays as routed and is counted in `recorded_rejected_but_routed` (§5).
- **Cached tokens are an assumption, not a record.** Outcome rows do not record cached
  prompt tokens. Both arms price none as cached unless a proposal sets `workload.cached_share`.
- **Pinned-model rejections are not in the input.** A pin the app may not use is a
  `model_not_found` 422 that writes no outcome row (`docs/reference/routing-outcomes.md`
  §3.1), so there is nothing to replay for it. Pins that *were* served are replayed (§1.1).
- **Rows exported before B55 replay unpinned** at the default output-token estimate (§1.1,
  §5). A deployed database needs the two `ALTER`s in `docs/reference/routing-outcomes.md` §1
  before new rows carry the fields.

## 7. Security: the snapshot is content-free and secret-free

`GET /admin/config-snapshot` (`app/apps/snapshot.py::build_config_snapshot`) is built so
that nothing it returns can leak a customer secret or request content — every field is
listed explicitly in the builder; nothing is dumped wholesale except the `routing`,
`pricing` and `policy` config sections, which hold no secrets by construction.
`settings.serving_policy` carries exactly four named posture values
(`app/apps/snapshot.py::SERVING_POLICY_FIELDS`) — a detector name, a default mode and two
booleans, none of them secrets.

**Excluded, on purpose:** endpoint URLs, `api_key_env`, `serving.observability` (Langfuse
host/keys), `pii_exempt_patterns` (operator regexes), custom recognizer definitions, and
every per-app credential column. `providers.<name>` carries only `enabled`, `region`,
`prompt_retention`, `accepts_raw_sensitive` — never `api_key_env` or an endpoint URL.
`apps[].policy` is every `AppPolicyView` field **except** `pii_exempt_patterns`
(`app/apps/snapshot.py::_EXCLUDED_POLICY_FIELDS`). `apps[].sensitivity_rules` carries
entity **types**, never patterns or values.

Tested by `tests/test_config_snapshot.py::test_snapshot_has_no_forbidden_keys_or_secret_values`,
which asserts none of the forbidden keys appear anywhere in the document and that a seeded
secret value (an API key env value, a Langfuse secret) appears nowhere in the serialised
JSON; `test_snapshot_requires_admin` pins the `401` without the session cookie.

Because the export is content-free too (§2 of `docs/reference/routing-outcomes.md`), the
three files a simulation run needs — export, snapshot, proposal — can be moved off the
deployment (to a laptop, a CI job) without carrying request content or credentials with
them.

## 8. Shadow prices (P3c-1)

Design: `docs/superpowers/specs/2026-09-26-shadow-prices-design.md`. Research notes (the
model, scoped attribution, the eligible-superset invariant and its counterexample): `docs/research/feasible-route-bench.md`
§17. Code: `app/eval/shadow.py` (`Relaxation`, `enumerate_relaxations`, `shadow_prices`,
`render_shadow_section`), wired into `scripts/policy_simulate.py` by `--shadow-prices`.

### 8.1 What it answers

§§1–7 answer "what would this specific proposal change?" Shadow prices answer a different
question over the same recorded traffic: **for each constraint an app actually has set,
what would relaxing just that one constraint change?** Every requirement an app's policy
currently enforces is priced, one at a time, by replaying the same rows through the real
eligibility (`feasible_set`) and routing (`Router`) code with only that one requirement
loosened — cost, rejections avoided, quality, latency and on-prem share, same as the §4
metrics. Constraints tied to a security requirement are priced too, but reported
separately, as the **cost of a protection** — never as a recommendation to relax anything,
and nothing is ever applied (handoff §37: "Security constraints are never automatically
relaxed").

`--proposal` becomes optional with `--shadow-prices`: without one, the constraints an app
sets **today** are priced against the snapshot as exported. With one, the baseline is the
snapshot **with the proposal already applied**, so an operator can price what a *proposed*
policy would still be costing before adopting it.

### 8.2 The relaxation table

One relaxation is one requirement, relaxed for one scope (an app, or one onboarded endpoint
for `endpoint_app_allowlist`). The requirement ids and the security flag are the same ones
P3b's minimal conflicting core uses (`app/policy/core.py::REQUIREMENT_ORDER`,
`SECURITY_IDS` — never restated here).

| requirement id | scope | relaxed to | rows re-simulated |
|---|---|---|---|
| `residency` (security) | app | the app's `residency: any` **and** `escalate_residency_on_detection: false` | that app's |
| `allowed_providers` (security) | app | `null` | that app's |
| `allowed_regions` (security) | app | `null` | that app's |
| `require_no_retention` (security) | app | `false` | that app's |
| `raw_sensitive` (security) | app | every `accepts_raw_sensitive: false` declaration flipped to `true` — covers onboarded rows, `settings.providers.*` and `settings.routing.model_pool[*]` | that app's |
| `allowed_tiers` | app | `null` | that app's |
| `allowed_models` | app | `null` | that app's |
| `endpoint_app_allowlist` | endpoint | that onboarded row's `allowed_app_ids: null` | **all** rows |
| `requested_model` | app | that app's rows replayed with `requested_model: null` | that app's |
| `max_cost_usd_per_request` | app | `1e12` | that app's |
| `risk_floor` (security) | app | every `settings.routing.risk_floors` entry set to `cheap` | that app's |

A relaxation is only a **candidate** when the constraint is actually set for that app (or,
for `endpoint_app_allowlist`, when the endpoint has a non-null `allowed_app_ids`) —
`max_cost_usd_per_request` is the one exception, always a candidate, because the deployment
default always applies. `raw_sensitive` counts a declaration as `false` the way serving
enforces it: a bool `false`, or any string that is `false` once stripped and lowercased
(`"False"`, `" FALSE "`); the flip keeps the field's shape (bool `true`, or the string
`"true"`). `risk_floor` is a candidate when a row's risk floor is above `cheap`, using the
risk class replay derives from the row's `detected_entity_types` and the app's resolved
sensitivity rules in the baseline doc — never the recorded `risk_class`, so a `--proposal`
rule change is respected. Candidates are enumerated deterministically: app id, then
`REQUIREMENT_ORDER`, then endpoint slug. `--max-relaxations N` (default 50) caps how many
are evaluated; when the cap cuts the list short, `skipped_by_cap` in the block and the
report footer say how many.

A relaxation whose rows all route identically under both arms is **non-binding** — counted
in `non_binding`, never listed in either table. A relaxation is **binding** when any of
`route_changed`, `rejections_avoided` or `newly_rejected` is non-zero.

### 8.3 The scoping rule

Every relaxation except `endpoint_app_allowlist` re-simulates only the scoped app's rows,
**even the two that edit deployment-wide data**: `raw_sensitive`'s endpoint/provider/
model-pool declarations are shared across every app, and `risk_floor`'s `risk_floors` map
is global. For those two, the *document* edit is still global, but only the app being
priced has its rows re-simulated — every other app's rows are left out of scope by
construction, so the price stays attributable to the one app it was priced for, never
smeared across every app the shared data happens to also affect. `endpoint_app_allowlist`
is the one exception: it is scoped to an **endpoint**, not an app, and re-simulates every
row, because any app's requests could route to that endpoint.

### 8.4 Metrics

Per relaxation, the baseline arm is compared with the relaxed arm over the re-simulated
rows, using the same per-row `ArmResult` and bucket-stat helpers §4 uses:
`rows_in_scope`, `route_changed`, `rejections_avoided` (the `newly_served` count),
`newly_rejected` (rows routed at baseline and rejected when relaxed — see the invariant
below), `newly_rejected_reasons` (a sorted `{reject code: rows}` map over those rows, from
the relaxed arm's reject codes, e.g. `{"over_budget": 1}`), `cost_usd` (baseline/relaxed/delta over each arm's routed rows), `cost_usd_both_routed`
(same shape, only rows routed in both arms), `cost_saved_per_day`, `quality_mean_delta`,
`latency_p50_delta`, `onprem_share` (baseline/relaxed), `affected_apps` and the top 5
`moves`.

**Why `cost_saved_per_day` is like-for-like.** `cost_usd` sums each arm over the rows *that
arm* routed — so a row the relaxation newly serves (a `rejections_avoided` row) simply adds
to the relaxed side's sum, the same way a proposal's newly rejected row silently drops out
of §4's `cost_usd`. Read on its own, that makes relaxing a constraint that serves more
traffic look like it *costs* more, which is true in the sense that more requests are now
served, but says nothing about whether the constraint was expensive **for the traffic
already being served**. `cost_saved_per_day` answers the narrower, actionable question:
`-cost_usd_both_routed.delta / window_days`, over only the rows routed under **both** arms.
A row the relaxation newly serves never enters this number — its cost is in `cost_usd`,
not here. `cost_usd_both_routed` is reported alongside it so both readings are visible.
`window_days` is the **export window** — the span of every simulated row, not just this
relaxation's scoped rows — so every relaxation's per-day figure is comparable against every
other's; it is `null` (and so is `cost_saved_per_day`) when that window is under a day.

**The eligible-superset invariant.** Every relaxation can only widen what an app may use:
for every re-simulated row, the relaxed eligible set (`feasible_set`) is a superset of the
baseline eligible set. That is the only hard invariant. A test enforces it (Hypothesis,
`tests/eval/test_shadow.py::test_eligible_superset_for_random_policies`); a breach is
`ShadowInvariantError`, which is never a reportable finding — the CLI exits `2` naming it
(§3 above), because it means a bug in the relaxation logic, not a real result.

**A relaxation can newly reject a row — reported, not raised.** The *router* is not
monotone in the feasible set, so "a row routed at baseline is never rejected when relaxed"
does **not** hold. Rule 8 clamps a high-risk row's target tier to the best *allowed* tier,
which can sit below the risk floor. When a relaxation makes the floor tier eligible, the
target lifts to that tier; if it is over the app's `max_cost_usd_per_request`, the budget
fallback will not drop below the floor, and the row is rejected `over_budget`. The hospital
persona is the canonical case: `onprem_only`, a high-risk SSN, on-prem serving only cheap
and mid, a cloud frontier enabled, and a tight budget. At baseline the row clamps to the
on-prem mid tier and routes; relaxing `residency` makes the cloud frontier floor eligible,
it is over budget, and the row is rejected. Each entry therefore reports `newly_rejected`
and `newly_rejected_reasons`, and such a relaxation is binding. (Amended 2026-09-26: the
original design claimed the routed→rejected half as an invariant; it was wrong. See
`docs/research/feasible-route-bench.md` §17.3.)

The same non-monotonicity applies to predicted context-window overflow (§6, added P3c-2): a
relaxation can newly reject a row with code `context_window` when the wider eligible set
reaches a candidate with a smaller declared window that the (scaled) request then overflows,
and conversely a baseline `context_window` rejection can be AVOIDED by a relaxation that
reaches a candidate with a larger or undeclared window — `requested_model`'s relaxation
(unpinning) is the clearest example, since the pinned candidate may be the only one with too
small a window.

### 8.5 The two tables and the non-recommendation rule

`report.md`'s "Shadow prices" section opens with this caveat, verbatim:

> **What each constraint costs.** Each row relaxes ONE constraint for one app (or endpoint) and
> replays the same recorded traffic. Nothing is applied. Security constraints are shown as the
> cost of a protection — they are never a recommendation to relax them.

Then two ranked tables over the same columns — requirement, scope, rows, route changed,
rejections avoided, newly rejected, cost saved/day (falling back to `cost_usd_both_routed`'s delta when the
export window is under a day), Δquality, Δp50 ms, on-prem share (baseline→relaxed):

- **Operational constraints** — every binding non-security requirement.
- **Cost of protection** — every binding security requirement (`SECURITY_IDS`), with one
  extra column on every row, always the literal text `price of a protection — not a
  recommendation`. This label is not cosmetic: it is the one place in the report a security
  finding could be misread as "relax this", and the row says otherwise every time.

Either table reads "None." when it has no binding entries. Both are ranked by the same key:
`(-(cost_saved_per_day if not None else -cost_usd_both_routed.delta), -rejections_avoided,
requirement, scope_key)` — the biggest like-for-like saving first (falling back to the raw
both-routed delta when the export window is under a day and there is no per-day figure),
then the most rejections avoided, then a deterministic tie-break on requirement id and
scope. Under the tables sits one sentence, verbatim: "Relaxing a constraint can newly
reject a row: a wider allowed set can lift the target to the risk floor, and the budget
fallback never goes below the floor." A footer then gives the non-binding count and the
number skipped by the cap: `Non-binding: N · skipped by cap: M`.

**`simulation.json`'s `shadow_prices` block** carries the same data as a nested object:
`baseline` (`"snapshot"` or `"snapshot+proposal"`), `evaluated`, `skipped_by_cap`,
`non_binding`, and `constraints` — the list of binding entries, each with `requirement`,
`scope` (`{"app": ...}` or `{"endpoint": ...}`), `security`, `relaxed_to`, and the §8.4
metrics. It is deterministic the same way the rest of `simulation.json` is: sorted keys,
floats rounded to 6 places.

**With no `--proposal`**, `simulation.json` is just `{"schema": "routeforge.simulation/v1",
"inputs": {...}, "shadow_prices": block}` — the `inputs` block is the same shape as §4.1's,
with `proposal_sha256` and `proposal_title` both `null` (there is no proposal to hash or
title), and there is no `totals`/`by_app`/`by_workload`/`baseline`/`moves`/`new_rejections`
section, because there is no baseline-vs-proposal comparison to report. `report.md` is just
the §4.2 caveat followed by the "Shadow prices" section. **With `--proposal`**, the ordinary
P3a output is unchanged in full, `shadow_prices` is added as one more top-level key, and the
"Shadow prices" section is appended to the end of the report.

**Shadow-prices-only hides the new exclusion and the estimated-row count (B63 backlog
item).** `shadow_prices` selects rows the same way `simulate()` does (`select_rows`), so a
row recorded before `routing_tokens` existed that is not `ok` and carries no size is excluded
here too, counted as `missing_routing_tokens` — but with no `--proposal` there is no `rows`
block in `simulation.json` or `report.md`, so this mode prints no count of how many rows were
excluded or sized from an estimate. A `rejections_avoided` row's share of a constraint's
`cost_usd` is still an admission estimate (its routing tokens plus the router's output-token
estimate, §6), the same estimate `rows.estimated_tokens` would count if this mode reported
it; `cost_saved_per_day` is unaffected, since it is computed only over rows routed in both
arms. To see the excluded and estimated counts, run with `--proposal` alongside
`--shadow-prices` — an empty proposal (`{"schema": "routeforge.policy-proposal/v1"}`)
predicts no change and still produces the `rows` block.

### 8.6 Flags

See the `--shadow-prices` and `--max-relaxations` rows in §3's flag table and exit-code
table above.

### 8.7 Limits

From spec §2, deliberate non-goals for this sub-project:

- **One constraint at a time.** Every relaxation is evaluated independently, against the
  same baseline. It says nothing about relaxing two constraints together — that interaction
  (joint relaxations) is P3c-2, scenarios.
- **A point estimate unless asked.** Every figure is a point estimate from replaying the
  recorded rows once. `--uncertainty` adds a range for each binding relaxation's cost and
  counts (§10); it is sampling variability only, not a sensitivity analysis.
- **No capacity effects.** A relaxation that would route far more traffic onto one endpoint
  is priced as if that endpoint has unlimited headroom. The capacity section (§11, P3c-4) is
  produced for a proposal or a scenario, never for a relaxation.
- Every §6 non-goal above still applies to shadow prices too: no live dry-run, no
  re-running PII detection, no daily-spend/RPM admission, no circuit-breaker state, no
  confidence-escalation/failover replay.

## 9. Scenario comparison (P3c-2)

Design: `docs/superpowers/specs/2026-10-05-p3c2-scenarios-design.md`. Research notes:
`docs/research/feasible-route-bench.md` §18. Code: `app/eval/scenarios.py`
(`load_scenarios`, `apply_scenarios`, `compare`, `comparison_doc`, `render_comparison`),
wired into `scripts/policy_simulate.py` by `--scenarios`.

### 9.1 What it answers

An administrator has several candidate changes and wants their predicted effects next to
each other, over the same recorded traffic and the same baseline. Each scenario is replayed
against one shared `current` arm.

**RouteForge never picks, ranks or recommends a scenario.** Scenarios are listed in file
order, no output field ranks or scores them, and nothing is applied.

### 9.2 The scenarios file

```json
{
  "schema": "routeforge.policy-scenarios/v1",
  "title": "Healthcare on-prem options",
  "scenarios": [
    {"name": "all-onprem",     "proposal": {"apps": {"claims": {"residency": "onprem_only"}}}},
    {"name": "raw-pii-onprem", "proposal": {"apps": {"claims": {"escalate_residency_on_detection": true}}}},
    {"name": "smaller-prompts", "proposal": {"workload": {"apps": {"claims": {"token_scale": 0.6}}}}}
  ]
}
```

- Top-level keys: `schema`, `title` (optional string), `scenarios`. Any other key is a load
  error naming it.
- `scenarios`: 1 to 20 objects, each with exactly `name` and `proposal`.
- `name`: 1 to 64 characters of `[A-Za-z0-9._+-]`, unique within the file.
- `proposal`: a v1 proposal body (§2). `schema` is optional inside it.
- Loading is all-or-nothing. Every scenario is validated against the snapshot before any
  replay starts. An error is named `scenarios[<i>](<name>).proposal.<dotted path>` and the
  CLI exits `2` with nothing written.
- An empty body `{}` is a valid scenario and predicts zero change.

### 9.3 Running it

```bash
uv run python scripts/policy_simulate.py \
  --export outcomes.ndjson --snapshot snapshot.json --scenarios scenarios.json \
  --out results/cmp [--since 2026-09-25T01:25:00Z] [--min-agreement 0.9] \
  [--fail-on-new-rejections]
```

`--out` receives `comparison.json` and `report.md`. With `--fail-on-new-rejections` the CLI
exits `3` if any scenario newly rejects a row (the files are still written).

### 9.4 `comparison.json`

Schema `routeforge.scenario-comparison/v1`. Deterministic: sorted keys, floats rounded to 6
places.

| Field | Contents |
|---|---|
| `comparison_id` | first 16 hex characters of the sha256 over `inputs` |
| `inputs` | `export_sha256`, `snapshot_sha256`, `scenarios_sha256`, `title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy`, `since`, `window` |
| `rows` | `simulated`, `estimated_tokens` and `excluded` — the same for every scenario |
| `baseline` | the fidelity block of §5, computed once from the shared `current` arm |
| `scenarios` | file order. Each: `name`, `edits`, `assumptions`, and `summary` — the §4.1 body (`totals`, `by_app`, `by_workload`, `moves`, `new_rejections`, `affected_apps`, `sources`, `per_day`) |
| `table` | file order. One row per scenario: `name`, `cost_per_day_delta_usd`, `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`, `overflow_hops`, `affected_apps`. With a stated capacity also `peak_utilisation`, `peak_mean_window`, `peak_buckets_over_share`, `peak_endpoint`, `peak_limit`, `peak_concurrency_not_computed` (§11.8) |
| `capacity` | only with a stated capacity: `scenarios`, file order, each scenario's capacity block (§11.8) |

**The table's cost columns are like-for-like.** `cost_delta_usd` is
`totals.cost_usd_both_routed.delta` — only rows routed in both arms — and
`cost_per_day_delta_usd` is that divided by the export window in days (`null` under one day).
A scenario that newly rejects rows therefore never looks cheaper because of it; read
`newly_rejected` for the service loss. The all-routed delta is in each scenario's
`summary.totals.cost_usd`. `overflow_hops` is the scenario arm's total.

A scenarios file with one scenario gives exactly the per-scenario numbers that `--proposal`
gives for the same body.

### 9.5 `report.md`

In order: the §4.2 caveat; the title; a **baseline agreement** banner (model agreement and
outcome agreement, each with its overall rate, a ⚠ warning under 90% and the rate per app;
a note for every recorded outcome replay does not reproduce and for rows sized from an
estimate; and the `--since` cut when one was given); **one
comparison table** in file order, followed by the sentence "Listed in file order. RouteForge
does not rank or recommend scenarios." and the like-for-like note; an **Assumptions** line
for each scenario that sets `workload` or `latency_ms`; a short section per scenario
(metrics, moves, new rejections by app, code and conflicting core, affected apps); and the
excluded rows.

### 9.6 Limits

- Endpoint load is opt-in (a stated capacity, §11) and has no queueing, overload or
  rejection model. Ranges are opt-in (`--uncertainty`, §10) and are sampling variability
  only.
- `--scenarios` cannot be combined with `--shadow-prices`.
- `token_scale` on a saturated row and every `cached_share` are assumptions; the report
  labels both.
- Every §6 limit applies to every scenario.

## 10. Uncertainty ranges (P3c-3)

Design: `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md`. Research notes:
`docs/research/feasible-route-bench.md` §20. Code: `app/eval/uncertainty.py`
(`UncertaintyPlan`, `run_bootstrap`, `results_block`, `load_posteriors`,
`render_uncertainty_section`), wired into `app/eval/scenarios.py`, `app/eval/shadow.py` and
`scripts/policy_simulate.py`.

### 10.1 What it answers

Every figure in §4, §8 and §9 is a point estimate from one replay of one export. "Cost −39%"
reads the same on 24 rows and on 2 million. `--uncertainty` puts a range beside each headline
figure, so a reader can see how much the figure would move if the same traffic had produced a
different sample of the same size.

It is **off by default**. Without the flag every output file is byte-identical to what it was
before this feature (pinned by `tests/eval/test_uncertainty_golden.py`). With it, the
documents gain keys and no existing key or value changes.

### 10.2 What the range is — and is not

Every report that shows a range prints this, verbatim:

> **Ranges show sampling variability only.** Each range is a percentile interval from
> resampling the recorded rows with replacement, within each application (applications with
> fewer than `--min-rows` rows are resampled together for the totals), under this replay
> model. It does not include replay model error (first-decision replay, frozen signals,
> predicted overflow), the error of the admission estimate for rows with no backend token
> count, inputs that were not recorded, or future traffic shift. Replay gives each endpoint
> one latency, so a latency range reflects only which endpoints the resampled rows used. A
> narrow range on a biased replay is still biased.

The block's `not_covered` list carries the same five exclusions as ids:
`replay_model_error`, `admission_estimate_error`, `unrecorded_inputs`, `traffic_shift`,
`per_request_latency`. Check the baseline agreements (§5) for bias; the range says nothing
about it.

One of the five can be counted. A row with no backend token count — rejected, failed before
a response, or served by a backend that reported no usage — is sized and priced from
RouteForge's own token count (§6), in every resample alike. Each bucket carries
`estimated_token_rows`, and the report prints the count beside the caveat:
"13 of 160 simulated rows have no backend token count (`estimated_token_rows`): …".

### 10.3 Running it

```bash
uv run python scripts/policy_simulate.py \
  --export outcomes.ndjson --snapshot snapshot.json --proposal proposal.json \
  --out results/sim --uncertainty \
  [--bootstrap 1000] [--interval 0.90] [--seed 0] [--min-rows 30] \
  [--quality-posteriors recommendation.json ...]
```

The same flags work with `--scenarios` and with `--shadow-prices`.

| Flag | Default | Valid | Meaning |
|---|---|---|---|
| `--uncertainty` | off | | compute and write ranges |
| `--bootstrap` | `1000` | integer, 1 to 20000 | bootstrap resamples |
| `--interval` | `0.90` | strictly between 0 and 1 | level of the interval |
| `--seed` | `0` | integer ≥ 0 | seed; the same seed gives the same ranges |
| `--min-rows` | `30` | integer ≥ 1 | a bucket with fewer rows gets "insufficient data" (§10.6); applications with fewer rows are resampled together for the totals (§10.4) |
| `--quality-posteriors` | none | repeatable | a P2b-3 `recommendation.json` that was applied (§10.5) |

A tuning flag given without `--uncertainty`, a value out of range, or a posterior file that
does not load is exit `2`; nothing is written. `--min-agreement` is checked before any
resampling starts, in every mode.

Two lines can appear on stderr; neither changes the exit code:

- **Before each bootstrap phase**, what it will cost:
  `policy_simulate: uncertainty: 1000 resamples x 111 rows x 1 pass: under 1 s (a rough
  estimate …)`. One pass per proposal, one per scenario, and at most one per shadow-price
  relaxation (only a binding relaxation is resampled). §10.9 says what the estimate is made
  from.
- **A warning when `--bootstrap` is too small for `--interval`.** With fewer than
  `2 / (1 − P)` resamples — 20 at the default level — the interval leaves no value out on
  either side, so every range is the smallest and the largest resample value. The run still
  completes; the block's head carries `"interval_is_min_max": true` and the report says so.

### 10.4 The method

- **Rows are resampled, never re-simulated.** Each row is replayed once, as always. A
  resample draws row positions with replacement and recomputes the figures.
- **Within each application — in strata.** An app with at least `--min-rows` rows is its own
  stratum: it is resampled to exactly its own row count from its own rows, so its figures are
  defined in every resample, and that one draw feeds both its bucket and the totals. All rows
  of the apps with **fewer** than `--min-rows` rows form **one pooled stratum**, resampled
  together; it feeds the totals only (those apps have no range of their own, §10.6).
- **Why small apps are pooled.** A stratum of `n` rows resampled on its own reproduces
  `(n − 1)/n` of its variance, and a one-row stratum none. Drawn one app at a time, an export
  of many small apps gets a totals range that is too narrow — width zero when every app has
  one row (§10.10 has the measurement). Pooling removes most of that. What remains is
  disclosed in `totals_strata` and in one report line, and **is not corrected**:

  ```json
  "totals_strata": {"own": 2, "pooled_apps": 4, "pooled_rows": 45, "smallest": 30,
                    "variance_factor": 0.975}
  ```

  `own` apps are their own stratum; `pooled_apps` apps (`pooled_rows` rows) are resampled
  together; `smallest` is the smallest stratum's row count. `variance_factor = (n − S)/n` for
  `n` rows in `S` strata is the share of the totals' variance that resampling within strata
  keeps **if every stratum has the same variance** — an analytic figure, not a measurement.
  With `--min-rows 1` nothing is pooled; 240 one-row apps then give `variance_factor: 0.0`
  and totals ranges of width zero, and the line says so.
- **Paired.** The rows drawn for an app in resample `b` depend only on the seed, `b`, the app
  and its row count; the pooled draw depends only on the seed, `b` and the pooled row count.
  Both arms, every scenario and every shadow-price relaxation therefore see the same resampled
  rows, and a delta's range is the range of the difference over the same rows. A proposal
  that changes nothing has every delta range at exactly `[0, 0]`. A relaxation scoped to one
  app equals that app's bucket under the equivalent proposal; the endpoint-scoped relaxation,
  which re-simulates every row, equals that proposal's totals.
- **Percentile interval.** With `N` resamples and level `P`, `lo` and `hi` are the
  `floor((1 − P)/2 · N) + 1`-th smallest and largest values. Fewer than `2 / (1 − P)`
  resamples make that the minimum and the maximum (§10.3).

It assumes the rows of one stratum are exchangeable and independent over the export window.
Neither is true of real traffic: the calls of one agent loop are related to each other, and
traffic shifts within a window. Both make the true uncertainty larger than the range shown.
There is no day-block resampling. It also holds the set of applications fixed: nothing here
covers variation **between** applications (§10.10).

### 10.5 Quality labels (`--quality-posteriors`)

A quality that came from P2b-3 calibration (`docs/reference/quality-calibration.md`) is the
mean of a Beta posterior. The snapshot carries the mean and the `set_id`, not the Beta's
parameters. They are in the `recommendation.json` the set was applied from, so pass that
file:

```bash
--uncertainty --quality-posteriors results/quality/recommendation.json
```

- The file must match its own `set_id` (a hash of its content — the check the admin API ran
  at apply time). An edited file is refused.
- In each resample, each calibrated `(model, workload)` unit of that set gets one quality
  drawn from its posterior, used for every row that resolves to that unit, in both arms.
- A published or tier-default quality has no stated uncertainty and gets none: it stays a
  point.
- The report says how much is covered: "Quality uncertainty covers k of n routed rows …; the
  rest use a point prior." `uncertainty.quality_posterior` has the same counts per arm
  (`routed`, `calibrated`, `posterior`).
- A file for a set the snapshot does not hold is ignored with a warning on stderr.

### 10.6 Insufficient data, zero counts, flags on a leaf

- **`insufficient data (n = k)`.** A bucket with fewer than `--min-rows` rows has no range:
  the JSON carries `{"insufficient_data": true, "n": k}`. That applies to an app, to the
  totals, to a scenario and to a shadow-price relaxation's rows in scope.
- **`insufficient data (n = k routed)`.** Latency, `quality_mean` and `onprem_share` are
  figures of the rows an arm **routed**, not of the bucket's rows. Every bucket carries
  `"routed": {"current": k, "proposed": k}`; when an arm routed fewer than `--min-rows` rows,
  those leaves of that arm — and the deltas they enter, on the smaller of the two counts —
  carry `"lo": null, "hi": null, "insufficient_data": true, "n": k`. A 100-row bucket whose
  proposed arm routes 10 rows gets no range for a mean of 10. Cost sums, the three counts and
  their shares are over every row and keep their ranges.
- **A zero count is not "no risk".** If no row is newly rejected, no resample has one: the
  range is `[0, 0]` for 24 rows and for 2 million. So the share's leaf also carries
  `zero_bound = 1 − (1 − P)^(1/n)` — the largest rate at which `n` independent rows would
  still show none with probability `1 − P` — and the report prints it in a sentence. It is
  about `2.3 / n` at the default level (9.2% for 24 rows). The bound is **one-sided**, and it
  bounds the rate **under this replay**, not the live rate. It is also on a relaxation's
  `newly_rejected` leaf in `shadow_prices.uncertainty` (`n` = the rows in scope) and on an
  `onprem_share` of exactly 0 (`n` = that arm's routed rows). Under the scenario comparison
  table and the shadow "Ranges" table, where a count prints as `0 [0, 0]`, the report says:
  "A count of 0 shows `[0, 0]` at any sample size; that is not zero risk."
- **A share of exactly 1 is not certainty.** The mirror case: every row of a bucket newly
  rejected, or every routed row on-prem, prints `1 to 1` at any sample size. Such a leaf
  carries `one_bound = (1 − P)^(1/n)`, the smallest rate at which `n` independent rows would
  all show the event with probability `1 − P`, and the report says so under that bucket's
  table. It is a statement about sampling a rate. It says **nothing about a share a policy
  fixes by construction**: an `onprem_only` application's on-prem share is 1 because a cloud
  provider is unreachable for it, whatever rows were sampled.
- **`point_outside: true`.** A percentile interval does not promise to contain the point
  estimate. When it does not, the leaf says so and the report marks the row — `(point outside
  range)` in a range table, `[lo, hi]*` with a footnote in the comparison table and the shadow
  "Ranges" table. Nothing is widened.
- **`undefined_replicates: k`.** A figure that does not exist in some resample (an arm with
  no routed row there) has no range: `lo` and `hi` are null.

### 10.7 Output

The added keys, and the only ones: `uncertainty`, `inputs.uncertainty`, and — with shadow
prices — `shadow_prices.uncertainty`.

**A leaf** is `{"point", "lo", "hi"}`, plus the keys of §10.6 when they apply
(`insufficient_data` with `n`, `zero_bound`, `one_bound`, `point_outside`,
`undefined_replicates`). `point` is the figure the document already carries at the same path.

**A bucket** is `{"n", "routed": {"current", "proposed"}, "estimated_token_rows", <leaves>}`,
or `{"insufficient_data": true, "n"}`.

**`inputs.uncertainty`** is `{replicates, interval, seed, min_rows,
quality_posteriors_sha256}`, so `simulation_id` / `comparison_id` differ between two runs that
differ in any of them, and between a run with the flag and one without.

**`simulation.json` → `uncertainty`:**

| Field | Contents |
|---|---|
| `method`, `strata` | `"stratified_paired_row_bootstrap"`, `"application_id"` |
| `replicates`, `interval`, `seed`, `min_rows` | the plan |
| `interval_is_min_max` | `true` when `replicates` is too few for `interval` to leave any value out, so every range is a minimum and a maximum (§10.3); otherwise `false` |
| `quality_posterior_sets` | the `set_id` of each posterior file |
| `not_covered` | the five ids of §10.2 |
| `totals`, `by_app.<id>` | a bucket: `n`, `routed` (each arm's routed row count), `estimated_token_rows` (§10.2) and one leaf per figure — `cost_usd` and `cost_usd_both_routed` (`current`, `proposed`, `delta`), `route_changed`, `newly_rejected`, `newly_served` and each one's `_share`, `latency_ms.p50`/`.p95` (`current`, `proposed`, `delta`), `quality_mean` (same), `onprem_share` (`current`, `proposed`) — or `{insufficient_data, n}` |
| `per_day` | `cost_usd` (`current`, `proposed`, `delta`) and `cost_usd_both_routed.delta`, each a leaf; `null` when the window is under a day |
| `totals_strata` | `own`, `pooled_apps`, `pooled_rows`, `smallest`, `variance_factor` (§10.4); `null` when nothing was drawn |
| `quality_posterior` | per arm: `routed`, `calibrated`, `posterior` (§10.5) |
| `points_outside_interval` | how many leaves carry `point_outside` |

With `--shadow-prices` and no `--proposal` the block holds only its head (`method` through
`not_covered`). `by_workload` and `overflow_hops` have no range.

**`shadow_prices.uncertainty`** is `{interval, constraints, points_outside_interval}`.
`constraints` is parallel to `shadow_prices.constraints` (same order); each entry has
`requirement`, `scope`, `security`, `n`, `totals_strata` (the strata its rows were resampled
in: one application's rows are one stratum; the endpoint-scoped relaxation uses the export's
strata, small applications pooled) and leaves for `route_changed`, `rejections_avoided`,
`newly_rejected` (with `zero_bound` when it is 0), `cost_usd_both_routed_delta` and
`cost_saved_per_day` (`null` under a one-day window) — or `insufficient_data`.
`points_outside_interval` counts the leaves that carry `point_outside`. A security
constraint's entry is still the **price of a protection — not a recommendation**; a range on
it changes how precisely the price is known, not what it is the price of.

**`comparison.json` → `uncertainty`** has the same head and `scenarios`, in file order: each
entry is `name` plus the `simulation.json` fields above for that scenario, plus `table` — the
leaves behind that scenario's comparison-table row, under the table's own column keys.

### 10.8 Reports

- **Single proposal.** The report is unchanged, followed by an **Uncertainty ranges** section:
  the caveat, one sentence on the method, the count of rows with no backend token count, the
  totals strata (with "ranges are not corrected for it"), the quality coverage, then a
  `figure | point | range` table for the totals, the per-day figures and each app.
- **Scenario comparison.** In the comparison table each figure is followed by its range,
  `-0.02895 [-0.034024, -0.023268]`; under the table, the zero-count sentence and, when a
  cell is marked `*`, its footnote. An **Uncertainty ranges** section follows, and each
  scenario's section gains a **Ranges** table and a line counting the apps with insufficient
  data (the first ten are named; all are in `comparison.json`). The table stays in file
  order. A range is not a ranking: overlapping ranges do not make two scenarios equal, and
  separate ranges do not make one of them the one to choose.
- **Shadow prices.** A **Ranges** table follows the two existing tables, then the zero-count
  sentence, the `*` footnote when one applies, and a sentence on strata. A security
  constraint's row keeps its `price of a protection — not a recommendation` label there too.

### 10.9 Cost

Pure Python, no `numpy`. The cost of one pass is

```
resamples × (rows × c_row  +  strata × c_stratum)
```

so it is linear in resamples × rows **and has a term per application**: every application
that is its own stratum (at or above `--min-rows`) costs a freshly seeded generator and a set
of figures per resample. The small applications, pooled, are one stratum however many they
are. A proposal is one pass, each scenario one, each binding shadow-price relaxation one over
its rows in scope. Memory does not grow with the number of applications.

Before each bootstrap phase the CLI prints an estimate on stderr, computed from two unit
costs (`c_row` 0.4 µs, `c_stratum` 30 µs — `uncertainty.ROW_SECONDS`, `STRATUM_SECONDS`).
They were fitted to the runs below on one laptop core and are **rough**: another machine can
be several times faster or slower, and `c_row` varies with how many of an application's four
cost columns differ.

Measured on synthetic rows built in memory (Python 3.12.3, one core of an Intel Core Ultra 7
165U, other processes running — load average about 3 of 14 cores; `results_block`, including
the one-off setup; peak RSS is the growth of `ru_maxrss` across the call):

| shape (apps × rows) | resamples | time | peak RSS growth |
|---|---|---|---|
| 1,000 × 40 | 1000 | 38.4 s | 20 MB |
| 10,000 × 3 | 1000 | 10.1 s | 13 MB |
| 3 × 33,334 | 20 | 99 ms per resample | 22 MB |
| 100,000 × 3 | 5 | 5.2 s | 112 MB |

(The last row's memory is the rows themselves and their strata, built once; it does not
grow with resamples.) For comparison, before the final review's changes the first two rows
took 44.4 s with 669 MB and 166.0 s: every application's resample values were held until the
end, and each small application was drawn on its own.

For a very large export lower `--bootstrap` or cut the export with `--since`. A
1,000,000-row export at the default 1000 resamples is of the order of ten to twenty minutes
per pass on this machine; nothing has been timed on a real export (**TO MEASURE**, research
§20.6.3).

### 10.10 Limits

- Everything in §10.2 and §10.4. In particular the range is too narrow when rows are
  correlated, and it knows nothing about a traffic shift.
- **It does not cover variation between applications.** The applications in the export are
  taken as given. If they are themselves a sample — of users, of tenants — the range is much
  too narrow when some of them are large: on synthetic data with applications of unequal
  means, 0.09 to 0.29 of the width that redrawing the applications gives (research §20.6.1,
  which states the method).
- **What stratification leaves out is disclosed, not corrected.** A stratum of `n` rows
  resampled on its own reproduces `(n − 1)/n` of its variance. `totals_strata.variance_factor`
  is that effect for the totals under an equal-variance assumption; the ranges are not
  rescaled by it. Before small applications were pooled, a synthetic 240-row export gave
  0.82 of the right totals width as 80 applications of 3 rows, 0.70 as 120 of 2 and a width
  of zero as 240 of 1; pooled, all three give 0.99 (research §20.6.1). `--min-rows 1`
  switches pooling off and brings the zero width back — with the disclosure reading
  `variance_factor: 0.0`.
- **Pooling assumes the small applications' rows are exchangeable.** Where those applications
  differ in mean, the pooled totals range is wider than a fixed-mix reading — by up to 14% on
  the same synthetic data.
- Thirty rows is a convention for `--min-rows`, not a guarantee that a range is reliable at
  31.
- `zero_bound` and `one_bound` are one-sided, assume independent rows, and bound the rate
  under this replay, not the live rate. A share fixed by policy (an `onprem_only`
  application's on-prem share) is not a sampled rate at all.
- No ranges per workload, on `overflow_hops`, on the baseline agreements, or on non-binding
  relaxations.
- Quality-label uncertainty needs the recommendation file; it is not read from the snapshot.
- Results are reproducible on the same Python minor version; the standard library's random
  streams are not guaranteed identical across versions.
- Every measurement quoted in this section is synthetic. Coverage of the ranges on recorded
  traffic is **TO MEASURE**.
- Capacity figures (§11) carry no range; §11.7 says why.

## 11. Capacity: stated limits against replayed demand (P3c-4)

Design: `docs/superpowers/specs/2026-10-07-p3c4-capacity-design.md` (as built: its
"Amendments" section). Research notes: `docs/research/feasible-route-bench.md` §21. Code:
`app/eval/capacity.py` (`CapacityPlan`, `load_capacity`, `build_plan`, `service_time`,
`arm_load`, `capacity_block`, `render_capacity_section`), wired into `app/eval/scenarios.py`
and `scripts/policy_simulate.py`.

### 11.1 What it answers

§4–§10 say where requests would go and what that costs. They treat every endpoint as having
unlimited room. This section answers the other half: **would the endpoints the traffic lands
on be able to carry it?** A change such as "all healthcare traffic on-prem" can be cheaper
and safer and still offer one local endpoint more than it was stated to serve.

It is a comparison of two things, and only that:

- **capacity the operator states** — RouteForge measures none of it and manages none of it
  (it deploys, scales and load-balances no inference);
- **demand the replay predicts** — the rows the simulator already routed, per endpoint, per
  arm, per time bucket.

It is **off unless a capacity is stated**: a `--capacity` file, or a `capacity` key in the
proposal or in any scenario. With none, every output file is byte-identical to what it was
before this feature (`tests/eval/test_uncertainty_golden.py` still pins that).

### 11.2 Stating capacity

**The deployment as it is — `--capacity FILE`**, schema
`routeforge.capacity-assumptions/v1`:

```json
{
  "schema": "routeforge.capacity-assumptions/v1",
  "title": "Lab endpoints, as load-tested 2026-10",
  "endpoints": {
    "local-llama": {"max_concurrency": 8, "requests_per_minute": 600,
                    "tokens_per_minute": 400000, "output_tokens_per_minute": 90000}
  },
  "workload": {"volume_scale": 10, "apps": {"claims": {"volume_scale": 25}}}
}
```

- Keys: `schema`, `title` (optional), `endpoints`, `workload`. Anything else is a load error.
- `endpoints.<slug>` must be an onboarded row of the snapshot (enabled or not) or a priced
  YAML pool model. A slug the snapshot does not hold is a load error — a typo never reads as
  "no limit stated".
- What the file states holds in **both arms**.

**What a change would make it — the proposal's `capacity` key** (§2.1): the same four limits
per slug, for the **proposed arm only**, overriding the file **limit by limit**. The slug
must exist in the proposed arm (an onboarded row the proposal does not remove, a priced pool
model, or an endpoint the proposal adds).

**The four limits.** Each is optional, and a finite number `> 0`:

| Limit | Compared with, per bucket |
|---|---|
| `max_concurrency` | offered concurrency: the service time that falls **in** the bucket ÷ the bucket length — the mean number of requests in flight during it. From recorded requests that is the number in flight at RouteForge **as recorded, waiting included** (a recorded latency includes any time spent in the endpoint's queue), not the number an idle endpoint would have had in service |
| `requests_per_minute` | requests **arriving** in the bucket, per minute |
| `tokens_per_minute` | prompt + completion tokens of those requests, per minute |
| `output_tokens_per_minute` | completion tokens of those requests, per minute |

**A limit of `0` is refused**, not read as "no capacity". Leave the key out for "not stated".
**A limit is printed exactly as it was stated.** `volume_scale` (§11.3) moves demand and never
a limit.

### 11.3 Volume scale

`volume_scale` (finite, `0 < x ≤ 1,000,000`, default `1`) counts every routed request of an
application that many times. It has a global value and per-application overrides, and can be
set in two places:

| Where | Applies to | Reads as |
|---|---|---|
| the `--capacity` file's `workload` | both arms | "real traffic is this many times the export" |
| a proposal's `workload` | the proposed arm | "…and suppose it grew by this much" |

The proposed arm's factor for an application is the product of the two.

- It changes **no route and no cost figure**. Everything outside the capacity section is for
  the recorded volume.
- It does **not rescale burstiness**: ten times the volume is the same arrivals, each counted
  ten times. Whenever any scale is not `1` the capacity section prints, once and verbatim:
  "A volume scale counts each recorded request that many times, at its recorded time; it
  does not model how more traffic would arrive."
- It does **not make a small export a large one**: 24 rows at `volume_scale: 1000` are 24
  arrival times. The window-too-short rule (§11.5) counts buckets, not scaled requests.
- `workload.token_scale` scales the prompt tokens in token demand (then `volume_scale`
  multiplies); it moves neither the request rate nor any service time.
- Setting it with no capacity stated anywhere is exit `2`: it would change nothing.

### 11.4 Running it

```bash
uv run python scripts/policy_simulate.py \
  --export outcomes.ndjson --snapshot snapshot.json --scenarios scenarios.json \
  --out results/cmp --capacity capacity.json \
  [--capacity-bucket 60] [--capacity-min-buckets 10]
```

The same flags work with `--proposal`. `--capacity` with `--shadow-prices` and no
`--proposal` is exit `2` (there is no proposed arm to measure).

| Flag | Default | Valid | Meaning |
|---|---|---|---|
| `--capacity` | none | a capacity-assumptions file | §11.2 |
| `--capacity-bucket` | `60` | integer, 1 to 86400 | bucket length in seconds |
| `--capacity-min-buckets` | `10` | integer ≥ 1 | with fewer buckets in which a request **arrived**: "window too short" (§11.5) |

Exit `2`, nothing written, the reason on stderr as `error: capacity: …`: a file that does
not load or validate; a flag out of range; a tuning flag, or a `workload.volume_scale`, with
no capacity stated anywhere; and a latency a proposal asserts for an endpoint (§2.1) of more
than 24 hours, when a row lands on that endpoint — a service time that long cannot be
placed, and an operator's claim is never dropped in silence (in a scenarios file the
location is `scenarios[<i>](<name>).endpoints.<slug>.latency_ms`). The same refusal applies
to a latency replay itself supplies, and then the location is where the value came from:
`snapshot.signals.<slug>.latency_ms`, `export.latency_ms (rows served by <slug>)` or
`snapshot.settings.routing.signals.tier_latency_ms`, with no scenario named.

`--min-agreement` refuses first in every mode: a run below the gate exits `4` and writes
nothing, even when the capacity model would have refused it with exit `2`.

### 11.5 The method

- **Which rows.** For each arm, every simulated row the arm **routed** adds demand to the
  endpoint replay served it from. A rejected row adds nothing.
- **When a request arrives.** `ts` on an outcome row is when the row was **written — the end
  of the request**. Arrival is estimated as `ts` minus the row's recorded `latency_ms`, in
  whole milliseconds (the timestamp is floored to the millisecond; no float decides which
  bucket a row is in). A latency that is missing, negative, not finite or **above 24 hours**
  is unusable: it subtracts nothing, and the row is counted in `rows.unusable_latency`.
  Buckets are aligned to the Unix epoch (with the default, a bucket is a UTC minute). A row
  with no `ts` is counted in `rows.without_ts` and is in no bucket.
- **Where its load goes.** A request is in service from its estimated arrival for its
  service time. Its **requests and tokens are counted in the bucket it arrived in** (a rate
  limit counts admissions). Its **service time is spread over every bucket it runs
  through**, by the milliseconds that fall in each. So one request is never more than one
  request of concurrency in any bucket, however long it runs and however short the bucket.
- **Service time, per row, per arm**, the first that applies:
  1. `asserted` — the proposal asserts a `latency_ms` for the arm's endpoint (§2.1). It is
     the operator's claim about the endpoint under that scenario, so it replaces the others.
     Proposed arm only.
  2. `recorded` — the arm serves the row on the endpoint that **served it alone** (recorded
     `status: ok`, one attempt, `chosen` = that endpoint): the row's own recorded latency.
  3. `endpoint_mean` — otherwise the endpoint's **mean** recorded latency over the simulated
     rows it served alone. The mean, not the median §4.1 reports as the replayed latency:
     offered load is the arrival rate times the mean service time, and with skewed service
     times the median understates it badly (research §21.5).
  4. `replay` — the endpoint served no simulated row alone: replay's one latency for it, its
     observed median, else the snapshot's signal, else the tier default (`replay_source`
     says which).

  `service_time_sources` counts the rows under each, per endpoint and arm. The report prints
  a sentence when a tenth or more of an endpoint's service time is `replay`.
- **A tier default is not a service time.** The tier default (`config/routing.yaml`: 200 /
  500 / 1000 ms by tier) is a constant of the routing score. When it is all an arm has for
  an endpoint — no simulated row served by it alone, no usable latency signal in the
  snapshot, no latency asserted — **no concurrency figure is computed for that endpoint in
  that arm**: `demand.concurrency` is null, a stated `max_concurrency` carries
  `"not_computed": "tier_default_service_time"` and null figures, and its row reads
  `n/a (no service time: state endpoints.<slug>.latency_ms)` in the proposed arm, or
  `n/a (no service time: no recorded request or latency signal for <slug>)` in the current
  arm (a proposal's `latency_ms` applies to the proposed arm only). The arm's headline is
  chosen without it and says `…; concurrency not computed for <slug>`. Requests and tokens
  are counted as usual. It is a state, like "window too short": the alternative was a
  number — ten-second requests moved to such an endpoint read `peak 2.0% / avg 1.9%` of a
  limit the current arm filled (synthetic; research §21.5.5).
- **Tokens.** The counts replay **priced** the call at: the backend's billed prompt count
  (scaled by `token_scale`) and the recorded completion count. A row no backend answered uses
  RouteForge's own `routing_tokens` and the router's output estimate (§6); the rows routed to
  an endpoint that are sized that way are counted in its `estimated_token_rows`.
  `tokens_per_minute` is prompt + completion, as a provider's TPM quota counts a call; a
  provider that reserves `max_tokens` at admission counts more. Cached tokens are not
  subtracted.
- **The window** runs from the bucket of the first estimated arrival to the last bucket a
  recorded request was still in service in. It comes from the recorded rows alone, so it is
  the same for both arms, every endpoint and every scenario. Service time that an asserted,
  mean or replayed latency pushes past the window's last bucket is in no bucket; it is
  reported (`service_ms_after_window`) so that service time adds up, and as a share of the
  arm's service time on the endpoint (`service_after_window_share`). That share is how much
  of the arm's concurrency the window does not show: the endpoint's note prints it with the
  seconds, and at a tenth or more the headline names it (§11.6, §11.9).
- **Window too short.** With fewer than `--capacity-min-buckets` buckets in which a request
  **arrived** (`window.arrival_buckets`), the block says `window_too_short: true` and carries
  no endpoint figure; the comparison table cell reads
  `window too short (k of the 10 buckets needed)`. Buckets a request merely ran through do
  not count: a long service time cannot make a short export look long.

Per endpoint, per arm, per **stated** limit: `limit`, `not_computed` (null, or the reason a
`max_concurrency` has no figure — every figure is then null), `peak` (the largest bucket ÷
the limit), `peak_bucket`, `mean_window`, `mean_active`, `buckets_over` and
`buckets_over_share` (buckets above 100%, as a count and as a share of **all the window's
buckets**), and `requests_over_share` (that endpoint's requests arriving in those buckets).
Demand of exactly the limit is 100%, which is not above it. Demand itself (`peak`, `mean_window`, `mean_active`
of each of the four) is shown for every endpoint with routed rows, **stated limit or not**.

Every figure is computed from exact integer counts of requests, tokens and milliseconds per
endpoint, bucket and application; the counts of applications that share a volume scale are
added before the scale is applied, once. The output does not depend on the order of the rows
in the export, and a uniform scale never moves a peak to another bucket.

### 11.6 Reading a peak: never without the average and the share

**A peak is a maximum over buckets.** It is the single busiest bucket of the window. For a
perfectly steady stream it still sits well above the average, and it grows when the window
gets longer (more buckets to be the maximum of) and when the bucket gets shorter (less
averaging inside one). A peak says nothing about how often the endpoint is that busy.

So no output prints a peak alone. Beside every peak are:

| Figure | Over | Reads as |
|---|---|---|
| `mean_window` — `avg (window)` | every bucket of the window, **empty ones included** | the time-average: how loaded the endpoint is over the whole period |
| `mean_active` — `avg (active)` | the endpoint's `active_buckets`: those in which it had an arrival or a request in service | how loaded it is while it is doing anything |
| `buckets_over`, `buckets_over_share` | every bucket of the window | how much of the period is above the stated limit |
| `requests_over_share` | the endpoint's requests | how much of its traffic arrives in those buckets |

and the report and the comparison table print the first and third together with the peak:

```
peak 355.2% / avg 59.3%, over 100% in 17.6% of 10081 buckets (local-llama, max_concurrency, 60 s buckets)
```

That is: the busiest bucket; the time-average; the share of the window's buckets above the
limit and how many buckets the window has; then the endpoint, the limit and the bucket
length. **The share is there because peak and average cannot tell "over the limit 1% of the
time" from "18% of the time"**: the same week of traffic against twice the limit reads
`peak 177.6% / avg 29.6%, over 100% in 1.2% of 10081 buckets` (synthetic; research
§21.5.5). And the ratio of peak to average grows with the length of the export, while the
share does not.

Two clauses can follow the parenthesis:

- `; 16.4% of service time on local-llama falls after the window` — a tenth or more of that
  arm's service time on an endpoint with a stated `max_concurrency` runs past the window, so
  its concurrency is understated by about that much (§11.9). Printed whichever limit the
  headline is on.
- `; concurrency not computed for local-llama` — a `max_concurrency` is stated for that
  endpoint and the arm has no service time for it (§11.5). The headline was chosen without
  it. With nothing else stated the whole text is `n/a; concurrency not computed for …`.

**The headline is a maximum over endpoints and limits as well as over buckets.** Stating
more limits, or limits for more endpoints, can only raise it.

How to read the pairs:

- **Peak high, window average low** — a sparse or bursty export. Twelve requests a day, each
  over two minutes long, against `max_concurrency: 1` is `peak 100.0% / avg 2.1%, over 100%
  in 0.0% of 1323 buckets`: the endpoint is fully used for a minute at a time and idle
  nearly always, and the limit is never exceeded.
- **Peak and window average both above 100%** — the endpoint is offered more than its stated
  limit on average, not just at the worst moment.
- **Window average low, active average high** — the export has long idle gaps (two test
  sessions a week apart). `mean_active` describes the sessions; `mean_window` the period.
  `active_buckets` and `window.buckets` are both printed.
- **A different `--capacity-bucket` moves the peak and leaves `mean_window` where it is.**
  If a conclusion changes with the bucket length, it rested on the peak.

### 11.7 What the figures are — and are not

Every capacity section opens with this, verbatim:

> **Stated capacity against replayed demand.** Every limit below is a figure the operator
> stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed
> route of each recorded request. A request arrives at its estimated arrival time (the time
> the record was written minus its recorded latency) and is in service from then for its
> service time: requests and tokens are counted in the bucket of arrival, service time in
> every bucket it runs through. Service time is the request's own recorded latency where
> replay serves it on the endpoint that served it alone; otherwise that endpoint's mean
> recorded latency; otherwise replay's latency for the endpoint, unless that is only a tier
> default, which is not a service time: no concurrency figure is then computed. A latency the
> proposal asserts replaces all three. A recorded latency is what RouteForge recorded for the
> whole request, not the backend's own time: it includes the time RouteForge spent before and
> after the backend call and any time the request waited in the endpoint's own queue, and for
> a request that failed over it cannot be attributed to one endpoint, so it is not used. So
> concurrency from recorded requests is the number in flight as recorded, waiting included,
> not the number an idle endpoint would have had in service. The window ends when the last
> recorded request ended; service time that runs past it is in no bucket and is reported as a
> share. A peak is a maximum over buckets: it grows with a longer window and with a shorter
> bucket even for a steady stream, so every peak is printed with the average over the whole
> window and the share of buckets above the limit. Nothing here models queueing, batching,
> autoscaling, the serving engine's real concurrency behaviour, load that reaches an endpoint
> without passing through RouteForge, bursts shorter than a bucket, or the load of failover
> attempts. RouteForge does not know what an endpoint does above a stated limit (queue, shed
> or slow down), so no rejection and no delay is predicted. An export of a few buckets says
> little.

`capacity.not_modelled` carries the exclusions as ids: `queueing`, `batching`,
`autoscaling`, `engine_concurrency`, `external_load`, `sub_bucket_bursts`, `failover_load`,
`moved_request_latency`, `overload_behaviour`. (`moved_request_latency`: a request replay
moves to an endpoint it never ran on gets that endpoint's mean service time, not a
prediction of its own.)

- **No predicted rejection.** `newly_rejected` stays a policy figure. Nothing in the capacity
  block counts a request as lost: live, a refused request fails over to the next candidate,
  which replay does not model.
- **No range.** With `--uncertainty` the capacity figures stay points and the section says
  why: the bootstrap resamples rows as if their order did not matter, and capacity depends on
  when requests arrive. The capacity block is identical with and without `--uncertainty`, and
  the `uncertainty` block is identical with and without capacity.
- **Never a recommendation.** The section names no remedy — not more capacity, and not a
  change to any policy or protection. The comparison table stays in file order; a lower peak
  load does not make a scenario the one to choose.

### 11.8 Output

**`simulation.json`** gains `capacity` and `inputs.capacity` (`file_sha256` — null with no
file — `bucket_seconds`, `min_buckets`), and nothing else changes:

| Field | Contents |
|---|---|
| `method` | `"spread_offered_load"` (the first model, withdrawn before any figure was quoted, was `"bucketed_offered_load"`; the id changed so the two cannot be confused) |
| `bucket_seconds`, `min_buckets` | the plan |
| `window` | `first_bucket`, `last_bucket` (ISO-8601 UTC bucket starts), `buckets` (every bucket from the first to the last), `arrival_buckets` (those in which a simulated row arrived) |
| `rows` | `simulated`, `without_ts`, `unusable_latency`, `excluded_not_counted` (excluded rows other than cache hits and rows before `--since`: they may have loaded an endpoint) |
| `volume_scale` | `base` (the file's) and `proposal`, each `{"*": global, "<app>": value}` |
| `not_modelled` | the nine ids of §11.7 |
| `window_too_short` | when true, `endpoints` is empty, both `peak` entries are null and both `concurrency_not_computed` lists are empty |
| `endpoints.<slug>.<arm>` | `requests` (volume-scaled); `active_buckets`; `service_time_sources` (`recorded`, `endpoint_mean`, `replay`, `asserted` — rows); `service_time_ms` (`mean`, `max` over those rows; null with none); `replay_share` (the share of the rows' service time that is `replay`) and `replay_source`; `service_ms_after_window` and `service_after_window_share` (that amount over all the service time the arm placed on the endpoint; null with no row there or with no service time); `estimated_token_rows`; `demand.<dimension>.{peak, mean_window, mean_active}` (all null for `concurrency` when the arm has no service time for the endpoint, §11.5); and `limits.<limit>` — the §11.5 figures, only for limits stated for that arm |
| `limits.<limit>.not_computed` | null, or `"tier_default_service_time"` on a `max_concurrency` the arm has no service time for; `limit` is still the stated value and every other figure is null |
| `peak.<arm>` | the arm's most loaded stated limit **among those with a figure**, with its context: `endpoint`, `limit`, `utilisation` (the peak), `bucket`, `mean_window`, `buckets_over_share`, `bucket_seconds`, `buckets` (the window's), `service_after_window_share` (that endpoint's); null when the arm has no stated limit with a figure |
| `concurrency_not_computed.<arm>` | the endpoints, sorted, whose stated `max_concurrency` has no figure in that arm: what `peak.<arm>` was chosen without. Usually empty |

**`comparison.json`** gains `capacity.scenarios` — file order, each `name` plus exactly the
block above for that scenario's body — `inputs.capacity`, and six keys on every `table`
row: `peak_utilisation`, `peak_mean_window`, `peak_buckets_over_share`, `peak_endpoint`,
`peak_limit` (the proposed arm's `peak`; null when nothing with a figure is stated or the
window is too short) and `peak_concurrency_not_computed` (the proposed arm's list above).

**Reports.** A single proposal gets a `## Capacity` section after the P3a report, before
"Uncertainty ranges" and "Shadow prices": the caveat, "This section reports load against
stated limits and proposes no remedy…", the window and what the two averages are over, the
volume sentence when a scale is in use, each arm's peak load with its average and the share
of buckets above the limit, then per endpoint its requests, active buckets and service
time (mean, longest, and how many rows used each source), a utilisation table (one row per
stated limit per arm: stated, peak, avg (window), avg (active), buckets over 100%, requests
arriving in those buckets, peak bucket) and a demand table. A comparison gets one more table
column, last — `peak / avg load (proposed)`, in the form of §11.6 — one `## Capacity`
section with the caveat and the current arm's peak load, and a `### Capacity` subsection per
scenario. No figure in either report is printed with an exponent.

### 11.9 Limits

Every figure quoted in this subsection is **synthetic** — rows built by hand with a known
arrival pattern, run through `capacity_block` — and the method for each is in research
§21.5.5. None is a measurement of real traffic.

- Everything in §11.7. In particular the model has **no queue**: 150% of a stated
  concurrency says the endpoint is offered more than it was stated to serve, not what
  happens next.
- **A recorded latency is not a backend service time.** It includes the time RouteForge
  spent before and after the backend call (the gate, compression, verification), and under
  load it includes any time the request waited in the serving engine's own queue — so
  demand computed from an overloaded period overstates the service time an unloaded endpoint
  would need. Concurrency from recorded requests is the number in flight as recorded,
  waiting included.
- **On an `ok` row, `latency_ms` is the sum of the trace records' latencies, not
  wall-clock time** (`docs/reference/routing-outcomes.md` §3.2). The arrival estimate and
  the service time both rest on it; how far the sum is from wall-clock is unmeasured
  (research §21.5.4, E10c and E10e).
- **A request recorded as failed gets the endpoint's mean service time.** A row recorded as
  `backend_failed` or `verify_failed`, one that failed over, or one that was rejected, that
  an arm routes, never uses its own latency: a failure row's latency is wall-clock from the
  request handler, not a service time. So a failure that held a slot for a long timeout is
  counted at the mean: with one row in ten recorded as a 120 s `backend_failed` timeout
  among 10 s requests, mean concurrency read 0.968 where holding the slot for 120 s would
  give 2.03.
- **A moved request has no service time of its own.** It gets the mean of the endpoint it is
  moved to (or replay's latency for it), whatever its size; where that latency is only a
  tier default there is no concurrency figure at all (§11.5).
- **The export is right-truncated, which understates an arm whose service time is not the
  recorded one.** The window ends when the last recorded request ended; service time an
  asserted, mean or replayed latency places after it is in no bucket. On a 30-minute export
  at 6 requests a minute with the proposal asserting a service time `S`, the share of the
  proposed arm's service time after the window was 1.4% at 60 s, 16.4% at 600 s
  (`mean_window` 50.1 where the steady state is 60), 49.8% at 1,800 s (90.4 against 180)
  and 87.4% at 7,200 s — beyond the window's length the figures stop responding to `S`. The
  share is printed (`service_after_window_share`, §11.5); the figures are not corrected.
- **Epoch-aligned buckets understate a sliding-window rate limit.** A provider that counts
  requests over any 60 seconds sees more than the busiest calendar minute: 20 requests in
  10 seconds straddling a minute boundary, against 10 a minute, read `peak 100.0%` with no
  bucket over, where the busiest sliding minute holds 20; on three steady Poisson streams
  the busiest sliding 60 seconds held 1.00 to 1.10 times the busiest calendar minute.
- **The headline is a maximum over endpoints and limits as well as over buckets.** Stating
  more limits can only raise it; two runs are comparable only when they state the same
  limits.
- **Client retries are counted as independent demand.** A request retried after a live
  failure or timeout is several rows; a scenario in which the first attempt would have
  succeeded would not have had the others.
- **Demand that left no usable record is censored.** A request refused before an outcome
  row was written is not in the export, and rows excluded from simulation
  (`unknown_status`, `unknown_app`, `missing_features`, `missing_routing_tokens`) are not
  in the demand. The excluded count is printed (`rows.excluded_not_counted`); the direction
  is understatement.
- **A congested recording compounds with `volume_scale` and with moved traffic.** Recorded
  latencies from a period in which the endpoint was queueing are inflated; scaling volume or
  moving more traffic onto that endpoint multiplies the inflated service time, and nothing
  feeds the extra load back into it (no queue is modelled).
- **Tokens are not spread.** A long generation's tokens are all counted in the bucket it
  arrived in, although the endpoint produces them over the whole service time.
- **Arrival is an estimate** (`ts` minus the recorded latency), and a failure row's latency
  includes gate time (`docs/reference/routing-outcomes.md` §3.2).
- **The window's left edge.** A request in service when the export begins, which arrived
  before it, is not in the export's demand.
- **First-decision replay.** A request that failed over live loaded two endpoints; here it
  loads one. Every §6 limit applies.
- **`volume_scale` is a multiplier**, not an arrival process (§11.3).
- **A stated limit is not a measurement**, and one number per endpoint ignores that capacity
  depends on the prompt and completion lengths being served.
- No capacity figure for a shadow-price relaxation (§8.7).
- Nothing has been run on recorded traffic: research §21.5.4.
