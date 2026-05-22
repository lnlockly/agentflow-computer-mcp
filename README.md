# agentflow-computer-mcp

Local daemon that lets AgentFlow's Eliza agents — and a direct LLM driver — control your desktop on **macOS, Linux, or Windows**. The package ships two surfaces in one wheel.

## Platform support

| OS | Screen | Input | Windows | Clipboard | Notes |
|---|---|---|---|---|---|
| macOS | Quartz CGDisplayCreateImage (~5 ms) | pyautogui | Quartz CGWindowList | pbcopy/pbpaste | grant Accessibility + Screen Recording |
| Linux X11 | mss (~30 ms) | pyautogui | wmctrl + xdotool | xclip / xsel | requires `wmctrl`, `xdotool`, `xclip` |
| Linux Wayland | grim | pyautogui (XWayland) | XWayland clients only | wl-copy / wl-paste | requires `grim`, `wl-clipboard` |
| Windows | mss (~20 ms) | pyautogui | pywin32 EnumWindows | pyperclip | requires Python 3.11+ |

Every OS-specific call routes through `agentflow_computer_mcp.platform.backend` (one Protocol, three implementations).

Run `agentflow-desktop selftest` to see an OK/FAIL grid for each backend method on the current host.

| Surface | Console script | Use |
|---|---|---|
| Single-process daemon | `agentflow-desktop` | HTTP viewer at http://localhost:8765 with chat input, MJPEG live stream, action log. Anthropic tool-use loop against your Mac + the AgentFlow REST API. |
| MCP server | `agentflow-computer-mcp` | stdio for Claude Desktop / Cursor, or `ws` reverse-tunnel for the AgentFlow cloud. Scoped tools only. |

## Tools exposed

Mac control (both surfaces):

- `computer.screen.capture(region?)` — PNG via Quartz, resized to 1280px wide
- `computer.mouse.click/move/scroll`
- `computer.keyboard.type/key/shortcut`
- `computer.window.list/focus`
- `computer.fs.read/list/write` (write requires confirm + allow_paths)
- `computer.shell.exec` (whitelist-only)
- `computer.clipboard.read/write`

Five paths can never be read or written, regardless of user scope: `~/.ssh`, `~/.config`, `~/Library/Keychains`, `~/.aws`, `~/.gnupg`.

The `agentflow-desktop` daemon adds an extra layer for the local LLM loop:

- `screen_capture`, `screen_region`, `mouse_click`, `keyboard_type`, `keyboard_shortcut`, `activate_app`, `window_list`, `read_terminal` — direct Mac control
- `chrome_open_url`, `chrome_tabs`, `chrome_eval` — drive the user's real Google Chrome (uses their cookies) via AppleScript
- `browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_press`, `browser_eval` — headed Playwright Chromium (separate from user's Chrome)
- `clipboard_read`, `clipboard_write`, `wait`, `task_complete`
- `af_*` — AgentFlow REST API (see below)

### LLM-facing `af_*` tools

| Tool | Wraps |
|---|---|
| `af_list_projects` / `af_get_project` / `af_create_project` / `af_approve_project` / `af_list_project_events` | `/_agents/me/projects/...` |
| `af_list_devices` / `af_get_device` | `/_agents/me/devices/...` |
| `af_list_agents` / `af_send_agent_message` | `/_agents/me/agents/...` |
| `af_send_telegram_message` | `/_agents/me/telegram/send` |
| `af_post_matrix_room` | `/_agents/me/matrix/send` |

These run inline in the driver loop and let the LLM act on AgentFlow itself (create projects, ping agents, broadcast) without scripting curl. The system prompt advertises them so the model picks them up when a task is platform-side.

## Install

From the AgentFlow cabinet at `https://agentflow.website/cabinet/devices`, click "Add device" — the modal prints the one-liner for the matching OS.

### Windows (recommended: setup.exe)

Download the GUI installer:

[**agentflow-desktop-setup.exe**](https://github.com/lnlockly/agentflow-computer-mcp/releases/latest/download/agentflow-desktop-setup.exe)

Double-click the file, paste the invite code from the cabinet into the single text field, click **Install**. The wizard pip-installs the package into your user site, writes `%USERPROFILE%\.agentflow\auth.json`, registers a `AgentFlowDesktop` scheduled task at logon, and starts the daemon. The «Open cabinet» button at the end jumps straight to your device's live view.

The .exe is unsigned, so Windows SmartScreen shows «Windows protected your PC» the first time. Click **More info → Run anyway**. Power users can keep using the PowerShell one-liner below.

The invite code is base64url of `{"k":"<api_key>","d":"<device_id>","t":"<device_token>"}`. If you prefer pasting raw values, expand «Расширенные настройки» inside the wizard.

### macOS

```bash
curl -sSL https://agentflow.website/install/computer-mcp.sh | \
  AF_KEY=<api-key> AF_DEVICE_ID=<uuid> AF_DEVICE_TOKEN=<one-time-token> bash
```

Installs via pip-user, writes `~/.agentflow/auth.json` (mode 0600), drops a default `~/.agentflow/computer-scope.toml`, loads a launchd plist at `~/Library/LaunchAgents/com.agentflow.computer-mcp.plist`. After install, grant Accessibility + Screen Recording to your terminal in System Settings → Privacy & Security.

### Linux (Debian/Ubuntu, Fedora, Arch)

```bash
curl -sSL https://agentflow.website/install/computer-mcp.sh | \
  AF_KEY=<api-key> AF_DEVICE_ID=<uuid> AF_DEVICE_TOKEN=<token> bash
```

Detects the package manager (apt / dnf / pacman), installs `wmctrl xdotool xclip xvfb` (and `grim wl-clipboard slurp` on Wayland), pip-installs the package, then drops a systemd user unit at `~/.config/systemd/user/agentflow-desktop.service` and enables it. Auth + scope live in `~/.agentflow/` with an XDG mirror at `~/.config/agentflow/`.

### Windows (PowerShell)

```powershell
$env:AF_KEY="..."; $env:AF_DEVICE_ID="..."; $env:AF_DEVICE_TOKEN="..."
iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex
```

For cmd.exe users:

```bat
curl -sSL https://agentflow.website/install/computer-mcp.bat -o install.bat && install.bat
```

Both pipe through PowerShell with `ExecutionPolicy Bypass`, pip-install the package, and register a Task Scheduler entry named `AgentFlowDesktop` that runs at logon.

The canonical scripts are `install.sh` / `install.ps1` / `install.bat` at the repo root (and mirrored in `scripts/` for backward compat). Matching `scripts/uninstall.sh` / `scripts/uninstall.ps1` remove the autostart unit and `~/.agentflow/auth.json`.

## Run

### `agentflow-desktop` — full daemon (local LLM loop)

```bash
agentflow-desktop run                                 # full daemon, port 8765
agentflow-desktop run --port 9000 --fps 12            # custom viewer port + capture fps
agentflow-desktop run --no-af-tools                   # hide af_* tools from the LLM
agentflow-desktop drive "screen_capture, then window_list, summarize"
agentflow-desktop tools                               # list LLM-facing tools
agentflow-desktop health                              # probe screen capture + AF API
agentflow-desktop selftest                            # OS-agnostic backend probe (no key needed)
agentflow-desktop version
```

API key resolution order: `--api-key` → `$AGENTFLOW_API_KEY` → `$AF_API_KEY` → `~/.agentflow/auth.json`.

The viewer's preset library loads from `presets/desktop-tasks.yaml` (or `--presets path/to/file.yaml`). 16 tasks ship by default.

### `agentflow-computer-mcp` — MCP server

```bash
agentflow-computer-mcp --version
agentflow-computer-mcp --mode stdio   # MCP stdio (Claude Desktop / Cursor / Continue)
agentflow-computer-mcp --mode ws      # reverse-tunnel to AgentFlow cloud (default in launchd)
```

## Auth

`~/.agentflow/auth.json` (mode 0600):

```json
{
  "api_key": "af_live_...",
  "device_id": "uuid",
  "enrollment_token": "one-time, valid 24h",
  "device_secret": "",
  "ws_url": "wss://agentflow.website/_devices/connect"
}
```

On first WS handshake the server returns `hello_ack` with a long-lived `device_secret`. The client persists it and drops the enrollment token.

WebSocket request headers:

- `x-api-key` — AgentFlow API key
- `x-device-id` — UUID assigned at registration
- `x-device-secret` after first connect, OR `x-enrollment-token` on first connect

## Protocol (MCP `ws` mode)

JSON over WebSocket:

```jsonc
// server → client
{ "type": "tool_call_request", "id": "<uuid>", "name": "computer.screen.capture", "args": {} }

// client → server
{ "type": "tool_call_result", "id": "<uuid>", "result": { "mime": "image/png", "base64": "...", "size_bytes": 12345 } }
// or
{ "type": "tool_call_result", "id": "<uuid>", "error": { "code": "ScopeDenied", "message": "..." } }

// both directions, every 15s
{ "type": "heartbeat", "ts": 1716300000000 }
```

Heartbeat timeout: 45s. Reconnect uses exponential backoff capped at 60s with jitter.

## Scope config

`~/.agentflow/computer-scope.toml`:

```toml
allow_apps = []
allow_paths = ["~/Documents/agent-workspace"]
deny_paths = ["~/.ssh", "~/.config", "~/Library/Keychains", "~/.aws", "~/.gnupg"]
shell_whitelist = ["ls", "pwd", "date"]
confirm_before = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd = 2.0
```

Hard rules (config cannot override):

- The five default `deny_paths` are always denied
- `fs.write` requires non-empty `allow_paths`
- `shell.exec` requires non-empty `shell_whitelist`
- Tools in `confirm_before` show a native macOS confirm dialog every call

## Self-modification

The daemon can request its own code to change via `selfmod_request_change`.
Requests land in `~/.agentflow-desktop/selfmod-queue.jsonl`; a background
worker spawns `claude -p` to drive the edit and open a PR. Both auto-merge
and auto-`pip install --upgrade` are off by default.

```bash
agentflow-desktop selfmod list
agentflow-desktop selfmod retry <request_id>
agentflow-desktop selfmod cancel <request_id>
agentflow-desktop run --selfmod-automerge --selfmod-autoapply
```

Full threat model + storage shape: [docs/SELFMOD.md](docs/SELFMOD.md).

## Dev

```bash
pip install -e ".[dev]"
pytest -v          # 50 tests
ruff check src tests
```

## License

Internal use. Part of the AgentFlow workspace.
