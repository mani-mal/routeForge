"""B108: the Switchyard advisor seam in `Router.select` (`advisor_tier`).

Design docs/architecture/switchyard-advisor-design.md §2: the advisor's tier
replaces exactly ONE input -- the length-derived `complexity_tier(complexity)`
-- and nothing else. These tests pin that from both sides:

* **Property (CLAUDE.md rules 3, 8, 2a).** For random pools, placements
  (including `onprem_only`), risk floors, escalation `min_tier`s, allowed
  provider sets, budgets, failover exclusions and open circuits, and EVERY
  `advisor_tier` (each canonical tier, garbage, None), `select`:
  - never returns a candidate outside the policy-eligible set minus
    `exclude` (and so never one policy filtered out);
  - never reaches a non-on-prem candidate under `onprem_only`;
  - never serves below the hard floor `max(risk floor, min_tier)` clamped to
    the best POLICY-eligible tier;
  - leaves the context -- every placement and protection field -- untouched.
* **Identity.** `advisor_tier=None`, an advisor tier outside the canonical
  tiers, an advisor tier EQUAL to the length-derived tier, and any advisor
  tier under an enforced B80 quality floor all give byte-identical decisions
  to the call without the argument.
* **Reachability.** Deterministic cases where the advisor really moves the
  target, so the property cannot pass vacuously.
"""
from __future__ import annotations

import logging

from hypothesis import (HealthCheck, event, given, settings as hyp_settings,
                        strategies as st, target)

from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext
from app.policy.feasible import feasible_set
from app.routing.floor_select import _TIER_ORDER
from app.routing.router import Router, complexity_tier
from tests.routing.test_floor_identity import (_AllQualify, _PROVIDERS, _TIERS, _model,
                                               _outcome, _settings)

_ADVISOR = st.sampled_from(["cheap", "mid", "frontier", None, "strong", "", 3])


def _ctx(names, providers, onprem, risk, max_cost, *, floor=None, floor_mode="off",
         workload=None) -> ExecutionPolicyContext:
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(names),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=onprem, detected_entities=(), redaction_map_id=None,
        workload=workload, quality_floor=floor, quality_floor_mode=floor_mode)


def _check(rows, providers, onprem, risk, max_cost, complexity, tokens, min_tier,
           exclude, circuits, advisor, floor_mode="off"):
    s = _settings(rows)
    names = [f"m{i}" for i in range(len(rows))]
    ctx = _ctx(names, providers, onprem, risk, max_cost,
               floor=0.5 if floor_mode != "off" else None, floor_mode=floor_mode,
               workload="claims" if floor_mode != "off" else None)
    before = ctx.model_dump()
    router = Router(s)
    try:
        d = router.select(ctx, tokens_after=tokens, complexity=complexity,
                          min_tier=min_tier, exclude=exclude, open_circuits=circuits,
                          advisor_tier=advisor)
    except NoEligibleCandidate:
        assert ctx.model_dump() == before
        return None
    # Rule 2a: the advisor reads and writes no placement/protection value.
    assert ctx.model_dump() == before
    policy = feasible_set(s, None, ctx, tenant_id=ctx.tenant_id).eligible
    survivors = {c.name for c in feasible_set(s, None, ctx, tenant_id=ctx.tenant_id,
                                              exclude=exclude).eligible}
    # Rule 3: only a policy-eligible candidate that survived failover.
    assert d.chosen.name in survivors
    assert d.chosen.name not in exclude
    assert d.chosen.provider in ctx.allowed_providers
    if onprem:
        assert d.chosen.provider == "onprem"
    # Rule 8: never below the hard floor (clamped only by POLICY).
    hard = max([s.routing.risk_floors[risk]] + ([min_tier] if min_tier else []),
               key=lambda t: _TIER_ORDER[t])
    best = max(_TIER_ORDER[c.tier] for c in policy)
    assert _TIER_ORDER[d.chosen.tier] >= min(_TIER_ORDER[hard], best)
    if d.tier_downgrade is not None:
        assert _TIER_ORDER[d.tier_downgrade["to"]] >= min(_TIER_ORDER[hard], best)
    return d


@hyp_settings(max_examples=400, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(rows=st.lists(_model, min_size=1, max_size=6),
       providers=st.sets(st.sampled_from(_PROVIDERS), min_size=1),
       onprem=st.booleans(),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-6, 1e-4, 0.05, 1.0, 1.0, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       tokens=st.integers(min_value=0, max_value=3000),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       advisor=_ADVISOR,
       floor_mode=st.sampled_from(["off", "off", "shadow", "enforce"]),
       data=st.data())
def test_advisor_never_escapes_policy_or_the_hard_floor(
        rows, providers, onprem, risk, max_cost, complexity, tokens, min_tier,
        advisor, floor_mode, data):
    names = [f"m{i}" for i in range(len(rows))]
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
    d = _check(rows, providers, onprem, risk, max_cost, complexity, tokens,
               min_tier, exclude, circuits, advisor, floor_mode)
    event(f"advisor={advisor!r} onprem={onprem} served={'none' if d is None else d.chosen.tier}")
    target(1.0 if (d is not None and onprem and advisor == "frontier") else 0.0,
           label="onprem_only_capable")


_BASE = [("cheap", "onprem", 0.1, 0.1, None), ("mid", "onprem", 0.5, 0.5, None),
         ("frontier", "azure", 1.0, 1.0, None), ("frontier", "bedrock", 1.0, 1.0, None)]


@hyp_settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(extra=st.lists(_model, max_size=3),
       onprem=st.booleans(),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-4, 1.0, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       advisor=_ADVISOR,
       data=st.data())
def test_failover_and_open_circuits_with_every_advisor_tier(
        extra, onprem, risk, max_cost, complexity, min_tier, advisor, data):
    """A pool with every tier on-prem and in the cloud, so random failure
    exclusions and open circuits really exercise upward failover, both
    step-down causes and the below-floor refusal under each advisor tier."""
    rows = _BASE + extra
    names = [f"m{i}" for i in range(len(rows))]
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
    d = _check(rows, set(_PROVIDERS), onprem, risk, max_cost, complexity, 100,
               min_tier, exclude, circuits, advisor)
    if d is not None and d.tier_downgrade:
        event(f"downgrade {d.tier_downgrade['cause']}")
    target(1.0 if (d is not None and d.tier_downgrade) else 0.0, label="downgrade")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
@hyp_settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(rows=st.lists(_model, min_size=1, max_size=6),
       providers=st.sets(st.sampled_from(_PROVIDERS), min_size=1),
       onprem=st.booleans(),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-6, 1e-4, 1e-3, 0.05, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       tokens=st.integers(min_value=0, max_value=3000),
       out=st.one_of(st.none(), st.integers(min_value=1, max_value=2000)),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       garbage=st.sampled_from(["strong", "", "CHEAP", 0, None]),
       data=st.data())
def test_identity_none_invalid_and_equal_to_the_length_tier(
        rows, providers, onprem, risk, max_cost, complexity, tokens, out,
        min_tier, garbage, data):
    s = _settings(rows)
    names = [f"m{i}" for i in range(len(rows))]
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
    ctx = _ctx(names, providers, onprem, risk, max_cost)
    router = Router(s)
    kw = dict(tokens_after=tokens, complexity=complexity, min_tier=min_tier,
              exclude=exclude, output_tokens=out, open_circuits=circuits)
    today = _outcome(router, ctx, kw)
    assert _outcome(router, ctx, {**kw, "advisor_tier": None}) == today
    assert _outcome(router, ctx, {**kw, "advisor_tier": garbage}) == today
    # The advisor replaces exactly the length-derived element: naming the
    # same tier changes nothing at all.
    same = complexity_tier(complexity, s.routing.complexity_thresholds)
    assert _outcome(router, ctx, {**kw, "advisor_tier": same}) == today


@hyp_settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(rows=st.lists(_model, min_size=1, max_size=6),
       providers=st.sets(st.sampled_from(_PROVIDERS), min_size=1),
       onprem=st.booleans(),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-4, 0.05, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       advisor=st.sampled_from(_TIERS),
       data=st.data())
def test_enforced_quality_floor_ignores_the_advisor(
        rows, providers, onprem, risk, max_cost, complexity, min_tier, advisor, data):
    """B80 keeps precedence: with a live floor in `enforce`, the decision is
    the same whatever the advisor says -- met or unmet."""
    s = _settings(rows)
    names = [f"m{i}" for i in range(len(rows))]
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    floor = data.draw(st.sampled_from([0.5, 0.999]))
    ctx = _ctx(names, providers, onprem, risk, max_cost, floor=floor,
               floor_mode="enforce", workload="claims")
    router = Router(s, quality_resolver=_AllQualify(Router(s)._quality))
    kw = dict(tokens_after=100, complexity=complexity, min_tier=min_tier,
              exclude=exclude)
    assert (_outcome(router, ctx, {**kw, "advisor_tier": advisor})
            == _outcome(router, ctx, kw))


# --------------------------------------------------------------------------- #
# Reachability: the advisor really moves the target
# --------------------------------------------------------------------------- #
def _router_and_ctx(*, onprem=False, risk="low", providers=("onprem", "azure", "bedrock"),
                    floor=None, floor_mode="off"):
    s = _settings(_BASE)
    names = [f"m{i}" for i in range(len(_BASE))]
    return Router(s), _ctx(names, providers, onprem, risk, 1.0, floor=floor,
                           floor_mode=floor_mode,
                           workload="claims" if floor else None)


def test_efficient_lowers_a_long_low_risk_request_to_cheap():
    router, ctx = _router_and_ctx()
    assert router.select(ctx, 100, complexity=0.95).chosen.tier == "frontier"
    d = router.select(ctx, 100, complexity=0.95, advisor_tier="cheap")
    assert d.chosen.tier == "cheap" and d.tier_target == "cheap"


def test_capable_raises_a_short_request():
    router, ctx = _router_and_ctx()
    assert router.select(ctx, 100, complexity=0.0).chosen.tier == "cheap"
    assert router.select(ctx, 100, complexity=0.0,
                         advisor_tier="frontier").chosen.tier == "frontier"
    assert router.select(ctx, 100, complexity=0.0,
                         advisor_tier="mid").chosen.tier == "mid"


def test_efficient_on_a_high_risk_request_stays_at_the_floor():
    router, ctx = _router_and_ctx(risk="high")
    d = router.select(ctx, 100, complexity=0.0, advisor_tier="cheap")
    assert d.chosen.tier == "frontier"


def test_escalation_min_tier_dominates_the_advisor():
    router, ctx = _router_and_ctx()
    d = router.select(ctx, 100, complexity=0.0, min_tier="mid", advisor_tier="cheap")
    assert d.chosen.tier == "mid"


def test_capable_under_onprem_only_clamps_to_the_best_onprem_tier():
    router, ctx = _router_and_ctx(onprem=True, risk="high")
    d = router.select(ctx, 100, complexity=0.0, advisor_tier="frontier")
    assert d.chosen.provider == "onprem" and d.chosen.tier == "mid"
    # ...and through failover: the on-prem mid failed -> never cloud. The hard
    # floor (high risk -> frontier, clamped by policy to on-prem mid) refuses
    # a step down to cheap.
    try:
        d = router.select(ctx, 100, complexity=0.0, advisor_tier="frontier",
                          exclude=frozenset({"m1"}))
    except NoEligibleCandidate as exc:
        assert exc.below_floor == "mid"
    else:
        raise AssertionError(f"served {d.chosen.name} below the floor")


def test_advisor_raised_target_steps_down_like_a_complexity_raised_one():
    """The step-down treats an advisor-raised target as the soft element: the
    frontier target failed -> served at mid, recorded as a downgrade, never
    below the (low-risk, cheap) hard floor."""
    router, ctx = _router_and_ctx(providers=("onprem", "azure"))
    d = router.select(ctx, 100, complexity=0.0, advisor_tier="frontier",
                      exclude=frozenset({"m2"}))
    assert d.chosen.tier == "mid"
    assert d.tier_downgrade == {"from": "frontier", "to": "mid", "cause": "prior_failure"}
    d = router.select(ctx, 100, complexity=0.0, advisor_tier="frontier",
                      open_circuits=frozenset({"m2"}))
    assert d.chosen.tier == "mid"
    assert d.tier_downgrade["cause"] == "circuit_open"


def test_invalid_advisor_tier_is_logged_not_raised(caplog):
    router, ctx = _router_and_ctx()
    with caplog.at_level(logging.WARNING, logger="serving.routing"):
        d = router.select(ctx, 100, complexity=0.0, advisor_tier="strong")
    assert d.chosen.tier == "cheap"
    assert any("ignoring advisor_tier" in r.getMessage() for r in caplog.records)


def test_floor_shadow_still_takes_the_advisor():
    """In floor `shadow` the tiered path decides, so the seam applies."""
    router, ctx = _router_and_ctx(floor=0.5, floor_mode="shadow")
    assert router.select(ctx, 100, complexity=0.0,
                         advisor_tier="frontier").chosen.tier == "frontier"
