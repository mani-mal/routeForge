---
name: output-controls
description: Use when implementing step 9 of the Serving Plane - output controls and verification. Leakage checks on completions, the require_verification flow, escalation on verification failure, sampling rate, streaming policy, and detokenization ordering. Trigger on mentions of output verification, leakage check, require_verification, egress redaction, structured-output validation, or streaming.
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

Verifying every token of every response is expensive. For the prototype, verify
**100%** of requests whose rule set `require_verification: true`, and sample the rest
at a configurable rate (default e.g. 0.1) so the honesty story holds without paying
full cost on trivially-public traffic. Record whether a given response was verified
or sampled-out in its DecisionRecord — don't let "not sampled" read as "verified
clean."

## Detokenization ordering

If the deployment detokenizes (`<SSN_1>` → real value; see `privacy-gate` skill), the
order is fixed and non-negotiable:

```
generate → step 9 leakage check + validation → detokenize (inside the gate) → return
```

Detokenize **after** verification, **inside the gate's trust boundary**, never before
and never outside it. In demo mode (default) there is no detokenization at all — the
placeholder is the deliverable.

## Streaming — rejected in the demo

Step 9 checks the **complete** response, which is incompatible with token streaming:
buffering defeats streaming, and incremental scanning leaves a partial-leak window.
For the demo, reject inbound `stream: true` with a clear error
(`server.reject_stream_requests: true`). Note this is a separate channel from the
decision stream the UI consumes over SSE (that one carries DecisionRecords, not model
tokens). Production streaming with incremental verification is an explicit open
question, not something to improvise here.

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
- `stream: true` inbound → rejected with a clear error, no model call
- Sampled-out public request → DecisionRecord shows `performed: false`, not a false clean
- Detokenization (when enabled) happens after the leakage check, inside the gate
