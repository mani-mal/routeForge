# RouteForge Policy and Data-Protection Redesign Handoff

## Purpose

This document is an implementation and documentation handoff for correcting RouteForge's policy model around:

- What the model is allowed to see
- What tools are allowed to see
- What the client is allowed to receive
- What Langfuse and other telemetry sinks are allowed to store

The current documents couple several of these decisions through `policy_mode`. That makes simple presets easy, but it prevents important enterprise configurations, particularly:

> Let a trusted model and trusted tool use the complete request, redact sensitive data from the final client response, and send only redacted content or no content to Langfuse.

This handoff describes the current documented behavior, the gaps, the target design, required documentation changes, and acceptance tests.

## Important scope statement

Semantic cache is **not implemented yet** and is out of scope for this change.

- Do not implement semantic caching as part of this work.
- Do not rely on a cache to preserve, restore, or protect PII.
- Any existing documentation that says semantic-cache pipeline wiring has shipped must be corrected to say **planned/not implemented**.
- The future cache design must eventually consume an explicitly named protected data representation, but that future work must not block this policy redesign.

## Source documents reviewed

This handoff is based on the attached RouteForge documents titled:

- `choosing-app-settings.md`
- `creating-a-sample-application.md`
- `faq.md`
- `getting-started.md`

Before changing code, compare the executable implementation against these documents. Where the code differs, record the difference and decide whether the code or the document represents the intended behavior. Do not silently assume the documents are accurate.

### Material changes found in the revised documents

This revision is necessary. The newly supplied documents materially change or clarify the earlier policy description:

1. A non-`onprem_only` application in `policy_mode: enforced` may use `capture_io: full` when it has its own Langfuse project. Only the `onprem_only + enforced + full` combination is prohibited. The previous handoff incorrectly described full capture as prohibited under `enforced` at every residency.
2. The guides now describe four streaming modes: `off`, `verified_then_stream`, `stream_unverified`, and `incremental_verify`. These modes must be validated against the proposed client-output policy so streaming cannot bypass required redaction or whole-response verification.
3. The documents explicitly define `capture_io: full` as the raw inbound prompt and raw model completion, even when the model itself received a sanitized prompt under `enforced`. This confirms that model visibility and telemetry visibility are already partly independent.
4. Application/model eligibility is two-sided: the model's `allowed_app_ids` and the application's provider/tier/model policy must both permit the model. Privacy classification may further raise effective residency per request, but it may never lower the application's residency floor.
5. Several contradictions remain and are now more visible: the decision guide says raw capture requires passthrough, while the FAQ and setup guide allow full capture under non-on-prem `enforced`; regex is described as performing redaction but is also treated as producing no sanitized telemetry view; and the exporter says it falls back to `redacted` even when the redacted path is declared unsafe under passthrough or disabled detection.

---

## 1. Example used throughout this handoff

The client sends:

```text
Summarize the support ticket for Jane Smith.
Her SSN is 123-45-6789.
```

The support-ticket tool requires the real SSN to locate the correct record:

```json
{
  "tool": "find_support_ticket",
  "arguments": {
    "ssn": "123-45-6789"
  }
}
```

The raw model completion could be:

```text
Jane Smith, SSN 123-45-6789, requested a password reset.
```

One desired client response is:

```text
[PERSON], SSN [SSN], requested a password reset.
```

The policy engine must be able to decide independently what each participant sees.

---

## 2. Current behavior described by the documents

### 2.1 Current policy coupling

The documents define two coarse protection postures:

| Current posture | Model input | Model output sent to client | PII detector |
|---|---|---|---|
| `policy_mode: enforced` | Redacted/sanitized | Egress-verified and redacted | Active |
| `policy_mode: passthrough` | Raw | Raw | Removed/null |

Telemetry is configured separately with `capture_io`, but validators currently couple it back to the protection posture.

### 2.2 Current protected recipe

```json
{
  "policy_mode": "enforced",
  "capture_io": "redacted"
}
```

Documented flow:

```text
Raw request
  → PII detection
  → input redaction
  → sanitized request to model
  → model/tool execution
  → raw completion
  → egress verification and redaction
  → redacted response to client
  → redacted I/O to telemetry
```

Using the SSN example, the model receives something like:

```text
Summarize the support ticket for [PERSON]. Her SSN is [SSN].
```

There is no documented reversible mapping between `[SSN]` and `123-45-6789`. Therefore, a model-generated tool call can only use the placeholder it saw. A tool requiring the actual SSN cannot complete the lookup unless some undocumented mechanism restores it.

### 2.3 Current passthrough recipe

```json
{
  "residency": "onprem_only",
  "policy_mode": "passthrough",
  "capture_io": "full"
}
```

Documented flow:

```text
Raw request
  → raw request to model
  → model can place raw SSN in tool call
  → raw completion
  → raw response to client
  → raw I/O to the application's own Langfuse project
```

This lets the model and tool use the SSN, but it does not support redacting the final response.

`onprem_only` still prevents cloud providers from entering the routing pool. Placement is independent from protection.

### 2.4 Current passthrough with telemetry content off

```json
{
  "residency": "onprem_only",
  "policy_mode": "passthrough",
  "capture_io": "off",
  "compression_enabled": false
}
```

Documented result:

| Surface | Content exposure |
|---|---|
| Model | Raw request |
| Tool | Potentially raw tool arguments |
| Client | Raw completion |
| Langfuse prompt/completion content | None |
| Routing/cost/latency/token metadata | Still available |

`capture_io: off` means **no I/O content capture**, not necessarily no telemetry record at all. Documentation and UI labels must make that distinction explicit.

### 2.5 Current strict recipe

```json
{
  "policy_mode": "enforced",
  "capture_io": "off"
}
```

Documented result:

| Surface | Content exposure |
|---|---|
| Model | Redacted request |
| Tool | No documented way to recover the real SSN |
| Client | Redacted response |
| Langfuse prompt/completion content | None |
| Routing/cost/latency/token metadata | Still available |

This protects PII but does not support the SSN-dependent tool call.

### 2.6 Current detection-off variant

The documents also describe:

```json
{
  "residency": "onprem_only",
  "policy_mode": "enforced",
  "pii_detection": "off",
  "capture_io": "off"
}
```

The intended result appears to be raw model input, raw output, and no telemetry I/O content. However, the documents also say custom recognizers may continue to run under `pii_detection: off`, while `passthrough` removes all recognizers. This behavior must be verified in code and documented unambiguously.

### 2.7 Current full-capture rule depends on residency

The revised documents define the following rule:

| Effective configuration | Is `capture_io: full` allowed? | Additional requirement |
|---|---:|---|
| `onprem_only + enforced` | No | Use `redacted` or `off`, or explicitly change the protection posture |
| `onprem_only + passthrough` | Yes | Application-specific Langfuse project |
| `private_cloud_ok/any + enforced` | Yes | Application-specific Langfuse project |
| `private_cloud_ok/any + passthrough` | Yes | Application-specific Langfuse project |

Under `private_cloud_ok/any + enforced + full`, the documented data path is unusual but important:

```text
raw inbound request
  ├─ policy gate → sanitized request → model
  └─ raw telemetry projection → application-specific Langfuse project

raw model completion
  ├─ egress verification/redaction → client
  └─ raw telemetry projection → application-specific Langfuse project
```

Therefore, a protected model input and redacted client response can currently coexist with raw telemetry for a non-`onprem_only` application. Preserve this behavior unless the product owner deliberately changes it. Do not reintroduce the previous, overly broad rule that all `enforced + full` combinations are illegal.

The documents themselves are inconsistent here: `choosing-app-settings.md` says raw capture requires a no-protection posture, but `faq.md` and `getting-started.md` explicitly allow full capture for a non-`onprem_only` application in `enforced` posture. The code and authoritative per-app configuration reference must decide the rule, and every guide must then say the same thing.

### 2.8 Current streaming modes affect output protection

The revised setup guide defines:

| `stream_mode` | Documented behavior | Protection consequence |
|---|---|---|
| `off` | Reject `stream: true` | Full non-streaming verification remains available |
| `verified_then_stream` | Complete and verify the entire answer, then emit chunks | Compatible with mandatory output redaction; no true latency benefit |
| `stream_unverified` | Release real-time tokens and waive step-9 verification | Incompatible with any policy that requires redaction or whole-response verification; already blocked for `onprem_only` |
| `incremental_verify` | Hold back enough text to redact entities across chunk boundaries | Can support incremental PII redaction, but cannot enforce a whole-response verdict |

The current floor says `onprem_only` or `require_verification` requests can never waive verification and fall back to `verified_then_stream`. The redesigned `client_output` policy must add another explicit floor: `client_output: redact` or `authorized_rehydrate` must never silently use `stream_unverified`.

---

## 3. Current scenario matrix

In this table, **protected model input** means the model does not see the original PII. **Raw client output** means there is no mandatory egress redaction policy.

| Model input | Client output | Langfuse I/O | Supported by current documents? | Notes |
|---|---|---|---|---|
| Raw | Raw | Full/raw | Yes | `passthrough + full`; also potentially non-on-prem `enforced + pii_detection: off + full`; application-specific Langfuse project required |
| Raw | Raw | Redacted | No | `passthrough + redacted` is rejected |
| Raw | Raw | Off | Yes | `passthrough + off` |
| Raw | Redacted | Full/raw | No | Input and output protection cannot be separated |
| Raw | Redacted | Redacted | No | Major missing enterprise scenario |
| Raw | Redacted | Off | No | Major missing enterprise scenario |
| Protected | Raw | Full/raw | No | `enforced` still applies egress verification/redaction; there is no independent raw-output control |
| Protected | Raw | Redacted | No | Egress redaction cannot be independently disabled |
| Protected | Raw | Off | No | Sensitive output would require authorized rehydration |
| Protected | Redacted | Full/raw | **Yes, conditionally** | Allowed for non-`onprem_only + enforced` with an application-specific Langfuse project; prohibited for `onprem_only + enforced` |
| Protected | Redacted | Redacted | Yes | Current protected recipe |
| Protected | Redacted | Off | Yes | Current strict/no-I/O recipe |

Nuance: when egress verification finds no sensitive entities, an `enforced` response may happen to equal the raw model completion. That is not the same as providing an explicit **never redact output** policy.

Another important nuance: `capture_io: full` means raw ingress and raw completion in telemetry, not merely "the exact sanitized payload sent to the model." Consequently, an `enforced` application can protect the model and client while still exposing the originals to its own Langfuse project when the residency rule permits it.

---

## 4. Primary design problem

`policy_mode` currently controls too many independent decisions:

1. Whether the input detector runs
2. Whether input is redacted before the model
3. Whether custom recognizers run
4. Whether sensitivity and risk are escalated
5. Whether egress detection/redaction runs
6. Which telemetry capture values are legal

This prevents RouteForge from expressing policies such as:

```text
Model input: raw
Trusted tool input: raw
Client output: redacted
Telemetry I/O: redacted or off
Placement: on-prem only
```

These are separate security decisions and must become separate policy axes.

---

## 5. Target policy model

### 5.1 Required independent axes

Introduce explicit controls similar to the following. Exact field names may change to fit the codebase, but the semantics must remain independent.

```yaml
residency: onprem_only

data_handling:
  model_input: raw | redact | tokenize
  tool_arguments: inherit_model | redact | resolve_tokens | trusted_raw
  tool_results_to_model: raw | redact | tokenize
  client_output: raw | redact | authorized_rehydrate

telemetry:
  capture_io: off | redacted | full

delivery:
  compression_enabled: false
  stream_mode: off | verified_then_stream | stream_unverified | incremental_verify
```

Definitions:

| Field | Meaning |
|---|---|
| `model_input: raw` | Model receives the original request content |
| `model_input: redact` | Detected values are replaced irreversibly before model invocation |
| `model_input: tokenize` | Detected values become opaque, request-scoped references that can be securely resolved |
| `tool_arguments: inherit_model` | Tool receives only the representation available to the model |
| `tool_arguments: redact` | Tool arguments are independently inspected and redacted |
| `tool_arguments: resolve_tokens` | An approved broker resolves protected references only for an authorized tool |
| `tool_arguments: trusted_raw` | A registered trusted tool may receive raw values under an explicit allowlist |
| `tool_results_to_model: raw` | The model receives the complete tool result |
| `tool_results_to_model: redact` | Tool output is inspected and sanitized before returning to the model |
| `tool_results_to_model: tokenize` | Sensitive tool-result values are converted to resolvable references |
| `client_output: raw` | Client receives post-model content without PII redaction |
| `client_output: redact` | Egress detector runs regardless of the model-input mode |
| `client_output: authorized_rehydrate` | Protected references are restored only after an end-user authorization decision |
| `capture_io: off` | No prompt, completion, tool arguments, or tool results are stored in telemetry |
| `capture_io: redacted` | Telemetry independently creates and stores redacted copies of every I/O surface |
| `capture_io: full` | Raw I/O may be stored only in the application's own approved Langfuse project |

`stream_mode` remains a delivery control, but its legal values must be derived from the effective client-output requirements:

- `client_output: redact` permits non-streaming, `verified_then_stream`, or a deliberately accepted `incremental_verify` mode. It must reject or safely override `stream_unverified`.
- `client_output: authorized_rehydrate` requires a complete authorization and rehydration decision before release, so its safe default is `verified_then_stream`.
- `client_output: raw` may use `stream_unverified` only when residency, verification requirements, and deployment ceilings all permit it.
- A fallback from an illegal streaming choice must be visible in the decision trace; it must never silently weaken output protection.

### 5.2 Backward compatibility

Keep `policy_mode` as a backwards-compatible preset instead of the sole enforcement mechanism.

Suggested default mappings:

```yaml
policy_mode: enforced
# maps to:
model_input: redact
tool_arguments: inherit_model
tool_results_to_model: redact
client_output: redact
```

```yaml
policy_mode: passthrough
# maps to:
model_input: raw
tool_arguments: inherit_model
tool_results_to_model: raw
client_output: raw
```

Recommended precedence:

1. Deployment-wide security ceilings
2. Explicit per-application axis values
3. `policy_mode` preset values
4. Deployment defaults

Every effective field must be included in `app_policy_version`.

Do not silently change existing applications. Existing persisted policies must continue to produce their current behavior until an operator explicitly adopts a new independent setting or recipe.

In particular, backward compatibility must preserve the revised documents' current full-capture behavior:

- `onprem_only + enforced + full` remains rejected.
- `onprem_only + passthrough + full` remains allowed with an application-specific Langfuse project.
- Non-`onprem_only + enforced + full` remains allowed with an application-specific Langfuse project.
- Moving an application between residency classes must revalidate the complete effective policy atomically.

---

## 6. Required supported scenarios

### Scenario A: Existing router-only/raw mode

```yaml
model_input: raw
tool_arguments: inherit_model
tool_results_to_model: raw
client_output: raw
capture_io: full
```

Expected result:

- Model sees the SSN.
- Model can place the SSN in the tool call.
- Tool can use it, subject to normal tool controls.
- Client receives the raw completion.
- Raw I/O goes only to the application's own Langfuse project.
- Streaming follows the existing floors: `stream_unverified` is still illegal for `onprem_only` or `require_verification`; use `off` or `verified_then_stream` there.

### Scenario B: Trusted compute, safe client response

This is the main missing scenario.

```yaml
model_input: raw
tool_arguments: trusted_raw
tool_results_to_model: raw
client_output: redact
capture_io: redacted
stream_mode: verified_then_stream
```

Expected result:

- Model sees `Jane Smith` and `123-45-6789`.
- Approved tool receives the real SSN.
- Raw model completion may contain PII internally.
- Egress verification independently redacts the completion.
- Client receives `[PERSON]` and `[SSN]`.
- Langfuse receives independently redacted prompt, tool I/O, and completion.
- No raw response token is released before egress redaction. If real streaming is required, `incremental_verify` must be an explicit weaker alternative with a visible trace record.

The same scenario must work with `capture_io: off`.

### Scenario C: Model protected, trusted tool functional

```yaml
model_input: tokenize
tool_arguments: resolve_tokens
tool_results_to_model: redact
client_output: redact
capture_io: redacted
stream_mode: verified_then_stream
```

Expected result:

- Model does not see the SSN.
- Model receives an opaque reference such as `pii_7f92`.
- Model asks an approved tool to look up `pii_7f92`.
- The trusted broker resolves the reference to the real SSN.
- Tool receives the SSN and finds the ticket.
- Tool response is sanitized before the model sees it.
- Client and Langfuse receive redacted content.

### Scenario D: Model protected, authorized user receives sensitive fields

```yaml
model_input: tokenize
tool_arguments: resolve_tokens
tool_results_to_model: tokenize
client_output: authorized_rehydrate
capture_io: redacted
stream_mode: verified_then_stream
```

Expected result:

- Model does not see the original PII.
- Trusted tool resolves only the references it is allowed to use.
- Model response contains protected references.
- RouteForge or the calling application verifies end-user authorization.
- Only authorized fields are restored for the authorized user.
- Telemetry remains redacted.

If RouteForge does not receive trustworthy end-user identity and authorization claims, rehydration must remain the calling application's responsibility. An application API token alone must not be treated as end-user authorization to view an SSN.

### Scenario E: Fully protected with no I/O telemetry

```yaml
model_input: redact
tool_arguments: redact
tool_results_to_model: redact
client_output: redact
capture_io: off
stream_mode: verified_then_stream
```

Expected result:

- No raw PII reaches the model or tool.
- Client receives redacted content.
- Telemetry retains decision metadata only.
- A tool that requires raw PII must be rejected or declared unusable under this policy.

---

## 7. Protected tool broker requirements

Generic placeholders such as `[SSN]` are not sufficient for functional tool use. They cannot distinguish two SSNs and cannot be resolved securely.

Implement a protected-reference abstraction for `tokenize`/`resolve_tokens` modes.

Example:

```text
Raw request:
"Summarize Jane Smith's ticket. SSN: 123-45-6789"

Model-visible request:
"Summarize the ticket for customer_ref pii_7f92"

Protected mapping:
pii_7f92 → {type: US_SSN, value: 123-45-6789}
```

Model-generated tool call:

```json
{
  "tool": "find_support_ticket",
  "arguments": {
    "customer_ref": "pii_7f92"
  }
}
```

The broker must:

1. Confirm the tool is registered and enabled for the tenant/application.
2. Confirm the tool is in an approved trust zone.
3. Confirm the tool is allowed to receive the entity type, such as `US_SSN`.
4. Confirm the reference belongs to the same tenant, application, request, and active agent run.
5. Prevent one tool from resolving a reference issued for another tool when scope is restricted.
6. Resolve the reference only immediately before invocation.
7. Inspect the tool result before it is sent to the model, client, telemetry, logs, or error traces.
8. Expire and destroy the mapping after the request or a short TTL.
9. Record an audit event containing the entity type and policy decision, never the raw value.
10. Fail closed when a token is missing, expired, from another tenant, or presented to an unauthorized tool.

Suggested per-tool policy shape:

```yaml
tools:
  find_support_ticket:
    trust_zone: onprem
    allowed_sensitive_entities:
      - US_SSN
    argument_mode: resolve_tokens
    result_to_model: redact
    telemetry: redacted
```

The implementation must explicitly cover:

- Tool definitions included in the client request
- Model-generated tool arguments
- Client-replayed assistant tool calls
- Client-supplied tool-result messages
- Internal multi-step agent tool arguments and results
- Tool errors and exception messages

Headroom bypassing client-driven tool calling is a compression behavior, not a privacy control. Do not treat the existing Headroom bypass as protection for tool I/O.

---

## 8. Independent telemetry projection

### Current problem

The documents say the `redacted` exporter reuses the policy gate's `sanitized_prompt`. Consequently, when the model is intentionally given raw input, RouteForge cannot produce redacted telemetry and rejects the configuration.

This coupling must be removed.

The revised documents expose a second problem. They say a missing/unavailable application-specific Langfuse project causes a `full` exporter to fail closed to `redacted`, while also saying the current `redacted` path is unsafe and prohibited under `passthrough` or `pii_detection: off/regex`. Until telemetry has an independent sanitizer, the only safe fallback in those modes is `off`, not the current reused "sanitized" value.

### Required design

The telemetry exporter must generate a separate policy-controlled representation for every content surface:

```text
raw input ───────────────→ model-input policy ─────→ model
    │
    └────────────────────→ telemetry sanitizer ────→ Langfuse

raw completion ──────────→ client-output policy ───→ client
    │
    └────────────────────→ telemetry sanitizer ────→ Langfuse
```

Do the same for tool arguments and tool results.

Required semantics:

| Content surface | `capture_io: off` | `capture_io: redacted` | `capture_io: full` |
|---|---|---|---|
| Inbound prompt/messages | Omitted | Independently redacted copy | Raw |
| Model completion | Omitted | Independently redacted copy | Raw |
| Tool arguments | Omitted | Independently redacted copy | Raw |
| Tool results | Omitted | Independently redacted copy | Raw |
| Tool errors/exceptions | No content | Independently redacted | Raw only when explicitly permitted |
| Routing/cost/token/latency metadata | Retained | Retained | Retained |

Additional requirements:

- `capture_io: redacted` must work even when `model_input: raw`.
- `capture_io: full` must require the application's own provisioned Langfuse project.
- Preserve the existing compatibility rule that non-`onprem_only + enforced + full` is allowed, while `onprem_only + enforced + full` is rejected. The redesign may add stronger deployment ceilings, but must not accidentally change persisted behavior.
- The shared project must never receive raw I/O.
- Redaction must happen before data is added to spans, attributes, events, logs, queues, or exporter buffers.
- Error strings and provider exceptions must pass through the same telemetry-content policy.
- Telemetry failure may fail closed to `redacted` only when the independent telemetry sanitizer successfully produced a safe representation. Otherwise it must fail closed to `off`. It must never fall back to raw capture or reuse a raw model-input buffer under a redacted label.
- The console must continue to avoid rendering raw prompts.

### `raw_completion` requirement

The documents are ambiguous about whether `raw_completion` is returned in the API response for full-capture telemetry. Correct this contract:

```text
raw_completion is an internal pipeline value.
It must never appear in the public OpenAI-compatible response.
It may be passed directly to an approved tenant-specific exporter only when raw capture is explicitly authorized.
```

If `raw_completion` is currently serialized to the caller, remove it from the public response model. Otherwise, client-output redaction can be bypassed by reading another field.

---

## 9. Explicit pipeline data representations

Do not use one mutable `sanitized_prompt` value for every consumer. Introduce explicit immutable representations or typed wrappers for at least:

```text
raw_input
model_input
tool_input
raw_tool_result
model_tool_result
raw_completion
client_output
telemetry_input
telemetry_output
```

Each transition must identify:

- Source representation
- Destination consumer
- Applied policy
- Detected entity types
- Whether raw data crossed a trust boundary
- Policy version

Raw values must not be included in the decision record. Entity types and counts are sufficient for policy evidence.

Avoid passing raw and redacted strings together in a general-purpose dictionary that can be accidentally serialized. Use types or structures that make the permitted destination explicit.

---

## 10. Placement remains independent

Do not change the existing placement guarantee:

```text
residency: onprem_only
```

must continue to remove cloud providers before routing and scoring.

The following are independent questions:

| Axis | Question |
|---|---|
| Placement | Where may the model or tool execute? |
| Model input | What content may the selected model see? |
| Tool access | What content may each registered tool see? |
| Client output | What content may the calling user receive? |
| Telemetry | What content may observability systems store? |

Never change residency to obtain a protection or telemetry outcome.

Model eligibility also remains two-sided:

```text
model.allowed_app_ids permits application
AND application allowed_models/providers/tiers permits model
AND effective per-request residency permits provider
AND provider/model is healthy
```

Privacy classification may raise effective residency for an individual request, such as an SSN forcing `onprem_only`, but it must never lower the application's declared residency floor. The new data-handling axes must not bypass either side of model scoping or the per-request residency escalation.

---

## 11. Compression and exact-content handling

The current documents correctly note that `passthrough` only disables the PII gate. Headroom may still alter messages by collapsing whitespace, removing duplicate lines, or compressing earlier tool results/history.

Therefore, the phrase **"don't touch my data"** is inaccurate unless compression and every other content transformation are disabled.

Required change:

- Either make the router-only/raw recipe explicitly set `compression_enabled: false`, or
- Rename/describe passthrough as **"PII detection and redaction disabled"**, not **"data untouched"**.

Consider adding an explicit data-integrity control:

```yaml
content_integrity: exact | transformations_allowed
```

When `exact`:

- Disable lossy compression.
- Do not normalize or rewrite message content.
- Preserve tool-call and tool-result structure.
- Record that exact-content mode was applied.

This is separate from whether PII detection runs.

---

## 12. Recommended recipes after redesign

The UI can remain simple by offering recipes built from independent controls.

| Recipe | Model | Trusted tool | Client | Telemetry I/O |
|---|---|---|---|---|
| Protected | Redacted or tokenized | Token-resolved | Redacted | Redacted |
| Protected, no I/O telemetry | Redacted or tokenized | Token-resolved | Redacted | Off |
| Protected serving, raw eval telemetry | Redacted or tokenized | Policy-controlled | Redacted | Full/raw in the application's own project; preserve current non-`onprem_only` compatibility rule |
| Trusted compute, safe output | Raw | Raw for approved tools | Redacted | Redacted or off |
| Authorized disclosure | Tokenized | Token-resolved | Rehydrated after authorization | Redacted or off |
| Router only | Raw | Raw | Raw | Full or off |

The recipes should fill the form without saving, as the current console does. The effective values must be shown before the operator commits the change.

Add warnings that state concrete exposure, for example:

```text
The selected model will receive raw SSNs.
Only approved on-prem tools may resolve protected references.
The client response will be redacted.
Langfuse will receive redacted copies of model and tool I/O.
```

---

## 13. Validation and security ceilings

Keep or add deployment-level controls so a locked-down installation can prohibit risky choices.

Suggested ceilings:

```yaml
policy:
  allow_raw_model_input: false
  allow_raw_tool_arguments: false
  allow_raw_client_output: false
  allow_raw_telemetry: false
  allow_authorized_rehydration: false
```

Validation requirements:

- A malformed ceiling value must close the permission rather than open it.
- `trusted_raw` must be rejected for unknown or untrusted tools.
- `resolve_tokens` must be rejected if no protected-reference broker is configured.
- `authorized_rehydrate` must be rejected if no trusted authorization context exists.
- `capture_io: full` must be rejected without the application's own Langfuse project.
- Preserve current policy compatibility: reject `onprem_only + enforced + full`, allow `onprem_only + passthrough + full`, and allow non-`onprem_only + enforced + full`, subject to the application-specific project requirement and any new deployment-wide raw-telemetry ceiling.
- `stream_unverified` must be rejected or visibly upgraded to `verified_then_stream` whenever `client_output: redact`, `client_output: authorized_rehydrate`, `onprem_only`, or `require_verification` applies.
- `incremental_verify` must never be represented as equivalent to whole-response verification; the response and trace must disclose the weaker guarantee.
- An `onprem_only` policy must still reject every cloud candidate regardless of protection settings.
- Any invalid combination must be rejected atomically with a specific 422 message.
- Partial updates and reset behavior must be evaluated against the complete effective post-write policy.

Raw model input and raw tool access may be allowed under a deliberate contract. They must not implicitly enable raw client output or raw telemetry.

---

## 14. Documentation corrections required

Update all four reviewed source documents, the UI guide, the API reference, the sample-request flows, and the per-app configuration reference after implementation.

### 14.1 Correct the protection explanation

Replace the two-state explanation that treats input and output redaction as inseparable. Explain that `policy_mode` is a preset and show the effective independent controls.

### 14.2 Add the SSN tool-call example

Document all of these explicitly:

- Redaction destroys the lookup value unless the tool has another identifier.
- Generic `[SSN]` placeholders cannot support a lookup.
- Tokenized references can be resolved only by approved tools.
- Tool results have their own model, client, and telemetry exposure policies.

### 14.3 Correct passthrough wording

Do not say `passthrough` means RouteForge does not touch the data while Headroom can still alter it. Say that the PII gate does not alter it, or ensure the recipe disables all transformations.

### 14.4 Correct telemetry descriptions

Clearly distinguish:

- Shared Langfuse project
- Application-specific Langfuse project
- Decision metadata
- Prompt/completion content
- Tool argument/result content
- `capture_io: off`, which suppresses content but retains permitted metrics and decisions

Reconcile the contradictory full-capture guidance. The current precise rule appears to be:

```text
onprem_only + enforced + full          → rejected
onprem_only + passthrough + full       → allowed with own project
non-onprem + enforced + full           → allowed with own project
non-onprem + passthrough + full        → allowed with own project
```

`choosing-app-settings.md` currently says raw capture requires a no-protection posture, which conflicts with the non-on-prem `enforced + full` rule in `faq.md` and `getting-started.md`. Pick one authoritative rule based on code and tests, then use it everywhere.

Also correct the fallback description. If a `full` exporter loses its application-specific project while the gate produced no safe sanitized representation, it cannot safely fall back to the current `redacted` exporter. With the proposed independent telemetry sanitizer it may fall back to a genuinely redacted copy; without one it must fall back to `off`.

If the current UI guide still says Langfuse prompt/answer fields are always null, reconcile it with the reviewed documents' redacted and full modes. State which project and capture mode each claim applies to.

### 14.5 Resolve `pii_detection: regex` contradiction

`getting-started.md` describes `regex` as a detector that recognizes SSNs, cards, emails, and custom patterns and says the gate replaces detected values with placeholders. The same document later rejects `capture_io: redacted` under `regex` on the grounds that the "sanitized" text is raw. Those statements cannot both be generally true. Verify actual behavior and make the following unambiguous:

- What regex mode detects
- Whether it redacts model input
- Whether it redacts client output
- Whether it can produce redacted telemetry
- Whether custom recognizers run

If regex mode really produces sanitized content, the validator should not reject `capture_io: redacted` merely because the detector type is regex.

### 14.6 Clarify detection-off and custom recognizers

State definitively whether `pii_detection: off` disables:

- Built-in Presidio recognizers
- Regex detection
- Application custom recognizers
- Sensitivity escalation
- Egress detection

If some recognizers remain active, do not describe the result as guaranteed raw passthrough.

### 14.7 Rename misleading fields

`pii_entity_denylist` is described as a list of entity types exempted from redaction. Consider renaming it to:

```text
pii_exempt_entity_types
```

If API compatibility requires the old name, retain it as a deprecated alias and document the migration.

### 14.8 Clarify `raw_completion`

State that it is internal-only and never serialized to the client. Document exactly when it can reach the tenant-specific telemetry exporter.

### 14.9 Document tool coverage

State whether every policy applies to:

- Client-supplied tools and schemas
- Model-generated tool arguments
- Replayed assistant tool calls
- Tool-result messages
- Internal agent tools
- Tool errors
- Every multi-step hop

### 14.10 Correct semantic-cache status

Any document claiming semantic cache is wired or shipped must be changed to:

```text
Status: planned/not implemented. No serving request currently performs semantic-cache lookup or storage.
```

Do not describe unimplemented enforcement as a current guarantee.

### 14.11 Document streaming against output policy

For every `stream_mode`, document:

- Whether tokens are released before whole-response verification
- Whether PII can be redacted across chunk boundaries
- Whether `require_verification`, `onprem_only`, or the new client-output mode overrides it
- Whether an incompatible selection is rejected or visibly changed to `verified_then_stream`
- What the trace records when verification is waived or weakened

Do not let a user select `client_output: redact` while `stream_unverified` releases the raw completion.

### 14.12 Document effective placement and model eligibility

Carry the two-sided scoping explanation from `creating-a-sample-application.md` into the policy reference:

- The model's `allowed_app_ids` must permit the application.
- The application's model/provider/tier allowlists must permit the model.
- Effective request residency may be raised by detected entities.
- Neither protection nor telemetry settings may lower the application residency floor.

### 14.13 Clarify deployment defaults versus deployment ceilings

`getting-started.md` says a per-application policy can only narrow global rules, but the same guide permits per-app `passthrough`, `pii_detection: off/regex`, and raw telemetry when the corresponding deployment ceilings allow them. Distinguish these concepts:

- A **default** is inherited when the application leaves a field unset.
- A **ceiling/floor** is an absolute deployment constraint an application cannot weaken.
- An explicitly permitted per-app downgrade may differ from the default without violating a ceiling.

Document the exact precedence order and use the same terminology in configuration, UI help, validation errors, and tests.

---

## 15. Console changes required

Update the Settings page so an operator can understand the effective data path without mentally decoding multiple fields.

Recommended organization:

1. Placement
2. Model input
3. Tool access
4. Client output
5. Telemetry content
6. Delivery/content integrity

The permanent summary should show at least:

```text
Placement: on-prem only
Model input: raw
Tools: trusted raw
Client output: redacted
Telemetry I/O: redacted
Compression: off
```

For every risky selection, display the affected consumer and data explicitly. Avoid vague warnings such as "protection reduced."

The request drill-down should show decisions, entity types, and transformations, but never raw sensitive values unless a separately authorized secure view is intentionally implemented.

Suggested trace fields:

```text
sp.model_input_mode
sp.tool_argument_mode
sp.tool_result_mode
sp.client_output_mode
sp.telemetry_io_mode
sp.content_integrity_mode
sp.app_policy_version
```

---

## 16. Acceptance tests

Implement tests at the policy, pipeline, tool, API, exporter, and UI validation layers.

### 16.1 Core scenario tests

| Test | Configuration | Required assertion |
|---|---|---|
| Raw end-to-end | Raw model/raw tool/raw client/full telemetry | Exact SSN reaches all explicitly authorized surfaces only |
| Raw model, safe client | Raw model/trusted raw tool/redacted client/redacted telemetry | Model and tool see SSN; client and Langfuse do not |
| Raw model, no I/O telemetry | Raw model/trusted raw tool/redacted client/off telemetry | No prompt, completion, argument, or result content exported |
| Protected model, functional tool | Tokenized model/resolved tool/redacted client | Model never sees SSN; approved tool receives it |
| Fully protected | Redacted model/redacted tool/redacted client/off telemetry | Raw SSN reaches none of those surfaces |
| Authorized disclosure | Tokenized model/resolved tool/authorized rehydrate | Only an authorized caller receives restored fields |
| Existing non-on-prem protected/full capture | `private_cloud_ok` or `any` + enforced model/client protection + full telemetry | Model/client receive protected views; raw ingress/completion go only to the application's own project |
| Existing on-prem protected/full capture rejection | `onprem_only + enforced + full` | Atomic 422; no policy change is persisted |

### 16.2 Tool security tests

- A token resolves for the correct tenant, application, request, and allowed tool.
- The same token fails for another tenant.
- The same token fails for another request.
- The same token fails for an unapproved tool.
- An expired token fails closed.
- A malformed token fails closed without leaking mapping details.
- Tool arguments are redacted in telemetry under `capture_io: redacted`.
- Tool results and tool errors are redacted in telemetry.
- Multi-step agent hops apply the same tool and telemetry policy every time.

### 16.3 Output tests

- With raw model input and `client_output: redact`, the model sees `123-45-6789` but the public response does not contain it.
- The public response never contains `raw_completion`.
- Egress redaction runs even when input detection/redaction is disabled.
- Verification failure returns the documented error or escalation behavior and never falls back to raw content.
- Streaming cannot emit raw tokens before required egress verification/redaction.
- `stream_unverified` is rejected or visibly upgraded when `client_output: redact`, `client_output: authorized_rehydrate`, `onprem_only`, or `require_verification` applies.
- `verified_then_stream` releases only the completed post-verification client representation.
- `incremental_verify` redacts an entity split across multiple chunks and records that whole-response verification was not performed.

### 16.4 Telemetry tests

For `capture_io: redacted`, assert the raw SSN is absent from:

- Trace input
- Trace output
- Span attributes
- Span events
- Model observation fields
- Tool-call arguments
- Tool results
- Exception messages
- Application logs used by the exporter

For `capture_io: off`, assert all I/O content fields are null/absent while permitted decision metadata remains available.

For `capture_io: full`, assert raw I/O goes only to the application's own project and never the shared project. Cover both `passthrough` and the currently supported non-`onprem_only + enforced` combination.

Simulate the application-specific Langfuse project becoming unavailable. Assert that the exporter emits a genuinely sanitized representation only when an independent sanitizer succeeds; otherwise it emits no I/O content. It must never relabel raw content as redacted.

### 16.5 Residency tests

- `onprem_only` removes all cloud candidates before router scoring in every protection combination.
- Enabling raw model input does not widen `allowed_providers`.
- Enabling raw tool access does not widen model placement.
- Changing telemetry capture does not change routing eligibility.
- Privacy classification may raise effective residency but never lower the application's residency floor.
- A model must pass both its own `allowed_app_ids` scope and the application's tier/provider/model filters.

### 16.6 Backward-compatibility tests

- Existing `enforced` applications retain current behavior.
- Existing `passthrough` applications retain current behavior.
- Existing partial PUT and `reset` semantics remain atomic and predictable.
- Existing non-`onprem_only + enforced + full` applications retain their documented behavior.
- Existing `onprem_only + enforced + full` writes remain rejected unless the product owner deliberately changes that rule and migrates the contract.
- Effective policy and `app_policy_version` change whenever any new axis changes.
- The UI shows inherited and effective values correctly.

---

## 17. Implementation workflow for the coding model

1. Trace the current executable paths for request detection, input redaction, model invocation, tool invocation, tool-result handling, verification, all four streaming modes, response serialization, and Langfuse export.
2. Produce a short code-evidence map with file paths, classes/functions, and tests.
3. Identify every place where `policy_mode`, `pii_detection`, or `sanitized_prompt` implicitly controls multiple consumers.
4. Introduce explicit effective policy fields with backward-compatible preset resolution.
5. Separate the pipeline's raw, model, tool, client, and telemetry representations.
6. Make egress protection independent from model-input protection.
7. Make telemetry redaction independent from model-input sanitization.
8. Add tool argument/result policies. Implement protected references only if included in the agreed implementation scope; otherwise fail closed for unsupported combinations and mark token resolution as a clearly tracked follow-up.
9. Ensure raw completion is internal-only.
10. Update server-side validation, streaming compatibility checks, configuration loading, environment overrides, persistence, policy versioning, API schemas, manual database migration notes, and UI controls.
11. Update all relevant documentation listed in this handoff.
12. Add and run the acceptance tests above.
13. Report any scenario that remains unsupported instead of documenting it as implemented.

Do not declare the redesign complete based only on unit tests for detector functions. Validate complete request paths, including client-driven tool calling, the internal multi-step agent path, true streaming, verified-then-stream delivery, and telemetry-export failure.

---

## 18. Required implementation report

When the work is complete, return a report containing:

### Implemented

- New or changed policy fields
- Exact semantics of every field
- Backward-compatibility behavior
- Pipeline data representations
- Tool behavior
- Output behavior
- Telemetry behavior
- UI behavior

### Security invariants

- What cannot reach cloud under `onprem_only`
- What cannot reach an untrusted tool
- What cannot reach shared telemetry
- What cannot appear in the public API response
- What happens on detector, authorization, token-resolution, or exporter failure

### Evidence

- Changed file paths
- Important functions/classes
- Database migration steps
- Tests added
- Test command and result

### Remaining gaps

- Any unsupported matrix combination
- Any tool path not covered
- Any documentation claim not proven by code
- Any deferred protected-reference or authorization capability

---

## 19. Definition of done

This work is complete only when RouteForge can demonstrably support the following without policy ambiguity:

```text
Raw request containing Jane Smith and SSN 123-45-6789
  → trusted model receives raw request
  → approved tool receives real SSN and finds the ticket
  → raw model completion is retained only inside the protected pipeline
  → client receives a redacted answer
  → Langfuse receives a redacted copy or no I/O content
  → streaming cannot release the raw answer before the selected output protection runs
  → cloud providers remain excluded when residency is on-prem only
  → public response, shared telemetry, console, logs, and error traces contain no raw SSN
```

The inverse strict scenario must also be explicit:

```text
Model is not allowed to see the SSN
  → RouteForge tokenizes it rather than replacing it with an unusable generic marker
  → only an approved trusted tool can resolve it
  → tool results are protected before returning to the model
  → client and telemetry receive the policy-selected representations
```

If protected reference resolution is deferred, RouteForge must reject the strict SSN-dependent tool scenario clearly. It must not pretend that `[SSN]` is sufficient for a functional lookup.

The redesign must also preserve and test the revised current contract that a non-`onprem_only` application may combine `policy_mode: enforced` with `capture_io: full` in its own Langfuse project, while `onprem_only + enforced + full` remains invalid. If that contract is intentionally changed, update every guide, validator, migration note, and conformance test together.
