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

### The four metric families

Source of truth: `app/obs/metrics.py`.

| Metric | Type | Labels |
|---|---|---|
| `routeforge_requests_total` | Counter | `model`, `status` |
| `routeforge_routing_decisions_total` | Counter | `tier`, `escalated` |
| `routeforge_routing_overhead_seconds` | Histogram | *(none)* |
| `routeforge_tokens_total` | Counter | `model`, `direction` (`in`\|`out`) |

- `status` values you'll see: `ok`, `unauthorized`, `rate_limited`,
  `budget_exceeded`, `rejected`, `error`. For pre-routing rejects `model` is `-`.
- `escalated="true"` marks a routing decision that was a second (or later)
  attempt — i.e. a fallback/escalation, not the first-choice candidate.
- `routeforge_routing_overhead_seconds` times `Router.select()` only — router
  wall-time, not end-to-end latency.

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

Every other rejection — residency, budget, tier, score, app scope — has `code: null`.
The codes are never fabricated: a rejection with no code of its own gets `null`, not a
guess. See `docs/reference/per-app-config.md` for the rules themselves.

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
| **HTTP response** — `policy.scoring` in the decision certificate | the **chosen candidate only**, plus `weights` and `signals_enabled` | the response is on *every* chat completion and has to stay small |

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
`json.dumps`/`json.loads` round-trip. Note the *response* form is deliberately
not replayable: it carries one candidate and omits `latency_scale_ms` /
`max_latency_norm`. Replay is a trace property.

### The four provenance values

`src` on `latency` and `availability`:

| `src` | Means |
|---|---|
| `measured` | an EWMA over at least `signals.min_samples` observed calls, not stale. `n` is the observation count. |
| `tier_default` | **latency only.** No usable measurement — fewer than `min_samples` observations, or the newest one is older than `signals.staleness_s`. The value is `signals.tier_latency_ms[tier]`. `n` still reports the true count, which may be non-zero. |
| `neutral` | **availability only.** Fewer than `min_samples` observations; the value is `1.0`, which is exactly the pre-P2a hardcoded constant. |
| `disabled` | `signals.enabled: false`. Tier default / `1.0`, and `n` is reported as `0` even when observations exist (see the caveat below). |

A stale latency falls back to the tier default **outright** rather than drifting
toward it: a drifted number would be neither measured nor default and could not
be labelled honestly.

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
  health gate**; it can never make a candidate ineligible, and failover remains
  the only recovery mechanism. If you need a failing endpoint out of rotation,
  disable it (`PUT /admin/infra/models/{slug}` with `enabled: false`) — do not
  expect the signal to do it. Raising `weights.availability` is not advised
  without measurement: it trades against cost and quality on every request, not
  only on requests involving a sick endpoint.
- **Per-replica drift is real.** The signal store is per-process: each API
  replica learns from its own traffic, so two replicas can hold different
  latency estimates for the same model and rank the same eligible set
  differently. This is acceptable because a signal is a *ranking preference*
  only — it reaches the strategy, which can reorder the eligible set but can
  never add to or remove from it, so drift cannot affect eligibility, residency,
  or cost caps. Boot seeding from `usage_records` (last
  `signals.seed_window_hours`) bounds the divergence. Cross-replica sharing is
  deliberately not built.
- **Streaming feeds no observations.** `observe_success` / `observe_error` are
  called only from `Gateway.run()`, never from `run_stream()`. A
  streaming-only application therefore **contributes nothing to the signal**.
  Whether its endpoints *report* a default depends on the rest of the
  deployment: the store is keyed per **model**, not per application, so those
  same endpoints go `measured` as soon as any *other* application sends
  non-streaming traffic to them. It is a single-tenant or
  streaming-only deployment whose endpoints stay at `tier_default` / `neutral`
  indefinitely. Either way the streaming request's own scoring record is still
  emitted; it just may carry only defaults. The omission is deliberate:
  recording streaming successes without failures would bias the signal, and
  time-to-last-token is not the same quantity as a non-streaming call's
  latency. Absent is honest; biased would not be.
- **Seeded availability is always `1.0`.** No code path writes
  `usage_records.status = 'error'` — a failed request raises before
  `record_usage` is reached, so no row is written at all. Latency seeds
  correctly from `ok` rows; availability does not seed. After a restart a
  badly-failing endpoint starts neutral and needs `min_samples` live
  observations to be down-weighted again. Tracked as a backlog item.
- **`n` is `0` on the `disabled` path** even when the store holds observations,
  unlike `tier_default`/`neutral`, which report the true count. Also backlogged.
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

## Quick verification checklist

```bash
# ① metrics render with the four families
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
