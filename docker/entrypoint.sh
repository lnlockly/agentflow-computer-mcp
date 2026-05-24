#!/bin/bash
# AgentFlow hosted daemon entrypoint.
#
# Boots Xvfb (always), opt-in starts fluxbox + x11vnc + noVNC, then hands
# off to `agentflow-desktop run`. The daemon picks up its API key + device
# token from environment variables baked into the pod by the hosted-device
# provisioner.
#
# Required env:
#   AF_API_KEY        — owner API key (af_live_*)
#   AF_DEVICE_TOKEN   — JWT issued by /me/devices/issue
#   AF_DEVICE_NAME    — friendly name surfaced in /cabinet/devices
#
# Optional:
#   AF_API_URL          — override AgentFlow base URL (default agentflow.website)
#   DISPLAY             — Xvfb display, default :99
#   XVFB_WHD            — Xvfb resolution WxHxDepth, default 1440x900x24
#   AF_ENABLE_SCREEN    — "1" → boot fluxbox + x11vnc + noVNC, expose
#                         screen on :6080 (browser iframe в кабинете).
#                         Дефолт «выкл», чтобы пользователи без визуальной
#                         нужды не платили за лишние процессы.
#   AF_VNC_PASSWORD     — пароль для x11vnc (auto-gen если не задан)

set -euo pipefail

: "${DISPLAY:=:99}"
: "${XVFB_WHD:=1440x900x24}"
: "${AF_ENABLE_SCREEN:=0}"

# PIDs of optional background processes; cleanup kills them all.
XVFB_PID=""
FLUXBOX_PID=""
X11VNC_PID=""
NOVNC_PID=""

cleanup() {
  for pid in $XVFB_PID $FLUXBOX_PID $X11VNC_PID $NOVNC_PID; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "[entrypoint] starting Xvfb on $DISPLAY @ $XVFB_WHD" >&2
Xvfb "$DISPLAY" -screen 0 "$XVFB_WHD" -ac +extension RANDR -nolisten tcp &
XVFB_PID=$!

# Wait until Xvfb actually accepts connections (max 10s).
for i in $(seq 1 50); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "[entrypoint] Xvfb ready after ${i}*0.2s" >&2
    break
  fi
  sleep 0.2
done

export DISPLAY

if [[ "$AF_ENABLE_SCREEN" == "1" ]]; then
  echo "[entrypoint] AF_ENABLE_SCREEN=1 — starting fluxbox + x11vnc + noVNC" >&2

  # Lightweight WM so opened windows don't float at (0,0) on top of each other.
  fluxbox &
  FLUXBOX_PID=$!
  sleep 0.5

  # x11vnc: bind to localhost only, websockify will publish it to :6080.
  # Password auto-generated when not provided so the раw vnc port isn't
  # open without an additional kubectl port-forward step.
  if [[ -z "${AF_VNC_PASSWORD:-}" ]]; then
    AF_VNC_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | cut -c1-16)
    echo "[entrypoint] generated vnc password ${AF_VNC_PASSWORD}" >&2
  fi
  mkdir -p ~/.vnc
  x11vnc -storepasswd "$AF_VNC_PASSWORD" ~/.vnc/passwd >/dev/null 2>&1
  x11vnc -display "$DISPLAY" -rfbauth ~/.vnc/passwd \
    -localhost -shared -forever -nopw -quiet \
    -rfbport 5900 &
  X11VNC_PID=$!
  sleep 0.5

  # noVNC websocket bridge — exposes a browser-friendly endpoint on :6080.
  # The cabinet renders this in an iframe via `wss://<pod>/websockify`.
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 &
  NOVNC_PID=$!

  # Surface the password to the daemon via env so it can publish it in
  # GET /me/hosted-devices/:id (so cabinet UI can auto-fill the noVNC prompt).
  export AF_VNC_PASSWORD
fi

if [[ -z "${AF_API_KEY:-}" ]]; then
  echo "[entrypoint] AF_API_KEY missing — running selftest only" >&2
  exec agentflow-desktop selftest
fi

# Hand off. `exec` so signals (SIGTERM from kubectl delete) reach the
# Python process directly.
echo "[entrypoint] handing off to agentflow-desktop run" >&2
exec agentflow-desktop run --headless
