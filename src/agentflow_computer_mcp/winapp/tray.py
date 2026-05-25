"""pystray glue.

This module imports `pystray` lazily because the package is optional —
tests and `--version` don't need it. The rest of `winapp` is pystray-free.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable

from . import actions, cloud, daemon_probe, menu
from .icon import load_icon
from .state import TrayState

POLL_INTERVAL_SECONDS = 30.0


def _refresh_state() -> TrayState:
    status, agents = daemon_probe.probe()
    authed_goals, goals = cloud.fetch_goals()
    authed_budget, budget = cloud.fetch_budget()
    authenticated = authed_goals or authed_budget
    return TrayState(
        daemon=status,
        agents=agents,
        goals=goals,
        budget=budget,
        authenticated=authenticated,
    )


def _to_pystray(items, icon_ref, pystray):
    """Convert our `MenuItem` tuple to a `pystray.Menu`."""
    out = []
    for item in items:
        if item.is_separator:
            out.append(pystray.Menu.SEPARATOR)
            continue
        if item.children:
            submenu = _to_pystray(item.children, icon_ref, pystray)
            out.append(pystray.MenuItem(item.label, submenu, enabled=item.enabled))
            continue
        action = item.action

        def _make_wrapped(act: Callable[[], None]) -> Callable[..., None]:
            def _wrapped(*_args: object, **_kwargs: object) -> None:
                act()
            return _wrapped

        wrapped = _make_wrapped(action) if action is not None else None
        out.append(pystray.MenuItem(item.label, wrapped, enabled=item.enabled))
    return pystray.Menu(*out)


def run() -> int:
    """Start the tray. Blocks until the user picks "Выйти". Returns exit code."""
    try:
        import pystray
    except ImportError as exc:
        print(f"pystray not installed: {exc}", flush=True)
        print("install with: pip install pystray Pillow", flush=True)
        return 2

    state_lock = threading.Lock()
    current_state = _refresh_state()
    icon_holder: dict[str, object] = {}

    def kill_action_factory(slot_id: str) -> Callable[[], None]:
        def _do() -> None:
            actions.kill_agent(slot_id)

        return _do

    def _notify(text: str) -> None:
        ic = icon_holder.get("icon")
        if ic is None:
            return
        with contextlib.suppress(Exception):
            ic.notify(text, title="AgentFlow")  # type: ignore[attr-defined]

    def _restore_connection() -> None:
        # Runs on the pystray callback thread; the Defender helper itself
        # is fire-and-forget (ShellExecuteW returns immediately after the
        # user clicks the UAC prompt) so blocking the menu briefly is OK.
        actions.restore_connection(notifier=_notify)

    restore_cb: Callable[[], None] | None = (
        _restore_connection if os.name == "nt" else None
    )

    def rebuild_menu() -> object:
        with state_lock:
            state = current_state
        items = menu.build_menu(
            state,
            on_open_cabinet=actions.open_cabinet,
            on_restart_daemon=actions.restart_daemon,
            on_restore_connection=restore_cb,
            on_quit=lambda: actions.quit_tray(icon_holder.get("icon")),
            on_kill_agent=kill_action_factory,
        )
        return _to_pystray(items, icon_holder, pystray)

    icon = pystray.Icon(
        "agentflow",
        icon=load_icon(),
        title="AgentFlow",
        menu=rebuild_menu(),
    )
    icon_holder["icon"] = icon

    def poll() -> None:
        nonlocal current_state
        while getattr(icon, "visible", True):
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                new_state = _refresh_state()
            except Exception:
                continue
            with state_lock:
                current_state = new_state
            try:
                icon.menu = rebuild_menu()
                icon.update_menu()
            except Exception:
                continue

    threading.Thread(target=poll, daemon=True).start()
    icon.run()
    return 0
