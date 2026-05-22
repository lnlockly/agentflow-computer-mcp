"""Self-update from GitHub releases.

The frozen Windows .exe checks the lnlockly/agentflow-computer-mcp `latest`
release on a schedule. When a newer build appears it downloads the asset,
verifies its sha256 against the release body, and swaps the running
binary in place.

Architecture rules:

- Only runs from a PyInstaller-frozen build. From source we log and exit.
- Never crosses major-version boundaries (0.x.x → 1.x.x stays manual).
- A sha256 mismatch is treated as a corrupted download — discard, log, skip.
- All failures are caught at the boundary so the daemon never dies because
  the updater hiccupped.
- ~/.agentflow/auth.json + computer-scope.toml live outside the .exe and
  survive a swap untouched.

Replacement strategy:

- macOS / Linux: `os.replace` swaps the binary in place, then `os.execv`
  reboots the same PID with the new binary.
- Windows: a running .exe is locked. We drop a sidecar `agentflow-desktop.new.exe`
  next to the current binary and a one-shot `update.bat` that waits for
  the current PID to exit, moves the sidecar over the live binary, and
  restarts the daemon. Then we `sys.exit(0)` so the running process
  releases the lock.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

GITHUB_API_LATEST = (
    "https://api.github.com/repos/lnlockly/agentflow-computer-mcp/releases/latest"
)
ASSET_NAME = "agentflow-desktop-setup.exe"
DEFAULT_INTERVAL_MIN = 30
MAX_INTERVAL_MIN = 24 * 60  # cap exponential backoff at 24h
USER_AGENT = f"agentflow-desktop/{__version__}"

# Hard upper bound — refuse to auto-cross a major version until a human
# bumps this constant. v1.0.0 ships with explicit consent, not via updater.
MAX_MAJOR = 0


class UpdateError(Exception):
    """Wraps any auto-update failure so callers see one exception type."""


def _is_frozen() -> bool:
    """True only when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _parse_version(tag: str) -> tuple[int, int, int]:
    """Parse `v0.4.3` or `0.4.3` into (0, 4, 3). Raises on garbage."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    if not m:
        raise UpdateError(f"unrecognized version tag: {tag!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _is_newer(remote: str, local: str) -> bool:
    try:
        return _parse_version(remote) > _parse_version(local)
    except UpdateError:
        return False


def _is_safe_major(remote: str) -> bool:
    """Refuse v(MAX_MAJOR+1).x.x and higher — major bumps need a human."""
    try:
        major, _, _ = _parse_version(remote)
    except UpdateError:
        return False
    return major <= MAX_MAJOR


def _sha256_from_body(body: str) -> str | None:
    """Pluck `sha256: <hex>` out of release notes. Case-insensitive, ignores
    surrounding markdown. Returns None when no digest is published — caller
    decides whether to refuse the update or proceed (we refuse)."""
    if not body:
        return None
    m = re.search(r"sha256[:\s]+([0-9a-fA-F]{64})", body)
    return m.group(1).lower() if m else None


def _http_get_json(url: str, *, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _http_download(url: str, dest: Path, *, timeout: float = 300.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:  # noqa: S310
        shutil.copyfileobj(resp, fh)


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_latest_release(*, _http=_http_get_json) -> dict:
    """Return the parsed `releases/latest` payload. Injectable for tests."""
    return _http(GITHUB_API_LATEST)


def _find_asset(release: dict) -> dict | None:
    for asset in release.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            return asset
    return None


def _replace_unix(new_exe: Path, current_exe: Path) -> None:
    """In-place swap on macOS / Linux, then re-exec same PID."""
    os.chmod(new_exe, 0o755)
    os.replace(new_exe, current_exe)
    log.info("auto-update: re-executing %s", current_exe)
    os.execv(str(current_exe), [str(current_exe), *sys.argv[1:]])


def _replace_windows(new_exe: Path, current_exe: Path) -> None:
    """Side-by-side swap on Windows.

    A running .exe holds a file lock, so we drop a `.new.exe` next to the
    current binary and a one-shot `update.bat` that:
      1. waits for the live PID to exit (<= 30s),
      2. moves the .new.exe over the live .exe,
      3. starts the daemon again.

    Then we exit so the lock releases and the .bat can finish the job.
    """
    sidecar = current_exe.with_suffix(".new.exe")
    bat = current_exe.parent / "update.bat"

    # Replace any leftover sidecar from a previous failed update.
    with contextlib.suppress(FileNotFoundError):
        sidecar.unlink()
    shutil.move(str(new_exe), str(sidecar))

    pid = os.getpid()
    script = (
        "@echo off\r\n"
        "setlocal\r\n"
        f"set PID={pid}\r\n"
        f'set EXE="{current_exe}"\r\n'
        f'set NEW="{sidecar}"\r\n'
        "rem wait up to 30s for the running daemon to release the .exe lock\r\n"
        "set /a tries=0\r\n"
        ":waitloop\r\n"
        'tasklist /FI "PID eq %PID%" | find "%PID%" >nul\r\n'
        "if errorlevel 1 goto swap\r\n"
        "set /a tries+=1\r\n"
        "if %tries% GEQ 30 goto swap\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        "goto waitloop\r\n"
        ":swap\r\n"
        "move /Y %NEW% %EXE% >nul\r\n"
        "start \"\" %EXE% --daemon\r\n"
        "endlocal\r\n"
    )
    bat.write_text(script, encoding="ascii")
    log.info("auto-update: spawning %s and exiting to release exe lock", bat)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    subprocess.Popen(  # noqa: S603
        ["cmd", "/c", str(bat)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    sys.exit(0)


def _apply_update(downloaded: Path) -> None:
    current_exe = Path(sys.executable).resolve()
    if os.name == "nt":
        _replace_windows(downloaded, current_exe)
    else:
        _replace_unix(downloaded, current_exe)


def check_now(
    *,
    current_version: str | None = None,
    fetch=fetch_latest_release,
    downloader=_http_download,
    apply=_apply_update,
    allow_unfrozen: bool = False,
) -> dict:
    """Run a single update probe.

    Returns a status dict so the GUI can render a friendly message:
        {
          "status": "current" | "available" | "applied" | "skipped" | "error",
          "current": "0.4.3",
          "latest":  "0.5.0" | None,
          "reason":  "<human-readable>",
        }

    Dependencies (`fetch`, `downloader`, `apply`) are injected so the smoke
    test can monkey-patch the network call without touching the registry.
    """
    local = current_version or __version__
    status: dict = {"status": "current", "current": local, "latest": None, "reason": ""}

    if not _is_frozen() and not allow_unfrozen:
        status.update(status="skipped", reason="running from source")
        return status

    try:
        release = fetch()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status.update(status="error", reason=f"github fetch failed: {exc}")
        return status
    except Exception as exc:  # noqa: BLE001
        status.update(status="error", reason=f"github fetch failed: {exc}")
        return status

    tag = release.get("tag_name") or ""
    status["latest"] = tag
    if not tag:
        status.update(status="error", reason="release has no tag_name")
        return status

    if not _is_newer(tag, local):
        status["reason"] = f"{local} is current (latest {tag})"
        return status

    if not _is_safe_major(tag):
        status.update(status="skipped", reason=f"refusing major bump {local} → {tag}")
        return status

    expected_sha = _sha256_from_body(release.get("body") or "")
    if not expected_sha:
        status.update(
            status="skipped",
            reason=f"release {tag} has no sha256 in body — refusing blind download",
        )
        return status

    asset = _find_asset(release)
    if not asset:
        status.update(status="error", reason=f"release {tag} missing {ASSET_NAME}")
        return status
    url = asset.get("browser_download_url")
    if not url:
        status.update(status="error", reason=f"release {tag} asset has no download URL")
        return status

    status["status"] = "available"
    status["reason"] = f"new version {tag} available"
    log.info("auto-update: %s available (local %s) — downloading", tag, local)

    with tempfile.TemporaryDirectory(prefix="agentflow-update-") as tmpdir:
        download_path = Path(tmpdir) / ASSET_NAME
        try:
            downloader(url, download_path)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status.update(status="error", reason=f"download failed: {exc}")
            return status
        except Exception as exc:  # noqa: BLE001
            status.update(status="error", reason=f"download failed: {exc}")
            return status

        got = _sha256_of_file(download_path)
        if got != expected_sha:
            status.update(
                status="error",
                reason=f"sha256 mismatch: expected {expected_sha}, got {got}",
            )
            log.error("auto-update: %s", status["reason"])
            return status

        # Move into a stable spot before handing off to the apply step. The
        # apply step may re-exec the process, so the temp dir must survive
        # past this function's `with` block for Windows; we copy to the
        # binary's parent dir.
        staged = Path(sys.executable).parent / f".{ASSET_NAME}.staged"
        shutil.copy2(download_path, staged)
        try:
            apply(staged)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(FileNotFoundError):
                staged.unlink()
            status.update(status="error", reason=f"apply failed: {exc}")
            return status

    status.update(status="applied", reason=f"updated {local} → {tag}")
    return status


def _interval_minutes() -> int:
    """Parse AF_UPDATE_INTERVAL_MIN. 0 disables the loop."""
    raw = os.environ.get("AF_UPDATE_INTERVAL_MIN", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_MIN
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_INTERVAL_MIN
    if v < 0:
        return 0
    return v


def start_in_background(stop_event: threading.Event | None = None) -> threading.Thread | None:
    """Spawn the polling loop. Returns None when auto-update is disabled.

    The loop catches every exception so it cannot ever crash the daemon.
    Backoff doubles after a failed probe, capped at 24h, and resets to the
    configured interval after a clean probe.
    """
    interval_min = _interval_minutes()
    if interval_min == 0:
        log.info("auto-update: disabled via AF_UPDATE_INTERVAL_MIN=0")
        return None
    if not _is_frozen():
        log.info("auto-update: running from source, polling disabled")
        return None

    stop = stop_event or threading.Event()

    def _loop() -> None:
        delay = interval_min * 60
        while not stop.is_set():
            try:
                result = check_now()
                if result.get("status") == "error":
                    log.warning("auto-update: %s", result.get("reason"))
                    delay = min(delay * 2, MAX_INTERVAL_MIN * 60)
                else:
                    if result.get("status") in {"available", "applied"}:
                        log.info("auto-update: %s", result.get("reason"))
                    delay = interval_min * 60
            except Exception as exc:  # noqa: BLE001
                log.warning("auto-update: probe crashed: %s", exc)
                delay = min(delay * 2, MAX_INTERVAL_MIN * 60)
            stop.wait(delay)

    t = threading.Thread(target=_loop, name="auto-updater", daemon=True)
    t.start()
    log.info("auto-update: polling every %d min", interval_min)
    return t


__all__ = [
    "ASSET_NAME",
    "GITHUB_API_LATEST",
    "UpdateError",
    "check_now",
    "fetch_latest_release",
    "start_in_background",
]
