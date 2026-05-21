#!/usr/bin/env bash
# AgentFlow Desktop installer (macOS).
#
# Usage:
#   curl -sSL https://agentflow.website/install/desktop.sh | bash
#   curl -sSL https://agentflow.website/install/desktop.sh | AF_KEY=af_live_... AF_DEVICE_NAME="my-mac" bash
#
# Env (all optional — installer will prompt for missing ones):
#   AF_KEY              owner API key (af_live_*)
#   AF_DEVICE_NAME      display name shown in /cabinet/devices
#   AF_API_BASE         default https://agentflow.website
#   AF_WS_URL           default wss://agentflow.website/_devices/connect
#   AF_PACKAGE_PATH     local path / VCS ref for pip install (default: git+https://github.com/lnlockly/agentflow-computer-mcp.git)
#   AF_NO_LAUNCHD=1     skip launchctl load (useful in CI / dry-run)
#   AF_NO_OPEN=1        skip opening System Settings for permissions
#
# Idempotent: safe to re-run. Detects existing install, upgrades package, refreshes plist.

set -euo pipefail

GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[1;31m'
BLUE=$'\033[1;34m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

log()   { printf '%s[agentflow]%s %s\n' "$BLUE" "$RESET" "$*"; }
ok()    { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%s⚠%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail()  { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

cat <<'BANNER'

  ╔═══════════════════════════════════════════════╗
  ║   AgentFlow Desktop · macOS installer         ║
  ╚═══════════════════════════════════════════════╝

BANNER

# ---------- platform guard ----------
if [[ "$(uname)" != "Darwin" ]]; then
  fail "AgentFlow Desktop targets macOS. Detected $(uname). Linux/Windows support: see https://agentflow.website/self-host"
fi

# ---------- defaults ----------
AF_API_BASE="${AF_API_BASE:-https://agentflow.website}"
AF_WS_URL="${AF_WS_URL:-wss://agentflow.website/_devices/connect}"
AF_PACKAGE_PATH="${AF_PACKAGE_PATH:-git+https://github.com/lnlockly/agentflow-computer-mcp.git}"
AF_DIR="${HOME}/.agentflow"
INSTALL_DIR="${HOME}/.agentflow-desktop"
VENV_DIR="${INSTALL_DIR}/venv"
LOG_DIR="${HOME}/Library/Logs"
LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
PLIST_LABEL="com.agentflow.desktop"
PLIST_PATH="${LAUNCHD_DIR}/${PLIST_LABEL}.plist"
OLD_PLIST_PATH="${LAUNCHD_DIR}/com.agentflow.computer-mcp.plist"

mkdir -p "$AF_DIR" "$INSTALL_DIR" "$LOG_DIR" "$LAUNCHD_DIR"
chmod 700 "$AF_DIR"

# ---------- prerequisites ----------
PYTHON_BIN=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$cand")"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  fail "python3 >= 3.11 not found. Install via 'brew install python@3.12' then re-run."
fi
ok "python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# ---------- interactive prompts ----------
if [[ -z "${AF_KEY:-}" ]]; then
  if [[ -t 0 ]]; then
    printf '%sAgentFlow API key%s (from https://agentflow.website/account/api-keys): ' "$BOLD" "$RESET"
    read -r AF_KEY
  else
    fail "AF_KEY not set and no TTY for prompt. Re-run as: curl -sSL ... | AF_KEY=af_live_... bash"
  fi
fi

if [[ ! "$AF_KEY" =~ ^af_live_ ]]; then
  warn "API key does not start with 'af_live_'. Continuing anyway."
fi

if [[ -z "${AF_DEVICE_NAME:-}" ]]; then
  default_name="$(scutil --get ComputerName 2>/dev/null || hostname -s)"
  if [[ -t 0 ]]; then
    printf '%sDevice name%s [%s]: ' "$BOLD" "$RESET" "$default_name"
    read -r entered
    AF_DEVICE_NAME="${entered:-$default_name}"
  else
    AF_DEVICE_NAME="$default_name"
  fi
fi
ok "device name: $AF_DEVICE_NAME"

# ---------- device enrollment ----------
EXISTING_AUTH="${AF_DIR}/auth.json"
DEVICE_ID=""
ENROLL_TOKEN=""

if [[ -f "$EXISTING_AUTH" ]]; then
  warn "existing $EXISTING_AUTH found — reusing enrollment if device still registered"
  DEVICE_ID="$("$PYTHON_BIN" -c "import json,sys; print(json.load(open('$EXISTING_AUTH')).get('device_id',''))" 2>/dev/null || true)"
fi

if [[ -z "$DEVICE_ID" ]]; then
  log "registering device with $AF_API_BASE ..."
  enroll_response="$(curl -sS -w '\n%{http_code}' -X POST \
    -H "x-api-key: $AF_KEY" \
    -H 'content-type: application/json' \
    -d "{\"name\":$(printf '%s' "$AF_DEVICE_NAME" | "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"kind\":\"macos\"}" \
    "$AF_API_BASE/_agents/me/devices" || true)"

  http_code="$(printf '%s' "$enroll_response" | tail -n1)"
  body="$(printf '%s' "$enroll_response" | sed '$d')"

  if [[ "$http_code" != "200" && "$http_code" != "201" ]]; then
    fail "device registration failed (HTTP $http_code): $body"
  fi

  DEVICE_ID="$(printf '%s' "$body" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or d.get("device_id") or "")')"
  ENROLL_TOKEN="$(printf '%s' "$body" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("enrollment_token") or d.get("token") or "")')"

  if [[ -z "$DEVICE_ID" || -z "$ENROLL_TOKEN" ]]; then
    fail "registration response missing id / enrollment_token. body=$body"
  fi
  ok "device id: $DEVICE_ID"
fi

# ---------- python venv + package ----------
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  log "creating venv at ${VENV_DIR}"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip wheel >/dev/null
log "installing agentflow-computer-mcp from ${AF_PACKAGE_PATH}"
"${VENV_DIR}/bin/pip" install --upgrade "$AF_PACKAGE_PATH" >/tmp/agentflow-pip.log 2>&1 || {
  tail -20 /tmp/agentflow-pip.log >&2
  fail "pip install failed. Full log: /tmp/agentflow-pip.log"
}
ENTRYPOINT="${VENV_DIR}/bin/agentflow-computer-mcp"
[[ -x "$ENTRYPOINT" ]] || fail "entrypoint missing: $ENTRYPOINT"
ok "installed: $("${VENV_DIR}/bin/agentflow-computer-mcp" --version 2>/dev/null || echo unknown)"

# ---------- auth.json ----------
if [[ -n "$ENROLL_TOKEN" || ! -f "$EXISTING_AUTH" ]]; then
  umask 077
  cat > "${AF_DIR}/auth.json.tmp" <<JSON
{
  "api_key": "${AF_KEY}",
  "device_id": "${DEVICE_ID}",
  "enrollment_token": "${ENROLL_TOKEN}",
  "device_secret": "",
  "ws_url": "${AF_WS_URL}",
  "api_base": "${AF_API_BASE}"
}
JSON
  mv "${AF_DIR}/auth.json.tmp" "${AF_DIR}/auth.json"
  chmod 600 "${AF_DIR}/auth.json"
  ok "wrote ${AF_DIR}/auth.json (mode 0600)"
fi

# ---------- default scope ----------
if [[ ! -f "${AF_DIR}/computer-scope.toml" ]]; then
  cat > "${AF_DIR}/computer-scope.toml" <<'TOML'
# AgentFlow Desktop scope. Edit to broaden or narrow what the agent can touch.
allow_apps = []
allow_paths = []
deny_paths = ["~/.ssh", "~/.config", "~/Library/Keychains", "~/.aws", "~/.gnupg"]
shell_whitelist = []
confirm_before = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd = 2.0
TOML
  ok "wrote ${AF_DIR}/computer-scope.toml (default scope)"
fi

# ---------- migrate old plist ----------
if [[ -f "$OLD_PLIST_PATH" ]]; then
  warn "removing legacy plist $OLD_PLIST_PATH"
  launchctl unload "$OLD_PLIST_PATH" 2>/dev/null || true
  rm -f "$OLD_PLIST_PATH"
fi

# ---------- launchd plist ----------
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${ENTRYPOINT}</string>
    <string>--mode</string>
    <string>ws</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
    <key>Crashed</key><true/>
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${LOG_DIR}/agentflow-desktop.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/agentflow-desktop.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>
</dict>
</plist>
PLIST
ok "wrote $PLIST_PATH"

if [[ -z "${AF_NO_LAUNCHD:-}" ]]; then
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load "$PLIST_PATH"
  ok "launchd: loaded $PLIST_LABEL"
else
  warn "AF_NO_LAUNCHD=1 — skipping launchctl load"
fi

# ---------- permission checks ----------
has_accessibility=0
has_screen=0

if [[ -e /Library/Application\ Support/com.apple.TCC/TCC.db ]] || true; then
  # Heuristic: try a Quartz call. If it throws, permissions are missing.
  if "${VENV_DIR}/bin/python" - <<'PY' 2>/dev/null
import Quartz
img = Quartz.CGWindowListCreateImage(
    Quartz.CGRectInfinite,
    Quartz.kCGWindowListOptionOnScreenOnly,
    Quartz.kCGNullWindowID,
    Quartz.kCGWindowImageDefault,
)
import sys
sys.exit(0 if img is not None else 1)
PY
  then has_screen=1; fi
fi

if [[ $has_screen -eq 1 ]]; then
  ok "Screen Recording permission: looks granted"
else
  warn "Screen Recording permission missing. Without it the daemon cannot capture frames."
fi

if [[ -z "${AF_NO_OPEN:-}" && $has_screen -eq 0 ]]; then
  warn "opening System Settings → Privacy & Security panes ..."
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true
fi

# ---------- success summary ----------
cabinet_url="${AF_API_BASE/_agents/}/cabinet/devices/${DEVICE_ID}/live"
# strip /_agents if API base contained it
cabinet_url="${AF_API_BASE%/_agents}/cabinet/devices/${DEVICE_ID}/live"

cat <<EOF

${GREEN}${BOLD}✓ AgentFlow Desktop installed.${RESET}

  Device id    ${DEVICE_ID}
  Device name  ${AF_DEVICE_NAME}
  Install dir  ${INSTALL_DIR}
  Logs         ${LOG_DIR}/agentflow-desktop.log
  Scope        ${AF_DIR}/computer-scope.toml

${BOLD}Next steps:${RESET}
  1. Grant ${BOLD}Screen Recording${RESET} + ${BOLD}Accessibility${RESET} to the python binary:
     System Settings → Privacy & Security → add:
       ${DIM}${VENV_DIR}/bin/python${RESET}

  2. Open the live console:
     ${BLUE}${cabinet_url}${RESET}

  3. Tail logs:
     tail -f ${LOG_DIR}/agentflow-desktop.log

  4. Manage daemon:
     launchctl kickstart -k gui/\$(id -u)/${PLIST_LABEL}   # restart
     launchctl unload ${PLIST_PATH}                        # stop
     launchctl load   ${PLIST_PATH}                        # start

EOF
