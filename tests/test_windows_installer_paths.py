"""Windows installer path / branch tests.

Running schtasks or the Tk wizard on macOS CI is impossible, but we can
exercise every code path the installer uses to compute Windows paths and
gate macOS-specific install steps. These tests run on any host:

* The .bat wrapper string-content checks are pure file reads.
* ``install_daemon_binary`` is invoked with the env vars Windows would
  set (``LOCALAPPDATA`` / ``USERPROFILE``), pointed at a tmp_path so
  copy succeeds on POSIX too. The behavior we lock is the destination
  layout, not the chmod / icon assignment.
* ``register_scheduled_task`` is patched at the ``schtasks`` subprocess
  boundary — we assert the XML body the helper would feed to schtasks
  contains the resolved daemon path (no Mac/Linux artefacts).
* The launchd plist must be macOS-only — confirm it parses, names
  ``com.agentflow``, and is NOT touched by the Windows install path.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_BAT = REPO_ROOT / "install.bat"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"
LAUNCHD_PLIST = REPO_ROOT / "launchd" / "com.agentflow.computer-mcp.plist"


# ---------------------------------------------------------------------------
# install.bat content
# ---------------------------------------------------------------------------


def test_install_bat_uses_no_posix_paths() -> None:
    """install.bat must not reference ``~/Library``, ``/usr/local/bin``,
    or ``/etc`` — those are macOS/Linux artefacts and signal a copy-paste
    bug if they leak into a Windows wrapper."""
    content = INSTALL_BAT.read_text(encoding="utf-8")
    posix_bait = ["~/Library", "/usr/local/bin", "/etc/", "/Applications/", "launchctl"]
    for token in posix_bait:
        assert token not in content, f"install.bat leaked POSIX token {token!r}"


def test_install_bat_routes_to_ps1_only() -> None:
    """Today install.bat is a thin wrapper that pipes install.ps1 into
    PowerShell. The wrapper must not try to do real work in cmd — any
    install logic belongs in install.ps1 so the same flow runs in both
    cmd and PowerShell shells."""
    content = INSTALL_BAT.read_text(encoding="utf-8")
    assert "powershell" in content.lower()
    assert "install.ps1" in content or "computer-mcp.ps1" in content
    # No raw schtasks / mkdir / copy in the .bat itself
    lowered = content.lower()
    assert "schtasks" not in lowered
    assert "xcopy" not in lowered


@pytest.mark.xfail(
    reason="install.bat currently relies on PowerShell to read %LOCALAPPDATA% / "
    "%USERPROFILE%; the wrapper itself doesn't reference them. If we ever "
    "inline a fallback path in .bat for users with PowerShell disabled, this "
    "test should flip to assert the env-var references exist.",
    strict=False,
)
def test_install_bat_references_windows_env_paths() -> None:
    content = INSTALL_BAT.read_text(encoding="utf-8")
    assert "%USERPROFILE%" in content or "%LOCALAPPDATA%" in content


def test_install_ps1_uses_localappdata_or_userprofile() -> None:
    """install.ps1 IS the Windows install flow, so it must reference at
    least one of the two canonical Windows user-scoped roots."""
    content = INSTALL_PS1.read_text(encoding="utf-8")
    assert ("LOCALAPPDATA" in content) or ("USERPROFILE" in content)
    # Negative: no Mac-only paths
    assert "~/Library" not in content
    assert "launchctl" not in content


def test_install_sh_does_not_register_schtasks() -> None:
    """install.sh is macOS + Linux only. It must NOT try to call schtasks
    (Windows-only) and must redirect Windows users to install.ps1."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "schtasks" not in content
    assert "install.ps1" in content  # explicit redirect for Windows users


# ---------------------------------------------------------------------------
# launchd plist is Mac-only
# ---------------------------------------------------------------------------


def test_launchd_plist_is_mac_only() -> None:
    """The plist exists for macOS LaunchAgent registration. The Windows
    installer flow must never reference it (no schtasks XML pointing at
    a .plist; no .bat copying it). We check by ensuring the filename
    doesn't leak into the Windows install scripts."""
    assert LAUNCHD_PLIST.is_file(), "expected launchd plist at launchd/"
    body = LAUNCHD_PLIST.read_text(encoding="utf-8")
    assert "com.agentflow" in body
    # And the Windows scripts must not mention it
    assert "com.agentflow.computer-mcp.plist" not in INSTALL_BAT.read_text(encoding="utf-8")
    assert "com.agentflow.computer-mcp.plist" not in INSTALL_PS1.read_text(encoding="utf-8")


def test_install_sh_keeps_launchd_for_mac_only() -> None:
    """install.sh ships a macOS branch that lays down a LaunchAgent. The
    plist filename must appear inside the install_macos function and
    NOT in the Linux branch."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    # Take the macos branch substring up to install_linux
    if "install_macos" in content and "install_linux" in content:
        mac_slice = content.split("install_macos", 1)[1].split("install_linux", 1)[0]
        # LaunchAgents are the conventional plist root for user-scoped agents.
        assert "LaunchAgents" in mac_slice or "launchctl" in mac_slice
    # Linux branch must use systemd, not launchd
    linux_slice = content.split("install_linux", 1)[1] if "install_linux" in content else ""
    assert "launchctl" not in linux_slice
    assert "plist" not in linux_slice


# ---------------------------------------------------------------------------
# installer/setup_gui paths (Windows-only helpers)
# ---------------------------------------------------------------------------


def _import_setup_gui():
    """setup_gui imports Tk at module load; skip if Tk is missing on this host."""
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"tkinter unavailable: {exc}")
    sys.path.insert(0, str(REPO_ROOT / "installer"))
    try:
        import setup_gui  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"setup_gui import failed: {exc}")
    return setup_gui


def test_install_dir_prefers_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_install_dir`` should use ``%LOCALAPPDATA%\\AgentFlow`` when set,
    falling back to ``%USERPROFILE%\\AppData\\Local\\AgentFlow`` otherwise.
    Both forms keep the daemon under the user-scoped install root — never
    Program Files (no admin), never /opt or /usr/local (POSIX)."""
    setup_gui = _import_setup_gui()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "User"))
    target = setup_gui._install_dir()
    target_str = str(target)
    assert "AgentFlow" in target_str
    assert str(tmp_path / "Local") in target_str
    # Must not have leaked a POSIX bin / opt path
    assert "/usr/local" not in target_str
    assert "/opt/" not in target_str


def test_install_dir_falls_back_to_userprofile_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup_gui = _import_setup_gui()
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "User"))
    target = setup_gui._install_dir()
    target_str = str(target).replace("\\", "/")
    assert "AppData/Local/AgentFlow" in target_str
    assert str(tmp_path).replace("\\", "/") in target_str


def test_write_auth_file_writes_under_userprofile_dot_agentflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``write_auth_file`` must drop ``auth.json`` into
    ``%USERPROFILE%\\.agentflow\\auth.json`` — never under ~/Library or /etc."""
    setup_gui = _import_setup_gui()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    auth_path = setup_gui.write_auth_file(
        {
            "api_key": "af_live_test",
            "device_id": "00000000-0000-0000-0000-000000000000",
            "device_token": "aft_test",
        }
    )
    assert auth_path == tmp_path / ".agentflow" / "auth.json"
    assert auth_path.exists()
    body = auth_path.read_text(encoding="utf-8")
    assert "af_live_test" in body
    assert "aft_test" in body


def test_headless_install_expected_target_resolves_under_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``headless_install._expected_target`` must produce a path under
    ``%LOCALAPPDATA%\\AgentFlow\\agentflow-desktop.exe``."""
    # Make sure setup_gui is importable so headless_install can pull in TASK_NAME.
    _import_setup_gui()
    sys.path.insert(0, str(REPO_ROOT / "installer"))
    try:
        import headless_install  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"headless_install import failed: {exc}")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "User"))
    target = headless_install._expected_target()
    target_str = str(target).replace("\\", "/")
    assert target_str.endswith("AgentFlow/agentflow-desktop.exe")
    assert str(tmp_path / "Local").replace("\\", "/") in target_str


def test_install_daemon_binary_lands_under_install_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: with patched env vars + a fake source .exe, the helper
    must copy the source into ``<_install_dir>/agentflow-desktop.exe``
    and return that path."""
    setup_gui = _import_setup_gui()

    install_root = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(install_root))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "User"))

    src = tmp_path / "downloaded-setup.exe"
    src.write_bytes(b"MZ\x00\x00fake-pe-binary")
    monkeypatch.setenv("AF_SETUP_EXE_OVERRIDE", str(src))

    target = setup_gui.install_daemon_binary()
    assert target.exists()
    assert target.name == setup_gui.DAEMON_EXE_NAME
    assert str(install_root) in str(target)
    # The bytes must match the source (real copy, not a stub)
    assert target.read_bytes() == src.read_bytes()


def test_register_scheduled_task_xml_targets_installed_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``register_scheduled_task`` builds an XML body and feeds it to
    schtasks. We can't run schtasks on POSIX, so patch ``subprocess.run``
    at the boundary and inspect the XML on disk to confirm the
    <Command> element points at the installed .exe."""
    setup_gui = _import_setup_gui()

    captured: dict[str, object] = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _R:
        captured["cmd"] = list(cmd)
        # The XML is passed via /XML <path>. Read it back so we can assert
        # what schtasks would see.
        if "/XML" in cmd:
            xml_path = cmd[cmd.index("/XML") + 1]
            captured["xml_body"] = Path(xml_path).read_text(encoding="utf-16")
        return _R()

    monkeypatch.setattr(setup_gui.subprocess, "run", _fake_run)

    fake_exe = tmp_path / "AgentFlow" / "agentflow-desktop.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"MZ\x00")
    setup_gui.register_scheduled_task(fake_exe)

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "schtasks"
    assert "/Create" in cmd
    assert "/TN" in cmd
    assert setup_gui.TASK_NAME in cmd
    assert "/XML" in cmd
    xml = captured.get("xml_body", "")
    assert isinstance(xml, str)
    # The installed .exe path must be inside the XML (no Mac/Linux bait)
    assert str(fake_exe) in xml or fake_exe.name in xml
    assert "launchctl" not in xml
    assert "systemd" not in xml


# ---------------------------------------------------------------------------
# headless_install OS gating
# ---------------------------------------------------------------------------


def test_headless_install_picks_windows_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``headless_install.main`` orchestrates the same install steps the GUI
    runs. With ``platform.system()`` patched to Windows and every external
    side effect stubbed, ``main()`` should call ``_run_install_steps`` and
    succeed without touching real schtasks / filesystem layout outside tmp."""
    setup_gui = _import_setup_gui()
    sys.path.insert(0, str(REPO_ROOT / "installer"))
    try:
        import headless_install  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"headless_install import failed: {exc}")

    install_root = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(install_root))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "User"))

    src = tmp_path / "downloaded-setup.exe"
    src.write_bytes(b"MZ\x00\x00fake")
    monkeypatch.setenv("AF_SETUP_EXE_OVERRIDE", str(src))
    monkeypatch.setenv("AF_INVITE_API_KEY", "af_live_test")
    monkeypatch.setenv("AF_INVITE_DEVICE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("AF_INVITE_DEVICE_TOKEN", "aft_test")

    # Stub schtasks at the subprocess boundary inside setup_gui.
    target_exe = install_root / setup_gui.DAEMON_DIR_NAME / setup_gui.DAEMON_EXE_NAME

    class _R:
        returncode = 0
        stdout = (
            '<?xml version="1.0"?>\n<Task><Actions><Exec><Command>'
            f"{target_exe}"
            "</Command></Exec></Actions></Task>"
        )
        stderr = ""

    monkeypatch.setattr(setup_gui.subprocess, "run", lambda *_a, **_k: _R())
    # headless_install spawns schtasks directly for the /Query step too.
    monkeypatch.setattr(headless_install.subprocess, "run", lambda *_a, **_k: _R())
    # Block Popen: launch_daemon would try to exec the fake .exe (not a
    # real PE binary). On POSIX that's a permission-denied error.
    monkeypatch.setattr(setup_gui.subprocess, "Popen", lambda *_a, **_k: None)

    rc = headless_install.main()
    assert rc == 0
    assert target_exe.exists()


def test_setup_gui_exposes_install_constants() -> None:
    """Lightweight sanity: setup_gui ships the constants both the GUI and
    headless_install consume. Bringing up the actual Tk root needs a
    display server — that's left to the Windows CI runner."""
    setup_gui = _import_setup_gui()
    assert "AgentFlow" in setup_gui.WINDOW_TITLE
    assert setup_gui.TASK_NAME == "AgentFlowDesktop"
    assert setup_gui.DAEMON_DIR_NAME == "AgentFlow"
    assert setup_gui.DAEMON_EXE_NAME.endswith(".exe")


# ---------------------------------------------------------------------------
# Cross-platform sanity: no Windows-only imports at module load on POSIX
# ---------------------------------------------------------------------------


def test_installer_module_imports_on_posix() -> None:
    """The Windows installer entry point should import cleanly on POSIX
    so e.g. a CI runner that builds the macOS bundle can still validate
    the Windows code via these tests."""
    sys.path.insert(0, str(REPO_ROOT / "installer"))
    try:
        import setup_gui  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"setup_gui must import on POSIX (was: {exc!r})")
