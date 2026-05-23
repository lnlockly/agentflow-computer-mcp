"""Autostart Run-key install/uninstall — fully mocked `winreg`."""
from __future__ import annotations

from agentflow_computer_mcp.winapp import autostart


class FakeReg:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def ops(self) -> autostart.RegOps:
        def set_value(name: str, value: str) -> None:
            self.values[name] = value

        def get_value(name: str) -> str | None:
            return self.values.get(name)

        def delete_value(name: str) -> bool:
            return self.values.pop(name, None) is not None

        return autostart.RegOps(set_value=set_value, get_value=get_value, delete_value=delete_value)


def test_install_writes_run_key_value() -> None:
    reg = FakeReg()
    cmd = autostart.install(command='"C:\\Python311\\pythonw.exe" -m agentflow_computer_mcp.winapp', opener=reg.ops)
    assert reg.values[autostart.VALUE_NAME] == cmd
    assert "pythonw.exe" in cmd


def test_install_uses_default_command_when_omitted() -> None:
    reg = FakeReg()
    cmd = autostart.install(opener=reg.ops)
    assert reg.values[autostart.VALUE_NAME] == cmd
    assert "-m agentflow_computer_mcp.winapp" in cmd


def test_read_returns_none_when_missing() -> None:
    reg = FakeReg()
    assert autostart.read(opener=reg.ops) is None


def test_read_returns_value_after_install() -> None:
    reg = FakeReg()
    autostart.install(command="some.exe", opener=reg.ops)
    assert autostart.read(opener=reg.ops) == "some.exe"


def test_uninstall_removes_value() -> None:
    reg = FakeReg()
    autostart.install(command="x.exe", opener=reg.ops)
    assert autostart.uninstall(opener=reg.ops) is True
    assert autostart.read(opener=reg.ops) is None
    # Idempotent: second call returns False
    assert autostart.uninstall(opener=reg.ops) is False
