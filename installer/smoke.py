"""Pre-build smoke for the self-contained installer.

The .exe now bundles Python + agentflow_computer_mcp + every dep, so
the old «stream pip output for 30s» check no longer applies. Instead
this script verifies the three pieces that still need wiring:

1. `parse_invite` happy + reject paths (still the user-facing entry).
2. `write_auth_file` writes the expected shape into a temp HOME.
3. The bundled daemon entry point imports cleanly — i.e. the same
   module the PyInstaller spec hard-pins is actually importable from
   the current Python.

Post-build a second gate runs against the artifact itself:

    agentflow-desktop-setup.exe --daemon --selftest

The release workflow asserts that exits 0 within 30 seconds. Together
the two gates catch (a) source-level regressions and (b) PyInstaller
collection misses.

Exit code 0 = release safe to publish.
Exit code != 0 = release MUST be blocked.

Run manually:
    python installer/smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))
sys.path.insert(0, str(ROOT / "src"))

from setup_gui import (  # noqa: E402  (sys.path manip)
    parse_invite,
    write_auth_file,
)


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[smoke] FAIL — {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def check_invite_roundtrip() -> None:
    log("invite-code parse: happy + reject paths")
    creds = parse_invite(
        "eyJrIjoiYWZfbGl2ZV90ZXN0IiwiZCI6IjAwMDAtMDAwMC0wMDAwIiwidCI6ImFmdF90ZXN0In0"
    )
    assert creds["api_key"] == "af_live_test"
    assert creds["device_id"] == "0000-0000-0000"
    assert creds["device_token"] == "aft_test"
    for bad, why in [
        ("", "empty"),
        ("not-base64!!", "junk"),
        # missing token prefix
        ("eyJrIjoiYWZfbGl2ZSIsImQiOiIwMCIsInQiOiJ4eHgifQ", "bad token prefix"),
    ]:
        try:
            parse_invite(bad)
        except ValueError:
            continue
        fail(f"parse_invite should have rejected: {why}")


def check_auth_file_shape() -> None:
    log("write_auth_file: shape + on-disk presence")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["USERPROFILE"] = tmp
        os.environ["HOME"] = tmp
        path = write_auth_file(
            {
                "api_key": "af_live_smoketest",
                "device_id": "smoke-uuid",
                "device_token": "aft_smoketest",
            }
        )
        if not path.exists():
            fail(f"auth.json not written to {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("api_key", "device_id", "enrollment_token", "ws_url"):
            if key not in data:
                fail(f"auth.json missing key: {key}")
        if data["api_key"] != "af_live_smoketest":
            fail("auth.json api_key mismatch")


def check_daemon_entrypoint_imports() -> None:
    """The bundle relies on `from agentflow_computer_mcp.desktop_cli
    import main` working in the frozen runtime. If that import breaks
    here, it breaks in the .exe too — catch it early."""
    log("daemon entry point: import agentflow_computer_mcp.desktop_cli")
    try:
        from agentflow_computer_mcp.desktop_cli import main  # noqa: F401
    except Exception as exc:
        fail(f"cannot import daemon entry point: {exc}")
    log("  ok — daemon main() is importable")


def check_auto_updater() -> None:
    """Verify the auto-update module imports + the no-op path runs.

    The check_now() call is invoked with an injected fetch() that returns
    a release OLDER than the current bundled version, so the updater MUST
    NOT trigger a download. Any download attempt fails the smoke.
    """
    log("auto_updater: import + mocked older-release no-download path")
    try:
        from agentflow_computer_mcp import __version__ as local_version
        from agentflow_computer_mcp.auto_updater import check_now
    except Exception as exc:
        fail(f"cannot import auto_updater: {exc}")

    download_called = {"hit": False}

    def fake_fetch() -> dict:
        # Return a release tagged at v0.0.1 — older than anything we'd ship.
        return {
            "tag_name": "v0.0.1",
            "body": "sha256: " + ("0" * 64),
            "assets": [
                {
                    "name": "agentflow-desktop-setup.exe",
                    "browser_download_url": "https://example.invalid/setup.exe",
                }
            ],
        }

    def fake_download(url: str, dest) -> None:  # noqa: ARG001, ANN001
        download_called["hit"] = True
        fail("auto_updater attempted a download for an older release")

    def fake_apply(_path) -> None:  # noqa: ANN001
        fail("auto_updater attempted to apply an older release")

    result = check_now(
        fetch=fake_fetch,
        downloader=fake_download,
        apply=fake_apply,
        allow_unfrozen=True,
    )
    if download_called["hit"]:
        fail("downloader called for an older release (should be skipped)")
    if result.get("status") != "current":
        fail(f"expected status=current, got {result!r}")
    log(f"  ok -- auto_updater stays put on {local_version} vs fake v0.0.1")


def check_os_aware_tool_filter() -> None:
    """The driver builds the LLM tool catalog from the current host OS.
    Mac-only tools must not leak onto Windows, and Windows-only tools must
    not leak onto macOS — otherwise the agent will call commands the host
    can't run.
    """
    log("os-aware tool filter: catalog respects current platform")
    from unittest.mock import patch

    from agentflow_computer_mcp.driver import desktop_tools

    # Simulate a Windows host. PowerShell tool should be present; AppleScript
    # tools (chrome_eval / chrome_tabs) should be filtered out.
    with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value="Windows"):
        names = {t["name"] for t in desktop_tools.all_tool_descriptors()}
        if "powershell_exec" not in names:
            fail("powershell_exec missing from Windows tool catalog")
        if "chrome_eval" in names:
            fail("chrome_eval leaked into Windows tool catalog (mac-only)")
        if "chrome_tabs" in names:
            fail("chrome_tabs leaked into Windows tool catalog (mac-only)")

    # Simulate a Mac host. AppleScript tools should be present; PowerShell
    # tools should be filtered out.
    with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value="Darwin"):
        names = {t["name"] for t in desktop_tools.all_tool_descriptors()}
        if "chrome_eval" not in names:
            fail("chrome_eval missing from macOS tool catalog")
        if "powershell_exec" in names:
            fail("powershell_exec leaked into macOS tool catalog (windows-only)")
        if "winget_search" in names:
            fail("winget_search leaked into macOS tool catalog (windows-only)")

    # start_app + chrome_open_url are cross-platform — visible on both.
    for sys_name in ("Darwin", "Windows", "Linux"):
        with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value=sys_name):
            names = {t["name"] for t in desktop_tools.all_tool_descriptors()}
            for cross in ("start_app", "chrome_open_url", "screen_capture", "browser_open"):
                if cross not in names:
                    fail(f"{cross} should be available on {sys_name}")

    log("  ok — Windows hides AppleScript tools, macOS hides PowerShell tools")


def check_os_aware_system_prompt() -> None:
    """The driver loop injects the current host OS into the system prompt
    so the LLM picks the right shell / clipboard / browser commands."""
    log("os-aware system prompt: build_system_prompt includes host OS")
    from agentflow_computer_mcp.driver.loop import HOST_OS, build_system_prompt

    prompt = build_system_prompt("(no windows)", af_tools_present=False)
    if "ОС хоста" not in prompt:
        fail("system prompt missing host-OS context block")
    if HOST_OS not in prompt:
        fail(f"system prompt does not mention HOST_OS={HOST_OS!r}")
    if "Codex" not in prompt:
        fail("system prompt missing Codex / package-manager knowledge block")
    log(f"  ok -- prompt declares host OS = {HOST_OS}")


def main() -> None:
    log("starting smoke for installer/setup_gui.py")
    check_invite_roundtrip()
    check_auth_file_shape()
    check_daemon_entrypoint_imports()
    check_auto_updater()
    check_os_aware_tool_filter()
    check_os_aware_system_prompt()
    log("ALL GREEN")


if __name__ == "__main__":
    main()
