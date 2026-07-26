# routeForge — Per-Application Admin Console (design)

**Date:** 2026-07-26
**Status:** Design — pending review
**Supersedes:** the demo-era "Live" dashboard framing

## 1. Context & problem

routeForge began as a model router, branched into a customer **demo** (the Serving
Plane, steps 1–10), and is now returning to the **real product**: a multi-tenant model
router. The current "Live" view was built as a *demo explainer* — an animated 10-step
spine that teaches the architecture. That job is done.

The concrete failure it produces today: the Live view is a **global, cross-application
firehose** with no per-app scope, and it presents the ten architecture steps as the
organizing metaphor of the whole UI. An admin can't tell whose traffic they're looking
at, and a step label like "step 5 (compression)" reads as noise because it teaches
architecture nobody asked for. Two panels (flow visualizer + decision inspector) do one
job — explain a single request — and one step (compression) is promoted to its own
panel while nine others are buried.

## 2. Audience & goals

**Primary user: the platform admin.** One login. The admin uses this console as the
**answer desk for application owners** (business users, who do not log in yet). The
three owner questions the console must answer on demand, per application:

1. **"Is my data safe?"** — PII/PHI handling, on-prem residency, egress leakage.
2. **"Why is my cost what it is?"** — spend vs. all-frontier baseline, savings levers.
3. **"Why aren't my users getting proper responses?"** — model tier chosen, escalations.

Plus the admin's own operational question: **"Is anything wrong?"** — quota hits,
failures, rejects.

## 3. Non-goals / out of scope

- **No demo surfaces.** Remove personas, traffic-generator controls, and the animated
  teaching spine from product surfaces. (The `tests/traffic/` CLI stays as seeding tooling.)
- **No Improvement Plane.** No training/eval/promotion loop in this work (see §10).
- **No business-user login.** Admin-only; owner-facing views are what the admin *shows*.
- **No inbound streaming.** Unchanged: step 9 verifies complete responses.

## 4. Information architecture — the reframe

**Organizing principle: the application is the unit.** Config and observability for one
app live in one place. Every panel that exists today becomes a *section of an app's page*
or a *drill-down from it*.

Three levels of navigation:

```
Level 0  Fleet overview        all-apps aggregate strip (fleet totals) on top, then a
         (admin home)          sortable per-application TABLE: volume, spend, savings,
                               PII/on-prem count, ⚠ attention flags. Row → drill into app.
            │
            ▼
Level 1  Application page       tabs: Overview · Cost · Privacy · Traffic/Quality
         (the answer desk)             · Requests · Policy · Tokens
            │
            ▼
Level 2  Request drill-down     one request's full journey, told at two altitudes
         (prove it / debug it)  (§6). The only place the 10 steps appear.
```

This merges today's separate **Live** (observability) and **Admin** (config) surfaces:
an application's policy/tokens and its cost/privacy/traffic now sit under the same app.

**Decision (Approach 1, user-confirmed):** unify into one app-centric console rather
than keeping a global Live monitor separate from Admin config.

## 5. The four app sections (Level 1)

Weighted by value, not by architecture step. Each answers one owner question.

| Section | Answers | Built from (step) | Contents |
|---|---|---|---|
| **Cost** | "why my cost?" | 6, 5, 10 | spend, all-frontier baseline, savings split into **routing** vs **compression** bars, trend over time |
| **Privacy** | "is my data safe?" | 2, 9 | # requests with PII (entity **types** only), # forced `onprem_only`, # kept out of cloud, # responses verified clean on egress |
| **Traffic / Quality** | "why bad responses?" | 6, 9, 4 | volume, **tier mix** (cheap/mid/frontier %), p50/p95 latency, % multi-step, escalation rate |
| **Requests** | (drill-in) | all | this app's request log; row → Level 2 |
| Policy | (config) | 3 | assigned policy shown *next to* observed behavior (proof it holds); Headroom toggle lives here |
| Tokens | (config) | — | issue/revoke API tokens |

**Fleet overview (Level 0)** — the admin's default landing. Two bands:
1. **All-apps aggregate strip** — fleet-wide totals (total spend, total saved, total
   requests, total kept on-prem) so the admin sees the whole platform at a glance.
2. **Per-application table** — same KPIs per app, one row each, **sortable** (by spend,
   savings, PII count, latency), with ⚠ attention flags. Click a row to drill into that
   app's Level 1 page. Table (not cards): dense, comparable columns scale to many apps.

## 6. The request drill-down (Level 2) — two altitudes

This is the **only** place the ten steps appear, and they appear as a **narrative of one
request**, not an architecture diagram. Because the admin drives but shows it to the
owner, the same trace is rendered at two altitudes:

- **Plain-language summary (top, owner-facing).** No step numbers, no jargon.
  *"Checked for sensitive data (none) → kept on your infrastructure → answered by a
  local model → verified clean, in 0.5s for $0.0002."* This is what the admin reads to
  the owner.
- **Technical trace (below, admin-facing).** The full journey for debugging: sensitivity
  + entity types, policy verdict, **router decision with chosen vs. rejected candidates
  and reasons** (the star), per-hop + cumulative cost, compression (if it ran),
  verification/escalation, provider eligibility (greyed-provider privacy visual), and the
  live Langfuse deep-link.

This consolidates today's flow visualizer + decision inspector + compression panel into
one view, eliminating the redundancy and the step-5 special-case.

## 7. The 10-step value weighting

What each step contributes once the demo-teaching job is gone:

- **Core (drive the aggregate sections + star of the drill-down):**
  **2** Gate → Privacy; **6** Router → Cost/Traffic + "why this model"; **9** Verify →
  Privacy (egress) + Quality; **10** Response → cost/latency headline.
- **Supporting (context/explainers, not their own screens):**
  **4** task mode (% multi-step), **5** compression (a savings number; **currently OFF**
  in the deploy — show only when it runs), **7** gateway (quota/breaker/fallback → event
  flags).
- **Fold / drop (little independent value):**
  **1** request (→ metadata: app, redacted input, requested model), **3** context (→
  drill-down policy snapshot only), **8** backend (→ a label on the chosen path).

## 8. Data availability

**Mostly already captured.** `RequestSummary` today carries: `tenant_id`, `sensitivity`,
`onprem_only`, `model`/`model_display`, `tier`, `total_usd`, `baseline_usd`,
`routing_saved_usd`, `compression_saved_usd`, `latency_ms`, `hops`, `detected_entities`,
`created_seq`. So **Cost, Privacy, and Traffic aggregates need little/no new backend
work**.

**Needs backend additions (only two places):**
1. **Verification/escalation events (step 9)** — the "why re-routed / quality" signal.
2. **Quota/breaker/fallback events (step 7)** — operational attention flags.

**Needs a small API/UI change:**
- `GET /api/requests` gains an optional `?app=<id>` filter; SSE stream filtered
  client-side by app.
- Render the app identity on each request row (data already present as `tenant_id`).

## 9. LiteLLM — gateway swap (steps 7–8)

Replace the hand-rolled `app/gateway/backends/` with **LiteLLM** (MIT core) as the
egress gateway. Three non-negotiable guardrails from the hard rules:

1. **Behind the gate (Rule 1).** Call LiteLLM as an SDK at step 7; never as the ingress
   proxy. FastAPI stays the front door.
2. **Pinned, not self-routing.** Switchyard (step 6) chose the model; call LiteLLM
   pinned to that deployment. Its own load-balancing/fallback stays off (or scoped to an
   equal, already-allowed tier) so two routers don't fight.
3. **`onprem_only` = cloud stripped by construction (Rule 3).** Hand LiteLLM a
   model-group with cloud deployments already removed for that request; filtering
   authority stays in routeForge.

Overlaps to reuse: LiteLLM per-call spend = ground-truth actual cost (two-lever
baseline/savings sits on top, no double-count); virtual-key budgets can back per-app
quotas.

## 10. Deferred (documented so they aren't relitigated)

- **LLMRouter (ulab-uiuc)** — learned step-6 router. Deferred: needs a quality/reward
  label, a Langfuse→training ETL, per-tenant training, in-boundary/redacted constraints;
  entering it = building the Improvement Plane. De-risk offline first.
- **vLLM semantic-router** — deferred to step 8 (inside the on-prem vLLM pool, below
  LiteLLM). Triggers to revisit: multiple on-prem models to choose between, or a need for
  semantic caching (with CCR-grade residency + step-9 verification).

See memory `routeforge-gateway-router-decisions.md`.

## 11. Honesty rules preserved

- Never render raw PII — entity **types** only.
- Never render CCR cache contents — counts only.
- Grey excluded providers with the reason (the privacy story).
- Both on-prem tiers are the same 7B today — label representative until a real 32B lands.
- Compression is OFF in the current deploy — show its savings only when it actually runs.

## 12. Phasing (proposed)

1. **IA + per-app scope** — app filter on `/api/requests`, app identity on rows, Fleet →
   App → Request navigation. (Mostly existing data.)
2. **Four app sections** — Cost, Privacy, Traffic/Quality from captured data.
3. **Drill-down two-altitude redesign** — consolidate flow + inspector + compression;
   plain-language summary over technical trace.
4. **Backend event exposure** — step 9 verification/escalation, step 7 quota/breaker.
5. **LiteLLM gateway swap** — steps 7–8 behind the guardrails.

## Resolved decisions

- **Step-9 verification/escalation is in scope** (phase 4) — it powers the Quality
  signal and the egress-clean count.
- **Fleet overview is a sortable table**, not cards — dense and comparable across many
  apps.
- **Global-first with drill-down** — the admin lands on an all-apps aggregate and can
  drill into any single application; both the fleet roll-up and per-app scope are
  first-class (not per-app only).
