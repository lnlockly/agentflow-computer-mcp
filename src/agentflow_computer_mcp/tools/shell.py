from __future__ import annotations

import asyncio
from typing import Any

from ..config import Scope
from ..scope import check_shell

MAX_OUTPUT_BYTES = 200_000


async def exec_cmd(cmd: str, scope: Scope, timeout_s: int = 30) -> dict[str, Any]:
    check_shell(cmd, scope)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "truncated": len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES,
    }
