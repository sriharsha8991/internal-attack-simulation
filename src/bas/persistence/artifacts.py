"""Per-engagement artifact store for pushed ability and adversary specs.

Layout::

    <root>/<engagement_id>/abilities/<ability_id>.json
    <root>/<engagement_id>/adversaries/<adversary_id>.json

Each JSON file records the exact spec POSTed to BAS — the local
source-of-truth for "what did the LLM tell the backend to do?"
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ._serialise import serialise
from .runs import now_iso


class ArtifactStore:
    """Writes one JSON file per created ability and adversary, on disk."""

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
            "ability": serialise(ability),
            "stages": serialise(stages),
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
            "adversary": serialise(adversary),
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
