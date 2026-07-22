# serving-plane Helm chart

Deploys routeForge's Serving Plane (`serving-plane-api` + `serving-plane-ui`)
to Kubernetes / HPE GreenLake. See `../../../.claude/skills/greenlake-deploy/SKILL.md`
for the full design rationale; this file is the values reference.

Langfuse itself is **not** deployed by this chart — point `langfuse.host` at
wherever it already runs (the local demo uses `docker compose`, which ships a
self-hosted Langfuse stack).

## Install

```bash
# Demo-safe defaults (mock backend, CPU only, no GPU nodes needed):
helm install serving-plane deploy/helm/serving-plane \
  --set langfuse.existingSecret=langfuse-keys

# GreenLake:
helm install serving-plane deploy/helm/serving-plane \
  -f deploy/helm/serving-plane/values-greenlake.yaml \
  --set langfuse.existingSecret=langfuse-keys
```

The referenced Secret must already exist with keys `langfuse-public-key` and
`langfuse-secret-key`:

```bash
kubectl create secret generic langfuse-keys \
  --from-literal=langfuse-public-key=pk-lf-... \
  --from-literal=langfuse-secret-key=sk-lf-...
```

## Switching backends / PII / compression — no rebuild required

Every knob in `config/serving.yaml` is settable here under `servingPlane:` and
is rendered into a ConfigMap that overlays `/app/config/serving.yaml` inside
the api Pod (routing.yaml/policy.yaml/pricing.yaml stay as baked into the
image — they aren't infra switches). A handful of the same switches are also
set as container env vars (`SP_INFERENCE_BACKEND`, `SP_PII_DETECTION`,
`SP_SERVER_PORT`, `SP_CONTEXT_OPT`), matching exactly what
`app/config/loader.py` already reads from the environment in the Dockerfile
and compose setup.

```bash
helm upgrade serving-plane deploy/helm/serving-plane --reuse-values \
  --set servingPlane.inferenceBackend=vllm

helm upgrade serving-plane deploy/helm/serving-plane --reuse-values \
  --set servingPlane.policy.piiDetection=regex

helm upgrade serving-plane deploy/helm/serving-plane --reuse-values \
  --set servingPlane.contextOptimization.enabled=true \
  --set servingPlane.contextOptimization.ccr.enabled=true
```

A pod-template checksum annotation on the api Deployment forces a rollout
whenever the rendered config changes.

## Values reference

| Key | Default | Notes |
|---|---|---|
| `servingPlane.inferenceBackend` | `mock` | `mock \| vllm \| dynamo \| none`. `mock` needs no GPU. |
| `servingPlane.allowBackendFallback` | `true` | `false` ⇒ api exits non-zero if backend unreachable. |
| `servingPlane.vllm.baseUrl` / `.timeoutS` | vLLM OpenAI-compatible endpoint | Only used when `inferenceBackend: vllm`. |
| `servingPlane.dynamo.endpoint` / `.routerMode` / `.timeoutS` | Dynamo frontend | Only used when `inferenceBackend: dynamo`. |
| `servingPlane.policy.piiDetection` | `presidio` | `presidio \| regex \| off`. Use `regex` if Presidio's model can't be downloaded air-gapped. |
| `servingPlane.policy.failClosed` | `true` | Never set `false`. |
| `servingPlane.contextOptimization.enabled` | `false` | Headroom compression. Off by default so a demo can show before/after. |
| `servingPlane.contextOptimization.mode` | `library` | `library \| proxy`. |
| `servingPlane.contextOptimization.ccr.enabled` | `true` | Only takes effect when `contextOptimization.enabled` is also `true`. |
| `servingPlane.contextOptimization.ccr.residency` | `local_only` | Must stay `local_only` — hard rule 6. |
| `servingPlane.contextOptimization.ccr.ttlHours` | `24` | TTL enforced by the app; not a storage-layer setting. |
| `servingPlane.contextOptimization.ccr.storageClassName` | `""` (cluster default) | **Set explicitly on GreenLake** — see values-greenlake.yaml. Must be in-cluster block/file storage, never an object store. |
| `servingPlane.contextOptimization.ccr.size` | `20Gi` | PVC size for the CCR store. |
| `servingPlane.providers.{onprem,azure,bedrock}.enabled` | `onprem: true`, rest `false` | Provider *availability* only — tier membership is `routing.yaml`'s job, unchanged here. |
| `servingPlane.server.streamDecisions` | `true` | **See warning below.** |
| `servingPlane.server.rejectStreamRequests` | `true` | Rejects inbound `stream: true` (demo policy — separate from the SSE decision stream). |
| `langfuse.enabled` | `true` | |
| `langfuse.host` | `http://langfuse-web:3000` | Point at your real Langfuse install. |
| `langfuse.existingSecret` | `""` | **Preferred.** Name of an existing Secret with `langfuse-public-key`/`langfuse-secret-key`. |
| `langfuse.createSecret.enabled` | `false` | Dev/demo only — creates a Secret from plaintext values. Never use with `existingSecret` set. |
| `image.api.repository` / `.tag` / `.pullPolicy` | `serving-plane-api` | Point at your private registry on GreenLake. |
| `image.ui.repository` / `.tag` / `.pullPolicy` | `serving-plane-ui` | Same. |
| `imagePullSecrets` | `[]` | List of `{name: ...}` — needed for most private registries. |
| `gpuWorkload.nodeSelector` / `.tolerations` / `.resources` | `{}` / `[]` / `{}` | Only ever applied to `vllmStub` (see below) or a future real vLLM/Dynamo Deployment. **Never applied to the api/ui Deployments** — the default install requires no GPU node. |
| `vllmStub.enabled` | `false` | Values-gated PLACEHOLDER Deployment for Phase 9. Not a working vLLM install — use NVIDIA's Dynamo K8s recipes for `dynamo`, and a real vLLM chart/manifest for `vllm`, before relying on GPU serving. |
| `ingress.enabled` / `.className` / `.host` / `.tls.*` | on, `""`, `serving-plane.local`, off | Set `ingress.className` explicitly — don't assume a default controller. |
| `autoscaling.enabled` / `.minReplicas` / `.maxReplicas` | `false` / `1` / `5` | HPA on the api Deployment (CPU + memory targets). |
| `resources.api` / `.ui` | see values.yaml | Requests/limits per component. |
| `securityContext.api` | non-root, all caps dropped | Matches the Dockerfile's dedicated non-root `app` user. |
| `securityContext.ui` | root, `NET_BIND_SERVICE` only | Stock `nginx:1.27-alpine` binds port 80 as root; forcing non-root here would crash-loop unless `ui/Dockerfile` switches to an unprivileged nginx image. |

## GPU gating

The default install (`inferenceBackend: mock`) schedules the api and ui
Deployments on ordinary CPU nodes — nothing in this chart requests a GPU by
default. `gpuWorkload.nodeSelector`/`.tolerations`/`.resources` exist purely
as plumbing for a GPU serving workload and are only wired into the optional,
disabled-by-default `vllmStub` Deployment (Phase 9 placeholder). Verify with:

```bash
helm template . | grep -A2 nodeSelector   # empty on default values
```

## CCR PVC gating

The `ccr-pvc.yaml` template only renders when **both**
`servingPlane.contextOptimization.enabled` and
`servingPlane.contextOptimization.ccr.enabled` are `true` (the default has
compression off, so no PVC is created by default). It is always inside-cluster
storage — `storageClassName` is a Kubernetes StorageClass, never an S3-style
endpoint (hard rule 6).

## Decision stream (SSE) — auth warning

`servingPlane.server.streamDecisions` (default `true`) exposes a live,
per-request decision stream carrying tenant/entity/cost metadata to the UI.
That's appropriate for a single-tenant demo. **Outside demo mode, in any
multi-tenant or customer-facing deployment, this stream must be placed behind
authentication and scoped per tenant at the ingress/gateway.** This chart
does not implement that auth — it only surfaces the switch and this warning.
Do not expose the ingress route unauthenticated in a shared environment.

## Air-gapped / offline notes

- Headroom (compression) fetches an ONNX runtime + the Kompress model from
  HuggingFace on first use. If `contextOptimization.enabled: true` in an
  air-gapped install, bake those assets into the image and set
  `HF_HUB_OFFLINE=1` via `extraEnv`. Leaving compression disabled (the
  default) needs neither asset.
- If Presidio's model can't be downloaded, set `servingPlane.policy.piiDetection: regex`.
- Behind corporate TLS-inspecting proxies, `HEADROOM_TLS_STRICT=0` (via
  `extraEnv`) narrows only the strict X.509 check; chain/expiry/hostname
  checks stay on.

## Validating locally

```bash
helm lint deploy/helm/serving-plane
helm template deploy/helm/serving-plane
helm template deploy/helm/serving-plane \
  -f deploy/helm/serving-plane/values-greenlake.yaml \
  --set servingPlane.inferenceBackend=vllm
```
