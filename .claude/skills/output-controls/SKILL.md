---
name: output-controls
description: Use when implementing step 9 of the Serving Plane - output controls and verification. Leakage checks on completions, the require_verification flow, escalation on verification failure, sampling rate, streaming policy, and the client_output egress axis (there is no detokenization - see the skill). Trigger on mentions of output verification, leakage check, require_verification, egress redaction, client_output, structured-output validation, or streaming.
---

# Output Controls & Verification (Step 9)

The last gate before a response leaves. Step 2 cleared the *input*; step 9 clears the
*output*. Completions and tool results carry PII the input gate never saw (a model
recalls a record; a tool returns a customer row). Skipping this makes redaction-on-
egress a claim instead of a fact. `config/policy.yaml` sets `require_verification`
per sensitivity rule — that field is consumed **here** and nowhere else.

## What a leakage check is

Re-run the same Presidio pass from the gate, this time on the **completion** (and any
tool output that reached the model). Same recognizers, same enum. If entities are
found in the output:

- redact them before the response returns (the egress redaction hard rule), and
- if the request's rule set `require_verification: true`, treat a find as a
  verification failure (below), not just a silent scrub.

This is also where structured-output validation runs (schema/JSON conformance) when
the caller requested it.

## The `require_verification` flow

`require_verification: true` comes from the policy rule that matched at the gate
(restricted/high-risk requests set it — see `config/policy.yaml`). When set:

1. Run the leakage check **and** any output-schema validation on the complete
   response.
2. On pass → return.
3. On fail → **escalate one tier and re-run** (same mechanism as the router's
   confidence escalation), capped by `max_escalations`. A high-risk `onprem_only`
   request escalates only *within the on-prem tiers* — verification never lifts a
   request across the residency boundary (residency beats everything).
4. On fail after the cap → **fail closed**: return a safe error, never the unverified
   completion. This mirrors the gate's fail-closed rule.

Every verification attempt emits a `DecisionRecord` (`step: "verify"`) with the
outcome and reason, so the UI shows why a response was held or escalated.

## Sampling

Verifying every token of every response is expensive. By default, verify
**100%** of requests whose rule set `require_verification: true`, and sample the rest
at a configurable rate (default e.g. 0.1) so the honesty story holds without paying
full cost on trivially-public traffic. Record whether a given response was verified
or sampled-out in its DecisionRecord — don't let "not sampled" read as "verified
clean."

## Egress redaction is `client_output`'s call — not the ingress detector's

Whether the completion is redacted on the way out is decided by **one axis**,
`client_output ∈ {raw, redact}`, and by nothing else. It is independent of what the model
was shown (`model_input`): an app running `model_input: raw` + `client_output: redact`
sends the model real values and still returns a redacted answer.

`app/output/verify.py::egress_detector(detector, client_output)` is the single place that
choice is made, because `verify()` is not the only egress surface — a completion can carry
PII in `tool_calls.function.arguments`, and the streaming hold-back redactor is a third.
All three read the same function so they cannot drift.

Fail-closed behaviour is **identical on both sides of the axis**: the `UNVERIFIABLE`
marker check reads the completion *text*, never the detector, so it fires for a `raw`-output
app exactly as for a `redact` one, and a failed verification returns `""`. Redaction is the
only thing `client_output: raw` switches off. `verify()` takes `client_output` as a
keyword-only argument with **no default**, so every caller must state which posture it is
verifying under.

## There is no detokenization — do not document one

Earlier versions of this skill described a "detokenize on egress" mode. **It does not
exist.** The redaction map is built during `anonymize()` and discarded; nothing anywhere
re-inserts an original value, there is no broker, and there is no config switch that turns
one on. `client_output: "authorized_rehydrate"` and `model_input: "tokenize"` are named in
the P1a design and are **rejected with a 422** (`AXIS_VALUE_NOT_IMPLEMENTED`) rather than
faked. Placeholders are the deliverable; an app that needs the real value needs
`model_input: "raw"`.

## Streaming — rejected by default, opt-in per app, floored absolutely

Step 9 checks the **complete** response, which token streaming cannot satisfy without a
partial-leak window. So inbound `stream: true` is still **rejected by default**
(`server.reject_stream_requests: true`) and for any app with no `stream_mode` set. An app
may opt into one of `verified_then_stream` (full verified pipeline, then chunk the
finished answer — guarantee intact, no TTFT win), `stream_unverified` (real tokens,
verification explicitly waived and recorded), or `incremental_verify` (real tokens through
a hold-back redactor that never releases raw entity text across a chunk boundary, but
cannot enforce a whole-response verdict).

**The floor is absolute and independent of the app's choice.** `pipeline.run_stream` raises
`StreamFallback` — eagerly, before any token is emitted — for exactly three reasons, and
`app/api/routes.py` serves those as `verified_then_stream`:

1. `ctx.onprem_only` or `ctx.require_verification` for *this* request (CLAUDE.md rule 8);
2. a client-driven tool conversation (needs the structured path — not a privacy matter);
3. `stream_mode: stream_unverified` **with an explicitly set** `client_output: redact`.

Reason 3 is scoped to an *explicit* `client_output` on purpose: `redact` is also what the
`enforced` preset resolves to, so keying on the effective value would floor every
`stream_unverified` app in existence. **The consequence to state out loud: an `enforced`
app that opts into `stream_unverified` streams unredacted tokens.** `client_output: redact`
is a guarantee on every path except that opt-in.

The decision stream the UI consumes over SSE is a separate channel (DecisionRecords, not
model tokens) and is unaffected by any of this.

## What the DecisionRecord must carry

```python
class VerificationRecord(BaseModel):
    step: str = "verify"
    required: bool                 # from require_verification
    performed: bool                # required OR hit the sampling rate
    passed: bool
    entities_found: tuple[str, ...]  # TYPES only, never values
    escalated: bool
    escalations_used: int
    reason: str                    # human-readable — this is the demo
    latency_ms: float
```

## Tests

- `require_verification: true` + clean output → `passed: true`, returned
- `require_verification: true` + PII in completion → redacted, escalated once, then
  passes or fails closed at the cap
- High-risk `onprem_only` + verification fail → escalates within on-prem, **never** cloud
- Verification fail past cap → safe error, unverified completion **never** returned
- `stream: true` inbound with no `stream_mode` → rejected with a clear error, no model call
- `stream_unverified` + an `onprem_only` request → `StreamFallback`, served verified
- `stream_unverified` + explicit `client_output: redact` → `StreamFallback`
- `client_output: raw` → completion returned verbatim, `entities_found` empty, and the
  `UNVERIFIABLE` fail-closed path still fires
- Sampled-out public request → DecisionRecord shows `performed: false`, not a false clean
