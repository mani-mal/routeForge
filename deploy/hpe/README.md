# routeForge on HPE Private Cloud AI (PCAI)

Deploys the whole demo — the routeForge **API + UI** *and* a self-hosted
**Langfuse v3** stack (Postgres, ClickHouse, Redis, MinIO) — as **one** custom
framework, imported through PCAI's **Tools & Frameworks → Import Framework**
wizard. No SSH into the cluster is required; everything here runs from your
workstation (Docker + Helm + network reachability to Harbor).

```
deploy/hpe/
├── README.md                 ← you are here
├── icon.svg                  ← upload at the wizard's "Framework Icon" step
├── scripts/
│   ├── build-and-push.sh     ← tag + push api/ui images into PCAI Harbor
│   └── package.sh            ← helm lint + package → routeforge-<ver>.tgz
└── routeforge/               ← the Helm chart (single bundle)
    ├── Chart.yaml
    ├── values.yaml           ← edit at the wizard's "Framework Values" step
    └── templates/
        ├── ezua/             ← PCAI integration: Kyverno + VirtualServices + SSO
        ├── api.yaml  ui.yaml  serving-config.yaml  ui-nginx-config.yaml
        ├── app-secret.yaml  registry-secret.yaml  ccr-pvc.yaml
        └── langfuse-datastores.yaml  langfuse-app.yaml
```

## What makes this PCAI-compatible

The `templates/ezua/` directory carries what the platform's service mesh and
policy controls require of an imported chart:

- **`kyverno.yaml`** — a `ClusterPolicy` (pre-install hook) that mutates every
  Pod in the release namespace to add `hpe-ezua/type: vendor-service`. PCAI
  rejects workloads without it.
- **`virtualservice-ui.yaml`** — Istio `VirtualService` binding the console to
  the `istio-system/ezaf-gateway` at `${RELEASE_NAME}-${NAMESPACE}.${DOMAIN_NAME}`.
- **`virtualservice-langfuse.yaml`** — a second endpoint for the Langfuse UI so
  the "open trace" deep-link works in a browser.
- **`authorizationpolicy.yaml`** — optionally puts the console behind PCAI SSO
  (oauth2-proxy). **Off by default** for this shareable-demo bundle (see
  "Logging in" below): routeForge's own admin login already gates all data, and
  SSO-on would force every viewer to have a PCAI account. Set
  `ezua.authorizationPolicy.enabled: true` for a locked-down deploy.

PCAI substitutes `${DOMAIN_NAME}`, `${RELEASE_NAME}`, `${NAMESPACE}` and the
`${OIDC_*}` tokens in `values.yaml` at import time — leave those tokens as-is.

## One-time prep (from your workstation)

### 1. Push the images into PCAI's Harbor

The api/ui images must live in a registry PCAI can pull from. The built-in
Harbor is the standard choice.

1. In PCAI, open Harbor (Tools & Frameworks) and **create a project** named
   `routeforge` (Public is simplest for a demo; Private needs pull creds — see
   step 3 below).
2. From your workstation:

   ```bash
   HARBOR=harbor.ingress.<your-pcai-domain> \
   PROJECT=routeforge \
   TAG=0.2.0 \
   SRC_REGISTRY=ghcr.io/mani-mal \
   deploy/hpe/scripts/build-and-push.sh
   ```

   (`SRC_REGISTRY` is where the images are pulled from first — the same
   `ghcr.io/mani-mal/serving-plane-*:0.2.0` you run on DGX. Omit it to push
   images you already built locally.)

Find `<your-pcai-domain>` in the console URL / Harbor endpoint, e.g.
`harbor.ingress.pcai0104.ld7.hpecolo.net` → domain is
`ingress.pcai0104.ld7.hpecolo.net`.

### 2. Package the chart

```bash
deploy/hpe/scripts/package.sh
# → deploy/hpe/routeforge-0.2.7.tgz
```

> **Bump the chart version on every re-upload.** PCAI stores uploaded charts in an
> internal ChartMuseum keyed by the `version:` in `Chart.yaml`. Re-uploading the
> **same** version silently keeps the *old* chart — your fixes won't apply. Either
> bump `version:` (e.g. `0.2.1` → `0.2.2`) before each upload, or delete the stuck
> version first (see "Iterating" below).

### 3. Set the values you must change

Edit these in `routeforge/values.yaml` (or in the wizard's Values step):

| Value | Set to | Needed when |
|---|---|---|
| `imageCredentials.enabled` + `imageCredentials.password` | `true` + a `read:packages` **GHCR classic PAT** | always (GHCR is private) |
| `image.registry` | `ghcr.io/mani-mal` (default) — or `harbor.ingress.<domain>/routeforge` for Harbor | only to switch off GHCR |
| `global.storageClassName` | your PCAI StorageClass | if no cluster default |
| `thirdPartyRegistry` | Harbor mirror prefix | **air-gapped** PCAI (no internet for langfuse/postgres/etc.) |
| `credentials.adminPassword` | a real password | always (demo default is `changeme`) |

Everything else is demo-safe as shipped: `inferenceBackend: mock` (CPU-only, no
GPU), Presidio PII detection, compression off.

## Import (in the PCAI console — matches the wizard steps)

1. **Framework Details** — Name `routeForge`; a short Description; Category
   (any — e.g. *Data Science* / *Gen AI*); **Framework Icon**: upload
   `deploy/hpe/icon.svg`.
2. **Framework Chart** — upload `routeforge-0.2.7.tgz` from step 2. Set **Release
   Name** to a plain DNS label like `routeforge` (no dots — `0.1` is rejected by
   `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`). Leave **Wait** unchecked (the Langfuse cold
   start can exceed the wait timeout and get marked failed); **Debug** on is handy.
3. **Framework Values** — paste/confirm your edited `values.yaml`.
4. **Review** — submit. PCAI installs the Helm release into its namespace and
   wires the gateway.

After submit, the console shows the app; open it at
`https://routeforge-<namespace>.<domain>`. Langfuse's first-boot DB migration
takes a few minutes before traces appear.

## Verified working install (DC15 trial, 2026-07-27)

The bundle came up `2/2` for both `routeforge-api` and `routeforge-ui` on the
**PCAI Hosted Trial DC15-S170-R109** (`pcai0109.dc15.hpecolo.net`). Facts to reuse:

- **Namespace: `project-user-mani-malarvannan`.** Install/debug here — it is the
  notebook's own namespace, so the notebook terminal's `kubectl` can `get/patch/logs`
  everything (the `default-editor` SA has **no** cross-namespace rights, and cannot
  list cluster-scoped `ezappconfigs` at all — use the console tile to delete a
  framework, not `kubectl delete ezappconfig`).
- **Images pull from private GHCR** (`ghcr.io/mani-mal/serving-plane-{api,ui}:0.2.0`)
  with a pull secret built from a **classic** `read:packages` PAT. A fine-grained
  `github_pat_...` token 401s on GHCR — must be a classic `ghp_...`.
- Both container-start fixes are baked into `values.yaml` `securityContext`
  (`api.runAsNonRoot: false` for the image's named `USER app`; the UI keeps default
  caps so stock nginx can `chown` its cache). Chart **0.2.7**.

### Logging in (three separate auth layers — don't mix them up)

| Door | Where | Credential |
|---|---|---|
| PCAI SSO (oauth2-proxy) | in front of the UI host, **only if** `ezua.authorizationPolicy.enabled: true` | your **PCAI account** (same as the console home page) — *not* the admin creds below |
| **routeForge admin login** | the UI console app itself (`/admin/login`) | **`credentials.adminEmail` / `adminPassword`** — default `admin@routeforge.local` / `changeme` |
| Langfuse | the separate `…-langfuse-…` URL | same `adminEmail` / `adminPassword` (Langfuse-seeded) |

The **routeForge admin login is the shareable demo credential.** The static UI
loads without it, but every `/api/*` call (including the SSE decision stream)
returns 401 until you log in — so it protects all data even with SSO off.
`changeme` is a demo default; **change `credentials.adminPassword` before
sharing the URL.**

The admin user is seeded into the shared Langfuse Postgres from
`ADMIN_EMAIL`/`ADMIN_PASSWORD` **idempotently** — if an earlier install already
seeded a row, changing `adminPassword` later does **not** update it. To reset,
change the password *and* delete the existing `admin_users` row (or drop the
seeded admin) so the next API start re-seeds.

**If `/admin/login` returns 500 (not 401)** with `asyncpg InvalidPasswordError:
password authentication failed for user "postgres"` in the API logs, and a
`psql` shows `relation "admin_users" does not exist` — that's the **env-ordering
bug fixed in chart 0.2.5**. In `api.yaml`, `DATABASE_URL` referenced
`$(POSTGRES_PASSWORD)` while `POSTGRES_PASSWORD` was declared *after* it;
Kubernetes only expands `$(VAR)` for vars declared *earlier*, so the API
connected with the literal string `$(POSTGRES_PASSWORD)` → auth fail → no
`create_all`, no seed. 0.2.5 reorders `POSTGRES_PASSWORD` before `DATABASE_URL`.
On a pre-0.2.5 deploy, patch it in place (bakes the real password into the URL):
```bash
NS=project-user-mani-malarvannan
PGPW=$(kubectl get secret routeforge-creds -n "$NS" -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
kubectl set env deployment/routeforge-api -n "$NS" \
  DATABASE_URL="postgresql+asyncpg://postgres:${PGPW}@langfuse-postgres.${NS}.svc.cluster.local:5432/postgres"
```

Note the SSO `AuthorizationPolicy` lives in `istio-system`, so a namespace-scoped
notebook SA can neither add nor remove it — toggling SSO requires a re-import
(the wizard runs with platform privileges).

### Filling the dashboard (traffic generator)

- **In-app (easiest):** log into the console → **Traffic** section → **Run
  burst**. The `/api/traffic` endpoint fires the seeded 500-request persona mix
  *inside the API pod* (the `tests/traffic` generator ships in the image). No
  CLI, no repo — prospects can click it themselves.
- **CLI (same tool, from the PCAI notebook — in-cluster, bypasses SSO):**
  ```bash
  NS=project-user-mani-malarvannan
  python -m tests.traffic --requests 500 --rps 10 --seed 42 \
    --base-url http://routeforge-api.$NS.svc.cluster.local:8080
  ```
  Prints tier split (~70% cheap), cost vs all-frontier baseline, on-prem count,
  p50/p95. `--seed 42` is reproducible.

### Browser login fails (502 or 426) — the UI nginx proxy

The API can be perfectly healthy (in-cluster `curl …/admin/login` → `200`) while the
**browser** login fails, because the request also traverses the UI's nginx reverse
proxy. Two config bugs lived in `templates/ui-nginx-config.yaml`; both are fixed in
**chart 0.2.7**. If you're on an older running install, patch the ConfigMap in place
(then `kubectl rollout restart deployment routeforge-ui`):

- **502 Bad Gateway**, nginx log `no resolver defined to resolve routeforge-api…svc…`
  — `proxy_pass` used `http://<host>$request_uri`. The `$request_uri` **variable**
  flips nginx into per-request DNS resolution, which needs a `resolver` directive it
  didn't have. **Fix:** bare `proxy_pass http://<host:port>;` (no `$request_uri`) —
  nginx forwards the URI automatically and resolves the name once at startup. A fresh
  UI pod does NOT fix this (it's not a stale IP); the config is the bug.
- **426 Upgrade Required**, `server: istio-envoy` — nginx defaults upstream
  connections to **HTTP/1.0**, and the API Pod's Istio sidecar (Envoy) rejects
  HTTP/1.0 with 426. **Fix:** add `proxy_http_version 1.1;` (and
  `proxy_set_header Connection "";`) to **every** proxied location, not just
  `/api/stream`.

Diagnose by comparing hops: in-cluster `curl` **direct to the API service** speaks
HTTP/1.1 → `200`; the same request **through the UI nginx** exposes the proxy bug.
The nginx error line (`kubectl logs -l app.kubernetes.io/component=ui -c ui`) names
the exact cause.

### Why this worked on DGX but cost a debug session on HPE

Both nginx bugs above are invisible on the DGX/Run:ai deploy
(`deploy/dgx_lab/routeforge-app.yaml`) and only bite on PCAI. The difference is two
environment deltas, not the app:

| | DGX / Run:ai | HPE PCAI | Consequence |
|---|---|---|---|
| **Cluster DNS in nginx** | `resolver 10.150.0.10` **hardcoded** + `proxy_pass $up$request_uri` (variable form, works *because* a resolver is present) | HPE chart **dropped the resolver** (CoreDNS ClusterIP differs per install, didn't want to hardcode it) but **kept `$request_uri`** → variable with no resolver | DGX: fine. HPE: **502 `no resolver defined`**. The HPE config half-converted variable→literal and left the `$request_uri`. |
| **Service mesh** | **none** — nginx connects straight to uvicorn | **Istio sidecar (Envoy) injected into every pod** (mandatory on PCAI) | Both configs send HTTP/1.0 to `/api//v1//admin/` (neither sets `proxy_http_version 1.1` there — DGX doesn't either!). DGX: uvicorn accepts HTTP/1.0 → **200**. HPE: Envoy rejects HTTP/1.0 → **426**. |

Takeaways so this doesn't recur:
- **The 426 is latent on DGX.** DGX's own `/api/ /v1/ /admin/` locations lack
  `proxy_http_version 1.1` — it only survives because there's no Envoy in the path.
  Move that config to *any* meshed cluster and it 426s. The 0.2.7 chart sets 1.1 on
  **every** location, so it's correct on both.
- **The HPE `ui-nginx-config.yaml` was rewritten from scratch, not ported** from the
  proven DGX ConfigMap, and lost two load-bearing directives in the process. When a
  target needs a config variant, diff it against the working one rather than
  re-deriving it.
- **PCAI = Istio everywhere.** Anything that talks plain HTTP pod-to-pod must be
  HTTP/1.1+. Sidecar mTLS/protocol strictness is the single biggest behavioural
  difference from the DGX lab; expect it to surface latent HTTP/1.0 and cleartext
  assumptions.

### Fast in-place recovery (if an import lands broken)

If the wizard import doesn't apply the baked values (symptom: pods stuck
`ImagePullBackOff` pulling a bare `serving-plane-api:0.2.0` → `docker.io/library/...`,
or `CreateContainerConfigError` / `CrashLoopBackOff`), you don't have to re-import to
test — patch the running Deployments in place. The `ezappconfig` controller on this
trial is one-shot (it does **not** continuously reconcile), so hand-patches stick:

```bash
NS=project-user-mani-malarvannan

# 1. Pull secret (skip the create if `get` succeeds — it persists across re-imports)
kubectl get secret routeforge-registry -n "$NS" 2>/dev/null || \
  kubectl create secret docker-registry routeforge-registry -n "$NS" \
    --docker-server=ghcr.io --docker-username=mani-mal \
    --docker-password='<GHCR classic read:packages PAT>'

# 2. API — real image + pull secret + runAsNonRoot=false
#    (no --type flag => strategic merge: containers merge by name, env/ports/probes kept)
kubectl patch deployment routeforge-api -n "$NS" -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"routeforge-registry"}],"containers":[{"name":"api","image":"ghcr.io/mani-mal/serving-plane-api:0.2.0","securityContext":{"runAsNonRoot":false}}]}}}}'

# 3. UI — real image + pull secret + restore CHOWN cap for nginx
kubectl patch deployment routeforge-ui -n "$NS" -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"routeforge-registry"}],"containers":[{"name":"ui","image":"ghcr.io/mani-mal/serving-plane-ui:0.2.0","securityContext":{"runAsNonRoot":false,"capabilities":{"drop":[]}}}]}}}}'

kubectl get pods -n "$NS" -w   # wait for both to reach 2/2
```

If the API pulls and starts but hangs at `1/2 Running`, that is the TLS/inject-certs
path — grab `kubectl logs -l app.kubernetes.io/component=api -c api -n "$NS" --tail=100`.
(In the verified run it did **not** hang, so `ezua.injectCerts: true` is working.)

### On the GHCR token — do NOT bake it into the chart

The pull PAT is a secret; it stays out of git (CLAUDE.md: *no secrets in code*).
Committing it is also self-defeating — **GitHub auto-revokes classic PATs it finds in
a pushed repo**, so a baked token dies on its own. Keep it out of the chart and supply
it one of three ways, best first:

1. **Make the two GHCR packages public** → no token anywhere: set
   `imageCredentials.enabled: false` and images pull anonymously. Lowest friction for
   a demo. (github.com → Packages → `serving-plane-*` → Package settings → Change
   visibility → Public.)
2. **Pre-create the `routeforge-registry` secret once** in the namespace (step 1
   above). It is namespace-scoped and survives re-imports, so you only touch it on
   rotation; leave `imageCredentials.password` blank in the chart.
3. **Paste the PAT in the wizard's Values step** each import (`imageCredentials.enabled:
   true` + `imageCredentials.password: <PAT>`) — the intended path; nothing hits git.

## Iterating on the chart (dev loop)

Per HPE's "Bring Your Own App" guide, when a framework fails or you need to change
the chart:

1. **Uninstall** the framework: tile → ⋮ → **Delete**. A framework stuck in
   *Installing* may refuse to delete immediately — it eventually moves to a
   deletable state. To force it, delete the underlying `EzAppConfig` (its name is
   `<chart>-<version>-<unix-epoch>`, e.g. `routeforge-0.2.1-1785159188566`):
   ```bash
   kubectl get ezappconfig -n <namespace>
   kubectl delete ezappconfig <name> -n <namespace>
   ```
   (Needs a context with rights to that namespace — the notebook SA is scoped to
   its own namespace, so install there if you want to run these yourself.)
2. **Re-uploading the same version?** ChartMuseum won't overwrite it. Either bump
   `version:` in `Chart.yaml`, or delete the stored version from a pod on-cluster:
   ```bash
   # list registered versions first, then delete the one you want to replace:
   curl -sS http://chartmuseum.ez-chartmuseumns.svc.cluster.local:8080/api/charts/routeforge
   curl -sS -X DELETE \
     http://chartmuseum.ez-chartmuseumns.svc.cluster.local:8080/api/charts/routeforge/0.2.7
   ```
   (The ChartMuseum hostname above is from HPE's guide but did not resolve on the
   DC15 trial. Bumping `version:` avoids needing it — see the version-bump note above.)
3. Re-run `package.sh` and re-import.

## Notes & gotchas

- **PCAI TLS-inspecting proxy (`ezua.injectCerts: true`, on by default).** PCAI
  proxies egress with a private CA. Raw TCP works, but Python HTTPS calls fail
  cert verification unless the platform CA (`ezaf-root-ca.crt`) is trusted. The
  chart tags the API/UI/Langfuse pods with the `inject-certs: enabled` annotation
  so PCAI's platform Kyverno policy mounts the CA bundle and sets
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`. This is the usual cause of an API pod that
  pulls fine but never goes Ready (startup TLS hangs). Leave it on for PCAI.

- **No GPU needed.** `inferenceBackend: mock` schedules on CPU nodes. Switch to
  `vllm`/`dynamo` later (GPU nodes + `nodeSelector`) — not required for the demo.
- **UI runs stock nginx as root** (binds :80). If PCAI's pod-security admission
  rejects root containers, rebuild `serving-plane-ui` on
  `nginxinc/nginx-unprivileged` (listens :8080) and adjust the nginx `listen`
  port + `service.ui`. See `templates/ui.yaml`.
- **CCR residency (hard rule 6).** If you turn Headroom on
  (`servingPlane.contextOptimization.enabled=true`), the CCR PVC must be an
  in-cluster **block/file** StorageClass, never object storage. Langfuse's MinIO
  is for event uploads only — never point CCR at it.
- **Air-gapped installs.** Mirror the `langfuse.images.*` refs into Harbor and
  set `thirdPartyRegistry`. If Presidio's model can't be fetched, set
  `servingPlane.policy.piiDetection: regex` or bake the model into the API image.
- **Verify against current HPE docs at deploy time** — the ezua contract
  (gateway name, required labels) can change between AI Essentials versions.
  This bundle targets the AI Essentials 1.9.x+ Import Framework.
