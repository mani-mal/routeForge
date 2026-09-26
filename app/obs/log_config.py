"""Process logging for the `serving.*` namespace (B57).

Nothing used to configure logging, so the root logger stayed at Python's
default WARNING with no handler and every `serving.*` INFO record -- the P3b
`no_eligible_model` line, the client-disconnect line, the structured request
lines -- was dropped in deployment. `configure_logging()` fixes that for our
namespace only:

* ONE stderr handler on the `serving` logger, one line per record
  (`%(asctime)s %(levelname)s %(name)s %(message)s`; the structured lines
  already carry their JSON in the message);
* the `serving` logger's level from `SP_LOG_LEVEL` (default `INFO`; an
  unknown value falls back to `INFO` with a warning);
* idempotent -- `create_app()` runs many times in the test suite, and the
  handler is marked so a second call never stacks another one;
* the root logger and every third-party logger (litellm is very chatty at
  INFO) are left exactly as they were.

`propagate` stays True. A plain `uvicorn app.main:app` process has no root
handler (uvicorn configures only its own `uvicorn.*` loggers), so nothing is
printed twice; and pytest's `caplog`, which listens on the ROOT logger, keeps
seeing `serving.*` records. The one cost: a deployment that installs its own
root handler (e.g. `logging.basicConfig`) sees serving lines once per handler.
"""
from __future__ import annotations

import logging
import os
import sys

SERVING_LOGGER = "serving"
LEVEL_ENV = "SP_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
HANDLER_NAME = "routeforge-serving-stderr"


class _StderrHandler(logging.StreamHandler):
    """A StreamHandler that writes to whatever `sys.stderr` is AT EMIT TIME,
    like `logging.lastResort`. Binding the stream once would pin whatever
    object `sys.stderr` happened to be when `create_app()` ran -- under a test
    runner that swaps it, a stream that is later closed."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def _resolve_level(raw: str | None) -> tuple[int, str | None]:
    """`(level, warning)`: the configured level, or INFO plus a warning to log
    when `raw` is not a standard level name."""
    if raw is None or not raw.strip():
        return logging.INFO, None
    name = raw.strip().upper()
    level = logging.getLevelNamesMapping().get(name)
    if level is None or name == "NOTSET":
        return logging.INFO, (f"{LEVEL_ENV}={raw!r} is not a log level; "
                              f"using {DEFAULT_LEVEL}")
    return level, None


def configure_logging() -> logging.Handler:
    """Attach (once) the stderr handler to the `serving` logger and set its
    level from `SP_LOG_LEVEL`. Returns the handler."""
    logger = logging.getLogger(SERVING_LOGGER)
    level, warning = _resolve_level(os.environ.get(LEVEL_ENV))
    logger.setLevel(level)

    handler = next((h for h in logger.handlers if h.get_name() == HANDLER_NAME), None)
    if handler is None:
        handler = _StderrHandler()
        handler.set_name(HANDLER_NAME)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    if warning is not None:
        logger.warning(warning)
    return handler
