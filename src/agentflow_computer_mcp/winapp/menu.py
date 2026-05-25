"""Pure-data menu builder.

Returns a tree of `MenuItem` dataclasses that the tray driver
(`tray.py`) converts into `pystray.MenuItem`s. Keeping this layer free
of pystray means tests can assert structure without spinning the icon
backend up.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..cli.format import fmt_budget
from .state import TrayState


@dataclass(frozen=True)
class MenuItem:
    label: str
    enabled: bool = True
    is_separator: bool = False
    children: tuple[MenuItem, ...] = field(default_factory=tuple)
    action: Callable[[], None] | None = None


def _separator() -> MenuItem:
    return MenuItem(label="-", is_separator=True, enabled=False)


def build_agents_submenu(
    state: TrayState,
    kill_action: Callable[[str], Callable[[], None]] | None = None,
) -> tuple[MenuItem, ...]:
    if state.daemon == "unsupported":
        return (MenuItem(label="Локальный демон недоступен на Windows (см. #94)", enabled=False),)
    if state.daemon == "down":
        return (MenuItem(label="Демон не запущен", enabled=False),)
    if not state.agents:
        return (MenuItem(label="(пусто)", enabled=False),)
    items: list[MenuItem] = []
    for ag in state.agents:
        label = f"{ag.name or ag.id} ({ag.status or 'idle'})"
        kill_cb = kill_action(ag.id) if kill_action else None
        items.append(
            MenuItem(
                label=label,
                children=(
                    MenuItem(label=f"id: {ag.id}", enabled=False),
                    MenuItem(label="Kill", action=kill_cb, enabled=kill_cb is not None),
                ),
            )
        )
    return tuple(items)


def build_goals_submenu(state: TrayState) -> tuple[MenuItem, ...]:
    if not state.authenticated:
        return (MenuItem(label="Не авторизован — agentflow login", enabled=False),)
    if not state.goals:
        return (MenuItem(label="(нет целей)", enabled=False),)
    items: list[MenuItem] = []
    for g in state.goals:
        tag = f"[{g.status}] " if g.status else ""
        title = (g.title[:42] + "…") if len(g.title) > 43 else g.title
        items.append(MenuItem(label=f"{tag}{title}", enabled=False))
    return tuple(items)


def _budget_label(state: TrayState) -> str:
    if not state.authenticated:
        return "Бюджет: —"
    return f"Бюджет: {fmt_budget(state.budget.spent, state.budget.cap)}"


def build_menu(
    state: TrayState,
    *,
    on_open_cabinet: Callable[[], None] | None = None,
    on_restart_daemon: Callable[[], None] | None = None,
    on_restore_connection: Callable[[], None] | None = None,
    on_quit: Callable[[], None] | None = None,
    on_kill_agent: Callable[[str], Callable[[], None]] | None = None,
) -> tuple[MenuItem, ...]:
    """Return the full top-level menu as a tuple of `MenuItem`.

    `on_restore_connection` is the Windows-only Defender exclusion path.
    When `None` (mac / linux) the entry is omitted entirely instead of
    rendered greyed-out — non-Windows users would be confused by an
    inactive Windows-only label.
    """
    header = MenuItem(label=state.header, enabled=False)
    agents = MenuItem(label="Агенты", children=build_agents_submenu(state, on_kill_agent))
    goals = MenuItem(label="Цели", children=build_goals_submenu(state))
    budget = MenuItem(label=_budget_label(state), enabled=False)

    items: list[MenuItem] = [
        header,
        _separator(),
        agents,
        goals,
        budget,
        _separator(),
        MenuItem(
            label="Открыть кабинет",
            action=on_open_cabinet,
            enabled=on_open_cabinet is not None,
        ),
        MenuItem(
            label="Перезапустить демон",
            action=on_restart_daemon,
            enabled=on_restart_daemon is not None and state.daemon != "unsupported",
        ),
    ]
    if on_restore_connection is not None:
        items.append(
            MenuItem(
                label="Восстановить связь",
                action=on_restore_connection,
                enabled=True,
            )
        )
    items.append(MenuItem(label="Выйти", action=on_quit, enabled=on_quit is not None))
    return tuple(items)
