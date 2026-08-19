from app.models import Entity
from app.policy.detectors import RegexDetector
from app.policy.recognizers import CustomRecognizer
from app.policy.effective import EffectiveDetector
from app.policy.merge import dedupe

def test_custom_entity_added_on_top_of_builtins():
    base = RegexDetector()
    det = EffectiveDetector(base, [CustomRecognizer("BADGE_ID", r"\bEMP-\d{6}\b", 0.9)])
    ents = det.analyze("ssn 482-73-1946 badge EMP-123456")
    types = {e.type for e in ents}
    assert "US_SSN" in types and "BADGE_ID" in types

def test_overlapping_custom_wins_by_score_then_length():
    base = RegexDetector()  # US_BANK_NUMBER matches long digit runs
    det = EffectiveDetector(base, [CustomRecognizer("ACCT", r"\b\d{10}\b", 0.99)])
    ents = det.analyze("acct 1234567890 end")
    spans = [(e.type, e.start, e.end) for e in ents]
    assert ("ACCT", 5, 15) in spans
    # no other entity occupies the same span
    assert sum(1 for _t, s, e in spans if (s, e) == (5, 15)) == 1

def test_anonymize_reuses_placeholder_scheme_for_custom():
    det = EffectiveDetector(RegexDetector(), [CustomRecognizer("BADGE_ID", r"\bEMP-\d{6}\b", 0.9)])
    text = "badge EMP-123456"
    ents = det.analyze(text)
    red, mapping = det.anonymize(text, ents)
    assert "EMP-123456" not in red and "<BADGE_ID_1>" in red

def test_partial_overlap_is_fully_redacted():
    # SHORT [5,13) and LONG [8,25) partially overlap (neither contains the
    # other). A naive greedy "earliest start wins" dedupe (the old
    # behavior) keeps SHORT and drops LONG entirely -- leaking text[13:25],
    # the part of LONG's span that SHORT never covered. The fix must merge
    # overlapping entities into a single span covering the whole cluster.
    text = "xxxxxSENSITIVE-PARTIAL-OVERLAPyyyyy"
    short = Entity(type="SHORT", start=5, end=13, value=text[5:13], score=None)
    long_ = Entity(type="LONG", start=8, end=25, value=text[8:25], score=0.99)

    merged = dedupe([short, long_])
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (5, 25)
    # Highest score (LONG=0.99 beats SHORT's None/0.0) wins the "best" pick.
    assert merged[0].type == "LONG"

    det = EffectiveDetector(RegexDetector(), [])
    redacted, _mapping = det.anonymize(text, merged)
    # No fragment of the sensitive region [5,25) may survive in the output.
    assert text[5:25] not in redacted
    assert text[13:25] not in redacted  # the part the old dedupe would have leaked
    assert "<LONG_1>" in redacted
