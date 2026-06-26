"""Central configuration.

Single source of truth: `config/config.yaml` (or `$BAS_CONFIG`).

Design rules:
    - One YAML file controls everything (bas client, llm provider, run output, logging).
    - Secrets and runtime tunables live in env vars, referenced from YAML using
      `${VAR}` or `${VAR:-default}` syntax (docker-compose style).
    - All settings are validated by pydantic; unknown keys raise on load.
    - Anything that may legitimately be missing has a default here — the YAML
      file only needs to override what differs from defaults.

Usage:
    from bas.config import AppConfig
    cfg = AppConfig.load()                       # auto-discovers
    cfg = AppConfig.load("config/config.yaml")   # explicit path

    client = BasClient.from_config(cfg.bas)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ----------------------------------------------------------------------------
# YAML loading + ${VAR} / ${VAR:-default} interpolation
# ----------------------------------------------------------------------------

_INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_DEFAULT_SEARCH_PATHS = (
    "config/config.yaml",
    "config/config.yml",
    "bas.config.yaml",
)
_CONFIG_ENV_VAR = "BAS_CONFIG"


def _interpolate(value: Any) -> Any:
    """Recursively substitute `${VAR}` and `${VAR:-default}` against `os.environ`."""
    if isinstance(value, str):

        def _repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")

        replaced = _INTERP_RE.sub(_repl, value)
        # Treat empty strings produced by missing vars without defaults as null.
        return None if replaced == "" and value != "" else replaced
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def _find_default_path() -> Path | None:
    env_path = os.environ.get(_CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path)
    for rel in _DEFAULT_SEARCH_PATHS:
        p = Path(rel)
        if p.is_file():
            return p
    return None


# ----------------------------------------------------------------------------
# Section schemas
# ----------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BasEnvironmentConfig(StrictModel):
    """If both id and name are null, the resolver falls back to most-recent."""

    id: str | None = None
    name: str | None = None


class BasConfig(StrictModel):
    base_url: str = "http://pelorushub.online:31763"
    sleep_ms: int = Field(default=250, ge=0)
    timeout: float = Field(default=30.0, gt=0)
    dry_run: bool = False
    environment: BasEnvironmentConfig = Field(default_factory=BasEnvironmentConfig)


class GroundingConfig(StrictModel):
    max_grounded_calls_per_run: int = Field(default=40, ge=0)
    default_depth: str = "light"

    @field_validator("default_depth")
    @classmethod
    def _check_depth(cls, v: str) -> str:
        if v not in {"skip", "light", "deep"}:
            raise ValueError("grounding.default_depth must be skip | light | deep")
        return v


class LlmConfig(StrictModel):
    provider: str = "gemini"
    model: str = "gemini-3.5-flash"
    classifier_model: str = "gemini-3.5-flash"
    api_key_env: str = "GEMINI_API_KEY"
    temperature: float | None = Field(default=None, ge=0.0, le=2.0, description="Leave None to use Gemini 3.x defaults")
    thinking_level: str = Field(default="high", description="Thinking level: off, low, medium, high")
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        if v not in {"gemini", "anthropic", "openai"}:
            raise ValueError("llm.provider must be gemini | anthropic | openai")
        return v

    @field_validator("thinking_level")
    @classmethod
    def _check_thinking(cls, v: str) -> str:
        if v not in {"off", "minimal", "low", "medium", "high"}:
            raise ValueError("llm.thinking_level must be off | minimal | low | medium | high")
        return v


class RunConfig(StrictModel):
    output_dir: str = "runs"
    log_level: str = "INFO"


class ExecutionConfig(StrictModel):
    """Controls graph checkpointing and result-wait behaviour."""

    result_wait_timeout: int = Field(default=6000, ge=0, description="Soft wait window (s). Past this an engagement is 'overdue' but stays parked — late results still resume it. Backend commands can run long.")
    result_hard_timeout: int = Field(default=0, ge=0, description="Hard cap (s) after which the scanner force-abandons a still-waiting engagement (dead-agent backstop). 0 = no hard cap (wait indefinitely for results / manual cancel). Should be > result_wait_timeout.")
    result_poll_enabled: bool = Field(default=True, description="Pull results by polling GET /operations/{id} instead of only waiting for the /results webhook.")
    result_poll_interval: int = Field(default=40, ge=30, description="Seconds between operation-detail polls (default 7 min).")
    max_result_size_mb: int = Field(default=10, ge=1)
    # Default to sqlite so engagements paused at `interrupt("awaiting_results")`
    # survive a process restart. The in-memory saver orphans any paused run.
    checkpointer: Literal["memory", "sqlite"] = "sqlite"
    checkpoint_db: str = "runs/checkpoints.db"


class KaliConfig(StrictModel):
    """Connection settings for the Kali toolbox sidecar."""

    base_url: str = "http://kali-toolbox:9000"
    timeout: float = Field(default=300.0, gt=0)
    connect_timeout: float = Field(default=10.0, gt=0)
    enabled: bool = False


# ----------------------------------------------------------------------------
# Top-level config
# ----------------------------------------------------------------------------


class AppConfig(StrictModel):
    bas: BasConfig = Field(default_factory=BasConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    kali: KaliConfig = Field(default_factory=KaliConfig)

    # Source path (debug/auditing). Excluded from `extra=forbid` because it's set
    # post-construction via `model_copy(update=...)`.
    source_path: str | None = Field(default=None)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        """Load + validate config. Returns defaults if no file is found."""
        resolved = Path(path) if path else _find_default_path()
        if resolved is None:
            return cls()
        if not resolved.is_file():
            raise FileNotFoundError(f"config file not found: {resolved}")
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        interpolated = _interpolate(raw)
        cfg = cls.model_validate(interpolated)
        return cfg.model_copy(update={"source_path": str(resolved.resolve())})
