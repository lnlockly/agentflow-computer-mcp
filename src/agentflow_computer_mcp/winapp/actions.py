"""Side-effecting menu actions: open browser, restart daemon, kill agent.

Each function is pure-function-ish: it takes its dependencies as args so
tests can substitute fakes. The tray glue (`tray.py`) wires them up to
the real `subprocess` / `webbrowser` modules.
"""
from __future__ import annotations

import contextlib
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from typing import Protocol

from ..cli import socket_client

CABINET_URL = "https://agentflow.website/cabinet"


class _Opener(Protocol):
    def __call__(self, url: str) -> bool: ...


def open_cabinet(opener: _Opener | None = None) -> bool:
    fn = opener or webbrowser.open
    try:
        return bool(fn(CABINET_URL))
    except Exception:
        return False


def restart_daemon(runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> bool:
    """Best-effort: stop + start the daemon via `agentflow daemon`.

    Returns True if both subprocesses exit 0. On Windows the daemon
    itself is not yet wired, so this is wired through `agentflow daemon`
    which already prints a friendly "недоступно" message — we propagate
    its exit code.
    """
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, check=False))
    try:
        stop = run(["agentflow", "daemon", "stop"])
        start = run(["agentflow", "daemon", "start"])
    except FileNotFoundError:
        return False
    return stop.returncode == 0 and start.returncode == 0


def kill_agent(slot_id: str, caller: Callable[..., object] | None = None) -> bool:
    fn = caller or socket_client.call
    try:
        fn("pause", id=slot_id)
    except (socket_client.DaemonUnavailable, socket_client.DaemonError, OSError):
        return False
    return True


def quit_tray(icon) -> None:  # type: ignore[no-untyped-def]
    """Called by the "Выйти" entry. `icon` is a `pystray.Icon` (or a stub)."""
    with contextlib.suppress(Exception):
        icon.visible = False
    with contextlib.suppress(Exception):
        icon.stop()
    sys.stdout.flush()
