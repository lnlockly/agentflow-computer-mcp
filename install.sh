#!/bin/bash
set -euo pipefail

if [[ -z "${AF_DEVICE_TOKEN:-}" || -z "${AF_DEVICE_ID:-}" ]]; then
  echo "error: AF_DEVICE_TOKEN and AF_DEVICE_ID env vars required" >&2
  exit 1
fi

# Ask for AF_KEY interactively if not provided (avoids shell-history leakage).
# Falls back to /dev/tty so the prompt works under `curl | bash` piping.
if [[ -z "${AF_KEY:-}" ]]; then
  if [[ -t 0 ]]; then
    printf "AgentFlow API key (af_live_...): "
    read -rs AF_KEY
    echo
  elif [[ -e /dev/tty ]]; then
    printf "AgentFlow API key (af_live_...): " >/dev/tty
    read -rs AF_KEY </dev/tty
    echo >/dev/tty
  else
    echo "error: AF_KEY env var required (no TTY for interactive prompt)" >&2
    exit 1
  fi
fi

if [[ -z "${AF_KEY:-}" ]]; then
  echo "error: AF_KEY is empty" >&2
  exit 1
fi

AF_WS_URL="${AF_WS_URL:-wss://agentflow.website/_devices/connect}"
AF_DIR="${HOME}/.agentflow"
LOG_DIR="${HOME}/Library/Logs"
LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
PLIST_NAME="com.agentflow.computer-mcp.plist"

mkdir -p "${AF_DIR}" "${LOG_DIR}" "${LAUNCHD_DIR}"

if [[ -n "${AF_PACKAGE_PATH:-}" ]]; then
  pip3 install --user --upgrade "${AF_PACKAGE_PATH}"
else
  pip3 install --user --upgrade "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
fi

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

cat <<EOF

agentflow-computer-mcp installed.

Next steps:
  1. Open System Settings -> Privacy & Security
     - Accessibility: add your terminal (Terminal.app / iTerm) and the python binary at ${PYTHON_BIN}
     - Screen Recording: same
  2. Start the agent:
       launchctl start com.agentflow.computer-mcp
  3. Tail logs:
       tail -f ${LOG_DIR}/agentflow-computer-mcp.log
  4. The cabinet at https://agentflow.website/cabinet/devices should flip your device to online within 30s.
EOF
