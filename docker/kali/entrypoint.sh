#!/usr/bin/env bash
set -e

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        Kali Toolbox Container (Optimized)            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Start sidecar API as PID 1 (receives Docker stop signals)
KALI_API_PORT="${KALI_API_PORT:-9000}"
LOG_LEVEL_LOWER=$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')
echo "[+] Starting sidecar API on port ${KALI_API_PORT}..."
exec uvicorn kali_api.main:app \
    --host 0.0.0.0 \
    --port "${KALI_API_PORT}" \
    --workers 1 \
    --app-dir /opt/kali-api \
    --log-level "${LOG_LEVEL_LOWER}"
