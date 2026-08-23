from app.models import Entity
from app.policy.merge import dedupe


def test_non_overlapping_entities_pass_through():
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946"),
            Entity(type="EMAIL_ADDRESS", start=20, end=27, value="a@b.co")]
    out = dedupe(ents)
    assert len(out) == 2


def test_scored_ssn_wins_type_over_coincidental_datetime_overlap():
    # Regression (B20): spaCy nondeterministically tags an SSN-format string
    # (e.g. 818-30-4221, 818 = a real area code) as DATE_TIME/PHONE_NUMBER on
    # the SAME span as the regex US_SSN backstop. If the coincidental Presidio
    # entity won the merge, the request would DOWNGRADE out of restricted/
    # onprem_only (policy.yaml) and an SSN could route to cloud (rule 3/8). The
    # backstop carries score=1.0 (PresidioDetector.analyze) so US_SSN wins.
    same = ("818-30-4221", 11)
    datetime_hit = Entity(type="DATE_TIME", start=0, end=same[1], value=same[0], score=0.0)
    ssn_hit = Entity(type="US_SSN", start=0, end=same[1], value=same[0], score=1.0)
    for ents in ([datetime_hit, ssn_hit], [ssn_hit, datetime_hit]):  # order-independent
        out = dedupe(ents)
        assert len(out) == 1
        assert out[0].type == "US_SSN", f"SSN lost the overlap to {out[0].type}"
        assert out[0].start == 0 and out[0].end == same[1]


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
