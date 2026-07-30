import pytest

from app.policy.detectors import NullDetector, RegexDetector, make_detector

# The SSN used across the demo and this suite. It must be detectable by EVERY
# configured detector, not just the regex one the tests happen to run on.
DEMO_SSN = "482-73-1946"

# Presidio's UsSsnRecognizer.invalidate_result() blocklists well-known sample
# SSNs ("123456789", "078051120", ...). The suite previously used 123-45-6789
# everywhere, so with the DEFAULT config (pii_detection: presidio) the gate
# scored the flagship privacy request as sensitivity=public / onprem_only=false
# and every test still passed, because tests only ever exercised RegexDetector.
PRESIDIO_BLOCKLISTED_SSN = "123-45-6789"


def test_regex_detects_and_redacts_ssn():
    d = RegexDetector()
    ents = d.analyze("my ssn is 482-73-1946 ok")
    assert any(e.type == "US_SSN" for e in ents)
    redacted, mapping = d.anonymize("my ssn is 482-73-1946 ok", ents)
    assert "482-73-1946" not in redacted
    assert "<US_SSN_1>" in redacted
    assert mapping["<US_SSN_1>"] == "482-73-1946"


def test_null_detector_finds_nothing():
    assert make_detector("off").analyze("482-73-1946") == []


def test_clean_text_no_entities():
    assert RegexDetector().analyze("what is the capital of france") == []


def test_multi_entity_placeholders_number_left_to_right():
    d = RegexDetector()
    text = "call 111-222-3333 or 444-555-6666 thanks"
    ents = d.analyze(text)
    redacted, mapping = d.anonymize(text, ents)
    assert mapping["<PHONE_NUMBER_1>"] == "111-222-3333"
    assert mapping["<PHONE_NUMBER_2>"] == "444-555-6666"
    assert redacted == "call <PHONE_NUMBER_1> or <PHONE_NUMBER_2> thanks"


# --- cross-detector agreement ----------------------------------------------
# These run only where the presidio extra is installed (uv sync --extra
# presidio); the container image and the DGX deployment both have it, so CI on
# a lean env skips rather than fails.


@pytest.mark.parametrize("detector", ["regex", "presidio"])
def test_every_detector_flags_the_demo_ssn(detector):
    """The demo SSN must trip US_SSN under whichever detector is configured.

    Guards the failure this suite missed: a demo SSN that regex flags and
    Presidio silently ignores, turning the privacy story into a no-op on the
    default config.
    """
    if detector == "presidio":
        pytest.importorskip("presidio_analyzer")
    d = make_detector(detector)
    ents = d.analyze(f"my ssn is {DEMO_SSN} ok")
    assert any(e.type == "US_SSN" for e in ents), (
        f"{detector} detector did not flag {DEMO_SSN}"
    )


def test_presidio_blocklists_sample_ssns():
    """Pin the upstream behaviour that caused the bug.

    Not a wish — a fact about Presidio we must design around. If a future
    Presidio release stops blocklisting sample SSNs this fails loudly, which is
    the signal to revisit the constraints in tests/traffic/personas.fake_ssn.
    """
    pytest.importorskip("presidio_analyzer")
    d = make_detector("presidio")
    ents = d.analyze(f"my ssn is {PRESIDIO_BLOCKLISTED_SSN} ok")
    assert not any(e.type == "US_SSN" for e in ents), (
        "Presidio now flags sample SSNs; fake_ssn's constraints can be relaxed"
    )


# --- warmup (startup cold-start avoidance) ---------------------------------
# create_app() calls detector.warmup() at boot so the demo's first request
# doesn't pay Presidio's lazy first-call latency (which was observed tripping
# the fail-closed gate into a one-off 503). Warmup must be a safe no-op on the
# lightweight detectors and must not perturb subsequent analyze().


@pytest.mark.parametrize("detector", ["regex", "off"])
def test_warmup_is_a_harmless_noop_on_light_detectors(detector):
    d = make_detector(detector)
    assert d.warmup() is None
    # still fully functional afterward
    d.analyze(f"my ssn is {DEMO_SSN}")


def test_every_detector_exposes_warmup():
    for d in (RegexDetector(), NullDetector()):
        assert callable(getattr(d, "warmup", None))


def test_presidio_warmup_runs_and_leaves_analyze_working():
    """Warmup must not raise and must not change detection behaviour.

    Runs only where the presidio extra is installed (image + DGX have it).
    """
    pytest.importorskip("presidio_analyzer")
    d = make_detector("presidio")
    d.warmup()  # must not raise
    # idempotent: a real analyze still flags the demo SSN after warmup
    ents = d.analyze(f"my ssn is {DEMO_SSN} ok")
    assert any(e.type == "US_SSN" for e in ents)
