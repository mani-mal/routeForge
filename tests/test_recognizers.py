import pytest
from app.policy.recognizers import (
    validate_recognizer, CustomRecognizer, RecognizerTimeout, PRESIDIO_BUILTINS)

def test_valid_recognizer_accepts_good_regex():
    validate_recognizer("BADGE_ID", r"\bEMP-\d{6}\b")  # no raise

def test_rejects_lowercase_name():
    with pytest.raises(ValueError):
        validate_recognizer("badge", r"\d+")

def test_rejects_builtin_name_collision():
    assert "US_SSN" in PRESIDIO_BUILTINS
    with pytest.raises(ValueError):
        validate_recognizer("US_SSN", r"\d{9}")

def test_rejects_uncompilable_regex():
    with pytest.raises(ValueError):
        validate_recognizer("X", r"(unterminated")

def test_rejects_catastrophic_regex():
    with pytest.raises(ValueError):
        validate_recognizer("X", r"(a+)+$")   # classic ReDoS shape

def test_custom_recognizer_finds_entities():
    r = CustomRecognizer("BADGE_ID", r"\bEMP-\d{6}\b", 0.9)
    ents = r.find("hello EMP-123456 world")
    assert [(e.type, e.value) for e in ents] == [("BADGE_ID", "EMP-123456")]
