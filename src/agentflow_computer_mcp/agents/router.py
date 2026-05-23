"""AgentRouter — fan-out for ws `task_dispatch` frames.

Holds `dict[slot_id, AgentSlot]`. `dispatch(frame)` looks at
`frame["agent_id"]` and pushes the payload into the matching slot's
queue. Unknown ids fall back to the `default` slot so legacy clients
still work. Each slot has a long-running consumer task that pops from
its queue, runs the handler, and survives handler exceptions.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .slot import AgentSlot

log = logging.getLogger(__name__)

TaskHandler = Callable[[AgentSlot, dict[str, Any]], Awaitable[Any]]


class AgentRouter:
    """Route ws frames into per-slot queues; manage consumer lifecycle."""

    DEFAULT_SLOT_ID = "default"

    def __init__(self, slots: list[AgentSlot], handler: TaskHandler) -> None:
        if not slots:
            raise ValueError("router needs at least one slot")
        self.slots: dict[str, AgentSlot] = {s.id: s for s in slots}
        self._handler = handler
        self._consumers: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    @property
    def slot_ids(self) -> list[str]:
        return list(self.slots.keys())

    def dispatch(self, frame: dict[str, Any]) -> str:
        """Push `frame` into the right slot's queue. Returns the resolved slot id.

        If `frame["agent_id"]` is missing or unknown we fall back to the
        `default` slot (or the first slot if no `default` is registered).
        """
        agent_id = str(frame.get("agent_id") or "").strip()
        slot = self.slots.get(agent_id)
        if slot is None:
            fallback = self.slots.get(self.DEFAULT_SLOT_ID) or next(iter(self.slots.values()))
            if agent_id:
                log.warning(
                    "[agent-router] unknown agent_id=%r, falling back to %s", agent_id, fallback.id
                )
            slot = fallback
        slot.queue.put_nowait(frame)
        return slot.id

    async def start(self) -> None:
        """Spawn a consumer task per slot. Safe to call once at boot."""
        for slot_id, slot in self.slots.items():
            if slot_id in self._consumers:
                continue
            self._consumers[slot_id] = asyncio.create_task(
                self._consume(slot), name=f"agent-consumer-{slot_id}"
            )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._consumers.values():
            task.cancel()
        # Wait for cancellation to settle without surfacing CancelledError.
        for task in self._consumers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._consumers.clear()

    async def _consume(self, slot: AgentSlot) -> None:
        """Long-running per-slot loop.

        Pops one frame at a time and runs the handler. Handler exceptions
        flip the slot to `crashed` but never kill the consumer — the next
        frame still runs. CancelledError exits the loop.
        """
        while not self._stop.is_set():
            try:
                frame = await slot.queue.get()
            except asyncio.CancelledError:
                return
            slot.status = "running"
            slot.last_action_at = time.time()
            try:
                await self._handler(slot, frame)
                # If the handler did not flip status to crashed/paused, go idle.
                if slot.status == "running":
                    slot.status = "idle"
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("[agent-router] slot %s crashed on task: %s", slot.id, exc)
                slot.status = "crashed"
                slot.last_error = str(exc)
            finally:
                # Defensive: always mark the queue item as done so joiners unblock.
                with contextlib.suppress(ValueError):
                    slot.queue.task_done()
            # Brief await to let other coroutines run between tasks.
            await asyncio.sleep(0)
