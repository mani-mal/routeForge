import pytest

from app.config.loader import load_settings
from app.models import ExecutionPolicyContext
from app.routing.router import Router

S = load_settings()
R = Router(S)


def _ctx(providers, risk="low", onprem=False):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
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
                      "local-32b": QualityScore(0.1, "customer")}))
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


def test_budget_exhaustion_raises():
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=0.0, max_latency_ms=15000,
        risk_class="high", require_verification=False, detected_entities=(),
        redaction_map_id=None)
    with pytest.raises(ValueError):
        R.select(ctx, tokens_after=20, complexity=0.1)
