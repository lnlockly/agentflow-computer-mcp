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


def restore_connection(
    add_exclusion: Callable[[], tuple[bool, str]] | None = None,
    notifier: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Re-run the Defender exclusion step for an already-installed daemon.

    Used by the «Восстановить связь» menu entry — covers users who
    installed v0.4.x before the installer started doing this on its own.
    Triggers a single UAC prompt and returns the outcome so the caller
    (the tray) can show a balloon notification.

    `add_exclusion` is injectable for tests; in production it's the
    helper from `installer.setup_gui`. We do a late import there so the
    tray .exe doesn't pull in Tk on every start.
    """
    if add_exclusion is None:
        try:
            from installer.setup_gui import (  # type: ignore[import-not-found]
                add_defender_exclusion,
            )
            add_exclusion = add_defender_exclusion
        except Exception as exc:  # noqa: BLE001
            if notifier:
                notifier(f"Не получилось загрузить установщик: {exc}")
            return False, f"import_failed: {exc}"
    try:
        ok, reason = add_exclusion()
    except Exception as exc:  # noqa: BLE001
        if notifier:
            notifier(f"Ошибка: {exc}")
        return False, f"unexpected: {exc}"
    if notifier:
        if ok:
            notifier("Исключение Defender добавлено")
        elif reason == "user_declined":
            notifier("Вы отказались от запроса UAC")
        elif reason == "not_windows":
            notifier("Доступно только на Windows")
        else:
            notifier(f"Не получилось: {reason}")
    return ok, reason


def quit_tray(icon) -> None:  # type: ignore[no-untyped-def]
    """Called by the "Выйти" entry. `icon` is a `pystray.Icon` (or a stub)."""
    with contextlib.suppress(Exception):
        icon.visible = False
    with contextlib.suppress(Exception):
        icon.stop()
    sys.stdout.flush()
