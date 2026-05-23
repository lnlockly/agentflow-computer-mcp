"""Static checks on the PyInstaller spec + setup_gui wiring for v0.5.0.

We can't run `pyinstaller` from pytest on every host (no Windows on the
Mac dev box, no pystray on Linux CI), so this test reads the spec + the
wizard module as text and asserts the wiring is in place:

  - Both `agentflow-desktop-setup` and `agentflow-tray` EXE blocks exist.
  - `pystray` is in `BUNDLE_PACKAGES`.
  - `tray_entry.py` exists and routes to `winapp.__main__`.
  - `setup_gui._run_install_steps` invokes both daemon AND tray installers
    when `install_tray=True` (the wizard default).

Together with `tests/winapp/test_autostart.py` (mocked winreg) this gives
us coverage of the new install path without touching real registry keys.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "installer" / "agentflow-setup.spec"
SETUP_GUI = REPO / "installer" / "setup_gui.py"
TRAY_ENTRY = REPO / "installer" / "tray_entry.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_spec_declares_both_exe_targets() -> None:
    text = _read(SPEC)
    assert 'name="agentflow-desktop-setup"' in text, "daemon EXE block missing"
    assert 'name="agentflow-tray"' in text, "tray EXE block missing"
    assert "tray_entry.py" in text, "tray Analysis must point at installer/tray_entry.py"


def test_spec_bundles_pystray() -> None:
    text = _read(SPEC)
    assert '"pystray"' in text, "pystray must be in BUNDLE_PACKAGES for tray onefile"


def test_tray_entry_exists_and_delegates() -> None:
    assert TRAY_ENTRY.exists(), "installer/tray_entry.py missing"
    text = _read(TRAY_ENTRY)
    assert "agentflow_computer_mcp.winapp.__main__" in text
    assert "def main" in text


def test_setup_gui_installs_tray_when_enabled() -> None:
    text = _read(SETUP_GUI)
    # The install pipeline must reference the tray helpers.
    for needle in (
        "TRAY_EXE_NAME = \"agentflow-tray.exe\"",
        "install_tray_binary",
        "register_tray_autostart",
        "launch_tray",
        "install_tray: bool = True",
        "AgentFlowTray",
        r"Software\Microsoft\Windows\CurrentVersion\Run",
    ):
        assert needle in text, f"setup_gui.py missing wiring: {needle!r}"


def test_setup_gui_wizard_checkbox_exists() -> None:
    text = _read(SETUP_GUI)
    assert "tray_autostart_var" in text, "wizard must expose tray autostart toggle"
    assert "Запускать иконку в трее при старте Windows" in text, "checkbox label missing"


def test_setup_gui_run_steps_passes_tray_flag() -> None:
    text = _read(SETUP_GUI)
    # The wizard worker must pipe the checkbox state through to _run_install_steps.
    assert "install_tray=bool(self.tray_autostart_var.get())" in text
