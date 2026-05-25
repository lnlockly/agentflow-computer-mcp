"""Centralised logging configuration for the desktop daemon.

Three call-sites used to repeat `logging.basicConfig(...)`:
`desktop_cli.cmd_run`, `desktop_cli.cmd_drive`, and `__main__.main`. None
of them attached a file handler, so a daemon launched from `schtasks` or
`launchd` (where stderr is discarded) left the user with zero on-disk
trace when something blew up. Reproducing a bug required the user to
open PowerShell and re-run the daemon by hand — a blocker for
diagnostics.

`init_logging` replaces those three call-sites with a single setup that
adds a rotating file handler in a per-platform log directory plus the
existing stderr stream. The directory is created with `parents=True,
exist_ok=True`; if creation fails (read-only home, sandboxed user) the
file handler is skipped and a warning lands on stderr — the daemon
still starts.

The matching `WsLogHandler` (in `ws_log_uploader.py`) ships ERROR-level
records to the backend over the existing WS so the cabinet can show a
recent error trail without the user touching the disk.
"""
from __future__ import annotations

import contextlib
import logging
import os
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
DEFAULT_BACKUP_COUNT = 5
LOG_FILENAME = "daemon.log"


def log_dir() -> Path:
    """Return the per-platform directory for daemon log files.

    Windows: ``%LOCALAPPDATA%\\AgentFlow\\logs\\`` (fallback ``%APPDATA%``,
    then ``~/AppData/Local/AgentFlow/logs``).
    macOS: ``~/Library/Logs/AgentFlow/``.
    Linux / other: ``$XDG_STATE_HOME/agentflow/logs`` or
    ``~/.local/state/agentflow/logs``.

    Pure: no filesystem side effects. Callers do the ``mkdir`` themselves
    so failure-handling lives in one place.
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "AgentFlow" / "logs"
        return Path.home() / "AppData" / "Local" / "AgentFlow" / "logs"
    if system == "Darwin":
        return Path.home() / "Library" / "Logs" / "AgentFlow"
    # Linux + every other POSIX. XDG Base Directory spec: state dir for
    # logs and other "persistent data that should not be in cache".
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "agentflow" / "logs"
    return Path.home() / ".local" / "state" / "agentflow" / "logs"


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def init_logging(
    level: str | int = "INFO",
    *,
    log_directory: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    extra_handlers: list[logging.Handler] | None = None,
    force: bool = True,
) -> Path | None:
    """Configure the root logger with stderr + rotating file handlers.

    Returns the path to the active log file, or ``None`` when the file
    handler could not be created (e.g. read-only home directory). In
    that case logging stays available via stderr only.

    ``force=True`` mirrors `logging.basicConfig(..., force=True)`: any
    handlers attached by an earlier call are removed before we add the
    new ones. Tests rely on this to keep handlers isolated between
    cases.
    """
    resolved_level = _resolve_level(level)
    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
    root.setLevel(resolved_level)

    formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    target_dir = log_directory if log_directory is not None else log_dir()
    log_path: Path | None = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        log_path = target_dir / LOG_FILENAME
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # No file handler. Stay alive on stderr only.
        print(
            f"agentflow-desktop: log file unavailable at {target_dir}: {exc}",
            file=sys.stderr,
        )
        log_path = None

    for handler in extra_handlers or []:
        root.addHandler(handler)

    return log_path
