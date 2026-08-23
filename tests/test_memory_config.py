from app.config.loader import load_settings


def test_memory_cfg_surfaced_and_default_off():
    """R7 / default OFF: shipped config/serving.yaml ships memory.enabled: false
    -- boot must load it unchanged and no other file may restate it."""
    s = load_settings()  # loads config/*.yaml
    mem = s.serving.memory
    assert mem.enabled is False
    assert mem.residency == "local_only"
    assert mem.ttl_hours == 24
    assert mem.export_to_telemetry is False
    assert mem.max_recall == 3
    assert mem.min_tokens == 0
    assert mem.bypass_on.high_risk is True
    assert mem.bypass_on.restricted_sensitivity is True


def test_sp_memory_env_override(monkeypatch):
    # unset -> follows config default (false)
    monkeypatch.delenv("SP_MEMORY", raising=False)
    assert load_settings().serving.memory.enabled is False
    # truthy spellings -> enabled
    for v in ("on", "On", "TRUE", "1", "yes"):
        monkeypatch.setenv("SP_MEMORY", v)
        assert load_settings().serving.memory.enabled is True, v
    # anything else -> disabled
    for v in ("off", "false", "0", "no", "garbage", ""):
        monkeypatch.setenv("SP_MEMORY", v)
        assert load_settings().serving.memory.enabled is False, v
