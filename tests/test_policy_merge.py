from app.models import Entity
from app.policy.merge import dedupe


def test_non_overlapping_entities_pass_through():
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946"),
            Entity(type="EMAIL_ADDRESS", start=20, end=27, value="a@b.co")]
    out = dedupe(ents)
    assert len(out) == 2


def test_overlapping_entities_merge_to_one_covering_span():
    # Two partially overlapping spans must collapse to ONE placeholder span
    # covering [min(start), max(end)) so no raw tail leaks (see the module
    # docstring / effective.py rationale).
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946"),
            Entity(type="US_BANK_NUMBER", start=8, end=15, value="1946123")]
    out = dedupe(ents)
    assert len(out) == 1
    assert out[0].start == 0 and out[0].end == 15


def test_empty_is_empty():
    assert dedupe([]) == []
