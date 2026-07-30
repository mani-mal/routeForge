"""Task 8: egress telemetry redaction must cover per-app CUSTOM entity types,
not just the built-in RegexDetector patterns (CLAUDE.md rule 2: redact on
egress). app/obs/redact.py's `_detector` is a module-level, app-agnostic
RegexDetector -- a custom recognizer's value (e.g. a hospital's MRN) is
invisible to it unless the caller threads the app's recognizers through."""
from app.obs.redact import redact
from app.policy.recognizers import CustomRecognizer


def _mrn_recognizer():
    return CustomRecognizer("MRN", r"\bMRN-\d{7}\b", 0.9)


def test_custom_entity_redacted_when_recognizers_supplied():
    out = redact("patient MRN-1234567 discharged", recognizers=[_mrn_recognizer()])
    assert "MRN-1234567" not in out
    assert "<MRN>" in out


def test_custom_entity_survives_without_recognizers_arg():
    # Proves the fix is load-bearing: with no recognizers passed, the custom
    # value is NOT redacted (this is the gap the fix closes when callers
    # thread recognizers through).
    out = redact("patient MRN-1234567 discharged")
    assert "MRN-1234567" in out


def test_builtin_still_redacted_with_no_recognizers():
    out = redact("ssn 482-73-1946")
    assert "482-73-1946" not in out
    assert "<US_SSN>" in out


def test_both_builtin_and_custom_redacted_together():
    out = redact(
        "ssn 482-73-1946 mrn MRN-1234567",
        recognizers=[_mrn_recognizer()],
    )
    assert "482-73-1946" not in out
    assert "MRN-1234567" not in out
    assert "<US_SSN>" in out
    assert "<MRN>" in out
