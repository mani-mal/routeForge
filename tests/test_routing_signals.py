from app.config.loader import load_settings
from app.config.schema import StageSignalsCfg
from app.routing.signals import SignalResult, derive_signal_tier

S = load_settings()

ON = StageSignalsCfg(enabled=True)


def test_stage_signals_defaults_off():
    cfg = S.routing.stage_signals
    assert cfg.enabled is False
    assert cfg.exploration_repeat >= 1
    assert cfg.tool_error_raise is True
    assert cfg.planner_role_raise is True


def _tool_msg(name, content):
    return {"role": "tool", "name": name, "content": content}


def test_plan_role_raises_on_first_hop_with_no_tools():
    msgs = [{"role": "user", "content": "plan a multi-step task"}]
    res = derive_signal_tier(msgs, hop_index=1, cfg=ON)
    assert res.min_tier == "frontier"
    assert "plan" in res.reason.lower()


def test_tool_error_raises_one_tier():
    msgs = [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "Error: connection refused (traceback ...)")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier == "mid"
    assert "error" in res.reason.lower()


def test_exploration_repeat_raises():
    msgs = [{"role": "user", "content": "go"}]
    for _ in range(3):
        msgs.append({"role": "assistant", "content": ""})
        msgs.append(_tool_msg("search", "no result"))
    res = derive_signal_tier(msgs, hop_index=4, cfg=ON)
    assert res.min_tier == "mid"
    assert "exploration" in res.reason.lower()


def test_steady_execute_hop_does_not_raise():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            _tool_msg("write_file", "ok, wrote 3 lines")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier is None
    assert res.reason is None


def test_disabled_cfg_never_raises():
    msgs = [{"role": "user", "content": "plan it"}]
    res = derive_signal_tier(msgs, hop_index=1, cfg=StageSignalsCfg(enabled=False))
    assert res == SignalResult(min_tier=None, reason=None)
