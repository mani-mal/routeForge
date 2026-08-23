# Per-app Langfuse projects (shared, manual, or auto-provision)

By default every application's traces land in one shared Langfuse project. RouteForge
can instead give **each onboarded application its own Langfuse project**, created
automatically when the app is onboarded. This isolates one tenant's traces from another
and is the mechanism behind the raw-capture path in non-negotiable rule 2 (an app's raw
I/O only ever goes to that app's *own* project).

This is **opt-in via environment variables** — the code is always present, but it stays
dormant until configured.

## Which path should I use? (pick one)

Three ways to route this deployment's traces, in increasing setup cost. Most
deployments want **shared** or **manual**; **auto** is a convenience that costs an
Enterprise license.

| Path | What you get | Setup | Needs EE? |
|---|---|---|---|
| **Shared project** (default) | Every app's traces in one project, still redacted on egress; all privacy rules hold. | Nothing. | No |
| **Manual per-app** | Each app's traces in **its own** project — full tenant isolation, and the only way to enable the raw-capture path (rule 2). | Per app, once: create a project in Langfuse, then paste its **project** keys into the app (Console → Applications → *Langfuse credentials*, or `PUT /admin/applications/{id}/langfuse`). | No |
| **Auto-provision** | Same per-app isolation as manual, but the project is created **automatically** on onboard — no per-app step. | One-time: set an org-scoped key pair (`LANGFUSE_ORG_*`) once. | **Yes** — org keys / project creation are EE-only on self-hosted. |

**How to choose:**

- Don't need to separate one tenant's traces from another? → **Shared.** Zero setup, fully
  functional, telemetry still redacted.
- Need per-tenant trace isolation, or want an app to opt into raw I/O capture (rule 2)?
  → **Manual per-app.** Isolation is byte-for-byte identical to auto; you just wire each
  app by hand once. **This is the recommended path on OSS Langfuse.**
- Onboarding apps often enough that the manual step is real friction, and you already run
  (or will license) Langfuse Enterprise? → **Auto-provision.**

The manual and auto paths are **mutually exclusive per app** — an app either has keys you
set by hand or keys the provisioner minted, never both. Switching a deployment from OSS to
EE later doesn't disturb apps already wired manually.

> ## ⚠️ Requires Langfuse Enterprise Edition (self-hosted)
>
> Auto-provision creates projects through Langfuse's admin API, which on **self-hosted
> Langfuse is an Enterprise-Edition feature**. Both routes to it —
> **organization-scoped API keys** (basic-auth `pk-lf`/`sk-lf` on `POST
> /api/public/projects`, which is what this provisioner uses) and the **`ADMIN_API_KEY`
> Org/Instance Management API** — are gated behind an EE license. On OSS Langfuse there
> is **no** way to create a project programmatically, and the org settings page shows
> only *General / Members / Projects* — **no "API Keys" tab** (that tab is what the EE
> license adds).
>
> - **To use auto-provision:** set `LANGFUSE_EE_LICENSE_KEY` on **both** Langfuse
>   containers (web + worker), then create an org-scoped key pair under **Organization →
>   Settings → API Keys** and configure it below. See
>   <https://langfuse.com/self-hosting/license-key>.
> - **Without an EE license (OSS):** use the **manual per-app path** instead — create a
>   project by hand in the Langfuse UI (OSS supports this), then assign its project keys
>   to the app via `PUT /admin/applications/{app_id}/langfuse`. Same per-app trace
>   isolation; just not automatic on onboard. See *Manual fallback* below.
> - **Or** leave it off: every app shares the default project (fully functional).

## How many orgs / projects do I create? (the mental model)

**One organization, configured once. RouteForge creates the per-app projects for you.**

- You create **one** Langfuse **organization** and generate **one** organization-scoped
  API key pair from it. You set that pair on RouteForge **once** (the env vars below).
- From then on, **every** application you onboard gets its **own new project created
  automatically** under that same org — named after the app, tagged
  `routeforge_app=<app_id>`. RouteForge calls the Langfuse admin API to make it.

So, concretely:

- **Do NOT** create a new organization per application. One org for the whole
  RouteForge deployment is enough.
- **Do NOT** manually pre-create a project per application (for the auto path). The
  provisioner makes a fresh project per app itself; a project you hand-create is simply
  ignored by auto-provision.
- A new application therefore needs **nothing** created by hand — onboard it and its
  project appears. The only setup is the one-time org + org keys.

> If you *want* an app to use a specific project you created by hand (e.g. an
> air-gapped site that provisions projects out of band), that's the **manual** path
> instead — `PUT /admin/applications/{app_id}/langfuse` with that project's own keys.
> The manual and auto paths are mutually exclusive per app.

## What happens on onboard

`POST /admin/applications` (create/onboard an app) calls
`LangfuseProvisioner.provision()` (`app/apps/langfuse_provision.py`), which, when
configured:

1. `POST {LANGFUSE_HOST}/api/public/projects` (org-scoped auth) → creates a Langfuse
   project named after the app, tagged `metadata.routeforge_app = <app_id>`.
2. `POST {LANGFUSE_HOST}/api/public/projects/{id}/apiKeys` → mints a project-scoped key
   pair for that app.
3. Persists `langfuse_project_id`, `langfuse_public_key`, and (Fernet-encrypted)
   `langfuse_secret_key_enc` on the `applications` row.

From then on, that app's traces export to its own project, and its console deep-links
point there (`app/obs/export.py`). It is **fail-safe**: if provisioning fails or is
unconfigured, app creation still succeeds and the app falls back to the shared project
(the provisioner logs a warning and returns `None`). It is also **fail-closed** for
raw capture: an app requesting `capture_io=full` without a provisioned project is
downgraded to `redacted` on export.

A manual fallback exists for air-gapped operators who provision projects out of band:
`PUT /admin/applications/{app_id}/langfuse` sets the same four columns directly.

## Environment variables

| Var | Required for auto-provision | Purpose |
|---|---|---|
| `LANGFUSE_HOST` | yes | Server-reachable Langfuse API base the provisioner calls (in-cluster, e.g. `http://langfuse-web:3000`). Also used by the shared trace exporter. |
| `LANGFUSE_ORG_PUBLIC_KEY` | yes | **Organization**-scoped public key (not a project key). Langfuse → Organization → Settings → API Keys. |
| `LANGFUSE_ORG_SECRET_KEY` | yes | Organization-scoped secret key (pairs with the public key above). |
| `ENCRYPTION_KEY` | recommended | Fernet key that encrypts each app's provisioned secret key at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Unset → plaintext (dev only). **Never rotate after apps are provisioned** — their stored secrets become undecryptable. |

All three of `LANGFUSE_HOST` + `LANGFUSE_ORG_PUBLIC_KEY` + `LANGFUSE_ORG_SECRET_KEY`
must be present together (`LangfuseProvisioner.is_configured()`); any missing → the
provisioner no-ops and apps share the default project.

> **Org keys vs project keys — the #1 gotcha.** `LANGFUSE_PUBLIC_KEY` /
> `LANGFUSE_SECRET_KEY` are *project*-scoped and drive the shared exporter. The `_ORG_`
> pair is *organization*-scoped and is the **only** kind that can create new projects.
> They look alike (both `pk-lf-…` / `sk-lf-…`) but come from **different settings
> pages**:
>
> - **Project** keys: Project → Settings → API Keys. Scoped to that one project. Can
>   read/write traces; **cannot create projects**.
> - **Organization** keys: **Organization → Settings → API Keys** (the org-level page,
>   not inside any project). These are what `LANGFUSE_ORG_*` must be.
>
> If you configure *project* keys by mistake, the provisioner's create call returns
> **HTTP 403 `Invalid API key. Organization-scoped API key required for this
> operation.`** — and every onboarded app silently falls back to the shared project. To
> check a pair before wiring it:
>
> ```bash
> curl -s -u "$PK:$SK" -X POST "$LANGFUSE_HOST/api/public/projects" \
>   -H 'content-type: application/json' -d '{"name":"scope-check"}' -w '\nHTTP=%{http_code}\n'
> # 200 + a project id  -> org-scoped (good; delete the scope-check project after)
> # 403 "...required..." -> project-scoped (wrong; get keys from the ORG settings page)
> ```

## Enabling auto-provision on the DGX deployment (requires Langfuse EE)

The Langfuse provisioner is already baked into the running image; enabling it is an
env-only change (no image rebuild, no DB migration — the `applications.langfuse_*`
columns already exist).

1. Create an organization-scoped key pair in the Langfuse UI (Organization → Settings →
   API Keys) and add both to the existing secret:

   ```bash
   kubectl -n runai-routeforge patch secret langfuse-demo-creds --type merge -p \
     "{\"stringData\":{\"LANGFUSE_ORG_PUBLIC_KEY\":\"pk-lf-org-...\",\"LANGFUSE_ORG_SECRET_KEY\":\"sk-lf-org-...\"}}"
   ```

2. Add the three env vars to the `api` container (`LANGFUSE_HOST` plain;
   the org keys from the secret) and roll:

   ```bash
   kubectl -n runai-routeforge set env deploy/routeforge-api \
     LANGFUSE_HOST=http://langfuse-web:3000
   kubectl -n runai-routeforge set env deploy/routeforge-api \
     --from=secret/langfuse-demo-creds \
     --keys=LANGFUSE_ORG_PUBLIC_KEY,LANGFUSE_ORG_SECRET_KEY
   kubectl -n runai-routeforge rollout status deploy/routeforge-api
   ```

   (`ENCRYPTION_KEY` is already wired from `langfuse-demo-creds`.)

3. Verify: onboard a throwaway app and confirm it got its own project.

   ```bash
   # via the API (admin-authed) or the console Infrastructure → Applications page
   curl -sX POST .../admin/applications -H 'cookie: <admin session>' \
     -d '{"id":"prov-smoke","name":"Provision Smoke"}'
   # then check the row picked up a project id:
   kubectl -n runai-routeforge exec deploy/langfuse-postgres -- psql -U postgres -d postgres \
     -tc "SELECT id, langfuse_project_id, langfuse_public_key FROM applications WHERE id='prov-smoke';"
   ```

   A non-null `langfuse_project_id` means provisioning worked; a new project also
   appears in the Langfuse org. Revoke/delete the throwaway app afterward.

The manifest comments in `deploy/dgx_lab/routeforge-app.yaml` carry the same wiring for
`kubectl apply`-based deploys.

## Manual per-app setup (OSS, no Enterprise license)

This is the path when Langfuse is OSS (no `LANGFUSE_EE_LICENSE_KEY`), so auto-provision
isn't available. You create the project by hand and assign its **project**-scoped keys
to one app. Per-app trace isolation is identical; it's just manual per app. No org keys,
no `LANGFUSE_ORG_*` env, no rollout.

Per application:

1. In the Langfuse UI, create a project (or reuse one) and copy its **project** API keys
   (Project → Settings → API Keys): `pk-lf-…` / `sk-lf-…`. Note the **project id** — the
   `id` from `GET {host}/api/public/projects` (org- or project-key auth both list it),
   e.g. `cmt5…`.

2. Assign them to the app via `PUT /admin/applications/{app_id}/langfuse` (admin-authed).
   `host` is the **server-reachable OTLP ingestion** host (in-cluster
   `http://langfuse-web:3000` on the DGX) — *not* the browser URL; the console deep-link
   uses the shared public URL with this app's project id automatically.

   ```bash
   curl -sX PUT "$API/admin/applications/<app_id>/langfuse" -b "$ADMIN_COOKIE" \
     -H 'content-type: application/json' -d '{
       "project_id":"cmt5...","public_key":"pk-lf-...",
       "secret_key":"sk-lf-...","host":"http://langfuse-web:3000"}'
   ```

   The secret is encrypted at rest (`ENCRYPTION_KEY`). From then on that app's traces
   export to its own project; a request with no per-app creds still uses the shared one.

3. Verify: send one request as that app and confirm a trace lands in the project.

   ```bash
   kubectl -n runai-routeforge exec deploy/langfuse-postgres -- psql -U postgres -d postgres \
     -tc "SELECT id, langfuse_project_id, langfuse_host FROM applications WHERE id='<app_id>';"
   # then, using that project's own keys, confirm the trace arrived:
   curl -s -u "$PK:$SK" "$LANGFUSE_HOST/api/public/traces?limit=1"
   ```

To move an app back to the shared project, `PUT` the same endpoint with the fields
cleared (or delete/recreate the app).
