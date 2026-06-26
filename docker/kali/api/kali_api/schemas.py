"""Pydantic request/response schemas for the Kali sidecar API."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── exec ──────────────────────────────────────────────────────────────────────

class ExecRequest(BaseModel):
    command: str = Field(..., max_length=8192)
    timeout: int = Field(default=120, ge=1, le=3600)
    cwd: str | None = None


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


# ── file transfer ─────────────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    path: str
    size: int


# ── key parsing ───────────────────────────────────────────────────────────────

class KeyParseRequest(BaseModel):
    path: str | None = None
    content: str | None = None  # base64-encoded
    parse_type: str = Field(default="auto", pattern="^(auto|ssh|kerberos|certificate)$")


class ParsedCredential(BaseModel):
    type: str
    identity: str | None = None
    details: dict = Field(default_factory=dict)


class KeyParseResponse(BaseModel):
    credentials: list[ParsedCredential] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ── BloodHound ingestion ─────────────────────────────────────────────────────

class BloodHoundIngestRequest(BaseModel):
    domain: str
    dc_ip: str
    username: str | None = None
    password: str | None = None
    hashes: str | None = None
    collection_method: str = "Default"
    output_dir: str = "/tmp/bloodhound"


class BloodHoundIngestResponse(BaseModel):
    output_files: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1


# ── tools ─────────────────────────────────────────────────────────────────────

class ToolInfo(BaseModel):
    name: str
    path: str
    version: str | None = None
    category: str | None = None


class ToolsResponse(BaseModel):
    tools: list[ToolInfo] = Field(default_factory=list)


# ── health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    tools_loaded: int = 0
