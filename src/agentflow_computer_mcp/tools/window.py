"""Window listing + focus — delegates to the platform backend.

The async ``focus`` signature is preserved for MCP handler compatibility.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..platform import backend


def list_windows() -> list[dict[str, Any]]:
    if backend is None:
        return []
    return backend.window_list()


async def focus(title: str) -> dict[str, Any]:
    if backend is None:
        return {"ok": False, "error": "no platform backend"}
    return await asyncio.to_thread(backend.window_focus, title)
