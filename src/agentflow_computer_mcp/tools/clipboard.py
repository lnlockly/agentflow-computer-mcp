"""Clipboard — async wrapper over the platform backend.

The async signatures are preserved so MCP handlers and ``await`` callers don't break.
The backend method is sync (subprocess calls). For long-running shells this is fine;
we keep the awaitable surface for API stability.
"""
from __future__ import annotations

import asyncio

from ..platform import backend


async def read() -> dict[str, str]:
    if backend is None:
        return {"text": ""}
    text = await asyncio.to_thread(backend.clipboard_read)
    return {"text": text}


async def write(text: str) -> dict[str, int]:
    if backend is None:
        return {"length": 0}
    await asyncio.to_thread(backend.clipboard_write, text)
    return {"length": len(text)}
