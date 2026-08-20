# Admin Setup — Onboarding an Application

This is the administrator's path: stand up routeForge, create an application for
a developer, issue its token, and set the policy that governs how that
application's requests are routed and protected.

Everything here is done either in the **Admin dashboard** or against the
**`/admin/*` API** (same operations). All `/admin/*` calls require an admin
session — see [Admin authentication](#1-admin-authentication).

---

## 0. Prerequisites the admin controls

routeForge boots from four config YAMLs and a handful of env vars. The
onboarding-relevant ones:

**Env vars**

| Var | Purpose | Note |
|---|---|---|
| `DATABASE_URL` | Application/token/policy/usage store | Unset → local SQLite `./routeforge.db`. In a real deploy point at the shared Postgres. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Seeds the first admin login on first boot | Defaults `admin@routeforge.local` / `changeme` — **override these**. |
| `ENCRYPTION_KEY` | Fernet key encrypting per-app Langfuse secrets at rest | 32-byte / 64-hex. Unset → secrets stored as plaintext (dev only). |
| `LANGFUSE_HOST` | Langfuse ingestion host | Needed for any Langfuse feature. |
| `LANGFUSE_ORG_PUBLIC_KEY` / `LANGFUSE_ORG_SECRET_KEY` | Org-scoped keys that enable **per-app Langfuse project auto-provisioning** | Optional. Absent → all apps share the default project. |

**Config files** (one owner per concern — don't restate a value across files; the
boot-time config lint fails otherwise):

- `config/policy.yaml` — sensitivity enum, residency, budgets, the `tenants:` map.
- `config/routing.yaml` — the canonical `tiers` (`cheap | mid | frontier`), the
  `model_pool` (each model's `name` slug, `provider`, `tier`), scoring, risk floors.
- `config/pricing.yaml` — per-model USD and the savings baseline.
- `config/serving.yaml` — infra switches (`pii_detection`, provider on/off, Langfuse
  on/off, `reject_stream_requests`). On-prem models are **onboarded at runtime**, not set
  here — see [`../onboarding-models.md`](../onboarding-models.md).

> The **model slugs** live in the `ModelRegistry`: cloud built-ins from `routing.yaml`'s
> `model_pool` (e.g. `azure-gpt-mid`, `bedrock-frontier`) plus whatever on-prem models the
> operator has onboarded. Those slugs are what you reference when you narrow an app to
> specific `allowed_models`. Providers are exactly `onprem | azure | bedrock`.

> **Schema note:** the DB uses `create_all` — it creates missing tables but
> **never ALTERs** existing ones. If a routeForge upgrade adds a *column* to an
> existing table, that column must be added manually (or the table recreated) on
> an already-deployed database. New *tables* appear automatically.

---

## 1. Admin authentication

The first admin is seeded from `ADMIN_EMAIL` / `ADMIN_PASSWORD` on first boot.

```bash
# Log in — sets the rf_admin_session cookie
curl -c cookies.txt http://<host>:<port>/admin/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@routeforge.local","password":"changeme"}'
```

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/admin/login` | `{email, password}` | Sets `rf_admin_session` cookie (HttpOnly, SameSite=Lax); `{email}`. 401 on bad creds. |
| GET  | `/admin/me` | — | `{admin_id}` |
| POST | `/admin/logout` | — | Clears the session; `{ok: true}` |

The session is an opaque server-side id stored in-process (12h TTL). It is
**not** a JWT and **not** a bearer token. Sessions are lost on server restart —
log in again. In the dashboard this all happens through the login screen; pass
`-b cookies.txt` on subsequent `curl` calls below.

---

## 2. Create the application

Creating an application also **mints its first token** (returned once) and, if
org Langfuse keys are configured, **provisions a per-app Langfuse project**.

```bash
curl -b cookies.txt http://<host>:<port>/admin/applications \
  -H "Content-Type: application/json" \
  -d '{"id":"acme-support","name":"Acme Support Bot","owner_email":"dev@acme.com","default_residency":"any"}'
```

**Request — `CreateAppIn`**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `id` | string | yes | — | Slug. This **is** the tenant key: it keys usage records, traces, and the `tenants:` map in `policy.yaml`. Immutable. |
| `name` | string | yes | — | Display name. |
| `owner_email` | string | no | `null` | |
| `default_residency` | string | no | `"any"` | `any \| private_cloud_ok \| onprem_only`. Fallback when the policy row leaves residency null. |

**Response** `{ "application": AppDict, "token": "rf_live_…" }` — **copy the
`token` now; the plaintext is never shown again** (only its hash is stored).

> Match `id` to a key in `policy.yaml`'s `tenants:` map so residency defaults
> resolve cleanly (e.g. `acme`, `hospital`, `default`). If it isn't listed, the
> app falls back to `default`.

Other application endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/applications` | List all apps (with policy + tokens) |
| GET | `/admin/applications/{id}` | Get one app |
| PATCH | `/admin/applications/{id}` | Update `name`, `owner_email`, `status` (`active\|disabled`), `default_residency` |
| PUT | `/admin/applications/{id}/policy` | Upsert routing/quota policy (§4) |
| PUT | `/admin/applications/{id}/privacy` | Replace the privacy profile (§5) |

Disabling an app (`status: disabled`) makes every request with its tokens fail
closed with `401`.

---

## 3. Issue and revoke tokens

An application can hold several tokens (rotate without downtime).

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/admin/applications/{id}/tokens` | `{"name":"ci"}` (name optional, default `"default"`) | `{token, id, prefix}` — plaintext token shown **once** |
| POST | `/admin/tokens/{token_id}/revoke` | — | `{ok: true, id}` |

```bash
curl -b cookies.txt http://<host>:<port>/admin/applications/acme-support/tokens \
  -H "Content-Type: application/json" -d '{"name":"prod"}'
```

- Token format: `rf_live_<random>`; only its SHA-256 is persisted.
- Rotation: mint a new token, hand it over, then revoke the old one. No config
  change — the developer just swaps the string.
- A revoked token → `401` on the next request.

---

## 4. Set routing & quota policy

`PUT /admin/applications/{id}/policy` — a per-app policy can only **narrow** the
global rules, never widen them. Any field left `null` means "no extra
restriction from this app."

```bash
curl -b cookies.txt -X PUT \
  http://<host>:<port>/admin/applications/acme-support/policy \
  -H "Content-Type: application/json" -d '{
    "allowed_tiers": ["cheap","mid"],
    "allowed_providers": ["onprem","azure"],
    "residency": "private_cloud_ok",
    "max_spend_usd_per_day": 5.0,
    "max_rpm": 120,
    "compression_enabled": true
  }'
```

**`PolicyIn`**

| Field | Type | Meaning |
|---|---|---|
| `allowed_tiers` | list[str] \| null | Subset of `cheap \| mid \| frontier`. |
| `allowed_providers` | list[str] \| null | Subset of `onprem \| azure \| bedrock`. |
| `allowed_models` | list[str] \| null | Specific model slugs from `routing.yaml` `model_pool`. |
| `residency` | str \| null | `any \| private_cloud_ok \| onprem_only`; falls back to the app's `default_residency`. |
| `max_spend_usd_per_day` | float \| null | Daily USD cap. Over it → request rejected `429` and a `rejected` usage row recorded. |
| `max_rpm` | int \| null | Requests/minute (sliding window). Over it → `429`. |
| `compression_enabled` | bool \| null | Gate Headroom compression for this app. `null` = inherit the global toggle. Safety bypass rules still apply regardless. |

> **Residency beats everything.** `onprem_only` removes cloud providers from the
> candidate pool *by construction* before routing scores anything. When no on-prem model
> is enabled (empty pool, or every onboarded model unreachable/disabled — `ModelRegistry`
> reports no healthy on-prem), an `onprem_only` request has no candidates and is
> **rejected** — never silently sent to cloud.

---

## 5. Set the privacy profile (custom recognizers + sensitivity rules)

`PUT /admin/applications/{id}/privacy` replaces the whole profile atomically. Use
this to teach the gate a customer-specific identifier (an MRN, an account
number) and to map detected entities to a sensitivity/residency outcome.

```bash
curl -b cookies.txt -X PUT \
  http://<host>:<port>/admin/applications/acme-support/privacy \
  -H "Content-Type: application/json" -d '{
    "recognizers": [
      {"name":"ACME_ACCT","regex":"\\bACME-\\d{8}\\b","score":0.9,"enabled":true}
    ],
    "rules": [
      {"entities":["US_SSN","ACME_ACCT"],"sensitivity":"restricted",
       "risk_class":"high","residency":"onprem_only","require_verification":true}
    ]
  }'
```

**`recognizers[]`** — `name` (must match `^[A-Z][A-Z0-9_]{1,31}$`, can't collide
with a Presidio builtin), `regex` (≤512 chars, must compile, passes a ReDoS
screen), `score` (0.0–1.0, default 0.85), `enabled` (default true). Max 32.

**`rules[]`** — `entities` (each a Presidio builtin or one of your recognizer
names), `sensitivity` (`public|internal|confidential|restricted`), `risk_class`
(`low|medium|high`), `residency` (`any|private_cloud_ok|onprem_only`),
`require_verification` (default false). Max 64, ordered most-severe first.

If `rules` is empty, the app inherits the global `policy.yaml` `sensitivity_rules`.
Any rule provided overrides the global set for that app. The whole profile is
validated **before** it is persisted — a bad recognizer or unknown entity → `422`
with the first violation, and nothing is saved.

`GET /admin/applications/{id}/privacy` returns the current profile.

---

## 6. Per-application observability (optional)

If you set `LANGFUSE_ORG_PUBLIC_KEY` / `LANGFUSE_ORG_SECRET_KEY` (org-scoped),
creating an application automatically creates a **dedicated Langfuse project**
for it and stores the project's keys (secret encrypted with `ENCRYPTION_KEY`).
The developer then sees only their own traces. This is best-effort: if Langfuse
is unreachable or the org keys are absent, the app is still created and its
traces land in the shared project.

Give the developer read access to their project in Langfuse, or point them at the
`trace` array returned in every response (see the API reference).

---

## 7. Watching traffic as an admin

The console API (admin-cookie-gated) surfaces what's flowing:

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/usage` | Usage records (per-app, billing/audit) |
| GET | `/api/requests?limit=&app=` | Recent request summaries |
| GET | `/api/requests/{request_id}` | Full stored trace for one request |
| GET | `/api/stats` | Aggregate cost/latency/tier stats |
| GET | `/api/fleet` | Per-application aggregate rows |
| GET | `/api/stream` | Live decision SSE stream (the dashboard's flow visualizer) |

Usage records store metadata only (model, provider, tier, tokens, cost, latency,
sensitivity, status) — **never** prompt or answer text.

---

## Onboarding checklist

1. [ ] Override `ADMIN_EMAIL` / `ADMIN_PASSWORD`; log in.
2. [ ] Confirm `policy.yaml` `tenants:` has (or defaults cover) the app id.
3. [ ] `POST /admin/applications` → **save the returned token**.
4. [ ] `PUT …/policy` — tiers, providers, residency, budget, rpm.
5. [ ] `PUT …/privacy` — custom recognizers + sensitivity rules (if any).
6. [ ] (Optional) confirm the per-app Langfuse project was created.
7. [ ] Hand the developer: the **base URL** (`…/v1`) and the **token**. Point
       them at [`api-reference.md`](./api-reference.md).
