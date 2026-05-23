"""AgentSlot dataclass defaults + snapshot shape."""
from __future__ import annotations

import asyncio

from agentflow_computer_mcp.agents import AgentSlot


def test_slot_defaults() -> None:
    slot = AgentSlot(id="trader")
    assert slot.status == "idle"
    assert slot.budget_remaining_usd == 2.0
    assert isinstance(slot.queue, asyncio.Queue)
    assert slot.browser_context is None


def test_slot_snapshot_has_expected_keys() -> None:
    slot = AgentSlot(id="trader", name="Trader", budget_remaining_usd=1.5)
    snap = slot.snapshot()
    assert snap["id"] == "trader"
    assert snap["name"] == "Trader"
    assert snap["status"] == "idle"
    assert snap["budget_remaining_usd"] == 1.5
    assert snap["queue_depth"] == 0
