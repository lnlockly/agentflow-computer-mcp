"""Probe the local daemon socket for agent list + liveness.

Wraps `cli.socket_client.call("list")` so the tray gets a clean tuple
of `AgentRow` plus a `DaemonStatus` literal. All exceptions become
`("down", ())`; the Windows-only-platform `DaemonUnavailable` (raised by
`socket_client._send` for `sys.platform == "win32"`) becomes
`("unsupported", ())`.
"""
from __future__ import annotations

import sys
from typing import Any

from ..cli import socket_client
from .state import AgentRow, DaemonStatus


def _coerce_agents(raw: Any) -> tuple[AgentRow, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[AgentRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            AgentRow(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                status=str(item.get("status", "")),
            )
        )
    return tuple(out)


def probe(socket_path: str | None = None) -> tuple[DaemonStatus, tuple[AgentRow, ...]]:
    """Return (status, agents). Never raises."""
    kwargs: dict[str, Any] = {}
    if socket_path is not None:
        kwargs["path"] = socket_path
    try:
        raw = socket_client.call("list", **kwargs)
    except socket_client.DaemonUnavailable as exc:
        # The UNIX-only short-circuit raises this with a message mentioning
        # "Windows" — promote to the dedicated "unsupported" status so the
        # menu can show the right copy.
        if sys.platform == "win32" or "Windows" in str(exc):
            return "unsupported", ()
        return "down", ()
    except (socket_client.DaemonError, OSError, RuntimeError):
        return "down", ()
    return "up", _coerce_agents(raw)
