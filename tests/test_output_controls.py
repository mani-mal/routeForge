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
    text, rec = verify(D, _ctx(False), "here is 482-73-1946")
    assert "482-73-1946" not in text
    assert "US_SSN" in rec.entities_found


def test_presidio_egress_masks_blocklisted_ssn():
    """B4: a completion containing the textbook 123-45-6789 must be masked on
    egress under the demo-default presidio detector -- not left raw."""
    pytest.importorskip("presidio_analyzer")
    d = make_detector("presidio")
    text, rec = verify(d, _ctx(False), "the record shows 123-45-6789 on file")
    assert "123-45-6789" not in text
    assert "US_SSN" in rec.entities_found


def test_required_unverifiable_fails_closed():
    text, rec = verify(D, _ctx(True), "UNVERIFIABLE answer")
    assert rec.passed is False
    assert text == ""


def test_required_clean_passes():
    text, rec = verify(D, _ctx(True), "the answer is 4")
    assert rec.passed is True
    assert rec.performed is True
    assert text == "the answer is 4"
    assert text != ""
