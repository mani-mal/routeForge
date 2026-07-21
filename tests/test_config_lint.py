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
