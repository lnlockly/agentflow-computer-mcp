"""PyInstaller entry point for `agentflow-tray.exe`.

Thin shim that delegates to `agentflow_computer_mcp.winapp.__main__`.
Kept separate from `setup_gui.py` so PyInstaller can build two EXEs
from the same Analysis+PYZ:

  - `agentflow-desktop-setup.exe` (wizard + daemon, from setup_gui.py)
  - `agentflow-tray.exe`          (tray only, from this file)

Both share the bundled CPython + agentflow_computer_mcp + pystray.

CLI surface (passed straight to winapp.__main__):

  agentflow-tray.exe              → run tray (blocks)
  agentflow-tray.exe install      → write Run-key autostart entry
  agentflow-tray.exe uninstall    → remove Run-key entry
  agentflow-tray.exe --version    → print package version + exit
"""
from __future__ import annotations

import sys


def main() -> int:
    from agentflow_computer_mcp.winapp.__main__ import main as winapp_main

    return int(winapp_main() or 0)


if __name__ == "__main__":
    sys.exit(main())
