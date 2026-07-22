from app.config.loader import load_settings


def test_context_optimization_cfg_surfaced(tmp_path, monkeypatch):
    s = load_settings()  # loads config/*.yaml
    co = s.serving.context_optimization
    assert co.enabled is False           # demo default
    assert co.min_tokens == 2000
    assert co.bypass_on.high_risk is True
    assert co.bypass_on.restricted_sensitivity is True
    assert co.bypass_on.user_prompt is True
    assert co.ccr.residency == "local_only"
    assert co.ccr.export_to_telemetry is False
    assert co.ccr.ttl_hours == 24
    assert co.cache_alignment is True


def test_sp_context_opt_env_override(monkeypatch):
    # unset → follows config default (false)
    monkeypatch.delenv("SP_CONTEXT_OPT", raising=False)
    assert load_settings().serving.context_optimization.enabled is False
    # truthy spellings → enabled
    for v in ("on", "On", "TRUE", "1", "yes"):
        monkeypatch.setenv("SP_CONTEXT_OPT", v)
        assert load_settings().serving.context_optimization.enabled is True, v
    # anything else → disabled
    for v in ("off", "false", "0", "no", "garbage", ""):
        monkeypatch.setenv("SP_CONTEXT_OPT", v)
        assert load_settings().serving.context_optimization.enabled is False, v
