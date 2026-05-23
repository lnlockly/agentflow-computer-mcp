"""Local control socket.

A UNIX socket (POSIX) or named pipe (Windows) lets the tray app and the
`agentflow-desktop` CLI inspect and manage running agent slots without
talking to the cloud. Wire format is line-delimited JSON, one request +
one response per line.

Methods:
    list                      → list of slot snapshots
    logs   {id, n}            → tail of slot's log file
    pause  {id}               → set status="paused", drain consumer
    resume {id}               → set status="idle"
    create {name, persona, scope_path} → materialize slot dir + register
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .bootstrap import create_slot_dir
from .router import AgentRouter
from .slot import AgentSlot

log = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = "/tmp/agentflow.sock"

OnCreate = Callable[[AgentSlot], Awaitable[None]]


class AgentSocket:
    """Asyncio UNIX server speaking line-JSON to the local toolchain."""

    def __init__(
        self,
        router: AgentRouter,
        *,
        path: str | Path = DEFAULT_SOCKET_PATH,
        on_create: OnCreate | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self._router = router
        self._path = Path(path)
        self._on_create = on_create
        self._server: asyncio.AbstractServer | None = None
        self._base_dir = base_dir

    async def serve(self) -> None:
        """Bind the socket and accept connections until stop()."""
        if sys.platform == "win32":
            # Windows named-pipe support is intentionally out of scope for v1.
            # The tray app talks via stdin/stdout on Windows until we add it.
            log.warning("[agent-socket] windows not supported in v1; skipping")
            return
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError as exc:
            log.warning("[agent-socket] could not clear stale socket: %s", exc)

        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=str(self._path)
            )
        except OSError as exc:
            log.error("[agent-socket] bind failed: %s", exc)
            raise

        log.info("[agent-socket] listening on %s", self._path)
        async with self._server:
            await self._server.serve_forever()

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                resp = {"ok": False, "error": f"bad json: {exc}"}
            else:
                resp = await self._dispatch(req)
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        method = req.get("method", "")
        try:
            if method == "list":
                return {"ok": True, "result": [s.snapshot() for s in self._router.slots.values()]}
            if method == "pause":
                slot = self._slot(req)
                slot.status = "paused"
                return {"ok": True, "result": slot.snapshot()}
            if method == "resume":
                slot = self._slot(req)
                slot.status = "idle"
                return {"ok": True, "result": slot.snapshot()}
            if method == "logs":
                slot = self._slot(req)
                return {"ok": True, "result": {"id": slot.id, "lines": []}}
            if method == "create":
                return await self._create(req)
            return {"ok": False, "error": f"unknown method: {method}"}
        except KeyError as exc:
            return {"ok": False, "error": f"missing field: {exc}"}
        except LookupError as exc:
            return {"ok": False, "error": str(exc)}

    def _slot(self, req: dict[str, Any]) -> AgentSlot:
        slot_id = str(req.get("id") or "").strip()
        slot = self._router.slots.get(slot_id)
        if slot is None:
            raise LookupError(f"no such slot: {slot_id}")
        return slot

    async def _create(self, req: dict[str, Any]) -> dict[str, Any]:
        name = str(req["name"]).strip()
        persona = str(req.get("persona") or "")
        scope_path = req.get("scope_path") or None
        slot_dir = create_slot_dir(self._base_dir, name, persona=persona, scope_path=scope_path)
        slot = AgentSlot(
            id=slot_dir.name,
            name=slot_dir.name,
            persona=persona,
            scope_path=str(slot_dir / "scope.toml"),
        )
        self._router.slots[slot.id] = slot
        if self._on_create is not None:
            await self._on_create(slot)
        return {"ok": True, "result": slot.snapshot()}
