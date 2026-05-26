#!/bin/bash
# AgentFlow daemon — runtime addon installer.
#
# The slim base image ships only the bits needed for opencode-driven
# project work. Owners can layer in browser / VNC support at runtime
# without a docker rebuild:
#
#   agentflow-install-addon browser   # Xvfb + Chromium + Playwright + ffmpeg
#   agentflow-install-addon vnc       # x11vnc + noVNC (requires browser)
#
# Re-running an already-installed addon is a no-op.

set -euo pipefail

usage() {
  cat >&2 <<EOF
usage: agentflow-install-addon <browser|vnc|all>

  browser   Xvfb, Chromium, Playwright, fonts, ffmpeg, xdotool, wmctrl.
            Needed for kwork money loop, browser tasks, screen recording.
  vnc       x11vnc, noVNC, websockify, fluxbox. Requires browser already
            installed. Enables /cabinet/devices/<id>/live screen mirror.
  all       Install browser then vnc.
EOF
  exit 1
}

[[ $# -eq 1 ]] || usage
addon="$1"

install_browser() {
  echo "[install-addon] browser — apt update + chromium + playwright" >&2
  sudo -n apt-get update
  sudo -n apt-get install -y --no-install-recommends \
    xvfb xauth x11-utils x11-xserver-utils xdotool wmctrl \
    chromium chromium-driver \
    fonts-dejavu fonts-noto-color-emoji fonts-noto-cjk \
    ffmpeg
  pip install --user "playwright>=1.40"
  "$HOME/.local/bin/playwright" install chromium 2>/dev/null \
    || python3 -m playwright install chromium
  echo "[install-addon] browser ready" >&2
}

install_vnc() {
  echo "[install-addon] vnc — apt update + x11vnc + noVNC" >&2
  sudo -n apt-get update
  sudo -n apt-get install -y --no-install-recommends \
    x11vnc novnc websockify python3-websockify fluxbox
  echo "[install-addon] vnc ready — enable via AF_ENABLE_SCREEN=1 and restart" >&2
}

case "$addon" in
  browser) install_browser ;;
  vnc)     install_vnc ;;
  all)     install_browser; install_vnc ;;
  -h|--help) usage ;;
  *) echo "unknown addon: $addon" >&2; usage ;;
esac
