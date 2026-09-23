import copy
import pytest

from app.config.lint import ConfigLintError, enforce, lint_settings
from app.config.loader import load_settings
from app.config.schema import SignalsCfg


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


def test_lint_rejects_memory_telemetry_export():
    s = copy.deepcopy(load_settings())
    s.serving.memory.enabled = True
    s.serving.memory.export_to_telemetry = True
    with pytest.raises(ConfigLintError, match="export_to_telemetry"):
        enforce(s)


def test_lint_rejects_memory_residency_drift():
    s = copy.deepcopy(load_settings())
    s.serving.memory.enabled = True
    s.serving.memory.residency = "cloud_ok"
    with pytest.raises(ConfigLintError, match="residency"):
        enforce(s)


def test_lint_rejects_memory_nonpositive_ttl():
    s = copy.deepcopy(load_settings())
    s.serving.memory.enabled = True
    s.serving.memory.ttl_hours = 0
    with pytest.raises(ConfigLintError, match="ttl_hours"):
        enforce(s)


def test_memory_disabled_skips_guards():
    # memory.enabled False -- a would-be-violating residency/export/ttl must
    # NOT trip the lint (mirrors CCR's disabled-skips-guards posture).
    s = copy.deepcopy(load_settings())
    assert s.serving.memory.enabled is False
    s.serving.memory.residency = "cloud_ok"
    s.serving.memory.export_to_telemetry = True
    s.serving.memory.ttl_hours = 0
    enforce(s)  # must not raise


def test_lint_rejects_bad_default_policy_mode():
    # A typo in the deployment-wide protection posture must be a NAMED boot
    # failure, not a silent resolve-to-not-passthrough.
    s = copy.deepcopy(load_settings())
    s.serving.default_policy_mode = "passthru"
    with pytest.raises(ConfigLintError, match="default_policy_mode"):
        enforce(s)


def test_lint_accepts_passthrough_default_policy_mode():
    s = copy.deepcopy(load_settings())
    s.serving.default_policy_mode = "passthrough"
    enforce(s)  # must not raise -- both POLICY_MODES values are legal


def test_missing_default_estimated_output_tokens_is_a_boot_failure():
    s = copy.deepcopy(load_settings())
    del s.policy.budgets["default_estimated_output_tokens"]
    errs = lint_settings(s)
    assert any("default_estimated_output_tokens" in e for e in errs), errs
    with pytest.raises(SystemExit):
        enforce(s)


# --- P1b: endpoint data-handling declarations (design §7) --------------------
# region has NO enum (customer vocabulary, normalized to lowercase — never
# linted as invalid). prompt_retention and accepts_raw_sensitive DO have a
# fixed enum, and a value outside it must fail the boot with a named
# violation, on either a serving.yaml providers.<name> default or a
# routing.yaml model_pool entry.

def test_provider_prompt_retention_bad_value_is_a_boot_failure():
    s = copy.deepcopy(load_settings())
    s.serving.providers["azure"].prompt_retention = "sometimes"
    errs = lint_settings(s)
    assert any("PROVIDER_PROMPT_RETENTION_UNKNOWN" in e and "azure" in e
               for e in errs), errs
    with pytest.raises(SystemExit):
        enforce(s)


def test_provider_raw_sensitive_bad_value_is_a_boot_failure():
    s = copy.deepcopy(load_settings())
    s.serving.providers["azure"].accepts_raw_sensitive = "sometimes"
    errs = lint_settings(s)
    assert any("PROVIDER_RAW_SENSITIVE_UNKNOWN" in e and "azure" in e
               for e in errs), errs
    with pytest.raises(SystemExit):
        enforce(s)


def test_pool_prompt_retention_bad_value_is_a_boot_failure():
    s = copy.deepcopy(load_settings())
    victim = s.routing.model_pool[0]
    victim.prompt_retention = "sometimes"
    errs = lint_settings(s)
    assert any("POOL_PROMPT_RETENTION_UNKNOWN" in e and victim.name in e
               for e in errs), errs
    with pytest.raises(SystemExit):
        enforce(s)


def test_pool_raw_sensitive_bad_value_is_a_boot_failure():
    s = copy.deepcopy(load_settings())
    victim = s.routing.model_pool[0]
    victim.accepts_raw_sensitive = "sometimes"
    errs = lint_settings(s)
    assert any("POOL_RAW_SENSITIVE_UNKNOWN" in e and victim.name in e
               for e in errs), errs
    with pytest.raises(SystemExit):
        enforce(s)


def test_region_is_not_linted_as_an_enum():
    # Regions are customer vocabulary (design §2) -- an unrecognised token is
    # never a lint violation, on either source.
    s = copy.deepcopy(load_settings())
    s.serving.providers["azure"].region = "narnia"
    s.routing.model_pool[0].region = "mordor"
    assert lint_settings(s) == []


def test_data_handling_valid_values_boot_clean():
    s = copy.deepcopy(load_settings())
    s.serving.providers["azure"].prompt_retention = "none"
    s.serving.providers["azure"].accepts_raw_sensitive = "true"
    s.routing.model_pool[0].prompt_retention = "retained"
    s.routing.model_pool[0].accepts_raw_sensitive = "false"
    assert lint_settings(s) == []


def test_data_handling_values_are_case_insensitive():
    # `ModelRegistry._resolve_data_handling`/`_declared` lowercase-strip at
    # read time -- the lint must accept what they will accept.
    s = copy.deepcopy(load_settings())
    s.serving.providers["azure"].prompt_retention = "NONE"
    s.serving.providers["azure"].accepts_raw_sensitive = " True "
    assert lint_settings(s) == []


def test_lint_rejects_a_tier_missing_from_tier_latency_ms():
    s = copy.deepcopy(load_settings())
    s.routing.signals.tier_latency_ms.pop("frontier")
    assert any(e.startswith("TIER_LATENCY_MISSING") for e in lint_settings(s))


def test_lint_rejects_a_nonpositive_tier_latency_ms():
    s = copy.deepcopy(load_settings())
    s.routing.signals.tier_latency_ms["mid"] = 0
    assert any(e.startswith("TIER_LATENCY_NOT_POSITIVE") for e in lint_settings(s))


def test_lint_rejects_a_nonpositive_latency_scale_ms():
    # `latency_scale_ms: 0` passes every other check and then ZeroDivisionErrors
    # on the first scored request (DefaultRoutingStrategy.rank divides by it) --
    # the lint must catch it at boot, not at request time.
    s = copy.deepcopy(load_settings())
    s.routing.signals.latency_scale_ms = 0.0
    assert any(e.startswith("LATENCY_SCALE_NOT_POSITIVE") for e in lint_settings(s))
    with pytest.raises(SystemExit):
        enforce(s)


def test_lint_rejects_weights_with_no_availability():
    s = copy.deepcopy(load_settings())
    s.routing.weights.pop("availability")
    assert any(e.startswith("WEIGHT_MISSING") for e in lint_settings(s))


# --- Signals writer knobs (observation log, task 1) -------------------------

def _lint_with_signals(**overrides):
    """Helper to test signals config knobs: mirror existing test construction."""
    s = copy.deepcopy(load_settings())
    for k, v in overrides.items():
        setattr(s.routing.signals, k, v)
    return lint_settings(s)


@pytest.mark.parametrize("field,value,code", [
    ("flush_interval_s", 0.0, "SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE"),
    ("flush_interval_s", -1.0, "SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE"),
    ("max_buffer", 0, "SIGNALS_MAX_BUFFER_NOT_POSITIVE"),
    ("max_seed_obs_per_model", 0, "SIGNALS_MAX_SEED_OBS_NOT_POSITIVE"),
])
def test_signals_writer_knobs_must_be_positive(field, value, code):
    errs = _lint_with_signals(**{field: value})
    assert any(e.startswith(code) for e in errs)


def test_signals_writer_knob_defaults_pass_lint():
    errs = _lint_with_signals()
    assert not [e for e in errs if e.startswith("SIGNALS_")]


def test_signals_writer_knob_defaults():
    c = SignalsCfg()
    assert (c.flush_interval_s, c.max_buffer, c.max_seed_obs_per_model) == (5.0, 10000, 200)


# --- Step-7 circuit breaker config (routing.yaml `breaker`) -----------------

from app.config.schema import BreakerCfg


def test_breaker_config_loads_with_shipped_values():
    b = load_settings().routing.breaker
    assert b == BreakerCfg(enabled=True, failure_threshold=3, cooldown_s=30.0,
                           max_cooldown_s=300.0)


@pytest.mark.parametrize("field,value,code", [
    ("failure_threshold", 0, "BREAKER_THRESHOLD_NOT_POSITIVE"),
    ("cooldown_s", 0, "BREAKER_COOLDOWN_NOT_POSITIVE"),
    ("max_cooldown_s", 10, "BREAKER_MAX_COOLDOWN_BELOW_COOLDOWN"),
])
def test_bad_breaker_values_are_caught(field, value, code):
    s = copy.deepcopy(load_settings())
    setattr(s.routing.breaker, field, value)
    assert any(code in e for e in lint_settings(s))
