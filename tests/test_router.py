import pytest

from app.config.loader import load_settings
from app.models import ExecutionPolicyContext
from app.routing.router import Router
from tests._litellm_fake import make_test_registry

# These tests exercise router scoring/tier logic and expect both cloud tiers
# (mid via azure, frontier via bedrock) to be present as real pool candidates
# -- that is orthogonal to B10 (disabled providers must not enter the pool),
# which is covered directly in test_model_registry.py and
# test_residency_rejection_is_labeled_residency_not_generic below. Enable
# both cloud providers here so the default-disabled B10 filter doesn't starve
# these scoring tests of the mid/frontier candidates they were written against.
S = load_settings()
_prov = dict(S.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S = S.model_copy(update={"serving": S.serving.model_copy(update={"providers": _prov})})
REG = make_test_registry(S)
R = Router(S, registry=REG)


def _eligible(providers):
    """The eligible set these scoring tests have always assumed: an app that
    narrows nothing, so every pool candidate whose provider is permitted is a
    candidate. Exactly what `build_context` computes for an app with no
    allowed_tiers/allowed_models -- stated here rather than left empty because
    `ctx.allowed_models` is binding on the router, with no flag that can turn
    it off (app/policy/feasible.py)."""
    return frozenset(c.name for c in REG.effective_pool()
                     if c.provider in providers)


def _ctx(providers, risk="low", onprem=False):
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers, allowed_models=_eligible(providers),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def test_clean_low_complexity_picks_cheap():
    d = R.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert d.chosen.tier == "cheap"


def test_onprem_only_excludes_cloud_with_reason():
    d = R.select(_ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert any(r.provider in {"azure", "bedrock"}
               and "residency" in r.reason for r in d.rejected)


def test_high_risk_cloud_allowed_picks_frontier():
    d = R.select(_ctx({"onprem", "azure", "bedrock"}, risk="high"),
                 tokens_after=20, complexity=0.1)
    assert d.chosen.tier == "frontier"


def test_high_risk_onprem_only_stays_on_prem():
    d = R.select(_ctx({"onprem"}, risk="high", onprem=True),
                 tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert d.chosen.tier == "mid"  # best on-prem tier, not frontier


def test_nearest_up_when_target_tier_absent():
    # only azure allowed; azure has no cheap-tier member -> nearest-up to mid
    d = R.select(_ctx({"azure"}), tokens_after=20, complexity=0.1)
    assert d.chosen.name == "azure-gpt-mid"
    assert d.chosen.tier == "mid"


def test_min_tier_escalation_raises_target_and_prefers_cheaper():
    d = R.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=20,
                 complexity=0.1, min_tier="mid")
    assert d.chosen.tier == "mid"
    assert d.chosen.name == "local-32b"  # cheaper on-prem beats pricier azure within mid


def test_higher_quality_wins_within_tier_when_cost_latency_equal():
    from app.routing.quality import QualityResolver, QualityScore

    # Two mid-tier models: local-32b (onprem) and azure-gpt-mid. Give azure a
    # much higher override so quality dominates the (cheaper-onprem) tie-break.
    r = Router(S, quality_resolver=QualityResolver(
        S, overrides={"azure-gpt-mid": QualityScore(1.0, "customer"),
                      "local-32b": QualityScore(0.1, "customer")}),
        registry=REG)
    # min_tier="mid" forces landing on the mid tier, same as
    # test_min_tier_escalation_raises_target_and_prefers_cheaper above, where
    # local-32b normally wins on cost. Here the quality override should flip it.
    decision = r.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=200,
                         complexity=0.1, min_tier="mid")
    assert decision.chosen.name == "azure-gpt-mid"
    assert decision.quality_source == "customer"
    assert decision.quality_score == 1.0


def test_default_resolver_reproduces_tier_default_quality():
    # R is the module-level Router(S) with no resolver injected -> settings-only,
    # empty overrides.
    decision = R.select(_ctx({"onprem", "azure", "bedrock"}), tokens_after=20,
                         complexity=0.1)
    assert decision.quality_source == "tier_default"
    assert decision.quality_score == S.routing.tier_quality[decision.chosen.tier]


def test_residency_rejection_is_labeled_residency_not_generic():
    from app.config.loader import load_settings
    from app.policy.context import build_context
    from app.models import Entity
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    # a registry with a healthy onboarded on-prem model, so the onprem_only
    # request still has an eligible candidate and select() returns normally
    # instead of raising "no candidates after residency filter" -- we need to
    # inspect dec.rejected for the mislabeled cloud rejection.
    reg = make_test_registry(s)
    # hospital tenant is onprem_only in the default policy.yaml
    ctx = build_context(s, "req", "hospital",
                        [Entity(type="US_SSN", start=0, end=11, value="482-73-1946")],
                        None, registry=reg)
    assert ctx.onprem_only is True
    dec = Router(s, registry=reg).select(ctx, tokens_after=50, complexity=0.1)
    reasons = {r.name: r.reason for r in dec.rejected}
    # a cloud candidate dropped by residency reads "residency", never the app-policy label
    assert any(v == "excluded · residency" for v in reasons.values())


def test_app_policy_provider_narrowing_is_labeled_app_policy_not_residency():
    # The router's else-branch: a non-onprem_only request whose app policy
    # narrows allowed_providers must read "not permitted for application",
    # never the residency label -- that label is reserved for genuinely
    # onprem_only requests (see the test above).
    from app.config.loader import load_settings
    from app.apps.policy import AppPolicyView
    from app.policy.context import build_context
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    reg = make_test_registry(s)
    app_policy = AppPolicyView(residency="any", allowed_providers=frozenset({"onprem"}))
    ctx = build_context(s, "req", "acme", [], None, app_policy=app_policy, registry=reg)
    assert ctx.onprem_only is False
    dec = Router(s, registry=reg).select(ctx, tokens_after=50, complexity=0.1)
    reasons = {r.name: r.reason for r in dec.rejected}
    assert reasons.get("bedrock-frontier") == "not permitted for application"
    assert "excluded · residency" not in reasons.values()


def test_budget_exhaustion_raises():
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=_eligible({"onprem", "azure", "bedrock"}),
        max_cost_usd=0.0, max_latency_ms=15000,
        risk_class="high", require_verification=False, detected_entities=(),
        redaction_map_id=None)
    with pytest.raises(ValueError):
        R.select(ctx, tokens_after=20, complexity=0.1)
