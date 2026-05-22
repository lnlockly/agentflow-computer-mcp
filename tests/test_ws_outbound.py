"""Driver → WS outbound bridge: task_action frames mirror push_action while a task runs."""
from __future__ import annotations

from typing import Any

from agentflow_computer_mcp.driver.state import DriverState


def test_push_action_publishes_task_action_when_task_active() -> None:
    state = DriverState()
    sent: list[dict[str, Any]] = []
    state.outbound_publisher = sent.append
    state.current_task_id = "t-99"

    state.push_action("mouse_click", detail="x=100 y=200")

    assert len(sent) == 1
    frame = sent[0]
    assert frame["type"] == "task_action"
    assert frame["task_id"] == "t-99"
    assert frame["action"] == "mouse_click"
    assert frame["detail"] == "x=100 y=200"
    assert isinstance(frame["ts"], int)


def test_push_action_does_not_publish_without_active_task() -> None:
    state = DriverState()
    sent: list[dict[str, Any]] = []
    state.outbound_publisher = sent.append
    # current_task_id stays empty (no remote dispatch + no local id assigned)
    state.push_action("idle", detail="waiting")
    assert sent == []


def test_push_action_does_not_publish_without_publisher() -> None:
    state = DriverState()
    state.current_task_id = "t-1"
    # no outbound_publisher set — should be a no-op, not a crash
    state.push_action("anything", detail="ok")


def test_push_action_includes_thinking_in_detail() -> None:
    state = DriverState()
    sent: list[dict[str, Any]] = []
    state.outbound_publisher = sent.append
    state.current_task_id = "t-x"
    state.push_action("thinking", detail="", thinking="I should open the browser")
    assert sent[0]["detail"] == "I should open the browser"


def test_publisher_exception_does_not_crash_push_action() -> None:
    state = DriverState()

    def boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("socket exploded")

    state.outbound_publisher = boom
    state.current_task_id = "t-x"
    state.push_action("step", detail="...")
    assert len(state.actions) == 1


def test_enqueue_task_assigns_local_id_when_missing() -> None:
    state = DriverState()
    tid = state.enqueue_task("hello world")
    assert tid.startswith("local-")
    queued = state.task_queue.get_nowait()
    assert queued == (tid, "hello world")


def test_enqueue_task_keeps_explicit_id() -> None:
    state = DriverState()
    tid = state.enqueue_task("hello", task_id="remote-42")
    assert tid == "remote-42"
    assert state.task_queue.get_nowait() == ("remote-42", "hello")
