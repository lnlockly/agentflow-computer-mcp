from __future__ import annotations

import asyncio
from typing import Any

try:
    import Quartz
    _HAS_QUARTZ = True
except ImportError:
    _HAS_QUARTZ = False


def list_windows() -> list[dict[str, Any]]:
    if not _HAS_QUARTZ:
        return []

    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    result: list[dict[str, Any]] = []
    for w in window_list:
        owner = w.get("kCGWindowOwnerName", "")
        title = w.get("kCGWindowName", "")
        bounds = w.get("kCGWindowBounds", {})
        result.append({
            "owner": owner,
            "title": title,
            "pid": int(w.get("kCGWindowOwnerPID", 0)),
            "window_id": int(w.get("kCGWindowNumber", 0)),
            "bounds": {
                "x": int(bounds.get("X", 0)),
                "y": int(bounds.get("Y", 0)),
                "width": int(bounds.get("Width", 0)),
                "height": int(bounds.get("Height", 0)),
            },
        })
    return result


async def focus(title: str) -> dict[str, Any]:
    safe = title.replace('"', "'")
    script = f'tell application "{safe}" to activate'
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"ok": False, "error": stderr.decode("utf-8", errors="replace")}
    return {"ok": True, "focused": title}
