"""Windows Defender exclusion — zero-config install path.

Defender's TLS MITM on locked-down corporate / Windows Home installs
broke every `urlopen` in the daemon (SSL EOF) and the auto-updater.
The installer now whitelists the install dir + both .exe names so the
user goes from invite-paste to working daemon without ever opening
PowerShell.

These tests run on every host (the helper has a `not_windows` no-op
branch) and inject a fake `ShellExecuteW` so we don't fire a real UAC
prompt during pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "installer"))

import setup_gui  # noqa: E402


def test_build_defender_command_includes_install_dir_and_both_exes() -> None:
    """The PS script must reference all three exclusion targets so a
    single elevation covers daemon + tray + the whole install dir."""
    cmd = setup_gui._build_defender_command(Path(r"C:\Users\X\AppData\Local\AgentFlow"))
    assert r"C:\Users\X\AppData\Local\AgentFlow" in cmd
    assert "agentflow-desktop.exe" in cmd
    assert "agentflow-tray.exe" in cmd
    # Idempotency: each Add-MpPreference is guarded by a -contains check.
    assert cmd.count("-contains") == 3
    assert cmd.count("Add-MpPreference") == 3
    # try/catch surrounds everything so a host without Defender exits 2,
    # not crashes with a Get-MpPreference exception.
    assert "try {" in cmd
    assert "exit 2" in cmd


def test_powershell_quote_escapes_single_quotes() -> None:
    assert setup_gui._powershell_quote("plain") == "plain"
    assert setup_gui._powershell_quote("it's") == "it''s"


def test_add_defender_exclusion_no_op_on_non_windows() -> None:
    """Mac / Linux dev hosts: the helper must return cleanly so the
    rest of the install pipeline keeps going. We can't monkeypatch
    `os.name` at import time, so guard via the sentinel return value."""
    if Path(__file__).exists() and __import__("os").name == "nt":
        # On a real Windows runner we'd test the success path with a
        # fake executor. Other tests below cover that branch.
        return
    ok, reason = setup_gui.add_defender_exclusion(Path(r"C:\AgentFlow"))
    assert ok is False
    assert reason == "not_windows"


def _force_nt(monkeypatch) -> None:
    """Spoof os.name=='nt' inside setup_gui so the Windows branch runs
    on a Mac/Linux CI host. We patch the imported `os` module in
    setup_gui, not the global one, so other tests are unaffected."""
    monkeypatch.setattr(setup_gui.os, "name", "nt", raising=False)


def test_add_defender_exclusion_success_returns_true(monkeypatch) -> None:
    _force_nt(monkeypatch)
    captured: dict = {}

    def fake_exec(exe: str, args: str) -> int:
        captured["exe"] = exe
        captured["args"] = args
        return 42  # ShellExecuteW success: value > 32

    ok, reason = setup_gui.add_defender_exclusion(
        Path(r"C:\Users\X\AppData\Local\AgentFlow"),
        shell_executor=fake_exec,
    )
    assert ok is True
    assert reason == ""
    assert captured["exe"] == "powershell.exe"
    # The command must reference both .exes — proves we wired _build_defender_command in.
    assert "agentflow-desktop.exe" in captured["args"]
    assert "agentflow-tray.exe" in captured["args"]
    assert "-NoProfile" in captured["args"]


def test_add_defender_exclusion_user_declined_uac(monkeypatch) -> None:
    """SE_ERR_ACCESSDENIED == 5 means user clicked «Нет» on the UAC
    dialog. Wizard must not crash — install keeps going without the
    exclusion."""
    _force_nt(monkeypatch)

    def fake_exec(_exe: str, _args: str) -> int:
        return 5

    ok, reason = setup_gui.add_defender_exclusion(
        Path(r"C:\AgentFlow"), shell_executor=fake_exec
    )
    assert ok is False
    assert reason == "user_declined"


def test_add_defender_exclusion_shellexecute_low_rc(monkeypatch) -> None:
    """ShellExecuteW return codes ≤ 32 are all errors per WinAPI. We
    surface them as `shellexecute_rc=<n>` so the wizard log explains
    why exclusion didn't apply."""
    _force_nt(monkeypatch)

    def fake_exec(_exe: str, _args: str) -> int:
        return 2  # SE_ERR_FNF / generic failure

    ok, reason = setup_gui.add_defender_exclusion(
        Path(r"C:\AgentFlow"), shell_executor=fake_exec
    )
    assert ok is False
    assert "shellexecute_rc=2" in reason


def test_add_defender_exclusion_exception_swallowed(monkeypatch) -> None:
    _force_nt(monkeypatch)

    def fake_exec(_exe: str, _args: str) -> int:
        raise RuntimeError("ctypes blew up")

    ok, reason = setup_gui.add_defender_exclusion(
        Path(r"C:\AgentFlow"), shell_executor=fake_exec
    )
    assert ok is False
    assert "shellexecute_failed" in reason


def test_run_install_steps_does_not_crash_when_defender_fails(
    monkeypatch, tmp_path
) -> None:
    """End-to-end: a Defender failure inside `_run_install_steps` must
    not raise out of the wizard. The user gets a warning in the log
    and the install keeps going (daemon still launches)."""
    events: list[str] = []
    target = tmp_path / "agentflow-desktop.exe"
    target.write_bytes(b"stub")

    monkeypatch.setattr(setup_gui, "install_daemon_binary", lambda: target)
    monkeypatch.setattr(setup_gui, "write_auth_file", lambda creds: tmp_path / "auth.json")
    monkeypatch.setattr(
        setup_gui, "register_scheduled_task", lambda exe: (True, "")
    )
    monkeypatch.setattr(
        setup_gui,
        "add_defender_exclusion",
        lambda *_a, **_kw: (False, "user_declined"),
    )
    monkeypatch.setattr(setup_gui, "launch_daemon", lambda exe: None)
    monkeypatch.setattr(setup_gui, "install_tray_binary", lambda: None)

    out = setup_gui._run_install_steps(
        {"api_key": "af_live_x", "device_id": "d", "device_token": "aft_x"},
        on_step=events.append,
        install_tray=False,
    )
    assert out == target
    assert any("отказался от UAC" in e for e in events)
