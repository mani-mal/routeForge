# API load balancing & horizontal scaling

**Status:** reference (2026-08-20)
**Related:** B3 (shared dashboard state), tracked in `docs/backlog/bugs.md`

This note answers: *when we run more than one API pod, what load-balances across
them, who installs it, and is it an "application load balancer"?*

Short answer: **nothing new gets installed.** The balancing is already in the Helm
chart. There are two *application-level* (L7) proxies and one *network-level* (L4)
balancer, and they are all standard Kubernetes / PCAI pieces that ship with the deploy.

---

## The full request path (Helm / PCAI)

There is **no** API `VirtualService` — the API is never exposed directly at the edge.
Every external call (browser console **and** OpenAI-compatible app clients) funnels
through the UI pod's nginx:

```
external client
   │  HTTP (host + path)
   ▼
[1] PCAI ezaf ingress gateway  (Istio)           ← L7, application load balancer (edge)
   │     VirtualService (virtualservice-ui.yaml): host "/" → routeforge-ui Service
   ▼
[2] UI pod: nginx reverse proxy                   ← L7, application load balancer (in-cluster)
   │     ui-nginx-config.yaml proxies by path:
   │       /api/  /api/stream  /v1/  /admin/  →  http://routeforge-api.<ns>.svc.cluster.local:8080
   ▼
[3] routeforge-api  Service  (type: ClusterIP)    ← L4, network load balancer (kube-proxy)
   │     spreads TCP connections across the api Deployment's pods
   ▼
[4] api pod  (replicaCount.api: N)
```

- **[1] ezaf ingress gateway (Istio)** — the edge **application load balancer**. Routes
  by HTTP host/path. This is the thing you were picturing; it already exists as part of
  PCAI, we just attach a `VirtualService` to it.
- **[2] UI nginx** — a second **application (L7) proxy**. It path-routes `/v1/`, `/api/`,
  `/admin/`, `/api/stream` to the API Service. (`proxy_http_version 1.1` + keepalive to
  the API's Istio sidecar — see the config's own CRITICAL notes.)
- **[3] routeforge-api Service (ClusterIP)** — the **network (L4) load balancer**. This
  is where traffic actually fans out across multiple API pods. `values.yaml`:
  ```yaml
  service:
    api: { type: ClusterIP, port: 8080 }
  replicaCount:
    api: 1        # ← bump to 2/3 (or attach an HPA) to scale out
  ```

## L4 vs L7 — the piece that was unclear

You were thinking of an **application load balancer (L7)** — one that terminates HTTP
and routes by URL/host/cookie (like an AWS ALB, nginx, or Istio gateway). Those *are*
present ([1] and [2]).

But the component that spreads requests across the **API pods** is the **Kubernetes
Service (L4)**. "L4" = transport level: it balances **TCP connections**, not HTTP
requests, and understands nothing about paths, headers, or sessions. It's not a
product you deploy — in Kubernetes a `Service` object *is* a load balancer,
implemented by `kube-proxy` (iptables/IPVS) on every node. Create the Service (already
done) and it round-robins new connections across all matching pods automatically.

So: **no one installs or wires a load balancer for the API.** Setting
`replicaCount.api: 2` is the operator's entire action; kube-proxy does the rest.

## What horizontal scaling changes — and what B3 did about it

With `replicaCount.api: 2`, the L4 Service immediately balances connections across both
pods. The API app used to keep two things **in each pod's memory**:

- `HistoryStore` (ring buffer) → `/api/requests`, `/stats`, `/fleet`
- `DecisionBus` (asyncio pub/sub) → `/api/stream` (SSE)

Left alone, a two-pod deploy would have fragmented the console: each pod only knows the
~half of traffic it served, and a browser's SSE stream (a long-lived connection pinned to
one pod) never sees the other pod's requests. **B3 (shipped)** moved that state into the
shared Postgres: the console's list/detail/stats views read the durable `request_snapshots`
table (`app/apps/models.py`), and a `LISTEN/NOTIFY` bridge (`app/api/pg_bridge.py`) fans
each published decision out to the other pods' SSE subscribers, so the console stays
coherent under the balancing Kubernetes already provides.

Not everything is shared yet: the per-app `max_rpm` limit is still enforced by a
**per-process** rate limiter, so with N pods the effective ceiling is up to N times the
configured value (backlog B59 covers real admission control).

Sticky sessions (Service `sessionAffinity: ClientIP`, or Istio consistent-hash) are
**not** a fix: requests are still spread across pods, so any one pod's view of *all*
traffic is still partial. The state has to be shared, not pinned.

## Docker Compose (single-node) is different

Compose has no built-in load balancer. The single-node deploy runs **one** API
container; to run more you would front them with your own nginx/traefik. The
multi-pod story above is specifically the Kubernetes / Helm / PCAI path. Both deploy
modes use Postgres, so B3's shared-state changes apply identically — a single-node
deploy simply never exercises the cross-pod path.
