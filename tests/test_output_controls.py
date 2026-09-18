import pytest

from app.models import ExecutionPolicyContext
from app.output.verify import verify
from app.policy.detectors import RegexDetector, make_detector

D = RegexDetector()

def _ctx(require):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="high",
        require_verification=require, detected_entities=(), redaction_map_id=None)

def test_pii_in_completion_is_redacted():
    text, rec = verify(D, _ctx(False), "here is 482-73-1946", client_output="redact")
    assert "482-73-1946" not in text
    assert "US_SSN" in rec.entities_found

def test_presidio_egress_masks_blocklisted_ssn():
    """B4: a completion containing the textbook 123-45-6789 must be masked on
    egress under the demo-default presidio detector -- not left raw."""
    pytest.importorskip("presidio_analyzer")
    d = make_detector("presidio")
    text, rec = verify(d, _ctx(False), "the record shows 123-45-6789 on file",
                       client_output="redact")
    assert "123-45-6789" not in text
    assert "US_SSN" in rec.entities_found

def test_required_unverifiable_fails_closed():
    text, rec = verify(D, _ctx(True), "UNVERIFIABLE answer", client_output="redact")
    assert rec.passed is False
    assert text == ""

def test_required_clean_passes():
    text, rec = verify(D, _ctx(True), "the answer is 4", client_output="redact")
    assert rec.passed is True
    assert rec.performed is True
    assert text == "the answer is 4"
    assert text != ""


# --- client_output: raw (design P1a §2.5) ----------------------------------- #
#
# Egress redaction is `client_output`'s call and nothing else's: a real
# detector may have run at intake (for placement, or for telemetry) and still
# must not touch the client's bytes. What `raw` does NOT relax is the
# fail-closed verdict -- that check reads the completion text, never the
# detector, and it stays in force on both sides of the axis.

def test_client_output_raw_leaves_the_completion_untouched():
    text, rec = verify(D, _ctx(False), "here is 482-73-1946", client_output="raw")
    assert text == "here is 482-73-1946"
    assert rec.entities_found == ()

def test_client_output_raw_still_fails_closed_on_unverifiable():
    text, rec = verify(D, _ctx(True), "UNVERIFIABLE 482-73-1946", client_output="raw")
    assert rec.passed is False
    assert text == ""          # never the raw content, even for a raw-output app

def test_client_output_raw_required_and_clean_returns_verbatim():
    text, rec = verify(D, _ctx(True), "ssn 482-73-1946 on file", client_output="raw")
    assert rec.passed is True
    assert rec.performed is True
    assert text == "ssn 482-73-1946 on file"
