#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# AgentFlow Desktop installer — Mac + Linux
# Required env vars: AF_KEY, AF_DEVICE_TOKEN, AF_DEVICE_ID
# Optional env vars: AF_WS_URL, AF_PACKAGE_PATH
# ---------------------------------------------------------------------------

if [[ -z "${AF_KEY:-}" || -z "${AF_DEVICE_TOKEN:-}" || -z "${AF_DEVICE_ID:-}" ]]; then
  echo "error: AF_KEY, AF_DEVICE_TOKEN, AF_DEVICE_ID env vars required" >&2
  exit 1
fi

AF_WS_URL="${AF_WS_URL:-wss://agentflow.website/_agents/_devices/connect}"
AF_DIR="${HOME}/.agentflow"
LOG_DIR="${HOME}/Library/Logs"

mkdir -p "${AF_DIR}"

# ---------------------------------------------------------------------------
# Install the Python package
# ---------------------------------------------------------------------------
if [[ -n "${AF_PACKAGE_PATH:-}" ]]; then
  pip3 install --user --upgrade "${AF_PACKAGE_PATH}"
else
  pip3 install --user --upgrade "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
fi

# ---------------------------------------------------------------------------
# Write auth.json (mode 600)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Write default scope file if absent
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Resolve entry-point binary
# ---------------------------------------------------------------------------
USER_BASE="$(python3 -m site --user-base)"
ENTRYPOINT="${USER_BASE}/bin/agentflow-desktop"
if [[ ! -f "${ENTRYPOINT}" ]]; then
  # fallback: old package name
  ENTRYPOINT="${USER_BASE}/bin/agentflow-computer-mcp"
fi

# ---------------------------------------------------------------------------
# macOS — launchd user agent
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
  mkdir -p "${LOG_DIR}"
  LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
  mkdir -p "${LAUNCHD_DIR}"
  PLIST_LABEL="com.agentflow.desktop"
  PLIST_NAME="${PLIST_LABEL}.plist"
  PLIST_PATH="${LAUNCHD_DIR}/${PLIST_NAME}"

  cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${ENTRYPOINT}</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key><true/>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/agentflow-desktop.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/agentflow-desktop.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${USER_BASE}/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>
</dict>
</plist>
EOF

  # Validate the plist before registering it.
  if ! plutil -lint "${PLIST_PATH}" > /dev/null 2>&1; then
    echo "error: generated plist failed plutil -lint — aborting" >&2
    exit 1
  fi

  # Re-bootstrap the job (do NOT bootout — avoids permission prompts on re-install).
  # launchctl bootstrap is idempotent: if already loaded it no-ops or bumps config.
  LAUNCHCTL_UID="$(id -u)"
  launchctl bootstrap "gui/${LAUNCHCTL_UID}" "${PLIST_PATH}" 2>/dev/null || \
    launchctl kickstart -k "gui/${LAUNCHCTL_UID}/${PLIST_LABEL}" 2>/dev/null || \
    true

  PYTHON_BIN="$(command -v python3)"
  echo ""
  echo "agentflow-desktop installed (macOS)."
  echo ""
  echo "Next steps:"
  echo "  1. Open System Settings -> Privacy & Security"
  echo "     - Accessibility: add your terminal and ${PYTHON_BIN}"
  echo "     - Screen Recording: same"
  echo "  2. Start now:"
  echo "       launchctl start ${PLIST_LABEL}"
  echo "  3. Tail logs:"
  echo "       tail -f ${LOG_DIR}/agentflow-desktop.log"
  echo "  4. Selftest:"
  echo "       agentflow-desktop selftest"
  echo "  5. The cabinet at https://agentflow.website/cabinet/devices should show your device online within 30s."
  echo ""

  # ---------------------------------------------------------------------------
  # Selftest
  # ---------------------------------------------------------------------------
  if command -v agentflow-desktop &>/dev/null; then
    echo "--- selftest ---"
    if agentflow-desktop selftest; then
      echo "selftest: PASS"
    else
      echo "selftest: FAIL (see above — grant Accessibility + Screen Recording and retry)"
    fi
  fi

  exit 0
fi

# ---------------------------------------------------------------------------
# Linux — systemd user service
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" == "Linux" ]]; then
  SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
  mkdir -p "${SYSTEMD_USER_DIR}"
  SERVICE_FILE="${SYSTEMD_USER_DIR}/agentflow-desktop.service"

  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=AgentFlow Desktop daemon
After=network.target

[Service]
ExecStart=${ENTRYPOINT} run
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=0
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now agentflow-desktop.service

  # Enable linger so the user service survives across reboots without a login session.
  CURRENT_USER="$(id -un)"
  if loginctl enable-linger "${CURRENT_USER}" 2>/dev/null; then
    echo "[linger] enabled for ${CURRENT_USER} — service will start at boot."
  else
    echo "[hint] systemd-linger requires sudo on this distro."
    echo "       Run: sudo loginctl enable-linger ${CURRENT_USER}"
    echo "       Without it, the service starts only after you log in."
  fi

  echo ""
  echo "agentflow-desktop installed (Linux)."
  echo "  Status:    systemctl --user status agentflow-desktop"
  echo "  Logs:      journalctl --user -u agentflow-desktop -f"
  echo "  Selftest:  agentflow-desktop selftest"
  echo ""

  if command -v agentflow-desktop &>/dev/null; then
    echo "--- selftest ---"
    if agentflow-desktop selftest; then
      echo "selftest: PASS"
    else
      echo "selftest: FAIL"
    fi
  fi

  exit 0
fi

echo "unsupported OS: $(uname -s)" >&2
exit 1
