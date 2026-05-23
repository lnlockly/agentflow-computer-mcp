"""AgentRouter dispatch + consumer survival."""
from __future__ import annotations

import asyncio

import pytest

from agentflow_computer_mcp.agents import AgentRouter, AgentSlot


async def _no_op_handler(_slot: AgentSlot, _frame: dict) -> None:
    return None


def test_router_requires_at_least_one_slot() -> None:
    with pytest.raises(ValueError):
        AgentRouter([], _no_op_handler)


async def test_dispatch_routes_to_correct_slot() -> None:
    a = AgentSlot(id="a")
    b = AgentSlot(id="b")
    router = AgentRouter([a, b], _no_op_handler)
    resolved = router.dispatch({"agent_id": "b", "id": "t1", "task": "do"})
    assert resolved == "b"
    assert b.queue.qsize() == 1
    assert a.queue.qsize() == 0


async def test_unknown_agent_falls_back_to_default() -> None:
    default = AgentSlot(id="default")
    trader = AgentSlot(id="trader")
    router = AgentRouter([default, trader], _no_op_handler)
    resolved = router.dispatch({"agent_id": "ghost", "id": "t1", "task": "do"})
    assert resolved == "default"
    assert default.queue.qsize() == 1


async def test_missing_agent_id_falls_back() -> None:
    default = AgentSlot(id="default")
    router = AgentRouter([default], _no_op_handler)
    resolved = router.dispatch({"id": "t1", "task": "do"})
    assert resolved == "default"


async def test_consumer_survives_crash() -> None:
    """First task raises, second succeeds. Consumer must keep going."""
    seen: list[str] = []

    async def handler(_slot: AgentSlot, frame: dict) -> None:
        seen.append(frame["id"])
        if frame["id"] == "boom":
            raise RuntimeError("synthetic")

    slot = AgentSlot(id="default")
    router = AgentRouter([slot], handler)
    await router.start()
    try:
        router.dispatch({"agent_id": "default", "id": "boom", "task": "x"})
        router.dispatch({"agent_id": "default", "id": "ok", "task": "y"})
        # Let the consumer drain the queue.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if seen == ["boom", "ok"]:
                break
    finally:
        await router.stop()
    assert seen == ["boom", "ok"]
    assert slot.last_error.startswith("synthetic")


async def test_two_slots_run_in_parallel() -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()
    results: list[str] = []

    async def handler(slot: AgentSlot, frame: dict) -> None:
        if frame["id"] == "slow":
            started.set()
            await proceed.wait()
        results.append(f"{slot.id}:{frame['id']}")

    a = AgentSlot(id="a")
    b = AgentSlot(id="b")
    router = AgentRouter([a, b], handler)
    await router.start()
    try:
        router.dispatch({"agent_id": "a", "id": "slow", "task": "x"})
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # While A is blocked, dispatch to B and confirm it completes.
        router.dispatch({"agent_id": "b", "id": "quick", "task": "y"})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if "b:quick" in results:
                break
        assert "b:quick" in results, "slot B blocked by slot A"
        proceed.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if "a:slow" in results:
                break
        assert "a:slow" in results
    finally:
        proceed.set()
        await router.stop()
