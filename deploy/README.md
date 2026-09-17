# Deploying the Serving Plane

Two targets (greenlake-deploy skill): **Compose is what you demo from**; **Helm**
proves it deploys to the customer's Kubernetes.

Two single-machine variants of the compose path also live here:
`deploy/dgx_lab/` (Kubernetes manifests for the Run:ai lab cluster) and
`deploy/spark/` (an **NVIDIA DGX Spark** workstation — see below).

## Local demo — Docker Compose

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
```

- **UI:** http://localhost:3001
- **API:** http://localhost:8080 (`/health`, `/v1/chat/completions`, `/api/*`)
- **Langfuse:** http://localhost:3000 (login `demo@example.com` / `demodemo`)

Default `inference_backend=mock`, regex PII detection → no GPU, no API keys, offline.
Fire the demo traffic from the UI's "Run burst" button, or:

```bash
docker compose exec api python -m tests.traffic --requests 500 --rps 20
```

GPU serving path (Phase 9) is behind a profile and off by default:

```bash
docker compose -f deploy/compose/docker-compose.yml --profile gpu up
# and set SP_INFERENCE_BACKEND=vllm
```

> First build compiles the API image with the `observability` extra (Langfuse
> export) — the "< 5 minutes" target assumes images are already built/pulled.
> For a lean mock-only image build with `--build-arg EXTRAS=""`.

## Single workstation — NVIDIA DGX Spark

A Spark (GB10, aarch64, Docker, no Kubernetes) has its own bundle — the
`deploy/dgx_lab/` manifests do not apply to it:

```bash
cp deploy/spark/.env.example deploy/spark/.env   # set ADMIN_PASSWORD, POSTGRES_PASSWORD
bash deploy/spark/install.sh                     # preflight, then build + up
```

Builds natively arm64 on the machine, stores state in Postgres so onboarding
survives a restart, and makes Langfuse an optional overlay. See
`deploy/spark/README.md`.

## Kubernetes / HPE GreenLake — Helm

```bash
helm lint deploy/helm/serving-plane
helm template deploy/helm/serving-plane            # inspect rendered manifests
helm install serving-plane deploy/helm/serving-plane \
  -f deploy/helm/serving-plane/values-greenlake.yaml
```

Every `config/serving.yaml` knob is a Helm value — switch backends without rebuilding:

```bash
helm upgrade serving-plane deploy/helm/serving-plane \
  --set servingPlane.inferenceBackend=vllm
```

See `deploy/helm/serving-plane/README.md` for the full values reference. Key GreenLake
notes (from the greenlake-deploy skill): set `storageClassName` and `ingress.className`
explicitly; reference existing K8s Secrets for Langfuse/provider creds (never chart
values); CCR PVC stays **inside the cluster** (residency hard-rule 6); GPU workloads
are gated behind `nodeSelector`/`tolerations`; and outside demo mode the SSE decision
stream must be auth'd + tenant-scoped (it carries tenant/entity/cost metadata).
