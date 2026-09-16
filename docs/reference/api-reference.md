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
| `messages` | array of `{role, content, …}` | **yes** | — | Standard chat messages. `role` is `system`/`user`/`assistant`/`tool`; `content` is a string **or `null`** (an assistant turn that only requested tools). Tool-calling turns may also carry `tool_calls`, `tool_call_id` and `name` — see [Tool calling](#tool-calling). |
| `model` | string | no | `"auto"` | `"auto"` lets the router choose. **A non-`"auto"` value is a hard model pin** — see below. |
| `stream` | bool | no | `false` | Per-app capability via `stream_mode`; default (unset) still **rejected** (`400`). See [Streaming](#streaming). |
| `tools` | array of tool definitions | no | `null` | Standard OpenAI tool/function definitions, passed through to the backend. See [Tool calling](#tool-calling). |
| `tool_choice` | string \| object | no | `null` | Standard OpenAI tool-choice control, passed through with `tools`. |
| `max_tokens` | integer ≥ 1 | no | — | Output length cap — forwarded to the backend. Priced at this cap if set, else the default estimated output tokens. A non-integer or value < 1 is rejected `422`. Because it is forwarded, a value larger than the backend's context window fails upstream (`502`) rather than being silently dropped. |
| `max_completion_tokens` | integer ≥ 1 | no | — | OpenAI-compatible alias for `max_tokens` — forwarded to the backend, validated the same way. |
| `temperature` | float | no | — | Forwarded to the backend. |
| `top_p` | float | no | — | Forwarded to the backend. |
| `stop` | string \| array | no | — | Stop sequences — forwarded to the backend. |
| `seed` | integer | no | — | Forwarded to the backend. |
| `presence_penalty` | float | no | — | Forwarded to the backend. |
| `frequency_penalty` | float | no | — | Forwarded to the backend. |
| `response_format` | object | no | — | Forwarded to the backend (e.g. `{"type": "json_object"}`). |
| `parallel_tool_calls` | boolean | no | — | Forwarded to the backend. |

Unknown fields are **accepted, not rejected** (the model allows extras). Two
demo-only hint fields are read if present: `mock_multistep` (force the agent
path) and `mock_confidence` / `mock_fail` (mock-backend generation hints). See
[`agentic-app-guide.md`](../guides/agentic-app-guide.md).

> **`model: "auto"` lets the router choose; any other value pins routing to that
> exact slug.** With `"auto"`, routeForge picks the tier from request complexity
> and the risk floor, clamps to your app's `allowed_tiers` / `allowed_providers` /
> `allowed_models`, then scores within the tier. If you send a concrete slug
> instead (e.g. `"onprem-7b"`), the router drops every candidate whose name
> doesn't match — routing is pinned to that one model, and the semantic cache
> honors the pin too. If the pinned slug isn't in your app's permitted/available
> pool the request is **rejected `422`** with
> `model request rejected: requested model '<model>' is not permitted or unavailable: <gate>`,
> where `<gate>` names what refused it (see the error table below).
> The pin can only narrow within your policy; it can't reach a model your admin
> hasn't allowed.
>
> There is also **no tenant field** in the body — your tenant is derived from the
> token.

### Response `200`

A standard `ChatCompletion` **plus routeForge-specific fields**:

```jsonc
{
  "id": "req-1a2b3c4d",                      // request id — correlates trace + usage
  "object": "chat.completion",
  "created": 1757030400,                     // unix seconds — always present
  "model": "onprem-7b",                      // slug of the model actually chosen
  "model_display": "Qwen2.5-7B-Instruct (on-prem)",
  "choices": [
    { "index": 0,
      "message": { "role": "assistant", "content": "…" },
      "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168 },

  // ---- routeForge extensions ----
  "trace":  [ /* DecisionRecord, VerificationRecord, CompressionRecord, MemoryRecord, CacheRecord */ ],
  "cost":   { "total_usd": 0.00012, "baseline_usd": 0.0009,
              "routing_saved_usd": 0.00078, "compression_saved_usd": 0.0 },
              // "cache_saved_usd" is also present when the semantic cache is enabled
  "quality":{ "sum": 0.5, "chosen": 0.5, "source": "tier_default" },
  "policy": { "sensitivity": "public", "onprem_only": false,
              "allowed_providers": ["azure","bedrock","onprem"],
              "detected_entities": [],           // entity TYPES only, never values
              "app_policy_version": "39260c5ee1ad",
              "policy_mode": "enforced" },
  "session_id": null,                        // echoes X-RouteForge-Session, or null
  "display": { "mode": "off", "input": null, "output": null },  // console-internal, see below
  "langfuse_url": "https://…",                  // present only when telemetry is configured
  "trace_id": "abc123…"                         // present only when telemetry is configured; use for POST /v1/feedback
}
```

Notes:
- `choices[0].message.content` is your answer — reading only that makes routeForge
  a drop-in for OpenAI.
- **`message.content` is the *post-verification* answer, not the raw model output.**
  Step 9 (Output Controls & Verification) can change it: egress PII redaction scrubs any
  PII the model itself emitted (values the input gate never saw), and a request with
  `require_verification` set is **rejected** if the completion is unverifiable. On the
  common path — no PII in the completion and `require_verification` unset — `content` is
  the model's answer verbatim. The `VerificationRecord` in `trace` (`step:"verify"`)
  reports what step 9 did. The unmodified model output — and, for tool calls, the
  unredacted `function.arguments` — is kept internally as `raw_completion` /
  `raw_tool_calls`: popped before serialization, **never** a key on this response body
  or on `display.output`. It reaches telemetry only when the app is configured
  `capture_io: "full"`, and only into that app's own Langfuse project.
- `model` is the slug; `model_display` is the human-readable name.
- `object` and `created` are **always** emitted, on every response shape (direct,
  cache hit, multi-step and streaming). The `openai` SDK's `ChatCompletion` model
  rejects a response missing `created`, so both are set at the single post-model
  chokepoint every shape passes through (`app/api/routes.py:_finalize`).
- `policy.detected_entities` lists entity **types** (`US_SSN`, `EMAIL_ADDRESS`),
  never the values. Raw PII never appears in the response metadata. `sensitivity` is one
  of the four canonical values (`public | internal | confidential | restricted`), never a
  risk-class value like `"low"`. `app_policy_version` and `policy_mode` are always present
  — see [`per-app-config.md`](./per-app-config.md) for what each means.
- `session_id` echoes the sanitized `X-RouteForge-Session` request header (`null` if you
  didn't send one or it failed validation) — an opaque workflow-run correlation label, never
  a policy input. See [`agentic-app-guide.md`](../guides/agentic-app-guide.md).
- `display` is additive metadata for the admin console's own drilldown (mode-appropriate
  redacted/raw copies of the prompt and completion, per the app's `capture_io`) — **not** a
  telemetry channel and not meant for API clients to consume; ignore it.
- `trace_id` is the Langfuse trace id, present only when telemetry export succeeded. Pass it
  as `trace_id` to `POST /v1/feedback` to attach a score to this request.

### The `trace` array

The trace is the "why." Each element is one of:

**`DecisionRecord`** — one per routing decision (one for a direct request; one
per hop, `step:"agent_hop"`, for a multi-step request):

| Field | Meaning |
|---|---|
| `step` | `router` \| `agent_hop` |
| `chosen` / `chosen_display_name` | Model slug / human name selected |
| `chosen_tier` / `chosen_provider` | Tier and provider of the chosen candidate |
| `reason` | Human-readable rationale (the demo line) |
| `rejected[]` | Candidates not chosen — each `{name, provider, tier, reason, display_name}`, including "excluded by policy" |
| `est_cost_usd` / `actual_cost_usd` | Estimated vs actual USD for this call |
| `latency_ms` | This call's latency |
| `hop_index` | Which agent hop (0 for direct) |
| `tokens_priced` / `tokens_out` | Post-compression prompt tokens the cost was computed on / completion tokens for this hop |
| `quality_score` / `quality_source` | Expected quality and where it came from (`customer` \| `calibrated` \| `published` \| `tier_default`) |

**`VerificationRecord`** (`step:"verify"`) — `required, performed, passed,
entities_found, escalated, escalations_used, reason, latency_ms`.

**`CompressionRecord`** (`step:"compression"`) — `enabled, applied, bypass_reason,
tokens_before, tokens_after, reduction_pct, compressor, ccr_stored,
ccr_retrievals, ccr_retrievals_to_cloud, latency_ms`.

**`MemoryRecord`** (`step:"memory"`) — persistent-memory recall, counts only:
`recalled_count, tokens_recalled, recall_stayed_local, retrievals_to_cloud`.
Present only when persistent memory is enabled. No memory contents ever reach the
trace.

**`CacheRecord`** (`step:"cache"`) — semantic-cache decision, counts only:
`enabled, hit, bypass_reason, tokens_saved, cache_saved_usd, entries_size,
blocked_cloud_entries, latency_ms`. Present only when the semantic cache is
enabled. No query or response text ever reaches the trace.

---

## Tool calling

routeForge is a drop-in OpenAI endpoint for tool-using clients. Send `tools`
(and optionally `tool_choice`) exactly as you would to OpenAI; both are threaded
through the gateway to the backend unchanged, and the assistant turn comes back
in the standard shape:

```jsonc
"choices": [
  { "index": 0,
    "message": {
      "role": "assistant",
      "content": null,                         // null when the turn only requested tools
      "tool_calls": [
        { "id": "call_abc", "type": "function",
          "function": { "name": "get_order", "arguments": "{\"id\":\"4821\"}" } }
      ]
    },
    "finish_reason": "tool_calls" }             // "stop" for an ordinary answer
]
```

Switch on `finish_reason`: `"tool_calls"` means run your tools and replay the
result. Replay it the normal way — append the assistant turn verbatim, then a
`{"role": "tool", "tool_call_id": "call_abc", "content": "…"}` message — and send
the whole list back as a new request.

Three things specific to routeForge:

- **Your loop, not routeForge's.** A request carrying `tools` is served as a
  single completion; it is never handed to routeForge's own internal agent
  runtime. The client drives the loop (this is Pattern A in
  [`agentic-app-guide.md`](../guides/agentic-app-guide.md)).
- **Every turn is a fresh, independently-gated request, redacted per message.** A
  tool conversation keeps its roles and `tool_calls` / `tool_call_id` structure
  intact and the gate redacts **each message's text individually**, rather than
  flattening the whole request into one blob as the ordinary single-prompt path
  does. So data a tool pulled is detected and its residency enforced on the turn
  that carries it, with the tool round-trip still well-formed.
- **Compression, the semantic cache and persistent memory are all skipped.** They
  each assume that single flattened prompt, and Headroom would corrupt tool-call
  structure. The trace records
  `CompressionRecord.bypass_reason = "tool_calling"`.

---

## Streaming

Inbound streaming is a **per-app capability**, not a flat rejection. Your admin
sets your `AppPolicy.stream_mode` — one of `off | verified_then_stream |
stream_unverified | incremental_verify` (see
[`per-app-config.md`](./per-app-config.md) for what each mode guarantees).

If your app has no `stream_mode` set (or it's `off`) and the deployment rejects
inbound streaming (`server.reject_stream_requests`), sending `"stream": true`
still returns:

```
400  {"detail": "streaming is not supported for this application"}
```

Output verification (step 9) needs the **complete** response, which token
streaming can't provide without a partial-leak window — which is why streaming is
gated and why `onprem_only` / `require_verification` requests always fall back to
the fully-verified path regardless of `stream_mode`. (The dashboard's live
decision stream is a *separate*, admin-only channel — see
[Decision stream](#the-decision-stream-admin-only).)

### Keepalive comments for long-running requests

When a request waits for a complete, verified answer — such as a tool turn, `verified_then_stream` streaming, or a request floored to the fully-verified path — RouteForge sends SSE keepalive comment lines every `server.stream_keepalive_s` seconds (default 5) once the answer takes longer than one interval. The format is `: keepalive` on a line by itself. OpenAI-compatible clients ignore comment lines, so keepalive frames do not affect request/response semantics. They are transparent to tool calling and standard streaming clients.

**Why keepalives matter:** Some OpenAI-SDK-based agent clients apply a separate request timeout (often 120 s) to connect and first response bytes. Without keepalives, a request that produces its first byte after 120 s abandons the backend work and retries, leading to overlapped computation and multiple identical usage rows. Keepalives are a few bytes each and hold the connection open.

### Late failures as SSE error frames

If an error occurs after the SSE stream has opened (status 200 is on the wire), the error is conveyed as an SSE frame:

```
data: {"error": {"message": "<detail>", "type": "routeforge_error", "code": <http-status>}}
data: [DONE]
```

The message text is identical to what the HTTP error response would contain. Clients that parse SSE will raise an `APIError` on the error frame.

**Fast responses and rejections** — errors that occur before streaming starts (validation, policy gate, no-eligible-model checks) — still return their normal HTTP status code and do not use SSE frames.

### Client disconnect handling

When a client disconnects while waiting for a complete answer, the backend work is cancelled and the usage row is recorded with `status: cancelled`.

For non-streamed requests (`stream: false`), a disconnected client receives:

```
499  (empty body)
```

The `499 Client Closed Request` status indicates the request was abandoned by the client, not a server error.

**Streamed requests** that use real backend streaming (`stream_unverified`, `incremental_verify`) drain the backend to keep cost and token accounting accurate, even if the client has disconnected.

### Sessions and tracing

Without an `x-routeforge-session` header, the session id is derived from a hash of:

- The application id
- The first system message (if any)
- The first user message

The derived id is formatted as `auto-` followed by 24 hex characters: a keyed HMAC-SHA256 (no content stored), not a raw unkeyed hash — an unkeyed hash over raw pre-gate text would let a telemetry viewer who sees the redacted prompt brute-force a low-entropy redacted value (e.g. an SSN) against the exported id. The HMAC key is `SP_SESSION_ID_KEY` when set (any non-empty string); otherwise a random 32-byte key generated once per process, which means derived ids reset on restart and differ across replicas unless `SP_SESSION_ID_KEY` is set to the same value on every replica. Derivation itself is controlled by `server.derive_session_id` (default `true`) and can be disabled by setting it to `false`.

The session id is used for **trace and console grouping only**. Persistent memory (agent long-term context) is partitioned by the client-supplied `x-routeforge-session` header only; a derived session id does not change memory partitioning or affect memory semantics. An agent conversation with no header still groups into one session in Langfuse and the console as its history grows, without touching the memory partition it reads and writes.

---

## Errors

Every error the inference endpoint can return:

| Status | `detail` message | Cause |
|---|---|---|
| `400` | `streaming is not supported for this application: set the application's stream_mode (verified_then_stream, stream_unverified or incremental_verify)` | You sent `stream: true` and your app has no `stream_mode`; set one, or send `stream: false`. |
| `401` | `invalid or missing API token` | Missing/unknown/revoked token, or the application is disabled. **Fail closed.** |
| `422` | *(FastAPI validation error)* | Malformed body — e.g. missing `messages`. Extra unknown fields do **not** cause this. |
| `422` | `no model is available for this request's policy: no candidates after residency filter: <counts and fixes>` | Nothing survived placement or app scope; the detail lists each reason and the field to change. |
| `422` | `no model is available for this request's policy: no candidates after budget filter: cheapest candidate … estimated at $X … over the per-request cap $Y …` | Raise the app's `max_cost_usd_per_request`, lower `max_tokens`, or lower the model's price. The router has already tried cheaper allowed tiers. |
| `422` | `model request rejected: requested model '<model>' is not permitted or unavailable: <gate>` | The pinned `model` failed the named gate; `GET /v1/models` lists what this app can pin. |
| `422` | `response failed verification: <detail>` | Output leakage / verification failed. |
| `429` | `rate limit exceeded for this application` | Over your `max_rpm`. |
| `429` | `daily budget exceeded for this application` | Over your `max_spend_usd_per_day`. |
| `500` | `agent did not converge within its step limit` | A multi-step workflow exceeded `agent_budgets.max_steps`. |
| `500` | `internal error` | Unexpected server-side failure. |
| `502` | `no backend could serve this request: … (last error: <model>: <ErrorType>: <message>)` | Every candidate failed upstream; the last upstream error is included. |
| `503` | `policy evaluation unavailable` | **Fail-closed:** the privacy gate itself errored. |

Things that surprise people:

- A **bad token is `401`, never `403`.** There is no `403` on this endpoint.
- `onprem_only` + no healthy on-prem model → **`422 no model is available…`**,
  never a silent cloud call.
- The `503` is now narrow: **only** a privacy-gate error. "No candidates" is
  `422`, backend exhaustion is `502`, and an agent step-cap is `500` — each maps
  to its own honest status rather than a catch-all `503`.

Handle `429` with backoff; treat a `422`/`503` rejection as "routeForge declined
to serve this safely," not "server crashed."

---

## `GET /v1/models`

App-token authenticated (same `Authorization: Bearer rf_live_…` as inference;
`401` without a valid token). Returns the OpenAI model list, **scoped to your
application**: exactly the ids you may send as `model` on
`/v1/chat/completions`. The list applies your app's residency,
`allowed_providers`, `allowed_tiers` and `allowed_models`, plus per-model
onboarding restrictions, and drops disabled models.

```json
{"object": "list",
 "data": [{"id": "qwen3.8-27b", "object": "model", "created": 0, "owned_by": "onprem"}]}
```

A request whose content raises residency (for example PII under an
`onprem_only` rule) can narrow the choice further than this list. `created` is
always `0`. Omitting `model` still lets RouteForge route.

---

## `POST /v1/feedback`

App-token authenticated (same `Authorization: Bearer rf_live_…` as inference).
Posts an evaluation/RLHF score for a prior request straight to your app's **own**
residency-local Langfuse project — never the shared project.

Body — `{ trace_id, score_name, value, comment?, corrected_output? }`. `value` is
a number or string; `score_name` must be one of the recognized score names
(otherwise `422 unknown score_name: …`). Returns `{"stored": <bool>}` (HTTP
`200`); `stored: false` when your app has no provisioned Langfuse project — a
missing project is not an error.

---

## `GET /metrics`

No auth. A Prometheus scrape surface (in-process exporter; `503 metrics
unavailable: install the observability extra` when the `observability` extra
isn't installed). Bounded labels only — no tenant or PII. See
[`routing-observability.md`](./routing-observability.md).

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
    model="auto",                 # router chooses; a concrete slug pins to that model
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
[`agentic-app-guide.md`](../guides/agentic-app-guide.md).
