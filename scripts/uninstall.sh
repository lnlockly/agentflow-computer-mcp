#!/usr/bin/env bash
# Remove the autostart unit + auth files. Leaves the pip package installed.
set -euo pipefail

OS="$(uname -s)"

case "${OS}" in
  Darwin)
    LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
    PLIST="${LAUNCHD_DIR}/com.agentflow.computer-mcp.plist"
    launchctl unload "${PLIST}" 2>/dev/null || true
    rm -f "${PLIST}"
    echo "removed launchd unit"
    ;;
  Linux)
    systemctl --user stop agentflow-computer-mcp.service 2>/dev/null || true
    systemctl --user disable agentflow-computer-mcp.service 2>/dev/null || true
    rm -f "${HOME}/.config/systemd/user/agentflow-computer-mcp.service"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed systemd user unit"
    ;;
  *)
    echo "unsupported OS: ${OS}" >&2
    exit 1
    ;;
esac

rm -f "${HOME}/.agentflow/auth.json"
echo "uninstall complete (pip package retained — remove with pip3 uninstall agentflow-computer-mcp)"
