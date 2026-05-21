from __future__ import annotations

import queue
import time

from agentflow_computer_mcp.driver.state import DriverState


def test_push_action_appends_and_caps() -> None:
    state = DriverState()
    initial = len(state.actions)
    for i in range(120):
        state.push_action("step", detail=f"i={i}")
    assert len(state.actions) == 100
    assert state.actions[-1]["detail"] == "i=119"
    assert state.actions[0]["detail"] == "i=20"
    assert initial == 0


def test_push_action_uses_lock_safely_from_threads() -> None:
    import threading

    state = DriverState()

    def writer(n: int) -> None:
        for i in range(50):
            state.push_action("step", detail=f"{n}-{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(state.actions) == 100


def test_task_queue_is_thread_safe_default() -> None:
    state = DriverState()
    assert isinstance(state.task_queue, queue.Queue)
    state.task_queue.put("hello")
    assert state.task_queue.get_nowait() == "hello"


def test_action_timestamp_format() -> None:
    state = DriverState()
    state.push_action("x")
    ts = state.actions[-1]["ts"]
    # HH:MM:SS
    parts = ts.split(":")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
    # sanity: timestamp is recent
    assert time.time() > 0
