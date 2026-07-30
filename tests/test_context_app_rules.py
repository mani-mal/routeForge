from app.apps.privacy import SensitivityRuleView
from app.config.loader import load_settings
from app.models import Entity
from app.policy.context import build_context

S = load_settings()

def test_app_rules_demote_location():
    rules = [SensitivityRuleView(frozenset({"LOCATION"}), "internal", "low", "any", False),
             SensitivityRuleView(frozenset(), "public", "low", "any", False)]
    ctx = build_context(S, "r1", "acme", [Entity(type="LOCATION", start=0, end=6, value="France")],
                        None, app_rules=rules)
    assert ctx.sensitivity == "internal" and ctx.risk_class == "low"

def test_onprem_tenant_downgrade_still_floored():
    # even if a rule says residency=any, an onprem_only tenant stays onprem_only
    rules = [SensitivityRuleView(frozenset({"US_SSN"}), "restricted", "high", "any", True),
             SensitivityRuleView(frozenset(), "public", "low", "any", False)]
    ctx = build_context(S, "r2", "hospital", [Entity(type="US_SSN", start=0, end=11, value="482-73-1946")],
                        None, app_rules=rules)
    # tenant default (hospital) is onprem_only in policy.yaml -> backstop holds
    assert ctx.onprem_only is True
