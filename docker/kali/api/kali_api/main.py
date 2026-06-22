"""Kali toolbox sidecar REST API.

Lightweight FastAPI service running inside the Kali container.  Exposes
command execution, file transfer, key parsing, BloodHound ingestion, and
tool inventory over HTTP on the Docker internal network.
"""

from __future__ import annotations

import asyncio
import base64
import glob as globmod
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from .schemas import (
    BloodHoundIngestRequest,
    BloodHoundIngestResponse,
    ExecRequest,
    ExecResponse,
    HealthResponse,
    KeyParseRequest,
    KeyParseResponse,
    ParsedCredential,
    ToolInfo,
    ToolsResponse,
    UploadResponse,
)

logger = logging.getLogger("kali_api")

app = FastAPI(title="Kali Toolbox Sidecar", version="1.0.0")

ALLOWED_ROOTS = ["/tmp", "/opt/tools", "/data", "/usr/share/wordlists", "/root"]
_TOOL_REGISTRY: list[ToolInfo] = []

# Shell metacharacter blocklist (defense-in-depth, not the primary boundary).
_DANGEROUS_PATTERNS = re.compile(r"\$\(|`|&&|\|\||;|\n|\r")


# ── helpers ───────────────────────────────────────────────────────────────────


def _validate_path(path_str: str) -> Path:
    """Resolve a path and ensure it falls within an allowed root."""
    resolved = Path(path_str).resolve()
    for root in ALLOWED_ROOTS:
        if str(resolved).startswith(root):
            return resolved
    raise HTTPException(
        status_code=400,
        detail=f"Path {path_str} is outside allowed roots: {ALLOWED_ROOTS}",
    )


async def _run_subprocess(
    cmd: str | list[str],
    *,
    timeout: int = 120,
    cwd: str | None = None,
    shell: bool = True,
) -> tuple[str, str, int, bool]:
    """Run a command with timeout.  Returns (stdout, stderr, exit_code, timed_out)."""
    if shell:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        timed_out = True
        stdout_bytes = b""
        stderr_bytes = b"Command timed out"

    return (
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        proc.returncode or -1,
        timed_out,
    )


# ── startup ───────────────────────────────────────────────────────────────────


KNOWN_TOOLS: dict[str, str] = {
    "nmap": "scanning",
    "masscan": "scanning",
    "impacket-secretsdump": "ad-attack",
    "impacket-psexec": "ad-attack",
    "impacket-wmiexec": "ad-attack",
    "impacket-smbexec": "ad-attack",
    "impacket-getTGT": "ad-attack",
    "impacket-getST": "ad-attack",
    "impacket-ticketConverter": "ad-attack",
    "impacket-ntlmrelayx": "ad-attack",
    "certipy": "ad-attack",
    "bloodhound-python": "ad-recon",
    "nxc": "ad-attack",
    "netexec": "ad-attack",
    "evil-winrm": "ad-attack",
    "responder": "poisoning",
    "kerbrute": "ad-attack",
    "hashcat": "cracking",
    "john": "cracking",
    "smbclient": "smb",
    "smbmap": "smb",
    "ldapsearch": "ldap",
    "enum4linux-ng": "enumeration",
    "nbtscan": "enumeration",
    "dnsrecon": "dns",
    "ssh-keygen": "keys",
    "openssl": "keys",
    "klist": "kerberos",
    "pypykatz": "credentials",
    "coercer": "coercion",
    "mitm6": "poisoning",
    "sprayhound": "spraying",
}

BUNDLED_TOOL_DIRS: dict[str, str] = {
    "PowerSploit": "powershell",
    "nishang": "powershell",
    "PEASS-ng": "privesc",
    "PrivescCheck": "privesc",
    "ligolo-ng": "tunneling",
}


@app.on_event("startup")
def _discover_tools() -> None:
    """Scan the system for known tools and populate the registry."""
    global _TOOL_REGISTRY
    tools: list[ToolInfo] = []

    for name, category in KNOWN_TOOLS.items():
        path = shutil.which(name)
        if path:
            tools.append(ToolInfo(name=name, path=path, category=category))

    for dirname, category in BUNDLED_TOOL_DIRS.items():
        tool_path = Path(f"/opt/tools/{dirname}")
        if tool_path.is_dir():
            tools.append(
                ToolInfo(name=dirname, path=str(tool_path), category=category)
            )

    wordlists = Path("/usr/share/wordlists")
    if wordlists.is_dir():
        tools.append(
            ToolInfo(name="wordlists", path=str(wordlists), category="wordlists")
        )

    seclists = Path("/usr/share/seclists")
    if seclists.is_dir():
        tools.append(
            ToolInfo(name="seclists", path=str(seclists), category="wordlists")
        )

    _TOOL_REGISTRY = tools
    logger.info("Discovered %d tools", len(tools))


# ── endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", tools_loaded=len(_TOOL_REGISTRY))


@app.post("/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest) -> ExecResponse:
    if _DANGEROUS_PATTERNS.search(req.command):
        raise HTTPException(
            status_code=400,
            detail="Command contains blocked shell metacharacters. Use individual commands without chaining.",
        )
    cwd = None
    if req.cwd:
        cwd_path = Path(req.cwd)
        if not cwd_path.is_dir():
            raise HTTPException(status_code=400, detail=f"cwd does not exist: {req.cwd}")
        cwd = req.cwd

    stdout, stderr, exit_code, timed_out = await _run_subprocess(
        req.command, timeout=req.timeout, cwd=cwd
    )
    return ExecResponse(
        stdout=stdout, stderr=stderr, exit_code=exit_code, timed_out=timed_out
    )


@app.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    dest_path: str = Form(...),
) -> UploadResponse:
    dest = _validate_path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return UploadResponse(path=str(dest), size=len(content))


@app.get("/download")
async def download_file(path: str = Query(...)) -> FileResponse:
    resolved = _validate_path(path)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return FileResponse(str(resolved), filename=resolved.name)


@app.post("/parse/keys", response_model=KeyParseResponse)
async def parse_keys(req: KeyParseRequest) -> KeyParseResponse:
    credentials: list[ParsedCredential] = []
    errors: list[str] = []

    target_path: str | None = req.path
    tmp_file: str | None = None

    if req.content and not req.path:
        try:
            raw = base64.b64decode(req.content)
        except Exception:
            return KeyParseResponse(errors=["Invalid base64 content"])
        fd, tmp_file = tempfile.mkstemp(prefix="kali_parse_")
        os.write(fd, raw)
        os.close(fd)
        target_path = tmp_file

    if not target_path:
        return KeyParseResponse(errors=["Provide either 'path' or 'content'"])

    resolved = _validate_path(target_path)
    if not resolved.is_file():
        return KeyParseResponse(errors=[f"File not found: {target_path}"])

    parse_type = req.parse_type

    # Auto-detect based on file content / extension
    if parse_type == "auto":
        ext = resolved.suffix.lower()
        content_peek = resolved.read_bytes()[:64]
        if ext in (".keytab",) or b"\x05\x02" in content_peek:
            parse_type = "kerberos"
        elif ext in (".pem", ".crt", ".cer", ".der"):
            parse_type = "certificate"
        elif b"ssh-rsa" in content_peek or b"ssh-ed25519" in content_peek or b"OPENSSH" in content_peek:
            parse_type = "ssh"
        elif ext in (".ccache", ".kirbi"):
            parse_type = "kerberos"
        else:
            parse_type = "ssh"

    try:
        if parse_type == "ssh":
            stdout, stderr, rc, _ = await _run_subprocess(
                ["ssh-keygen", "-l", "-f", str(resolved)], shell=False, timeout=10
            )
            if rc == 0 and stdout.strip():
                for line in stdout.strip().splitlines():
                    credentials.append(ParsedCredential(
                        type="ssh_key", details={"fingerprint": line.strip()}
                    ))
            else:
                errors.append(f"ssh-keygen: {stderr.strip()}")

        elif parse_type == "kerberos":
            stdout, stderr, rc, _ = await _run_subprocess(
                ["klist", "-k", str(resolved)], shell=False, timeout=10
            )
            if rc == 0 and stdout.strip():
                for line in stdout.strip().splitlines():
                    line = line.strip()
                    if line and not line.startswith("Keytab") and not line.startswith("KVNO"):
                        parts = line.split(None, 1)
                        principal = parts[-1] if parts else line
                        credentials.append(ParsedCredential(
                            type="kerberos_keytab",
                            identity=principal,
                            details={"raw": line},
                        ))
            else:
                errors.append(f"klist: {stderr.strip()}")

        elif parse_type == "certificate":
            stdout, stderr, rc, _ = await _run_subprocess(
                ["openssl", "x509", "-in", str(resolved), "-text", "-noout"],
                shell=False, timeout=10,
            )
            if rc == 0 and stdout.strip():
                subject = ""
                issuer = ""
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Subject:"):
                        subject = line.split(":", 1)[1].strip()
                    elif line.startswith("Issuer:"):
                        issuer = line.split(":", 1)[1].strip()
                credentials.append(ParsedCredential(
                    type="x509_certificate",
                    identity=subject,
                    details={"subject": subject, "issuer": issuer, "raw": stdout[:2000]},
                ))
            else:
                errors.append(f"openssl: {stderr.strip()}")

    except Exception as exc:
        errors.append(str(exc))
    finally:
        if tmp_file and os.path.exists(tmp_file):
            os.unlink(tmp_file)

    return KeyParseResponse(credentials=credentials, errors=errors)


@app.post("/bloodhound/ingest", response_model=BloodHoundIngestResponse)
async def bloodhound_ingest(req: BloodHoundIngestRequest) -> BloodHoundIngestResponse:
    output_dir = Path(req.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "bloodhound-python",
        "-d", req.domain,
        "-dc", req.dc_ip,
        "-c", req.collection_method,
        "--zip",
        "-o", str(output_dir),
    ]
    if req.username:
        cmd.extend(["-u", req.username])
    if req.password:
        cmd.extend(["-p", req.password])
    if req.hashes:
        cmd.extend(["--hashes", req.hashes])

    stdout, stderr, exit_code, timed_out = await _run_subprocess(
        cmd, shell=False, timeout=600, cwd=str(output_dir)
    )

    output_files = globmod.glob(str(output_dir / "*.zip")) + globmod.glob(
        str(output_dir / "*.json")
    )

    return BloodHoundIngestResponse(
        output_files=output_files,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )


@app.get("/tools", response_model=ToolsResponse)
def list_tools() -> ToolsResponse:
    return ToolsResponse(tools=_TOOL_REGISTRY)
