import copy
import pathlib

import pytest
import yaml

from app.config.lint import ConfigLintError, enforce, lint_settings
from app.config.loader import load_settings


def test_boot_survives_serving_yaml_without_semantic_cache_block(tmp_path):
    # Upgrade safety: a mounted serving.yaml that predates feature #6 (the DGX
    # ConfigMap, Helm/HPE serving-config — none ship a semantic_cache: block)
    # must still boot. semantic_cache is optional/defaulted like memory and
    # context_optimization; a required key would KeyError every existing
    # deployment on upgrade.
    raw = yaml.safe_load(pathlib.Path("config/serving.yaml").read_text())
    for k in ("semantic_cache", "memory"):
        raw.pop(k, None)
    (tmp_path / "serving.yaml").write_text(yaml.safe_dump(raw))
    for f in ("policy.yaml", "routing.yaml", "pricing.yaml"):
        (tmp_path / f).write_text(pathlib.Path("config", f).read_text())
    s = load_settings(config_dir=str(tmp_path))  # must not raise
    assert s.serving.semantic_cache.enabled is False
    assert s.serving.semantic_cache.residency == "local_only"
    assert s.serving.memory.enabled is False
    enforce(s)  # boot lint must still pass on the defaulted config


def test_semantic_cache_cfg_surfaced_default_off():
    s = load_settings()  # loads config/*.yaml
    sc = s.serving.semantic_cache
    assert sc.enabled is False           # default OFF (C7) -- behavior unchanged
    assert sc.residency == "local_only"
    assert sc.ttl_hours == 24
    assert sc.export_to_telemetry is False
    assert sc.bypass_on.high_risk is True
    assert sc.bypass_on.restricted_sensitivity is True


def test_default_boot_lint_passes_clean():
    # Shipped config/serving.yaml semantic_cache.enabled: false -- boot lint
    # must be clean and nothing in the pipeline touches a cache instance.
    assert lint_settings(load_settings()) == []
    enforce(load_settings())  # must not raise / exit


def test_sp_semantic_cache_env_override(monkeypatch):
    monkeypatch.delenv("SP_SEMANTIC_CACHE", raising=False)
    assert load_settings().serving.semantic_cache.enabled is False
    for v in ("on", "On", "TRUE", "1", "yes"):
        monkeypatch.setenv("SP_SEMANTIC_CACHE", v)
        assert load_settings().serving.semantic_cache.enabled is True, v
    for v in ("off", "false", "0", "no", "garbage", ""):
        monkeypatch.setenv("SP_SEMANTIC_CACHE", v)
        assert load_settings().serving.semantic_cache.enabled is False, v


# --- C5/C6 fail-closed lint guards ------------------------------------------

def test_lint_rejects_semantic_cache_residency_drift_when_enabled():
    s = copy.deepcopy(load_settings())
    s.serving.semantic_cache.enabled = True
    s.serving.semantic_cache.residency = "cloud_ok"
    with pytest.raises(ConfigLintError, match="residency"):
        enforce(s)


def test_lint_rejects_semantic_cache_telemetry_export_when_enabled():
    s = copy.deepcopy(load_settings())
    s.serving.semantic_cache.enabled = True
    s.serving.semantic_cache.export_to_telemetry = True
    with pytest.raises(ConfigLintError, match="export_to_telemetry"):
        enforce(s)


def test_lint_rejects_zero_or_negative_ttl_when_enabled():
    s = copy.deepcopy(load_settings())
    s.serving.semantic_cache.enabled = True
    s.serving.semantic_cache.ttl_hours = 0
    with pytest.raises(ConfigLintError, match="ttl_hours"):
        enforce(s)

    s2 = copy.deepcopy(load_settings())
    s2.serving.semantic_cache.enabled = True
    s2.serving.semantic_cache.ttl_hours = -1
    with pytest.raises(ConfigLintError, match="ttl_hours"):
        enforce(s2)


def test_lint_ignores_semantic_cache_when_disabled():
    # A drifted value on a DISABLED cache must not fail the boot -- only
    # enabled=true makes these load-bearing (mirrors the CCR guards).
    s = copy.deepcopy(load_settings())
    assert s.serving.semantic_cache.enabled is False
    s.serving.semantic_cache.residency = "cloud_ok"
    s.serving.semantic_cache.export_to_telemetry = True
    s.serving.semantic_cache.ttl_hours = 0
    enforce(s)  # must not raise
