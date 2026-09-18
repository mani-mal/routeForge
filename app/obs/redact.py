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


def redacted_copy(text: str, detector, recognizers=None) -> str:
    """A redacted copy of a RAW value, built here and not borrowed (design
    P1a §2.6).

    The one rule shared by every surface that shows a request's content
    without being allowed to show its PII: the Langfuse trace
    (`app/obs/export.py::_set_io`) and the console's `display` block
    (`app/api/routes.py::_finalize`). It lives in one function on purpose --
    two copies of a privacy rule drift, and the bug this fixes (CE-1) was a
    surface reusing a view that had been built for a DIFFERENT consumer under
    a different rule.

    `detector` is the request's own detector -- the same one the gate ran, so
    the placeholder scheme (`<US_SSN_1>`) matches -- or None when nothing
    scanned this request. **A None detector means no redacted copy exists**;
    callers must not call this to launder raw text, they must fall closed to
    showing nothing. It is still safe if they do (the regex pass below runs
    regardless), but "safe apart from what a regex happens to catch" is
    precisely the guarantee CE-1 shipped and this design refuses.

    The `redact()` regex pass afterwards is defence in depth, and the only
    thing that applies an app's custom `recognizers` when the detector itself
    doesn't carry them.
    """
    if not text:
        return text
    out = text
    if detector is not None:
        out, _ = detector.anonymize(out, detector.analyze(out))
    return redact(out, recognizers=recognizers)
