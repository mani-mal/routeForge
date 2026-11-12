"""B80 Task 6: the router's qualify-then-cheapest selection under a quality
floor (spec §4.4). The pool is the test registry's: local-8b (onprem, cheap),
local-32b (onprem, mid), azure-gpt-mid (azure, mid), bedrock-frontier
(bedrock, frontier) -- cheapest to dearest in that order."""
from __future__ import annotations

import pytest

from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext
from app.routing.floor_select import FloorPick, qualify
from app.routing.quality import QualityResolver
from app.routing.router import Router
from tests._litellm_fake import make_test_registry
from tests.test_router import S

ALL = frozenset({"onprem", "azure", "bedrock"})


class _Lowers:
    """A real QualityResolver for the ranking term, with a stubbed
    calibrated lower bound per model (workload-independent)."""

    def __init__(self, real: QualityResolver, lowers: dict[str, float | None]) -> None:
        self._real = real
        self._lowers = lowers
        self.calls: list[tuple[str, str | None]] = []

    def resolve_lower(self, model, workload):
        self.calls.append((model, workload))
        v = self._lowers.get(model)
        return (v, "calibrated") if v is not None else (None, None)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _router(lowers: dict[str, float | None], *, windows: dict[str, int] | None = None):
    reg = make_test_registry(S)
    if windows:
        base = reg.effective_pool
        reg.effective_pool = lambda: [
            c.model_copy(update={"context_window": windows[c.name]})
            if c.name in windows else c for c in base()]
    q = _Lowers(QualityResolver(S, registry=reg), lowers)
    return Router(S, registry=reg, quality_resolver=q), reg


def _ctx(reg, providers=ALL, *, risk="low", onprem=False, floor=0.8,
         mode="enforce", workload="claims", max_cost=0.05):
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in reg.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None,
        workload=workload, quality_floor=floor, quality_floor_mode=mode)


def _off(ctx):
    return ctx.model_copy(update={"quality_floor": None, "quality_floor_mode": "off"})


def _rej(d):
    return {r.name: (r.reason, r.code) for r in d.rejected}


def _same(a, b):
    assert a.chosen.name == b.chosen.name
    assert a.tier_target == b.tier_target
    assert a.est_cost_usd == b.est_cost_usd
    assert [r.model_dump() for r in a.rejected] == [r.model_dump() for r in b.rejected]


# --- the pure function ------------------------------------------------------

def _pc(name, tier):
    from app.config.schema import PoolCandidate
    return PoolCandidate(name=name, provider="onprem", tier=tier)


def test_qualify_orders_by_cost_and_names_each_first_failed_filter():
    allowed = [_pc("a", "cheap"), _pc("b", "mid"), _pc("c", "mid"),
               _pc("d", "frontier"), _pc("e", "mid")]
    cost = {"a": 0.001, "b": 0.003, "c": 0.002, "d": 0.5, "e": 0.004}
    lowers = {"a": 0.99, "b": 0.9, "c": 0.9, "d": 0.95, "e": None}
    pick = qualify(allowed, floor=0.8, workload="w", hard_tier="mid",
                   prompt_tokens=10, output_tokens=10, max_cost_usd=0.1,
                   est_cost=lambda c: cost[c.name],
                   lower=lambda n: (lowers[n], "calibrated" if lowers[n] else None))
    assert isinstance(pick, FloorPick)
    assert pick.status == "met"
    assert [(c.name, e) for c, e in pick.qualifiers] == [("c", 0.002), ("b", 0.003)]
    assert pick.rejections == {"a": "below risk floor", "d": "over budget",
                               "e": "below quality floor"}
    assert pick.bounds == {"a": (0.99, "calibrated"), "b": (0.9, "calibrated"),
                           "c": (0.9, "calibrated"), "d": (0.95, "calibrated"),
                           "e": (None, None)}


@pytest.mark.parametrize("window,cap,lower,status", [
    (None, 0.1, 0.5, "unmet_no_qualifier"),
    (5, 0.1, 0.9, "unmet_context"),
    (None, 0.0001, 0.9, "unmet_budget"),
    (20, 0.1, 0.9, "met"),            # exactly prompt + output fits
])
def test_qualify_status_is_the_first_filter_that_empties_the_set(window, cap, lower, status):
    from app.config.schema import PoolCandidate
    c = PoolCandidate(name="m", provider="onprem", tier="cheap", context_window=window)
    pick = qualify([c], floor=0.8, workload="w", hard_tier="cheap",
                   prompt_tokens=10, output_tokens=10, max_cost_usd=cap,
                   est_cost=lambda _c: 0.001, lower=lambda _n: (lower, "calibrated"))
    assert pick.status == status


# --- the router -------------------------------------------------------------

def test_cheapest_qualifier_wins_across_tiers():
    r, reg = _router({"local-8b": 0.85, "local-32b": 0.95})
    # complexity 0.9 alone would target frontier
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.9)
    assert d.chosen.name == "local-8b"
    assert d.tier_target == "cheap"
    assert d.quality_floor["status"] == "met"
    assert d.reason.startswith("quality floor 0.80 (workload claims): cheapest qualifying, "
                               "lower bound 0.85 (calibrated)")
    rej = _rej(d)
    assert rej["local-32b"] == ("higher cost", "higher_cost")
    assert rej["azure-gpt-mid"] == ("below quality floor", "quality_floor")
    assert d.quality_floor["chosen_lower"] == 0.85
    assert d.quality_floor["chosen_lower_source"] == "calibrated"
    # the bound was resolved for the request's own workload
    assert ("local-8b", "claims") in r._quality.calls


def test_unknown_bound_never_qualifies():
    r, reg = _router({"local-8b": None, "local-32b": 0.9})
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1)
    assert d.chosen.name == "local-32b"
    assert _rej(d)["local-8b"] == ("below quality floor", "quality_floor")
    assert d.quality_floor["bounds"]["local-8b"] == [None, None]

    r, reg = _router({})
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1)
    assert d.quality_floor["status"] == "unmet_no_qualifier"
    assert d.chosen.name == "local-8b"   # today's path


def test_onprem_only_never_picks_cloud_qualifier():
    r, reg = _router({"azure-gpt-mid": 0.95})
    # control: with cloud permitted, the cloud model is the (only, so cheapest) qualifier
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1)
    assert d.chosen.name == "azure-gpt-mid"
    d = r.select(_ctx(reg, {"onprem"}, onprem=True), tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert d.quality_floor["status"] == "unmet_no_qualifier"
    assert "azure-gpt-mid" not in d.quality_floor["bounds"]


def test_high_risk_onprem_only_floors_to_best_onprem_tier():
    """Rule 8: the hard floor clamps to the best allowed tier; never lifts to cloud."""
    r, reg = _router({"local-8b": 0.9, "local-32b": 0.9, "bedrock-frontier": 0.99})
    d = r.select(_ctx(reg, {"onprem"}, onprem=True, risk="high"),
                 tokens_after=20, complexity=0.1)
    assert d.chosen.name == "local-32b"
    assert d.quality_floor["status"] == "met"
    assert _rej(d)["local-8b"] == ("below risk floor", None)


def test_high_risk_floor_excludes_lower_tiers():
    r, reg = _router({"local-8b": 0.9, "local-32b": 0.9, "azure-gpt-mid": 0.9,
                      "bedrock-frontier": 0.9})
    d = r.select(_ctx(reg, risk="high"), tokens_after=20, complexity=0.1)
    assert d.chosen.tier == "frontier"
    assert d.quality_floor["status"] == "met"
    rej = _rej(d)
    for name in ("local-8b", "local-32b", "azure-gpt-mid"):
        assert rej[name] == ("below risk floor", None)


def test_context_window_too_small_disqualifies():
    r, reg = _router({"local-8b": 0.9, "local-32b": 0.9}, windows={"local-8b": 100})
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1, output_tokens=512)
    assert d.chosen.name == "local-32b"
    assert _rej(d)["local-8b"] == ("context window too small", "context_window")


def test_no_declared_window_qualifies():
    r, reg = _router({"local-8b": 0.9})
    assert all(c.context_window is None for c in reg.effective_pool())
    d =r.select(_ctx(reg), tokens_after=20, complexity=0.9, output_tokens=512)
    assert d.chosen.name == "local-8b"
    assert d.quality_floor["status"] == "met"


def test_all_too_small_is_unmet_context_and_falls_back():
    names = ("local-8b", "local-32b", "azure-gpt-mid", "bedrock-frontier")
    r, reg = _router({n: 0.9 for n in names}, windows={n: 100 for n in names})
    ctx = _ctx(reg)
    d = r.select(ctx, tokens_after=20, complexity=0.5, output_tokens=512)
    off = r.select(_off(ctx), tokens_after=20, complexity=0.5, output_tokens=512)
    assert d.quality_floor["status"] == "unmet_context"
    _same(d, off)
    assert d.reason == ("quality floor 0.80 unmet (context window too small): "
                        + off.reason)
    assert off.quality_floor is None


def test_over_budget_qualifier_skipped_then_unmet_budget():
    r, reg = _router({"local-32b": 0.9, "bedrock-frontier": 0.9})
    d = r.select(_ctx(reg, max_cost=1e-4), tokens_after=20, complexity=0.1,
                 output_tokens=512)
    assert d.chosen.name == "local-32b"
    assert _rej(d)["bedrock-frontier"] == ("over budget", None)

    r, reg = _router({"bedrock-frontier": 0.9})
    ctx = _ctx(reg, max_cost=1e-4)
    d = r.select(ctx, tokens_after=20, complexity=0.1, output_tokens=512)
    off = r.select(_off(ctx), tokens_after=20, complexity=0.1, output_tokens=512)
    assert d.quality_floor["status"] == "unmet_budget"
    _same(d, off)
    assert d.reason == "quality floor 0.80 unmet (over budget): " + off.reason


def test_failover_exclude_picks_next_cheapest_qualifier():
    """Review Focus 2: a failed qualifier is replaced by the next-cheapest
    qualifier, never by a non-qualifier while one remains."""
    r, reg = _router({"local-32b": 0.9, "azure-gpt-mid": 0.9, "bedrock-frontier": 0.9})
    first = r.select(_ctx(reg), tokens_after=20, complexity=0.1)
    assert first.chosen.name == "local-32b"
    second = r.select(_ctx(reg), tokens_after=20, complexity=0.1,
                      exclude=frozenset({"local-32b"}))
    # local-8b is cheaper but does not qualify
    assert second.chosen.name == "azure-gpt-mid"
    assert second.quality_floor["status"] == "met"
    third = r.select(_ctx(reg), tokens_after=20, complexity=0.1,
                     exclude=frozenset({"local-32b", "azure-gpt-mid"}))
    assert third.chosen.name == "bedrock-frontier"


def test_open_circuit_qualifier_dropped_and_all_open_bypass_within_qualifiers():
    r, reg = _router({"local-8b": 0.9, "local-32b": 0.9})
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1,
                 open_circuits=frozenset({"local-8b"}))
    assert d.chosen.name == "local-32b"
    assert _rej(d)["local-8b"][1] == "circuit_open"
    assert not d.circuit_bypass

    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1,
                 open_circuits=frozenset({"local-8b", "local-32b"}))
    assert d.chosen.name == "local-8b"     # bypass never leaves the qualifiers
    assert d.circuit_bypass


def test_escalation_min_tier_raises_hard_floor():
    r, reg = _router({"local-8b": 0.9, "local-32b": 0.9, "azure-gpt-mid": 0.9})
    d = r.select(_ctx(reg), tokens_after=20, complexity=0.1, min_tier="mid")
    assert d.chosen.name == "local-32b"
    assert _rej(d)["local-8b"] == ("below risk floor", None)


def test_shadow_decision_equals_off_and_records_would_be_choice():
    r, reg = _router({"local-8b": 0.9})
    ctx = _ctx(reg, mode="shadow")
    d = r.select(ctx, tokens_after=20, complexity=0.9)
    off = r.select(_off(ctx), tokens_after=20, complexity=0.9)
    _same(d, off)
    assert d.reason == off.reason
    assert d.chosen.name == "bedrock-frontier"
    qf = d.quality_floor
    assert qf["status"] == "shadow_met"
    assert qf["mode"] == "shadow"
    assert qf["shadow_choice"] == "local-8b"
    assert qf["shadow_est_cost_usd"] == pytest.approx(r._est_cost(
        next(c for c in reg.effective_pool() if c.name == "local-8b"), 20, 512))
    assert qf["chosen_lower"] is None   # the actually chosen model's bound

    r, reg = _router({})
    d = r.select(_ctx(reg, mode="shadow"), tokens_after=20, complexity=0.9)
    assert d.quality_floor["status"] == "shadow_unmet_no_qualifier"
    assert d.quality_floor["shadow_choice"] is None


def test_scoring_record_carries_floor_and_all_bounds():
    r, reg = _router({"local-8b": 0.9, "local-32b": 0.95})
    ctx = _ctx(reg)
    d = r.select(ctx, tokens_after=20, complexity=0.1)
    rec = d.scoring_record()
    assert rec["candidates"]            # strategy.rank output, unchanged
    qf = rec["quality_floor"]
    assert set(qf) == {"floor", "mode", "workload", "status", "chosen_lower",
                       "chosen_lower_source", "bounds", "shadow_choice",
                       "shadow_est_cost_usd"}
    assert set(qf["bounds"]) == {c.name for c in reg.effective_pool()}
    assert qf["bounds"]["local-32b"] == [0.95, "calibrated"]
    assert (qf["floor"], qf["mode"], qf["workload"]) == (0.8, "enforce", "claims")

    off = r.select(_off(ctx), tokens_after=20, complexity=0.1)
    assert "quality_floor" not in off.scoring_record()
    assert off.scoring_record() == off.scoring.to_record()


def test_unmet_budget_with_no_fallback_raises_like_off():
    r, reg = _router({"bedrock-frontier": 0.9})
    ctx = _ctx(reg, max_cost=1e-9)
    with pytest.raises(NoEligibleCandidate) as on:
        r.select(ctx, tokens_after=20, complexity=0.1)
    with pytest.raises(NoEligibleCandidate) as off:
        r.select(_off(ctx), tokens_after=20, complexity=0.1)
    assert str(on.value) == str(off.value)
