---
name: greenlake-deploy
description: Use when packaging or deploying the Serving Plane - Dockerfiles, docker-compose for local demo, Helm charts, and HPE GreenLake / Kubernetes deployment. Trigger on mentions of Docker, compose, Helm, Kubernetes, GreenLake, deployment, or packaging.
---

# Packaging & HPE GreenLake Deployment

Two targets with different jobs. **Compose is what you demo from.** Helm proves it
deploys to the customer's platform — but don't run a live customer demo off a fresh
cluster deploy.

## Docker Compose (local demo)

Must reach a working demo in under 5 minutes on a laptop, no GPU.

Services: `api` (FastAPI + NAT + Headroom library), `ui` (nginx-served React),
`langfuse` + `postgres` + `clickhouse` (Langfuse self-hosted needs both), optional
`headroom` proxy sidecar when `context_optimization.mode: proxy`. There is no `vllm`
service in compose any more — routeForge deploys nothing; `docker compose up` boots
with an empty on-prem pool and no GPU/API keys required. On-prem models are onboarded
against an externally-reachable endpoint after boot (`docs/guides/onboarding-models.md`).

Healthchecks on every service; `depends_on: condition: service_healthy` so the
API doesn't race Langfuse's migrations.

Ship `.env.example`, never `.env`.

## Images

Multi-stage, non-root, pinned bases. Python: `uv` in builder, slim runtime.
UI: node builder → nginx. Add a `HEALTHCHECK`. Target under 500MB for the API.

## Helm chart

```
deploy/helm/serving-plane/
├── Chart.yaml
├── values.yaml
├── values-greenlake.yaml
└── templates/   # deployment, service, ingress, configmap, secret, hpa, NOTES.txt
```

Everything from `config/serving.yaml` must be settable through `values.yaml`.

```yaml
servingPlane:
  policy: {piiDetection: presidio}
  contextOptimization:
    enabled: false
    mode: library              # library | proxy
    ccr: {enabled: true, ttlHours: 24, storageClassName: "", size: 20Gi}
  providers:
    onprem: {enabled: true}
    azure: {enabled: false}
langfuse: {enabled: true, host: ""}
```

**`servingPlane.inferenceBackend` is a dead field — not yet removed.** Both shipped
charts (`deploy/helm/serving-plane/values.yaml` and `deploy/hpe/routeforge/values.yaml`)
still declare `inferenceBackend: mock # mock | vllm | dynamo | none`, and their
templates (`deployment-api.yaml`/`api.yaml`, `configmap.yaml`/`serving-config.yaml`,
`NOTES.txt`) and `README.md` still branch on `.Values.servingPlane.inferenceBackend` —
`deploy/hpe/routeforge/templates/NOTES.txt` even still describes
`inferenceBackend=none` as how you reject `onprem_only`, which is stale: that reject is
now derived from `ModelRegistry.has_healthy_onprem()` (no onboarded on-prem model
enabled), not a chart value. This is **cosmetically stale, not a deploy bug** —
`config/serving.yaml` no longer defines `inference_backend` at all (the loader dropped
it), so whatever the chart renders for it is simply ignored; on-prem models are
onboarded at runtime against the deployed router regardless of what
`inferenceBackend` is set to. Purging `inferenceBackend` from both charts'
`values.yaml`/templates/`README.md`/`NOTES.txt` is tracked as a **separate,
already-deferred packaging-cleanup task** — do not set it expecting it to do
anything, and do not remove it from the charts as a side effect of unrelated work.

Secrets via existing K8s Secret references, never chart values. GreenLake customers
will have opinions about secret management; don't fight them.

## HPE GreenLake specifics

GreenLake gives you managed Kubernetes — a conformant cluster, so standard Helm
applies. The deltas worth handling explicitly:

- **Storage** — set `storageClassName` explicitly in `values-greenlake.yaml`;
  don't rely on a default existing. Langfuse's Postgres/ClickHouse need PVCs.
- **Ingress** — confirm the controller class; parameterise `ingress.className`.
- **Registry** — customers usually run a private registry. Parameterise
  `image.repository` and support `imagePullSecrets`.
- **GPU nodes** — routeForge deploys no inference, so its own pods schedule on **CPU**.
  GPUs are only relevant to the operator's separately-deployed vLLM/Dynamo endpoints.
- **CCR storage** — if compression is on, CCR needs a PVC **inside the cluster**.
  Never point it at an external object store or cloud bucket; that breaks the
  residency guarantee (hard rule 6). Set TTL via `contextOptimization.ccr.ttlHours`
  and size the PVC for it.
- **Headroom offline assets** — Headroom fetches the ONNX runtime and the
  Kompress model from HuggingFace at first use. In air-gapped GreenLake, bake them
  into the image or pre-download and set `HF_HUB_OFFLINE=1`. Running with
  compression disabled needs neither asset — the safe fallback.
- **SSL inspection** — behind a corporate MITM proxy, `HEADROOM_TLS_STRICT=0`
  clears only the strict X.509 flag; chain, expiry and hostname checks stay on.
- **Air-gapped** — assume no internet. Pre-pull images; set
  `policy.piiDetection: regex` if the Presidio model can't be downloaded, or bake the
  model into the image.
- **Decision stream** — the UI's SSE/WebSocket carries every `DecisionRecord`
  (tenants, entity types, costs, routing reasons). Fine on a demo laptop; in a
  multi-tenant GreenLake deploy it is a metadata leak. Outside demo mode the decision
  stream **requires auth and must be tenant-scoped** — a viewer sees only their own
  tenant's records. `server.stream_decisions` stays on; put it behind the ingress
  auth and a per-tenant filter.

Verify current GreenLake specifics against HPE's docs at deploy time rather than
trusting this file — the platform changes.

## Dynamo on K8s

routeForge's chart deploys the router only — it never deploys Dynamo. If the customer
is also standing up a Dynamo-fronted on-prem endpoint in the same cluster (to onboard
into routeForge afterward), don't write that deployment by hand. Use NVIDIA's verified
skills (`dynamo-recipe-runner` for K8s recipes, `dynamo-interconnect-check` to validate
NIXL/UCX/NCCL before blaming the app). Dynamo requires GPU nodes. See
`inference-backends` skill and `docs/guides/onboarding-models.md`.

## Pre-demo checklist

1. `docker compose up` clean on a fresh machine → under 5 min
2. Traffic generator runs; dashboard populates
3. Langfuse reachable, traces landing, no raw PII
4. Helm chart lints and dry-runs against the target cluster
5. Rehearse offline — assume venue wifi fails
