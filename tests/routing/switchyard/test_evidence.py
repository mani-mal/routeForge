"""B103/B104: evidence keeps allowlisted keys with the declared type only."""

import math

import pytest

from app.routing.switchyard.evidence import DECISION_SOURCES, filter_evidence


def test_real_stage_router_evidence_passes_through():
    ev = {"source": "dimensions", "confidence": 0.874053287886007, "threshold": 0.5}
    assert filter_evidence(ev) == ev


@pytest.mark.parametrize("source", sorted(DECISION_SOURCES))
def test_known_sources_are_kept(source):
    assert filter_evidence({"source": source}) == {"source": source}


def test_free_text_under_an_allowed_key_is_dropped():
    ev = {"source": "the patient John Smith, SSN 123-45-6789", "confidence": 0.7}
    assert filter_evidence(ev) == {"confidence": 0.7}


def test_unknown_keys_are_dropped():
    ev = {"source": "override", "reason_code": "client_error", "note": "anything", "score": 0.9}
    assert filter_evidence(ev) == {"source": "override"}


@pytest.mark.parametrize(
    "bad", ["0.5", True, None, [0.5], {"v": 0.5}, math.nan, math.inf, -math.inf]
)
def test_numeric_keys_reject_non_numbers(bad):
    assert filter_evidence({"confidence": bad, "threshold": bad}) == {}


def test_ints_are_kept_as_floats():
    assert filter_evidence({"threshold": 1}) == {"threshold": 1.0}


@pytest.mark.parametrize("bad", [None, "source", ["source"], 3])
def test_non_mapping_evidence_is_empty(bad):
    assert filter_evidence(bad) == {}
