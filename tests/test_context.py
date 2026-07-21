from app.config.loader import load_settings
from app.models import Entity
from app.policy.context import build_context

S = load_settings()


def test_ssn_forces_onprem_only():
    ents = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = build_context(S, "r1", "default", ents, "map1")
    assert ctx.sensitivity == "restricted"
    assert ctx.onprem_only is True
    assert "azure" not in ctx.allowed_providers
    assert "bedrock" not in ctx.allowed_providers
    assert ctx.require_verification is True


def test_clean_request_full_pool():
    ctx = build_context(S, "r1", "default", [], None)
    assert ctx.sensitivity == "public"
    assert ctx.onprem_only is False
    assert "onprem" in ctx.allowed_providers


def test_tenant_residency_tightens():
    # hospital defaults to onprem_only even with no entities
    ctx = build_context(S, "hospital", "hospital", [], None)
    assert ctx.onprem_only is True
