"""Logging bootstrap for the BAS orchestrator.

A tiny, idempotent setup that:
    * attaches a stream handler to the ``bas`` root logger,
    * uses a single human-readable format suitable for both `uv run` and Docker,
    * respects ``LOG_LEVEL`` env var (default ``INFO``),
    * keeps uvicorn's own access/error loggers untouched.

Call :func:`configure_logging` once at process start (the FastAPI app's
startup hook does this). Modules then just do::

    import logging
    logger = logging.getLogger(__name__)
    logger.info("...")
"""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s :: %(message)s"
_DATEFMT = "%H:%M:%S"
_CONFIGURED = False


def configure_logging() -> None:
    """Attach a stdout handler to the ``bas`` logger exactly once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger("bas")
    root.setLevel(level)
    # Avoid duplicate lines if uvicorn already attached something.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    root.propagate = False

    _CONFIGURED = True
