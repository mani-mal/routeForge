import pytest
from app.compression import compress
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.models import ExecutionPolicyContext, Message


def _ctx(**over):
    base = dict(request_id="r", tenant_id="acme", sensitivity="internal",
                onprem_only=False, allowed_providers=frozenset({"onprem"}),
                allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
                risk_class="low", require_verification=False,
                detected_entities=(), redaction_map_id=None)
    base.update(over)
    return ExecutionPolicyContext(**base)


def _cfg(**over):
    co = load_settings().serving.context_optimization
    return co.model_copy(update=over)


def _msgs(body):
    return [Message(role="user", content=body)]


def test_disabled_is_passthrough():
    cfg = _cfg(enabled=False)
    r = compress(_msgs("x" * 50), _ctx(), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "disabled"
    assert r.tokens_after == r.tokens_before


def test_below_min_tokens_bypassed():
    cfg = _cfg(enabled=True, min_tokens=100000)
    r = compress(_msgs("short"), _ctx(), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "below_min_tokens"


def test_high_risk_bypassed():
    cfg = _cfg(enabled=True, min_tokens=1)
    r = compress(_msgs("INFO line\n" * 500), _ctx(risk_class="high"), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "high_risk"


def test_applies_and_reduces_history():
    cfg = _cfg(enabled=True, min_tokens=1)
    cfg = cfg.model_copy(update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    verbose = "INFO log line\n" * 800
    r = compress([Message(role="assistant", content=verbose)], _ctx(), cfg)
    assert r.record.applied is True
    assert r.tokens_after < r.tokens_before
    assert r.record.reduction_pct > 0


def test_onprem_only_ccr_never_to_cloud():
    cfg = _cfg(enabled=True, min_tokens=1)
    cfg = cfg.model_copy(update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    ccr = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    r = compress([Message(role="assistant", content="INFO\n" * 800)],
                 _ctx(onprem_only=True), cfg, ccr=ccr)
    assert r.record.ccr_retrievals_to_cloud == 0
    assert ccr.retrievals_to_cloud == 0


def test_restricted_sensitivity_bypassed():
    cfg = _cfg(enabled=True, min_tokens=1)
    r = compress(_msgs("INFO line\n" * 500), _ctx(sensitivity="restricted"), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "restricted_sensitivity"


def test_fail_open_on_compressor_error(monkeypatch):
    """A compressor exception must fall back to UNCOMPRESSED (fail OPEN) — the
    request still succeeds. Contrast the policy gate, which fails CLOSED."""
    import app.compression as comp_mod

    class _Boom:
        name = "boom"

        def compress_text(self, text: str) -> str:
            raise RuntimeError("compressor exploded")

    monkeypatch.setattr(comp_mod, "select_compressor", lambda cfg: _Boom())
    cfg = _cfg(enabled=True, min_tokens=1)
    cfg = cfg.model_copy(update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    original = [Message(role="assistant", content="INFO line\n" * 500)]
    r = compress(original, _ctx(), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "compressor_error"
    assert r.tokens_after == r.tokens_before
    assert r.messages == original  # unchanged, not partially compressed
