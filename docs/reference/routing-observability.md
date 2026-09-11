# Routing observability & signals (`0.11.0`, extended through `0.18.0`)

Three shipped, operator-facing capabilities, first landed in `0.11.0` and since
extended (through the seven-feature program deployed at `0.18.0` — the stage
signals grew full role tagging, and a third always-on trace attribute
`sp.prompt_version` joined the metadata set; both noted inline below). All three
are pure Serving-Plane changes — none deploys inference, none touches the
Improvement Plane, none weakens a privacy invariant.

1. **Prometheus `/metrics`** — a scrape surface for routing behaviour.
2. **Stage/role-aware routing signals** — a flag-gated, raise-only tier bias on
   the multi-step path.
3. **Always-on candidate snapshot + routing-policy-version** — extra Langfuse
   trace metadata, emitted regardless of `capture_io`.

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

## 2. Stage/role-aware routing signals (flag-gated, raise-only)

On the **multi-step (agent) path only**, each hop can derive a *raise-only* tier
bias from what the agent loop is doing — a tool error, repeated exploration, or
an inferred planning turn — and feed it into the step-6 router. It only ever
**raises** the tier floor; it never lowers a tier, never adds a classifier, and
never overrides residency.

**Default OFF.** Baseline and demo numbers are unchanged until you flip it. The
direct (single-shot) path never sees this — no hops, no signal.

### Config — `config/routing.yaml`

```yaml
stage_signals:
  enabled: false            # master switch — flip to true to demonstrate the lever
  tool_error_raise: true    # latest tool result looks like an error → raise to mid
  exploration_repeat: 3     # same tool called ≥ N times in the run → raise to mid
  planner_role_raise: true  # gates the "plan" role specifically (see role_tiers)
  role_tiers:               # inferred role → min_tier raise; absent roles never raise
    plan: frontier          # (only raises when planner_role_raise is also true)
    verify: mid
```

### What each signal does

Source of truth: `app/routing/signals.py::derive_signal_tier`. Evaluated per hop,
all signals are combined by **strongest raise** (not first-match):

- **Inferred role** (`_infer_role`) — full role tagging from observable message
  state only, no LLM classifier: `plan` (`hop_index ≤ 1` with no tool results
  yet), `verify` (latest non-tool message matches verify/validate/check markers),
  `format` (matches format/summarize/final-answer markers), `execute` (tool
  results present, no other role matched). The role → `min_tier` raise is
  **config-driven** via `stage_signals.role_tiers`: by default `plan → frontier`
  and `verify → mid`, while `execute`/`format` are deliberately **absent** from
  the map (they never raise, leaving the cost/quality router free to pick the
  cheap tier). A role absent from `role_tiers` never raises; `plan` raises only
  when `planner_role_raise` is also true. Heuristic — the trace reason literally
  says `inferred role: <role> -> <tier>`.
- **Tool error** (latest tool message matches an error marker — word-boundary
  regex `error/exception/traceback/failed/failure`, with a negation denylist so
  "no errors" doesn't trip it) → suggests `mid`.
- **Exploration** (same tool name invoked ≥ `exploration_repeat` times) →
  suggests `mid`.

### Why it can't break privacy

The signal is passed as the existing `min_tier` argument to
`Router.select(...)`, which joins it into
`targets = max(complexity_tier, risk_floor, min_tier)` and **then** applies the
residency clamp (`app/routing/router.py`). Because the clamp runs after the max,
a signal-raised target is still capped to the best residency-permitted tier. An
`onprem_only` request whose signal says "frontier" still clamps to the best
*on-prem* tier — **the signal can never lift a request to cloud.** Same
guarantee escalation already relies on (CLAUDE.md rules 3 & 8), reused.

Wiring: `app/agent/internal_endpoint.py` calls `derive_signal_tier(...)` only
when `stage_signals.enabled` is true; otherwise it passes `min_tier=None` and the
router is untouched.

### Trace honesty

When a signal raises the tier, the hop's `DecisionRecord.reason` records why
(e.g. `stage: tool-error -> raised to mid`, `inferred role: plan (hop 1) ->
frontier`) and flows to Langfuse as `sp.reason`. "Inferred" is deliberate — role
is a heuristic and the trace says so.

---

## 3. Always-on candidate snapshot + routing-policy-version

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

# ② signals: flip on, re-run a multi-step task, watch a hop's reason string
#    (config/routing.yaml → stage_signals.enabled: true; restart not required if
#     settings reload, else redeploy). onprem_only requests must stay on-prem.

# ③ open any Langfuse trace: root span has sp.routing_policy_version;
#    each decision step has sp.candidates (chosen + rejected).
```

## Related

- `docs/guides/onboarding-models.md` — the model pool `sp.candidates` is drawn from.
- `docs/roadmap/rlhf-readiness.md` — what the candidate snapshot + policy version feed.
- `docs/architecture/routing-tier-selection.md` — the `cheap|mid|frontier` ladder
  the signal biases (length-based complexity, no modality/capability axis).
- Design record: `docs/superpowers/specs/2026-08-15-routing-observability-and-signals-design.md`.
