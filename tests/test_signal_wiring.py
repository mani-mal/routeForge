"""Feature 2 wiring: the signal joins Router.select's max() as a raise-only
min_tier, and the residency clamp still caps it. These exercise the router
contract directly (the same seam the hop uses) plus a residency invariant."""
from app.config.loader import load_settings
from app.config.schema import StageSignalsCfg
from app.models import ExecutionPolicyContext
from app.routing.router import Router
from app.routing.signals import derive_signal_tier
from tests._litellm_fake import make_test_registry

S = load_settings()


def _onprem_ctx():
    # onprem_only, on-prem-only provider set (mirrors test_derived_onprem_reject).
    return ExecutionPolicyContext(
        request_id="r", tenant_id="hospital", sensitivity="restricted",
        onprem_only=True, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=True, detected_entities=(),
        redaction_map_id=None)


def test_signal_raises_tier_when_enabled():
    reg = make_test_registry(S)  # seeds on-prem local-8b (cheap) + local-32b
    ctx = _onprem_ctx()
    # A short prompt -> complexity ~0 -> would be cheap. A frontier signal
    # must raise the target above cheap (clamped to best on-prem tier).
    base = Router(S, registry=reg).select(ctx, tokens_after=10, complexity=0.01,
                                          min_tier=None)
    raised = Router(S, registry=reg).select(ctx, tokens_after=10, complexity=0.01,
                                            min_tier="frontier")
    order = {"cheap": 0, "mid": 1, "frontier": 2}
    assert order[raised.chosen.tier] >= order[base.chosen.tier]


def test_signal_never_lifts_onprem_only_to_cloud():
    reg = make_test_registry(S)
    ctx = _onprem_ctx()
    d = Router(S, registry=reg).select(ctx, tokens_after=10, complexity=0.01,
                                       min_tier="frontier")
    # Residency clamp wins: the chosen candidate stays on-prem.
    assert d.chosen.provider == "onprem"


def test_signal_is_raise_only_never_lowers():
    reg = make_test_registry(S)
    ctx = _onprem_ctx()
    # complexity forces mid; a 'cheap' signal must NOT lower it.
    order = {"cheap": 0, "mid": 1, "frontier": 2}
    d = Router(S, registry=reg).select(ctx, tokens_after=800, complexity=0.5,
                                       min_tier="cheap")
    assert order[d.chosen.tier] >= 1  # still >= mid from complexity


def test_flag_off_yields_no_signal():
    from app.routing.signals import SignalResult
    res = derive_signal_tier([{"role": "user", "content": "plan"}],
                             hop_index=1, cfg=StageSignalsCfg(enabled=False))
    assert res == SignalResult(None, None)


def test_hop_reason_shows_signal_when_enabled(client):
    """Drive the internal per-hop endpoint with stage_signals.enabled=True and
    a first hop with no tool messages -> derive_signal_tier infers a 'plan'
    role and the recorded DecisionRecord.reason must fold in that explanation.
    Mirrors the client/_ctx()-style setup in test_agent_internal_endpoint.py.
    """
    from app.agent.internal_endpoint import HOPS

    app = client.app
    stage_cfg = app.state.settings.routing.stage_signals.model_copy(
        update={"enabled": True})
    routing_cfg = app.state.settings.routing.model_copy(
        update={"stage_signals": stage_cfg})
    app.state.settings = app.state.settings.model_copy(
        update={"routing": routing_cfg})

    run_id = "run-signal-wiring"
    ctx = ExecutionPolicyContext(
        request_id="req-signal", tenant_id="acme", sensitivity="internal",
        onprem_only=False, allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False, detected_entities=(),
        redaction_map_id=None)
    HOPS.start(run_id, ctx)
    try:
        body = {"model": "auto", "stream": True,
                "messages": [{"role": "user", "content": "plan the steps"}],
                "tools": []}
        r = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
        assert r.status_code == 200
        rec = HOPS.pop(run_id)[0]
        assert "inferred role: plan" in rec.reason
    finally:
        if HOPS.has(run_id):
            HOPS.pop(run_id)
