# Design — Routing observability + smarter routing signals (`:0.11.0`)

**Status:** approved design, pre-implementation
**Date:** 2026-08-15
**Scope:** three API-image-only features, built together, shipped in one DGX redeploy at `:0.11.0`.

## Motivation

Three features surfaced from the Switchyard-incorporation review
(`docs/architecture/switchyard-features-to-incorporate.md`,
`docs/architecture/nemo-switchyard-comparison.md`) and the RLHF-readiness backlog
(`docs/rlhf-readiness.md`) as the highest-leverage, lowest-regret next steps:

1. **① Prometheus `/metrics`** — ops observability (switchyard #3). Standard scrape
   surface so operators can see routing behaviour without opening Langfuse.
2. **② Stage + role-aware routing signals** — feed agent-loop signals (tool errors,
   exploration, hop role) into the step-6 router as a *raise-only* tier bias
   (switchyard #2 Stage Router + #12 role-aware). The multi-step latency=0 bug that
   previously blocked this is fixed (B5), so per-hop signals are now real.
3. **③ Always-on candidate snapshot + routing-policy-version** — export the full
   eligible/rejected candidate set and a content-hash of the scoring config on every
   trace (rlhf-readiness F2 + F3). Lays the RLHF substrate (B16) **without** building a
   feedback API — that stays out of scope.

All three are pure Serving-Plane observability/routing changes. None touches the
Improvement Plane, none deploys inference, none weakens a privacy invariant.

## Non-goals (explicit scope guards)

- **No feedback/training pipeline.** Feature ③ *captures* the substrate; the feedback
  API and any dataset export (B16/F1) are out of scope here.
- **No new routing axis.** Feature ② biases the *existing* tier ladder
  (`cheap|mid|frontier`) via the *existing* `min_tier` parameter. It never lowers a
  tier, never adds a semantic classifier, never touches residency/risk precedence.
- **No modality/capability axis** (unchanged; see
  `docs/architecture/routing-tier-selection.md`).
- **No inbound streaming, no Improvement Plane, no inference deployment** — all
  unchanged CLAUDE.md invariants.

---

## Feature ① — Prometheus `/metrics` endpoint

### Interface

- New route `GET /metrics` on the API app (`app/api/routes.py`), returning
  `text/plain; version=0.0.4` Prometheus exposition.
- **Open, unauthenticated** — the conventional scrape posture, same as `/health`.
  Auth is a noted production concern, consistent with the decision-stream SSE posture
  (not solved here).

### Module

- New `app/obs/metrics.py` owns the metric objects and a `render() -> (bytes, content_type)`
  wrapper over `prometheus_client.generate_latest()`.
- New dependency: `prometheus_client` under an `observability` extra in `pyproject.toml`.
  Pure-Python, no C extensions — safe for air-gapped installs.

### Metrics (bounded-cardinality labels only)

| Metric | Type | Labels |
|---|---|---|
| `routeforge_requests_total` | Counter | `model, tier, provider, status` |
| `routeforge_routing_decisions_total` | Counter | `tier, escalated` |
| `routeforge_routing_overhead_seconds` | Histogram | (none) |
| `routeforge_tokens_total` | Counter | `model, direction` (`in`\|`out`) |

**Privacy rule (load-bearing):** labels are model/tier/provider/status/direction only —
all bounded-cardinality enums or model ids. **No prompt text, no PII, and no
`tenant`/`application_id`.** Per-tenant rollups live in RouteForge's own store (B15),
never in Prometheus label cardinality.

### Instrumentation points

- `routeforge_requests_total` + `routeforge_tokens_total`: in `app/pipeline.py` where a
  request completes, so both direct and multi-step paths increment.
- `routeforge_routing_overhead_seconds`: wrap the `router.select()` call site(s).
- `routeforge_routing_decisions_total` (+ `escalated`): at the per-attempt routing
  record in `app/gateway/*` (Gateway.run appends one router record per attempt; an
  escalation is a second attempt → `escalated="true"`).

### Testing

- A completed request increments `requests_total` and `tokens_total`.
- `/metrics` renders parseable exposition with the four metric families present.
- **Assert no PII/tenant labels**: scan rendered output, fail if any label key is
  `tenant`/`application_id` or any label value looks like free text.

---

## Feature ② — Stage + role-aware routing signals (flag-gated, raise-only)

### Config

New block in `config/routing.yaml` (routing.yaml owns routing concerns):

```yaml
stage_signals:
  enabled: false            # default OFF — baseline/demo numbers unchanged
  tool_error_raise: true    # a tool error in the latest result → raise one tier
  exploration_repeat: 3     # same tool called ≥N times → raise one tier
  planner_role_raise: true  # inferred "plan" hop → bias toward frontier
```

Added to `RoutingCfg`. Default-off means the config lint and every existing test see
the unchanged router.

### Signal derivation (pure, testable)

New `app/routing/signals.py`:

```python
def derive_signal_tier(hop_ctx, cfg) -> str | None:
    """Return a raise-only min_tier ('mid'|'frontier') or None.
    Pure function of the hop's observable state — no NAT internals."""
```

Inputs come entirely from what `app/agent/internal_endpoint.py` already has per hop:
the incoming `messages` (including tool results), `tools`, whether the prior hop emitted
`tool_calls`, and `hop_index`.

- **Stage signal (grounded):**
  - latest incoming tool result contains an error marker → raise one tier.
  - same tool invoked `≥ exploration_repeat` times across the run → raise one tier.
- **Role signal (heuristic, labelled as such):**
  - `hop_index == 0/1` with no tool results yet → inferred role `"plan"` → bias toward
    `frontier`.
  - a hop that emits `tool_calls` → `"execute"` → no raise.
  - final synthesis hop (no tool call) → `"format"` → no raise.

The function returns the **highest** raise the signals request, or `None`.

### Router wiring (raise-only by construction)

`Router.select(ctx, tokens_after, complexity, min_tier)` already accepts `min_tier` and
joins it into `targets = max(complexity_tier, risk_floor, min_tier)` at
`app/routing/router.py:99-103`. The signal feeds through **this existing parameter**.

Because the residency clamp runs *after* the max (router.py:104-109), a signal-raised
target is still capped to the best residency-permitted tier. Concretely: an
`onprem_only` request whose signal says "frontier" still clamps to the best *on-prem*
tier — **the signal can never lift a request to cloud.** This is the same guarantee
escalation already relies on (CLAUDE.md hard rule 8), reused, not re-implemented.

Wiring site: `app/agent/internal_endpoint.py` currently calls
`model_router.select(ctx, tokens_after, complexity, None)`. When
`routing.stage_signals.enabled` is true, replace the `None` with
`derive_signal_tier(hop_ctx, cfg)`. When false, it stays `None` — **direct path is
never touched** (no hops → no stage/role signal there).

### Trace honesty

When a signal raises the tier, the hop's `DecisionRecord.reason` records why, e.g.
`"stage: tool-error → raised cheap→mid"` or `"inferred role: plan (hop 1) → frontier"`.
The word "inferred" is deliberate: role is a heuristic and the trace says so. This flows
to Langfuse via the existing `sp.reason` attribute (export.py:91-92).

### Testing

- stage: tool-error result → raise; `exploration_repeat` reached → raise.
- role: hop 1, no tools yet → plan → raise; hop with tool_calls → no raise.
- **raise-only:** a signal never lowers a tier below `max(complexity, risk_floor)`.
- **residency clamp still wins:** `onprem_only` + frontier signal → chosen candidate is
  on-prem (never cloud).
- **flag-off regression:** with `enabled: false`, `select()` receives `min_tier=None`
  and every existing routing test is unchanged.

---

## Feature ③ — Always-on candidate snapshot + routing-policy-version

### F2 — `sp.candidates`

`DecisionRecord` already carries `chosen` (str) and `rejected: list[RejectedCandidate]`
(name/provider/tier/display_name/reason) — models.py:84-89 — and these already flow in
the `trace` dicts passed to `export_trace`. The per-step export loop (export.py:82-95)
just doesn't emit them.

Change: in that loop, serialize an always-on `sp.candidates` attribute per step:

```json
[
  {"model": "...", "tier": "mid", "provider": "onprem",  "eligible": true,  "reason": "tier mid (target mid)"},
  {"model": "...", "tier": "frontier", "provider": "bedrock", "eligible": false, "reason": "excluded · residency"}
]
```

`chosen` → `eligible:true`; each `rejected` entry → `eligible:false` + its reason.
Values are model ids / enums / fixed reason strings — no PII — so this is emitted
**regardless of `capture_io`** (it is decision metadata, not request I/O), consistent
with the rlhf-readiness always-on rule. Run defensively through `redact_value` like the
existing `sp.chosen` for future-proofing.

### F3 — `sp.routing_policy_version`

A short content hash (first 12 hex of sha256) over the **canonicalized**
`routing.yaml` + `pricing.yaml` raw dicts — the scoring weights, thresholds, tiers,
model pool, and prices that determine a routing decision.

- Computed once in `app/config/loader.py::load_settings` from `routing_raw` +
  `pricing_raw` (canonical JSON with sorted keys → stable across boots), stored on
  `Settings` (new field, e.g. `routing_policy_version: str`).
- Stamped on the **root span** in `export_trace` as `sp.routing_policy_version`
  (alongside `sp.request_id` etc., export.py:66-75).
- Automatic — no manual semver. Editing a scoring weight or a price changes the hash;
  identical config across two deploys yields the same hash, so a later feedback/eval
  pipeline can group decisions by the exact policy that produced them.

### Testing

- `sp.candidates` shape: a decision with 1 chosen + N rejected serializes to N+1
  entries with correct `eligible` flags and reasons.
- policy version: stable across two `load_settings` of the same config; changes when a
  value in routing.yaml or pricing.yaml changes; unaffected by serving.yaml/policy.yaml
  edits.
- both attributes present even when `capture_io == "off"`.

---

## Data flow summary

```
request → gate → (direct | multi-step)
  ① metrics:   pipeline/gateway increment counters + observe overhead histogram
  ② signals:   [multi-step, flag-on] internal_endpoint → derive_signal_tier
                 → router.select(min_tier=signal) → residency clamp caps it
                 → DecisionRecord.reason explains the raise
  ③ export:    export_trace stamps sp.routing_policy_version (root)
                 + sp.candidates per step (from DecisionRecord.chosen+rejected)
GET /metrics → app/obs/metrics.render() → prometheus_client.generate_latest()
```

## Invariant checklist (CLAUDE.md)

- **Gate-first:** unchanged — all three act at/after step 6.
- **`onprem_only` absolute / residency beats risk floor:** ② feeds `min_tier` which is
  clamped by the residency filter that runs first; a signal can only raise within the
  residency-permitted set. Verified by test.
- **Fail-closed:** unchanged; signals only bias tier, never bypass the gate.
- **Deploys/promotes nothing:** ③ captures metadata; no promotion, no training.
- **Telemetry redacted on egress:** ③ emits enums/model-ids/reason strings, run through
  `redact_value`; no I/O widening. ① labels carry no PII.

## Deployment (`:0.11.0`, after all three land + reviewed)

Own vLLM on the DGX (RouteForge's own model, 1 of 3 free H200s — see decision), full
stack recreate:

1. Recreate `runai-routeforge` ns + secrets.
2. Langfuse + Postgres (`deploy/dgx_lab/langfuse.yaml`).
3. RouteForge's own vLLM (`deploy/dgx_lab/vllm-local-8b.yaml`, Qwen2.5-7B).
4. Build + push `ghcr.io/mani-mal/serving-plane-{api,ui}:0.11.0` (classic PAT, isolated
   DOCKER_CONFIG).
5. API + UI (`deploy/dgx_lab/routeforge-app.yaml`) at `:0.11.0`.
6. Onboard `local-8b` via `POST /admin/infra/models`.
7. Smoke-test:
   - `/metrics` scrapes and shows the four families.
   - toggle `routing.stage_signals.enabled = true`, re-run a multi-step task → a
     planner/tool-error hop visibly raises tier, reason string shows why.
   - a Langfuse trace shows `sp.candidates` (chosen + rejected) and
     `sp.routing_policy_version`.
   - invariant re-check: an `onprem_only` request with the signal on still stays
     on-prem.
