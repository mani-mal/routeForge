# Routing observability (`0.11.0`, extended through `0.18.0`)

Two shipped, operator-facing capabilities, first landed in `0.11.0` and since
extended (through the seven-feature program deployed at `0.18.0` — a third
always-on trace attribute `sp.prompt_version` joined the metadata set, noted
inline below). Both are pure Serving-Plane changes — neither deploys inference,
neither touches the Improvement Plane, neither weakens a privacy invariant.

1. **Prometheus `/metrics`** — a scrape surface for routing behaviour.
2. **Always-on candidate snapshot + routing-policy-version** — extra Langfuse
   trace metadata, emitted regardless of `capture_io`.

3. **Per-hop scoring record** (P2a, 2026-09-19) — `sp.scoring`: each eligible
   candidate's scoring terms and, per term, where the value came from.
4. **The observation log** (signal hardening, 2026-09-21) — `endpoint_observations`:
   the per-attempt log every measured signal is drawn from, and what boot seeding now
   replays instead of aggregating `usage_records`.
5. **The step-7 circuit breaker** (2026-09-23, B45) — takes an endpoint that keeps failing
   out of rotation for a cooldown; visible as the `circuit_open` rejection code, the
   `circuit_bypass` flag, a fifth metric family and a `circuit` object on the admin models
   list (§5).

> **Superseded 2026-09-17:** this page also documented `routing.stage_signals`, a
> flag-gated raise-only tier bias derived from the internal agent loop's state.
> The internal NAT agent and the multi-step path were removed, so the signal had
> no hop to observe; `config/routing.yaml`'s `stage_signals` block and the old
> `app/routing/signals.py` are gone. Agent loops are client-driven — the
> equivalent bias would have to be derived per conversation, which is not built.
> The removed code is recoverable from git history.
>
> **Name reused 2026-09-19.** `app/routing/signals.py` exists again and is a
> *different* thing: P2a's per-model measured latency/availability store (§3
> below). It observes completed calls; it derives nothing from an agent hop, and
> it can only reorder an eligible set, never bias a tier.

This is what shipped and is verified live — code, not intent. The design record
is `docs/superpowers/specs/2026-08-15-routing-observability-and-signals-design.md`;
where the two disagree, this page is right.

---

## 1. Prometheus `/metrics`

`GET /metrics` on the API app returns Prometheus text exposition
(`text/plain`; the exact `version=` parameter tracks whatever `prometheus_client`
version is installed — `pyproject.toml` pins `prometheus-client>=0.20` with no
upper bound). It is an **in-process exporter** — counters live
in the API pod's memory and are incremented inline as requests flow. RouteForge
bundles **no** Prometheus server; point your own monitoring at this endpoint.
Metrics reset to zero on every API-pod restart (persistence is the scraper's
job).

- **Open, unauthenticated** — same posture as `/health`. Auth is a noted
  production concern, not solved here. Don't expose it to untrusted networks.
- **Opt-in dependency.** `prometheus_client` ships in the `observability` extra
  (`uv sync --extra observability`, same extra as the Langfuse exporter — pure
  Python, air-gap safe). Without it the app still boots and serves, every
  `observe_*` call is a no-op, and `/metrics` returns **503**
  (`metrics unavailable: install the observability extra`).

### The five metric families

Source of truth: `app/obs/metrics.py`.

| Metric | Type | Labels |
|---|---|---|
| `routeforge_requests_total` | Counter | `model`, `status` |
| `routeforge_routing_decisions_total` | Counter | `tier`, `escalated` |
| `routeforge_routing_overhead_seconds` | Histogram | *(none)* |
| `routeforge_tokens_total` | Counter | `model`, `direction` (`in`\|`out`) |
| `routeforge_breaker_transitions_total` | Counter | `to_state` (`open`\|`half_open`\|`closed`) |

- `status` values you'll see: `ok`, `unauthorized`, `rate_limited`,
  `budget_exceeded`, `rejected`, `error`. For pre-routing rejects `model` is `-`.
- `escalated="true"` marks a routing decision that was a second (or later)
  attempt — i.e. a fallback/escalation, not the first-choice candidate.
- `routeforge_routing_overhead_seconds` times `Router.select()` only — router
  wall-time, not end-to-end latency.
- `routeforge_breaker_transitions_total` counts step-7 circuit-breaker transitions by the
  state entered (§5). It deliberately has **no `model` label**: which model is open is on
  the admin models list and in the transition log line, not in label cardinality.

**Privacy rule (load-bearing).** Labels are bounded-cardinality enums or model
ids only. There is **no `tenant`/`application_id` label and no prompt text** —
per-tenant spend and cost-vs-baseline rollups live in RouteForge's own DB store
and the console, never in Prometheus label cardinality. Do not add tenant labels
here.

### Scraping it

Nothing scrapes `/metrics` by default. On a Prometheus-Operator cluster, a
`ServiceMonitor` in the API's namespace targeting the API service's HTTP port
with `path: /metrics` picks it up. (On the DGX lab specifically, the existing
`kube-prometheus-stack` Prometheus watches all namespaces but only selects
monitors labeled `release: kube-prometheus-stack` — add that label and its
Grafana graphs it, no new workload.) A quick manual check:

```bash
curl -s http://<api-host>:<port>/metrics | grep '^routeforge_'
```

---

## 2. Always-on candidate snapshot + routing-policy-version

Every trace now carries three extra attributes, emitted **regardless of
`capture_io`** — they are decision metadata (enums, model ids, fixed reason
strings, content hashes), not request I/O, and are still run through
`redact_value` defensively. Source of truth: `app/obs/export.py`.

### `sp.candidates` — per decision step

The full eligible/rejected candidate set for that routing decision:

```json
[
  {"model": "onprem-7b", "tier": "cheap", "provider": "onprem",  "eligible": true,  "reason": "...", "code": null},
  {"model": "gpt-4o",   "tier": "frontier", "provider": "bedrock", "eligible": false, "reason": "excluded · residency", "code": null},
  {"model": "eu-32b", "tier": "mid", "provider": "onprem", "eligible": false,
   "reason": "excluded · region · endpoint declares 'eu', application allows 'us'",
   "code": "region_not_allowed"}
]
```

The chosen candidate is `eligible: true`; each rejected candidate is
`eligible: false` with its rejection reason. This is the RLHF/eval substrate
(see `docs/roadmap/rlhf-readiness.md`) — it records *what else could have been chosen and
why it wasn't*, without building any feedback API (that stays out of scope).

**`code` — the three endpoint data-handling exclusions.** A rejection caused by an
endpoint's declared data handling carries a stable code alongside the operator-facing
`reason`, so a consumer can branch on *which rule fired* without parsing prose:

| `code` | Fired because |
|---|---|
| `region_not_allowed` | the app set `allowed_regions` and the endpoint's declared region is not in it |
| `prompt_retention_not_allowed` | the app set `require_no_retention` and the endpoint's declared retention is not `none` |
| `raw_sensitive_not_accepted` | the request sends raw sensitive content and the endpoint declares `accepts_raw_sensitive: false` |

**`circuit_open` — excluded because the step-7 breaker holds the model open** (§5). Its
reason is `"excluded · circuit open"`. It is the last eligibility stage, so it only ever
labels a candidate that every policy stage admitted; a model that is both out of policy and
open reads the policy reason.

Every rejection from the eligibility seam (`app/policy/feasible.py`) carries its stage
code: `residency`, `prior_failure`, `app_scope`, `not_requested`, `model_scope`, the three
data-handling codes above, and `circuit_open`. The rejections the router adds after
eligibility — below/above target tier, over budget — have `code: null`. The codes are never
fabricated: a rejection with no code of its own gets `null`, not a guess. See `docs/reference/per-app-config.md` for the rules themselves.

### `sp.endpoint_registry_version` — on the root span

A short content hash (first 12 hex of sha256) over the **effective endpoint pool** — each
candidate's `name` / `provider` / `tier` plus its three resolved data-handling
declarations (`region`, `prompt_retention`, `accepts_raw_sensitive`), sorted for
stability. Computed by `ModelRegistry.registry_version()` and stamped on every trace's
root span beside `sp.app_policy_version` and `sp.routing_policy_version`, so a decision
names the registry it was made against and an operator editing a declaration becomes
visible in the trace instead of silent.

- Deliberately narrow: `quality`, `display_name` and price are **not** in it, because
  they never gate eligibility — a cosmetic rename or a price edit must not churn a
  version a replay consumer keys on. Price changes already move
  `sp.routing_policy_version`.
- Onboarding, disabling or re-declaring any endpoint changes it.
- The response `policy` block additionally carries `endpoint_data_handling`: the three
  declarations of the endpoint that **served** the request, plus the provenance of each
  (`model` / `provider_default` / `unknown`). These are operator declarations RouteForge
  enforces against, never facts it verified.

### `sp.routing_policy_version` — on the root span

A short content hash (first 12 hex of sha256) over the canonicalized
`routing.yaml` + `pricing.yaml` — the scoring weights, thresholds, tiers, model
pool, and prices that determine a routing decision. Computed once at load
(`app/config/loader.py`, stored on `Settings.routing_policy_version`) and stamped
on every trace's root span.

- Automatic, no manual semver. Editing a scoring weight or a price changes the
  hash; identical config across two deploys yields the same hash.
- Edits to `serving.yaml`/`policy.yaml` do **not** change it — only the two files
  that govern routing/pricing do.
- Lets a later eval pipeline group decisions by the exact policy that produced
  them.

### `sp.prompt_version` — on the root span

A content hash over the active managed-prompt texts (`Settings.prompt_version`),
added by feature 4 (DSPy prompt-optimization). Stamped on every trace's root
span **when non-empty**, mirroring `sp.routing_policy_version` exactly — so a
later eval pipeline can also group decisions by the exact managed-prompt set in
effect. Source: `app/obs/export.py` (docstring lines 112-114, `set_attribute` at
line 196).

---

## 3. The scoring record — `sp.scoring` (P2a)

Before P2a the router presented a four-term score of which three terms did not
measure what their names claimed: latency was a per-tier constant, `capacity`
added the *same* constant to every candidate (so it never changed an ordering at
all), and quality's provenance ladder existed but was invisible outside the
chosen candidate. P2a replaces the first two with measured signals and makes all
four report **where their value came from**. Source of truth:
`app/routing/strategy.py` (`RankResult.to_record`), `app/obs/export.py`,
`app/pipeline.py` (`_chosen_scoring`).

### Where it appears

| Surface | Carries | Why |
|---|---|---|
| **Trace** — `sp.scoring`, on each router hop's own span, beside `sp.candidates` | **every eligible candidate**, plus `weights`, `signals_enabled`, `latency_scale_ms`, `max_latency_norm` | the audit and replay surface |
| **HTTP response** — `policy.scoring` in the decision certificate | the **chosen candidate only**, plus `weights`, `signals_enabled`, `latency_scale_ms`, `max_latency_norm` | the response is on *every* chat completion and has to stay small |

**Per router hop, never collapsed.** A failover or confidence escalation
produces a second hop with a different eligible set and different signal state;
each hop emits its own record. A hop whose backend call *failed* also carries
one (its `chosen` is `null`, because nothing was served, but the failed
candidate is recoverable as `scoring.candidates[0].name` — the strategy sorts
best-first and the router takes `ordered[0]`). Without that, a failed
candidate's pre-failure provenance would be lost permanently, and failover is
exactly where availability matters.

**Rejected candidates get no entry.** They were never scored — inventing terms
for them would be the overstatement this record exists to remove. What they
were rejected *for* is already in `sp.candidates`.

**Emitted regardless of `capture_io`**, like `sp.candidates`, and for the same
reason: it is ids, enums and numbers only, never free text, and it still runs
through `redact_value` defensively at emit time.

### Shape (trace form)

```json
{"weights": {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2},
 "signals_enabled": true,
 "latency_scale_ms": 1000.0,
 "max_latency_norm": 3.0,
 "candidates": [
   {"name": "qwen3-8b", "score": 0.71,
    "quality":      {"v": 0.5,    "src": "tier_default"},
    "latency":      {"ms": 342.0, "src": "measured", "n": 128},
    "availability": {"v": 0.99,   "src": "measured", "n": 128},
    "cost":         {"est_usd": 0.00012, "norm": 0.4, "cached": "unpriced"}}]}
```

The score is

```
score = w.quality      · quality
      − w.cost         · cost.norm
      − w.latency      · min(latency.ms / latency_scale_ms, max_latency_norm)
      + w.availability · availability
```

so the trace record is **sufficient to recompute the hop's ranking offline** —
no signal store, no live config. That is pinned by
`tests/routing/test_scoring_record_replay.py`, which replays the record after a
`json.dumps`/`json.loads` round-trip.

**The response form now carries `latency_scale_ms`/`max_latency_norm` too**
(added alongside `weights`/`signals_enabled`), so a client can recompute the
*chosen* candidate's score from the response alone. Be precise about what that
does and does not buy: the response still carries **one** candidate, never the
full eligible set, so it can verify that one score but can never reproduce the
*ordering* among candidates it was never shown — only the trace form, with
every eligible candidate, can do that. Full replay (the ranking, not just one
candidate's score) stays a trace-only property.

### The five provenance values

`src` on `latency` and `availability`:

| `src` | Means |
|---|---|
| `measured` | an EWMA over at least `signals.min_samples` observed calls, not stale. `n` is the observation count. |
| `peer_prior` | **latency only.** No usable measurement of this model (as for `tier_default`), but at least one other *eligible* candidate in the **same tier** is `measured`. The value is the median of those peers' measured latencies. `n` still reports this model's own true count. (B51) |
| `tier_default` | **latency only.** No usable measurement — fewer than `min_samples` observations, or the newest one is older than `signals.staleness_s` — **and** no measured same-tier peer in the eligible set. The value is `signals.tier_latency_ms[tier]`. `n` still reports the true count, which may be non-zero. |
| `neutral` | **availability only.** Fewer than `min_samples` observations; the value is `1.0`, which is exactly the pre-P2a hardcoded constant. |
| `disabled` | `signals.enabled: false`. Tier default / `1.0`, and `n` is reported as `0` even when observations exist (see the caveat below). |

A stale latency falls back to the peer prior or the tier default **outright** rather than drifting
toward it: a drifted number would be neither measured nor default and could not
be labelled honestly.

**Why `peer_prior` exists (B51).** The tier defaults are optimistic (200 ms for
`cheap`), while real on-prem reasoning models measure several seconds. Scoring
an unmeasured model at the default made it look up to ~80× faster than its
measured tier-mates, so after every restart, onboarding or idle hour the router
sent each unmeasured model `min_samples` requests on missing data alone,
regardless of whether it could do the task. The peer prior assumes a new model is
*typical* for its tier instead. It is computed from the eligible set the hop
scored, and the value used is the `ms` in the record, so a hop remains
recomputable offline from the record alone. With no measured peer — including an
empty store — the tier default stands, so the identity property is unchanged.

`quality.src` is the existing four-rung ladder (`app/routing/quality.py`:
`customer` / `calibrated` / `published` / `tier_default`), now reported for
every eligible candidate rather than only the chosen one. `cost.cached` is
`priced` when the operator declared a `price_cached_input` for that endpoint and
`unpriced` otherwise — see `docs/guides/onboarding-models.md`.

### The identity property

With `signals.enabled: true` and an empty store, the defaults
(`tier_latency_ms` 200/500/1000 against `latency_scale_ms` 1000) make the
normalized latency term `0.2 / 0.5 / 1.0` and availability `1.0` — **numerically
identical** to the pre-P2a constants. Turning measurement on changes no score
until a measurement actually exists. `signals.enabled: false` is the operator's
escape hatch and restores the same numbers with `src: "disabled"`.

### Caveats an operator should know

- **A dead endpoint is penalized, not removed — and the penalty is small.**
  Availability is an additive term in `[0, w.availability]`, so swinging a model
  from perfectly healthy to completely dead changes its score by **at most
  `weights.availability`** — `0.2` with the shipped weights, against `0.6` for
  cost and `1.0` for quality. **A dead endpoint can and does still rank first**
  when it is enough cheaper, or scores enough higher on quality, than a healthy
  peer. Availability is a ranking preference, **not a circuit breaker and not a
  health gate**; it can never make a candidate ineligible. Taking a failing
  endpoint out of rotation is the step-7 circuit breaker's job (§5), which
  excludes it after `breaker.failure_threshold` consecutive endpoint failures;
  to take one out permanently, disable it (`PUT /admin/infra/models/{slug}`
  with `enabled: false`). Do not expect the signal to do either. Raising
  `weights.availability` is not advised: it trades against cost and quality on
  every request, not only on requests involving a sick endpoint, and B45's
  measurement found no weight that kept a dead, cheaper endpoint out without
  doing that (`docs/research/feasible-route-bench.md` §9, §11).
- **Per-replica drift is real.** The signal store is per-process: each API
  replica learns from its own traffic, so two replicas can hold different
  latency estimates for the same model and rank the same eligible set
  differently. This is acceptable because a signal is a *ranking preference*
  only — it reaches the strategy, which can reorder the eligible set but can
  never add to or remove from it, so drift cannot affect eligibility, residency,
  or cost caps. Boot seeding replays the shared `endpoint_observations` log
  (last `signals.seed_window_hours`; see §4 below) — a state no single
  replica held live — which bounds the divergence without unifying it.
  Cross-replica sharing of the *live* store is deliberately not built.
- **Streaming feeds availability and TTFT, never latency (signal hardening,
  2026-09-21; was B41).** `Gateway.run_stream` now observes a stream's
  terminal chunk as an availability-only success (`observe_stream_success`) and
  logs time-to-first-token — but never touches the latency EWMA, because a
  stream's timing is a different quantity from a non-streaming call's backend
  latency. An endpoint error before or during a stream also counts (subject to
  the same `counts_as_endpoint_unhealth` classifier as non-streaming calls); a
  client disconnect or a stream that ends with no terminal chunk records
  nothing — neither is attributable to the endpoint. **Correction to an earlier
  version of this page and of B41 itself:** it is not true that streaming
  clients contribute nothing. Every Qwen Code / `kit-gtm` request carries
  `tools`, which raises `StreamFallback` before `run_stream` is reached, so
  those requests are served (and observed) by `Gateway.run()` already — live:
  `availability n=98 measured`. What was blind, and is now fixed, is
  plain-chat streaming with no tools, not `onprem_only`, not
  `require_verification`, `stream_mode` in `stream_unverified |
  incremental_verify`.
- **Seeded availability now reflects real failures (signal hardening,
  2026-09-21; was B38).** Boot seeding replays `endpoint_observations`, and a
  failed request now writes an `error` row (`usage_records.status = "error"`)
  and one `error` observation per failed attempt that
  `counts_as_endpoint_unhealth`; a request-shaped 4xx is metered as an `error`
  row but is not observed — see §4 below and `usage_summary.error_count`.
- **`n` is `0` on the `disabled` path** even when the store holds observations,
  unlike `tier_default`/`neutral`, which report the true count. Still
  backlogged (B42) — unrelated to this slice.
- **A semantic-cache hit still never pollutes latency, by construction rather
  than by luck.** A cache hit never calls a backend, so `SignalStore`'s
  observation methods are never invoked for it and no `endpoint_observations`
  row is written — the mechanism that could have leaked the ~3 ms cache lookup
  into a model's latency EWMA (old B39, boot-seeding from `usage_records`) no
  longer exists, because seeding no longer reads `usage_records` at all (see
  §4). The whole-trace-latency blending B39 originally described is resolved
  the same way: the seed now replays `endpoint_observations`, which records
  only `Gateway.run`'s own backend-only latency per hop, never a
  pipeline-wide sum.
- **Cached-token capture depends on the provider.** A provider that does not
  report `usage.prompt_tokens_details.cached_tokens` yields `0`, which is
  indistinguishable from a genuine cache miss.
- **None of this claims routing got better.** The record says what was decided
  and what it rested on. Whether the ranking is *right* is not measured here.

### Configuration

All of it lives in `config/routing.yaml` under `signals` — see that file and
`app/config/schema.py::SignalsCfg` for the current defaults
(`min_samples`, `half_life_s`, `staleness_s`, `seed_window_hours`,
`latency_scale_ms`, `max_latency_norm`, `tier_latency_ms`). These are **chosen
defaults, not values tuned against production traffic.** The config lint fails
the boot if `tier_latency_ms` does not cover every canonical tier with a
positive value, or if `weights` names neither `availability` nor its deprecated
alias `capacity`.

---

## 4. The observation log — `endpoint_observations` (signal hardening, 2026-09-21)

Every observation `SignalStore` applies — success, stream success, error — is also
handed to a small `ObservationSink` (`app/routing/observations.py`) and, from there,
persisted to a new table, `endpoint_observations`. It is the source boot seeding now
replays, replacing the old `usage_records`-aggregation seed. Source of truth:
`app/routing/observations.py`, `app/apps/models.py::EndpointObservation`,
`app/apps/store.py` (`write_observations`, `load_seed_observations`,
`prune_observations`), `app/main.py` (the lifespan wiring).

### Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | int, PK, autoincrement | |
| `ts` | timestamptz, indexed | wall-clock UTC of the observation |
| `model` | str(100), indexed | model slug attempted |
| `outcome` | str(8) | `ok` \| `error` |
| `latency_ms` | float, nullable | backend-only latency; set only for a **non-streamed** `ok` |
| `streamed` | bool | whether the attempt was a stream |
| `ttft_ms` | float, nullable | time to first token; set only for a **streamed** `ok`; logged, never scored |

### What it never holds

**No application id, request id, tenant, or content.** It records endpoint health
only — the same bounded-label stance as `GET /metrics`. It carries no PII, so it needs
no redaction and no residency treatment. A cache hit never calls a backend, so it never
writes a row here — the reason a semantic-cache hit can no longer pollute a latency
estimate (see the caveat above).

### The writer: one sink, a background flusher

`SignalStore` calls `sink.put(...)` exactly once per observation it applies, so the log
can never disagree with the live signal — there is no second place deciding what counts.
`ObservationSink.put` is on the serving path: it appends to a bounded in-memory
`deque(maxlen=signals.max_buffer)`, never blocks, never raises, and does no I/O. On
overflow the oldest entry is dropped and a counter increments, logged at the next flush.

A background task started in the app's lifespan (`app/main.py`, not `create_app`'s
throwaway loop) drains the sink into the table every `signals.flush_interval_s` seconds
(default `5.0`) in one transaction; a DB failure is logged at warning and the batch is
dropped, never raised into serving. It also prunes rows older than
`signals.seed_window_hours` — first shortly after boot (on its first iteration), then
hourly (loop time); a prune failure is logged and never stops the flusher. A flusher that
dies is logged when it dies.

**A graceful shutdown flushes everything buffered.** Shutdown never cancels the flusher
mid-flush: it asks the loop to stop, waits for an in-flight write to commit (up to
`flush_interval_s + 5` s, cancelling only as a last resort), then runs one final flush.
**Only a crash loses observations — up to `flush_interval_s` of buffered ones** —
they were applied to the live signal (so the in-memory ranking already reflects them)
but never reached the table, so a subsequent boot's replay won't see them either. Small
and bounded, not zero.

### Replay seeding

At boot, `load_seed_observations` loads, per model, the most recent
`signals.max_seed_obs_per_model` rows (default `200`) within `signals.seed_window_hours`
(default `24`), concatenates across models, and orders the combined set ascending by
`(ts, id)`. `SignalStore.replay` then re-applies each row through the store's own
`observe_success` / `observe_stream_success` / `observe_error` methods, with the sink
detached (replay must not re-log what it read), at a monotonic time that preserves each
row's **true age** relative to boot (`mono_i = row.ts_wall - wall_offset`). That is what
closes the older stale-as-fresh defect — the previous seed stamped every row "now",
so a 23-hour-old average looked fresh and seeded availability had experienced zero
decay; replay ages every row correctly, so the staleness cliff and availability decay
apply to seeded state exactly as they do to live state. A seed failure is fail-soft: it
logs a warning, leaves the store empty, and never blocks serving.

Because the log holds the union of every replica's own observations (each replica writes
to the same table), the *seed* a replica boots with is a reconstruction of a state no
single replica held live — the live store stays per-replica (unchanged), but the seed is
cross-replica.

### First-boot behaviour

The table is new; `create_all` creates it, no `ALTER`. **First boot after this deploy
seeds nothing** — the log is empty. Every model reports `tier_default` (latency) /
`neutral` (availability) until it accumulates `signals.min_samples` (default `5`) live
observations, which is a numerically identical ranking to the pre-signals formula in the
meantime (the identity property, §3 above). This is expected, not a regression — see
`deploy/spark/README.md` §7.

### `usage_records` also gains error rows

Independent of the observation log, a failed request that previously wrote **no**
`usage_records` row at all now writes exactly one row with `status = "error"`: model =
the last attempted candidate, tier/provider resolved from the registry, all tokens and
cost zero, `latency_ms` = wall time to failure. `usage_summary` and
`usage_by_application` both gain `error_count`; every other figure (spend, tokens,
latency percentiles, budgets) is still computed over `status == "ok"` rows only, asserted
by test rather than assumed. No `error` row is written for a context-window overflow
(400 — the client's request, not an outage), `NoEligibleCandidate` (a policy/routing
outcome), or a client disconnect (already `cancelled`).

**The error row's `sensitivity` is only reliable on one of its three write paths.** A
non-streamed request's 502 and a keepalive stream's SSE error frame both write
`sensitivity: "-"` (the route has no gate context in scope at that failure point); only a
real-token stream's mid-generation failure records the request's true sensitivity. Don't
build a per-sensitivity error breakdown from these rows without accounting for that gap.

---

## 5. The step-7 circuit breaker (2026-09-23, B45)

`weights.availability` can only lower a failing endpoint's score by its weight, so a dead
endpoint that is cheaper than its peer keeps winning, and every request pays a wasted
attempt plus the failure latency before failover serves it. B45 measured this (research doc
§9, §11). The breaker takes such an endpoint out of rotation instead. Source of truth:
`app/gateway/breaker.py`; design:
`docs/superpowers/specs/2026-09-23-endpoint-circuit-breaker-design.md`.

### States

```
closed    --failure_threshold consecutive endpoint failures--> open       (cooldown_s)
open      --cooldown elapsed-------------------------------------> half_open
half_open --first reported success-------------------------------> closed
half_open --first reported failure-------------------------------> open       (cooldown doubled, up to max_cooldown_s)
```

- **What counts as a failure:** only what `counts_as_endpoint_unhealth` counts — transport
  errors with no status, 5xx and 429. A 4xx (including a context-window overflow) is the
  request's fault and never trips a circuit. Any success while closed resets the count.
- **Open:** the model is excluded from every routing decision until the cooldown ends.
  Results reported during the cooldown change nothing, including a success from a bypass
  (below).
- **Half-open:** once the cooldown has elapsed the model is admitted to **every** request
  again. There is no single-trial lease. The first result reported after that decides: a
  success closes the circuit and resets the cooldown; a failure reopens it with the cooldown
  doubled. **Cost to know about:** several requests in flight during the half-open window may
  each spend one attempt on an endpoint that is still dead; failover serves them, a streamed
  request fails that one call.
- **Where it acts:** the open set is the **last** stage of the one eligibility seam
  (`feasible_set` stage 7, code `circuit_open`, §2). It can only remove a candidate policy
  admitted; it cannot widen placement. `Gateway.run` and `Gateway.run_stream` pass the
  current open set to every `Router.select`, including failover and escalation re-selects.
  A streamed request has no failover, so the up-front exclusion is the only protection it
  gets.

### `circuit_bypass`

If every candidate that policy admits is open, the router does not reject the request. It
selects again without the open set, sets `circuit_bypass: true` on the router's
`DecisionRecord`, and appends `"; circuit bypass: every eligible candidate is open"` to the
decision's reason (so the Langfuse span's `sp.reason` shows it). Trying an endpoint that may
have recovered is never worse than having no breaker. The retry only restores candidates
policy already admitted: an `onprem_only` request whose on-prem models are all open still
never reaches cloud. If the set is empty for a policy reason, the request is rejected as
before.

### Per-replica state

The breaker is per process and in memory, like the signal store. Each API replica trips on
its own failures, so with N replicas an outage can cost up to N × `failure_threshold`
wasted attempts before every replica has excluded the endpoint, and replicas can disagree
about a model's state. A restart starts every circuit closed; nothing is replayed from
`endpoint_observations`.

### Admin view: the `circuit` field

`GET /admin/infra/models` adds a `circuit` object to each onboarded model. The console
Infrastructure table shows a badge next to the health badge when the state is not
`closed` ("circuit open", or "trial" while half-open):

```json
"circuit": {"state": "open", "consecutive_failures": 3, "open_until_s": 24.7}
```

- `state` — `closed`, `open` or `half_open`.
- `consecutive_failures` — the current count. While open it stays at the count that tripped
  the circuit.
- `open_until_s` — seconds until the cooldown ends, **only** while `state` is `open`;
  `null` otherwise.

It is **this replica's view**. Behind a load balancer, two reads can come from two replicas
and disagree.

### Metrics and logs

Each transition increments `routeforge_breaker_transitions_total{to_state}` (§1) and logs one
line on the `serving.breaker` logger: `circuit <model> -> <state>` plus the cooldown when
opening. `half_open` is counted once per open period.

### Configuration

`config/routing.yaml`, owned there only and checked by the config lint (a positive
threshold and cooldown, `max_cooldown_s ≥ cooldown_s`):

```yaml
breaker:
  enabled: true
  failure_threshold: 3     # consecutive endpoint failures that open the circuit
  cooldown_s: 30           # first open period; doubles on each failed half-open result
  max_cooldown_s: 300
```

**`enabled: false` is the pre-breaker behaviour exactly:** the open set is always empty and
nothing is recorded. Because `routing.yaml` is hashed into `sp.routing_policy_version`,
turning the breaker on or off changes that version on every trace.

**What it costs.** Simulated (research doc §12): on the §9 outage scenario it removed 7 of
10 wasted first attempts when requests were 1 s apart and 4 of 10 at 30 s apart, and it kept
the recovered, cheaper endpoint out for the rest of its cooldown afterwards (+51% and +39%
mean chosen cost in that scenario). An operator trades outage-time latency against
post-recovery cost through `cooldown_s` and `max_cooldown_s`.

---

## Quick verification checklist

```bash
# ① metrics render with the five families
curl -s http://<api>/metrics | grep -E '^# TYPE routeforge_'

# ② open any Langfuse trace: root span has sp.routing_policy_version and
#    sp.endpoint_registry_version; each decision step has sp.candidates
#    (chosen + rejected, each rejection carrying its `code` or null).

# ③ the same trace: each ROUTER-hop span also has sp.scoring, whose
#    `candidates` list covers every ELIGIBLE candidate from that hop's
#    sp.candidates (and none of the rejected ones). Spot-check one entry:
#    every term carries a `src`, and `latency.src` is `measured` only where
#    `n` >= signals.min_samples.

# ④ the response body: policy.scoring carries exactly ONE candidate, whose
#    `name` equals the top-level `model`, and no `candidates` list.
curl -s -X POST http://<api>/v1/chat/completions \
  -H 'Authorization: Bearer <app-token>' -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}]}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["policy"]["scoring"]; \
print(d["model"], s["name"], s["latency"]["src"], s["availability"]["src"])'
```

## Related

- `docs/guides/onboarding-models.md` — the model pool `sp.candidates` is drawn from.
- `docs/roadmap/rlhf-readiness.md` — what the candidate snapshot + policy version feed.
- `docs/architecture/routing-tier-selection.md` — the `cheap|mid|frontier` ladder
  (length-based complexity, no modality/capability axis).
- Design record: `docs/superpowers/specs/2026-08-15-routing-observability-and-signals-design.md`.
