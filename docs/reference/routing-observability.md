# Routing observability (`0.11.0`, extended through `0.18.0`)

Two shipped, operator-facing capabilities, first landed in `0.11.0` and since
extended (through the seven-feature program deployed at `0.18.0` — a third
always-on trace attribute `sp.prompt_version` joined the metadata set, noted
inline below). Both are pure Serving-Plane changes — neither deploys inference,
neither touches the Improvement Plane, neither weakens a privacy invariant.

1. **Prometheus `/metrics`** — a scrape surface for routing behaviour.
2. **Always-on candidate snapshot + routing-policy-version** — extra Langfuse
   trace metadata, emitted regardless of `capture_io`.

> **Superseded 2026-09-17:** this page also documented `routing.stage_signals`, a
> flag-gated raise-only tier bias derived from the internal agent loop's state.
> The internal NAT agent and the multi-step path were removed, so the signal had
> no hop to observe; `config/routing.yaml`'s `stage_signals` block and
> `app/routing/signals.py` are gone. Agent loops are client-driven — the
> equivalent bias would have to be derived per conversation, which is not built.
> The removed code is recoverable from git history.

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
  {"model": "onprem-7b", "tier": "cheap", "provider": "onprem",  "eligible": true,  "reason": "..."},
  {"model": "gpt-4o",   "tier": "frontier", "provider": "bedrock", "eligible": false, "reason": "excluded · residency"}
]
```

The chosen candidate is `eligible: true`; each rejected candidate is
`eligible: false` with its rejection reason. This is the RLHF/eval substrate
(see `docs/roadmap/rlhf-readiness.md`) — it records *what else could have been chosen and
why it wasn't*, without building any feedback API (that stays out of scope).

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

## Quick verification checklist

```bash
# ① metrics render with the four families
curl -s http://<api>/metrics | grep -E '^# TYPE routeforge_'

# ② open any Langfuse trace: root span has sp.routing_policy_version;
#    each decision step has sp.candidates (chosen + rejected).
```

## Related

- `docs/guides/onboarding-models.md` — the model pool `sp.candidates` is drawn from.
- `docs/roadmap/rlhf-readiness.md` — what the candidate snapshot + policy version feed.
- `docs/architecture/routing-tier-selection.md` — the `cheap|mid|frontier` ladder
  (length-based complexity, no modality/capability axis).
- Design record: `docs/superpowers/specs/2026-08-15-routing-observability-and-signals-design.md`.
