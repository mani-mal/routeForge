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


def redact_tree(obj, recognizers: list[CustomRecognizer] | None = None):
    """Deep copy of a JSON-shaped structure with every str leaf AND every str
    dict key passed through `redact()`.

    Redacting the serialised JSON string instead lets the regex detector read
    the fractional digits of an unrounded float as a card/bank number (B74).
    Numbers, bools and None pass through unchanged; no key is exempt; tuples
    become lists."""
    if isinstance(obj, str):
        return redact(obj, recognizers)
    if isinstance(obj, dict):
        return {(redact(k, recognizers) if isinstance(k, str) else k):
                redact_tree(v, recognizers) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_tree(v, recognizers) for v in obj]
    return obj

def redacted_copy(text: str, detector, recognizers=None, *,
                  prepared: str | None = None) -> str:
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

    `prepared` is `detector`'s own redaction of **this same `text`**, when the
    caller already has one, and skips the detector pass. The gate makes exactly
    that for the request prompt (`Representations.text_redacted`), and on a
    Presidio deployment repeating it here is a second spaCy NER pass over the
    whole prompt for no new information. This is NOT the reuse CE-1 was about:
    that was reading the MODEL-FACING view, which under `model_input: raw` is
    the raw text and under `passthrough` is unredacted. This is a redaction
    made with this very detector, over this very string.

    The pairing has to hold, and nothing here can check it — so both values
    must come from one `Representations`, which is frozen and built in one
    place. Never assemble a `(text, prepared)` pair from two sources.

    The `redact()` regex pass ALWAYS runs, `prepared` or not: it applies an
    app's custom `recognizers` (which a denylist or score floor may have
    exempted at intake) and the base regex net a `pii_detection: regex` app
    relies on. Dropping it would make telemetry weaker than the detector pass
    alone, which is the opposite of the point.
    """
    if not text:
        return text
    if prepared is not None:
        out = prepared
    elif detector is not None:
        out, _ = detector.anonymize(text, detector.analyze(text))
    else:
        out = text
    return redact(out, recognizers=recognizers)
