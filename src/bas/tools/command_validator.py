"""Shell-native command syntax validation.

Uses actual shell parsers via subprocess to catch syntax errors **before**
commands are pushed to the BAS backend.  Parse-only — nothing is executed.

    PowerShell:  [System.Management.Automation.Language.Parser]::ParseInput()
    Bash / sh:   bash -n -c '<command>'
    Cmd:         regex heuristics (no parse-only mode exists)

Falls back gracefully when the required shell binary is not available on the
orchestrator host (e.g. validating PowerShell from a Linux orchestrator
without ``pwsh`` installed).

Zero external dependencies — stdlib only (``subprocess``, ``shutil``, ``shlex``).
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import AbilityStageCreate

logger = logging.getLogger(__name__)

# Maximum time (seconds) for any subprocess call.  Prevents hangs on
# pathological input like deeply nested loops.
_SUBPROCESS_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Result dataclass (mirrors llm.base.CommandValidation for interop)
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome for a single command_template validation."""

    name: str
    executor: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PowerShell validator
# ---------------------------------------------------------------------------

# Locate the PowerShell binary once at import time so subsequent calls skip
# the PATH scan.  Prefer ``pwsh`` (cross-platform PS 7+) over ``powershell``
# (Windows-only PS 5.1).
_PS_BIN: str | None = shutil.which("pwsh") or shutil.which("powershell")


def _validate_powershell(command: str, name: str) -> ValidationResult:
    """Parse a PowerShell command using the .NET Parser — zero execution."""
    result = ValidationResult(name=name, executor="psh", valid=True)

    if not _PS_BIN:
        result.warnings.append(
            "PowerShell binary not found on orchestrator — syntax check skipped"
        )
        return result

    # We pass the command via a temp file rather than inline to avoid
    # quote-escaping issues between Python → shell → PowerShell.
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ps1",
            delete=False,
            encoding="utf-8",
        )
        tmp.write(command)
        tmp.close()

        # ParseFile returns (tokens, errors).  We only care about errors.
        ps_script = (
            "$errors = $null; "
            "[void][System.Management.Automation.Language.Parser]"
            f"::ParseFile('{tmp.name}', [ref]$null, [ref]$errors); "
            "foreach ($e in $errors) { "
            "  Write-Output (\"PARSE_ERROR:\" + $e.Message + \"|\" + $e.Extent.StartLineNumber + \":\" + $e.Extent.StartColumnNumber) "
            "}"
        )

        proc = subprocess.run(
            [_PS_BIN, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )

        # Collect parse errors from stdout
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("PARSE_ERROR:"):
                result.valid = False
                result.errors.append(line[len("PARSE_ERROR:"):])

        # If the PS process itself failed (e.g., bad script), report stderr
        if proc.returncode != 0 and not result.errors:
            stderr = proc.stderr.strip()
            if stderr:
                result.valid = False
                result.errors.append(stderr[:500])

    except subprocess.TimeoutExpired:
        result.warnings.append("PowerShell parser timed out — skipped")
    except OSError as exc:
        result.warnings.append(f"PowerShell parser unavailable: {exc}")
    finally:
        if tmp is not None:
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except OSError:
                pass

    return result


# ---------------------------------------------------------------------------
# Bash / sh validator
# ---------------------------------------------------------------------------

_BASH_BIN: str | None = shutil.which("bash")
_SH_BIN: str | None = shutil.which("sh")


def _validate_bash(command: str, name: str, executor: str = "bash") -> ValidationResult:
    """Syntax-check a bash/sh command with ``-n`` (no execution)."""
    result = ValidationResult(name=name, executor=executor, valid=True)

    shell = _BASH_BIN if executor == "bash" else (_SH_BIN or _BASH_BIN)
    if not shell:
        result.warnings.append(
            f"{executor} binary not found on orchestrator — syntax check skipped"
        )
        return result

    try:
        proc = subprocess.run(
            [shell, "-n", "-c", command],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )

        if proc.returncode != 0:
            result.valid = False
            stderr = proc.stderr.strip()
            if stderr:
                # bash -n outputs errors like:
                #   bash: -c: line 1: syntax error near unexpected token `('
                for line in stderr.splitlines():
                    result.errors.append(line.strip()[:300])
            else:
                result.errors.append(f"exit code {proc.returncode} (no details)")

    except subprocess.TimeoutExpired:
        result.warnings.append("bash syntax check timed out — skipped")
    except OSError as exc:
        result.warnings.append(f"bash unavailable: {exc}")

    return result


# ---------------------------------------------------------------------------
# Cmd validator (heuristic — cmd has no parse-only mode)
# ---------------------------------------------------------------------------

# Patterns that indicate common cmd syntax problems.
_CMD_UNBALANCED_PARENS = re.compile(r"[()]")
_CMD_PIPE_DANGLING = re.compile(r"\|\s*$")
_CMD_REDIRECT_DANGLING = re.compile(r"[<>]\s*$")


def _validate_cmd(command: str, name: str) -> ValidationResult:
    """Heuristic syntax check for cmd.exe commands."""
    result = ValidationResult(name=name, executor="cmd", valid=True)

    # 1. Balanced parentheses
    depth = 0
    for ch in command:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth < 0:
            break
    if depth != 0:
        result.valid = False
        result.errors.append(
            f"Unbalanced parentheses (depth={depth})"
        )

    # 2. Balanced double quotes
    quote_count = command.count('"')
    if quote_count % 2 != 0:
        result.valid = False
        result.errors.append(
            f"Unbalanced double quotes ({quote_count} found)"
        )

    # 3. Dangling pipe or redirect at end
    stripped = command.rstrip()
    if _CMD_PIPE_DANGLING.search(stripped):
        result.valid = False
        result.errors.append("Dangling pipe at end of command")
    if _CMD_REDIRECT_DANGLING.search(stripped):
        result.valid = False
        result.errors.append("Dangling redirect at end of command")

    return result


# ---------------------------------------------------------------------------
# Universal pre-checks (all executors)
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r"#\{[^}]+\}"            # #{network.cidr}
    r"|<TARGET>"             # <TARGET>
    r"|<PLACEHOLDER[^>]*>"   # <PLACEHOLDER_IP>
    r"|\{\{[^}]+\}\}",       # {{variable}}
    re.IGNORECASE,
)


def _check_universal(command: str, name: str) -> list[str]:
    """Return warnings for issues common to all executors."""
    warnings: list[str] = []

    if not command or not command.strip():
        warnings.append("Empty command_template")
        return warnings

    # Placeholder tokens that will never be substituted at runtime
    placeholders = _PLACEHOLDER_RE.findall(command)
    if placeholders:
        warnings.append(
            f"Unresolved placeholder tokens: {', '.join(placeholders)}"
        )

    return warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Executor aliases → canonical executor name
_EXECUTOR_MAP: dict[str, str] = {
    "psh": "psh",
    "powershell": "psh",
    "pwsh": "psh",
    "bash": "bash",
    "sh": "sh",
    "cmd": "cmd",
}


def validate_command(
    command: str,
    *,
    executor: str = "psh",
    name: str = "",
) -> ValidationResult:
    """Validate a single command_template string.

    Returns a ``ValidationResult`` with ``valid=True`` when no syntax errors
    are detected.  Warnings (e.g., placeholder tokens) are non-blocking.
    """
    canonical = _EXECUTOR_MAP.get((executor or "").lower().strip(), "")
    result: ValidationResult

    if canonical == "psh":
        result = _validate_powershell(command, name)
    elif canonical in ("bash", "sh"):
        result = _validate_bash(command, name, executor=canonical)
    elif canonical == "cmd":
        result = _validate_cmd(command, name)
    else:
        result = ValidationResult(
            name=name,
            executor=executor or "unknown",
            valid=True,
            warnings=[f"Unknown executor {executor!r} — syntax check skipped"],
        )

    # Append universal warnings (placeholders etc.)
    result.warnings.extend(_check_universal(command, name))

    return result


def validate_stages(
    stages: list[AbilityStageCreate],
    *,
    ability_name: str = "",
) -> list[ValidationResult]:
    """Validate every stage in a list.

    Parameters
    ----------
    stages:
        ``AbilityStageCreate`` objects from a ``GeneratedAbility``.
    ability_name:
        Human-readable ability name prefixed to each result for logging.

    Returns
    -------
    List of ``ValidationResult`` — one per stage.
    """
    results: list[ValidationResult] = []
    for stage in stages:
        label = f"{ability_name}/{stage.stage_name}" if ability_name else stage.stage_name
        cmd = stage.command_template or ""
        r = validate_command(cmd, executor=stage.executor or "psh", name=label)
        results.append(r)
    return results


def validate_plan(plan) -> list[ValidationResult]:
    """Validate every command in a ``SpecialistPlan``.

    Parameters
    ----------
    plan:
        A ``SpecialistPlan`` (imported lazily to avoid circular imports).

    Returns
    -------
    All ``ValidationResult`` objects.  Caller can filter on ``valid=False``.
    """
    all_results: list[ValidationResult] = []
    for gen in plan.abilities:
        all_results.extend(
            validate_stages(gen.stages, ability_name=gen.ability.name)
        )
    return all_results


def format_errors(results: list[ValidationResult]) -> str:
    """Build a human-readable error report from validation results.

    Only includes stages that have errors or warnings.
    """
    lines: list[str] = []
    for r in results:
        if not r.errors and not r.warnings:
            continue
        header = f"  [{r.executor}] {r.name}:"
        for e in r.errors:
            lines.append(f"{header} ERROR: {e}")
        for w in r.warnings:
            lines.append(f"{header} WARN: {w}")
    return "\n".join(lines) if lines else ""
