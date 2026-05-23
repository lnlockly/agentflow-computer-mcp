"""Runtime glue: start the multi-agent router + control socket from
`desktop_cli.cmd_run` without forcing every existing test to learn about
the new module.

Returns a small handle the caller stops on shutdown. Returns None when
`AGENTFLOW_MULTI_AGENT` is not set (legacy single-slot stays in charge).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from .bootstrap import discover_slots, is_multi_agent_enabled
from .router import AgentRouter
from .slot import AgentSlot
from .socket import DEFAULT_SOCKET_PATH, AgentSocket

log = logging.getLogger(__name__)


@dataclass
class RuntimeHandle:
    router: AgentRouter
    socket: AgentSocket
    thread: threading.Thread
    loop: asyncio.AbstractEventLoop
    serve_task: asyncio.Task[None] | None = None

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.socket.stop()

        async def _shutdown() -> None:
            if self.serve_task is not None:
                self.serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.serve_task
            await self.router.stop()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_shutdown(), self.loop).result(timeout=5)
        with contextlib.suppress(Exception):
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


async def _placeholder_handler(slot: AgentSlot, frame: dict[str, Any]) -> None:
    """v1 placeholder.

    Until the server tags frames with `agent_id`, the legacy DriverState
    path handles everything. This handler logs that a multi-agent frame
    arrived so we can verify routing in prod before flipping the cutover.
    """
    log.info(
        "[multi-agent] slot=%s received task id=%s task=%r",
        slot.id,
        frame.get("id"),
        str(frame.get("task", ""))[:80],
    )


def maybe_start_runtime() -> RuntimeHandle | None:
    """Spin up the router + socket in a sidecar thread when env-enabled.

    The sidecar owns its own asyncio loop so we don't perturb the legacy
    blocking `task_worker` running on the main thread.
    """
    if not is_multi_agent_enabled():
        return None
    slots = discover_slots()
    if not slots:
        return None

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_thread, name="multi-agent-runtime", daemon=True)
    thread.start()

    sock_path = os.environ.get("AGENTFLOW_AGENT_SOCKET") or DEFAULT_SOCKET_PATH
    router = AgentRouter(slots, _placeholder_handler)
    sock = AgentSocket(router, path=sock_path)
    serve_task_holder: dict[str, asyncio.Task[None]] = {}

    async def _bring_up() -> None:
        await router.start()
        # Serve the control socket on the same loop. asyncio.start_unix_server
        # blocks, so we put it on its own task.
        serve_task_holder["t"] = asyncio.create_task(
            sock.serve(), name="agent-socket-serve"
        )
        ready.set()

    asyncio.run_coroutine_threadsafe(_bring_up(), loop)
    if not ready.wait(timeout=5):
        log.warning("multi-agent runtime did not become ready in 5s")

    log.info(
        "[multi-agent] runtime up: %d slots (%s)",
        len(slots),
        ",".join(s.id for s in slots),
    )
    return RuntimeHandle(
        router=router,
        socket=sock,
        thread=thread,
        loop=loop,
        serve_task=serve_task_holder.get("t"),
    )
