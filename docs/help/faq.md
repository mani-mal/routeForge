# routeForge FAQ (operator & customer-facing)

Questions that come up repeatedly while configuring per-application policy, residency,
and telemetry. For the full per-app config reference see
[`../per-app-config.md`](../per-app-config.md).

---

## Privacy, residency & telemetry

### The gate is mangling my app's data — it redacted an engineering label / part number / field name that isn't PII. How do I turn redaction off *without* giving up `onprem_only`?

**Set `pii_detection: "off"` and `capture_io: "off"`, and leave `residency` at
`onprem_only`.** This is allowed, and it is the right configuration for an app whose
prompts are the customer's own documents.

```json
{ "residency": "onprem_only", "pii_detection": "off", "capture_io": "off" }
```

Detection and residency are independent axes. The `onprem_only` floor is computed from the
app's residency in `app/policy/context.py` and never consults `pii_detection`, so turning
detection off cannot lift the app to cloud — the model stays inside the customer's
boundary and no external party ever sees the prompt. On-prem is the *safest* place to run
the gate open, not the most dangerous.

`capture_io: "off"` is required, and it is what makes the combination airtight: with no
telemetry I/O captured there is no egress path for the unsanitized prompt. The two
telemetry modes stay blocked for this app — `redacted` because that exporter path reuses
the gate's sanitized prompt (raw when detection is off), and `full` because raw capture is
never available to an `onprem_only` app (rule 8). Both are still HTTP 422.

> **Do not reach for `residency: "any"` + an `allowed_providers` pin to get this.** That
> workaround is for raw *Langfuse capture* (next question), not for unredacted prompts,
> and it is strictly weaker: it swaps a hard residency floor for a mutable allowlist and
> drops `ctx.onprem_only`, which the step-9 streaming floor and the `capture_io=full`
> clamp both key on.

**Prefer a narrower lever when you can.** `pii_detection: off` blinds the gate to
*everything*, including a real SSN. If only a few false positives are the problem, use
[`pii_entity_denylist` / `pii_exempt_patterns`](../per-app-config.md) instead — they also
work while `onprem_only`. Reach for `off` when the app's whole input is customer-owned
content whose every field is load-bearing output, so there is nothing to protect and no
enumerable set of shapes to exempt.

### My customer is on-prem, has no PII, and wants to store *everything* (raw prompts + completions) in Langfuse. Can an `onprem_only` app do that?

**No — not while the app is classed `onprem_only`.** An `onprem_only` app is capped at
`capture_io` of `off` or `redacted`:

- `capture_io=full` is rejected for `onprem_only` **whether PII detection is on or off.**
- `capture_io=redacted` is rejected when detection is `off`/`regex` (the exporter's
  "redacted" path reuses the gate's sanitized prompt, which is raw when detection is off).

Note this question is specifically about **raw telemetry**. If all you need is unredacted
prompts *reaching the model*, you do not have to move off `onprem_only` at all — see the
previous question.

This is deliberate, not a bug. `onprem_only` is the maximally-sensitive class (the
hospital persona), and its *telemetry* ceiling is intentionally non-configurable so a
good-faith "we don't need redaction here" can't silently become plaintext PII sitting in a
trace console. That is the risk redaction defends even on-prem: raw PII in Langfuse is
readable by anyone with console access to that project — staying on your infrastructure
does not remove that exposure surface. Nothing above constrains what reaches the *model*,
which is why detection can be turned off for an `onprem_only` app while capture cannot.

**Best available for a true `onprem_only` app:** `capture_io=redacted` with detection on.
You still get full decision traces, routing, candidate list, cost, latency, and redacted
I/O — everything except the raw entity text.

### Can I just set `residency=private_cloud_ok` (or `any`) so those customers can log everything raw?

**Yes, the write succeeds — but residency is not only a telemetry gate, it is the
provider filter, so this has a side effect you must handle deliberately.**

The valid residency values are `any`, `private_cloud_ok`, and `onprem_only`. Any value
other than `onprem_only` allows the enabled cloud providers into the routing pool. So
moving off `onprem_only` does two things at once:

1. ✅ Unlocks `capture_io=full`. (`pii_detection=off` never required this flip — it is
   available while `onprem_only`, with `capture_io=off`.)
2. ⚠️ **Removes the on-prem lock.** If any cloud provider is enabled
   (`azure`/`bedrock` in `config/serving.yaml`), the router is now free to send that
   customer's traffic to cloud — off their infrastructure. That contradicts "on-prem
   only."

**Whether this is safe depends on the deployment:**

- **No cloud provider enabled** (typical air-gapped install: `azure.enabled=false`,
  `bedrock.enabled=false`): the routing pool resolves to on-prem only anyway, so routing
  stays on-prem even with `residency: any`. Your plan works and nothing egresses.
- **A cloud provider enabled:** `residency: any` means those requests *can and will* go
  to cloud when the router scores it cheaper. Don't do this for an on-prem customer
  without the pin below.

### Recommended config: raw Langfuse capture *and* routing pinned on-prem

Add the per-app `allowed_providers` pin so routing can never pick cloud regardless of
residency:

| Field | Value | Effect |
|---|---|---|
| `residency` | `private_cloud_ok` (or `any`) | Clears the PII-downgrade / `full` restrictions |
| `allowed_providers` | `{"onprem"}` | Hard-pins routing to on-prem regardless of residency |
| `pii_detection` | `off` | No redaction |
| `capture_io` | `full` | Raw I/O to the app's **own** Langfuse project |
| deployment `allow_app_pii_downgrade` (`serving.yaml`) | `true` | Ceiling must permit downgrades |
| app's own Langfuse project | provisioned | `capture_io=full` is rejected (422) without it |

Two caveats to make consciously, per customer:

1. **`capture_io=full` still requires the app's own provisioned Langfuse project** — raw
   I/O never lands in the shared/default project even after this change. Provision one per
   app (see [`../langfuse-per-app-projects.md`](../langfuse-per-app-projects.md)).
2. **You keep the on-prem *behavior* but lose the `onprem_only` *guarantee*.** These are
   now cloud-eligible apps you've manually pinned, not privacy-first `onprem_only` apps in
   routeForge's model. If someone later clears `allowed_providers`, cloud routing silently
   becomes possible. Decide whether that residual risk is acceptable for each customer.

**Rule of thumb:** the residency label should reflect the actual sensitivity of the data.
If the data genuinely has no PII and raw eval capture is wanted, a non-`onprem_only` app
with `allowed_providers={"onprem"}` is the honest configuration. If the data *is*
sensitive, keep `onprem_only` and accept `capture_io=redacted` as the ceiling.

---

## Response behavior & output controls

### Does routeForge return the LLM's response to the user unchanged — just wrapping it with a trace?

**Usually yes on the happy path, but *not* as an unconditional guarantee.** routeForge is
OpenAI-compatible and, in the common case, `choices[0].message.content` is the model's
answer verbatim with routeForge fields (`trace`, `cost`, `policy`, …) added *around* it.
But **step 9 (Output Controls & Verification) can alter or block the completion** before
you see it — what the user receives is the *post-verification* text, and the raw model
output is preserved separately in `raw_completion` for telemetry, never substituted into
`choices[0].message.content`.

Three ways the completion can differ from the raw model output:

1. **Egress PII redaction always runs.** If the model emits PII the detector recognizes
   (an SSN, an email the input never contained), those entity **values are scrubbed** from
   the answer — because model completions can leak PII the input gate never saw, so egress
   is redacted by default.
2. **`require_verification` fails closed.** If policy set `require_verification` and the
   completion is "unverifiable," the request is **rejected** — the caller gets an error,
   not the model's text.
3. **Escalation** can re-serve the answer from a different/stronger model (recorded in the
   trace as `escalated`), so the returned text may not come from the first model tried.

**When *is* it returned verbatim?** Redaction depends on the active detector, so:

| App config | Is `content` the raw model output? |
|---|---|
| `pii_detection=off` | **Yes** — nothing is scrubbed. |
| detection on, completion has no detected entities, no `require_verification` | **Yes, in practice** — redacted text == original. |
| detection on, completion **contains** detected PII | **No** — those values are redacted on egress. |
| `require_verification` set + completion unverifiable | **No** — request is rejected. |

So the precise statement for customers: *routeForge adds the trace and never edits the
answer for style or content — but it does enforce output controls, which can redact PII
the model emitted or block an unverifiable response.* The `VerificationRecord` in the
`trace` (`step:"verify"`, fields `performed / passed / entities_found / escalated`) tells
you exactly what step 9 did on any given request. (Note: `raw_completion` and
`sanitized_prompt` are returned for full-capture telemetry; whether they reach Langfuse
still obeys the app's `capture_io` — see the residency/PII entries above.)
