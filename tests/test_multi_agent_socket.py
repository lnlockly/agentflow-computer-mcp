"""Control socket: list + create + pause + resume.

POSIX-only (UNIX socket). On Windows, `AgentSocket.serve` is a no-op and
these tests are skipped.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from agentflow_computer_mcp.agents import AgentRouter, AgentSlot
from agentflow_computer_mcp.agents.socket import AgentSocket

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UNIX socket only")


def _short_socket_path() -> Path:
    """AF_UNIX caps at ~104 chars on Mac; pytest tmpdir blows past that.

    Use a short /tmp filename to stay safely under the limit.
    """
    tmp = Path(tempfile.gettempdir())
    return tmp / f"af-{uuid.uuid4().hex[:8]}.sock"


async def _no_op_handler(_slot: AgentSlot, _frame: dict) -> None:
    return None


async def _send_line(path: Path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return json.loads(line.decode("utf-8"))


async def _start_socket(
    tmp_path: Path, slots: list[AgentSlot]
) -> tuple[AgentSocket, AgentRouter, Path, asyncio.Task]:
    router = AgentRouter(slots, _no_op_handler)
    sock_path = _short_socket_path()
    sock = AgentSocket(router, path=sock_path, base_dir=tmp_path)
    task = asyncio.create_task(sock.serve())
    # Wait for the socket file to appear so the test client never races.
    for _ in range(50):
        if sock_path.exists():
            break
        await asyncio.sleep(0.02)
    assert sock_path.exists(), "socket did not bind"
    return sock, router, sock_path, task


async def test_list_returns_slot_ids(tmp_path: Path) -> None:
    sock, _, path, task = await _start_socket(
        tmp_path, [AgentSlot(id="default"), AgentSlot(id="trader")]
    )
    try:
        resp = await _send_line(path, {"method": "list"})
        assert resp["ok"] is True
        ids = sorted(item["id"] for item in resp["result"])
        assert ids == ["default", "trader"]
    finally:
        sock.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_unknown_method_returns_error(tmp_path: Path) -> None:
    sock, _, path, task = await _start_socket(tmp_path, [AgentSlot(id="default")])
    try:
        resp = await _send_line(path, {"method": "do-the-thing"})
        assert resp["ok"] is False
        assert "unknown" in resp["error"]
    finally:
        sock.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_create_persists_dir_and_registers(tmp_path: Path) -> None:
    sock, router, path, task = await _start_socket(tmp_path, [AgentSlot(id="default")])
    try:
        resp = await _send_line(
            path,
            {"method": "create", "name": "trader", "persona": "trade safely"},
        )
        assert resp["ok"] is True
        assert resp["result"]["id"] == "trader"
        assert "trader" in router.slots
        slot_dir = tmp_path / "agents" / "trader"
        assert slot_dir.exists()
        assert (slot_dir / "persona.txt").read_text(encoding="utf-8") == "trade safely"
    finally:
        sock.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_pause_then_resume_toggles_status(tmp_path: Path) -> None:
    sock, _, path, task = await _start_socket(tmp_path, [AgentSlot(id="default")])
    try:
        resp = await _send_line(path, {"method": "pause", "id": "default"})
        assert resp["ok"] is True
        assert resp["result"]["status"] == "paused"
        resp = await _send_line(path, {"method": "resume", "id": "default"})
        assert resp["ok"] is True
        assert resp["result"]["status"] == "idle"
    finally:
        sock.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
