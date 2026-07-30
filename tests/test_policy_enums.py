from app.policy.enums import SENSITIVITY, RISK_CLASS, RESIDENCY


def test_canonical_sets_exact():
    assert SENSITIVITY == frozenset({"public", "internal", "confidential", "restricted"})
    assert RISK_CLASS == frozenset({"low", "medium", "high"})
    assert RESIDENCY == frozenset({"any", "private_cloud_ok", "onprem_only"})
