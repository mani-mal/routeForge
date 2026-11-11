"""B80 insurance on CLAUDE.md rules 3 and 8: whenever `Router.select` returns
under a quality floor (shadow or enforce), the chosen candidate is in the
policy-feasible set and is at or above the hard tier -- max(risk floor,
min_tier) clamped down to the best allowed tier."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st

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


@hyp_settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(rows=st.lists(_model, min_size=1, max_size=6),
       providers=st.sets(st.sampled_from(_PROVIDERS), min_size=1),
       onprem=st.booleans(),
       risk=st.sampled_from(["low", "medium", "high"]),
       max_cost=st.sampled_from([1e-6, 1e-4, 1e-3, 0.05, 1.0]),
       complexity=st.floats(min_value=0.0, max_value=1.0),
       tokens=st.integers(min_value=0, max_value=3000),
       min_tier=st.one_of(st.none(), st.sampled_from(_TIERS)),
       floor=st.floats(min_value=0.01, max_value=1.0),
       mode=st.sampled_from(["shadow", "enforce"]),
       data=st.data())
def test_floor_never_leaves_feasible_set_or_drops_below_hard_tier(
        rows, providers, onprem, risk, max_cost, complexity, tokens, min_tier,
        floor, mode, data):
    s = _settings(rows)
    names = [f"m{i}" for i in range(len(rows))]
    bounds = {n: data.draw(st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)))
              for n in names}
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
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
        return
    eligible = feasible_set(s, None, ctx, tenant_id=ctx.tenant_id,
                            exclude=exclude).eligible
    assert d.chosen.name in {c.name for c in eligible}              # rule 3
    hard = max([s.routing.risk_floors[risk]] + ([min_tier] if min_tier else []),
               key=lambda t: _TIER_ORDER[t])
    best = max(_TIER_ORDER[c.tier] for c in eligible)
    assert _TIER_ORDER[d.chosen.tier] >= min(_TIER_ORDER[hard], best)  # rule 8
