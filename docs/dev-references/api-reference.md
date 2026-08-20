# Developer API Reference

The contract your application uses to send requests to routeForge and read the
response. routeForge is **OpenAI-compatible**: any OpenAI client works if you
point its base URL at routeForge and use your app token as the API key.

- **Base URL:** `http://<host>:<port>/v1` (default port `8080`).
- **Auth:** `Authorization: Bearer rf_live_…` (your app token). Never `X-API-Key`.
- **You get from your admin:** the base URL and the token. That's all.

---

## `POST /v1/chat/completions`

The one endpoint your app calls for inference.

### Request

Headers: `Authorization: Bearer rf_live_…`, `Content-Type: application/json`.

Body:

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `messages` | array of `{role, content}` | **yes** | — | Standard chat messages. `role` is `system`/`user`/`assistant`; `content` is a string. |
| `model` | string | no | `"auto"` | **Currently inert for routing** — see below. Send `"auto"`. |
| `stream` | bool | no | `false` | `true` is **rejected** (`400`). See [Streaming](#streaming-is-rejected). |

Unknown fields are **accepted, not rejected** (the model allows extras). Two
demo-only hint fields are read if present: `mock_multistep` (force the agent
path) and `mock_confidence` / `mock_fail` (mock-backend generation hints). See
[`agentic-app-guide.md`](./agentic-app-guide.md).

> **The `model` field does not select a model.** Routing is entirely
> policy-and-router driven: routeForge picks the tier from request complexity and
> the risk floor, clamps to your app's `allowed_tiers` / `allowed_providers` /
> `allowed_models`, then scores within the tier. The `model` string you send is
> accepted but ignored for selection. To constrain which models you can reach,
> your **admin** narrows your policy — you can't pin a model from the request
> body. (An older help doc implied otherwise; the code does not honor it.)
>
> There is also **no tenant field** in the body — your tenant is derived from the
> token.

### Response `200`

A standard `ChatCompletion` **plus routeForge-specific fields**:

```jsonc
{
  "id": "req-1a2b3c4d",                      // request id — correlates trace + usage
  "object": "chat.completion",
  "model": "local-8b",                       // slug of the model actually chosen
  "model_display": "Qwen2.5-7B-Instruct (local)",
  "choices": [
    { "index": 0,
      "message": { "role": "assistant", "content": "…" },
      "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168 },

  // ---- routeForge extensions ----
  "trace":  [ /* DecisionRecord, VerificationRecord, CompressionRecord */ ],
  "cost":   { "total_usd": 0.00012, "baseline_usd": 0.0009,
              "routing_saved_usd": 0.00078, "compression_saved_usd": 0.0 },
  "quality":{ "sum": 0.5, "chosen": 0.5, "source": "tier_default" },
  "policy": { "sensitivity": "low", "onprem_only": false,
              "allowed_providers": ["azure","bedrock","onprem"],
              "detected_entities": [] },        // entity TYPES only, never values
  "langfuse_url": "https://…"                   // present only when telemetry is configured
}
```

Notes:
- `choices[0].message.content` is your answer — reading only that makes routeForge
  a drop-in for OpenAI.
- `model` is the slug; `model_display` is the human-readable name.
- `created` is **not** emitted by the non-streaming path.
- `policy.detected_entities` lists entity **types** (`US_SSN`, `EMAIL_ADDRESS`),
  never the values. Raw PII never appears in the response metadata.

### The `trace` array

The trace is the "why." Each element is one of:

**`DecisionRecord`** — one per routing decision (one for a direct request; one
per hop, `step:"agent_hop"`, for a multi-step request):

| Field | Meaning |
|---|---|
| `step` | `router` \| `agent_hop` |
| `chosen` / `chosen_display_name` | Model slug / human name selected |
| `reason` | Human-readable rationale (the demo line) |
| `rejected[]` | Candidates not chosen — each `{name, provider, tier, reason, display_name}`, including "excluded by policy" |
| `est_cost_usd` / `actual_cost_usd` | Estimated vs actual USD for this call |
| `latency_ms` | This call's latency |
| `hop_index` | Which agent hop (0 for direct) |
| `tokens_priced` | Post-compression tokens the cost was computed on |
| `quality_score` / `quality_source` | Expected quality and where it came from |
| `serving_engine` | e.g. `vllm`, `trtllm` |

**`VerificationRecord`** (`step:"verify"`) — `required, performed, passed,
entities_found, escalated, escalations_used, reason, latency_ms`.

**`CompressionRecord`** (`step:"compression"`) — `enabled, applied, bypass_reason,
tokens_before, tokens_after, reduction_pct, compressor, ccr_stored,
ccr_retrievals, ccr_retrievals_to_cloud, latency_ms`.

---

## Streaming is rejected

Sending `"stream": true` returns:

```
400  {"detail": "streaming is not supported in the demo"}
```

Output verification (step 9) needs the **complete** response, which token
streaming can't provide without a partial-leak window. Request the full
completion instead. (The dashboard's live decision stream is a *separate*,
admin-only channel — see [Decision stream](#the-decision-stream-admin-only).)

---

## Errors

Every error the inference endpoint can return:

| Status | `detail` message | Cause |
|---|---|---|
| `400` | `streaming is not supported in the demo` | You sent `stream: true`. |
| `401` | `invalid or missing API token` | Missing/unknown/revoked token, or the application is disabled. **Fail closed.** |
| `422` | *(FastAPI validation error)* | Malformed body — e.g. missing `messages`. Extra unknown fields do **not** cause this. |
| `422` | `response failed verification: <detail>` | Output leakage / verification failed. |
| `429` | `rate limit exceeded for this application` | Over your `max_rpm`. |
| `429` | `daily budget exceeded for this application` | Over your `max_spend_usd_per_day`. |
| `503` | `policy evaluation unavailable` | **Catch-all, fail-closed:** the privacy gate errored, routing found no candidates (e.g. `onprem_only` with no local backend), the backend failed on all reachable tiers, or an agent step-cap was exceeded. Retry-safe only if you know the cause is transient. |

Things that surprise people:

- A **bad token is `401`, never `403`.** There is no `403` on this endpoint.
- There is **no `502` / `504`.** Provider/backend failures are retried and fall
  back inside the gateway; exhausting all allowed tiers surfaces as `503`.
- `onprem_only` + no on-prem backend → **`503`**, never a silent cloud call. That
  rejection is the privacy guarantee working as designed.

Handle `429` with backoff; treat `503` as "routeForge declined to serve this
safely," not "server crashed."

---

## `GET /health`

No auth. `{ "status": "...", "backend": "...", "models": [...] }`. Use it for
readiness checks.

---

## The decision stream (admin-only)

`GET /api/stream` is a Server-Sent Events feed of live `DecisionRecord`s that
powers the dashboard's flow visualizer. **It is gated by the admin session
cookie, not your app token** — your application cannot subscribe to it. To
observe your own traffic programmatically, read the `trace` array in each
response, or ask your admin for access to your Langfuse project.

For completeness, the wire format: `text/event-stream`, a `: connected` comment on
open, `: ping` heartbeats every 15s, and `data: <json>\n\n` events of shape
`{"type":"request","summary":{…}}` and `{"type":"step","request_id":…,"index":…,"record":{…}}`.
There is no replay on connect; backfill via `GET /api/requests`.

---

## Minimal working examples

**curl**

```bash
curl http://<host>:<port>/v1/chat/completions \
  -H "Authorization: Bearer rf_live_x7Kp...your-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Summarize this ticket: ..."}]}'
```

**Python (official OpenAI SDK — there is no routeForge SDK)**

```python
from openai import OpenAI

client = OpenAI(base_url="http://<host>:<port>/v1", api_key="rf_live_...")

resp = client.chat.completions.create(
    model="auto",                 # router chooses; body model is not honored
    messages=[{"role": "user", "content": "Summarize this ticket: ..."}],
)
print(resp.choices[0].message.content)

# routeForge extensions ride alongside the standard fields:
raw = resp.model_dump()
print(raw["model_display"], raw["cost"]["total_usd"])
for rec in raw["trace"]:
    print(rec["step"], rec.get("chosen"), rec.get("reason"))
```

The OpenAI SDK ignores unknown response fields, so `resp.choices[0].message`
works normally; reach the routeForge extras via `resp.model_dump()` (or a raw
`httpx`/`requests` call if you prefer the plain dict).

Next: if you're building an agent, read
[`agentic-app-guide.md`](./agentic-app-guide.md).
