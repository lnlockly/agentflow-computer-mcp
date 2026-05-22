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


def main() -> None:
    log("starting smoke for installer/setup_gui.py")
    check_invite_roundtrip()
    check_auth_file_shape()
    check_daemon_entrypoint_imports()
    log("ALL GREEN")


if __name__ == "__main__":
    main()
