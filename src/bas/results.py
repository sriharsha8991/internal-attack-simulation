"""Result parsing, issue detection, and structural summarisation.

Pure Python — no LLM calls.  Accepts raw JSON from the BAS backend webhook
and produces typed models + a compact text summary for the master agent.

Usage::

    from bas.results import parse_operation_result, detect_issues, build_structural_summary

    op = parse_operation_result(raw_json)
    issues = detect_issues(op, stage_id_map)
    summary = build_structural_summary(op, issues)
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class StageExecution(BaseModel):
    """One stage within an executed ability."""

    stage_name: str
    executor: str = ""
    command_executed: str = ""
    execution_status: Literal["passed", "failed"] = "failed"
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    timestamp: datetime | None = None


class AbilityResult(BaseModel):
    """Execution outcome for a single ability (all its stages)."""

    ability_id: str
    name: str = ""
    mitre_technique_id: str | None = None
    platform: str = ""
    stages: list[StageExecution] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when every stage exited with code 0."""
        return bool(self.stages) and all(s.exit_code == 0 for s in self.stages)

    @property
    def failed(self) -> bool:
        """True when any stage exited with a non-zero code."""
        return any(s.exit_code != 0 for s in self.stages)


class OperationResult(BaseModel):
    """Top-level result of a backend operation execution."""

    operation_id: str
    operation_name: str = ""
    operation_status: Literal["completed", "failed", "partial"] = "completed"
    completed_at: datetime | None = None
    adversary: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] = Field(default_factory=dict)
    abilities: list[AbilityResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Issue detection (code-based, deterministic)
# ---------------------------------------------------------------------------


class IssueKind(str, Enum):
    """Categories of problems detectable without LLM reasoning."""

    PLACEHOLDER_TOKEN = "placeholder_token"
    TOOL_NOT_FOUND = "tool_not_found"
    TIMEOUT = "timeout"
    CROSS_VAR_LEAK = "cross_var_leak"
    PSH_PARSE_ERROR = "psh_parse_error"


class StageIssue(BaseModel):
    """A specific problem found in one stage of an executed ability."""

    ability_id: str
    ability_name: str
    stage_id: str
    stage_name: str
    kind: IssueKind
    detail: str


# Detection patterns (compiled once)
_PLACEHOLDER_RE = re.compile(
    r"#\{[^}]+\}"            # #{network.cidr}
    r"|<TARGET>"             # <TARGET>
    r"|\{\{[^}]+\}\}"        # {{variable}}
    r"|<PLACEHOLDER[^>]*>",  # <PLACEHOLDER_IP>
    re.IGNORECASE,
)
_TOOL_NOT_FOUND_RE = re.compile(
    r"is not recognized as an? "
    r"|command not found"
    r"|not found in PATH"
    r"|cannot be loaded because running scripts is disabled"
    r"|'(\w+)' is not recognized",
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(r"timed?\s*out|deadline exceeded", re.IGNORECASE)
_PSH_PARSE_RE = re.compile(
    r"Missing argument in parameter list"
    r"|Missing expression after"
    r"|unexpected token"
    r"|FullyQualifiedErrorId\s*:\s*MissingArgument",
    re.IGNORECASE,
)
_CROSS_VAR_RE = re.compile(
    r"\$(?:env:)?[A-Za-z_]\w*"   # $var or $env:var (PowerShell)
    r"|\$\([^)]+\)",             # $(command) subshell
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _adapt_operation_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a ``GET /operations/{id}`` detail payload to the webhook shape.

    The webhook (``OperationResultRequest``) nests metadata under ``operation``
    and carries ``abilities`` (stage *definitions*) alongside ``execution_logs``.
    The operations-detail endpoint instead puts ``operation_id``/``status`` at the
    top level and ships ONLY ``execution_logs`` (no ``abilities``). When we detect
    that shape, synthesise the ``operation`` wrapper and rebuild ``abilities`` from
    the logs so :func:`parse_operation_result` works unchanged. Webhook payloads
    (which already have ``operation``/``abilities``) pass through untouched.
    """
    if raw.get("operation") and raw.get("abilities"):
        return raw  # already the webhook shape
    logs = raw.get("execution_logs")
    if not logs or raw.get("abilities"):
        return raw  # nothing to synthesise from / definitions already present

    # Group logs by ability_id; each distinct stage_id becomes a stage. The
    # command_template is taken from the executed command so detect_issues and
    # the structural summary still see the command text.
    by_ability: dict[str, list[dict[str, Any]]] = {}
    for log in logs:
        by_ability.setdefault(str(log.get("ability_id") or ""), []).append(log)

    abilities: list[dict[str, Any]] = []
    for ab_id, ab_logs in by_ability.items():
        stages = [
            {
                "stage_id": str(lg.get("stage_id") or ""),
                "stage_name": str(lg.get("stage_id") or ""),
                "executor": lg.get("executor") or "",
                "command_template": lg.get("command_executed") or "",
            }
            for lg in ab_logs
        ]
        abilities.append({"ability_id": ab_id, "name": ab_id, "stages": stages})

    return {
        **raw,
        "operation": {
            "operation_id": raw.get("operation_id"),
            "name": raw.get("name"),
            "status": raw.get("status"),
            "started_at": raw.get("started_at"),
            "completed_at": raw.get("completed_at"),
            "kill_switch_triggered": raw.get("kill_switch_triggered"),
        },
        "abilities": abilities,
        "execution_logs": logs,
    }


def parse_operation_result(raw: dict[str, Any]) -> OperationResult:
    """Validate and parse raw backend JSON into a typed model.

    Accepts both the ``/results`` webhook payload and the ``GET /operations/{id}``
    detail payload (the latter is normalised by :func:`_adapt_operation_detail`).

    - ``operation``: nested metadata (operation_id, status, completed_at, ...)
    - ``abilities``: stage definitions (stage_id, command_template, ...)
    - ``execution_logs``: actual output (stdout, stderr, exit_code, ...)

    This function merges execution_logs into abilities by matching
    (ability_id, stage_id) so downstream code sees a single unified model
    with stdout/stderr on each stage.
    """
    raw = _adapt_operation_detail(raw)
    # Handle the nested operation structure from the backend.
    op_info = raw.get("operation") or {}
    operation_id = op_info.get("operation_id") or raw.get("operation_id", "")
    operation_name = op_info.get("name") or raw.get("operation_name", "")
    operation_status = op_info.get("status") or raw.get("operation_status", "completed")
    completed_at = op_info.get("completed_at") or raw.get("completed_at")

    # Build execution_logs index: (ability_id, stage_id) -> log entry
    exec_logs = raw.get("execution_logs") or []
    log_index: dict[tuple[str, str], dict[str, Any]] = {}
    for log in exec_logs:
        ab_id = str(log.get("ability_id") or "")
        st_id = str(log.get("stage_id") or "")
        if ab_id and st_id:
            log_index[(ab_id, st_id)] = log

    # Merge abilities + execution_logs into AbilityResult entries.
    abilities: list[AbilityResult] = []
    for ab_raw in raw.get("abilities") or []:
        ab_id = str(ab_raw.get("ability_id") or "")
        stages: list[StageExecution] = []

        for stg_raw in ab_raw.get("stages") or []:
            st_id = str(stg_raw.get("stage_id") or "")
            log_entry = log_index.get((ab_id, st_id), {})

            # Merge: stage definition provides structure; execution_log
            # provides actual output.
            exit_code = log_entry.get("exit_code", -1)
            stages.append(StageExecution(
                stage_name=stg_raw.get("stage_name") or st_id,
                executor=log_entry.get("executor") or stg_raw.get("executor") or "",
                command_executed=log_entry.get("command_executed") or stg_raw.get("command_template") or "",
                execution_status="passed" if exit_code == 0 else "failed",
                stdout=log_entry.get("stdout") or "",
                stderr=log_entry.get("stderr") or "",
                exit_code=exit_code,
                timestamp=log_entry.get("timestamp"),
            ))

        abilities.append(AbilityResult(
            ability_id=ab_id,
            name=ab_raw.get("name") or "",
            mitre_technique_id=ab_raw.get("mitre_technique_id"),
            platform=ab_raw.get("platform") or "",
            stages=stages,
        ))

    # Normalise operation_status to allowed literals
    if operation_status not in ("completed", "failed", "partial"):
        operation_status = "completed"

    return OperationResult(
        operation_id=str(operation_id),
        operation_name=str(operation_name),
        operation_status=operation_status,
        completed_at=completed_at,
        adversary=raw.get("adversary") or {},
        progress=raw.get("progress") or {},
        abilities=abilities,
    )


def detect_issues(
    result: OperationResult,
    stage_id_map: dict[str, dict[str, str]] | None = None,
) -> list[StageIssue]:
    """Scan all stages for known issue patterns.

    Parameters
    ----------
    result:
        Parsed operation result.
    stage_id_map:
        ``{ability_name -> {stage_name -> stage_id}}``.  If ``None``, stage_id
        falls back to ``ability_id`` (less precise but still functional).
    """
    stage_id_map = stage_id_map or {}
    issues: list[StageIssue] = []

    for ab in result.abilities:
        ab_stages = stage_id_map.get(ab.name, {})
        for stage in ab.stages:
            sid = ab_stages.get(stage.stage_name, ab.ability_id)

            # Placeholder tokens in the command itself
            placeholders = _PLACEHOLDER_RE.findall(stage.command_executed)
            if placeholders:
                issues.append(StageIssue(
                    ability_id=ab.ability_id,
                    ability_name=ab.name,
                    stage_id=sid,
                    stage_name=stage.stage_name,
                    kind=IssueKind.PLACEHOLDER_TOKEN,
                    detail=f"unresolved tokens: {', '.join(placeholders)}",
                ))

            # Tool / command not found in stderr
            if _TOOL_NOT_FOUND_RE.search(stage.stderr):
                issues.append(StageIssue(
                    ability_id=ab.ability_id,
                    ability_name=ab.name,
                    stage_id=sid,
                    stage_name=stage.stage_name,
                    kind=IssueKind.TOOL_NOT_FOUND,
                    detail=f"stderr: {stage.stderr[:200]}",
                ))

            # Timeout (exit_code == -1 or explicit message)
            if stage.exit_code == -1 or _TIMEOUT_RE.search(stage.stderr):
                issues.append(StageIssue(
                    ability_id=ab.ability_id,
                    ability_name=ab.name,
                    stage_id=sid,
                    stage_name=stage.stage_name,
                    kind=IssueKind.TIMEOUT,
                    detail="exit_code=-1" if stage.exit_code == -1
                           else f"stderr: {stage.stderr[:200]}",
                ))

            # PowerShell parse error (commas treated as arg separators, etc.)
            if stage.exit_code != 0 and _PSH_PARSE_RE.search(stage.stderr):
                issues.append(StageIssue(
                    ability_id=ab.ability_id,
                    ability_name=ab.name,
                    stage_id=sid,
                    stage_name=stage.stage_name,
                    kind=IssueKind.PSH_PARSE_ERROR,
                    detail=f"stderr: {stage.stderr[:200]}",
                ))

            # Cross-ability variable leak (likely empty at runtime)
            if stage.exit_code != 0:
                var_refs = _CROSS_VAR_RE.findall(stage.command_executed)
                if var_refs:
                    issues.append(StageIssue(
                        ability_id=ab.ability_id,
                        ability_name=ab.name,
                        stage_id=sid,
                        stage_name=stage.stage_name,
                        kind=IssueKind.CROSS_VAR_LEAK,
                        detail=f"likely empty vars: {', '.join(var_refs)}",
                    ))

    return issues


def derive_phase_done(
    op_result: OperationResult,
    issues: list,
) -> bool:
    """Decide whether the phase is complete based on execution results.

    The phase is done when:
      1. The operation didn't fail at the infrastructure level.
      2. At least one ability passed.
      3. No critical issues (placeholder tokens, tool-not-found, parse errors).
      4. A majority of abilities passed — a single passing ability amid
         mostly-failed ones is not enough to call the phase complete.
    """
    total = len(op_result.abilities)
    if total == 0:
        return False

    if op_result.operation_status == "failed":
        return False

    passed_count = sum(1 for a in op_result.abilities if a.passed)
    if passed_count == 0:
        return False

    critical_kinds = {
        IssueKind.PLACEHOLDER_TOKEN,
        IssueKind.TOOL_NOT_FOUND,
        IssueKind.PSH_PARSE_ERROR,
    }
    critical_issues = [i for i in issues if i.kind in critical_kinds]
    if critical_issues:
        return False

    if total > 1 and passed_count < (total / 2):
        return False

    return True


def build_structural_summary(
    result: OperationResult,
    issues: list[StageIssue],
) -> str:
    """Produce a compact, deterministic text summary for the master agent.

    The summary contains structural facts only — no raw stdout. The master
    requests specific stdout via ``read_stage_output()`` when it needs it.
    """
    # Build issue index: (ability_id, stage_name) -> list[IssueKind]
    issue_idx: dict[tuple[str, str], list[str]] = {}
    for iss in issues:
        key = (iss.ability_id, iss.stage_name)
        issue_idx.setdefault(key, []).append(iss.kind.value)

    passed_count = sum(1 for a in result.abilities if a.passed)
    total = len(result.abilities)

    lines: list[str] = []
    op_short = result.operation_id[:8] if len(result.operation_id) >= 8 else result.operation_id
    lines.append(
        f"OP: {op_short} | STATUS: {result.operation_status} | "
        f"{passed_count}/{total} abilities passed"
    )
    lines.append("")

    for ab in result.abilities:
        tag = "PASS" if ab.passed else "FAIL"
        tech = ab.mitre_technique_id or "?"
        lines.append(f"[{tag}] {ab.name} ({tech}, {ab.platform})")
        for i, st in enumerate(ab.stages, 1):
            markers: list[str] = []
            for ik in issue_idx.get((ab.ability_id, st.stage_name), []):
                markers.append(f"⚠ {ik}")
            marker_str = " ".join(markers)
            parts = [f"  {i}. {st.stage_name} → exit={st.exit_code}"]
            if st.stdout.strip():
                # Inline a short preview so the LLM triage step can extract
                # facts without needing a second read_stage_output round-trip.
                preview = " | ".join(
                    ln.strip()
                    for ln in st.stdout.strip().splitlines()
                    if ln.strip()
                )[:400]
                parts.append(f'stdout: "{preview}"')
            elif st.stderr.strip():
                preview = st.stderr.strip()[:200].replace("\n", " ")
                parts.append(f'stderr: "{preview}"')
            if marker_str:
                parts.append(marker_str)
            lines.append(" ".join(parts))

    # Issue tally
    if issues:
        counts = Counter(i.kind.value for i in issues)
        tally = ", ".join(f"{k}({v})" for k, v in sorted(counts.items()))
        lines.append("")
        lines.append(f"ISSUES: {tally}")

    return "\n".join(lines)
