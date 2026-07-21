from app.policy.detectors import RegexDetector, make_detector


def test_regex_detects_and_redacts_ssn():
    d = RegexDetector()
    ents = d.analyze("my ssn is 123-45-6789 ok")
    assert any(e.type == "US_SSN" for e in ents)
    redacted, mapping = d.anonymize("my ssn is 123-45-6789 ok", ents)
    assert "123-45-6789" not in redacted
    assert "<US_SSN_1>" in redacted
    assert mapping["<US_SSN_1>"] == "123-45-6789"


def test_null_detector_finds_nothing():
    assert make_detector("off").analyze("123-45-6789") == []


def test_clean_text_no_entities():
    assert RegexDetector().analyze("what is the capital of france") == []
