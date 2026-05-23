"""End-to-end smoke: 2 slots, 2 contexts, 2 tasks, no real browser.

Asserts the contract that lets the README claim «one host runs N agents
in parallel»:
  1. Each slot acquires its own BrowserContext.
  2. A cookie set in slot A's context never reaches slot B's.
  3. Both tasks complete without one blocking the other.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentflow_computer_mcp.agents import AgentRouter, AgentSlot, BrowserPool


class FakeContext:
    _ids = itertools.count()

    def __init__(self) -> None:
        self.id = next(FakeContext._ids)
        self.cookies: list[Any] = []
        self.closed = False

    async def add_cookies(self, cookies: list[Any]) -> None:
        self.cookies.extend(cookies)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    async def new_context(self) -> FakeContext:
        return FakeContext()

    async def close(self) -> None:
        return None


async def test_two_slots_two_contexts_two_tasks() -> None:
    pool = BrowserPool()
    pool._launch_browser = AsyncMock(return_value=FakeBrowser())  # type: ignore[method-assign]

    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    completed = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def handler(slot: AgentSlot, frame: dict) -> None:
        # Lazy-attach the context.
        if slot.browser_context is None:
            slot.browser_context = await pool.acquire(slot.id)
        started[slot.id].set()
        # Slot A sets a cookie; slot B never touches it.
        if slot.id == "a":
            await slot.browser_context.add_cookies(
                [{"name": "sid", "value": "from-A"}]
            )
        completed[slot.id].set()

    a = AgentSlot(id="a")
    b = AgentSlot(id="b")
    router = AgentRouter([a, b], handler)
    await router.start()
    try:
        router.dispatch({"agent_id": "a", "id": "t1", "task": "x"})
        router.dispatch({"agent_id": "b", "id": "t2", "task": "y"})
        await asyncio.wait_for(completed["a"].wait(), timeout=1.0)
        await asyncio.wait_for(completed["b"].wait(), timeout=1.0)
    finally:
        await router.stop()
        await pool.shutdown()

    # Both contexts exist and are distinct.
    assert a.browser_context is not None
    assert b.browser_context is not None
    assert a.browser_context is not b.browser_context
    # Cookie isolation: only A's context received the cookie.
    assert a.browser_context.cookies == [{"name": "sid", "value": "from-A"}]
    assert b.browser_context.cookies == []


async def test_dispatch_with_unknown_id_falls_back_and_runs() -> None:
    """End-to-end: a frame with an unknown agent_id still completes."""
    ran = asyncio.Event()

    async def handler(_slot: AgentSlot, _frame: dict) -> None:
        ran.set()

    default = AgentSlot(id="default")
    router = AgentRouter([default], handler)
    await router.start()
    try:
        router.dispatch({"agent_id": "ghost", "id": "t1", "task": "x"})
        await asyncio.wait_for(ran.wait(), timeout=1.0)
    finally:
        await router.stop()


@pytest.mark.parametrize("count", [3, 5])
async def test_many_slots_can_be_registered(count: int) -> None:
    slots = [AgentSlot(id=f"agent-{i}") for i in range(count)]
    router = AgentRouter(slots, lambda _s, _f: asyncio.sleep(0))
    assert len(router.slot_ids) == count
