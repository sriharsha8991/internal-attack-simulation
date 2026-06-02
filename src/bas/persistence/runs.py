"""File-backed engagement registry.

One JSON file per engagement under ``runs_dir``. Writes are atomic
(write-tmp + rename) so a crash mid-write never leaves a half-formed file.
The in-memory dict is the source of truth at runtime; disk is the durability
tier.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from ._serialise import serialise

RunStatus = Literal["queued", "running", "awaiting_results", "completed", "failed"]


class RunStore:
    """Thread-safe JSON-file backed registry of engagements."""

    def __init__(self, runs_dir: str | Path) -> None:
        self._dir = Path(runs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_existing()

    # ---- lifecycle ---------------------------------------------------------

    def _load_existing(self) -> None:
        for path in sorted(self._dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    record = json.load(f)
                run_id = record.get("run_id") or path.stem
                self._cache[run_id] = record
            except (OSError, json.JSONDecodeError):
                continue

    # ---- read --------------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._cache.get(run_id)
            return dict(rec) if rec else None

    def list_all(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._cache.values())
        records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        if limit is not None:
            records = records[:limit]
        return [dict(r) for r in records]

    def __contains__(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cache

    # ---- write -------------------------------------------------------------

    def save(self, record: dict[str, Any]) -> None:
        run_id = record["run_id"]
        payload = serialise(record)
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / f"{run_id}.json"
        tmp = target.with_suffix(".json.tmp")

        with self._lock:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            self._cache[run_id] = payload

    def delete(self, run_id: str) -> bool:
        target = self._dir / f"{run_id}.json"
        with self._lock:
            removed = self._cache.pop(run_id, None) is not None
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return removed

    # ---- convenience -------------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        return self._dir


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_record(
    *,
    run_id: str,
    request: dict[str, Any],
    status: RunStatus = "queued",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": status,
        "started_at": now_iso(),
        "finished_at": None,
        "request": request,
        "state": None,
        "error": None,
    }


def iter_records(store: RunStore) -> Iterable[dict[str, Any]]:
    yield from store.list_all()
