# =============================================================================
# Kali Linux Toolbox Container — BAS Orchestrator Sidecar (Optimized)
# =============================================================================
#
# DESCRIPTION:
#   Lightweight Kali Linux red team environment with:
#   - REST API sidecar on port 9000
#   - Core Active Directory and Network attack toolset
#   - Python-based domain collection/parsing (bloodhound-python)
#
# BUILD:
#   docker compose build kali-toolbox
#
# =============================================================================

FROM kalilinux/kali-rolling:latest

LABEL maintainer="Red Team Lab"
LABEL description="Optimized Kali toolbox — AD tools, REST sidecar"

# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT
# ─────────────────────────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="$PATH:/usr/local/go/bin:/root/go/bin:/opt/tools/bin"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Install lightweight system packages and tools
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    # Essentials
    curl wget git vim nano unzip zip tar \
    ca-certificates gnupg \
    # Basic network utilities
    nmap netcat-traditional iputils-ping \
    # Python + pip
    python3 python3-pip python3-venv python3-dev \
    # AD/Network exploitation tools
    python3-impacket impacket-scripts \
    smbclient smbmap \
    evil-winrm \
    netexec \
    certipy-ad \
    golang-go \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Python libraries via pip
# ─────────────────────────────────────────────────────────────────────────────
RUN pip3 install --break-system-packages --no-cache-dir --ignore-installed \
    bloodhound \
    impacket \
    certipy-ad \
    ldapdomaindump \
    pypykatz \
    coercer

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Kerbrute
# ─────────────────────────────────────────────────────────────────────────────
RUN go install github.com/ropnop/kerbrute@latest && \
    cp /root/go/bin/kerbrute /usr/local/bin/kerbrute || true

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Git-cloned post-exploitation guides (stored in /opt/tools)
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /opt/tools && \
    git clone --depth=1 https://github.com/PowerShellMafia/PowerSploit.git   /opt/tools/PowerSploit  && \
    git clone --depth=1 https://github.com/samratashok/nishang.git           /opt/tools/nishang      && \
    git clone --depth=1 https://github.com/peass-ng/PEASS-ng.git             /opt/tools/PEASS-ng     && \
    git clone --depth=1 https://github.com/itm4n/PrivescCheck.git            /opt/tools/PrivescCheck && \
    git clone --depth=1 https://github.com/nicocha30/ligolo-ng.git           /opt/tools/ligolo-ng

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Sidecar REST API + shared data directory
# ─────────────────────────────────────────────────────────────────────────────
COPY api/ /opt/kali-api/
RUN pip3 install --break-system-packages --ignore-installed --no-cache-dir \
    -r /opt/kali-api/requirements.txt

RUN mkdir -p /data/kali-shared

# ─────────────────────────────────────────────────────────────────────────────
# EXPOSED PORTS
# ─────────────────────────────────────────────────────────────────────────────
EXPOSE 9000

WORKDIR /root

CMD ["uvicorn", "kali_api.main:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "1", "--app-dir", "/opt/kali-api", "--log-level", "info"]
