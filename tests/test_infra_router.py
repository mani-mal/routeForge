import pytest

from app.apps.infra import InfraRoster, InfraSnapshot, InfraClusterRow, InfraModelRow
from app.config.loader import load_settings
from app.models import ExecutionPolicyContext
from app.routing.router import Router

S = load_settings()


def _ctx(providers, risk="low", onprem=False):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _roster(models: dict[str, InfraModelRow], *, default_engine="vllm"):
    r = InfraRoster(S)
    r._snap = InfraSnapshot(
        cluster=InfraClusterRow(default_engine=default_engine), models=models)
    return r


def _model(name, *, engine="vllm", enabled=True):
    return InfraModelRow(model_name=name, engine=engine, enabled=enabled)


def _onprem_names():
    return [c.name for c in S.routing.model_pool if c.provider == "onprem"]


def test_disabled_onprem_model_excluded_with_reason():
    names = _onprem_names()
    assert len(names) >= 2, "test needs >=2 on-prem pool members"
    disabled = names[0]
    roster = _roster({disabled: _model(disabled, enabled=False)})
    r = Router(S, infra_roster=roster)
    d = r.select(_ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.9)
    assert d.chosen.name != disabled
    assert any(rc.name == disabled and "disabled by admin" in rc.reason
               for rc in d.rejected)


def test_all_onprem_disabled_onprem_only_rejects_never_cloud():
    roster = _roster({n: _model(n, enabled=False) for n in _onprem_names()})
    r = Router(S, infra_roster=roster)
    with pytest.raises(ValueError):
        r.select(_ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.1)


def test_all_enabled_roster_is_regression_noop():
    roster = _roster({n: _model(n, enabled=True) for n in _onprem_names()})
    with_roster = Router(S, infra_roster=roster).select(
        _ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    without = Router(S).select(
        _ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert with_roster.chosen.name == without.chosen.name


def test_serving_engine_annotated_for_onprem_choice():
    roster = _roster({n: _model(n, enabled=True) for n in _onprem_names()})
    d = Router(S, infra_roster=roster).select(
        _ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert d.serving_engine == "vllm"  # S.serving.inference_backend is "vllm" in tests


def test_decision_record_has_serving_engine_field():
    from app.models import DecisionRecord
    rec = DecisionRecord(step="router", chosen="local-8b", reason="x",
                         serving_engine="vllm")
    assert rec.serving_engine == "vllm"
    # default is None (cloud / not annotated)
    assert DecisionRecord(step="router", chosen="x", reason="y").serving_engine is None
