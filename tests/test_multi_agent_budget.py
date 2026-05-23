"""BudgetSlice deductions and exhaustion."""
from __future__ import annotations

import pytest

from agentflow_computer_mcp.agents import BudgetExhausted, BudgetSlice


async def test_deduct_then_exhaust() -> None:
    slice_ = BudgetSlice(0.10)
    assert await slice_.deduct(0.04) == pytest.approx(0.06)
    assert await slice_.deduct(0.04) == pytest.approx(0.02)
    with pytest.raises(BudgetExhausted):
        await slice_.deduct(0.04)


def test_negative_initial_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetSlice(-0.5)


def test_sync_deduct_matches_async() -> None:
    slice_ = BudgetSlice(0.50)
    assert slice_.deduct_sync(0.10) == pytest.approx(0.40)
    assert slice_.remaining == pytest.approx(0.40)
    with pytest.raises(BudgetExhausted):
        slice_.deduct_sync(0.50)
