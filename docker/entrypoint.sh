#!/bin/bash
# AgentFlow hosted daemon entrypoint.
#
# Boots Xvfb (always), opt-in starts fluxbox + x11vnc + noVNC, then hands
# off to `agentflow-desktop run`. The daemon picks up its API key + device
# token from environment variables baked into the pod by the hosted-device
# provisioner.
#
# Required env (one of two enrollment modes):
#
#   Mode A — hosted-device pod (default for kind=daemon hosted_devices):
#     AF_DEVICE_ID                  — user_devices.id minted by /me/hosted-devices
#     AF_HOSTED_DEVICE_ID           — hosted_devices.id (used for /reenroll)
#     AF_HOSTED_ROTATION_SECRET     — long-lived per-pod secret; entrypoint
#                                     exchanges it for a fresh enrollment_token
#                                     via POST /me/hosted-devices/:id/reenroll
#                                     on every pod start (pods are stateless)
#     AF_ENROLLMENT_TOKEN           — fallback token in case /reenroll is down;
#                                     valid 30 days from create time
#
#   Mode B — selftest / dev image (no hosted backing):
#     AF_API_KEY                    — owner API key (af_live_*)
#     AF_DEVICE_TOKEN               — JWT issued by /me/devices/issue
#
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
#   AF_SHELL_WHITELIST  — comma- or newline-separated list of programs the
#                         daemon may pass to `code_run_command`. Defaults
#                         below to a safe dev baseline (git, gh, node, npm,
#                         python, pip, pytest, kubectl, curl, bash, …) so
#                         autonomous plans aren't blocked by an empty
#                         scope. Owner override per-device via
#                         `POST /me/devices/:id/scope`. See Bug C in
#                         qa-reports/2026-05-25/hosted-autonomous-goals-e2e.md.

set -euo pipefail

: "${DISPLAY:=:99}"
: "${XVFB_WHD:=1440x900x24}"
: "${AF_ENABLE_SCREEN:=0}"

# Baseline shell whitelist for hosted daemons. Without this, autonomous
# LLM plans that call `git fetch` / `pytest` / `npm install` hit
# `shell_whitelist is empty; shell.exec disabled` and abort the session
# as COMPLETION_BLOCKED. The daemon merges per-device scope on top, so
# owners can narrow this through the cabinet without redeploying the
# image. Argument-level guard for `rm -r` lives in scope.py.
if [[ -z "${AF_SHELL_WHITELIST:-}" ]]; then
  export AF_SHELL_WHITELIST="ls, cat, head, tail, grep, find, wc, tree, pwd, whoami, env, echo, date, uname, which, file, stat
git, gh
node, npm, yarn, pnpm, npx, tsc
python, python3, pip, pip3, uv, pytest, ruff, black, mypy
docker, kubectl
curl, wget, jq
mkdir, rmdir, rm, cp, mv, touch, chmod, ln
tar, gzip, gunzip, unzip, zip
bash, sh, awk, sed
make, cmake, go, cargo, rustc"
fi

# Emit a wizard step event on stdout so the cabinet pod-log tail can
# attribute log lines to install-wizard row updates. Format must match
# installer/steps.json names and StepStatusRuntime from installer/steps.py
# (running|ok|error|skipped_surface|skipped_planned). See
# agentflow-code-docs/subsystems/install-wizard.mdx for the contract.
emit_step() {
  local name="$1" status="$2" detail="${3:-}"
  if [[ -n "$detail" ]]; then
    printf 'STEP %s %s %s\n' "$name" "$status" "$detail"
  else
    printf 'STEP %s %s\n' "$name" "$status"
  fi
}

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

# Mark hosted-only steps as skipped so the cabinet UI keeps row order.
emit_step request_permissions skipped_surface
emit_step install_daemon_binary skipped_surface
emit_step autostart_register skipped_surface

emit_step prepare_workspace running
mkdir -p "$HOME/.agentflow"
emit_step prepare_workspace ok

# /tmp/.X11-unix must exist with sticky 1777 before Xvfb starts. The
# image creates it at build time but if anything mounts an emptyDir over
# /tmp the directory is gone — re-create here. Without this Xvfb logs
# «euid != 0,directory /tmp/.X11-unix will not be created» and every
# X11 client (pyautogui, tk) fails with «Cannot connect to display».
if [[ ! -d /tmp/.X11-unix ]]; then
  mkdir -p /tmp/.X11-unix
fi
# chmod is no-op if already 1777; sudo so it works whether we're root or
# the agentflow user (the image gives agentflow NOPASSWD for chmod).
sudo -n chmod 1777 /tmp/.X11-unix 2>/dev/null || chmod 1777 /tmp/.X11-unix 2>/dev/null || true

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

# ─── Hosted-device enrollment ────────────────────────────────────────
#
# For pods born from /me/hosted-devices kind=daemon: the entrypoint trades
# the long-lived AF_HOSTED_ROTATION_SECRET for a fresh enrollment_token via
# POST /me/hosted-devices/:id/reenroll, then writes ~/.agentflow/auth.json
# in the format the daemon's auth.py expects. We do this every pod start
# because hosted pods are stateless (no PVC under ~/.agentflow).
write_hosted_auth() {
  local api_url="${AF_API_URL:-https://agentflow.website}"
  local device_id="${AF_DEVICE_ID:-}"
  local enrollment="${AF_ENROLLMENT_TOKEN:-}"
  local ttl="2592000"

  if [[ -z "$device_id" ]]; then
    return 1
  fi

  # If we have both hosted_device_id + rotation_secret, swap them for a
  # fresh enrollment_token first. Best-effort: on /reenroll failure we
  # fall back to whatever AF_ENROLLMENT_TOKEN was baked in at pod-create.
  if [[ -n "${AF_HOSTED_DEVICE_ID:-}" && -n "${AF_HOSTED_ROTATION_SECRET:-}" ]]; then
    local payload
    payload=$(printf '{"rotation_secret":"%s"}' "$AF_HOSTED_ROTATION_SECRET")
    local resp
    if resp=$(curl -fsS --max-time 15 \
        -H 'content-type: application/json' \
        -X POST \
        --data "$payload" \
        "${api_url}/_agents/me/hosted-devices/${AF_HOSTED_DEVICE_ID}/reenroll" 2>/dev/null); then
      # Extract enrollment_token + device_id without pulling a JSON parser
      # into the base image. Tolerant of either ordering.
      local re_token re_device re_ttl
      re_token=$(printf '%s' "$resp" | sed -n 's/.*"enrollment_token":"\([^"]*\)".*/\1/p')
      re_device=$(printf '%s' "$resp" | sed -n 's/.*"device_id":"\([^"]*\)".*/\1/p')
      re_ttl=$(printf '%s' "$resp" | sed -n 's/.*"ttl_sec":\([0-9]*\).*/\1/p')
      if [[ -n "$re_token" ]]; then
        enrollment="$re_token"
        echo "[entrypoint] /reenroll ok — fresh enrollment_token (ttl=${re_ttl:-?}s)" >&2
      fi
      if [[ -n "$re_device" ]]; then
        device_id="$re_device"
      fi
      if [[ -n "$re_ttl" ]]; then
        ttl="$re_ttl"
      fi
    else
      echo "[entrypoint] /reenroll failed, falling back to baked-in token" >&2
    fi
  fi

  if [[ -z "$enrollment" ]]; then
    echo "[entrypoint] no enrollment_token available — cannot write auth.json" >&2
    return 1
  fi

  local ws_url="${api_url//https:/wss:}/_agents/_devices/connect"
  ws_url="${ws_url//http:/ws:}"

  mkdir -p "$HOME/.agentflow"
  local auth_file="$HOME/.agentflow/auth.json"
  cat > "$auth_file" <<JSON
{
  "device_id": "${device_id}",
  "enrollment_token": "${enrollment}",
  "api_key": "${AF_API_KEY:-}",
  "ws_url": "${ws_url}"
}
JSON
  chmod 600 "$auth_file"
  echo "[entrypoint] wrote $auth_file (device_id=${device_id:0:8}…)" >&2
  return 0
}

# Detect mode and act accordingly.
if [[ -n "${AF_DEVICE_ID:-}" ]]; then
  emit_step verify_token running
  emit_step write_auth_json running
  if ! write_hosted_auth; then
    emit_step write_auth_json error "hosted enrollment failed"
    emit_step verify_token error "no enrollment_token"
    echo "[entrypoint] hosted enrollment failed — running selftest only" >&2
    exec agentflow-desktop selftest
  fi
  emit_step verify_token ok
  emit_step write_auth_json ok
  # install_opencode_cli runs inside the daemon (MCP tool computer.opencode.install).
  # Planned rows are display-only — emit so the cabinet keeps row order.
  emit_step install_pencil_mcp skipped_planned
  emit_step sync_skill_packs skipped_planned
  emit_step register_mcp_servers skipped_planned
elif [[ -z "${AF_API_KEY:-}" ]]; then
  echo "[entrypoint] AF_API_KEY missing — running selftest only" >&2
  exec agentflow-desktop selftest
fi

# Hand off. `exec` so signals (SIGTERM from kubectl delete) reach the
# Python process directly. No `--headless` flag — `agentflow-desktop run`
# is headless by default (Xvfb provides the display via env DISPLAY=:99).
# Passing `--headless` aborted the CLI with «unrecognized arguments» so
# the pod looped through CrashLoopBackOff until this was fixed.
# The daemon itself emits `STEP launch_daemon ok` after WS handshake and
# `STEP verify_install <status>` after the hello-world task completes.
emit_step launch_daemon running
echo "[entrypoint] handing off to agentflow-desktop run" >&2
exec agentflow-desktop run
