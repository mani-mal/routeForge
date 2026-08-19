from app.api.routes import _sanitize_session_id


def test_valid_id_passes():
    assert _sanitize_session_id("inv-tck-1001-ab12cd34") == "inv-tck-1001-ab12cd34"
    assert _sanitize_session_id("  ok.id:1  ") == "ok.id:1"  # trimmed


def test_absent_or_empty_is_none():
    assert _sanitize_session_id(None) is None
    assert _sanitize_session_id("") is None
    assert _sanitize_session_id("   ") is None


def test_over_128_chars_is_none():
    assert _sanitize_session_id("a" * 128) == "a" * 128
    assert _sanitize_session_id("a" * 129) is None


def test_bad_charset_is_none():
    assert _sanitize_session_id("has space") is None
    assert _sanitize_session_id("bad@char") is None
    assert _sanitize_session_id("123-45-6789 leaked") is None  # embedded PII-ish + space
    assert _sanitize_session_id("tab\tchar") is None
