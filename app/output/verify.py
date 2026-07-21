from __future__ import annotations

from app.models import ExecutionPolicyContext, VerificationRecord
from app.policy.detectors import Detector


def verify(detector: Detector, ctx: ExecutionPolicyContext, completion_text: str
           ) -> tuple[str, VerificationRecord]:
    entities = detector.analyze(completion_text)
    etypes = tuple(sorted({e.type for e in entities}))
    redacted, _map = detector.anonymize(completion_text, entities)

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
