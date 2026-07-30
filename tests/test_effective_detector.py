from app.policy.detectors import RegexDetector
from app.policy.recognizers import CustomRecognizer
from app.policy.effective import EffectiveDetector

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
