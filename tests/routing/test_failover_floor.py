"""B70 items 1-3 / B71 items 1-2: failover and the breaker never serve a
request below its HARD floor -- max(risk floor, escalation min_tier), clamped
down to the best tier in the POLICY-eligible set (CLAUDE.md rule 8). Only
policy may lower the floor; a failure on this request or an open circuit is
endpoint state and never does.

A tier above the hard floor that only complexity raised the target to MAY be
stepped down (to the highest surviving tier >= the hard floor), and every such
step is recorded as `tier_downgrade`."""
import asyncio

import pytest

from app.apps.db import Database
from app.apps.outcomes import build_failure_outcome, trace_features
from app.apps.registry import ModelRegistry
from app.apps import store
from app.config.loader import load_settings
from app.errors import BackendUnavailable, NoEligibleCandidate
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, Message
from app.routing.quality import QualityResolver
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM

_S0 = load_settings()
_prov = dict(_S0.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S = _S0.model_copy(update={"serving": _S0.serving.model_copy(update={"providers": _prov})})

FRONTIER = "bedrock-frontier"   # the static pool's only frontier member
# onprem + bedrock: on-prem tiers plus exactly one frontier (no azure mid).
_ONPREM_FRONTIER = {"onprem", "bedrock"}


def _onprem(slug: str, *, tier: str = "cheap") -> dict:
    return dict(display_name=slug, provider="onprem", tier=tier,
               price_input=0.1, price_output=0.1, endpoint=f"http://{slug}/v1",
               enabled=True, allowed_app_ids=None)


async def _build_reg(rows: dict[str, dict]) -> ModelRegistry:
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, fields in rows.items():
            await store.upsert_onboarded(s, slug, **fields)
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg


def _reg(rows: dict[str, dict]) -> ModelRegistry:
    return asyncio.run(_build_reg(rows))


def _ctx(reg, providers, *, onprem: bool = False, risk: str = "low",
         max_cost: float = 1.0, **extra) -> ExecutionPolicyContext:
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in reg.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None,
        **extra)


class _FailForModels(_DeterministicLiteLLM):
    def __init__(self, settings, fail_models: set[str]) -> None:
        super().__init__(settings)
        self._fail = set(fail_models)
        self.calls: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(model)
        if model in self._fail:
            raise RuntimeError(f"forced failure for {model}")
        return await super().generate(model, messages, price, tier, hint)


def _gw_run(reg, backend, ctx, complexity, router=None):
    gw = Gateway(S, backend, router or Router(S, registry=reg), reg)
    return asyncio.run(gw.run(ctx, [Message(role="user", content="hello")],
                              20, complexity, {}))


def _failure_row(exc, ctx_extra: dict | None = None) -> dict:
    return build_failure_outcome(
        status="backend_failed", request_id="r", application_id="t",
        outcome_ctx=ctx_extra or {}, routing_policy_version="v",
        endpoint_registry_version=None, workload=None, workload_source="none",
        attempts=exc.attempted_models, routing_trace=exc.routing_trace,
        no_failover=exc.no_failover)


# --- 1. high risk: the only frontier fails -> 502, never cheap -------------

def test_router_high_risk_frontier_failed_is_not_lowered_to_cheap():
    reg = _reg({"a": _onprem("a")})
    ctx = _ctx(reg, _ONPREM_FRONTIER, risk="high")
    with pytest.raises(NoEligibleCandidate) as ei:
        Router(S, registry=reg).select(ctx, 20, 0.1, exclude=frozenset({FRONTIER}))
    assert ei.value.below_floor == "frontier"
    assert "risk floor (frontier)" in str(ei.value)


def test_gateway_high_risk_only_frontier_fails_is_a_no_failover_502():
    reg = _reg({"a": _onprem("a")})
    backend = _FailForModels(S, {FRONTIER})
    with pytest.raises(BackendUnavailable) as ei:
        _gw_run(reg, backend, _ctx(reg, _ONPREM_FRONTIER, risk="high"), 0.1)
    assert backend.calls == [FRONTIER]          # never the cheap endpoint
    exc = ei.value
    assert exc.no_failover is True
    assert "no failover candidate" in str(exc)
    assert "risk floor (frontier)" in str(exc)
    row = _failure_row(exc)
    assert row["status"] == "backend_failed"
    assert row["tier_target"] == "frontier"
    assert FRONTIER in row["eligible"]
    assert row["attempts"] == [FRONTIER]
    assert row["route_reason"] == "no_failover"
    assert row["chosen"] is None


# --- 2. complexity-raised frontier fails -> stepped down, recorded --------

def test_router_complexity_raised_frontier_failed_steps_down_and_records():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER), 20, 0.9,
                                        exclude=frozenset({FRONTIER}))
    assert d.chosen.name == "m"
    assert d.tier_target == "frontier"
    assert d.tier_downgrade == {"from": "frontier", "to": "mid", "cause": "prior_failure"}
    assert d.scoring_record()["tier_downgrade"] == d.tier_downgrade


def test_gateway_complexity_raised_frontier_fails_is_served_lower_and_recorded():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    backend = _FailForModels(S, {FRONTIER})
    _result, records = _gw_run(reg, backend, _ctx(reg, _ONPREM_FRONTIER), 0.9)
    assert backend.calls == [FRONTIER, "m"]
    failed, served = records
    assert failed.tier_downgrade is None
    assert served.chosen == "m"
    assert served.tier_downgrade == {"from": "frontier", "to": "mid",
                                     "cause": "prior_failure"}
    f = trace_features([r.model_dump() for r in records], complexity_tier="frontier")
    assert f["route_reason"] == "failover"
    assert f["tier_target"] == "frontier"
    assert f["scoring"]["tier_downgrade"] == served.tier_downgrade


# --- 3. same-tier peer first, no downgrade -------------------------------

def test_same_tier_peer_is_used_without_a_downgrade():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b"), "m": _onprem("m", tier="mid")})
    backend = _FailForModels(S, {"a"})
    _result, records = _gw_run(reg, backend, _ctx(reg, {"onprem"}, onprem=True), 0.1)
    assert backend.calls == ["a", "b"]
    assert all(r.tier_downgrade is None for r in records)


def test_upward_failover_on_tier_exhaustion_is_unchanged():
    # target cheap (= hard floor cheap), cheap fails -> mid, as before; going
    # UP never undercuts a floor and is not a downgrade.
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        exclude=frozenset({"a"}))
    assert d.chosen.name == "m" and d.tier_downgrade is None


# --- 4. escalation floor ---------------------------------------------------

def test_router_escalated_mid_failed_is_not_lowered_to_cheap():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    with pytest.raises(NoEligibleCandidate) as ei:
        Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        min_tier="mid", exclude=frozenset({"m"}))
    assert ei.value.below_floor == "mid"
    assert "escalation floor (mid)" in str(ei.value)


def test_gateway_escalation_whose_mid_fails_never_reserves_on_cheap():
    """cheap answers with low confidence -> escalate to mid -> mid fails. The
    old clamp re-served on cheap; now the escalation is skipped and the
    already-served answer stands -- cheap is called exactly once."""
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    backend = _FailForModels(S, {"m"})
    gw = Gateway(S, backend, Router(S, registry=reg), reg)
    result, records = asyncio.run(gw.run(
        _ctx(reg, {"onprem"}, onprem=True), [Message(role="user", content="hello")],
        20, 0.1, {"mock_confidence": 0.1}))
    assert backend.calls == ["a", "m"]
    assert records[-1].reason.startswith("escalation skipped")


# --- 5. onprem_only + high risk ---------------------------------------------

def test_onprem_only_high_risk_clamps_to_best_onprem_and_never_drops_below():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    ctx = _ctx(reg, {"onprem"}, onprem=True, risk="high")
    r = Router(S, registry=reg)
    d = r.select(ctx, 20, 0.1)
    assert d.chosen.name == "m" and d.tier_target == "mid"     # rule 8 clamp
    with pytest.raises(NoEligibleCandidate) as ei:
        r.select(ctx, 20, 0.1, exclude=frozenset({"m"}))
    assert ei.value.below_floor == "mid"


def test_gateway_onprem_only_high_risk_only_mid_fails_is_a_502():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    backend = _FailForModels(S, {"m"})
    with pytest.raises(BackendUnavailable) as ei:
        _gw_run(reg, backend, _ctx(reg, {"onprem"}, onprem=True, risk="high"), 0.1)
    assert backend.calls == ["m"]
    assert ei.value.no_failover is True
    assert not any(c.startswith(("azure", "bedrock")) for c in backend.calls)


def test_app_allowed_tiers_without_frontier_clamps_high_risk_as_today():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    ctx = _ctx(reg, _ONPREM_FRONTIER, risk="high").model_copy(update={
        "allowed_models": frozenset({"a", "m"}),
        "app_allowed_tiers": frozenset({"cheap", "mid"})})
    d = Router(S, registry=reg).select(ctx, 20, 0.1)
    assert d.chosen.name == "m" and d.tier_target == "mid"


# --- 6. breaker -------------------------------------------------------------

def test_breaker_open_complexity_raised_frontier_steps_down_to_closed_mid():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER), 20, 0.9,
                                        open_circuits=frozenset({FRONTIER}))
    assert d.chosen.name == "m"
    assert d.circuit_bypass is False
    assert d.tier_target == "frontier"
    assert d.tier_downgrade == {"from": "frontier", "to": "mid", "cause": "circuit_open"}
    assert any(r.name == FRONTIER and r.code == "circuit_open" for r in d.rejected)


def test_breaker_open_steps_down_one_tier_at_a_time_highest_first():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER), 20, 0.9,
                                        open_circuits=frozenset({FRONTIER, "m"}))
    assert d.chosen.name == "a"
    assert d.tier_downgrade == {"from": "frontier", "to": "cheap", "cause": "circuit_open"}
    assert d.circuit_bypass is False


def test_breaker_open_risk_floor_frontier_still_bypasses_within_frontier():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER, risk="high"), 20, 0.9,
                                        open_circuits=frozenset({FRONTIER}))
    assert d.chosen.name == FRONTIER
    assert d.circuit_bypass is True
    assert d.tier_downgrade is None


def test_breaker_open_with_every_lower_tier_open_bypasses_within_the_tier():
    reg = _reg({"m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER), 20, 0.9,
                                        open_circuits=frozenset({FRONTIER, "m"}))
    assert d.chosen.name == FRONTIER and d.circuit_bypass is True
    assert d.tier_downgrade is None


def test_breaker_never_steps_below_the_escalation_floor():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER), 20, 0.9,
                                        min_tier="mid",
                                        open_circuits=frozenset({FRONTIER, "m"}))
    # frontier open, mid open, cheap closed but below the escalation floor:
    # step down to mid is not possible (no closed mid), so bypass in frontier.
    assert d.chosen.name == FRONTIER and d.circuit_bypass is True
    assert d.tier_downgrade is None


def test_breaker_skips_an_over_budget_lower_tier():
    pricey = {**_onprem("m", tier="mid"), "price_input": 100000.0,
              "price_output": 100000.0}
    reg = _reg({"a": _onprem("a"), "m": pricey})
    d = Router(S, registry=reg).select(
        _ctx(reg, _ONPREM_FRONTIER, max_cost=1.0), 20, 0.9,
        open_circuits=frozenset({FRONTIER}))
    assert d.chosen.name == "a"
    assert d.tier_downgrade["to"] == "cheap"


def test_gateway_records_circuit_open_downgrade_on_the_trace():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})

    class _Open:
        def open_set(self):
            return frozenset({FRONTIER})

        def record_success(self, name):
            pass

        def record_failure(self, name):
            pass

    gw = Gateway(S, _FailForModels(S, set()), Router(S, registry=reg), reg,
                 breaker=_Open())
    _result, records = asyncio.run(gw.run(
        _ctx(reg, _ONPREM_FRONTIER), [Message(role="user", content="hello")],
        20, 0.9, {}))
    [served] = records
    assert served.tier_downgrade["cause"] == "circuit_open"
    f = trace_features([r.model_dump() for r in records], complexity_tier="frontier")
    assert f["route_reason"] == "failover"
    assert f["scoring"]["tier_downgrade"]["cause"] == "circuit_open"


# --- 8. B80 floor path ------------------------------------------------------

def test_floor_enforce_met_by_frontier_that_fails_is_not_served_below_hard_floor():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    quality = QualityResolver(S, registry=reg)
    quality.set_bounds({(FRONTIER, "claims"): (0.95, "set-1"),
                        ("a", "claims"): (0.95, "set-1"),
                        ("m", "claims"): (0.95, "set-1")})
    router = Router(S, registry=reg, quality_resolver=quality)
    ctx = _ctx(reg, _ONPREM_FRONTIER, risk="high", workload="claims",
               quality_floor=0.8, quality_floor_mode="enforce")
    d = router.select(ctx, 20, 0.1)
    assert d.chosen.name == FRONTIER and d.quality_floor["status"] == "met"
    with pytest.raises(NoEligibleCandidate) as ei:
        router.select(ctx, 20, 0.1, exclude=frozenset({FRONTIER}))
    assert ei.value.below_floor == "frontier"
    backend = _FailForModels(S, {FRONTIER})
    with pytest.raises(BackendUnavailable):
        _gw_run(reg, backend, ctx, 0.1, router=router)
    assert backend.calls == [FRONTIER]


# --- streaming: same router decision, same record --------------------------

def test_run_stream_carries_tier_downgrade_like_run():
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})

    class _Open:
        def open_set(self):
            return frozenset({FRONTIER})

        def record_success(self, name):
            pass

        def record_failure(self, name):
            pass

    gw = Gateway(S, _DeterministicLiteLLM(S), Router(S, registry=reg), reg,
                 breaker=_Open())
    _it, records = asyncio.run(gw.run_stream(
        _ctx(reg, _ONPREM_FRONTIER), [Message(role="user", content="hello")],
        20, 0.9, {}))
    assert records[0].chosen == "m"
    assert records[0].tier_downgrade == {"from": "frontier", "to": "mid",
                                         "cause": "circuit_open"}


def test_circuit_step_down_after_upward_failover_records_the_targeted_tier():
    """Review fix (minor 1): target mid (complexity), mid failed -> upward to
    frontier, frontier open -> step down to cheap. `from` is the tier
    targeting picked (mid), never the upward-failover tier."""
    reg = _reg({"a": _onprem("a"), "m": _onprem("m", tier="mid")})
    d = Router(S, registry=reg).select(_ctx(reg, _ONPREM_FRONTIER), 20, 0.5,
                                        exclude=frozenset({"m"}),
                                        open_circuits=frozenset({FRONTIER}))
    assert d.chosen.name == "a"
    assert d.tier_downgrade == {"from": "mid", "to": "cheap", "cause": "circuit_open"}


def test_no_failover_502_names_the_floor_once():
    reg = _reg({"a": _onprem("a")})
    backend = _FailForModels(S, {FRONTIER})
    with pytest.raises(BackendUnavailable) as ei:
        _gw_run(reg, backend, _ctx(reg, _ONPREM_FRONTIER, risk="high"), 0.1)
    assert str(ei.value).count("no failover candidate") == 1
    assert "the only eligible endpoint at or above that floor failed" in str(ei.value)
