"""validate_bounds: a `no_change` unit naming a model no longer in the pool is
skipped with a warning; a `recommended` one still refuses the apply."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.apps.calibration_sets import CalibrationError, validate_bounds


class _Reg:
    def effective_pool(self):
        return [SimpleNamespace(name="in-pool")]


def _u(model, status):
    return {"model": model, "workload": None, "mean": 0.9, "ci_low": 0.7,
            "status": status}


def test_no_change_unit_with_missing_model_is_skipped(caplog):
    rec = {"units": [_u("gone", "no_change"), _u("in-pool", "no_change")]}
    with caplog.at_level(logging.WARNING, logger="serving.apps"):
        out = validate_bounds(rec, registry=_Reg())
    assert [b["model"] for b in out] == ["in-pool"]
    assert "gone" in caplog.text


def test_recommended_unit_with_missing_model_still_refuses():
    with pytest.raises(CalibrationError, match="unknown model"):
        validate_bounds({"units": [_u("gone", "recommended")]}, registry=_Reg())
