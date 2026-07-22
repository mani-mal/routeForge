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


def test_persona_build_shapes():
    rng = random.Random(7)
    for p in load_personas():
        body = p.build(rng)
        assert "messages" in body and body["messages"]
        if p.name == "multi_step":
            assert body.get("mock_multistep") is True
