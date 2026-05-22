#!/usr/bin/env bash
# AgentFlow Desktop — cross-platform installer (macOS + Linux).
#
# One-liner usage from the cabinet:
#   curl -sSL https://agentflow.website/install/computer-mcp.sh | \
#     AF_KEY=<key> AF_DEVICE_ID=<uuid> AF_DEVICE_TOKEN=<token> bash
#
# For Windows use install.ps1 (or install.bat for cmd.exe shells).

set -euo pipefail

AF_DIR="${HOME}/.agentflow"
AF_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/agentflow"
AF_WS_URL="${AF_WS_URL:-wss://agentflow.website/_agents/_devices/connect}"

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install] error: %s\n' "$*" >&2; exit 1; }

prompt_if_missing() {
  local var="$1" prompt="$2" value=""
  if [[ -z "${!var:-}" ]]; then
    if [[ -t 0 || -r /dev/tty ]]; then
      printf '%s: ' "$prompt" >&2
      if [[ -r /dev/tty ]]; then
        IFS= read -r value </dev/tty
      else
        IFS= read -r value
      fi
      printf -v "$var" '%s' "$value"
      export "${var?}"
    fi
  fi
  if [[ -z "${!var:-}" ]]; then
    fail "$var is required (set env var or run from cabinet curl one-liner)"
  fi
}

prompt_if_missing AF_KEY "AgentFlow API key (af_live_…)"
prompt_if_missing AF_DEVICE_ID "Device ID (uuid from cabinet)"
prompt_if_missing AF_DEVICE_TOKEN "One-time device token"

mkdir -p "${AF_DIR}" "${AF_CONFIG_DIR}"

install_pkg() {
  local pip_target
  if command -v pip3 >/dev/null 2>&1; then
    pip_target="pip3"
  elif command -v python3 >/dev/null 2>&1; then
    pip_target="python3 -m pip"
  else
    fail "python3 not found on PATH; install Python 3.11+ first"
  fi

  if [[ -n "${AF_PACKAGE_PATH:-}" ]]; then
    log "installing package from ${AF_PACKAGE_PATH}"
    $pip_target install --user --upgrade "${AF_PACKAGE_PATH}"
  else
    log "installing agentflow-computer-mcp from GitHub"
    $pip_target install --user --upgrade "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
  fi
}

write_auth() {
  local target="${AF_DIR}/auth.json"
  umask 077
  cat > "${target}.tmp" <<EOF
{
  "api_key": "${AF_KEY}",
  "device_id": "${AF_DEVICE_ID}",
  "enrollment_token": "${AF_DEVICE_TOKEN}",
  "device_secret": "",
  "ws_url": "${AF_WS_URL}"
}
EOF
  chmod 600 "${target}.tmp"
  mv "${target}.tmp" "${target}"
  log "wrote ${target}"

  # XDG mirror so apps that look at $XDG_CONFIG_HOME find the same data.
  if [[ "${AF_CONFIG_DIR}" != "${AF_DIR}" ]]; then
    if [[ ! -e "${AF_CONFIG_DIR}/auth.json" && ! -L "${AF_CONFIG_DIR}/auth.json" ]]; then
      ln -s "${target}" "${AF_CONFIG_DIR}/auth.json" 2>/dev/null || true
    fi
  fi
}

write_scope() {
  if [[ -f "${AF_DIR}/computer-scope.toml" ]]; then
    return
  fi
  cat > "${AF_DIR}/computer-scope.toml" <<'EOF'
allow_apps = []
allow_paths = []
deny_paths = ["~/.ssh", "~/.config", "~/Library/Keychains", "~/.aws", "~/.gnupg"]
shell_whitelist = []
confirm_before = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd = 2.0
EOF
  log "wrote default scope at ${AF_DIR}/computer-scope.toml"
}

success_banner() {
  local url="https://agentflow.website/cabinet/devices/${AF_DEVICE_ID}/live"
  cat <<EOF

AgentFlow Desktop installed.

  Cabinet:   ${url}
  Auth file: ${AF_DIR}/auth.json
  Scope:     ${AF_DIR}/computer-scope.toml

EOF
}

# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

install_macos() {
  if ! command -v python3 >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1 && [[ -t 0 ]]; then
      log "python3 missing — installing via Homebrew"
      brew install python@3.11
    else
      fail "python3 not found; install via 'brew install python@3.11' or python.org"
    fi
  fi

  install_pkg
  write_auth
  write_scope

  local log_dir="${HOME}/Library/Logs"
  local launchd_dir="${HOME}/Library/LaunchAgents"
  local plist_name="com.agentflow.computer-mcp.plist"
  mkdir -p "${log_dir}" "${launchd_dir}"

  local user_base entrypoint
  user_base="$(python3 -m site --user-base)"
  entrypoint="${user_base}/bin/agentflow-computer-mcp"

  if [[ ! -x "${entrypoint}" ]]; then
    fail "expected ${entrypoint} after pip install but it is not executable"
  fi

  cat > "${launchd_dir}/${plist_name}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.agentflow.computer-mcp</string>
  <key>ProgramArguments</key>
  <array>
    <string>${entrypoint}</string>
    <string>--mode</string>
    <string>ws</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${log_dir}/agentflow-computer-mcp.log</string>
  <key>StandardErrorPath</key><string>${log_dir}/agentflow-computer-mcp.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${user_base}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

  launchctl unload "${launchd_dir}/${plist_name}" 2>/dev/null || true
  launchctl load "${launchd_dir}/${plist_name}"

  success_banner
  cat <<EOF
Grant permissions:
  System Settings -> Privacy & Security
    Accessibility:    add Terminal / iTerm / the python binary
    Screen Recording: same

Logs:
  tail -f ${log_dir}/agentflow-computer-mcp.log
EOF
}

# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

linux_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    log "no sudo available; skipping: $*"
    return 1
  fi
}

install_linux_deps() {
  local wayland=0
  if [[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]]; then
    wayland=1
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "apt-get detected; installing wmctrl xdotool xclip xvfb"
    linux_sudo apt-get update -y || true
    linux_sudo apt-get install -y python3 python3-pip wmctrl xdotool xclip xvfb || true
    if [[ "${wayland}" -eq 1 ]]; then
      linux_sudo apt-get install -y grim wl-clipboard slurp || true
    fi
  elif command -v dnf >/dev/null 2>&1; then
    log "dnf detected; installing wmctrl xdotool xclip"
    linux_sudo dnf install -y python3 python3-pip wmctrl xdotool xclip || true
    if [[ "${wayland}" -eq 1 ]]; then
      linux_sudo dnf install -y grim wl-clipboard slurp || true
    fi
  elif command -v pacman >/dev/null 2>&1; then
    log "pacman detected; installing wmctrl xdotool xclip"
    linux_sudo pacman -Sy --noconfirm python python-pip wmctrl xdotool xclip || true
    if [[ "${wayland}" -eq 1 ]]; then
      linux_sudo pacman -Sy --noconfirm grim wl-clipboard slurp || true
    fi
  else
    log "no known package manager (apt/dnf/pacman); ensure wmctrl xdotool xclip are installed"
  fi
}

install_linux() {
  install_linux_deps

  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found after dependency install; install Python 3.11+ manually"
  fi

  install_pkg
  write_auth
  write_scope

  local systemd_dir="${HOME}/.config/systemd/user"
  mkdir -p "${systemd_dir}"

  local user_base entrypoint
  user_base="$(python3 -m site --user-base)"
  entrypoint="${user_base}/bin/agentflow-computer-mcp"

  if [[ ! -x "${entrypoint}" ]]; then
    fail "expected ${entrypoint} after pip install but it is not executable"
  fi

  cat > "${systemd_dir}/agentflow-desktop.service" <<EOF
[Unit]
Description=AgentFlow Desktop daemon
After=network.target graphical-session.target

[Service]
Type=simple
ExecStart=${entrypoint} --mode ws
Restart=on-failure
RestartSec=5
Environment=PATH=${user_base}/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
    systemctl --user enable --now agentflow-desktop.service || \
      log "systemctl --user enable failed; start manually: systemctl --user start agentflow-desktop.service"
  else
    log "systemctl not available; start manually: ${entrypoint} --mode ws"
  fi

  success_banner
  cat <<EOF
Service:
  systemctl --user status agentflow-desktop.service
  journalctl --user -u agentflow-desktop -f
EOF
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

main() {
  case "$(uname -s)" in
    Darwin) install_macos ;;
    Linux)  install_linux ;;
    MINGW*|MSYS*|CYGWIN*)
      fail "use install.ps1 on Windows (iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex)"
      ;;
    *)
      fail "unsupported OS: $(uname -s)"
      ;;
  esac
}

main "$@"
