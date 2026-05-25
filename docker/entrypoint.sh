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
  if ! write_hosted_auth; then
    echo "[entrypoint] hosted enrollment failed — running selftest only" >&2
    exec agentflow-desktop selftest
  fi
elif [[ -z "${AF_API_KEY:-}" ]]; then
  echo "[entrypoint] AF_API_KEY missing — running selftest only" >&2
  exec agentflow-desktop selftest
fi

# ─── Preinstall wizard ──────────────────────────────────────────────
#
# AF_PREINSTALL is a comma-separated list of tool slugs the user picked
# at hosted-device create-time ("opencode,nodejs,python_ds"). For each
# slug we run the install block and POST a status event back to the
# backend so the cabinet can render per-tool progress.
#
# Idempotent on a restarted pod: each tool's own command is a no-op if
# already installed (curl install scripts re-write the same binary, apt
# is a no-op for already-installed packages). Failures are non-fatal —
# the daemon still boots; the failed step is just visible in the cabinet.
preinstall_event() {
  local tool="$1"
  local status="$2"
  local detail="${3:-}"
  if [[ -z "${AF_HOSTED_DEVICE_ID:-}" || -z "${AF_INTERNAL_SECRET:-}" ]]; then
    return 0
  fi
  local api_url="${AF_API_URL:-https://agentflow.website}"
  # Use python3 to JSON-encode the body so embedded quotes / newlines in
  # `detail` don't blow up the request. Falls back to a stripped string
  # if python3 isn't on PATH (shouldn't happen — image has it).
  local payload
  if command -v python3 >/dev/null 2>&1; then
    payload=$(TOOL="$tool" STATUS="$status" DETAIL="$detail" python3 -c '
import json, os
print(json.dumps({
    "tool": os.environ["TOOL"],
    "status": os.environ["STATUS"],
    "detail": os.environ.get("DETAIL", "")[:2000],
}))
')
  else
    local safe_detail="${detail//\"/\'}"
    payload=$(printf '{"tool":"%s","status":"%s","detail":"%s"}' "$tool" "$status" "${safe_detail:0:2000}")
  fi
  curl -fsS --max-time 10 \
    -H 'content-type: application/json' \
    -H "x-agentflow-secret: ${AF_INTERNAL_SECRET}" \
    -X POST \
    --data "$payload" \
    "${api_url}/_agents/internal/hosted-devices/${AF_HOSTED_DEVICE_ID}/preinstall-event" \
    >/dev/null 2>&1 || true
}

run_preinstall_step() {
  local tool="$1"
  echo "[preinstall] $tool starting" >&2
  preinstall_event "$tool" "installing"
  local log_file
  log_file=$(mktemp)
  local rc=0
  case "$tool" in
    opencode)
      # AI coding CLI. The install script drops a binary into ~/.opencode/bin.
      # We then write a minimal config pointing at AgentFlow's hosted LLM
      # router so the user doesn't have to wire an api key by hand.
      (
        set -e
        curl -fsSL https://opencode.ai/install | bash
        mkdir -p "$HOME/.config/opencode"
        AUTH_FILE="$HOME/.agentflow/auth.json" python3 - <<'PY' > "$HOME/.config/opencode/opencode.json"
import json, os, sys
auth_path = os.environ.get("AUTH_FILE", "")
api_key = ""
if auth_path and os.path.exists(auth_path):
    try:
        with open(auth_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        api_key = data.get("api_key") or ""
    except Exception:
        api_key = ""
config = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {
        "agentflow": {
            "name": "AgentFlow",
            "npm": "@ai-sdk/openai-compatible",
            "options": {
                "baseURL": "https://agentflow.website/_agents/llm/v1",
                "apiKey": api_key,
            },
            "models": {
                "claude-sonnet-4-6": {"name": "Claude Sonnet 4.6"},
                "claude-opus-4-7":   {"name": "Claude Opus 4.7"},
            },
        },
    },
}
sys.stdout.write(json.dumps(config, indent=2))
PY
      ) >"$log_file" 2>&1 || rc=$?
      ;;
    codex)
      npm install -g @openai/codex >"$log_file" 2>&1 || rc=$?
      ;;
    nodejs)
      # Daemon image already has node 20 baked in. We install npm + npx
      # via apt for users who want the classic dev tooling on PATH and
      # bump yarn while we're here.
      (
        set -e
        sudo -n apt-get update -y
        sudo -n apt-get install -y --no-install-recommends nodejs npm
        sudo -n npm install -g yarn
      ) >"$log_file" 2>&1 || rc=$?
      ;;
    python_ds)
      pip install --no-cache-dir --quiet jupyter pandas numpy >"$log_file" 2>&1 || rc=$?
      ;;
    ffmpeg)
      (
        set -e
        sudo -n apt-get update -y
        sudo -n apt-get install -y --no-install-recommends ffmpeg
      ) >"$log_file" 2>&1 || rc=$?
      ;;
    *)
      echo "[preinstall] unknown tool '$tool' — skipping" >&2
      rm -f "$log_file"
      preinstall_event "$tool" "failed" "unknown tool slug"
      return 0
      ;;
  esac
  if [[ $rc -eq 0 ]]; then
    echo "[preinstall] $tool ok" >&2
    preinstall_event "$tool" "installed"
  else
    # Tail of the install log gives the cabinet a debuggable hint.
    local tail_out
    tail_out=$(tail -c 1800 "$log_file" 2>/dev/null || true)
    echo "[preinstall] $tool failed (rc=$rc) — see cabinet for log tail" >&2
    preinstall_event "$tool" "failed" "$tail_out"
  fi
  rm -f "$log_file"
}

if [[ -n "${AF_PREINSTALL:-}" ]]; then
  IFS=',' read -ra _PREINSTALL_TOOLS <<< "$AF_PREINSTALL"
  for _tool in "${_PREINSTALL_TOOLS[@]}"; do
    _tool="${_tool// /}"
    [[ -z "$_tool" ]] && continue
    run_preinstall_step "$_tool"
  done
fi

# Hand off. `exec` so signals (SIGTERM from kubectl delete) reach the
# Python process directly. No `--headless` flag — `agentflow-desktop run`
# is headless by default (Xvfb provides the display via env DISPLAY=:99).
# Passing `--headless` aborted the CLI with «unrecognized arguments» so
# the pod looped through CrashLoopBackOff until this was fixed.
echo "[entrypoint] handing off to agentflow-desktop run" >&2
exec agentflow-desktop run
