"""Egress PII redaction for telemetry (CLAUDE.md rule 2).

Reuses the RegexDetector so exported traces are redacted the same way intake is.
Never reads Entity.value into telemetry — it maps detected spans to <TYPE> tags.
"""
from __future__ import annotations

from app.policy.detectors import RegexDetector

_detector = RegexDetector()


def redact(text: str) -> str:
    if not text:
        return text
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
