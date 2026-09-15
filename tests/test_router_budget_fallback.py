import pytest

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.policy.context import build_context
from app.routing.router import Router
from tests._litellm_fake import make_test_registry

# local-8b: cheap $0.05/$0.05; local-32b: mid $0.10/$0.10 (per 1M).
# 2000 prompt tokens -> complexity 1.0 -> frontier target, clamped to mid.
# mid est = (2000*.10 + 100*.10)/1e6 = 0.00021; cheap est = 0.000105.


def _ctx(risk_class="low", cap=0.00015):
    s = load_settings()
    reg = make_test_registry(s)
    ctx = build_context(s, "req-f", "acme", [], None,
                        app_policy=AppPolicyView(residency="any"), registry=reg)
    return s, reg, ctx.model_copy(update={"max_cost_usd": cap, "risk_class": risk_class})


def test_over_budget_target_tier_falls_back_to_cheaper_allowed_tier():
    s, reg, ctx = _ctx()
    d = Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)
    assert d.chosen.name == "local-8b"
    assert "budget fallback from mid" in d.reason
    assert {r.name: r.reason for r in d.rejected}["local-32b"] == "over budget"


def test_fallback_never_goes_below_the_risk_floor():
    s, reg, ctx = _ctx(risk_class="medium")        # risk floor mid
    with pytest.raises(NoEligibleCandidate, match="no candidates after budget filter"):
        Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)


def test_fallback_never_goes_below_escalation_min_tier():
    s, reg, ctx = _ctx()
    with pytest.raises(NoEligibleCandidate, match="no candidates after budget filter"):
        Router(s, registry=reg).select(ctx, 2000, 1.0, min_tier="mid", output_tokens=100)


def test_no_fallback_needed_keeps_old_reason():
    s, reg, ctx = _ctx(cap=1.0)
    d = Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)
    assert d.chosen.name == "local-32b" and "fallback" not in d.reason
