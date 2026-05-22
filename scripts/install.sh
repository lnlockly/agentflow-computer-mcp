#!/usr/bin/env bash
# Cross-platform installer for agentflow-desktop (macOS + Linux).
# Detects OS, installs deps, sets up an autostart unit, writes ~/.agentflow/auth.json.
set -euo pipefail

if [[ -z "${AF_KEY:-}" || -z "${AF_DEVICE_TOKEN:-}" || -z "${AF_DEVICE_ID:-}" ]]; then
  echo "error: AF_KEY, AF_DEVICE_TOKEN, AF_DEVICE_ID env vars required" >&2
  exit 1
fi

AF_WS_URL="${AF_WS_URL:-wss://agentflow.website/_devices/connect}"
AF_DIR="${HOME}/.agentflow"
mkdir -p "${AF_DIR}"

OS="$(uname -s)"

install_pkg() {
  if [[ -n "${AF_PACKAGE_PATH:-}" ]]; then
    pip3 install --user --upgrade "${AF_PACKAGE_PATH}"
  else
    pip3 install --user --upgrade "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
  fi
}

write_auth() {
  cat > "${AF_DIR}/auth.json.tmp" <<EOF
{
  "api_key": "${AF_KEY}",
  "device_id": "${AF_DEVICE_ID}",
  "enrollment_token": "${AF_DEVICE_TOKEN}",
  "device_secret": "",
  "ws_url": "${AF_WS_URL}"
}
EOF
  chmod 600 "${AF_DIR}/auth.json.tmp"
  mv "${AF_DIR}/auth.json.tmp" "${AF_DIR}/auth.json"
}

write_scope() {
  if [[ ! -f "${AF_DIR}/computer-scope.toml" ]]; then
    cat > "${AF_DIR}/computer-scope.toml" <<'EOF'
allow_apps = []
allow_paths = []
deny_paths = ["~/.ssh", "~/.config", "~/Library/Keychains", "~/.aws", "~/.gnupg"]
shell_whitelist = []
confirm_before = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd = 2.0
EOF
  fi
}

install_macos() {
  install_pkg
  write_auth
  write_scope

  LOG_DIR="${HOME}/Library/Logs"
  LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
  PLIST_NAME="com.agentflow.computer-mcp.plist"
  mkdir -p "${LOG_DIR}" "${LAUNCHD_DIR}"

  PYTHON_BIN="$(command -v python3)"
  USER_BASE="$(python3 -m site --user-base)"
  ENTRYPOINT="${USER_BASE}/bin/agentflow-computer-mcp"

  cat > "${LAUNCHD_DIR}/${PLIST_NAME}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.agentflow.computer-mcp</string>
  <key>ProgramArguments</key>
  <array>
    <string>${ENTRYPOINT}</string>
    <string>--mode</string>
    <string>ws</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/agentflow-computer-mcp.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/agentflow-computer-mcp.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${USER_BASE}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

  launchctl unload "${LAUNCHD_DIR}/${PLIST_NAME}" 2>/dev/null || true
  launchctl load "${LAUNCHD_DIR}/${PLIST_NAME}"

  echo "macOS install complete. Grant Accessibility + Screen Recording in System Settings, then:"
  echo "  launchctl start com.agentflow.computer-mcp"
  echo "  tail -f ${LOG_DIR}/agentflow-computer-mcp.log"
}

install_linux() {
  echo "Detecting Linux dependencies..."
  if command -v apt-get >/dev/null 2>&1; then
    SUDO=""
    if [[ "$EUID" -ne 0 ]]; then SUDO="sudo"; fi
    $SUDO apt-get update -y
    $SUDO apt-get install -y wmctrl xdotool xclip || true
    if [[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]]; then
      $SUDO apt-get install -y grim wl-clipboard || true
    fi
  fi

  install_pkg
  write_auth
  write_scope

  SYSTEMD_DIR="${HOME}/.config/systemd/user"
  mkdir -p "${SYSTEMD_DIR}"
  USER_BASE="$(python3 -m site --user-base)"
  ENTRYPOINT="${USER_BASE}/bin/agentflow-computer-mcp"

  cat > "${SYSTEMD_DIR}/agentflow-computer-mcp.service" <<EOF
[Unit]
Description=AgentFlow Computer MCP daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=${ENTRYPOINT} --mode ws
Restart=on-failure
RestartSec=5
Environment=PATH=${USER_BASE}/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable agentflow-computer-mcp.service
  systemctl --user restart agentflow-computer-mcp.service

  echo "Linux install complete."
  echo "  systemctl --user status agentflow-computer-mcp"
  echo "  journalctl --user -u agentflow-computer-mcp -f"
}

case "${OS}" in
  Darwin) install_macos ;;
  Linux)  install_linux ;;
  *)
    echo "unsupported OS: ${OS}. For Windows use scripts/install.ps1" >&2
    exit 1
    ;;
esac

echo
echo "Verify with: agentflow-desktop selftest"
