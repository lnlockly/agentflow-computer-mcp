"""End-to-end smoke for the install flow.

Runs the exact pip install + locate_launcher + write_auth_file path the
setup.exe takes, against a temp HOME, on a real Windows or POSIX host.
NO Tkinter, NO schtasks, NO daemon launch — just the parts that fail
in the wild.

Exit code 0 = release safe to publish.
Exit code != 0 = release MUST be blocked (release workflow gates on
this script before uploading the .exe).

Run manually:
    python installer/smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

from setup_gui import (  # noqa: E402  (sys.path manip)
    PACKAGE_GIT_URL,
    find_python,
    locate_launcher,
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


def check_pip_install_streams() -> None:
    """The actual cause of «висит на шаге 2» — pip output not streaming.

    Run `python -u -m pip install --dry-run` against the real GitHub
    URL and assert at least 3 distinct lines arrive within 30 seconds.
    Anything less → the unbuffered fix regressed.
    """
    log("pip stream: running install --dry-run against live GitHub URL")
    python_exe = find_python()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    proc = subprocess.Popen(
        [
            python_exe,
            "-u",
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--no-deps",
            "--progress-bar",
            "off",
            "-v",
            PACKAGE_GIT_URL,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout is not None
    lines = 0
    buf = bytearray()
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if chunk in (b"\n", b"\r"):
            if buf:
                line = buf.decode("utf-8", "replace").strip()
                buf.clear()
                if line:
                    lines += 1
                    if lines <= 5:
                        log(f"  pip: {line[:120]}")
        else:
            buf.extend(chunk)
    rc = proc.wait()
    if rc != 0:
        fail(f"pip --dry-run failed (rc={rc})")
    if lines < 3:
        fail(f"pip emitted only {lines} lines — streaming broken")
    log(f"  ok — {lines} lines streamed")


def check_install_for_real() -> None:
    """Actually install the package into a throwaway --user dir and
    verify the entry point or `python -m` fallback works."""
    log("real install: pip install --user --upgrade from GitHub")
    python_exe = find_python()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    rc = subprocess.call(
        [
            python_exe,
            "-u",
            "-m",
            "pip",
            "install",
            "--user",
            "--upgrade",
            "--progress-bar",
            "off",
            PACKAGE_GIT_URL,
        ],
        env=env,
    )
    if rc != 0:
        fail(f"pip install --user exited rc={rc}")

    log("locate_launcher: should find .exe or fall back to python -m")
    exe, args = locate_launcher(python_exe)
    log(f"  launcher: {exe} {args}")
    if not exe:
        fail("locate_launcher returned empty executable")

    log("launcher --help: must exit 0 and produce output")
    out = subprocess.run([exe, *args[:-1], "--help"], capture_output=True, text=True)
    if out.returncode != 0:
        # Some entrypoints don't have --help on subcommand; try plain --help
        out = subprocess.run([exe, "--help"], capture_output=True, text=True)
    if out.returncode != 0:
        fail(f"launcher --help failed: rc={out.returncode} stderr={out.stderr[:200]}")
    if not out.stdout.strip() and not out.stderr.strip():
        fail("launcher --help produced no output")


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


def main() -> None:
    log("starting smoke for installer/setup_gui.py")
    check_invite_roundtrip()
    check_auth_file_shape()
    check_pip_install_streams()
    # The full install pulls ~50 MB; only run it on CI / when explicitly
    # asked. SMOKE_FULL=1 keeps local runs fast.
    if os.environ.get("SMOKE_FULL") == "1":
        check_install_for_real()
    else:
        log("skipping real install (set SMOKE_FULL=1 to enable)")
    log("ALL GREEN")


if __name__ == "__main__":
    main()
