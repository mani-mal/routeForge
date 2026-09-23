"""`Router.select(open_circuits=...)` -- the step-7 breaker's name-set plumbing
into eligibility (`app/policy/feasible.py`'s `circuit_open` stage) and the
all-open bypass.

This task does NOT use `BreakerCfg`/`CircuitBreaker` (app/gateway/breaker.py,
Tasks 1-2) -- it only adds the router-side name-set param and the bypass.
Task 4 wires the gateway to pass `breaker.open_set()` in.

Helpers below are copied from `tests/test_gateway_lateral_failover.py` per
the task brief -- this file does not import from another test module.
"""
import asyncio

import pytest

from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.apps import store
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext
from app.routing.router import Router

_S0 = load_settings()
_prov = dict(_S0.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S = _S0.model_copy(update={"serving": _S0.serving.model_copy(update={"providers": _prov})})


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
         max_cost: float = 0.05) -> ExecutionPolicyContext:
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in reg.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def test_open_circuit_is_skipped_for_a_peer():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"a"}))
    assert d.chosen.name == "b"
    assert d.circuit_bypass is False
    assert any(r.name == "a" and r.code == "circuit_open" for r in d.rejected)


def test_all_open_bypasses_and_marks_it():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"a", "b"}))
    assert d.chosen.name in {"a", "b"}
    assert d.circuit_bypass is True
    assert "circuit bypass" in d.reason


def test_bypass_never_widens_placement():
    # every on-prem model open, cloud enabled in the pool, onprem_only ctx
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"a", "b"}))
    assert d.chosen.provider == "onprem"


def test_no_eligible_for_policy_reasons_still_raises():
    # nothing on-prem onboarded, onprem_only: the empty set is policy, not breaker
    reg = _reg({})
    with pytest.raises(NoEligibleCandidate):
        Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"x"}))


def test_prior_failure_plus_open_circuits_bypass_keeps_prior_failure():
    # a failed on this request, b open: the bypass restores b, never a
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        exclude=frozenset({"a"}),
                                        open_circuits=frozenset({"b"}))
    assert d.chosen.name == "b" and d.circuit_bypass is True
