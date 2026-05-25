"""Autostart resilience — schtasks failure must not abort the install.

Before this contract `register_scheduled_task` raised
`subprocess.CalledProcessError` on any non-zero schtasks rc, which
bubbled out of `_run_install_steps` and aborted the wizard at step 4/5
with a Python traceback. The user saw «ломается на schtasks /create»
and was left without a working install.

Now the helper returns `(ok, detail)`; the wizard logs the failure and
keeps going via `HKCU\\…\\Run\\AgentFlowDaemon`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "installer"))

import setup_gui  # noqa: E402


class _FakeProc:
    def __init__(self, rc: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_register_scheduled_task_returns_false_on_schtasks_failure(monkeypatch) -> None:
    def fake_run(cmd, **_kwargs):
        assert cmd[0] == "schtasks"
        return _FakeProc(rc=1, stderr="ERROR: Access is denied.")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = setup_gui.register_scheduled_task(Path("C:/x/agentflow-desktop.exe"))
    assert ok is False
    assert "schtasks rc=1" in detail
    assert "Access is denied" in detail


def test_register_scheduled_task_returns_true_on_success(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc(rc=0))
    ok, detail = setup_gui.register_scheduled_task(Path("C:/x/agentflow-desktop.exe"))
    assert ok is True
    assert detail == ""


def test_install_step_4_falls_back_to_run_key(monkeypatch, tmp_path) -> None:
    """When schtasks fails, install must continue via HKCU Run-key fallback,
    not raise out of the wizard."""
    events: list[str] = []

    target = tmp_path / "agentflow-desktop.exe"
    target.write_bytes(b"stub")

    monkeypatch.setattr(setup_gui, "install_daemon_binary", lambda: target)
    monkeypatch.setattr(setup_gui, "write_auth_file", lambda creds: tmp_path / "auth.json")
    monkeypatch.setattr(
        setup_gui,
        "register_scheduled_task",
        lambda exe: (False, "schtasks rc=1 ERROR: Access denied"),
    )

    fallback_calls: list[Path] = []

    def fake_run_key(exe: Path) -> None:
        fallback_calls.append(exe)

    monkeypatch.setattr(setup_gui, "register_daemon_run_key", fake_run_key)
    monkeypatch.setattr(setup_gui, "launch_daemon", lambda exe: None)
    monkeypatch.setattr(setup_gui, "install_tray_binary", lambda: None)

    # Must not raise — wizard reaches the end.
    out = setup_gui._run_install_steps(
        {"api_key": "af_live_x", "device_id": "d", "device_token": "aft_x"},
        on_step=events.append,
        install_tray=False,
    )

    assert out == target
    assert fallback_calls == [target]
    assert any("schtasks отказал" in e for e in events)
    assert any("Run-key установлен" in e for e in events)
