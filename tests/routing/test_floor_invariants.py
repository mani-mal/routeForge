"""B80 insurance on CLAUDE.md rules 3 and 8: whenever `Router.select` returns
-- under a quality floor (shadow or enforce) or on the plain tiered path (mode
off) -- the chosen candidate is in the feasible set (what survives `exclude`)
and is at or above the hard tier: max(risk floor, min_tier) clamped down to the
best tier in the POLICY-eligible set (B70: no failure exclusions, no circuits
-- a failure or an open circuit never lowers the floor)."""
from __future__ import annotations

from hypothesis import (HealthCheck, event, example, given, settings as hyp_settings,
                        strategies as st, target)

from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext
from app.policy.feasible import feasible_set
from app.routing.floor_select import _TIER_ORDER
from app.routing.quality import QualityResolver
from app.routing.router import Router
from tests.routing.test_floor_identity import _PROVIDERS, _TIERS, _model, _settings


class _Bounds:
    """Per-model random lower bounds (None = no bound)."""

    def __init__(self, real, bounds):
        self._real, self._bounds = real, bounds

    def resolve_lower(self, model, workload):
        b = self._bounds.get(model)
        return (None, None) if b is None else (b, "calibrated")

    def __getattr__(self, name):
        return getattr(self._real, name)


def _check(rows, providers, onprem, risk, max_cost, complexity, tokens, min_tier,
           floor, mode, bounds, exclude, circuits):
    """The invariant body. Returns the floor status the router reported
    ("no_eligible" when it raised NoEligibleCandidate)."""
    s = _settings(rows)
    names = [f"m{i}" for i in range(len(rows))]
    router = Router(s, quality_resolver=_Bounds(QualityResolver(s), bounds))
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(names),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None,
        workload="claims", quality_floor=floor, quality_floor_mode=mode)
    try:
        d = router.select(ctx, tokens_after=tokens, complexity=complexity,
                          min_tier=min_tier, exclude=exclude, open_circuits=circuits)
    except NoEligibleCandidate:
        return "no_eligible", None
    eligible = feasible_set(s, None, ctx, tenant_id=ctx.tenant_id,
                            exclude=exclude).eligible
    assert d.chosen.name in {c.name for c in eligible}              # rule 3
    policy_eligible = feasible_set(s, None, ctx, tenant_id=ctx.tenant_id).eligible
    hard = max([s.routing.risk_floors[risk]] + ([min_tier] if min_tier else []),
               key=lambda t: _TIER_ORDER[t])
    best = max(_TIER_ORDER[c.tier] for c in policy_eligible)
    assert _TIER_ORDER[d.chosen.tier] >= min(_TIER_ORDER[hard], best)  # rule 8
    if d.tier_downgrade is not None:
        assert _TIER_ORDER[d.tier_downgrade["to"]] < _TIER_ORDER[d.tier_downgrade["from"]]
        assert d.tier_downgrade["to"] == d.chosen.tier
    return (d.quality_floor["status"] if d.quality_floor else "off",
            d.tier_downgrade["cause"] if d.tier_downgrade else None)


_bound = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))


@hyp_settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(rows=st.lists(_model, min_size=1, max_size=6),
       providers=st.sets(st.sampled_from(_PROVIDERS), min_size=1),
       onprem=st.booleans(),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-6, 1e-4, 0.05, 1.0, 1.0, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       tokens=st.integers(min_value=0, max_value=3000),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       mode=st.sampled_from(["shadow", "enforce", "enforce", "off"]),
       data=st.data())
def test_floor_never_leaves_feasible_set_or_drops_below_hard_tier(
        rows, providers, onprem, risk, max_cost, complexity, tokens, min_tier,
        mode, data):
    names = [f"m{i}" for i in range(len(rows))]
    bounds = {n: data.draw(_bound) for n in names}
    # Half the time the floor is one of the drawn bounds (a model sits exactly
    # on it, so it qualifies); a uniform draw almost never lands under a bound
    # that also survives the hard tier, context and budget steps.
    drawn = [b for b in bounds.values() if b is not None and b >= 0.01]
    floor = data.draw(st.one_of(st.floats(min_value=0.01, max_value=1.0),
                                st.sampled_from(drawn)) if drawn
                      else st.floats(min_value=0.01, max_value=1.0))
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
    status, downgrade = _check(rows, providers, onprem, risk, max_cost, complexity,
                               tokens, min_tier, floor, mode, bounds, exclude, circuits)
    event(f"floor outcome: {mode}/{status}")
    if downgrade:
        event(f"tier downgrade: {downgrade}")
    # Steers hypothesis toward the paths that prove the property: the floor
    # actually picked the model, and endpoint state stepped a tier down (B70).
    target(1.0 if (mode == "enforce" and status == "met") else 0.0, label="enforce_met")
    target(1.0 if downgrade else 0.0, label="tier_downgrade")


_BASE = [("cheap", "onprem", 0.1, 0.1, None), ("mid", "onprem", 0.5, 0.5, None),
         ("frontier", "azure", 1.0, 1.0, None)]


@hyp_settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(extra=st.lists(_model, max_size=3),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-4, 1.0, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       mode=st.sampled_from(["off", "shadow", "enforce"]),
       data=st.data())
@example(extra=[], risk="low", max_cost=1e-4, complexity=0.95, min_tier=None,
         mode="off", data=None).via("review: prior-failure downgrade then budget fallback")
def test_failover_and_breaker_never_drop_below_hard_floor(
        extra, risk, max_cost, complexity, min_tier, mode, data):
    """B70/B71: the same invariant over a pool that always has every tier, so
    random failure exclusions and open circuits actually exercise both
    downgrade causes and the below-floor refusal."""
    rows = _BASE + extra
    names = [f"m{i}" for i in range(len(rows))]
    if data is None:   # the @example: the frontier failed, nothing open
        bounds, exclude, circuits = {}, frozenset({"m2"}), frozenset()
    else:
        bounds = {n: data.draw(_bound) for n in names}
        exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
        circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
    status, downgrade = _check(rows, {"onprem", "azure", "bedrock"}, False, risk,
                               max_cost, complexity, 100, min_tier, 0.5, mode,
                               bounds, exclude, circuits)
    event(f"downgrade: {downgrade}")
    if status == "no_eligible":
        event("refused")
    target(1.0 if downgrade else 0.0, label="tier_downgrade")


def test_downgrade_paths_are_reachable():
    """Deterministic guard: both downgrade causes are reachable through the
    invariant body, on the tiered path and with the floor live, so the
    property above cannot silently stop covering them. Pool: cheap on-prem,
    mid on-prem, frontier azure; low risk, complexity raises the target to
    frontier."""
    rows = [("cheap", "onprem", 1.0, 1.0, None), ("mid", "onprem", 1.0, 1.0, None),
            ("frontier", "azure", 5.0, 5.0, None)]
    s = _settings(rows)
    for mode in ("off", "shadow"):
        for cause, exclude, circuits in (("prior_failure", frozenset({"m2"}), frozenset()),
                                         ("circuit_open", frozenset(), frozenset({"m2"}))):
            assert _check(rows, {"onprem", "azure"}, False, "low", 1.0, 0.9, 100,
                          None, 0.8, mode, {}, exclude, circuits)[1] == cause
            ctx = ExecutionPolicyContext(
                request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
                allowed_providers=frozenset({"onprem", "azure"}),
                allowed_models=frozenset({"m0", "m1", "m2"}), max_cost_usd=1.0,
                max_latency_ms=15000, risk_class="low", require_verification=False,
                detected_entities=(), redaction_map_id=None, workload="claims",
                quality_floor=0.8, quality_floor_mode=mode)
            d = Router(s).select(ctx, tokens_after=100, complexity=0.9,
                                 exclude=exclude, open_circuits=circuits)
            assert d.tier_downgrade == {"from": "frontier", "to": "mid", "cause": cause}
    # high risk: the frontier floor holds -- the failure is a refusal, not mid
    assert _check(rows, {"onprem", "azure"}, False, "high", 1.0, 0.9, 100, None,
                  0.8, "off", {}, frozenset({"m2"}), frozenset())[0] == "no_eligible"


def test_enforce_met_path_is_reachable():
    """Deterministic guard: this exact case must hit enforce + met, so the
    property above cannot silently degrade into never exercising the floor."""
    rows = [("cheap", "onprem", 1.0, 1.0, None), ("frontier", "azure", 5.0, 5.0, None)]
    status = _check(rows, {"onprem", "azure"}, False, "low", 1.0, 0.5, 100, None,
                    0.8, "enforce", {"m0": 0.9, "m1": 0.95}, frozenset(), frozenset())[0]
    assert status == "met"


def test_prior_failure_downgrade_then_budget_fallback_records_the_served_tier():
    """Review fix: frontier failed -> stepped down to mid -> mid over budget ->
    budget fallback to cheap. `tier_downgrade.to` is the SERVED tier."""
    s = _settings(_BASE)
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset({"m0", "m1", "m2"}), max_cost_usd=1e-4,
        max_latency_ms=15000, risk_class="low", require_verification=False,
        detected_entities=(), redaction_map_id=None)
    d = Router(s).select(ctx, tokens_after=100, complexity=0.95,
                         exclude=frozenset({"m2"}))
    assert d.chosen.tier == "cheap"
    assert "budget fallback from mid" in d.reason
    assert d.tier_downgrade == {"from": "frontier", "to": "cheap",
                                "cause": "prior_failure"}
