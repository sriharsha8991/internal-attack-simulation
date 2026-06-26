"""Skills catalog audit logger.

Logs all manual Markdown modifications to `runs/skills_audit_log.json`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_audit_log_path() -> Path:
    """Resolve the persistent audit log file path dynamically from the bootstrap store."""
    from ..bootstrap import _bootstrap
    try:
        _, _, store, _ = _bootstrap()
        runs_dir = Path(store.root)
    except Exception:
        # Fallback to current workspace runs/ or engagements/
        runs_dir = Path.cwd() / "engagements"

    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir / "skills_audit_log.json"


def log_skill_modification(
    username: str,
    skill_name: str,
    file_type: str,
    action: str,
    details: str = "",
) -> None:
    """Append an edit record atomically to the skills audit log JSON database."""
    log_path = get_audit_log_path()
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "username": username.strip() or "anonymous",
        "skill": skill_name,
        "file": file_type,
        "action": action,
        "details": details.strip(),
    }

    try:
        # Read existing records
        if log_path.is_file():
            try:
                with log_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, list):
                        data = []
            except Exception:
                data = []
        else:
            data = []

        data.append(entry)

        # Write atomically
        temp_path = log_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_path.replace(log_path)

        logger.info(
            "[audit] logged skill edit skill=%s file=%s user=%s",
            skill_name, file_type, username
        )
    except Exception as exc:
        logger.error("[audit] failed to write skill edit log: %s", exc)


def read_audit_log() -> list[dict]:
    """Retrieve all logged skill modification records from disk."""
    log_path = get_audit_log_path()
    if not log_path.is_file():
        return []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return sorted(data, key=lambda x: x.get("timestamp", ""), reverse=True)
    except Exception:
        pass
    return []
