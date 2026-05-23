"""Pure-data tests over `winapp.menu.build_menu`.

No pystray, no I/O — just assert the menu structure changes correctly
with `TrayState`.
"""
from __future__ import annotations

from agentflow_computer_mcp.winapp.menu import build_menu
from agentflow_computer_mcp.winapp.state import AgentRow, Budget, GoalRow, TrayState


def _labels(items) -> list[str]:
    return [i.label for i in items]


def test_daemon_down_with_three_cloud_goals_shows_correct_items() -> None:
    state = TrayState(
        daemon="down",
        agents=(),
        goals=(
            GoalRow(id="g1", title="Цель 1", status="pending"),
            GoalRow(id="g2", title="Цель 2", status="running"),
            GoalRow(id="g3", title="Цель 3", status="done"),
        ),
        budget=Budget(spent=1.25, cap=5.0),
        authenticated=True,
    )
    items = build_menu(
        state,
        on_open_cabinet=lambda: None,
        on_restart_daemon=lambda: None,
        on_quit=lambda: None,
    )
    labels = _labels(items)

    assert labels[0] == "Демон не запущен"
    assert "Агенты" in labels
    assert "Цели" in labels
    assert any(lbl.startswith("Бюджет: $1.25 / $5.00") for lbl in labels)
    assert "Открыть кабинет" in labels
    assert "Перезапустить демон" in labels
    assert "Выйти" in labels

    agents_item = next(i for i in items if i.label == "Агенты")
    assert _labels(agents_item.children) == ["Демон не запущен"]

    goals_item = next(i for i in items if i.label == "Цели")
    goal_labels = _labels(goals_item.children)
    assert goal_labels == ["[pending] Цель 1", "[running] Цель 2", "[done] Цель 3"]


def test_daemon_up_with_two_agents_and_five_goals() -> None:
    state = TrayState(
        daemon="up",
        agents=(
            AgentRow(id="a1", name="Pikku", status="running"),
            AgentRow(id="a2", name="Mika", status="paused"),
        ),
        goals=tuple(GoalRow(id=f"g{i}", title=f"Цель {i}", status="pending") for i in range(5)),
        budget=Budget(spent=0.5, cap=2.0),
        authenticated=True,
    )

    items = build_menu(
        state,
        on_open_cabinet=lambda: None,
        on_restart_daemon=lambda: None,
        on_quit=lambda: None,
        on_kill_agent=lambda slot_id: (lambda: None),
    )
    labels = _labels(items)
    assert labels[0] == "Подключено"

    agents_item = next(i for i in items if i.label == "Агенты")
    agent_labels = _labels(agents_item.children)
    assert agent_labels == ["Pikku (running)", "Mika (paused)"]
    # Each agent has a Kill action enabled
    for ag in agents_item.children:
        kill_entry = next(c for c in ag.children if c.label == "Kill")
        assert kill_entry.enabled is True
        assert kill_entry.action is not None

    goals_item = next(i for i in items if i.label == "Цели")
    assert len(goals_item.children) == 5


def test_windows_unsupported_header_disables_local_features() -> None:
    state = TrayState(
        daemon="unsupported",
        goals=(GoalRow(id="g1", title="Cloud goal", status="running"),),
        budget=Budget(spent=0.0, cap=2.0),
        authenticated=True,
    )
    items = build_menu(
        state,
        on_open_cabinet=lambda: None,
        on_restart_daemon=lambda: None,
        on_quit=lambda: None,
    )
    labels = _labels(items)
    assert labels[0] == "Локальные команды требуют Windows-pipe — в работе"
    restart = next(i for i in items if i.label == "Перезапустить демон")
    assert restart.enabled is False
    # Cloud goals still rendered
    goals_item = next(i for i in items if i.label == "Цели")
    assert _labels(goals_item.children) == ["[running] Cloud goal"]


def test_unauthenticated_hides_goals_and_budget_values() -> None:
    state = TrayState(daemon="down", authenticated=False)
    items = build_menu(
        state,
        on_open_cabinet=lambda: None,
        on_restart_daemon=lambda: None,
        on_quit=lambda: None,
    )
    goals_item = next(i for i in items if i.label == "Цели")
    assert _labels(goals_item.children) == ["Не авторизован — agentflow login"]
    budget_item = next(i for i in items if i.label.startswith("Бюджет"))
    assert budget_item.label == "Бюджет: —"
