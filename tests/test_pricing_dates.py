"""Model metrics slice 1, Task 6: optional as_of dates on prices."""
from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config.loader import load_settings
from app.config.schema import PriceCfg, PricingCfg

_PRICE = dict(input=1.0, output=2.0, tier="mid", provider="azure")


def _config_copy(tmp_path: Path, edit) -> str:
    dst = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", dst)
    p = dst / "pricing.yaml"
    raw = yaml.safe_load(p.read_text())
    edit(raw)
    p.write_text(yaml.safe_dump(raw))
    return str(dst)


def test_dates_optional():
    cfg = PricingCfg(models={"m": PriceCfg(**_PRICE)}, baseline_model="m")
    assert cfg.models["m"].as_of is None
    assert cfg.baseline_as_of is None
    assert cfg.pricing_stale_days == 30


def test_dates_parse(tmp_path):
    def edit(raw):
        raw["models"]["azure-gpt-mid"]["as_of"] = "2026-10-01"
        raw["baseline_as_of"] = "2026-10-02"
    s = load_settings(_config_copy(tmp_path, edit))
    assert s.pricing.models["azure-gpt-mid"].as_of == datetime.date(2026, 10, 1)
    assert s.pricing.baseline_as_of == datetime.date(2026, 10, 2)


def test_shipped_config_is_dated():
    s = load_settings()
    assert s.pricing.baseline_as_of == datetime.date(2026, 11, 4)


def test_malformed_date_fails_load(tmp_path):
    def edit(raw):
        raw["models"]["azure-gpt-mid"]["as_of"] = "2026-13-45"
    with pytest.raises(ValidationError):
        load_settings(_config_copy(tmp_path, edit))


def test_stale_days_must_be_positive():
    for bad in (0, -5):
        with pytest.raises(ValidationError):
            PricingCfg(models={}, baseline_model="m", pricing_stale_days=bad)


def test_policy_version_changes_with_as_of(tmp_path):
    base = load_settings(_config_copy(tmp_path / "a", lambda r: None))
    def edit(raw):
        raw["models"]["azure-gpt-mid"]["as_of"] = "2026-10-01"
    changed = load_settings(_config_copy(tmp_path / "b", edit))
    assert base.routing_policy_version != changed.routing_policy_version
