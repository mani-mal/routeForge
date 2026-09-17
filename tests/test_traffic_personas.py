import random

from tests.traffic.personas import load_personas, sample, fake_ssn, fake_email


def test_weights_load():
    ps = {p.name: p for p in load_personas()}
    assert ps["simple_qa"].weight == 45
    assert ps["pii_bearing"].weight == 15
    assert ps["pii_bearing"].expect_onprem is True


def test_sampling_deterministic_and_weighted():
    ps = load_personas()
    a = [p.name for p in sample(ps, random.Random(42), 1000)]
    b = [p.name for p in sample(ps, random.Random(42), 1000)]
    assert a == b  # seeded -> reproducible
    # simple_qa (weight 45) should dominate; pii_bearing present
    assert a.count("simple_qa") > a.count("pii_bearing") > 0


def test_synthetic_pii_matches_detector_patterns():
    from app.policy.detectors import RegexDetector
    d = RegexDetector()
    rng = random.Random(1)
    ssn = fake_ssn(rng)
    assert any(e.type == "US_SSN" for e in d.analyze(f"my ssn is {ssn}"))
    email = fake_email(rng)
    assert any(e.type == "EMAIL_ADDRESS" for e in d.analyze(f"reach me at {email}"))


def test_generated_ssns_are_allocatable():
    """Every generated SSN must satisfy the SSA rules Presidio enforces.

    Checked structurally so it runs without the presidio extra. The old
    generator emitted ~11% unallocatable numbers (area 9xx alone is 10%), each
    a high_risk persona request Presidio scored as clean — invisible because
    the burst only ever reported the regex detector's view.
    """
    rng = random.Random(1234)
    for _ in range(2000):
        ssn = fake_ssn(rng)
        area, group, serial = ssn.split("-")
        digits = area + group + serial
        assert area not in ("000", "666"), ssn
        assert not area.startswith("9"), ssn
        assert group != "00", ssn
        assert serial != "0000", ssn
        assert len(set(digits)) > 1, ssn
        assert digits not in ("123456789", "078051120"), ssn


def test_generated_ssns_are_detected_by_presidio():
    """End-to-end proof against the real recognizer, where it's installed."""
    import pytest
    pytest.importorskip("presidio_analyzer")
    from app.policy.detectors import make_detector
    d = make_detector("presidio")
    rng = random.Random(99)
    for _ in range(50):
        ssn = fake_ssn(rng)
        ents = d.analyze(f"my ssn is {ssn}, please summarize my account")
        assert any(e.type == "US_SSN" for e in ents), f"presidio missed {ssn}"


def test_persona_build_shapes():
    rng = random.Random(7)
    for p in load_personas():
        body = p.build(rng)
        assert "messages" in body and body["messages"]
        if p.name == "large_context":
            assert "mock_multistep" not in body
            assert len(body["messages"][0]["content"]) > 2000
