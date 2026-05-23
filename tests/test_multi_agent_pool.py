"""BrowserPool with a fake Playwright stack.

We never touch real chromium in unit tests. The pool's `_launch_browser`
hook is monkey-patched to return a fake browser whose `new_context()`
yields a fresh `FakeContext` each call.
"""
from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentflow_computer_mcp.agents import BrowserPool, PoolFull


class FakeContext:
    _ids = itertools.count()

    def __init__(self) -> None:
        self.id = next(FakeContext._ids)
        self.added_cookies: list[Any] = []
        self.closed = False

    async def add_cookies(self, cookies: list[Any]) -> None:
        self.added_cookies.append(cookies)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.new_contexts: list[FakeContext] = []
        self.closed = False

    async def new_context(self) -> FakeContext:
        ctx = FakeContext()
        self.new_contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


def _patched_pool(**kwargs: Any) -> tuple[BrowserPool, FakeBrowser]:
    pool = BrowserPool(**kwargs)
    browser = FakeBrowser()
    pool._launch_browser = AsyncMock(return_value=browser)  # type: ignore[method-assign]
    return pool, browser


async def test_two_slots_two_contexts() -> None:
    pool, _ = _patched_pool()
    ctx_a = await pool.acquire("a")
    ctx_b = await pool.acquire("b")
    assert ctx_a is not ctx_b
    assert ctx_a.id != ctx_b.id


async def test_same_slot_reuses_context() -> None:
    pool, _ = _patched_pool()
    ctx1 = await pool.acquire("a")
    ctx2 = await pool.acquire("a")
    assert ctx1 is ctx2


async def test_pool_cap_reached() -> None:
    pool, _ = _patched_pool(max_contexts=2)
    await pool.acquire("a")
    await pool.acquire("b")
    with pytest.raises(PoolFull):
        await pool.acquire("c")


async def test_cookie_isolation_between_contexts() -> None:
    pool, _ = _patched_pool()
    ctx_a = await pool.acquire("a")
    ctx_b = await pool.acquire("b")
    await ctx_a.add_cookies([{"name": "sid", "value": "AAA"}])
    # Slot B's context must never see A's cookie writes.
    assert ctx_a.added_cookies == [[{"name": "sid", "value": "AAA"}]]
    assert ctx_b.added_cookies == []


async def test_release_closes_context() -> None:
    pool, _ = _patched_pool()
    ctx_a = await pool.acquire("a")
    await pool.release("a")
    assert ctx_a.closed is True
    assert pool.context_count == 0


async def test_shutdown_closes_everything() -> None:
    pool, browser = _patched_pool()
    await pool.acquire("a")
    await pool.acquire("b")
    await pool.shutdown()
    assert browser.closed is True
    assert pool.context_count == 0
