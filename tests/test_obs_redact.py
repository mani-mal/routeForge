from app.obs.redact import redact, redact_value, redacted_copy
from app.policy.detectors import RegexDetector
from app.policy.effective import EffectiveDetector
from app.policy.recognizers import CustomRecognizer


def test_redacts_ssn_email_phone():
    out = redact("SSN 482-73-1946, mail a@b.com, call 415-555-0100")
    assert "482-73-1946" not in out
    assert "a@b.com" not in out
    assert "415-555-0100" not in out
    assert "<US_SSN>" in out or "<SSN>" in out


def test_passes_through_clean_text():
    assert redact("just a normal sentence") == "just a normal sentence"


def test_redact_value_non_str_passthrough():
    assert redact_value(42) == 42
    assert redact_value(None) is None
    assert redact_value(1.5) == 1.5


# --------------------------------------------------------------------------- #
# `redacted_copy` — the one rule both I/O surfaces share (design P1a §2.6)
# --------------------------------------------------------------------------- #
_RAW = "patient 482-73-1946 has MRN-1234567"


def _gate_style_redaction(detector, text):
    """What the gate produces for `Representations.text_redacted`: this
    detector's own analyze+anonymize over the raw text."""
    out, _ = detector.anonymize(text, detector.analyze(text))
    return out


def test_prepared_is_byte_identical_to_running_the_detector():
    """The `prepared=` fast path exists to skip a second (expensive, on
    Presidio) detector pass over a string the gate already scanned. It is only
    safe while it is indistinguishable from doing the work -- so pin that, or
    the optimisation is free to drift into a different answer than the slow
    path and nothing would notice."""
    det = RegexDetector()
    slow = redacted_copy(_RAW, det)
    fast = redacted_copy(_RAW, det, prepared=_gate_style_redaction(det, _RAW))
    assert fast == slow
    assert "482-73-1946" not in fast


def test_prepared_still_runs_the_custom_recognizer_egress_pass():
    """The non-negotiable half: `prepared` skips the DETECTOR pass, never the
    `redact()` egress pass. A custom recognizer an app's denylist or score
    floor exempted at intake is applied here and nowhere else -- dropping it
    would make telemetry weaker than the detector pass alone.

    The gate here runs a BARE RegexDetector (no MRN recognizer), so its
    redaction leaves the MRN untouched; only the egress pass can mask it.
    """
    det = RegexDetector()
    mrn = CustomRecognizer(name="MRN", pattern=r"\bMRN-\d{7}\b", score=0.9)
    prepared = _gate_style_redaction(det, _RAW)
    assert "MRN-1234567" in prepared, "premise: the gate's copy still has it"

    out = redacted_copy(_RAW, det, [mrn], prepared=prepared)
    assert "MRN-1234567" not in out
    assert "<MRN>" in out
    assert "482-73-1946" not in out


def test_no_detector_leaves_the_regex_net_as_the_only_pass():
    """A None detector means no redacted copy exists -- callers fall closed
    rather than laundering raw text through this. It is still not a passthrough
    if one does: the regex net runs regardless."""
    det = EffectiveDetector(RegexDetector(), [])
    assert "482-73-1946" not in redacted_copy(_RAW, None)
    assert redacted_copy("", det) == ""
