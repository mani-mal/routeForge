"""Task 7: `StreamingVerifier` hold-back boundary math (unit-level, no FastAPI).

The single most important property: a PII entity split across chunk boundaries
must NEVER have its raw prefix released. These tests drive the hold-back window
directly against a `RegexDetector` (the detector the test env actually uses --
`SP_PII_DETECTION=regex` in conftest; no Presidio required), asserting the
concatenated *released* stream (all `feed` outputs + `flush`) is redaction-safe
and equals the redacted full text.
"""
from __future__ import annotations

from app.models import ExecutionPolicyContext
from app.output.streaming_verify import HOLDBACK_CHARS, StreamingVerifier
from app.policy.detectors import RegexDetector


def _ctx() -> ExecutionPolicyContext:
    # A non-onprem, no-verification ctx -- the only shape that ever reaches the
    # incremental_verify streaming path (anything floored is StreamFallback'd).
    return ExecutionPolicyContext(
        request_id="req-x", tenant_id="acme", sensitivity="internal",
        onprem_only=False, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False, detected_entities=(),
        redaction_map_id=None)


def test_ssn_split_across_boundary_never_leaks_raw_prefix() -> None:
    """(a) The SSN is fed as `...123-45-` then `6789 ok`. The raw digits must
    never appear in the released stream; the entity surfaces redacted."""
    v = StreamingVerifier(RegexDetector(), _ctx())
    outs = [v.feed("my ssn is 123-45-"), v.feed("6789 ok"), v.flush()]
    released = "".join(outs)

    assert "123-45-6789" not in released
    assert "123-45-" not in released           # not even the raw prefix leaked
    assert "<US_SSN_1>" in released
    assert released == "my ssn is <US_SSN_1> ok"


def test_benign_stream_released_in_order_with_holdback_delay() -> None:
    """(b) A benign stream (no entities) is released in order; the hold-back
    delays some text to `flush`. Concatenated released output == the full text
    (redaction is a no-op when there is nothing to redact)."""
    text = ("The quick brown fox jumps over the lazy dog and then keeps on "
            "running down the country road for a good long while.")
    assert len(text) > HOLDBACK_CHARS  # long enough to release before flush

    v = StreamingVerifier(RegexDetector(), _ctx())
    feeds = [v.feed(text[0:30]), v.feed(text[30:70]), v.feed(text[70:])]
    tail = v.flush()

    before_flush = "".join(feeds)
    assert before_flush != ""                  # some text released before flush
    assert text.startswith(before_flush)       # released strictly in order
    assert tail != ""                          # flush released the held-back tail
    assert before_flush + tail == text


def test_entity_fully_inside_one_delta_is_redacted() -> None:
    """(c) An SSN sitting well before the hold-back window (followed by enough
    trailing text) is redacted in the very feed that completes it."""
    delta = ("here is 123-45-6789 and then a lot more padding text that easily "
             "exceeds the hold-back window so the entity releases immediately.")
    v = StreamingVerifier(RegexDetector(), _ctx())
    out = v.feed(delta)

    assert "123-45-6789" not in out
    assert "<US_SSN_1>" in out


def test_entity_straddling_the_safe_boundary_stays_buffered() -> None:
    """An entity whose span crosses the `safe` cut is held whole -- its raw
    prefix is never released mid-entity -- and surfaces redacted at flush."""
    # 11-char SSN + a space + 41 trailing chars => safe = 53-48 = 5, which lands
    # *inside* the SSN span (0..11): the cut is pulled back to 0, releasing
    # nothing. (The trailing space gives the SSN its closing word boundary, as a
    # real token stream would -- the regex needs `\b` after the digits.)
    out = StreamingVerifier(RegexDetector(), _ctx()).feed("123-45-6789")
    assert out == ""  # < HOLDBACK_CHARS, nothing releasable yet

    v = StreamingVerifier(RegexDetector(), _ctx())
    straddle = v.feed("123-45-6789 " + "z" * 41)
    assert "123-45-6789" not in straddle        # straddled boundary -> held
    whole = straddle + v.flush()
    assert "123-45-6789" not in whole
    assert "<US_SSN_1>" in whole


def test_flush_on_empty_buffer_returns_empty() -> None:
    v = StreamingVerifier(RegexDetector(), _ctx())
    assert v.flush() == ""
