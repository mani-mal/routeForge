import pytest

pytest.importorskip("headroom")  # skip when [compression] extra absent


def test_headroom_compressor_conforms():
    from app.compression.headroom import HeadroomCompressor
    c = HeadroomCompressor()
    assert c.name == "headroom"
    out = c.compress_text("INFO log line\n" * 500)
    assert isinstance(out, str)
