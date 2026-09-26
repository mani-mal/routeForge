"""B57: RouteForge configures logging for the `serving.*` namespace.

Before this, nothing called basicConfig/dictConfig/setLevel, so the root
logger stayed at WARNING with no handler and every `serving.*` INFO record was
dropped in deployment. `caplog` hid that (it captures whatever the handler
configuration), so these tests read what the CONFIGURED HANDLER actually
wrote to stderr (`capsys`), not caplog.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.main import create_app
from app.obs.log_config import HANDLER_NAME, SERVING_LOGGER
from tests.test_conflicting_core_api import (
    HELLO, _client, _no_candidate_app, _with_azure_enabled,
)


def _our_handlers():
    return [h for h in logging.getLogger(SERVING_LOGGER).handlers
            if h.get_name() == HANDLER_NAME]


@pytest.fixture(autouse=True)
def _restore_serving_logger(monkeypatch):
    monkeypatch.delenv("SP_LOG_LEVEL", raising=False)
    logger = logging.getLogger(SERVING_LOGGER)
    level = logger.level
    yield
    logger.setLevel(level)


def test_serving_info_record_reaches_stderr(capsys):
    create_app()
    capsys.readouterr()
    logging.getLogger("serving.api").info("b57-probe-line")
    err = capsys.readouterr().err
    line = next(ln for ln in err.splitlines() if "b57-probe-line" in ln)
    # One line: timestamp, level, logger name, message.
    assert " INFO serving.api b57-probe-line" in line


def test_create_app_twice_leaves_exactly_one_handler():
    create_app()
    create_app()
    assert len(_our_handlers()) == 1


def test_root_and_third_party_loggers_untouched():
    root = logging.getLogger()
    before = (root.level, list(root.handlers),
              logging.getLogger("LiteLLM").level, logging.getLogger("litellm").level)
    create_app()
    after = (root.level, list(root.handlers),
             logging.getLogger("LiteLLM").level, logging.getLogger("litellm").level)
    assert before == after


def test_sp_log_level_warning_suppresses_info(monkeypatch, capsys):
    monkeypatch.setenv("SP_LOG_LEVEL", "WARNING")
    create_app()
    capsys.readouterr()
    logging.getLogger("serving.api").info("b57-info-hidden")
    logging.getLogger("serving.api").warning("b57-warning-shown")
    err = capsys.readouterr().err
    assert "b57-info-hidden" not in err
    assert "b57-warning-shown" in err


def test_invalid_sp_log_level_falls_back_to_info_with_a_warning(monkeypatch, capsys):
    monkeypatch.setenv("SP_LOG_LEVEL", "LOUD")
    create_app()
    err = capsys.readouterr().err
    assert "SP_LOG_LEVEL='LOUD' is not a log level" in err
    assert logging.getLogger(SERVING_LOGGER).level == logging.INFO


def test_no_eligible_line_reaches_the_handler_end_to_end(monkeypatch, capsys):
    """The P3b `no_eligible_model` structured line, through the real HTTP
    boundary, printed by the configured handler (not merely captured)."""
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = _no_candidate_app(c, "b57-log")
        capsys.readouterr()
        assert app.chat(HELLO).status_code == 422
        err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines()
             if " INFO serving.api " in ln and "no_eligible_model" in ln]
    assert len(lines) == 1, err
    payload = json.loads(lines[0].split(" INFO serving.api ", 1)[1])
    assert payload["event"] == "no_eligible_model"
    assert payload["request_id"]
    assert "residency" in [r["id"] for r in payload["conflicting_core"]["requirements"]]
