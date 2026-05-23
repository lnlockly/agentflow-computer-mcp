"""BudgetSlice — per-slot USD cap.

Each LLM call (or any costed action) deducts from the slice. When the
remaining balance drops below the next deduction the slice raises
`BudgetExhausted`, which the slot consumer catches to transition the
slot to `paused`.
"""
from __future__ import annotations

import asyncio


class BudgetExhausted(Exception):
    """Raised when a deduction would push the slice below zero."""


class BudgetSlice:
    """Atomic USD counter scoped to one agent.

    Not thread-safe at the bare-counter level — guarded by `asyncio.Lock`
    so concurrent coroutines on the same slot serialize their deductions.
    """

    def __init__(self, initial_usd: float) -> None:
        if initial_usd < 0:
            raise ValueError("initial_usd must be non-negative")
        self._remaining = float(initial_usd)
        self._lock = asyncio.Lock()

    @property
    def remaining(self) -> float:
        return self._remaining

    async def deduct(self, usd: float) -> float:
        """Subtract `usd` atomically. Raise `BudgetExhausted` if it would underflow.

        Returns the new remaining balance.
        """
        if usd < 0:
            raise ValueError("usd must be non-negative")
        async with self._lock:
            if self._remaining - usd < 0:
                raise BudgetExhausted(
                    f"budget exhausted: requested {usd:.4f} > remaining {self._remaining:.4f}"
                )
            self._remaining -= usd
            return self._remaining

    def deduct_sync(self, usd: float) -> float:
        """Sync variant for code paths outside an event loop (tests, control socket).

        Skips the lock — callers must ensure no concurrent async deduct is in
        flight, otherwise use `deduct`.
        """
        if usd < 0:
            raise ValueError("usd must be non-negative")
        if self._remaining - usd < 0:
            raise BudgetExhausted(
                f"budget exhausted: requested {usd:.4f} > remaining {self._remaining:.4f}"
            )
        self._remaining -= usd
        return self._remaining
