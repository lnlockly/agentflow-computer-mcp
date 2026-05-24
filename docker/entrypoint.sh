#!/bin/bash
# AgentFlow hosted daemon entrypoint.
#
# Boots Xvfb, then hands off to `agentflow-desktop run`. The daemon picks
# up its API key + device token from environment variables baked into the
# pod by the hosted-device provisioner.
#
# Required env:
#   AF_API_KEY        — owner API key (af_live_*)
#   AF_DEVICE_TOKEN   — JWT issued by /me/devices/issue
#   AF_DEVICE_NAME    — friendly name surfaced in /cabinet/devices
#
# Optional:
#   AF_API_URL        — override AgentFlow base URL (default agentflow.website)
#   DISPLAY           — Xvfb display, default :99
#   XVFB_WHD          — Xvfb resolution WxHxDepth, default 1440x900x24

set -euo pipefail

: "${DISPLAY:=:99}"
: "${XVFB_WHD:=1440x900x24}"

cleanup() {
  local xvfb_pid=$1
  echo "[entrypoint] cleanup — killing xvfb pid=$xvfb_pid" >&2
  kill "$xvfb_pid" 2>/dev/null || true
}

echo "[entrypoint] starting Xvfb on $DISPLAY @ $XVFB_WHD" >&2
Xvfb "$DISPLAY" -screen 0 "$XVFB_WHD" -ac +extension RANDR -nolisten tcp &
XVFB_PID=$!
trap "cleanup $XVFB_PID" EXIT INT TERM

# Wait until Xvfb actually accepts connections (max 10s).
for i in $(seq 1 50); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "[entrypoint] Xvfb ready after ${i}*0.2s" >&2
    break
  fi
  sleep 0.2
done

export DISPLAY

if [[ -z "${AF_API_KEY:-}" ]]; then
  echo "[entrypoint] AF_API_KEY missing — running selftest only" >&2
  exec agentflow-desktop selftest
fi

# Hand off. `exec` so signals (SIGTERM from kubectl delete) reach the
# Python process directly.
echo "[entrypoint] handing off to agentflow-desktop run" >&2
exec agentflow-desktop run --headless
