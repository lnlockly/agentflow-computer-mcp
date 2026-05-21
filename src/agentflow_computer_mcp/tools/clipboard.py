from __future__ import annotations

import asyncio


async def read() -> dict[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "pbpaste",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return {"text": stdout.decode("utf-8", errors="replace")}


async def write(text: str) -> dict[str, int]:
    proc = await asyncio.create_subprocess_exec(
        "pbcopy",
        stdin=asyncio.subprocess.PIPE,
    )
    await proc.communicate(text.encode("utf-8"))
    return {"length": len(text)}
