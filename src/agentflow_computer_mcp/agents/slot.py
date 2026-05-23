"""AgentSlot — per-agent state container.

A slot is the runtime handle the router uses to enqueue tasks for one agent.
Persona/scope come from `~/.agentflow/agents/<id>/`. Browser context is
attached lazily when the first task runs (cheap when the agent is idle).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

SlotStatus = Literal["idle", "running", "paused", "crashed", "stopped"]


@dataclass
class AgentSlot:
    """Runtime state for one agent.

    `queue` carries `task_dispatch` payloads (dicts with id/task/scope).
    `browser_context` is the Playwright object once attached; tests pass
    a mock here. Status transitions: idle → running → idle (happy path),
    or running → crashed → idle (after the consumer recovers).
    """

    id: str
    name: str = ""
    persona: str = ""
    scope_path: str = ""
    status: SlotStatus = "idle"
    budget_remaining_usd: float = 2.0
    last_action_at: float = 0.0
    browser_context: Any = None
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    last_error: str = ""

    def snapshot(self) -> dict[str, Any]:
        """Serializable view for control-socket `list` responses."""
        return {
            "id": self.id,
            "name": self.name or self.id,
            "status": self.status,
            "budget_remaining_usd": round(self.budget_remaining_usd, 4),
            "queue_depth": self.queue.qsize(),
            "last_action_at": self.last_action_at,
            "last_error": self.last_error,
        }
