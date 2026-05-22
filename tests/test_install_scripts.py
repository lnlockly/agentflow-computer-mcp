"""Smoke tests for the cross-platform installer scripts.

The full install.sh / install.ps1 / install.bat live at the repo root. We can't
exec them in CI (they would write systemd units and pip-install), but we can:

1. Confirm they exist.
2. Confirm install.sh parses with ``bash -n``.
3. Confirm install.sh advertises all three OS branches.
4. Confirm install.ps1 mentions Task Scheduler.
5. Confirm install.bat invokes powershell.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_BAT = REPO_ROOT / "install.bat"


def test_install_sh_exists() -> None:
    assert INSTALL_SH.is_file(), "install.sh missing from repo root"


def test_install_ps1_exists() -> None:
    assert INSTALL_PS1.is_file(), "install.ps1 missing from repo root"


def test_install_bat_exists() -> None:
    assert INSTALL_BAT.is_file(), "install.bat missing from repo root"


def test_install_sh_parses() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not on PATH")
    r = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"bash -n failed: {r.stderr}"


def test_install_sh_handles_all_three_os() -> None:
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "install_macos" in content, "install.sh missing macOS branch"
    assert "install_linux" in content, "install.sh missing Linux branch"
    assert "install.ps1" in content, "install.sh should redirect Windows users to install.ps1"


def test_install_sh_linux_handles_apt_dnf_pacman() -> None:
    content = INSTALL_SH.read_text(encoding="utf-8")
    for pm in ("apt-get", "dnf", "pacman"):
        assert pm in content, f"install.sh Linux branch missing {pm} support"


def test_install_sh_linux_drops_systemd_unit() -> None:
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert ".config/systemd/user" in content
    assert "agentflow-desktop.service" in content


def test_install_ps1_registers_scheduled_task() -> None:
    content = INSTALL_PS1.read_text(encoding="utf-8")
    assert "Register-ScheduledTask" in content
    assert "AgentFlowDesktop" in content


def test_install_bat_invokes_powershell() -> None:
    content = INSTALL_BAT.read_text(encoding="utf-8")
    assert "powershell" in content.lower()
    assert "computer-mcp.ps1" in content


def test_install_sh_writes_xdg_mirror() -> None:
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "XDG_CONFIG_HOME" in content


def test_install_sh_prints_cabinet_url() -> None:
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "/cabinet/devices/" in content
