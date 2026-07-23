import copy

from app.config.loader import load_settings
from app.models import Entity
from app.policy.context import build_context

S = load_settings()


def test_ssn_forces_onprem_only():
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946")]
    ctx = build_context(S, "r1", "default", ents, "map1")
    assert ctx.sensitivity == "restricted"
    assert ctx.onprem_only is True
    assert "azure" not in ctx.allowed_providers
    assert "bedrock" not in ctx.allowed_providers
    assert ctx.require_verification is True
    assert ctx.detected_entities == ("US_SSN",)
    assert "482-73-1946" not in ctx.detected_entities


def test_clean_request_includes_cloud_when_enabled():
    s = copy.deepcopy(S)
    s.serving.providers["azure"].enabled = True
    s.serving.providers["bedrock"].enabled = True
    ctx = build_context(s, "r1", "default", [], None)
    assert ctx.sensitivity == "public"
    assert ctx.onprem_only is False
    assert "onprem" in ctx.allowed_providers
    assert "azure" in ctx.allowed_providers      # else-branch: cloud reachable when policy permits
    assert "bedrock" in ctx.allowed_providers


def test_onprem_only_excludes_cloud_even_when_enabled():
    s = copy.deepcopy(S)
    s.serving.providers["azure"].enabled = True
    s.serving.providers["bedrock"].enabled = True
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946")]
    ctx = build_context(s, "r1", "default", ents, "map1")
    assert ctx.onprem_only is True
    assert ctx.allowed_providers == frozenset({"onprem"})  # cloud excluded by construction


def test_tenant_residency_tightens():
    # hospital defaults to onprem_only even with no entities
    ctx = build_context(S, "hospital", "hospital", [], None)
    assert ctx.onprem_only is True
