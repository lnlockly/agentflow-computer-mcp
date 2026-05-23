# Windows tray app — spec (2026-05-23)

## TL;DR

Lightweight system-tray app for Windows that mirrors the Mac menu-bar app:
shows daemon status, local agents, recent cloud goals, daily budget, with
quick actions for opening the cabinet and restarting the daemon. Built on
`pystray` + native menus. No PyQt, no PyWebView in v1.

## Options considered

- **A. pystray + PyQt6 dialog** — richer UI but heavy dep + signing pain.
- **B. pystray + native menu** *(picked)* — small footprint, mirrors Mac drop-down.
- **C. PyWebView + tiny HTML** — neat but extra processes & WebView2 dependency.

v1 goes with **B**. Future settings dialog can come from any of the three
without disturbing the menu surface.

## Entry points

- Module: `python -m agentflow_computer_mcp.winapp`
- Console script: `agentflow-tray = agentflow_computer_mcp.winapp.__main__:main`
  (added to `pyproject.toml` only inside the `winapp/` subdir patch).
- CLI install/uninstall:
  - `python -m agentflow_computer_mcp.winapp install --autostart`
  - `python -m agentflow_computer_mcp.winapp uninstall`

## Menu layout

```
[icon]
  Подключено  (or "Демон не запущен" / "Локальные команды требуют Windows-pipe — в работе")
  ──────────
  Агенты ▸
    name (status)  ▸  Kill
    …
    (пусто)            — when daemon is down on Windows
  Цели ▸
    [pending] Title 1
    [running] Title 2
    …
  Бюджет: $X / $Y
  ──────────
  Открыть кабинет
  Перезапустить демон
  Выйти
```

## Sources

| Surface | Source | Module |
|---|---|---|
| Header (daemon up?) | `agents.socket.call("list")` (UNIX socket / Windows pipe — falls back gracefully) | `winapp/daemon_probe.py` |
| Agents submenu | same socket `list` | `winapp/daemon_probe.py` |
| Goals submenu | `GET /me/autonomous/goals` (last 5 by recency) | `winapp/cloud.py` |
| Budget | `GET /me/autonomous/budget` | `winapp/cloud.py` |

REST helpers come from the existing `agentflow_computer_mcp.cli.rest_client`
(read-only — no duplication). Socket reads use a Windows-pipe-aware shim
that returns `None` when the daemon is unreachable.

## Auto-start on login

Write `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`
value `AgentFlowTray` with `pythonw.exe -m agentflow_computer_mcp.winapp`
(quoted, full paths). `winapp.autostart.install()` / `.uninstall()`.

## Windows-pipe limitation (CRITICAL)

`agentflow_computer_mcp.agents.socket` is UNIX-only (`AF_UNIX`). On
Windows the tray:

- Catches `DaemonUnavailable` / `OSError` / `AttributeError` from
  `socket_client.call` and renders **"Локальные команды требуют
  Windows-pipe — в работе"** in the header.
- Greys out agent-related menu items (their handlers no-op + show toast).
- Cloud goals + budget keep working (pure REST).

Tracked as known limitation in RAG (`subsystems/windows-tray-app.mdx`).

## HiDPI icons

Bundle 16, 32, 48-px PNGs at `winapp/assets/logo-{N}.png`. If asset
missing, synthesise a flat-colour PIL image at runtime so dev installs
still work.

## Threading

`pystray.Icon.run()` blocks the main thread on Windows. A background
`threading.Thread(daemon=True)` polls cloud + daemon every 30 s and calls
`Icon.update_menu()`. Polling errors are swallowed; the menu just keeps
stale data with a faint dash in the header.

## Notifications (optional v1)

When the polling thread detects a goal that flipped to `done`, fire
`Icon.notify("Цель завершена", title="AgentFlow")`. Errors swallowed.

## Quality gates

- `pytest tests/winapp/` green.
- `ruff check winapp tests/winapp` clean.
- `python -m agentflow_computer_mcp.winapp --version` exits 0.
- Manual smoke: file qa-issue (no Windows runner — visual check pending).

## Out of scope (v1)

- Settings dialog / scope editor.
- Goal creation from tray (deep link to web cabinet instead).
- Codesigning (separate task — file).
- WebView2-based richer UI.
