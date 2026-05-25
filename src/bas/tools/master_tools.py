"""Plain-Python tools for master-level result inspection.

No LLM calls — these functions read result files from disk and return
text summaries the master can feed into its analyse prompt.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def list_operations(results_dir: str) -> list[str]:
    """Return all operation_ids with saved results in this engagement."""
    d = Path(results_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


# ---------------------------------------------------------------------------
# Cached loader — avoids repeated disk I/O, JSON parse, and Pydantic
# validation when the master calls multiple tools on the same operation.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=16)
def _load_and_parse(results_dir: str, operation_id: str) -> tuple[dict[str, Any], Any]:
    """Load + parse once, cache the result.

    Returns ``(raw_dict, OperationResult | None)``.  The cache is bounded
    to the 16 most-recent operations so memory stays under control.
    """
    from ..results import parse_operation_result

    path = Path(results_dir) / f"{operation_id}.json"
    if not path.is_file():
        return {}, None
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw, parse_operation_result(raw)


def invalidate_cache(results_dir: str | None = None, operation_id: str | None = None) -> None:
    """Drop cached entries — call after new result files land on disk."""
    if results_dir and operation_id:
        try:
            _load_and_parse.cache_clear()  # targeted eviction unsupported; clear all
        except Exception:  # noqa: BLE001
            pass
    else:
        _load_and_parse.cache_clear()


def read_structural_summary(results_dir: str, operation_id: str) -> str:
    """Load raw result JSON and return a compact structural summary.

    Uses the same logic as ``results.build_structural_summary`` but works
    from the on-disk file rather than an in-memory object.
    """
    _, op = _load_and_parse(results_dir, operation_id)
    if op is None:
        return f"[no result file found for operation {operation_id}]"

    from ..results import build_structural_summary, detect_issues

    issues = detect_issues(op, {})
    return build_structural_summary(op, issues)


def read_stage_output(
    results_dir: str,
    operation_id: str,
    ability_name: str,
    stage_name: str | None = None,
) -> str:
    """Return stdout + stderr for a specific ability/stage.

    If ``stage_name`` is None, returns all stages for the given ability.
    """
    _, op = _load_and_parse(results_dir, operation_id)
    if op is None:
        return f"[no result file for operation {operation_id}]"

    # Index abilities by lowered name for O(1) lookup instead of O(A) scan
    key = ability_name.lower()
    matching = [ab for ab in op.abilities if ab.name.lower() == key]
    if not matching:
        return f"[no matching ability: ability={ability_name!r}]"

    stage_key = stage_name.lower() if stage_name else None
    lines: list[str] = []

    for ab in matching:
        for stg in ab.stages:
            if stage_key and stg.stage_name.lower() != stage_key:
                continue
            lines.append(
                f"--- {ab.name} / {stg.stage_name} "
                f"(exit={stg.exit_code}, executor={stg.executor}) ---"
            )
            stdout = stg.stdout.strip()
            stderr = stg.stderr.strip()
            if stdout:
                lines.append(f"STDOUT:\n{stdout}")
            if stderr:
                lines.append(f"STDERR:\n{stderr}")
            if not stdout and not stderr:
                lines.append("(no output)")
            lines.append("")

    if not lines:
        return f"[no matching stage: ability={ability_name!r} stage={stage_name!r}]"
    return "\n".join(lines)


def grep_results(results_dir: str, operation_id: str, pattern: str) -> str:
    """Regex search across all stdout/stderr. Returns matching lines with context."""
    _, op = _load_and_parse(results_dir, operation_id)
    if op is None:
        return f"[no result file for operation {operation_id}]"

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"[invalid regex: {exc}]"

    matches: list[str] = []
    cap = 200

    for ab in op.abilities:
        for stg in ab.stages:
            prefix = f"{ab.name}/{stg.stage_name}"
            for field_name, text in (("stdout", stg.stdout), ("stderr", stg.stderr)):
                # Fast pre-check: skip splitlines entirely if no match in blob
                if not regex.search(text):
                    continue
                for line_num, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{prefix}:{field_name}:{line_num}: {line.rstrip()}")
                        if len(matches) >= cap:
                            return "\n".join(matches)

    if not matches:
        return f"[no matches for /{pattern}/ across operation {operation_id}]"
    return "\n".join(matches)
