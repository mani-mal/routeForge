# Per-user access to a shared on-prem model

How to give several people their own API key to one self-hosted model (for example a
vLLM deployment of GLM 5.3 across 8 GPUs), so that each user has their own limits,
usage and traces, and can be revoked alone. It also covers where RouteForge falls short
of a plain key gateway such as LiteLLM Proxy today.

*Written 2026-09-27, verified against `main` at `e5859db`.*

## The problem

vLLM checks one shared `--api-key`. It can't tell users apart, rate-limit one of them,
or revoke one without rotating the key for everyone. It also serves every caller from
one queue and one KV cache, so one user's agent loop can take all the capacity.

The model server keeps no per-user state. Chat sessions and files live in each user's
client (OpenCode, an IDE, a script). So "not sharing a session" only needs **separate
keys at a gateway**. Nothing has to change inside vLLM.

## The pattern

```
user A ─┐  rf_live_A                                   vLLM key (env var)
user B ─┼──────────────▶ RouteForge ──────────────────▶ vLLM (ClusterIP only)
user C ─┘  one app per user    policy · limits · traces
```

1. **Onboard the model once.** RouteForge holds the vLLM key. Users never see it.
2. **Create one application per user**, and issue that application a token.
3. **Lock vLLM down** so that only RouteForge can reach it.

### 1. Onboard the model

Put the vLLM key in an env var on the RouteForge API pod (a Kubernetes Secret →
`env`, or `.env` for Compose). Then onboard the endpoint and pass the **name** of that
env var, never the value:

```bash
curl -sX POST https://<routeforge>/admin/infra/models \
  -H "Content-Type: application/json" --cookie "rf_admin_session=<session>" \
  -d '{
        "slug": "glm-5.3",
        "display_name": "GLM 5.3",
        "tier": "frontier",
        "endpoint": "http://glm-vllm.<ns>:8000/v1",
        "api_key_env": "SP_GLM_API_KEY",
        "price_input": 1.0,
        "price_output": 1.0,
        "context_window": 131072
      }'
```

- `slug` must match vLLM's `--served-model-name`.
- `context_window` gives an oversized request a clean OpenAI-style 400 instead of a 502
  from vLLM. If you leave it out, RouteForge probes it.
- The prices are **nominal**. They turn `max_spend_usd_per_day` into a daily token
  budget (at `1.0`/`1.0`, $1 is about 1M tokens). Set them to your internal chargeback
  rate if you have one.
- Use the short `<svc>.<ns>` DNS form for in-cluster endpoints on the DGX. The full
  `.svc.cluster.local` name resolves to a corporate wildcard there.

The backend key is resolved per call in `app/gateway/backends/litellm_backend.py`, from
the onboarded model's `api_key_env`. Full field reference:
[`onboarding-models.md`](onboarding-models.md).

### 2. One application per user

**Use one application per user, not many tokens on one application.** Limits
(`max_rpm`, `max_spend_usd_per_day`, `max_cost_usd_per_request`), usage, dashboards
and the Langfuse project all belong to the *application*. Every token on an application
shares them. So separate applications are the only way to get separate limits.

```bash
# create the user's app — the response includes the first token, shown ONCE
curl -sX POST https://<routeforge>/admin/applications \
  -H "Content-Type: application/json" --cookie "rf_admin_session=<session>" \
  -d '{"id":"u-alice","name":"Alice (OpenCode)","default_residency":"onprem_only"}'

# policy: placement, protection, streaming, limits
curl -sX PUT https://<routeforge>/admin/applications/u-alice/policy \
  -H "Content-Type: application/json" --cookie "rf_admin_session=<session>" \
  -d '{
        "allowed_providers": ["onprem"],
        "allowed_models": ["glm-5.3"],
        "residency": "onprem_only",
        "policy_mode": "passthrough",
        "stream_mode": "verified_then_stream",
        "max_rpm": 30,
        "max_spend_usd_per_day": 20.0,
        "max_cost_usd_per_request": 0.5
      }'

# rotate: issue a new token / revoke an old one
curl -sX POST https://<routeforge>/admin/applications/u-alice/tokens \
  --cookie "rf_admin_session=<session>" -H "Content-Type: application/json" -d '{"name":"laptop"}'
curl -sX POST https://<routeforge>/admin/tokens/<token_id>/revoke --cookie "rf_admin_session=<session>"
```

The user then points their client at `https://<routeforge>/v1` with their
`rf_live_…` token, and with `model` set to `glm-5.3` or `auto`.

#### Choosing the settings for a coding agent (OpenCode and similar)

| Setting | Recommended | Why |
|---|---|---|
| `stream_mode` | any value except `off` (use `verified_then_stream`) | Coding agents send `stream: true`. With the default `off`, the deployment rejects it (`server.reject_stream_requests: true`) with a 400. **Read the streaming limitation below.** |
| `policy_mode` | `passthrough` (trusted internal developers) | On source code, Presidio flags identifiers, hashes and fixture data as PII. The resulting redaction corrupts the code the model sees and returns, and it adds latency. Needs `policy.allow_app_passthrough: true` in `serving.yaml` (the default). |
| `residency` | `onprem_only` | This is placement, not protection. It stays honest alongside `passthrough` (CLAUDE.md rule 2a), and it ensures that adding a cloud provider later can never pull this user's code off-prem. |
| `allowed_models` | `["glm-5.3"]` | Pins the user to the shared model even after more models are onboarded. |
| `max_rpm` / `max_spend_usd_per_day` / `max_cost_usd_per_request` | sized per user | These are the only fairness levers today. See the limitations below. |

If some users handle regulated data, give *their* applications `policy_mode: enforced`
instead. The setting is per application, so you can mix both on the same deployment. See
[`choosing-app-settings.md`](choosing-app-settings.md).

### 3. Network lockdown (independent of the gateway choice)

- Make the vLLM Service `ClusterIP` only. Remove any NodePort.
- Add a NetworkPolicy that admits ingress to vLLM only from the RouteForge API pods.
- Expose RouteForge over HTTPS (an ingress with a certificate). Users keep the same base
  URL format, and only their key differs.
- Keep a vLLM `--api-key` anyway. It becomes a service credential that only RouteForge
  holds.

## Does this work the same for other models?

**Yes.** Nothing here is specific to GLM or vLLM:

- **Any OpenAI-compatible endpoint** (vLLM, SGLang, NIM, TGI in OpenAI mode, Dynamo's
  frontend, llama.cpp server, Ollama's `/v1`) onboards through the same
  `POST /admin/infra/models`. It is probe-gated and needs no restart.
- **Per-user applications, tokens and limits don't depend on the model.** One set of
  applications can reach several models.
- **With several models onboarded**, choose per application:
  - `allowed_models` pins a user to specific models.
  - `allowed_tiers` plus `model: "auto"` lets the router pick per call. `tier` is
    length-based complexity, not semantic; see
    `docs/architecture/routing-tier-selection.md`.
  - A model's `allowed_app_ids` restricts it to named applications, which suits an
    expensive model that only some users should reach.
- **What differs per model** is only what you declare at onboarding: `tier`, prices,
  `context_window`, `region` / `prompt_retention` / `accepts_raw_sensitive`, and the
  key env var. Each model can have its own backend key.
- **Cloud models** (Azure, Bedrock) are provider config (`serving.yaml`
  `providers.*.enabled` plus the `routing.yaml` / `pricing.yaml` pool), not onboarding.
  An `onprem_only` application can never reach them.

## Limitations today (and what RouteForge needs)

Before relying on RouteForge for this, know these gaps. Each one is tracked in
[`../backlog/bugs.md`](../backlog/bugs.md).

1. **Coding agents get no live token streaming (B58).** Streaming only returns tokens as
   they are generated when the request carries no tools. Any request that offers `tools`
   (`app/pipeline.py::_is_tool_conversation`) runs the full buffered pipeline, and the
   finished answer is sent as SSE frames. OpenCode, Claude-Code-style clients and most IDE
   agents send `tools` on **every** request, so every turn arrives all at once after
   generation finishes. The answer is correct, and keepalive frames prevent client
   timeouts, but the user sees nothing appear while the model generates. `onprem_only` /
   `require_verification` apply the same floor to requests without tools.
2. **No tokens-per-minute or concurrency limit (B59).** Only `max_rpm` exists. In an
   agent loop, one request can carry 100K+ tokens of context, so an RPM cap does not stop
   one user from filling a shared KV cache (about 550K tokens on the GLM deployment). The
   daily and per-request cost caps are a coarse substitute, not per-minute fairness.
3. **The rate limiter is per process (B59).** `RateLimiter` in `app/apps/policy.py` is
   in memory. With `replicaCount.api: N`, a user's effective limit is `N × max_rpm`, and a
   restart resets it. Run one API replica, or divide `max_rpm` by the replica count.
4. **Limits belong to applications, not tokens.** This is by design (see step 2). The
   cost is that a team of many users means many applications, and there is no bulk
   "create N user apps" call. Script it against the admin API.

**When LiteLLM Proxy is the better fit:** if you need strict per-minute token fairness,
or live streaming for agent clients, *today*, and you don't need RouteForge's decision
traces, per-user privacy posture or multi-model routing. LiteLLM gives per-key
TPM/RPM/budget and streams tool-bearing requests natively. The two can also be chained
(RouteForge → LiteLLM → vLLM) if you need both.
