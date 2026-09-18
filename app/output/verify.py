"""Step 9 — output controls and verification.

Egress is independent of ingress (design P1a §2.5). What the model was SENT is
`model_input`'s decision; what the CLIENT gets back is `client_output`'s, and
this module reads only the second one. The configuration the whole feature
exists for — `model_input: raw` + `client_output: redact` — is exactly the case
where the two disagree: a trusted model inside the customer's boundary is handed
the real value, and the completion is still scrubbed before it reaches the
client.

The decision lives HERE rather than at the call site, so no caller can select a
detector that quietly disagrees with the app's policy. Callers pass the axis
value; `verify` picks the detector.
"""
from __future__ import annotations

from app.models import ExecutionPolicyContext, VerificationRecord
from app.policy.detectors import Detector, NullDetector

#: Stateless, so one shared instance is safe (it holds nothing and returns no
#: entities). Used whenever `client_output` is `raw`.
_NO_REDACTION = NullDetector()


def egress_detector(detector: Detector, client_output: str) -> Detector:
    """The detector egress redaction runs with: the request's own detector when
    `client_output: redact`, and the NullDetector when the client has declared
    `raw`.

    Exported because `verify()` is not the only egress surface — a completion
    can also carry PII in `tool_calls.function.arguments`, and the streaming
    hold-back redactor is a third. All three must make the SAME choice from the
    SAME axis, which is why the rule is written once, here, instead of at each
    of them.

    Anything other than the literal `"redact"` means no redaction: the axis is a
    closed two-value vocabulary that `compile_policy` has already failed closed
    on (an unparseable value resolves to `redact`), so a value that reaches here
    is either `redact` or a deliberate `raw`.
    """
    return detector if client_output == "redact" else _NO_REDACTION


def verify(detector: Detector, ctx: ExecutionPolicyContext, completion_text: str,
           *, client_output: str) -> tuple[str, VerificationRecord]:
    """Leakage-check the completion and return `(client_text, record)`.

    `client_output` is keyword-only and has no default on purpose: every caller
    must state which egress posture it is verifying under. A default would be a
    silent answer to the one question this function exists to ask.

    Fail-closed behaviour is identical on both sides of the axis. The
    `UNVERIFIABLE` marker check reads the completion TEXT, never the detector,
    so it fires for a `raw`-output app exactly as it does for a `redact` one —
    and a failed verification returns `""`, never the content. Redaction is the
    only thing `client_output: raw` switches off.
    """
    det = egress_detector(detector, client_output)
    entities = det.analyze(completion_text)
    etypes = tuple(sorted({e.type for e in entities}))
    redacted, _map = det.anonymize(completion_text, entities)

    if not ctx.require_verification:
        return redacted, VerificationRecord(
            required=False, performed=False, passed=True,
            entities_found=etypes, reason="not required")

    # required path
    unverifiable = "UNVERIFIABLE" in completion_text
    passed = not unverifiable
    return (redacted if passed else ""), VerificationRecord(
        required=True, performed=True, passed=passed,
        entities_found=etypes,
        reason="verified" if passed else "failed closed: unverifiable")
