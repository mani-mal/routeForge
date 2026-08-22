# Per-app Langfuse projects (auto-provision on onboard)

By default every application's traces land in one shared Langfuse project. RouteForge
can instead give **each onboarded application its own Langfuse project**, created
automatically when the app is onboarded. This isolates one tenant's traces from another
and is the mechanism behind the raw-capture path in non-negotiable rule 2 (an app's raw
I/O only ever goes to that app's *own* project).

This is **opt-in via environment variables** — the code is always present, but it stays
dormant until configured.

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

> **Org keys vs project keys.** `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are
> *project*-scoped and drive the shared exporter. The `_ORG_` pair is
> *organization*-scoped and is the only kind that can create new projects. They are
> different keys from a different Langfuse settings page.

## Enabling it on the DGX deployment

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
