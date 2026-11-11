"""B80 Task 6 identity: with no floor for the request (`quality_floor=None`)
or with mode `off`, `Router.select` decides exactly what a router whose
context carries no floor fields at all decides -- over random pools,
contexts, complexities, budgets, escalations, exclusions and open circuits.

Registry-free: the pool is `settings.routing.model_pool` priced from
`settings.pricing.models`, so each example builds its own pool cheaply."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st

from app.config.schema import PoolCandidate, PriceCfg
from app.errors import NoEligibleCandidate
from app.models import ExecutionPolicyContext
from app.routing.router import Router
from tests.test_router import S

_TIERS = ["cheap", "mid", "frontier"]
_PROVIDERS = ["onprem", "azure", "bedrock"]


class _AllQualify:
    """Every model has a high calibrated bound, so a live floor branch WOULD
    change decisions -- the identity cannot pass by accident."""

    def __init__(self, real):
        self._real = real

    def resolve_lower(self, model, workload):
        return 0.99, "calibrated"

    def __getattr__(self, name):
        return getattr(self._real, name)


_model = st.tuples(st.sampled_from(_TIERS), st.sampled_from(_PROVIDERS),
                   st.floats(min_value=0.01, max_value=20.0),
                   st.floats(min_value=0.01, max_value=60.0),
                   st.one_of(st.none(), st.integers(min_value=1, max_value=4000)))


def _settings(rows):
    pool = [PoolCandidate(name=f"m{i}", provider=p, tier=t, context_window=w)
            for i, (t, p, _pi, _po, w) in enumerate(rows)]
    prices = {f"m{i}": PriceCfg(input=pi, output=po, tier=t, provider=p)
              for i, (t, p, pi, po, _w) in enumerate(rows)}
    routing = S.routing.model_copy(update={"model_pool": pool})
    pricing = S.pricing.model_copy(update={"models": prices})
    return S.model_copy(update={"routing": routing, "pricing": pricing})


def _outcome(router, ctx, kw):
    try:
        d = router.select(ctx, **kw)
    except NoEligibleCandidate as e:
        return ("raise", str(e))
    return (d.chosen.name, d.reason, [r.model_dump() for r in d.rejected],
            d.tier_target, d.est_cost_usd, d.circuit_bypass, d.quality_floor,
            d.scoring_record())


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
       data=st.data())
def test_no_floor_or_mode_off_is_the_unfloored_router(rows, providers, onprem, risk,
                                                      max_cost, complexity, tokens,
                                                      out, min_tier, data):
    s = _settings(rows)
    names = [f"m{i}" for i in range(len(rows))]
    exclude = frozenset(data.draw(st.sets(st.sampled_from(names))))
    circuits = frozenset(data.draw(st.sets(st.sampled_from(names))))
    from app.routing.quality import QualityResolver
    router = Router(s, quality_resolver=_AllQualify(QualityResolver(s)))
    base = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(names),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None,
        workload="claims")
    kw = dict(tokens_after=tokens, complexity=complexity, min_tier=min_tier,
              exclude=exclude, output_tokens=out, open_circuits=circuits)

    expected = _outcome(router, base, kw)
    if expected[0] != "raise":
        assert expected[6] is None          # no floor fields -> no floor record
    for mode in ("off", "shadow", "enforce"):
        no_floor = base.model_copy(update={"quality_floor": None,
                                           "quality_floor_mode": mode})
        assert _outcome(router, no_floor, kw) == expected
    mode_off = base.model_copy(update={"quality_floor": 0.5, "quality_floor_mode": "off"})
    assert _outcome(router, mode_off, kw) == expected
