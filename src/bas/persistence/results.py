"""Per-engagement result store for raw backend execution results.

Layout::

    <root>/<engagement_id>/results/<operation_id>.json

Writes are idempotent: if a file for a given ``operation_id`` already exists,
``save`` returns the existing path without overwriting. This prevents
duplicate processing when the backend retries a webhook delivery.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class ResultStore:
    """Stores raw execution-result JSON received from the BAS backend."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    def _results_dir(self, engagement_id: str) -> Path:
        d = self._root / engagement_id / "results"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def exists(self, engagement_id: str, operation_id: str) -> bool:
        return (self._root / engagement_id / "results" / f"{operation_id}.json").is_file()

    def save(self, engagement_id: str, operation_id: str, raw_payload: dict[str, Any]) -> Path:
        """Atomically write result JSON. Idempotent — skips if file exists."""
        target = self._results_dir(engagement_id) / f"{operation_id}.json"
        if target.is_file():
            return target
        tmp = target.with_suffix(".json.tmp")
        with self._lock:
            if target.is_file():  # re-check under lock
                return target
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(raw_payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        return target

    def get(self, engagement_id: str, operation_id: str) -> dict[str, Any] | None:
        target = self._root / engagement_id / "results" / f"{operation_id}.json"
        if not target.is_file():
            return None
        with target.open("r", encoding="utf-8") as f:
            return json.load(f)

    def list_ids(self, engagement_id: str) -> list[str]:
        d = self._root / engagement_id / "results"
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    @property
    def root(self) -> Path:
        return self._root
