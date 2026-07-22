from app.compression.compressors import DeterministicCompressor
from app.gateway.backends.base import count_tokens


def test_reduces_verbose_repeated_content():
    c = DeterministicCompressor()
    verbose = ("INFO log line here\n" * 200) + "\n\n\n\n" + ("  padded   spaces  \n" * 50)
    out = c.compress_text(verbose)
    assert count_tokens(out) < count_tokens(verbose)


def test_idempotent():
    c = DeterministicCompressor()
    verbose = "a\n\n\n\nb\nb\nb\n" + ("row\t\tvalue\n" * 30)
    once = c.compress_text(verbose)
    assert c.compress_text(once) == once


def test_terse_text_barely_changes():
    c = DeterministicCompressor()
    terse = "What is 12 times 7?"
    out = c.compress_text(terse)
    assert count_tokens(out) <= count_tokens(terse)
    assert "12" in out and "7" in out  # never drops semantic content of a short prompt


def test_drops_consecutive_duplicate_lines():
    c = DeterministicCompressor()
    assert c.compress_text("b\nb\nb\n") == "b\n"
    # non-consecutive duplicates are preserved (only consecutive runs collapse)
    assert c.compress_text("b\nx\nb\n") == "b\nx\nb\n"


def test_idempotent_on_whitespace_only_blank_lines():
    c = DeterministicCompressor()
    x = "a\n   \n   \n   \nb"
    once = c.compress_text(x)
    assert c.compress_text(once) == once  # was the reordering-bug repro
