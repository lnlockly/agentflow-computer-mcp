"""BrowserPool — lazy Playwright host shared across agent slots.

One chromium binary is launched on first `acquire`. Each call to
`acquire(slot_id)` returns a fresh `BrowserContext` so cookies and
storage_state are isolated between slots. Hard caps prevent runaway
memory: `max_browsers` (we only ever launch one chromium today, kept
for future Firefox/Webkit fan-out) and `max_contexts` (total live
contexts across slots).

Playwright import is deferred so unit tests can run without playwright
installed; the real daemon installs it via `pip install playwright`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class PoolFull(RuntimeError):
    """Raised when the caller wants a new context past `max_contexts`."""


class BrowserPool:
    """Shared chromium + per-slot BrowserContext.

    Usage:
        pool = BrowserPool()
        ctx_a = await pool.acquire("slot_a")
        ctx_b = await pool.acquire("slot_b")  # different cookies than ctx_a
        await pool.release("slot_a")

    `_launch_browser` is overridable so tests can substitute a fake
    playwright stack.
    """

    def __init__(
        self,
        *,
        max_browsers: int = 4,
        max_contexts: int = 8,
        headless: bool = True,
    ) -> None:
        self._max_browsers = max_browsers
        self._max_contexts = max_contexts
        self._headless = headless
        self._browser: Any = None
        self._playwright: Any = None
        self._contexts: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def context_count(self) -> int:
        return len(self._contexts)

    async def acquire(self, slot_id: str) -> Any:
        """Return the BrowserContext for `slot_id`. Reuses an existing context
        if the slot already holds one; otherwise creates a fresh isolated one.
        """
        async with self._lock:
            if slot_id in self._contexts:
                return self._contexts[slot_id]
            if len(self._contexts) >= self._max_contexts:
                log.warning("[browser-pool] cap reached (%d contexts)", len(self._contexts))
                raise PoolFull(
                    f"pool full: {len(self._contexts)} contexts active (max {self._max_contexts})"
                )
            if self._browser is None:
                self._browser = await self._launch_browser()
            ctx = await self._browser.new_context()
            self._contexts[slot_id] = ctx
            return ctx

    async def release(self, slot_id: str) -> None:
        """Close the slot's context. Idempotent."""
        async with self._lock:
            ctx = self._contexts.pop(slot_id, None)
        if ctx is None:
            return
        try:
            await ctx.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("[browser-pool] context close failed: %s", exc)

    async def shutdown(self) -> None:
        """Close every context and stop the browser. Used on daemon exit."""
        ids = list(self._contexts.keys())
        for slot_id in ids:
            await self.release(slot_id)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("[browser-pool] browser close failed: %s", exc)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("[browser-pool] playwright stop failed: %s", exc)
            self._playwright = None

    async def _launch_browser(self) -> Any:
        # Deferred import: playwright is optional at unit-test time.
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]

        self._playwright = await async_playwright().start()
        return await self._playwright.chromium.launch(headless=self._headless)
