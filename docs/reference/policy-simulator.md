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

- Design: `docs/superpowers/specs/2026-09-24-policy-simulator-design.md`.
- Research notes (the model, the two-arm design, the invariants):
  `docs/research/feasible-route-bench.md` §15.
- Prerequisite datasets: `docs/reference/routing-outcomes.md` (P2b-2, the export this reads)
  and `docs/reference/quality-calibration.md` (P2b-3, the quality ladder replay uses).

Code: `app/apps/snapshot.py` (snapshot builder), `app/api/admin_routes.py`
(`GET /admin/config-snapshot`), `app/eval/scenario.py` (snapshot/proposal → scenario),
`app/eval/proposal.py` (proposal load/validate/apply), `app/eval/simulate.py` (replay +
report), `scripts/policy_simulate.py` (CLI).

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
session cookie). It calls `state.registry.ensure_loaded(state.db)` before snapshotting
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

**Top level (`_TOP_KEYS`):** `schema`, `title`, `apps`, `providers`, `endpoints`, `prices`.
Every container must be the documented type — `apps`, `providers`, `prices`,
`endpoints` and `endpoints.update` objects; `endpoints.add` a list of objects;
`remove`/`disable`/`enable` lists of slug strings — or loading fails naming the path.

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
| `add` (list of objects) | Keys: `slug`, `provider`, `tier`, `price`, `region`, `prompt_retention`, `accepts_raw_sensitive`, `context_window`, `quality`, `allowed_app_ids`. `slug` must be new — not an existing onboarded row or a priced YAML pool model. `provider` must be declared. `tier` must be canonical. `price.input`/`price.output` must be finite ≥ 0; `price.cached_input` null or finite ≥ 0. `prompt_retention` null or one of `none`/`retained`/`unknown`. `accepts_raw_sensitive` null or bool. `context_window` null or an int `> 0`. `quality` null or a finite value in `[0, 1]`. `allowed_app_ids` null or a list of application ids that exist in the snapshot. `region` must be a string or `null`; the token itself is customer vocabulary and is not checked against a list (admin parity). |
| `remove`, `disable`, `enable` (lists of slugs) | Each slug must already be an onboarded row. |
| `update` (map slug → fields) | Same slug requirement. Fields: `tier`, `region`, `prompt_retention`, `accepts_raw_sensitive`, `context_window`, `allowed_app_ids` — validated the same way as `add`'s matching field, only when the field is present (`region`: string or `null`). |

**`prices.<slug>`** — `{"input", "output", "cached_input"}`, same price validation as
`endpoints.add[].price`. The slug must be an onboarded row not being removed in this same
proposal, or a priced YAML pool model.

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
| `--proposal` | required | `routeforge.policy-proposal/v1` JSON file |
| `--out` | required | output directory (created if missing) |
| `--config-dir` | `config` | the base config directory the snapshot is overlaid onto. The snapshot replaces `routing`/`pricing`/`policy` wholesale and overrides each declared provider's data-handling fields and the four `serving_policy` posture values, so the base only supplies what no routing decision reads (and the set of declared providers). |
| `--fail-on-new-rejections` | off | exit `3` instead of `0` when the proposal newly rejects any simulated row (for CI/GitOps gating) |

**Exit codes** (`scripts/policy_simulate.py`):

| Code | Meaning |
|---|---|
| `0` | `simulation.json` and `report.md` written |
| `2` | an input file failed to load, validate or apply — a bad export, an invalid proposal (unknown key, bad value), or a snapshot that loads structurally but fails **deep** validation during `simulate()` (for example a provider the running deployment's own `config/` never declared). Nothing is written; the offending location is on stderr. |
| `3` | the proposal introduces new rejections and `--fail-on-new-rejections` was passed |

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
| `inputs` | `export_sha256`, `snapshot_sha256`, `proposal_sha256`, `proposal_title`, `routing_policy_version`, `registry_version`, `snapshot_generated_at`, `serving_policy` (the four posture values every app was compiled under — the snapshot's), `window` (`{start, end}` over the simulated rows' `ts`, or `null`) |
| `edits` | the proposal, minus `schema`/`title`, keys sorted — exactly what was asked for |
| `rows` | `simulated` (count) and `excluded` (count per reason: `cache_hit`, `unknown_status`, `unknown_app`, `missing_features`) |
| `baseline` | the fidelity check — `rows_compared`, `agreement`, `warning`, `by_app`, `top_disagreements` (§5) |
| `totals`, `by_app.<id>`, `by_workload.<label>` | each the same block: `requests`, `route_changed`, `newly_rejected`, `newly_served`, `cost_usd` (`{current, proposed, delta}`), `latency_ms.p50`/`.p95` (same shape), `quality_mean` (same shape), `onprem_share` (`{current, proposed}`) |
| `moves` | top 20 `{from, to, count}` model-pair moves; `(rejected)` is a legal `from`/`to` value |
| `new_rejections` | `{by_app: {...}, by_code: {...}}` — rows that were `routed` under `current` and `rejected` under `proposed` |
| `affected_apps` | apps with any `route_changed`, `newly_rejected` or `newly_served` row |
| `sources` | per arm, a count of `latency_source`/`quality_source` values (`observed`/`signal`/`tier_default`, `published`/`tier_default`/`calibrated`/`customer`) across routed rows |
| `per_day` | `{cost_usd: {current, proposed, delta}}` divided by the export window in days; `null` when the window is under 1 day |

### 4.2 `report.md`

Opens with the caveat, verbatim, as a blockquote:

> **Simulation only.** Nothing in this report has been applied. It predicts the first routing
> decision of recorded requests under the proposal, using the real eligibility and routing
> code and a frozen snapshot of signals. It does not model failover, escalation, breaker
> state, request pins or caching.

Then, in order: a baseline warning (only if `agreement < 0.90`), **Headline** (the `totals`
metrics table), **New rejections** (by app, by code — or "None."), **Moves** (from → to →
count — or "None."), **By app** (one subsection per app, same metrics table), **By workload**
(same, per workload label), **Latency and quality sources**, **Excluded rows** (reason →
count).

## 5. Fidelity: the baseline check

Simulation reconstructs context and re-runs real code, so it can drift from what was
actually served. The baseline check (spec §7, `app/eval/simulate.py::_baseline`) is that
fidelity check — **not** the headline result.

For every simulated `ok` row that carries a recorded first attempt (`row["attempts"]`), the
`current` arm's pick is compared with `attempts[0]`. The report shows the overall agreement
rate, the rate per app, and the top 10 `recorded → simulated` disagreement pairs by count.
Agreement below 90% prints a warning at the top of `report.md`.

**What lowers it:**

- **Drift since the export window** — onboarding, price or signal changes between when the
  traffic was served and when the snapshot was taken.
- **The unrecorded pin and `max_tokens`** (B55, §6 below) — a pinned request replays as an
  ordinary routed request, and every row prices at the deployment's default output-token
  estimate rather than its real `max_tokens`.
- **Circuit-breaker state** — a request served while a circuit was open replays with no open
  circuits (§6).

**The headline diff itself is never logged-vs-simulated.** Both `current` and `proposed` are
simulated arms sharing one signals snapshot, so drift and fidelity loss affect both sides
equally — that is the reason the two-arm design was chosen over comparing the `proposed` arm
against the raw export (`docs/research/feasible-route-bench.md` §15.2).

## 6. What is not simulated

From spec §2, plus what the build settled during P3a:

- **No live dry-run of an admin write.** The simulator adds one read-only endpoint
  (`GET /admin/config-snapshot`) and no other serving behaviour.
- **No explanation of why a proposal rejects a request** (P3b) and **no shadow prices,
  alternative scenarios, capacity model or uncertainty ranges** (P3c).
- **No re-running PII detection.** Recorded `detected_entity_types` are replayed as-is, so a
  proposal cannot change `pii_detection`, a denylist, exempt patterns or recognizers.
- **No daily-spend or RPM admission simulation** — those are admission limits, not routing.
- **No circuit-breaker state.** Both arms run with no circuits open.
- **No confidence-escalation or failover replay.** Only the request's first routing decision
  is simulated; later hops are never replayed.
- **A proposed change to an endpoint's `context_window` has no visible effect.** Context-window
  enforcement (`ContextWindowExceeded`) happens at gateway call time
  (`app/gateway/gateway.py`), not in eligibility or routing — the router never reads
  `context_window` when scoring. The proposal field is still validated and applied to the
  scenario's registry; a first-decision replay simply cannot show what a call-time rejection
  would have done.
- **B55 — pinned requests and `max_tokens` are not recorded, so they cannot be replayed.**
  `routing_outcomes` does not currently record a request's pinned model or its `max_tokens`.
  The simulator therefore replays every request as if it were unpinned (an ordinary routed
  decision, never a `model_not_found` pinned-model rejection), and prices every row's
  completion at the deployment's default output-token estimate
  (`expected_output_tokens(settings, None)`), never the request's real cap. This lowers
  baseline agreement for pinned or large-`max_tokens` traffic. See `docs/backlog/bugs.md` B55
  for the deferred fix (two nullable columns, needs an `ALTER`).

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
