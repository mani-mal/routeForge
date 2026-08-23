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
    assert cfg.role_tiers == {"plan": "frontier", "verify": "mid"}


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


def test_disabled_cfg_suppresses_error_and_exploration_branches():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "Error: connection refused (traceback ...)")]
    res = derive_signal_tier(msgs, hop_index=3, cfg=StageSignalsCfg(enabled=False))
    assert res == SignalResult(None, None)


def test_benign_content_with_error_substring_does_not_raise():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "no error found; error-free result")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier is None
    assert res.reason is None


def test_plural_error_content_still_raises():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "3 errors occurred; several failures logged")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier == "mid"
    assert "error" in res.reason.lower()


# --- Task 1: full role tagging (plan / verify / execute / format) ---------

def test_verify_role_raises_to_mid():
    msgs = [{"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "ok, found a result"),
            {"role": "user", "content": "please verify this is correct"}]
    res = derive_signal_tier(msgs, hop_index=3, cfg=ON)
    assert res.min_tier == "mid"
    assert "inferred role: verify" in res.reason.lower()


def test_format_role_does_not_raise():
    msgs = [{"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "ok, found a result"),
            {"role": "user", "content": "please summarize the final answer"}]
    res = derive_signal_tier(msgs, hop_index=3, cfg=ON)
    assert res.min_tier is None
    assert res.reason is None


def test_execute_role_with_no_cues_does_not_raise():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": ""},
            _tool_msg("write_file", "ok, wrote 3 lines")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=ON)
    assert res.min_tier is None
    assert res.reason is None


def test_planner_role_raise_flag_gates_plan_role_specifically():
    # planner_role_raise=False must suppress the plan raise even though the
    # role_tiers map still names "plan" -> "frontier".
    cfg = StageSignalsCfg(enabled=True, planner_role_raise=False)
    msgs = [{"role": "user", "content": "plan a multi-step task"}]
    res = derive_signal_tier(msgs, hop_index=1, cfg=cfg)
    assert res.min_tier is None
    assert res.reason is None


def test_role_tiers_is_config_driven_not_hardcoded():
    # With role_tiers={"verify": "frontier"} a verify hop must raise to
    # frontier, proving the map is read rather than a hardcoded "mid".
    cfg = StageSignalsCfg(enabled=True, role_tiers={"verify": "frontier"})
    msgs = [{"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "ok, found a result"),
            {"role": "user", "content": "check this is correct"}]
    res = derive_signal_tier(msgs, hop_index=3, cfg=cfg)
    assert res.min_tier == "frontier"


def test_strongest_raise_wins_when_multiple_signals_fire():
    # "plan" requires zero role=='tool' messages by definition (preserved
    # trigger), which structurally can never co-occur with the tool-error
    # grounded signal (which requires a tool message). To exercise the
    # strongest-raise combine honestly, configure "execute" (the role that
    # naturally applies once tool activity exists) to also raise to
    # "frontier" via role_tiers, and pair it with a real tool error (mid).
    # frontier must win, and the reason must name both firing signals.
    cfg = StageSignalsCfg(enabled=True,
                          role_tiers={"plan": "frontier", "verify": "mid",
                                      "execute": "frontier"})
    msgs = [{"role": "user", "content": "do it"},
            {"role": "assistant", "content": ""},
            _tool_msg("search", "Error: connection refused (traceback ...)")]
    res = derive_signal_tier(msgs, hop_index=2, cfg=cfg)
    assert res.min_tier == "frontier"
    assert "inferred role: execute" in res.reason.lower()
    assert "tool-error" in res.reason.lower()


def test_role_absent_from_role_tiers_never_raises():
    cfg = StageSignalsCfg(enabled=True, role_tiers={})
    msgs = [{"role": "user", "content": "plan a multi-step task"}]
    res = derive_signal_tier(msgs, hop_index=1, cfg=cfg)
    assert res.min_tier is None
    assert res.reason is None
