"""Thin client for the daemon's UNIX socket (`agents/socket.py`).

Sends one line-JSON request, reads one line-JSON response. Sync wrapper
around asyncio.open_unix_connection because the CLI is otherwise sync.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_SOCKET_PATH = "/tmp/agentflow.sock"


class DaemonUnavailable(RuntimeError):
    """Raised when the local daemon socket is missing or refuses connections."""


class DaemonError(RuntimeError):
    """Raised when the daemon returns `{ok: false}`."""


async def _send(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if sys.platform == "win32":
        raise DaemonUnavailable("Windows: локальный socket пока не поддерживается (см. #94)")
    if not Path(path).exists():
        raise DaemonUnavailable(f"socket not found: {path}")
    try:
        reader, writer = await asyncio.open_unix_connection(path=path)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        raise DaemonUnavailable(f"cannot connect: {exc}") from exc
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if not line:
        raise DaemonError("empty response from daemon")
    resp = json.loads(line.decode("utf-8"))
    if not resp.get("ok"):
        raise DaemonError(str(resp.get("error") or "unknown daemon error"))
    return resp.get("result")


def call(method: str, *, path: str = DEFAULT_SOCKET_PATH, **kwargs: Any) -> Any:
    """Synchronous one-shot request to the daemon socket."""
    payload: dict[str, Any] = {"method": method}
    payload.update(kwargs)
    return asyncio.run(_send(path, payload))
