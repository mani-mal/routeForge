import os

import pytest
from pydantic import ValidationError

from app.config.loader import load_settings
from app.config.schema import HistoryCfg


def test_history_retention_default_and_lint_green():
    s = load_settings()  # loads config/*.yaml + runs no exception (lint passes at construction)
    assert s.serving.history.retention_rows == 5000
    assert int(s.policy.agent_budgets["max_concurrent_runs"]) == 8


def test_history_retention_rows_zero_rejected():
    # 0 would make the prune threshold equal the just-inserted row's seq,
    # silently pruning every new row on write (dashboards always empty).
    # That must be a boot failure, not a silent misconfiguration.
    with pytest.raises(ValidationError):
        HistoryCfg(retention_rows=0)


def test_history_retention_rows_negative_rejected():
    with pytest.raises(ValidationError):
        HistoryCfg(retention_rows=-1)


def test_history_retention_rows_positive_accepted():
    assert HistoryCfg(retention_rows=1).retention_rows == 1
    assert HistoryCfg(retention_rows=42).retention_rows == 42
