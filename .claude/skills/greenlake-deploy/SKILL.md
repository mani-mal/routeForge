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

Services: `api` (FastAPI + Headroom library), `ui` (nginx-served React),
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
    ccr: {enabled: true, ttlHours: 24}
  providers:
    onprem: {enabled: true}
    azure: {enabled: false}
langfuse: {enabled: true, host: ""}
```

There is no `servingPlane.inferenceBackend` (or `allowBackendFallback` / `failClosed`)
value any more — they were removed from both charts (B92/B28). The `onprem_only` reject
is derived from `ModelRegistry.has_healthy_onprem()` (no onboarded on-prem model
enabled), not a chart value; on-prem models are onboarded at runtime. Each
`servingPlane.providers.<name>` carries `litellmPrefix` (+ `apiKeyEnv` etc.) because the
boot lint requires it, and `tests/test_helm_render_lint.py` renders both charts and runs
the boot lint on the result.

Secrets via existing K8s Secret references, never chart values. GreenLake customers
will have opinions about secret management; don't fight them.

## HPE GreenLake specifics

GreenLake gives you managed Kubernetes — a conformant cluster, so standard Helm
applies. The deltas worth handling explicitly:

- **Storage** — the serving-plane chart provisions no PVC (CCR is in-memory). In the
  HPE bundle set `global.storageClassName` explicitly; Langfuse's Postgres/ClickHouse need PVCs.
- **Ingress** — confirm the controller class; parameterise `ingress.className`.
- **Registry** — customers usually run a private registry. Parameterise
  `image.repository` and support `imagePullSecrets`.
- **GPU nodes** — routeForge deploys no inference, so its own pods schedule on **CPU**.
  GPUs are only relevant to the operator's separately-deployed vLLM/Dynamo endpoints.
- **CCR storage** — `CCRStore` is in-memory today (no PVC). If it ever gains
  persistence it must be **inside the cluster**: never an external object store or
  cloud bucket (hard rule 6). Set TTL via `contextOptimization.ccr.ttlHours`.
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
