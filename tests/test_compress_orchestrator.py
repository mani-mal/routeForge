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


def _reducible_cfg():
    """enabled globally, min_tokens=1, user prompt not protected — so a long
    single message actually compresses when the app override lets it."""
    cfg = _cfg(enabled=True, min_tokens=1)
    return cfg.model_copy(
        update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})


def test_app_override_off_while_global_on():
    # Global Headroom ON, but this app forces it OFF → passthrough, reason
    # "disabled", exactly as if the global toggle were off for this app alone.
    cfg = _reducible_cfg()
    r = compress([Message(role="assistant", content="INFO\n" * 800)],
                 _ctx(compression_override=False), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "disabled"
    assert r.record.enabled is False
    assert r.tokens_after == r.tokens_before


def test_app_override_on_while_global_off():
    # Global Headroom OFF, but this app forces it ON → compression runs.
    cfg = _cfg(enabled=False, min_tokens=1)
    cfg = cfg.model_copy(
        update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    r = compress([Message(role="assistant", content="INFO log line\n" * 800)],
                 _ctx(compression_override=True), cfg)
    assert r.record.applied is True
    assert r.record.enabled is True
    assert r.tokens_after < r.tokens_before


def test_app_override_none_inherits_live_global():
    body = [Message(role="assistant", content="INFO\n" * 800)]
    # None (inherit) tracks whatever the passed-in (live) co_cfg says.
    off = _cfg(enabled=False, min_tokens=1)
    r_off = compress(body, _ctx(compression_override=None), off)
    assert r_off.record.bypass_reason == "disabled"
    on = _reducible_cfg()
    r_on = compress(body, _ctx(compression_override=None), on)
    assert r_on.record.applied is True


def test_app_override_on_still_obeys_safety_bypass():
    # Forcing compression ON per-app must NOT override the high-risk / restricted
    # safety bypasses — an app can't push high-stakes content through Headroom.
    cfg = _cfg(enabled=False, min_tokens=1)
    r_hr = compress([Message(role="assistant", content="INFO\n" * 800)],
                    _ctx(compression_override=True, risk_class="high"), cfg)
    assert r_hr.record.applied is False
    assert r_hr.record.bypass_reason == "high_risk"

    r_rs = compress([Message(role="assistant", content="INFO\n" * 800)],
                    _ctx(compression_override=True, sensitivity="restricted"), cfg)
    assert r_rs.record.applied is False
    assert r_rs.record.bypass_reason == "restricted_sensitivity"


def test_app_override_on_onprem_only_ccr_never_to_cloud():
    cfg = _cfg(enabled=False, min_tokens=1)
    cfg = cfg.model_copy(
        update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    ccr = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    r = compress([Message(role="assistant", content="INFO\n" * 800)],
                 _ctx(onprem_only=True, compression_override=True), cfg, ccr=ccr)
    assert r.record.applied is True
    assert r.record.ccr_retrievals_to_cloud == 0
    assert ccr.retrievals_to_cloud == 0


def test_content_integrity_exact_bypasses_compression():
    """content_integrity: exact must disable compression outright, even with
    compression enabled and a payload well over min_tokens (design P1a §8 --
    a DATA control, not a privacy one). Arranged exactly like
    test_applies_and_reduces_history (bypass_on.user_prompt forced False,
    min_tokens=1) so the control case actually would have compressed --
    proving the exact-mode test isn't passing for the wrong reason (the
    default bypass_on.user_prompt=True no-ops single-turn requests anyway)."""
    cfg = _reducible_cfg()
    verbose = "INFO log line\n" * 800
    messages = [Message(role="assistant", content=verbose)]

    # Control: WITHOUT content_integrity: exact, this payload DOES compress.
    baseline = compress(messages, _ctx(), cfg)
    assert baseline.record.applied is True
    assert baseline.tokens_after < baseline.tokens_before

    # Same cfg, same messages, only content_integrity flipped to "exact" ->
    # must NOT compress, and the messages that come back must be
    # byte-identical to what went in.
    r = compress(messages, _ctx(content_integrity="exact"), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "content_integrity_exact"
    assert r.tokens_after == r.tokens_before
    assert r.messages == messages
    assert r.messages[0].content == verbose


def test_content_integrity_exact_is_absolute_over_compression_override():
    """Unlike the ordinary per-app compression_override (which can force
    compression ON), content_integrity: exact cannot be overridden by it --
    exact wins regardless of override."""
    cfg = _cfg(enabled=False, min_tokens=1)
    cfg = cfg.model_copy(
        update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    messages = [Message(role="assistant", content="INFO log line\n" * 800)]
    r = compress(messages, _ctx(compression_override=True, content_integrity="exact"),
                 cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "content_integrity_exact"
    assert r.messages == messages


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
