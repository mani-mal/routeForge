"""4.1 regression + backward-compat test.

Architecture-review 4.1: a clean "capital of France"-style request (a LOCATION
entity) must route to the CHEAP tier when the app's own privacy profile demotes
LOCATION to risk_class low. Under the OLD global rules (no app profile), the
same request still targets MID (LOCATION -> confidential -> risk_class medium
-> risk_floors.medium: mid). This file pins both: the fix is driven by
per-app rules, not a change to the global defaults.
"""
from app.apps.privacy import SensitivityRuleView
from app.config.loader import load_settings
from app.models import Entity
from app.policy.context import build_context
from app.routing.router import Router
from tests._litellm_fake import make_test_registry

S = load_settings()
REG = make_test_registry(S)
R = Router(S, registry=REG)

_LOCATION_ENTITY = [Entity(type="LOCATION", start=13, end=19, value="France")]

_DEMOTED_RULES = [
    SensitivityRuleView(
        entities=frozenset({"LOCATION"}), sensitivity="internal",
        risk_class="low", residency="any", require_verification=False),
    SensitivityRuleView(
        entities=frozenset(), sensitivity="public",
        risk_class="low", residency="any", require_verification=False),
]


def test_demoted_location_targets_cheap():
    ctx = build_context(
        S, request_id="r1", tenant_id="default", entities=_LOCATION_ENTITY,
        redaction_map_id=None, app_policy=None, app_rules=_DEMOTED_RULES)

    assert ctx.risk_class == "low"
    assert "onprem" in ctx.allowed_providers  # cheap tier must be reachable

    decision = R.select(ctx, tokens_after=9, complexity=0.0045)

    assert decision.chosen.tier == "cheap"
    assert decision.chosen.name == "local-8b"


def test_global_rules_still_target_mid():
    ctx = build_context(
        S, request_id="r2", tenant_id="default", entities=_LOCATION_ENTITY,
        redaction_map_id=None, app_policy=None, app_rules=None)

    assert ctx.risk_class == "medium"
    assert "onprem" in ctx.allowed_providers

    decision = R.select(ctx, tokens_after=9, complexity=0.0045)

    assert decision.chosen.tier == "mid"
