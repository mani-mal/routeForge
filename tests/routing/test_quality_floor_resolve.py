import copy

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.policy.context import build_context
from app.routing.quality_floor import resolve_floor

BASE = load_settings()


def _settings(mode="off", workloads=None):
    s = copy.deepcopy(BASE)
    s.routing.quality_floors.mode = mode
    s.routing.quality_floors.workloads = dict(workloads or {})
    return s


def _app(floors=None, mode=None):
    return AppPolicyView(
        residency="any",
        quality_floors=tuple(sorted(floors.items())) if floors else None,
        quality_floor_mode=mode,
    )


def test_deployment_only():
    assert resolve_floor(_settings(workloads={"claims": 0.8}), None, "claims") == (0.8, "off")


def test_app_raises():
    s = _settings(workloads={"claims": 0.8})
    assert resolve_floor(s, _app({"claims": 0.9}), "claims") == (0.9, "off")


def test_app_lower_does_not_lower():
    s = _settings(workloads={"claims": 0.8})
    assert resolve_floor(s, _app({"claims": 0.7}), "claims") == (0.8, "off")


def test_app_only_workload():
    assert resolve_floor(_settings(), _app({"claims": 0.6}), "claims") == (0.6, "off")


def test_no_workload():
    s = _settings(mode="shadow", workloads={"claims": 0.8})
    assert resolve_floor(s, None, None) == (None, "shadow")


def test_no_value_for_workload():
    assert resolve_floor(_settings(workloads={"claims": 0.8}), None, "other") == (None, "off")


def test_app_mode_overrides_deployment():
    s = _settings(mode="shadow", workloads={"claims": 0.8})
    assert resolve_floor(s, _app(mode="enforce"), "claims") == (0.8, "enforce")


def test_app_mode_null_inherits():
    s = _settings(mode="shadow", workloads={"claims": 0.8})
    assert resolve_floor(s, _app({"claims": 0.7}), "claims") == (0.8, "shadow")


def test_does_not_mutate_settings():
    s = _settings(workloads={"claims": 0.8})
    resolve_floor(s, _app({"claims": 0.9}), "claims")
    assert s.routing.quality_floors.workloads == {"claims": 0.8}


def test_build_context_sets_both_fields():
    s = _settings(mode="shadow", workloads={"claims": 0.8})
    ctx = build_context(s, "r1", "default", [], None,
                        app_policy=_app({"claims": 0.9}, mode="enforce"),
                        workload="claims")
    assert ctx.quality_floor == 0.9
    assert ctx.quality_floor_mode == "enforce"


def test_build_context_defaults_without_workload():
    ctx = build_context(_settings(), "r1", "default", [], None)
    assert ctx.quality_floor is None
    assert ctx.quality_floor_mode == "off"
