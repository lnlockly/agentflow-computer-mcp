"""`python -m agentflow_computer_mcp.winapp` CLI surface."""
from __future__ import annotations

import pytest

from agentflow_computer_mcp.winapp import __main__ as entry
from agentflow_computer_mcp.winapp import autostart


class FakeReg:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def ops(self) -> autostart.RegOps:
        return autostart.RegOps(
            set_value=lambda name, value: self.values.__setitem__(name, value),
            get_value=lambda name: self.values.get(name),
            delete_value=lambda name: self.values.pop(name, None) is not None,
        )


def test_version_flag_prints_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.strip()  # version was printed


def test_install_subcommand_writes_run_key(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeReg()
    monkeypatch.setattr(autostart, "_real_opener", reg.ops)
    rc = entry.main(["install", "--autostart"])
    assert rc == 0
    assert autostart.VALUE_NAME in reg.values


def test_uninstall_subcommand_removes_run_key(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeReg()
    reg.values[autostart.VALUE_NAME] = "anything"
    monkeypatch.setattr(autostart, "_real_opener", reg.ops)
    rc = entry.main(["uninstall"])
    assert rc == 0
    assert autostart.VALUE_NAME not in reg.values


def test_uninstall_idempotent_when_already_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeReg()
    monkeypatch.setattr(autostart, "_real_opener", reg.ops)
    rc = entry.main(["uninstall"])
    assert rc == 0  # prints "not installed" but exits clean
