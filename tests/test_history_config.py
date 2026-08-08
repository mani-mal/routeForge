import os
from app.config.loader import load_settings


def test_history_retention_default_and_lint_green():
    s = load_settings()  # loads config/*.yaml + runs no exception (lint passes at construction)
    assert s.serving.history.retention_rows == 5000
    assert int(s.policy.agent_budgets["max_concurrent_runs"]) == 8
