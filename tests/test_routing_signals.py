from app.config.loader import load_settings

S = load_settings()


def test_stage_signals_defaults_off():
    cfg = S.routing.stage_signals
    assert cfg.enabled is False
    assert cfg.exploration_repeat >= 1
    assert cfg.tool_error_raise is True
    assert cfg.planner_role_raise is True
