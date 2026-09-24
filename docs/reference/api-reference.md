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
Optional: `x-routeforge-session` (see [Sessions and tracing](#sessions-and-tracing)) and
`x-routeforge-workload` (see [Workload tag](#workload-tag)).

Body:

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `messages` | array of `{role, content, …}` | **yes** | — | Standard chat messages. `role` is `system`/`user`/`assistant`/`tool`; `content` is a string **or `null`** (an assistant turn that only requested tools). Tool-calling turns may also carry `tool_calls`, `tool_call_id` and `name` — see [Tool calling](#tool-calling). |
| `model` | string | no | `"auto"` | `"auto"` lets the router choose. **A non-`"auto"` value is a hard model pin** — see below. |
| `stream` | bool | no | `false` | Per-app capability via `stream_mode`; default (unset) still **rejected** (`400`). See [Streaming](#streaming). |
| `tools` | array of tool definitions | no | `null` | Standard OpenAI tool/function definitions, passed through to the backend. See [Tool calling](#tool-calling). |
| `tool_choice` | string \| object | no | `null` | Standard OpenAI tool-choice control, passed through with `tools`. |
| `max_tokens` | integer ≥ 1 | no | — | Output length cap — forwarded to the backend. Priced at this cap if set, else the default estimated output tokens. A non-integer or value < 1 is rejected `422`. Because it is forwarded, a prompt plus `max_tokens` larger than the backend's context window is refused upstream and returned as `400 request exceeds the model's context window` rather than being silently dropped. |
| `max_completion_tokens` | integer ≥ 1 | no | — | OpenAI-compatible alias for `max_tokens` — forwarded to the backend, validated the same way. |
| `temperature` | float | no | — | Forwarded to the backend. |
| `top_p` | float | no | — | Forwarded to the backend. |
| `stop` | string \| array | no | — | Stop sequences — forwarded to the backend. |
| `seed` | integer | no | — | Forwarded to the backend. |
| `presence_penalty` | float | no | — | Forwarded to the backend. |
| `frequency_penalty` | float | no | — | Forwarded to the backend. |
| `response_format` | object | no | — | Forwarded to the backend (e.g. `{"type": "json_object"}`). |
| `parallel_tool_calls` | boolean | no | — | Forwarded to the backend. |

Unknown fields are **accepted, not rejected** (the model allows extras). The
OpenAI `metadata` object is accepted too; only its `workload` key is read (see
[Workload tag](#workload-tag)), and `metadata` is never forwarded to the backend. The
demo-only hint fields `mock_confidence` / `mock_fail` (mock-backend generation
hints) are read if present. See
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
  "id": "req-1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6", // request id: "req-" + 32 hex (a UUID4) — correlates trace + usage
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
  cache hit and streaming). The `openai` SDK's `ChatCompletion` model
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
  as `trace_id` to `POST /v1/feedback` to attach a score to this request. `id` works too,
  as `request_id`, and needs no telemetry.

### The `trace` array

The trace is the "why." Each element is one of:

**`DecisionRecord`** — one per routing decision. RouteForge answers every
request with a single model call, so a request's trace carries one `router`
record (plus a `gateway` record when a candidate failed and the gateway fell
back). An application's agent loop produces one request, and therefore one
trace, per hop:

| Field | Meaning |
|---|---|
| `step` | `router` \| `gateway` |
| `chosen` / `chosen_display_name` | Model slug / human name selected |
| `chosen_tier` / `chosen_provider` | Tier and provider of the chosen candidate |
| `reason` | Human-readable rationale (the demo line) |
| `rejected[]` | Candidates not chosen — each `{name, provider, tier, reason, display_name}`, including "excluded by policy" |
| `est_cost_usd` / `actual_cost_usd` | Estimated vs actual USD for this call |
| `latency_ms` | This call's latency |
| `hop_index` | Index of this record within the request's trace (0 for the first) |
| `tokens_priced` / `tokens_out` | Post-compression prompt tokens the cost was computed on / completion tokens for this call |
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
  single completion. routeForge runs no agent loop of its own — the client drives
  the loop (see [`agentic-app-guide.md`](../guides/agentic-app-guide.md)).
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
400
{"error": {"message": "streaming is not supported for this application: set the application's stream_mode (verified_then_stream, stream_unverified or incremental_verify)",
           "type": "invalid_request_error", "param": null, "code": "streaming_not_supported"}}
```

(All `/v1/*` errors use this envelope — see [Errors](#errors) below.)

Output verification (step 9) needs the **complete** response, which token
streaming can't provide without a partial-leak window — which is why streaming is
gated and why `onprem_only` / `require_verification` requests always fall back to
the fully-verified path regardless of `stream_mode`. (The dashboard's live
decision stream is a *separate*, admin-only channel — see
[Decision stream](#the-decision-stream-admin-only).)

### Token usage on a stream (`stream_options.include_usage`)

Send `"stream_options": {"include_usage": true}` to receive token usage on a stream,
exactly as OpenAI does. The stream then ends with one extra frame, after the
`finish_reason` frame and before `data: [DONE]`, whose `choices` array is empty:

```
data: {"id":"req-…","object":"chat.completion.chunk","created":…,"model":"…","choices":[],"usage":{"prompt_tokens":53,"completion_tokens":8,"total_tokens":61,"cached_prompt_tokens":0}}
data: [DONE]
```

The numbers are RouteForge's own accounting, the same `usage` object a non-streamed
response carries (including the `cached_prompt_tokens` extension). The frame is
emitted in every `stream_mode`. Without `include_usage: true` the stream is
unchanged and carries no usage frame. A stream that ends early (client disconnect)
or fails at the backend has no usage frame. Agent clients that size their context
from reported usage (for example Qwen Code's auto-compaction) need this set:
without it they fall back to a local estimate that can undercount token-dense
history badly (B53).

### Keepalive comments for long-running requests

When a request waits for a complete, verified answer — such as a tool turn, `verified_then_stream` streaming, or a request floored to the fully-verified path — RouteForge sends SSE keepalive comment lines every `server.stream_keepalive_s` seconds (default 5) once the answer takes longer than one interval. The format is `: keepalive` on a line by itself. OpenAI-compatible clients ignore comment lines, so keepalive frames do not affect request/response semantics. They are transparent to tool calling and standard streaming clients.

**Why keepalives matter:** Some OpenAI-SDK-based agent clients apply a separate request timeout (often 120 s) to connect and first response bytes. Without keepalives, a request that produces its first byte after 120 s abandons the backend work and retries, leading to overlapped computation and multiple identical usage rows. Keepalives are a few bytes each and hold the connection open.

### Late failures as SSE error frames

If an error occurs after the SSE stream has opened (status 200 is on the wire), the error is conveyed as an SSE frame:

```
data: {"error": {"message": "<detail>", "type": "<openai-type>", "param": null, "code": "<openai-code>"}}
data: [DONE]
```

**This shape changed (B49).** The frame used to carry the constant `"type":
"routeforge_error"` and `"code": <http-status>` (the integer status, e.g. `502`).
It now shares the exact same `(type, code)` mapping the non-streamed HTTP body
uses (see [Errors](#errors) below) — `type` is a real OpenAI error type
(`invalid_request_error`, `rate_limit_error`, `insufficient_quota`,
`server_error`) and `code` is a string code (e.g. `"context_length_exceeded"`),
never an integer. A failure now renders identically whether or not the
response had already started streaming. Anything parsing the old frame shape
by field type must be updated.

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

### Workload tag

An optional label for the kind of work a request does, such as `claims.summary`. It
groups your requests in RouteForge's routing outcome records
([`routing-outcomes.md`](./routing-outcomes.md)). It **never affects routing** and is not
echoed on the response.

- Send it as the `x-routeforge-workload` header, or as `metadata.workload` in the body.
  The header wins whenever it is present, even if it is invalid.
- The value is lower-cased and must match `^[a-z0-9][a-z0-9._-]{0,63}$`.
- **An invalid tag is dropped, never rejected.** The request is served normally (`200`)
  and the outcome record notes that the tag was invalid.
- `metadata` is never forwarded to the backend.
- **Do not put personal data in it.** Like `x-routeforge-session`, it is a label for a
  kind of work, not an identifier for a person or a record. It is stored as given.

---

## Errors

**Every failure on `/v1/*` renders in the OpenAI error envelope (B49):**

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "context_length_exceeded"}}
```

The HTTP status is unchanged from before this shape existed; only the body
changed. `param` is always present and `null` — routeForge never attributes a
failure to one request field. The OpenAI Python SDK reads `code`/`type` off
the inner `error` object, so `openai.BadRequestError.code` is exactly the
string in the table below — this is what let a real OpenAI-SDK client (the
origin of this fix) show a real message instead of "API error".

**`/admin/*` and `/api/*` are not affected** — those keep FastAPI's
`{"detail": ...}` shape, unchanged, because the admin console reads `detail`
directly. This envelope is `/v1/*` only.

Every error the inference endpoint can return:

| Status | `error.message` | `code` | Cause |
|---|---|---|---|
| `400` | `streaming is not supported for this application: set the application's stream_mode (verified_then_stream, stream_unverified or incremental_verify)` | `streaming_not_supported` | You sent `stream: true` and your app has no `stream_mode`; set one, or send `stream: false`. |
| `401` | `invalid or missing API token` | `invalid_api_key` | Missing/unknown/revoked token, or the application is disabled. **Fail closed.** |
| `422` | *(FastAPI validation error, e.g. `messages: field required`)* | `invalid_request` | Malformed body — e.g. missing `messages`. Extra unknown fields do **not** cause this. |
| `422` | `no model is available for this request's policy: no candidates after residency filter: <counts and fixes>` | `no_eligible_model` | Nothing survived placement or app scope; the detail lists each reason and the field to change. |
| `400` | `request exceeds the model's context window: <model>: This model's maximum context length is … -- shorten the conversation …` | `context_length_exceeded` | Prompt + `max_tokens` is larger than every tried model's context window. Compact or summarise the history, or lower `max_tokens`. Returned only when **every** candidate refused for this reason; retrying the same request will fail the same way. |
| `422` | `no model is available for this request's policy: no candidates after budget filter: cheapest candidate … estimated at $X … over the per-request cap $Y …` | `no_eligible_model` | Raise the app's `max_cost_usd_per_request`, lower `max_tokens`, or lower the model's price. The router has already tried cheaper allowed tiers. |
| `422` | `model request rejected: requested model '<model>' is not permitted or unavailable: <gate>` | `model_not_found` | The pinned `model` failed the named gate; `GET /v1/models` lists what this app can pin. |
| `422` | `response failed verification: <detail>` | `response_failed_verification` | Output leakage / verification failed. |
| `422` | `unknown score_name: <name>` (`POST /v1/feedback` only) | `invalid_value` | `score_name` is not one of the recognized names. |
| `422` | `one of trace_id or request_id is required` (`POST /v1/feedback` only) | `invalid_value` | The feedback body names no request. |
| `429` | `rate limit exceeded for this application` | `rate_limit_exceeded` | Over your `max_rpm`. |
| `429` | `daily budget exceeded for this application` | `insufficient_quota` | Over your `max_spend_usd_per_day`. |
| `500` | `internal error` | `internal_error` | Unexpected server-side failure. |
| `502` | `no backend could serve this request: … (last error: <model>: <ErrorType>: <message>)` | `backend_unavailable` | Every candidate failed upstream; the last upstream error is included. A context-window refusal mixed with any other failure stays `502`. |
| `503` | `policy evaluation unavailable` | `policy_unavailable` | **Fail-closed:** the privacy gate itself errored. |

**Which codes are whose.** `context_length_exceeded`, `invalid_api_key`,
`model_not_found`, `rate_limit_exceeded`, `insufficient_quota` and
`invalid_value` are **OpenAI's own codes** — the ones a client's existing
`except openai.BadRequestError` branches already look for. `no_eligible_model`,
`response_failed_verification`, `streaming_not_supported`,
`backend_unavailable`, `policy_unavailable`, `internal_error` and
`invalid_request` are **routeForge's own** — conditions OpenAI has no
equivalent for. routeForge
never reuses an OpenAI code for a condition OpenAI does not have (a policy
rejection is never dressed up as `insufficient_quota`, for example). An
`HTTPException` raised on `/v1/*` with no code registered for it still renders
a valid envelope: `type` falls back to the status class
(4xx → `invalid_request_error`, 5xx → `server_error`) and `code: null` — this
should not currently be reachable for any status in the table above, but the
fallback exists so a future addition can't ship without a valid envelope.

**Client-side example** (the OpenAI Python SDK):

```python
import openai

client = openai.OpenAI(base_url="http://<host>:<port>/v1", api_key="rf_live_...")
try:
    client.chat.completions.create(model="auto", messages=[...])
except openai.BadRequestError as e:
    if e.code == "context_length_exceeded":
        # shorten the conversation and retry
        ...
```

Things that surprise people:

- A **bad token is `401`, never `403`.** There is no `403` on this endpoint.
- `onprem_only` + no healthy on-prem model → **`422 no model is available…`**,
  never a silent cloud call.
- The `503` is now narrow: **only** a privacy-gate error. "No candidates" is
  `422`, a request too large for the context window is `400`, backend exhaustion is `502`, and an agent step-cap is `500` — each maps
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
 "data": [{"id": "qwen3.8-27b", "object": "model", "created": 0, "owned_by": "onprem",
           "max_model_len": 131072}]}
```

A request whose content raises residency (for example PII under an
`onprem_only` rule) can narrow the choice further than this list. `created` is
always `0`. Omitting `model` still lets RouteForge route.

**`max_model_len` (B50).** An onboarded (on-prem) model's context window, in
tokens — vLLM's own field name, used here because it's the one clients
already read. Present only when known and **omitted entirely** (never `null`
or `0`) when it is not — a static cloud pool entry from `pricing.yaml`/
`routing.yaml` has no such column and always omits it. The value is captured
from the endpoint's own `GET /models` (`max_model_len`) when the endpoint
onboards or is edited, or set explicitly by the operator; it is **never
guessed**, and **routeForge does not enforce it** — publishing the number
does not stop an oversized request from being sent. An oversized request
still fails upstream and returns as this endpoint's own `400
context_length_exceeded` (B36; see [Errors](#errors) above). See
[`onboarding-models.md`](../guides/onboarding-models.md) for how the value is
captured and kept current.

---

## `POST /v1/feedback`

App-token authenticated (same `Authorization: Bearer rf_live_…` as inference).
Posts an evaluation/RLHF score for a prior request straight to your app's **own**
residency-local Langfuse project — never the shared project.

Body — `{ request_id?, trace_id?, score_name, value, comment?, corrected_output? }`.

- **Name the request** by `request_id` (the response `id`), by `trace_id`, or both. At
  least one is required; neither gives `422 one of trace_id or request_id is required`.
  `request_id` works without telemetry. With only `request_id`, the Langfuse post uses
  that request's own `trace_id`, if it has one.
- `score_name` must be one of `route_correct`, `preferred_model`, `failure_type`,
  `quality`, `corrected_output` (otherwise `422 unknown score_name: …`).
- `value` is a number or a string. A string is stored locally only if it matches
  `[A-Za-z0-9._:/-]{1,100}` (so a model slug such as `nvidia/llama-3.1-nemotron-nano-8b-v1`
  fits); a number only if it is finite.
- `comment` and `corrected_output` go to your Langfuse project only. RouteForge never
  stores their text itself.

Returns `{"stored": <bool>, "recorded": <bool>}`, always HTTP `200`:

- `stored` — the score reached your app's own Langfuse project. `false` when your app
  has no provisioned project or no trace id is known. A missing project is not an error.
- `recorded` — the score was written to RouteForge's own outcome records
  ([`routing-outcomes.md`](./routing-outcomes.md) §6). Lookups are scoped to your
  application: a request that belongs to another app is treated exactly like one that
  does not exist, and gives `recorded: false`. Also `false` when outcome records are
  switched off, or `value` fails the checks above.

---

## `GET /admin/outcomes/export` (admin-only)

Not for applications: it needs the admin session cookie, not an app token (`401`
without it). It streams RouteForge's routing outcome records, with their feedback, as
NDJSON (`application/x-ndjson`), one `routeforge.outcome/v1` record per line, across all
applications.

| Parameter | Default | Notes |
|---|---|---|
| `since`, `until` | the last 7 days | ISO-8601; a value with no offset is UTC; a bad value is `422` |
| `application_id` | all | filter to one application |
| `limit` | `100000` | capped at 100 000; below 1 is `422` |

Errors use FastAPI's `{"detail": ...}` shape, like the rest of `/admin/*`. The records
hold ids, enum values, numbers, entity types and version hashes only, never request or
response text. Line format, columns and an example:
[`routing-outcomes.md`](./routing-outcomes.md) §7.

---

## `POST /admin/quality/calibrations` (admin-only)

Admin-only, like every route in this section (`401` without the admin session cookie).
Validates and, unless `?dry_run=true`, applies a `recommendation.json` produced by
`scripts/quality_recommend.py` — the offline, human-approved quality-calibration workflow
(P2b-3). Body is the recommendation file as-is; query params `dry_run` (default `false`)
and `force_registry_mismatch` (default `false`).

A validation failure (unsupported schema, a `set_id` that doesn't match the file's own
content hash, an unknown model, an out-of-range score, or a registry-version mismatch
without `force_registry_mismatch`) is `422` and writes nothing. Re-applying an already-
applied `set_id` is `409`. A dry run previews each recommended unit's current effective
quality/source and proposed score, and whether a `customer` row would still win, without
writing anything; an apply writes one `model_quality` row per recommended unit and
refreshes this replica's routing quality snapshot immediately.

## `GET /admin/quality/calibrations` (admin-only)

Lists applied calibration sets: `[{"set_id", "applied_at", "units"}, ...]`, newest first.

## `DELETE /admin/quality/calibrations/{set_id}` (admin-only)

Reverts one applied set (deletes its `model_quality` rows and refreshes this replica's
snapshot immediately). `404` if `set_id` is not an applied set. Other sets and any
`customer` row are untouched.

Full field reference, the estimator, the replay diff, precedence and the refresh
behaviour: [`quality-calibration.md`](./quality-calibration.md).

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
