import copy
import pytest

from app.config.lint import enforce, lint_settings
from app.config.loader import load_settings


def test_current_config_is_clean():
    assert lint_settings(load_settings()) == []


def test_provider_drift_is_caught():
    s = load_settings()
    s = copy.deepcopy(s)
    s.routing.model_pool[0].provider = "on_prem"  # drifted spelling
    errs = lint_settings(s)
    assert any("PROVIDER_NAME_DRIFT" in e for e in errs)


def test_enforce_exits_on_violation():
    s = copy.deepcopy(load_settings())
    s.routing.risk_floors["high"] = "strong"  # nonexistent tier
    with pytest.raises(SystemExit):
        enforce(s)


def test_bad_tenant_residency_is_caught():
    s = copy.deepcopy(load_settings())
    s.policy.tenants["hospital"].default_residency = "on_prem_only"
    assert any("RESIDENCY_UNKNOWN" in e for e in lint_settings(s))


def test_bad_rule_residency_is_caught():
    s = copy.deepcopy(load_settings())
    s.policy.sensitivity_rules[0].residency = "onpremonly"
    assert any("RESIDENCY_UNKNOWN" in e for e in lint_settings(s))
