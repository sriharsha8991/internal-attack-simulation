#!/usr/bin/env bash
set -e

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        Kali Toolbox Container                        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# SSH / SFTP
service ssh start && echo "[+] SSH/SFTP running on port 22  (root:toor)"

# FTP
vsftpd /etc/vsftpd.conf &
sleep 1
echo "[+] FTP running on port 21       (ftpuser:ftppass)"

# Neo4j / BloodHound (optional — don't block if it fails)
neo4j start 2>/dev/null && sleep 3 && echo "[+] Neo4j running on ports 7474/7687" || echo "[-] Neo4j skipped"

echo ""
echo "── QUICK REFERENCE ─────────────────────────────────────"
echo " bloodhound-python -d DOMAIN -u USER -p PASS -dc DC_IP -c All --zip"
echo " nxc smb <target> -u user -p pass --users"
echo " kerbrute userenum --dc <dc-ip> -d DOMAIN users.txt"
echo " impacket-secretsdump DOMAIN/user:pass@<dc-ip>"
echo " certipy find -u user@DOMAIN -p pass -dc-ip <dc-ip>"
echo " evil-winrm -i <target> -u user -p pass"
echo " /opt/tools/  → PowerSploit, Nishang, PEASS-ng, PrivescCheck, Ligolo-ng"
echo " /usr/share/wordlists/rockyou.txt"
echo " /usr/share/seclists/"
echo "────────────────────────────────────────────────────────"
echo ""

# Start sidecar API as PID 1 (receives Docker stop signals)
KALI_API_PORT="${KALI_API_PORT:-9000}"
echo "[+] Starting sidecar API on port ${KALI_API_PORT}..."
exec uvicorn kali_api.main:app \
    --host 0.0.0.0 \
    --port "${KALI_API_PORT}" \
    --workers 1 \
    --app-dir /opt/kali-api \
    --log-level "${LOG_LEVEL:-info}"
