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
