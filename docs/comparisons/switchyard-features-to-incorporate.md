# NeMo Switchyard — features worth incorporating into routeForge

**Source of record:** `NVIDIA_Nemotron_and_Switchyard_Report.docx` (internal, our own
write-up of Nemotron 3.5 Lightning + NeMo Switchyard). This doc pulls the *interesting,
actionable* features out of that report and maps each to a concrete routeForge decision.

**Companion doc:** [`nemo-switchyard-comparison.md`](./nemo-switchyard-comparison.md)
holds the concept-by-concept comparison and the (deferred, not rejected) "should we
replace our custom router with Switchyard?" decision. **This doc is the opposite lens:**
assuming we keep our own router, which Switchyard *features* do we borrow, and how.

> Scope reminder: routeForge is the Serving Plane and a **pure router** — it deploys no
> inference and builds no Improvement Plane. Every borrow below is filtered through the
> CLAUDE.md hard rules (gate-first, `onprem_only` absolute, residency-beats-risk-floor,
> fail-closed, CCR/cache stays in-boundary). Tiers are `cheap | mid | frontier` — there
> is **no `strong` tier**, so Switchyard's `strong_target` maps to `frontier`.

## Priority table

| # | Feature (from report) | routeForge fit | Action | Priority |
|---|---|---|---|---|
| 1 | **Escalation Router** — optimistic cheap-first run, downstream judge catches loops/drift/stall, re-runs on frontier | We verify-then-escalate at step 9; missing the *loop/drift/stall* signals and per-turn posture | **Incorporate** — add progress/loop signals to step-9 escalation | High |
| 2 | **Stage Router signals** — tool-state changes & failure flags drive tier, no judge model | Step-6 doesn't read agent-loop state as a feature | ✅ **SHIPPED (partial)** — `app/routing/signals.py`; rules-first raise-only bias, `routing.yaml` `stage_signals` (default OFF) | High |
| 3 | **Prometheus `/metrics` endpoint** — `requests_total`, `routing_decisions_total`, `routing_overhead_seconds`, `tokens_total` | We have Langfuse + DecisionRecord but no scrape endpoint or explicit router-overhead metric | ✅ **SHIPPED** — `GET /metrics` (`app/api/routes.py`), `app/obs/metrics.py` (`routeforge_*` families) | High |
| 4 | **Semantic caching** — instantly return answers for semantically duplicate subtasks | New lever; but a response cache is a second copy of sensitive data | ✅ **SHIPPED** — `app/cache/store.py`, CCR-class (residency, TTL, per-app isolation); default OFF | Medium |
| 5 | **Library-mode split** (`libsy` returns a model *name*, never holds creds/traffic) | Our step-6 is already a pure module; validates the seam | ✅ **SHIPPED** — pluggable `RoutingStrategy` Protocol + `build_strategy` (`app/routing/strategy.py`), `routing.yaml` `strategy: default` | Medium |
| 6 | **Configurable low-confidence fallback** (`strong_target`) | We escalate on verify failure, not on low routing *confidence* | **Incorporate** — fallback-to-`frontier` on low confidence, within `allowed_providers` | Medium |
| 7 | **Kong separation-of-concerns** — Switchyard picks model; Kong does PII/rate-limit/creds | This is *literally routeForge's architecture* (gate + LiteLLM = "Kong"; step-6 = "Switchyard") | **Validation, not a change** — cite as external confirmation of our seams | — |
| 8 | **Omnivorous API translation** (OpenAI Chat + Anthropic Messages + Responses inbound) | We accept OpenAI `/v1/chat/completions` only | **Consider, guarded** — must keep no-inbound-stream + gate-first | Low |
| 9 | **`routing_overhead_seconds` watch** — mid-stream judge latency can erase the cheap-model win | Directly relevant if we adopt #1/#6 | **Adopt as a guardrail** — budget the judge/verify latency | (rides with #1) |
| 10 | **Lineage:** Switchyard is the formal successor to the deprecated `llm-router` blueprint (v1/v2) | We had `LLMRouter` deferred with triggers | **Note** — the thing we deferred is now superseded; re-point the trigger | — |
| 11 | **Failure-driven model *switch* in long-running agents** — on repeated failure, pick a *different* model (not only a higher tier) | Our escalation only steps the *tier* up; no notion of switching to a different/lateral model on failure | ✅ **SHIPPED** — `app/gateway/gateway.py` lateral switch, re-selects excluding already-failed models, still within `allowed_providers` | High |
| 12 | **Task-decomposition-aware routing** — plan/roadmapping sub-tasks → frontier; execution/verification/formatting sub-tasks → cheap | Step-4 already splits direct vs multi-step; we don't route by the *role* of a sub-task within a decomposed plan | ✅ **SHIPPED** — `app/routing/signals.py` `_infer_role`/`role_tiers` raise-only bias; `routing.yaml` `stage_signals`/`role_tiers` | High |
| 13 | **LLM-as-judge** — a judge model gates the escalation decision | Step-9 verification is our judge seam; but an *LLM* judge is a routed call with cost/PII exposure | **Incorporate, as a routed call** — sanitized input, respects `allowed_providers`, emits its own DecisionRecord | Medium |

## The high-value borrows, in detail

### 1. Escalation Router → enrich our step-9 escalation with loop/drift/stall signals
The report's Escalation Router runs the cheap tier first ("optimistic execution
posture"), then a downstream judge watches for **repetitive semantic drift, circular
loop states, and script-stall exceptions**; on a hit it drops the output and re-runs on
frontier. routeForge already does verify-then-escalate at step 9 — what's missing is the
*signal set*. Add loop/drift/stall detection to the verification path and, in the
multi-step agent loop, let sustained difficulty escalate the tier for subsequent hops.

**Guardrails:** escalation moves only within `allowed_providers` — an `onprem_only` task
escalates cheap→mid→frontier-**on-prem** and is **never** lifted to cloud to satisfy a
floor (hard rule 8). Needs real per-hop signal aggregation, which is now in place — the
multi-step latency=0 bug this was gated on is resolved (`docs/backlog/bugs.md` B5,
2026-08-06); the prerequisite is satisfied, only the escalation feature itself remains
to build.

### 11. Failure-driven model *switch* (not just a higher tier)
✅ **SHIPPED** (`app/gateway/gateway.py`): on backend failure the gateway re-selects, excluding every model already failed this request, staying within `allowed_providers`.

The report's long-running-agent behaviour isn't only "escalate cheap→frontier on
failure" — on repeated failure it can pick a **different model**, not merely a more
expensive one. That's a distinct axis from our current escalation, which only steps the
*tier* up. Two models can share a tier; one may handle a task the other keeps failing.

**Incorporate** as an extension of #1: when a hop fails repeatedly, the step-6 router
should be allowed to try a *lateral* candidate (different model, same or higher tier)
before/alongside tier escalation, and record *why* the previous model was abandoned in
the DecisionRecord ("switched: repeated tool-error on `<model>`"). **Guardrails:** the
alternate candidate is still drawn from the `ModelRegistry` pool filtered by
`allowed_providers` — a failing `onprem_only` hop switches among *on-prem* models, never
to cloud. Avoid switch-thrash: cap switches per request and don't re-pick a model already
abandoned this request.

### 12. Task-decomposition-aware routing (route by the sub-task's *role*)
✅ **SHIPPED** (`app/routing/signals.py` `_infer_role`/`role_tiers`, `routing.yaml` `stage_signals`/`role_tiers`, default OFF): rules-first per-hop role inference feeds a **raise-only** tier bias — the residency clamp still caps it, so it never lifts `onprem_only` to cloud.

The report's "Strategic Division of Labor": high-level planning / roadmapping / abstract
structuring → frontier; heavy structural processing, verification routines, syntax
evaluation, and formatting → the cheap model. This is **role-aware routing** — the
routing feature is *what kind of sub-task this hop is within the decomposed plan*, not
just its raw prompt. Our step-4 task-mode router already splits direct vs multi-step; the
new part is, within a multi-step plan, tagging each hop's **role** (plan / execute /
verify / format) and feeding that role into the step-6 scorer as a feature so planner
hops bias toward `frontier` and executor/verify/format hops bias toward `cheap`.

**Guardrails:** role is a *bias*, not an override — residency and the risk floor still
win (hard rules 3, 8). A planner hop on an `onprem_only` request biases toward the best
**on-prem** frontier-tier model, never cloud. Keep role-tagging **rules-first** (derive
from the agent runtime's plan structure / tool schema), consistent with "classifier off
by default"; if a role-classifier ever becomes an LLM call, it's a routed call — see #13.

### 13. LLM-as-judge is a routed call, not a free side-channel
Both the Escalation Router (#1) and failure-switch (#11) lean on a judge that inspects
output + tool state. If that judge is an **LLM** (vs a rules check), CLAUDE.md's rule for
the step-4 classifier applies verbatim: the judge call goes through steps 5–7 like any
other — **sanitized input** (never the raw request), **respects `allowed_providers`** (so
a judge on an `onprem_only` request can't quietly run on cloud), and **emits its own
DecisionRecord** so its cost and latency aren't invisible. Watch its latency against
`routing_overhead_seconds` (#9): a slow judge erases the cheap-tier win. Prefer a
rules-based judge where the signals (loops, stalls, repeated errors) are deterministic;
reserve the LLM judge for genuinely semantic calls.

### 2. Stage Router → tool-activity as a step-6 routing feature
✅ **SHIPPED (partial)** (`app/routing/signals.py`, `routing.yaml` `stage_signals`, default OFF): agent-loop state now drives a rules-first, raise-only `min_tier` bias on the multi-step path.

Route on the agent's own state: tool errors / exploration → capable model; steady
writes/edits → efficient model. No judge model needed — purely rule-based history and
execution state. This is the most defensible borrow because it's **rules-first**, in
keeping with our "step-4 classifier off by default" stance. Feed the NAT runtime's
tool-call history into the step-6 scorer as an additional feature.

### 3. Prometheus `/metrics` endpoint
✅ **SHIPPED** (`GET /metrics` in `app/api/routes.py`, `app/obs/metrics.py`): an in-process exporter with the `routeforge_*` families below, bounded labels only, no bundled server.

The report gives exact counters worth mirroring (renamed to our prefix):
- `routeforge_requests_total{model,route_id,status}` — tier traffic comparison
- `routeforge_routing_decisions_total` — increments on escalation/route change
- `routeforge_routing_overhead_seconds` — histogram of the router's own evaluation cost
- `routeforge_tokens_total{model}` — live token/cost tracking

We already emit a DecisionRecord per step and export to Langfuse; a scrape endpoint is
additive and gives ops teams a standard surface without touching the trace pipeline.
**Privacy:** metrics are label-cardinality-only (model/tier/status) — **never** put
prompt/PII or tenant-identifying free-text in a label (egress-redaction rule 2).

### 4. Semantic caching — incorporate, but govern it like CCR
✅ **SHIPPED** (`app/cache/store.py`, default OFF): built CCR-class exactly as this section prescribes — residency-partitioned, TTL-enforced, never exported, tenant-isolated; an `onprem_only` request can never be served a cloud-produced entry. See [`semantic-cache-residency-spec.md`](../architecture/specs/semantic-cache-residency-spec.md).

Returning a cached answer for a semantically duplicate subtask eliminates redundant
inference — attractive for agent loops that re-ask similar things. **But a response
cache is a second copy of sensitive data**, exactly the risk profile of the Headroom CCR
cache. If we build it, it inherits CCR's rules verbatim (hard rules 6 & 7):
- **Residency:** cache lives inside the approved boundary, never exported to telemetry,
  enforced TTL. If residency can't be guaranteed for a tenant, **disable the cache for
  them.**
- **Per-app isolation:** cache is keyed within a single application/tenant's policy
  context. A cross-tenant cache hit would leak one customer's answer to another — treat
  that as a P0 privacy defect by construction, not a tuning concern.
- **After the gate, policy-aware:** lookup happens *inside* the trust boundary and
  respects the request's `ExecutionPolicyContext` — a cache hit must not bypass the
  policy gate or serve an answer disallowed by the current app's policy.

Given the caveats, this is Medium priority: real savings, but only worth it behind a
flag with the CCR-class guarantees wired in first.

### 6. Low-confidence fallback (`strong_target` → `frontier`)
Distinct from escalation-on-failure: when the router's *own* confidence is low
(ambiguous prompt, near-tie score), fall back to a designated high-capability model
rather than risk a low-confidence pick. routeForge already tracks routing confidence in
the DecisionRecord; add a configurable threshold in `routing.yaml` that routes
sub-threshold decisions to `frontier` — **within `allowed_providers`**, so an
`onprem_only` request falls back to the best on-prem frontier-tier model, never cloud.

### 7. Kong separation-of-concerns — external validation of our design
The report notes NVIDIA officially **decoupled model selection from traffic management**:
Switchyard does the ML intelligence (context/loops/model pick); Kong is the "network
vault" (PII sanitization, rate limits, guardrails, credentials). That split is
**exactly routeForge's architecture** — our policy gate (step 2) + LiteLLM gateway
(steps 7–8) play the Kong role, and our step-6 router plays the Switchyard role. This
isn't a change to make; it's strong outside confirmation that our seams are drawn where
a major vendor independently drew them. Worth citing to skeptics.

## What still stays out of scope

- **Prefill Router** (residual-stream complexity → trained MLP): requires training →
  Improvement Plane → out of scope (hard rule 4).
- **Nemotron 3.5 Lightning as a bundled default model** / RTX-Jetson-DGX placement:
  routeForge deploys and bundles **no** inference. Nemotron is a fine thing for an
  *operator* to onboard as an on-prem endpoint, but device placement (RTX vs DGX) is the
  serving layer's job — see [`inference-layer-boundary.md`](../architecture/decisions/inference-layer-boundary.md).
- **Adopting the Rust binary itself:** deferred — see the companion comparison doc's
  decision section (pre-alpha, Rust vs our Python/air-gapped target, and a generic router
  can't enforce our invariants).

## Suggested sequencing

1. **#3 Prometheus endpoint** — smallest, self-contained, immediate ops value.
2. **#2 Stage-Router tool signals** + **#12 role-aware routing** into step-6 — both are
   rules-first per-hop routing features, no new dependencies; land them together.
3. **#1 Escalation loop/drift signals** + **#11 failure-driven model switch** — one body
   of work (per-hop failure signals → escalate tier *and* switch model); the multi-step
   latency=0 bug this used to be gated on is resolved, so this can start unblocked.
   **#13** decides rules-based vs LLM judge here.
4. **#6 low-confidence fallback** — small config + router change once #1's escalation
   plumbing exists.
5. **#4 semantic cache** — last, behind a flag, only after CCR-class guarantees are wired.

None of these *required* the pluggable-strategy interface up front — but that interface
has since **shipped** (`app/routing/strategy.py`: `RoutingStrategy` Protocol +
`build_strategy`, selected by `routing.yaml` `strategy: default`, with the default strategy
reproducing the native scoring formula). Building the routing features (#1/#2/#6/#11/#12) as
named strategies behind that common interface (the comparison doc's low-regret move) keeps a
future swap to `switchyard-libsy` a per-strategy adapter rather than a router rewrite.
