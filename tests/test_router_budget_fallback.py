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


def _three_tier_registry(s):
    """The seeded cheap/mid test models plus an on-prem frontier model."""
    import asyncio

    from app.apps import store
    from app.apps.bootstrap import seed_test_models
    from app.apps.db import Database
    from app.apps.registry import ModelRegistry

    async def build():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as session:
            await seed_test_models(session)
            await store.upsert_onboarded(
                session, "local-frontier", display_name="local-frontier",
                provider="onprem", tier="frontier", endpoint="http://test-local/v1",
                api_key_env=None, price_input=1.0, price_output=1.0, quality=None,
                context_window=None, allowed_app_ids=None, notes=None,
                enabled=True, probe_status="healthy", probe_error=None)
            await session.commit()
        reg = ModelRegistry(s)
        await reg.refresh(db)
        return reg

    return asyncio.run(build())


def test_fallback_relabels_skipped_over_budget_tier():
    # frontier est 0.0021, mid 0.00021, cheap 0.000105; cap 0.00015. The
    # fallback skips mid (over budget too) and lands on cheap: mid must read
    # "over budget", not "below target tier".
    s = load_settings()
    reg = _three_tier_registry(s)
    ctx = build_context(s, "req-f3", "acme", [], None,
                        app_policy=AppPolicyView(residency="any"), registry=reg)
    ctx = ctx.model_copy(update={"max_cost_usd": 0.00015, "risk_class": "low"})
    d = Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)
    reasons = {r.name: r.reason for r in d.rejected}
    assert d.chosen.name == "local-8b"
    assert "budget fallback from frontier" in d.reason
    assert reasons["local-frontier"] == "over budget"
    assert reasons["local-32b"] == "over budget"
