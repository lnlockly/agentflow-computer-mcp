# agentflow-computer-mcp

Local MCP server that lets AgentFlow's Eliza agents control your macOS desktop over a WebSocket reverse-tunnel.

## What it does

Exposes scoped tools to an attached agent:

- `computer.screen.capture(region?)` — PNG via Quartz, resized to 1280px wide
- `computer.mouse.click/move/scroll`
- `computer.keyboard.type/key/shortcut`
- `computer.window.list/focus`
- `computer.fs.read/list/write` (write requires confirm + allow_paths)
- `computer.shell.exec` (whitelist-only)
- `computer.clipboard.read/write`

Five paths can never be read or written, regardless of user scope: `~/.ssh`, `~/.config`, `~/Library/Keychains`, `~/.aws`, `~/.gnupg`.

## Install

From the AgentFlow cabinet at `https://agentflow.website/cabinet/devices`, click "Add device" — the modal prints a one-liner of the form:

```bash
curl -sSL https://agentflow.website/install/computer-mcp.sh | \
  AF_KEY=<api-key> AF_DEVICE_TOKEN=<one-time-token> AF_DEVICE_ID=<uuid> bash
```

The installer:

1. `pip install --user` the package
2. Writes `~/.agentflow/auth.json` (mode 0600) with your key + enrollment token
3. Writes a default `~/.agentflow/computer-scope.toml`
4. Drops a launchd plist into `~/Library/LaunchAgents/com.agentflow.computer-mcp.plist`
5. Loads it via `launchctl`

After install you must grant Accessibility and Screen Recording permissions to your terminal / shell / python binary in System Settings → Privacy & Security.

Start it:

```bash
launchctl start com.agentflow.computer-mcp
tail -f ~/Library/Logs/agentflow-computer-mcp.log
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

On first successful handshake the server returns `hello_ack` with a long-lived `device_secret`. The client persists it and drops the enrollment token.

WebSocket headers on connect:

- `x-api-key`: AgentFlow API key
- `x-device-id`: assigned by `POST /me/devices`
- `x-device-secret` (after first connect) or `x-enrollment-token` (first connect only)

## Protocol

JSON over WS:

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

Hard rules (cannot be overridden by user config):

- The five default deny_paths are always denied
- `fs.write` requires non-empty `allow_paths`
- `shell.exec` requires non-empty `shell_whitelist`
- Tools in `confirm_before` show a native macOS confirm dialog every call

## Run manually

```bash
pip install -e .
python -m agentflow_computer_mcp --version
python -m agentflow_computer_mcp --mode stdio   # MCP stdio mode (for Claude Desktop etc.)
python -m agentflow_computer_mcp --mode ws      # reverse-tunnel to AgentFlow
```

## Dev

```bash
pip install -e ".[dev]"
pytest -v
ruff check src tests
```

## License

Internal use. Part of the AgentFlow workspace.
