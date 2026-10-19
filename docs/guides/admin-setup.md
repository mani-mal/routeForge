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
  here — see [`onboarding-models.md`](./onboarding-models.md).

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
| GET  | `/admin/me` | — | `{admin_id, deployment: {...}}` — session identity plus the deployment's read-only policy envelope (below) |
| POST | `/admin/logout` | — | Clears the session; `{ok: true}` |

**`GET /admin/me` response**

```json
{
  "admin_id": "adm_…",
  "deployment": {
    "default_policy_mode": "enforced",
    "default_pii_detection": "presidio",
    "default_capture_io": "redacted",
    "reject_stream_requests": true,
    "allow_app_passthrough": true,
    "allow_app_pii_downgrade": true
  }
}
```

| Field | Source in `config/serving.yaml` | Why it's here |
|---|---|---|
| `default_policy_mode` | `policy.default_policy_mode` | The posture a per-app `policy_mode: null` inherits. |
| `default_pii_detection` | `policy.pii_detection` | The detector a per-app `pii_detection: null` inherits. |
| `default_capture_io` | `observability.langfuse.capture_io` | The capture mode a per-app `capture_io: null` inherits. |
| `reject_stream_requests` | `server.reject_stream_requests` | Whether inbound `stream: true` is refused for an app that never opted in. |
| `allow_app_passthrough` | `policy.allow_app_passthrough` | Deployment **ceiling**: `false` and no app may resolve to `policy_mode: passthrough`. |
| `allow_app_pii_downgrade` | `policy.allow_app_pii_downgrade` | Deployment **ceiling**: `false` and no app may set `pii_detection` to `off`/`regex`. |

The `deployment` block exists because a per-app field left `null` inherits a deployment
default, and a control (or a script) that renders that as a bare "global default" says
nothing useful — this lets the console name the value actually being inherited. The two
ceilings decide whether a posture is reachable *at all*, so without them a client can
only offer `passthrough` and let the write 422.

**It is deliberately read-only.** These values live in the operator's `serving.yaml`
ConfigMap and constrain what an admin may do; an admin-editable ceiling would not be a
ceiling. There is no endpoint to change them — edit the ConfigMap and restart. Nothing
in the block is per-tenant or secret: it is the same envelope every app on the
deployment sits inside.

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
| PUT | `/admin/applications/{id}/langfuse` | Set per-app Langfuse credentials manually (§6) |

Disabling an app (`status: disabled`) makes every request with its tokens fail
closed with `401`.

---

## 3. Issue and revoke tokens

An application can hold several tokens (rotate without downtime).

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/admin/applications/{id}/tokens` | `{"name":"ci", "expires_in_s":3600, "session":"attempt-42"}` (all optional) | `{token, id, prefix, expires_at, session}` — plaintext token shown **once** |
| POST | `/admin/tokens/{token_id}/revoke` | — | `{ok: true, id}` |

```bash
curl -b cookies.txt http://<host>:<port>/admin/applications/acme-support/tokens \
  -H "Content-Type: application/json" -d '{"name":"prod"}'
```

- Token format: `rf_live_<random>`; only its SHA-256 is persisted.
- Rotation: mint a new token, hand it over, then revoke the old one. No config
  change — the developer just swaps the string.
- A revoked token → `401` on the next request.
- `expires_in_s` (optional, 1 to 2 592 000 = 30 days): the token is refused with
  `401` from that many seconds after creation, exactly like a revoked token, and
  is listed with `status: "expired"`. Omitted = never expires.
- `session` (optional, 1–128 chars of `[A-Za-z0-9._:-]`): the token is refused
  with `401` on any call whose `x-routeforge-session` header is missing or
  different. Omitted = the header is not checked (it still groups requests).

**Upgrading an existing database.** These three `app_tokens` columns are new and
RouteForge does not migrate (`create_all` adds the new `service_credentials` table
but never alters an existing one). **Run these before rolling the image**: the ORM
names every column when it looks a token up, so against an unmigrated table every
`/v1/*` request fails authentication.

```sql
ALTER TABLE app_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE app_tokens ADD COLUMN IF NOT EXISTS session_binding VARCHAR(128);
ALTER TABLE app_tokens ADD COLUMN IF NOT EXISTS issued_by VARCHAR(64);
```

Existing tokens keep all three null: no expiry, no session binding, admin-issued.

### Service credentials (machine-issued, short-lived tokens)

A **service credential** lets a system — for example a CI runner or an agent
platform that starts one sandbox per task — create and revoke tokens for **one**
application without an admin session. It can do exactly two things:

- `POST /admin/applications/{id}/tokens` for its own application (`403` for any
  other application), with the same optional `expires_in_s` and `session`;
- `POST /admin/tokens/{token_id}/revoke` for tokens **it created** (`404` for
  any other token, including the admin's).

Every other `/admin/*` route answers it with `401`, and it is not an app token:
`/v1/*` refuses it. Send it as `Authorization: Bearer rf_svc_…`.

| Method | Path (admin cookie) | Body | Response |
|---|---|---|---|
| POST | `/admin/applications/{id}/service-credentials` | `{"name":"ci"}` (optional) | `{credential, id, prefix, application_id}` — plaintext shown **once** |
| GET | `/admin/applications/{id}/service-credentials` | — | list, without the secret |
| POST | `/admin/service-credentials/{cred_id}/revoke` | — | `{ok: true, id}` — also revokes every token it issued |

Expiry and session binding are checked when a request authenticates; a request
already in flight (for example a long stream) finishes.

```bash
# admin: issue a service credential for one app
curl -b cookies.txt http://<host>:<port>/admin/applications/acme-support/service-credentials \
  -H "Content-Type: application/json" -d '{"name":"agent-platform"}'

# the service: one token per task, bound to the task id, expiring with it
curl http://<host>:<port>/admin/applications/acme-support/tokens \
  -H "Authorization: Bearer $RF_SERVICE_CREDENTIAL" -H "Content-Type: application/json" \
  -d '{"name":"task-42","expires_in_s":5400,"session":"task-42"}'

# the task's calls carry the session header
curl http://<host>:<port>/v1/chat/completions -H "Authorization: Bearer $TOKEN" \
  -H "x-routeforge-session: task-42" -H "Content-Type: application/json" -d '{...}'

# the service: revoke when the task's workspace is gone
curl -X POST http://<host>:<port>/admin/tokens/$TOKEN_ID/revoke \
  -H "Authorization: Bearer $RF_SERVICE_CREDENTIAL"
```

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
| `capture_io` | str \| null | `off \| redacted \| full` telemetry I/O capture. `null` = inherit global. `full` **always** needs the app's own Langfuse project, and is refused for an `onprem_only` app whose effective `policy_mode` is `enforced` — set `policy_mode: passthrough` to declare the app unprotected. `redacted` is refused wherever nothing is sanitized (`passthrough`, or `pii_detection: off`) — `pii_detection: regex` still sanitizes (at lower fidelity than `presidio`) so it pairs fine with `redacted`. See [`../reference/per-app-config.md`](../reference/per-app-config.md). |
| `policy_mode` | str \| null | `enforced \| passthrough` — the **protection** posture. `null` = inherit `policy.default_policy_mode`. `enforced` runs detection, redaction, escalation and the capture ceiling; `passthrough` makes RouteForge a router, meter and tracer only. Refused when the deployment sets `allow_app_passthrough: false`. **It never touches placement** — it cannot widen `allowed_providers` or clear `onprem_only`, and `onprem_only` + `passthrough` is a valid configuration. |
| `pii_detection` | str \| null | `off \| regex \| presidio` per-app override of the detector. `null` = inherit global (not a downgrade). An explicit `off`/`regex` is refused unless the deployment sets `allow_app_pii_downgrade`. Only `off` is additionally refused in combination with `capture_io: redacted` (`regex` is allowed there). It is **allowed** for an `onprem_only` app (detection and residency are independent axes) — but an `onprem_only` + `enforced` app with `pii_detection: off` needs `capture_io: off`. |
| `pii_entity_denylist` | list[str] \| null | Entity **types** the gate detects but does not redact for this app. Presidio built-in names only; an unknown name is a 422. `US_SSN` can never be named. No deployment ceiling — this is a narrowing, not a downgrade. |
| `pii_score_threshold` | float \| null | `[0.0, 1.0]`. Scored entities below this confidence are dropped before redaction. Never applies to `US_SSN`; never drops unscored (regex / custom-recognizer) hits. To remove a stored floor entirely, name it in `reset`; `0` is a different thing — a floor that passes every score. |
| `pii_exempt_patterns` | list[str] \| null | Regexes `fullmatch`ed against a detected entity's **value**, whatever type it was given — the type-independent complement to the denylist. Max 25 patterns, 200 chars each, each must compile under Python `re`. Never exempts `US_SSN`. |
| `stream_mode` | str \| null | `off \| verified_then_stream \| stream_unverified \| incremental_verify`. `null`/`off` = reject inbound `stream:true`. `stream_unverified` is refused for `onprem_only` apps at write time; `onprem_only`/`require_verification` requests always fall back to the verified path at runtime. See [`../reference/per-app-config.md`](../reference/per-app-config.md). |
| `reset` | list[str] \| null | Policy field names to write back to `NULL` ("inherit the deployment default"), applied **after** the merge — the only way to clear a field, since `null` in the body means "leave it alone". An unknown name is a 422 (never silently ignored), and naming a field here *and* sending a value for it in the same request is a 422 contradiction. Every clamp above is judged on the **post-reset** effective state, so a reset that would produce a forbidden posture is refused whole. |

> **The PUT merges — `null` does not clear; `reset` does.** The body is persisted with
> `exclude_none=True`, so an omitted field *or an explicit JSON `null`* keeps the app's
> currently stored value. To clear a list send `[]`; to return any field to "inherit the
> deployment default" name it in `reset`, e.g.
> `{"reset": ["policy_mode", "pii_score_threshold"]}`. Every clamp above is evaluated on
> the *effective* (post-merge, post-reset) state, so a partial PUT is judged on what the
> write will actually produce — clearing `policy_mode` on an `onprem_only` app that is
> storing `capture_io: full` is refused, because the app would land back in the
> `enforced` posture while still capturing raw I/O. Full detail in
> [`../reference/per-app-config.md`](../reference/per-app-config.md).
>
> **All of the above is also settable in the console** — *Application → Settings tab*,
> since `ui:0.17.0`. The tab is six numbered steps: **1 Access** (tokens), **2 Placement**,
> **3 Protection**, **4 Telemetry** (the app's Langfuse credentials, then `capture_io`),
> **5 Delivery and limits**, **6 Privacy rules**; steps 2–5 all write through this one
> PUT. Same endpoint, same clamps; the console shows the server's 422 verbatim. This API
> remains the scriptable path.

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

**Manual credentials** — `PUT /admin/applications/{id}/langfuse` sets an app's
Langfuse host + key pair by hand (secret encrypted at rest with `ENCRYPTION_KEY`).
Use it in an air-gapped deploy that can't reach the org-scoped provisioning API.
Once set, the app has its own residency-local project, which is what makes it
eligible for `capture_io: full` (raw I/O goes only to that project, never the
shared one).

---

## 7. Watching traffic as an admin

The console API (admin-cookie-gated) surfaces what's flowing:

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/usage` | Usage records (per-app, billing/audit) |
| GET | `/admin/usage/by-application` | Per-application usage rollup (backs the cross-app usage report) |
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
       them at [`api-reference.md`](../reference/api-reference.md).
