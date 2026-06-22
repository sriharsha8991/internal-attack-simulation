# =============================================================================
# Kali Linux Toolbox Container — BAS Orchestrator Sidecar
# =============================================================================
#
# DESCRIPTION:
#   Full Kali Linux red team environment with:
#   - REST API sidecar on port 9000 (internal network only)
#   - FTP server (vsftpd) on port 21
#   - SFTP/SSH server (OpenSSH) on port 22
#   - BloodHound GUI + Neo4j backend on ports 7474 / 7687
#   - BloodHound Python ingestor
#   - Full Active Directory attack toolset
#   - Kali meta-packages (top10, exploitation, web, password, etc.)
#
# BUILD (standalone):
#   docker build -t kali-toolbox .
#
# BUILD (via compose, from project root):
#   docker compose build kali-toolbox
#
# DEFAULT CREDENTIALS (change before exposing to any network):
#   root        : toor         (SSH / SFTP)
#   ftpuser     : ftppass      (FTP)
#   neo4j       : bloodhound123 (Neo4j / BloodHound)
#
# =============================================================================

FROM kalilinux/kali-rolling:latest

LABEL maintainer="Red Team Lab"
LABEL description="Kali toolbox — AD tools, BloodHound, FTP/SFTP, REST sidecar"

# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT
# ─────────────────────────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="$PATH:/usr/local/go/bin:/root/go/bin:/opt/tools/bin"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — System update, upgrade, dist-upgrade
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update -y && \
    apt-get upgrade -y && \
    apt-get dist-upgrade -y && \
    apt-get autoremove -y && \
    apt-get clean

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Core system dependencies
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update -y && apt-get install -y \
    # Essentials
    curl wget git vim nano unzip zip tar \
    build-essential cmake pkg-config \
    software-properties-common apt-transport-https \
    ca-certificates gnupg lsb-release \
    # FTP / SFTP / SSH
    openssh-server openssh-client \
    vsftpd ftp lftp \
    # Networking utilities
    netcat-traditional ncat nmap masscan \
    iproute2 iputils-ping net-tools \
    dnsutils whois traceroute \
    tcpdump wireshark-common tshark \
    # Python
    python3 python3-pip python3-venv \
    python3-dev python3-setuptools python3-wheel \
    # Ruby (used by some Kali tools)
    ruby ruby-dev \
    # Go (for Go-based tools like kerbrute)
    golang-go \
    # Java (required by BloodHound GUI and Neo4j)
    default-jre default-jdk \
    # X11 / GUI support (BloodHound GUI)
    xauth x11-apps \
    libx11-dev libxext-dev libxrender-dev \
    libglib2.0-0 libgtk-3-0 libdbus-glib-1-2 \
    libnss3 libatk-bridge2.0-0 libdrm2 \
    libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libasound2t64 \
    # Utilities
    tmux screen jq \
    sqlite3 libsqlite3-dev \
    libssl-dev libffi-dev \
    less file dos2unix \
    && apt-get clean

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Kali meta-packages (large toolsets, install once)
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update -y && apt-get install -y \
    kali-tools-top10 \
    kali-tools-information-gathering \
    kali-tools-vulnerability \
    kali-tools-web \
    kali-tools-passwords \
    kali-tools-exploitation \
    kali-tools-sniffing-spoofing \
    kali-tools-post-exploitation \
    && apt-get clean

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Active Directory / Windows attack tools (apt)
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update -y && apt-get install -y \
    python3-impacket impacket-scripts \
    smbclient smbmap \
    ldap-utils \
    krb5-user krb5-config \
    enum4linux enum4linux-ng \
    nbtscan \
    hashcat john \
    responder \
    evil-winrm \
    netexec \
    certipy-ad \
    bloodhound \
    neo4j \
    dnsrecon \
    && apt-get clean

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Wordlists
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update -y && apt-get install -y \
    seclists \
    wordlists \
    && apt-get clean && \
    [ -f /usr/share/wordlists/rockyou.txt.gz ] && \
    gunzip /usr/share/wordlists/rockyou.txt.gz || true

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Python tools via pip
# ─────────────────────────────────────────────────────────────────────────────
RUN pip3 install --break-system-packages --ignore-installed \
    bloodhound \
    impacket \
    certipy-ad \
    ldapdomaindump \
    ldeep \
    pypykatz \
    mitm6 \
    coercer \
    sprayhound \
    git+https://github.com/garrettfoster13/pre2k.git \
    pyasn1 pyasn1-modules \
    pycryptodome \
    requests \
    dnspython \
    ldap3 \
    pyOpenSSL \
    pysmb

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Go tools
# ─────────────────────────────────────────────────────────────────────────────
RUN go install github.com/ropnop/kerbrute@latest && \
    cp /root/go/bin/kerbrute /usr/local/bin/kerbrute || true

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Git-cloned tools (stored in /opt/tools)
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /opt/tools && \
    git clone --depth=1 https://github.com/PowerShellMafia/PowerSploit.git   /opt/tools/PowerSploit  && \
    git clone --depth=1 https://github.com/samratashok/nishang.git           /opt/tools/nishang      && \
    git clone --depth=1 https://github.com/peass-ng/PEASS-ng.git             /opt/tools/PEASS-ng     && \
    git clone --depth=1 https://github.com/itm4n/PrivescCheck.git            /opt/tools/PrivescCheck && \
    git clone --depth=1 https://github.com/nicocha30/ligolo-ng.git           /opt/tools/ligolo-ng

# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 — SSH / SFTP server configuration
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /var/run/sshd && \
    echo 'root:toor' | chpasswd && \
    sed -i 's|#PermitRootLogin prohibit-password|PermitRootLogin yes|'   /etc/ssh/sshd_config && \
    sed -i 's|#PasswordAuthentication yes|PasswordAuthentication yes|'   /etc/ssh/sshd_config && \
    grep -q "Subsystem sftp" /etc/ssh/sshd_config || \
        echo "Subsystem sftp /usr/lib/openssh/sftp-server" >> /etc/ssh/sshd_config && \
    ssh-keygen -A

# ─────────────────────────────────────────────────────────────────────────────
# STEP 10 — FTP server configuration (vsftpd)
# ─────────────────────────────────────────────────────────────────────────────
RUN useradd -m ftpuser && \
    echo "ftpuser:ftppass" | chpasswd && \
    mkdir -p /var/ftp/pub && \
    printf '%s\n' \
        'listen=YES' \
        'listen_ipv6=NO' \
        'anonymous_enable=NO' \
        'local_enable=YES' \
        'write_enable=YES' \
        'local_umask=022' \
        'chroot_local_user=NO' \
        'xferlog_enable=YES' \
        'xferlog_file=/var/log/vsftpd.log' \
        'pasv_enable=YES' \
        'pasv_min_port=40000' \
        'pasv_max_port=40100' \
        'ftpd_banner=Kali FTP Lab Server' \
        'background=NO' \
        > /etc/vsftpd.conf

# ─────────────────────────────────────────────────────────────────────────────
# STEP 11 — Neo4j configuration (BloodHound backend)
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /var/lib/neo4j/data /var/log/neo4j && \
    neo4j-admin dbms set-initial-password bloodhound123 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# STEP 12 — Sidecar REST API + shared data directory
# ─────────────────────────────────────────────────────────────────────────────
COPY api/ /opt/kali-api/
RUN pip3 install --break-system-packages --ignore-installed --no-cache-dir \
    -r /opt/kali-api/requirements.txt

RUN mkdir -p /data/kali-shared

# ─────────────────────────────────────────────────────────────────────────────
# STEP 13 — Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ─────────────────────────────────────────────────────────────────────────────
# EXPOSED PORTS
# ─────────────────────────────────────────────────────────────────────────────
EXPOSE 22 21 40000-40100 7474 7687 9000

WORKDIR /root

ENTRYPOINT ["/entrypoint.sh"]
