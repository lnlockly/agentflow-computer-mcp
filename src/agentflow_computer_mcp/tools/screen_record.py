"""Local screen-video recording.

Spawns ffmpeg with stdin=PIPE and feeds JPEG frames captured via
``streamer.fast_capture_jpeg`` into the process at a fixed cadence. Output
goes to a local ``.mp4`` file under the user's ``~/Movies``, ``~/tmp``,
or an explicit ``recordings/`` directory; the rest of the home tree is
refused so a stuck recording can't dump frames into ``~/.ssh`` or similar.

Singleton: only one recording at a time. ``max_duration_s`` is a hard cap so
a forgotten ``stop()`` cannot fill the disk.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import threading
import time
from glob import glob
from pathlib import Path
from typing import Any

from ..driver.streamer import fast_capture_jpeg

log = logging.getLogger(__name__)

# Output directories the recorder may write into. Everything else is refused.
_ALLOWED_OUTPUT_ROOTS: tuple[str, ...] = (
    "~/Movies",
    "~/tmp",
    "~/Downloads",
)
# Additional escape hatch: a "recordings/" directory anywhere under $HOME is
# allowed too — useful when the daemon runs inside a workspace.
_RECORDINGS_DIRNAME = "recordings"

_FFMPEG_BUNDLE_GLOBS: tuple[tuple[str, str], ...] = (
    ("~/Library/Caches/ms-playwright/ffmpeg-*", "ffmpeg-mac"),
    ("~/.cache/ms-playwright/ffmpeg-*", "ffmpeg-linux"),
    ("~/AppData/Local/ms-playwright/ffmpeg-*", "ffmpeg-win.exe"),
)


def find_ffmpeg() -> str | None:
    """Cross-OS ffmpeg discovery.

    1. ``shutil.which("ffmpeg")``.
    2. Playwright vendored bundle.
    3. ``None`` if neither is available.
    """
    direct = shutil.which("ffmpeg")
    if direct:
        return direct
    for pattern, binary_name in _FFMPEG_BUNDLE_GLOBS:
        for match in sorted(glob(str(Path(pattern).expanduser()))):
            candidate = Path(match) / binary_name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


_HARD_DENY_PATH_ROOTS: tuple[str, ...] = (
    "~/.ssh",
    "~/.config",
    "~/Library/Keychains",
    "~/.aws",
    "~/.gnupg",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/System",
)


def _check_output_path(path: str | Path) -> Path:
    """Resolve and validate the output path.

    Returns the resolved ``Path`` on success, raises ``ValueError`` with
    ``scope_blocked_path`` if the file would land outside the allowed roots.

    Allowed: ``~/Movies``, ``~/tmp``, ``~/Downloads``, OR any path whose
    components include a ``recordings/`` segment (so workspace-style layouts
    like ``~/Code/proj/recordings/clip.mp4`` work). Always refused: the
    hard-deny system roots regardless of layout.
    """
    target = Path(path).expanduser().resolve(strict=False)

    def _within(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    # Hard deny wins over any allow rule.
    for deny in _HARD_DENY_PATH_ROOTS:
        deny_resolved = Path(deny).expanduser().resolve(strict=False)
        if _within(target, deny_resolved):
            raise ValueError("scope_blocked_path")

    for root in _ALLOWED_OUTPUT_ROOTS:
        root_resolved = Path(root).expanduser().resolve(strict=False)
        if _within(target, root_resolved):
            return target

    # Allow any ".../recordings/..." segment in the path.
    if _RECORDINGS_DIRNAME in target.parts:
        return target

    raise ValueError("scope_blocked_path")


class ScreenRecorder:
    """Thread-safe singleton recorder.

    State transitions:
    - ``idle`` → ``start()`` → ``recording``
    - ``recording`` → ``stop()`` or auto-stop → ``idle``
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._worker: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._path: Path | None = None
        self._started_at: float = 0.0
        self._fps: int = 10
        self._width_cap: int = 1280
        self._max_duration_s: int = 120
        self._frames_written: int = 0
        self._stopped_at: float | None = None

    # ── public API ───────────────────────────────────────────────────────

    def start(
        self,
        path: Path | str,
        fps: int = 10,
        width_cap: int = 1280,
        max_duration_s: int = 120,
    ) -> dict[str, Any]:
        with self._lock:
            if self._proc is not None:
                return {"ok": False, "error": "already_recording"}

            try:
                output = _check_output_path(path)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            ffmpeg = find_ffmpeg()
            if ffmpeg is None:
                return {"ok": False, "error": "ffmpeg_not_found"}

            output.parent.mkdir(parents=True, exist_ok=True)

            cmd = [
                ffmpeg,
                "-y",
                "-f", "mjpeg",
                "-framerate", str(fps),
                "-i", "pipe:0",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-preset", "veryfast",
                "-crf", "28",
                str(output),
            ]

            try:
                proc = subprocess.Popen(  # noqa: S603 — args list, fixed binary
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                return {"ok": False, "error": f"ffmpeg_spawn_failed: {exc}"}

            self._proc = proc
            self._path = output
            self._fps = max(1, int(fps))
            self._width_cap = int(width_cap)
            self._max_duration_s = int(max_duration_s)
            self._started_at = time.time()
            self._frames_written = 0
            self._stopped_at = None
            self._stop_evt = threading.Event()

            self._worker = threading.Thread(
                target=self._run, name="screen-recorder", daemon=True
            )
            self._worker.start()

            return {
                "ok": True,
                "path": str(output),
                "started_at": self._started_at,
                "max_duration_s": self._max_duration_s,
            }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._proc is None:
                return {"ok": False, "error": "not_recording"}
            proc = self._proc
            path = self._path
            self._stop_evt.set()

        worker = self._worker
        if worker is not None:
            worker.join(timeout=5.0)

        # Close ffmpeg stdin so it can flush + finalize the file.
        if proc.stdin is not None:
            # Already closed by worker on broken-pipe is fine.
            with contextlib.suppress(Exception):
                proc.stdin.close()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)

        with self._lock:
            self._stopped_at = time.time()
            duration_ms = int((self._stopped_at - self._started_at) * 1000)
            file_bytes = 0
            if path is not None and path.exists():
                file_bytes = path.stat().st_size
            result = {
                "ok": True,
                "path": str(path) if path is not None else None,
                "duration_ms": duration_ms,
                "file_bytes": file_bytes,
                "frames_written": self._frames_written,
            }
            self._proc = None
            self._worker = None
            self._path = None

        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            recording = self._proc is not None
            elapsed_ms = (
                int((time.time() - self._started_at) * 1000) if recording else 0
            )
            return {
                "recording": recording,
                "path": str(self._path) if self._path else None,
                "frames_written": self._frames_written,
                "elapsed_ms": elapsed_ms,
            }

    # ── internals ────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Worker loop: capture → write to ffmpeg stdin at ``1/fps`` cadence."""
        period = 1.0 / max(1, self._fps)
        deadline = self._started_at + self._max_duration_s

        while not self._stop_evt.is_set():
            loop_start = time.time()
            if loop_start >= deadline:
                # Auto-stop: schedule cleanup on a side thread so we don't
                # self-join inside the worker. The side thread calls stop()
                # which sets _stop_evt and joins us — we exit the loop
                # immediately, then it finishes the ffmpeg drain.
                threading.Thread(target=self._auto_stop, daemon=True).start()
                return

            try:
                frame = fast_capture_jpeg(
                    width_cap=self._width_cap, quality=70
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[screen_record] capture err: %s", exc)
                time.sleep(period)
                continue

            proc = self._proc
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                # ffmpeg exited; bail.
                return

            with self._lock:
                self._frames_written += 1

            dt = time.time() - loop_start
            if dt < period:
                # Honour stop_evt during the sleep window so stop() is snappy.
                self._stop_evt.wait(period - dt)

    def _auto_stop(self) -> None:
        # Best-effort: stop() handles the "already stopped" race gracefully
        # because we re-check ``self._proc`` under the lock.
        try:
            self.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("[screen_record] auto_stop err: %s", exc)


_singleton: ScreenRecorder | None = None
_singleton_lock = threading.Lock()


def get_recorder() -> ScreenRecorder:
    """Process-wide singleton."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ScreenRecorder()
    return _singleton


__all__ = [
    "ScreenRecorder",
    "find_ffmpeg",
    "get_recorder",
]
