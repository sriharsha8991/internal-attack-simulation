"""File-backed run registry.

One JSON file per run under `runs_dir`. Writes are atomic (write-tmp + rename)
so a crash mid-write never leaves a half-formed file. The in-memory dict is
the source of truth at runtime; disk is the durability tier.

Schema is intentionally informal — we read whatever's on disk best-effort and
swallow malformed files with a log line. Production persistence (Redis +
Celery + checkpointed graph state) replaces this in M2; nothing else in the
codebase touches the files directly.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel

RunStatus = Literal["queued", "running", "completed", "failed"]


def _serialise(value: Any) -> Any:
    """Recursive JSON-safe coercion. Pydantic models → model_dump(mode='json')."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


class RunStore:
    """Thread-safe JSON-file backed registry of runs."""

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
                # Skip corrupt files silently — operator can `bas runs prune`.
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
        payload = _serialise(record)
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


# ---------------------------------------------------------------------------
# Artifact store — per-engagement subdirectory holding ability + adversary JSON
# ---------------------------------------------------------------------------


class ArtifactStore:
    """Writes one JSON file per created ability and adversary, on disk.

    Layout::

        <root>/<engagement_id>/abilities/<ability_id>.json
        <root>/<engagement_id>/adversaries/<adversary_id>.json

    The ability JSON contains the full body, every stage's command_template,
    MITRE technique, executor, etc. — the exact spec we POSTed to BAS. This is
    the local source-of-truth for "what did the LLM tell the backend to do?"
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    def _engagement_dir(self, engagement_id: str) -> Path:
        d = self._root / engagement_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_ability(
        self,
        engagement_id: str,
        *,
        ability_id: str,
        skill: str,
        ability: Any,
        stages: list[Any],
        rationale: str | None = None,
        provider: str | None = None,
        cited_urls: list[str] | None = None,
    ) -> Path:
        target_dir = self._engagement_dir(engagement_id) / "abilities"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{ability_id}.json"
        payload = {
            "ability_id": ability_id,
            "skill": skill,
            "saved_at": now_iso(),
            "ability": _serialise(ability),
            "stages": _serialise(stages),
            "rationale": rationale,
            "provider": provider,
            "cited_urls": cited_urls or [],
        }
        return self._atomic_write(target, payload)

    def write_adversary(
        self,
        engagement_id: str,
        *,
        adversary_id: str,
        skill: str,
        adversary: Any,
        linked_ability_ids: list[str],
    ) -> Path:
        target_dir = self._engagement_dir(engagement_id) / "adversaries"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{adversary_id}.json"
        payload = {
            "adversary_id": adversary_id,
            "skill": skill,
            "saved_at": now_iso(),
            "adversary": _serialise(adversary),
            "linked_ability_ids": list(linked_ability_ids),
        }
        return self._atomic_write(target, payload)

    def _atomic_write(self, target: Path, payload: dict[str, Any]) -> Path:
        tmp = target.with_suffix(target.suffix + ".tmp")
        with self._lock:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        return target

    @property
    def root(self) -> Path:
        return self._root
