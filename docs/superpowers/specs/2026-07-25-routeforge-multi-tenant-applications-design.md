# Design: routeForge as a multi-tenant model-router product

**Date:** 2026-07-25
**Status:** Draft for review (no implementation until approved)
**Author:** Mani + Claude

---

## 1. Context & the reframe

routeForge was built and demoed as a "Serving Plane prototype," which made it *look*
like a self-contained test harness. It is not — the core is already a real model
router:

- It exposes an **OpenAI-compatible `/v1/chat/completions`** endpoint. Any application
  points the OpenAI SDK at it with `base_url` + a bearer token and gets a routed
  response. No routeForge SDK required.
- A **bearer token already maps to a "tenant"** (`app/main.py` →
  `API_KEYS = {"acme-key": "acme", …}`), each tenant carries policy, and every Langfuse
  trace is already tagged `tenant_id`.
- **"Tenant" is the same concept as "application."** `acme` / `hospital` are example
  applications, thinly configured.
- The **traffic generator + personas** (`tests/traffic/`) are **test tooling** that
  *simulate* many client apps so the UI would move during a demo. They are not users of
  the product. Their presence in the UI is the main source of the "is this a test app?"
  confusion.

This spec productizes that core into a multi-tenant router: real application/token
management, per-application policy, durable usage metering, an admin dashboard, and
per-application observability. **The routing engine, privacy gate, compression, and
output controls are not changed** — they already sit behind the right seams.

### Relationship to CLAUDE.md

CLAUDE.md frames the repo as a "demo-grade prototype of the Serving Plane only." This
work is a deliberate scope expansion from *demo* to *product*, still confined to the
Serving Plane (the Improvement Plane remains out of scope). **All non-negotiable rules
still hold and are preserved:** policy gate runs first, telemetry redacted on egress,
`onprem_only` absolute, no auto-deploy, fail closed, CCR residency, compression after the
gate, residency beats the risk floor. Nothing here weakens them; per-application policy
only ever *narrows* what a request may do.

---

## 2. Goals & non-goals

### Goals
- An application can be registered, issued API tokens, and call routeForge like OpenAI.
- A routeForge **admin** configures, per application: allowed models/providers,
  residency, and budget/rate caps.
- The admin views **usage per application** (requests, tokens, model/tier mix, cost,
  latency) over time in a dashboard.
- Each **application owner** sees only their own traces/cost in a **dedicated Langfuse
  project**.
- Unknown/missing/revoked tokens are **rejected** (fail closed), replacing today's
  fail-open `→ default tenant`.

### Non-goals (this phase)
- Multi-node / HA scaling, self-service external signup, full RBAC (chosen scale:
  **internal / few trusted apps**).
- App owners logging into the *routeForge* dashboard (chosen access: **admin-only**;
  owners view their data via their Langfuse project).
- The Improvement Plane (training/eval/promotion) — permanently out of scope.
- Billing/invoicing (usage metering yes; monetization no).

---

## 3. Locked decisions (from review)

| Axis | Decision | Consequence |
|---|---|---|
| Scale | **Internal / few trusted apps** | Single deployment, one datastore, simple admin auth |
| Data store | **Reuse the Langfuse Postgres** | routeForge gets its own tables in the existing in-cluster Postgres |
| Langfuse isolation | **Project per application** | routeForge provisions a Langfuse project + keys per app; owners see only theirs |
| Dashboard access | **Admin-only, built-in login** | One operator role; app owners get observability via Langfuse only |

---

## 4. Target architecture

```
        ┌─────────────┐   rf_live_… (bearer)   ┌───────────────────────────────┐
 App A ─┤ OpenAI SDK  ├───────────────────────►│  routeForge /v1/chat/…         │
        └─────────────┘                        │                                │
        ┌─────────────┐                        │  1. token → application (hash) │
 App B ─┤ OpenAI SDK  ├───────────────────────►│     fail closed on miss        │
        └─────────────┘                        │  2. per-app policy → gate/route│
                                               │  3. record usage (Postgres)    │
   Admin ─► routeForge dashboard (login) ──────┤  4. export trace → app's       │
             apps · tokens · usage             │     Langfuse project           │
                                               └───────────┬───────────────────┘
                                                           │
                          ┌────────────────────────────────┴───────────┐
                          ▼                                             ▼
                 Postgres (shared w/ Langfuse):              Langfuse projects
                 applications, app_tokens,                   project-per-app:
                 app_policies, usage_records,                App A owner ─► proj A
                 admin_users                                 App B owner ─► proj B
```

---

## 5. Data model (new tables in the Langfuse Postgres)

A dedicated schema (e.g. `routeforge`) keeps our tables separate from Langfuse's. Access
via SQLAlchemy async + `asyncpg`. Pydantic models at the boundary, per project
convention.

### `applications`
| Column | Type | Notes |
|---|---|---|
| `id` | text (slug) PK | e.g. `acme`; stable, used as `application_id` in traces |
| `name` | text | Display name |
| `owner_email` | text | For reference / Langfuse project membership |
| `status` | enum(`active`,`disabled`) | Disabled → all tokens rejected |
| `langfuse_project_id` | text null | The app's Langfuse project (Phase 5) |
| `langfuse_public_key` | text null | Project pk (for the owner) |
| `langfuse_secret_key_enc` | bytea null | Project sk, **encrypted at rest** (reuse `ENCRYPTION_KEY` pattern) |
| `created_at` | timestamptz | |

### `app_tokens`
| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `application_id` | fk → applications | |
| `token_hash` | text unique | **sha256(token)**; plaintext never stored |
| `prefix` | text | Display only, e.g. `rf_live_a1b2…` |
| `name` | text | Human label ("prod", "staging") |
| `status` | enum(`active`,`revoked`) | |
| `created_at` / `last_used_at` / `revoked_at` | timestamptz | |

Token format: `rf_<env>_<32+ random chars>`. Multiple tokens per app (rotation without
downtime). Resolution: sha256 the incoming bearer, look up an `active` token whose app is
`active`; otherwise **reject 401**.

### `app_policies` (1:1 with application)
| Column | Type | Notes |
|---|---|---|
| `application_id` | fk PK | |
| `allowed_tiers` | text[] | subset of `cheap|mid|frontier` |
| `allowed_providers` | text[] | subset of `onprem|azure|bedrock` |
| `allowed_models` | text[] null | optional finer grain; null = all in allowed tiers/providers |
| `residency` | enum | `any|private_cloud_ok|onprem_only` — app default; request rule can only make it *stricter* |
| `max_spend_usd_per_day` | numeric null | budget cap; null = unlimited |
| `max_rpm` | int null | rate limit; null = unlimited |

### `usage_records`
| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `application_id` | fk (indexed) | |
| `request_id` | text | correlates to the Langfuse trace |
| `ts` | timestamptz (indexed) | |
| `model` / `provider` / `tier` | text | chosen path |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | int | |
| `cost_usd` / `baseline_usd` | numeric | |
| `latency_ms` | numeric | |
| `sensitivity` / `onprem_only` | text / bool | |
| `status` | enum(`ok`,`rejected`,`error`) | rejections (policy, quota) recorded too |

This is the **durable** replacement for the in-memory `HistoryStore`
(`app/api/history.py`), which becomes a small hot cache in front of it. Enables per-app
historical dashboards that survive restarts. **Never stores prompt/answer text** — same
redaction guarantees as today (types-only, no PII, no CCR contents).

### `admin_users`
`id`, `email`, `password_hash` (argon2/bcrypt). Seeded with one admin at bootstrap.

---

## 6. Token lifecycle & auth (fail-closed)

- **Issue:** admin creates a token → server generates `rf_live_…`, stores `sha256`,
  returns plaintext **once**.
- **Use:** `Authorization: Bearer rf_live_…` → `sha256` → lookup → application + policy.
- **Miss / revoked / app disabled → 401**, never `default`. (Fixes the current
  `_tenant_of` fail-open in `app/api/routes.py`.)
- **Rotate:** issue a new token, hand it over, revoke the old — no downtime.
- **Revoke:** flip `status`; effective immediately (cache TTL ≤ a few seconds).

---

## 7. Per-application policy & enforcement

Wire into the existing seam. `build_context` (`app/policy/context.py`) already computes
`allowed_providers` from global `serving.providers.enabled` ∩ residency ∩ pool. Change:

1. Intersect additionally with the **application's** `allowed_providers` /
   `allowed_models`.
2. Residency = **stricter of** the request's entity rule and the app's default (unchanged
   `_stricter` logic, now app-sourced).
3. Budget/latency caps come from the app's policy instead of the global
   `policy.yaml` defaults.

Enforcement additions (fail closed):
- **Rate limit** (`max_rpm`): in-memory sliding window per app, rejected → `429`.
- **Daily budget** (`max_spend_usd_per_day`): check running `usage_records` sum (cached),
  rejected → `429` with a clear reason. Recorded as a `rejected` usage row.
- **Empty allowed set** after intersection (e.g. `onprem_only` app on a `none` backend) →
  the existing reject path; never silently routed.

Config ownership: infra stays in the four YAMLs; **per-application** policy lives in the
DB (admin-editable at runtime). The config lint is unaffected.

---

## 8. Per-application Langfuse (validated)

**Finding (probed against the live v3 image, 2026-07-25):** `POST /api/public/projects`
exists and is gated behind an **organization-scoped API key** (returns `401 "No
authorization header"`, i.e. a real route needing org auth — not a 404). `GET
/api/public/projects` confirms the `demo-org`/`demo-project` structure. So auto-provision
is supported by our image; the only one-time setup is generating an org-scoped key in the
Langfuse org settings and giving it to routeForge.

Flow:
1. On application creation, routeForge calls the org API: **create project** → then
   **create project API keys** (`pk`/`sk`), storing them on the `applications` row
   (`sk` encrypted).
2. Add the owner's email to that project (org API), so the owner can log in and see only
   their traces.
3. Per request, export the decision trace using **that application's project keys**
   (per-app OTLP exporter, cached by `application_id`) instead of one global exporter.

**Sub-decision deferred to Phase 5:** the current global `TracerProvider`
(`app/obs/otel.py`, registered process-wide so NAT agent spans share it) can't be swapped
per concurrent request. Plan: route **per-request decision traces** to the app's project
first (covers the "owner sees their data" need); route **NAT full-trajectory** spans to
the app project as a follow-up using OTel context propagation rather than a global
provider. If org-API provisioning is ever unavailable, fallback = admin pre-creates
projects and pastes keys (still per-app isolation, not one-click).

---

## 9. Admin dashboard (evolve the current UI)

Add top-level nav to the existing React app; keep the three-column flow view as one tab.

- **Applications** — list; create/edit (name, owner, allowed tiers/providers/models,
  residency, budget, rate); enable/disable; **tokens** sub-panel (create → show once,
  rotate, revoke); link to the app's Langfuse project.
- **Usage** — filter by application + date range; charts: requests over time, cost,
  model/tier breakdown, tokens, p50/p95 latency, on-prem count, rejections. Fed by
  `usage_records` aggregation endpoints.
- **Debug / Live flow** — the existing 10-step clickable flow visualizer + decision
  inspector, now scoped/filterable by application. This is the per-request trace view.
- **Auth** — built-in admin login (session cookie); all admin routes + the dashboard
  behind it. Serving API (`/v1/*`) stays token-authed and separate.

New backend surface (`app/api/admin_routes.py`): CRUD for applications/tokens/policies,
usage queries. Separate from the OpenAI-compatible serving routes.

---

## 10. Client integration & reframing the traffic tool

- **Reference client** (`examples/client/`): a minimal script showing "admin issues token
  → app calls with OpenAI SDK." Documented in a new `docs/help/integrating-an-app.md`.
- **Traffic generator** stays as an internal **load/seed** tool, relabeled as such in its
  README and the UI guide, so it's never mistaken for the product's user model. It can
  seed usage against real registered apps.

---

## 11. Phased plan

| Phase | Delivers | Size |
|---|---|---|
| **0** | This spec, reviewed + committed | S |
| **1** | DB layer (SQLAlchemy async) + `applications`/`app_tokens`/`usage_records`/`admin_users`; token→app resolution replacing `API_KEYS`, **fail-closed**; usage persisted (HistoryStore → cache); bootstrap importing the 3 demo apps | M |
| **2** | `app_policies` + per-app policy wired into `build_context`; budget + rate enforcement (429) | M |
| **3** | `admin_routes.py` + built-in admin auth; CRUD apps/tokens/policy; usage-aggregation endpoints | M |
| **4** | Admin dashboard UI (Applications, Usage tabs) + flow visualizer scoped as debug view | L |
| **5** | Per-app Langfuse project provisioning (org API) + per-app decision-trace export; `application_id` on traces | M |
| **6** | Reference client + `integrating-an-app` docs; relabel traffic generator | S |

Each phase is independently shippable and leaves the serving core working. TDD per
project convention; config lint stays green.

---

## 12. Risks & open sub-decisions

- **Langfuse org-API provisioning (Phase 5)** — de-risked (route exists); remaining work
  is generating a one-time org-scoped key and confirming the key-creation sub-endpoint.
  Fallback documented above.
- **NAT trajectory per-app export** — deferred within Phase 5 (see §8).
- **`model` field semantics** — apps send `model: "auto"` (router decides) or an explicit
  allowed tier/model. Recommend supporting both: `"auto"` default; an explicit value must
  be in the app's allowed set or it's rejected. *Decide before Phase 2.*
- **Secret-at-rest encryption** — reuse the existing `ENCRYPTION_KEY` for app Langfuse
  `sk` and any sensitive columns. No new secret management.
- **Migration** — introduce a lightweight migration tool (Alembic) for the `routeforge`
  schema, or manage DDL at startup. *Decide in Phase 1.*

---

## 13. Security considerations

- Tokens hashed at rest; plaintext shown once; fail-closed resolution.
- Admin API and serving API are separate surfaces with separate auth.
- Per-app policy can only *narrow* the global privacy rules — never widen them; all
  CLAUDE.md hard rules preserved.
- Usage records keep the redaction guarantee (types only, no PII, no CCR contents).
- Per-app Langfuse projects isolate observability data between owners.
- Rate/budget caps protect against a single app exhausting the on-prem backend.

---

## 14. Testing approach

- **Phase 1:** token resolution (hit/miss/revoked/disabled → fail closed); usage
  persistence round-trips; HistoryStore-cache consistency.
- **Phase 2:** per-app allowed-set intersection (incl. `onprem_only` reject); budget/rate
  429 paths; residency-beats-risk-floor still holds per app.
- **Phase 3:** admin auth gate; CRUD invariants; usage aggregation correctness.
- **Phase 5:** project provisioning against a test Langfuse; per-app trace lands in the
  right project and nowhere else; zero raw PII (reuse `tests/test_obs_wiring.py`).
- Existing suite (`test_two_lever_cost`, `test_traffic_honesty`, config lint) stays green.
