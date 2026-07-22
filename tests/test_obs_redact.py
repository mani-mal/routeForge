from app.obs.redact import redact, redact_value


def test_redacts_ssn_email_phone():
    out = redact("SSN 123-45-6789, mail a@b.com, call 415-555-0100")
    assert "123-45-6789" not in out
    assert "a@b.com" not in out
    assert "415-555-0100" not in out
    assert "<US_SSN>" in out or "<SSN>" in out


def test_passes_through_clean_text():
    assert redact("just a normal sentence") == "just a normal sentence"


def test_redact_value_non_str_passthrough():
    assert redact_value(42) == 42
    assert redact_value(None) is None
    assert redact_value(1.5) == 1.5
