"""Headless equivalent of clicking «Install» in setup_gui.py.

Used by the `installer-e2e` GitHub Actions workflow on a Windows runner.
The goal is to exercise the EXACT same code path the GUI uses:

    install_daemon_binary  → copy the bundled .exe to %LOCALAPPDATA%\\AgentFlow\\
    write_auth_file        → write %USERPROFILE%\\.agentflow\\auth.json
    register_scheduled_task → schtasks /Create /TN AgentFlowDesktop /XML
    launch_daemon          → spawn the daemon detached

…without bringing up Tk (the CI runner has no interactive desktop).

Inputs (read from env so the workflow can pass dummy values):

    AF_SETUP_EXE_OVERRIDE  — path to the freshly built setup .exe used as
                             the source for install_daemon_binary. Required
                             when running this from python directly (not
                             frozen), otherwise the helper would copy its
                             own .py file and the schtasks XML <Command>
                             would point at agentflow-desktop.exe but
                             contain a Python script.
    AF_INVITE_API_KEY      — defaults to af_live_ci_placeholder
    AF_INVITE_DEVICE_ID    — defaults to 00000000-…
    AF_INVITE_DEVICE_TOKEN — defaults to aft_ci_placeholder
    AF_WS_URL              — passed through to write_auth_file

Exit codes:

    0  install steps + schtasks /Query verification all passed
    1  any step raised — full traceback + step name printed to stderr
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from setup_gui import (  # noqa: E402
    DAEMON_DIR_NAME,
    DAEMON_EXE_NAME,
    TASK_NAME,
    _run_install_steps,
)


def _expected_target() -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or (Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "Local")
    )
    return base / DAEMON_DIR_NAME / DAEMON_EXE_NAME


def _seeded_creds() -> dict:
    return {
        "api_key": os.environ.get("AF_INVITE_API_KEY", "af_live_ci_placeholder"),
        "device_id": os.environ.get(
            "AF_INVITE_DEVICE_ID", "00000000-0000-0000-0000-000000000000"
        ),
        "device_token": os.environ.get("AF_INVITE_DEVICE_TOKEN", "aft_ci_placeholder"),
    }


def _query_task_xml() -> str:
    """Return the XML of the registered task. Raises CalledProcessError
    when the task is missing — that is the exact signal we want."""
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def main() -> int:
    print("[headless] step: _run_install_steps")
    print(f"[headless] AF_SETUP_EXE_OVERRIDE={os.environ.get('AF_SETUP_EXE_OVERRIDE')!r}")
    try:
        target = _run_install_steps(_seeded_creds(), on_step=lambda m: print(f"[headless]   {m}"))
    except Exception:
        print("[headless] FAIL: _run_install_steps raised", file=sys.stderr)
        traceback.print_exc()
        return 1

    print(f"[headless] daemon installed at: {target}")

    expected = _expected_target()
    if Path(target).resolve() != expected.resolve():
        print(
            f"[headless] FAIL: returned target {target} != expected {expected}",
            file=sys.stderr,
        )
        return 1

    if not Path(target).exists():
        print(f"[headless] FAIL: target does not exist on disk: {target}", file=sys.stderr)
        return 1

    auth_path = (
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".agentflow" / "auth.json"
    )
    if not auth_path.exists():
        print(f"[headless] FAIL: auth.json missing at {auth_path}", file=sys.stderr)
        return 1
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[headless] FAIL: auth.json invalid json: {exc}", file=sys.stderr)
        return 1
    for field in ("api_key", "device_id", "enrollment_token", "ws_url"):
        if not auth.get(field):
            print(f"[headless] FAIL: auth.json missing field {field}", file=sys.stderr)
            return 1
    print(f"[headless] auth.json OK at {auth_path}")

    try:
        xml = _query_task_xml()
    except subprocess.CalledProcessError as exc:
        print(
            "[headless] FAIL: schtasks /Query returned non-zero",
            file=sys.stderr,
        )
        print(exc.stdout, file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        return 1

    print("[headless] ---- schtasks /XML ----")
    print(xml)
    print("[headless] ---- end ----")

    # Windows schtasks /XML output is UTF-16LE; subprocess decodes as
    # the codepage but the <Command> string still ends up readable.
    # Look for the installed path inside the XML.
    expected_str = str(expected)
    if expected_str not in xml:
        print(
            f"[headless] FAIL: schtasks XML <Command> does not contain {expected_str}",
            file=sys.stderr,
        )
        return 1
    print(f"[headless] schtasks <Command> contains expected path: {expected_str}")

    print("[headless] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
