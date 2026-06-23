from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return the current time in UTC ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _log_step(tag: str, msg: str, *, level: str = "info") -> None:
    """Emit a visually distinct graph-step line.

    Format: ``[GRAPH][TAG          ]  message``
    The fixed-width tag makes it easy to grep / column-align in log viewers.
    """
    getattr(logger, level)("[GRAPH][%-16s]  %s", tag, msg)
