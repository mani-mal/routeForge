"""Egress PII redaction for telemetry (CLAUDE.md rule 2).

Reuses the RegexDetector so exported traces are redacted the same way intake is.
Never reads Entity.value into telemetry — it maps detected spans to <TYPE> tags.

An app's CUSTOM recognizers (Task 7's AppPrivacyProfile.recognizers) are NOT
visible to the base RegexDetector -- a custom entity value (e.g. a hospital's
MRN) would otherwise survive egress redaction untouched. Callers with an
app's recognizers in scope must pass them through `recognizers=` so this path
layers them on via EffectiveDetector, the same overlap-safe dedup used at
intake (Task 4/7)."""
from __future__ import annotations

from app.policy.detectors import RegexDetector
from app.policy.effective import EffectiveDetector
from app.policy.recognizers import CustomRecognizer

_detector = RegexDetector()


def redact(text: str, recognizers: list[CustomRecognizer] | None = None) -> str:
    if not text:
        return text
    if recognizers:
        entities = EffectiveDetector(_detector, list(recognizers)).analyze(text)
    else:
        entities = _detector.analyze(text)
    if not entities:
        return text
    # Replace right-to-left so earlier offsets stay valid.
    out = text
    for e in sorted(entities, key=lambda x: x.start, reverse=True):
        out = out[: e.start] + f"<{e.type}>" + out[e.end :]
    return out


def redact_value(v):
    return redact(v) if isinstance(v, str) else v
