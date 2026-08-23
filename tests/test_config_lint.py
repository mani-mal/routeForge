import copy
import pytest

from app.config.lint import ConfigLintError, enforce, lint_settings
from app.config.loader import load_settings


def test_current_config_is_clean():
    assert lint_settings(load_settings()) == []


def test_provider_drift_is_caught():
    s = load_settings()
    s = copy.deepcopy(s)
    s.routing.model_pool[0].provider = "on_prem"  # drifted spelling
    errs = lint_settings(s)
    assert any("PROVIDER_UNDECLARED" in e for e in errs)


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


def test_lint_rejects_ccr_telemetry_export():
    s = copy.deepcopy(load_settings())
    s.serving.context_optimization.ccr.export_to_telemetry = True
    with pytest.raises(ConfigLintError, match="export_to_telemetry"):
        enforce(s)


def test_lint_rejects_ccr_residency_drift():
    s = copy.deepcopy(load_settings())
    s.serving.context_optimization.ccr.residency = "cloud_ok"
    with pytest.raises(ConfigLintError, match="residency"):
        enforce(s)


def test_pool_model_without_price_is_caught():
    s = copy.deepcopy(load_settings())
    s.routing.model_pool.append(
        s.routing.model_pool[0].model_copy(update={"name": "no-such-price-row"}))
    errs = lint_settings(s)
    assert any("POOL_MODEL_NO_PRICE" in e and "no-such-price-row" in e for e in errs)


def test_empty_onprem_tier_boots_clean():
    # No built-in on-prem pool member any more (onboarded at runtime) — an
    # empty on-prem tier must not trip TIER_HAS_NO_MEMBER (that check is gone)
    # or any other lint violation.
    s = load_settings()
    assert not any(c.provider == "onprem" for c in s.routing.model_pool)
    assert lint_settings(s) == []


def test_tier_quality_missing_tier_flags():
    s = copy.deepcopy(load_settings())
    s.routing.tier_quality.pop("frontier")
    assert any("TIER_QUALITY_MISSING" in e for e in lint_settings(s))


def test_tier_quality_out_of_range_flags():
    s = copy.deepcopy(load_settings())
    s.routing.tier_quality["mid"] = 1.5
    assert any("TIER_QUALITY_OUT_OF_RANGE" in e for e in lint_settings(s))


def test_pool_quality_out_of_range_flags():
    s = copy.deepcopy(load_settings())
    s.routing.model_pool[0].quality = 2.0
    assert any("POOL_QUALITY_OUT_OF_RANGE" in e for e in lint_settings(s))


def test_role_tier_unknown_is_caught():
    s = copy.deepcopy(load_settings())
    s.routing.stage_signals.role_tiers["plan"] = "strong"  # nonexistent tier
    assert any("ROLE_TIER_UNKNOWN" in e for e in lint_settings(s))
