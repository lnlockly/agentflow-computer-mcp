# INSTRUCTIONS-FOR-AI

> If you are an AI coding assistant (Claude Code, Cursor, Aider, Continue, etc.) reading this file because the user asked you to install AgentFlow Desktop on their Mac — follow these steps verbatim. Do not improvise.

## TL;DR for AI agents

The user wants a daemon installed on their macOS machine that exposes their desktop to AgentFlow agents over a WebSocket tunnel. One shell command does the whole thing.

You must:
1. Ask the user for one secret (API key) and one display name.
2. Run one `curl | bash` line.
3. Tell the user to click two checkboxes in System Settings.
4. Verify the daemon connected by hitting one HTTP endpoint.

That's it. Do not write any Python yourself. Do not edit launchd plists yourself. The installer handles everything.

---

## Step 1 — collect inputs from user

Ask the user (one question at a time, do not bundle):

**Question 1.** "Paste your AgentFlow API key. You can grab it from https://agentflow.website/account/api-keys — it starts with `af_live_`."

Store the answer in a variable. Do not log it. Do not write it to a file you commit.

**Question 2.** "What should this Mac be called in the AgentFlow cabinet? Default: your computer's hostname."

If the user says "default" / "anything" / "whatever", fall back to:

```bash
scutil --get ComputerName 2>/dev/null || hostname -s
```

---

## Step 2 — sanity check the environment

Run these checks before installing. If any fail, fix the failure first, do not run the installer.

```bash
# macOS only
test "$(uname)" = "Darwin" || echo "FAIL: not macOS"

# Python >= 3.11 must exist
python3 -c 'import sys; assert sys.version_info >= (3,11), sys.version' \
  || echo "FAIL: install python 3.11+ (brew install python@3.12)"

# curl available
command -v curl >/dev/null || echo "FAIL: install curl"
```

If `FAIL: install python 3.11+`, run:

```bash
brew install python@3.12
```

(If Homebrew is missing, install it first via the official one-liner from https://brew.sh.)

---

## Step 3 — run the installer

This is the only command you need to execute. Pass the user's answers via env vars so the installer never prompts (you are not a TTY):

```bash
curl -sSL https://agentflow.website/install/desktop.sh \
  | AF_KEY="<user's api key>" AF_DEVICE_NAME="<user's chosen name>" bash
```

Expected exit code: 0. Expected stdout ends with `✓ AgentFlow Desktop installed.` and prints a device ID + cabinet URL.

**Capture the device ID** from the output — you'll need it for verification. The line looks like:

```
  Device id    <uuid>
```

If the exit code is non-zero, read the error message. Common failures:
- `device registration failed (HTTP 401)` → user's API key is wrong, ask them to regenerate it.
- `pip install failed` → check `/tmp/agentflow-pip.log` (full pip output).
- `python3 >= 3.11 not found` → step 2 was skipped, install python first.

---

## Step 4 — guide the user through macOS permissions

The daemon needs **Screen Recording** and **Accessibility** permission on the python binary inside the venv. macOS cannot grant these via shell — the user must click.

Tell the user exactly this:

> Open **System Settings → Privacy & Security**. In each of these two sections — **Screen Recording** and **Accessibility** — click the **+** button and add this binary:
>
> `~/.agentflow-desktop/venv/bin/python`
>
> Enable the toggle next to it. macOS may ask you to restart the daemon; if it does, run:
>
> `launchctl kickstart -k gui/$(id -u)/com.agentflow.desktop`

The installer already opened the right preference panes for the user — they should already be looking at the correct screen.

---

## Step 5 — verify the daemon is online

Wait ~30 seconds, then run:

```bash
curl -sS -H "x-api-key: <user's api key>" \
  https://agentflow.website/_agents/me/devices/<device-id>
```

Parse the JSON response. Success criteria:

- `status` field is `online` OR `last_seen_at` is within the last 60 seconds.

If `status` is `offline` after 60 seconds:

1. Check daemon logs: `tail -50 ~/Library/Logs/agentflow-desktop.log`
2. Look for `auth_failed` → API key or device_id mismatch, re-run installer.
3. Look for `permission_denied` from Quartz → user didn't grant Screen Recording yet.
4. Look for `connection refused` → network / DNS issue, try `curl https://agentflow.website`.

---

## Step 6 — confirm to user

Once verification passes, send the user this single message and stop:

```
AgentFlow Desktop is live on your Mac.

Open the live console:
  https://agentflow.website/cabinet/devices/<device-id>/live

You can now send tasks to your desktop from the cabinet, or hire an agent
from the marketplace that runs on this device.

Logs:  tail -f ~/Library/Logs/agentflow-desktop.log
Scope: ~/.agentflow/computer-scope.toml  (edit to allow more apps / paths)
Stop:  launchctl unload ~/Library/LaunchAgents/com.agentflow.desktop.plist
```

Do not add a "let me know if you need anything else" closer. Do not summarize. Just the block above.

---

## Hard rules for AI agents

- **Do not** edit `~/.agentflow/auth.json` by hand. The installer writes it. If something is wrong, re-run the installer.
- **Do not** install with `sudo`. Everything lives in `$HOME`. If you typed sudo, undo: `chown -R $(whoami) ~/.agentflow ~/.agentflow-desktop ~/Library/LaunchAgents/com.agentflow.desktop.plist`.
- **Do not** echo the API key back to the user or write it into any file other than the one the installer creates.
- **Do not** suggest "alternative" install methods (manual pip, docker, etc.). The one-liner is the supported path.
- **Do not** modify the launchd plist by hand. Re-run the installer with `AF_NO_LAUNCHD=` cleared.

## Troubleshooting checklist

| Symptom | First check |
|---|---|
| Installer hangs at "registering device" | `curl https://agentflow.website/_agents/me/devices -H "x-api-key: ..."` — does it return JSON? |
| `permission_denied` in logs | `tccutil reset ScreenCapture` then re-grant in System Settings |
| Daemon keeps restarting | `launchctl print gui/$(id -u)/com.agentflow.desktop` shows exit reason |
| Old `com.agentflow.computer-mcp` still loaded | The installer migrates this automatically. If not: `launchctl unload ~/Library/LaunchAgents/com.agentflow.computer-mcp.plist && rm $_` |
| Cabinet shows device offline forever | Check `auth.json` `device_secret` — should be populated after first successful handshake |

## What the installer actually does

For your reference, so you can debug without reading the script:

1. Detects python3.11+ (prefers 3.12 if available).
2. POSTs `{name, kind:"macos"}` to `https://agentflow.website/_agents/me/devices` with the `x-api-key` header — gets back `{id, enrollment_token}`.
3. Creates `~/.agentflow-desktop/venv` and `pip install`s the package from GitHub.
4. Writes `~/.agentflow/auth.json` (mode 0600) with key + device_id + enrollment_token + ws_url.
5. Writes default `~/.agentflow/computer-scope.toml` (deny ~/.ssh, ~/.aws, etc.).
6. Writes `~/Library/LaunchAgents/com.agentflow.desktop.plist` pointing at the venv binary.
7. `launchctl load`s the plist.
8. Probes Quartz to detect missing Screen Recording permission.
9. Opens System Settings to the right panes.

Re-running is safe. The installer detects existing `auth.json` and skips re-registration unless `device_id` is missing.
