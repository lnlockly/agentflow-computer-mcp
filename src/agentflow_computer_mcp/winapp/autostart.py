"""Windows auto-start via `HKEY_CURRENT_USER\\…\\Run`.

`install()` writes a Run-key entry pointing at `pythonw.exe -m
agentflow_computer_mcp.winapp`. `uninstall()` deletes it. `read()`
returns the current value or `None`.

All three accept an optional `opener` to make the writers fully testable
without `winreg` on the host (Mac/Linux dev boxes). The default opener
imports `winreg` lazily — on non-Windows hosts it raises
`UnsupportedPlatform`, which the CLI surfaces as a friendly error.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AgentFlowTray"


class UnsupportedPlatform(RuntimeError):
    """Raised when autostart is called outside Windows without an injected opener."""


@dataclass
class RegOps:
    """Minimal surface needed from `winreg`. Real impl wraps stdlib."""

    set_value: Any
    get_value: Any
    delete_value: Any


class RegOpener(Protocol):
    def __call__(self) -> RegOps: ...


def _real_opener() -> RegOps:
    if sys.platform != "win32":
        raise UnsupportedPlatform("autostart only works on Windows")
    import winreg  # type: ignore[import-not-found]

    def set_value(name: str, value: str) -> None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def get_value(name: str) -> str | None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ
            ) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return str(value)

    def delete_value(name: str) -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return False
        return True

    return RegOps(set_value=set_value, get_value=get_value, delete_value=delete_value)


def _default_command() -> str:
    """`pythonw.exe -m agentflow_computer_mcp.winapp` quoted for Run-key."""
    exe = Path(sys.executable)
    # Prefer pythonw to avoid a console window on login. On Windows it
    # lives next to python.exe under the same Scripts/ root.
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else exe
    return f'"{runner}" -m agentflow_computer_mcp.winapp'


def install(command: str | None = None, opener: RegOpener | None = None) -> str:
    """Write the Run-key entry. Returns the command that was written."""
    ops = (opener or _real_opener)()
    cmd = command or _default_command()
    ops.set_value(VALUE_NAME, cmd)
    return cmd


def uninstall(opener: RegOpener | None = None) -> bool:
    ops = (opener or _real_opener)()
    return ops.delete_value(VALUE_NAME)


def read(opener: RegOpener | None = None) -> str | None:
    ops = (opener or _real_opener)()
    return ops.get_value(VALUE_NAME)
