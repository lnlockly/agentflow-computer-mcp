# AgentFlow Tray — Windows

Lightweight system-tray app that mirrors the Mac menu-bar (`macapp/`).
The implementation lives under `src/agentflow_computer_mcp/winapp/` so
PyInstaller picks it up automatically via the existing
`collect_submodules("agentflow_computer_mcp")` line in
`installer/agentflow-setup.spec`. This directory keeps the Windows-only
assets + this README so the repo root stays clean.

## Run

```powershell
pip install -e .[dev] pystray Pillow
python -m agentflow_computer_mcp.winapp
```

## Autostart

```powershell
python -m agentflow_computer_mcp.winapp install --autostart
python -m agentflow_computer_mcp.winapp uninstall
```

## Layout

| Path | Role |
|---|---|
| `src/agentflow_computer_mcp/winapp/__main__.py` | argparse entry, `python -m …` |
| `src/agentflow_computer_mcp/winapp/tray.py` | pystray glue |
| `src/agentflow_computer_mcp/winapp/menu.py` | pure-data menu builder (tested) |
| `src/agentflow_computer_mcp/winapp/state.py` | dataclasses for menu state |
| `src/agentflow_computer_mcp/winapp/daemon_probe.py` | UNIX/Win-pipe socket probe |
| `src/agentflow_computer_mcp/winapp/cloud.py` | REST goals + budget |
| `src/agentflow_computer_mcp/winapp/autostart.py` | Run-key install/uninstall |
| `src/agentflow_computer_mcp/winapp/actions.py` | open browser, restart daemon |
| `src/agentflow_computer_mcp/winapp/icon.py` | HiDPI PNG loader |
| `src/agentflow_computer_mcp/winapp/assets/logo-{16,32,48}.png` | tray icons |
| `winapp/README.md` | this file |

## Known limitations

- Windows-pipe daemon socket not yet shipped (#94). Until it lands the
  tray shows "Локальные команды требуют Windows-pipe — в работе" and
  greys out agent-specific menu items. Cloud goals + budget keep working.
- PyInstaller-built tray binaries trigger Windows Defender false
  positives. Codesigning task pending separately from this PR.

See `docs/specs/2026-05-23-windows-tray-app.md` for the design + critique.
