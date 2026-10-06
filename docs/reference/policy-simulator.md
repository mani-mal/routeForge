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
  are `docs/superpowers/specs/2026-09-26-shadow-prices-design.md` (P3c-1).
- Research notes (the model, the two-arm design, the invariants):
  `docs/research/feasible-route-bench.md` §15; shadow prices §17.
- Prerequisite datasets: `docs/reference/routing-outcomes.md` (P2b-2, the export this reads)
  and `docs/reference/quality-calibration.md` (P2b-3, the quality ladder replay uses).

Code: `app/apps/snapshot.py` (snapshot builder), `app/api/admin_routes.py`
(`GET /admin/config-snapshot`), `app/eval/scenario.py` (snapshot/proposal → scenario),
`app/eval/proposal.py` (proposal load/validate/apply), `app/eval/simulate.py` (replay +
report), `app/eval/shadow.py` (shadow prices, P3c-1), `scripts/policy_simulate.py` (CLI).

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
`workload`. Every container must be the documented type — `apps`, `providers`, `prices`,
`endpoints`, `endpoints.update`, `workload` and `workload.apps` objects; `endpoints.add` a
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
| `apps.<application_id>` | The app must be in the snapshot. Same two keys. | Overrides the global value for that app. |

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

**Where assumptions live.** `apply_proposal` writes them into the applied document as a
top-level `assumptions` block: `{"token_scale": {"*": x, "<app>": y}, "cached_share": {...},
"latency_ms": {"<slug>": ms}}`, absent when the proposal sets none. A snapshot **file** that
already contains `assumptions` is a load error, so a hand-edited snapshot cannot put
assumptions into the `current` arm.

## 3. Running the CLI

```bash
uv run python scripts/policy_simulate.py \
  --export outcomes.ndjson --snapshot snapshot.json --proposal proposal.json \
  --out results/sim \
  [--config-dir config] [--fail-on-new-rejections]
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

`--proposal` is required unless `--shadow-prices` or `--scenarios` is given — with none of
the three, the CLI exits `2` (an argparse usage error, since there is nothing to simulate).

**Exit codes** (`scripts/policy_simulate.py`):

| Code | Meaning |
|---|---|
| `0` | `simulation.json` and `report.md` written (`--proposal`, or `--shadow-prices` alone); `comparison.json` and `report.md` written (`--scenarios`, §9.4) |
| `2` | an input failed to load, validate or apply — a bad export, an invalid proposal (unknown key, bad value, a container of the wrong type), a malformed snapshot (a missing or mistyped nested key, named by its dotted path), a snapshot that loads structurally but fails **deep** validation during `simulate()`/`shadow_prices()` (for example a provider the running deployment's own `config/` never declared), a `--config-dir` that cannot load (missing key, bad YAML, a failed config lint), neither `--proposal` nor `--shadow-prices` given, `--max-relaxations < 1`, or a shadow-prices eligible-superset invariant breach (`ShadowInvariantError`, printed as `error: internal invariant: ...` — spec §3; a relaxation narrowed some row's eligible set, which should never happen since every relaxation only widens eligibility, so it means a bug, not a reportable finding). A relaxation that newly **rejects** a baseline-routed row is *not* a breach — it is reported as `newly_rejected` (§8.4) and exits `0`. `--scenarios` combined with `--proposal` or `--shadow-prices` (an argparse usage error), neither `--proposal` nor `--shadow-prices` nor `--scenarios` given (also an argparse usage error), an invalid scenarios file (named `scenarios[<i>](<name>).proposal.<path>`), a `--since` that is not a timestamp, a `--min-agreement` outside `[0, 1]`, and a `--scenarios` comparison invariant breach (`ComparisonInvariantError`, printed as `error: internal invariant: ...` — two scenarios disagreed about the rows, excluded counts or baseline they share by construction, which means a bug, never a reportable result) are also exit `2`. Nothing is written; the offending location is on stderr — never a traceback. |
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
| `inputs` | `export_sha256`, `snapshot_sha256`, `proposal_sha256`, `proposal_title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy` (the four posture values every app was compiled under — the snapshot's), `window` (`{start, end}` over the simulated rows' `ts`, or `null`), `since` (the `--since` cut as an ISO string, or `null`) |
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
sources**, **Excluded rows** (reason → count).

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
  individual constraint costs. Alternative scenarios are §9 (P3c-2). **A capacity model and
  uncertainty ranges** remain out of scope (P3c-3, P3c-4).
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
- **No uncertainty.** Every figure is a point estimate from replaying the recorded rows
  once; there is no confidence interval or sensitivity range. That is P3c-3.
- **No capacity effects.** A relaxation that would route far more traffic onto one endpoint
  is priced as if that endpoint has unlimited headroom — there is no queueing, saturation
  or capacity model. That is P3c-4.
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
| `table` | file order. One row per scenario: `name`, `cost_per_day_delta_usd`, `cost_delta_usd`, `route_changed`, `newly_rejected`, `newly_served`, `latency_p50_delta_ms`, `latency_p95_delta_ms`, `quality_mean_delta`, `onprem_share_current`, `onprem_share_proposed`, `overflow_hops`, `affected_apps` |

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

- No uncertainty ranges (P3c-3) and no capacity or overload model (P3c-4).
- `--scenarios` cannot be combined with `--shadow-prices`.
- `token_scale` on a saturated row and every `cached_share` are assumptions; the report
  labels both.
- Every §6 limit applies to every scenario.
