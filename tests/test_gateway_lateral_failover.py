"""Failure-driven lateral model switch (feature #2/7).

On a retryable backend failure the gateway must first try another healthy model
in the SAME tier (lateral switch) via an exclusion set on the router, and only
escalate to a higher tier when the current tier is exhausted. Bounded by
routing.max_failover_attempts; the residency invariant stays absolute (an
onprem_only request never reaches a cloud provider through this path)."""
import asyncio

import pytest

from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.apps import store
from app.config.loader import load_settings
from app.errors import BackendUnavailable
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, Message
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM

# Enable both cloud providers so their static-pool candidates are actually
# present in the effective pool -- the residency test then proves they are
# excluded by residency, not merely absent.
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


class _FailForModels(_DeterministicLiteLLM):
    """Deterministic backend that raises for any model whose name is in
    `fail_models`, and records every model it was asked to generate for so a
    test can assert which candidates were actually reached (e.g. cloud never)."""

    def __init__(self, settings, fail_models: set[str]) -> None:
        super().__init__(settings)
        self._fail = set(fail_models)
        self.calls: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(model)
        if model in self._fail:
            raise RuntimeError(f"forced failure for {model}")
        return await super().generate(model, messages, price, tier, {})


def _ctx(reg, providers, *, onprem: bool = False, risk: str = "low",
         max_cost: float = 0.05) -> ExecutionPolicyContext:
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        # The eligible set these tests have always assumed: an app that narrows
        # nothing, so every pool candidate whose provider is permitted is a
        # candidate -- what `build_context` computes for such an app. Stated
        # because `ctx.allowed_models` is binding on the router, with no flag
        # that can turn it off (app/policy/feasible.py). The failover exclusion
        # set is applied by the router on top of it, not folded in here.
        allowed_models=frozenset(c.name for c in reg.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _run(backend, ctx, reg, complexity=0.1):
    gw = Gateway(S, backend, Router(S, registry=reg), reg)
    msgs = [Message(role="user", content="hello")]
    return asyncio.run(gw.run(ctx, msgs, 20, complexity, {}))


# --- Invariant 1: residency absolute --------------------------------------

def test_onprem_only_lateral_peer_chosen_cloud_never_called():
    # onprem cheap peer A fails; onprem cheap peer B is healthy; cloud models
    # exist in the pool but the ctx is onprem_only. B must be served and no
    # cloud model may ever be called.
    reg = _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b")})
    backend = _FailForModels(S, {"onprem-a"})
    result, records = _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    chosen = next(r for r in records if r.step == "router")
    assert chosen.chosen == "onprem-b"
    assert chosen.chosen_provider == "onprem"
    assert all(m in {"onprem-a", "onprem-b"} for m in backend.calls)
    assert not any(m.startswith("azure") or m.startswith("bedrock")
                   for m in backend.calls)


def test_onprem_only_all_peers_fail_raises_never_cloud():
    # Every reachable on-prem model fails. The gateway must raise
    # BackendUnavailable rather than fall through to an available cloud model.
    reg = _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b")})
    backend = _FailForModels(S, {"onprem-a", "onprem-b"})
    with pytest.raises(BackendUnavailable):
        _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    assert set(backend.calls) <= {"onprem-a", "onprem-b"}
    assert not any(m.startswith("azure") or m.startswith("bedrock")
                   for m in backend.calls)


# --- Invariant 2: lateral before escalation -------------------------------

def test_lateral_switch_stays_in_tier():
    # cheap A fails, cheap peer B healthy -> B used, tier stays cheap (no jump
    # to mid even though a mid on-prem model is available).
    reg = _reg({"onprem-a": _onprem("onprem-a"),
                "onprem-b": _onprem("onprem-b"),
                "onprem-mid": _onprem("onprem-mid", tier="mid")})
    backend = _FailForModels(S, {"onprem-a"})
    result, records = _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    chosen = next(r for r in records if r.step == "router")
    assert chosen.chosen == "onprem-b"
    assert chosen.chosen_tier == "cheap"
    assert "onprem-mid" not in backend.calls


# --- Invariant 3: escalation on tier exhaustion ---------------------------

def test_escalates_to_next_tier_when_cheap_tier_exhausted():
    # both cheap models fail -> cheap tier empties from the exclude set -> the
    # router escalates to the mid tier on its own (unified mechanism).
    reg = _reg({"onprem-a": _onprem("onprem-a"),
                "onprem-b": _onprem("onprem-b"),
                "onprem-mid": _onprem("onprem-mid", tier="mid")})
    backend = _FailForModels(S, {"onprem-a", "onprem-b"})
    result, records = _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    chosen = next(r for r in records if r.step == "router")
    assert chosen.chosen == "onprem-mid"
    assert chosen.chosen_tier == "mid"


# --- Invariant 4: bounded -------------------------------------------------

def test_bounded_by_max_failover_attempts():
    # max_failover_attempts=2: first attempt + at most 2 retries = 3 backend
    # calls before BackendUnavailable. Three failing models guarantees the
    # bound (not candidate exhaustion) is what stops the loop.
    reg = _reg({"onprem-a": _onprem("onprem-a"),
                "onprem-b": _onprem("onprem-b"),
                "onprem-mid": _onprem("onprem-mid", tier="mid")})
    backend = _FailForModels(S, {"onprem-a", "onprem-b", "onprem-mid"})
    assert S.routing.max_failover_attempts == 2
    with pytest.raises(BackendUnavailable):
        _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    assert len(backend.calls) == 3


def test_zero_max_failover_makes_single_failure_terminal():
    # max_failover_attempts=0 disables lateral retry: one failure is terminal.
    s = S.model_copy(update={"routing": S.routing.model_copy(
        update={"max_failover_attempts": 0})})
    reg = _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b")})
    backend = _FailForModels(s, {"onprem-a"})
    gw = Gateway(s, backend, Router(s, registry=reg), reg)
    msgs = [Message(role="user", content="hello")]
    with pytest.raises(BackendUnavailable):
        asyncio.run(gw.run(_ctx(reg, {"onprem"}, onprem=True), msgs, 20, 0.1, {}))
    assert len(backend.calls) == 1


# --- Invariant 5: fail-closed ---------------------------------------------

def test_everything_failing_raises_backend_unavailable():
    reg = _reg({"onprem-a": _onprem("onprem-a")})
    backend = _FailForModels(S, {"onprem-a"})
    with pytest.raises(BackendUnavailable):
        _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)


# --- Invariant 6: trace completeness --------------------------------------

def test_each_failed_attempt_emits_its_own_decision_record():
    reg = _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b")})
    backend = _FailForModels(S, {"onprem-a"})
    result, records = _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    failures = [r for r in records if r.step == "gateway" and r.chosen is None]
    assert len(failures) == 1
    assert "failed" in failures[0].reason
    # the failure record still carries the rejected-candidate set for the trace
    assert failures[0].est_cost_usd >= 0.0


def test_failed_attempt_carries_scoring_too_not_just_the_survivor():
    """P2a: the FAILED hop's own scoring must not be discarded. No
    step="router" record is ever emitted for a candidate that failed, so if
    its scoring lived only on that never-created record, a replay could never
    ask whether the failed choice was reasonable given what was known at the
    time -- and failover is exactly where the availability signal matters.
    Both the failed gateway-step record and the survivor's router-step record
    must carry a scoring dict, and they must disagree about which candidate
    ranked first (the failed hop's excluded onprem-a, the survivor's
    onprem-b)."""
    reg = _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b")})
    backend = _FailForModels(S, {"onprem-a"})
    result, records = _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    scored = [r for r in records if r.scoring is not None]
    assert len(scored) == 2
    failed, served = scored
    assert failed.step == "gateway" and failed.chosen is None
    assert served.step == "router" and served.chosen == "onprem-b"
    failed_top = failed.scoring["candidates"][0]["name"]
    served_top = served.scoring["candidates"][0]["name"]
    assert failed_top == "onprem-a"
    assert served_top == "onprem-b"
    assert failed_top != served_top


# --- Invariant 7: default-safe (success on first try, no exclusion) --------

def test_success_first_try_no_failover_records():
    reg = _reg({"onprem-a": _onprem("onprem-a"), "onprem-b": _onprem("onprem-b")})
    backend = _FailForModels(S, set())
    result, records = _run(backend, _ctx(reg, {"onprem"}, onprem=True), reg)
    assert len(backend.calls) == 1
    assert not any(r.step == "gateway" and r.chosen is None for r in records)
