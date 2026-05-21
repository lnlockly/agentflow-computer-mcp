# agentflow-computer-mcp

Local MCP daemon that puts your Mac behind an AgentFlow agent. The agent sees your screen, drives the mouse and keyboard, runs shell commands inside a whitelist, and reads / writes files inside a sandbox you control. All traffic flows through a single outbound WebSocket to `agentflow.website` — no inbound ports.

> **Status:** macOS only (Quartz + pyautogui). Linux / Windows: see [self-host docs](https://agentflow.website/self-host).

## Quick install (60 seconds)

```bash
curl -sSL https://agentflow.website/install/desktop.sh | bash
```

The script asks for your API key, registers the device, sets up a venv, and loads a launchd job. You grant Screen Recording + Accessibility once. Then the cabinet at `https://agentflow.website/cabinet/devices` shows your Mac online and ready to receive tasks.

Already running an AI assistant in your terminal? Point it at [`INSTRUCTIONS-FOR-AI.md`](./INSTRUCTIONS-FOR-AI.md) and it will do the install for you.

## What the agent gets

| Tool | What it does |
|---|---|
| `computer.screen.capture(region?)` | PNG via Quartz, resized to 1280px wide |
| `computer.mouse.click(x,y)` / `move` / `scroll` | Cursor control via pyautogui |
| `computer.keyboard.type(text)` / `key(...)` / `shortcut(...)` | Input synthesis |
| `computer.window.list()` / `focus(app)` | Cocoa window enumeration + activation |
| `computer.fs.read(path)` / `list(dir)` | Read inside `allow_paths` |
| `computer.fs.write(path, content)` | Write inside `allow_paths`, requires confirm |
| `computer.shell.exec(cmd)` | Run inside `shell_whitelist`, requires confirm |
| `computer.clipboard.read()` / `write(text)` | Pasteboard |

Five paths are denied to every tool, no matter what the user scope says: `~/.ssh`, `~/.config`, `~/Library/Keychains`, `~/.aws`, `~/.gnupg`.

## Architecture

```
[Your Mac]                                  [agentflow.website]
agentflow-desktop daemon          ←→   agentflow-agents
  ├─ ws_client (outbound WS)            ├─ /me/devices CRUD
  ├─ tool dispatcher                    ├─ POST /me/devices/:id/dispatch_task
  ├─ scope guard (deny_paths,           ├─ GET /me/devices/:id/tasks/stream (SSE)
  │   shell_whitelist, confirm)         └─ device registry + WS hub
  ├─ native confirm dialog (TCC)
  └─ launchd-managed lifecycle
```

The Mac speaks first: the daemon opens an outbound WebSocket to `wss://agentflow.website/_devices/connect`. The server holds the socket open and pushes `tool_call_request` frames whenever an attached agent (or the cabinet UI) wants to act on this device. The daemon replies with `tool_call_result` — either a successful payload or a structured error. Server never initiates a connection inward.

## Auth

`~/.agentflow/auth.json` (mode 0600, written by the installer):

```json
{
  "api_key": "af_live_…",
  "device_id": "uuid",
  "enrollment_token": "one-time, 24h ttl",
  "device_secret": "",
  "ws_url": "wss://agentflow.website/_devices/connect",
  "api_base": "https://agentflow.website"
}
```

First WS handshake exchanges `enrollment_token` for a long-lived `device_secret`. The client persists the secret and drops the enrollment token on disk.

WebSocket request headers:

- `x-api-key` — AgentFlow API key (owner of the device)
- `x-device-id` — UUID assigned at registration
- `x-device-secret` after first connect, OR `x-enrollment-token` on first connect

## Scope config

`~/.agentflow/computer-scope.toml`:

```toml
allow_apps          = ["Safari", "Cursor", "Terminal"]
allow_paths         = ["~/Documents/agent-workspace"]
deny_paths          = ["~/.ssh", "~/.config", "~/Library/Keychains", "~/.aws", "~/.gnupg"]
shell_whitelist     = ["ls", "pwd", "date", "git"]
confirm_before      = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd          = 2.0
```

Hard rules baked into the daemon (config cannot override):

- The five default `deny_paths` are always denied.
- `fs.write` is denied unless `allow_paths` is non-empty AND the target resolves inside one.
- `shell.exec` is denied unless `shell_whitelist` is non-empty AND argv[0] is on it.
- Anything listed in `confirm_before` triggers a native macOS confirm dialog on every call.

## Protocol

JSON frames over the single outbound WebSocket:

```jsonc
// server → client
{ "type": "tool_call_request", "id": "<uuid>", "name": "computer.screen.capture", "args": {} }

// client → server (success)
{ "type": "tool_call_result", "id": "<uuid>", "result": {
    "mime": "image/png", "base64": "…", "size_bytes": 12345
}}

// client → server (failure)
{ "type": "tool_call_result", "id": "<uuid>", "error": {
    "code": "ScopeDenied", "message": "shell.exec: 'rm' not in whitelist"
}}

// both directions, every 15s
{ "type": "heartbeat", "ts": 1716300000000 }
```

Heartbeat timeout: 45s. Reconnect uses exponential backoff capped at 60s with jitter.

## CLI

```bash
agentflow-computer-mcp --version
agentflow-computer-mcp --mode stdio   # MCP stdio (Claude Desktop / Cursor / Continue)
agentflow-computer-mcp --mode ws      # reverse-tunnel to AgentFlow cloud (default in launchd)
```

## Manage the daemon

```bash
# Status
launchctl print gui/$(id -u)/com.agentflow.desktop | head

# Restart
launchctl kickstart -k gui/$(id -u)/com.agentflow.desktop

# Stop / start
launchctl unload ~/Library/LaunchAgents/com.agentflow.desktop.plist
launchctl load   ~/Library/LaunchAgents/com.agentflow.desktop.plist

# Logs (stdout + stderr merged)
tail -f ~/Library/Logs/agentflow-desktop.log
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.agentflow.desktop.plist
rm ~/Library/LaunchAgents/com.agentflow.desktop.plist
rm -rf ~/.agentflow-desktop
rm ~/.agentflow/auth.json
# Optional: revoke the device from the cabinet
curl -X DELETE -H "x-api-key: $AF_KEY" https://agentflow.website/_agents/me/devices/<id>
```

## Dev

```bash
pip install -e ".[dev]"
pytest -v
ruff check src tests
```

## Security model

- Outbound-only network: no inbound port, no Bonjour, no LAN discovery.
- `auth.json` is `0600` and never logged. Daemon redacts `api_key` and `device_secret` substrings from stdout / stderr.
- TCC-gated APIs (`screen.capture`, `mouse.*`, `keyboard.*`) require explicit user grant in System Settings.
- The five default `deny_paths` are compiled into the scope module (`src/agentflow_computer_mcp/scope.py`). User config can extend, not override.
- Every tool call carries a server-generated `id`; the daemon includes it verbatim in the result so replay is detectable.
- `confirm_before` produces a native `osascript` dialog — agent cannot suppress it.

## Related

- [`INSTRUCTIONS-FOR-AI.md`](./INSTRUCTIONS-FOR-AI.md) — drop-in guide for AI assistants installing this for a user.
- [AgentFlow Desktop spec](https://github.com/lnlockly/agentflow-code-docs/blob/main/src/content/docs/subsystems/desktop.mdx) — internal RAG entry.
- [Cabinet live view](https://agentflow.website/cabinet/devices) — connect, watch, dispatch.

## License

Internal use. Part of the AgentFlow workspace.
