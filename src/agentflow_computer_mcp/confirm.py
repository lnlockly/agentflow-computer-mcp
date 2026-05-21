from __future__ import annotations

import asyncio
import logging
import shlex

log = logging.getLogger(__name__)


async def confirm(tool_name: str, summary: str, timeout_s: int = 30) -> bool:
    """Show native macOS confirm dialog. Returns True if user clicked Allow."""
    title = f"AgentFlow: {tool_name}"
    safe_summary = summary.replace('"', "'")[:400]
    script = (
        f'display dialog "{safe_summary}" '
        f'with title "{title}" '
        f'buttons {{"Deny","Allow"}} '
        f'default button "Deny" '
        f'with icon caution '
        f'giving up after {timeout_s}'
    )

    cmd = ["osascript", "-e", script]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 5)
    except (TimeoutError, FileNotFoundError) as exc:
        log.warning("confirm dialog failed: %s", exc)
        return False

    out = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        log.info("confirm denied (rc=%s): %s", proc.returncode, stderr_text)
        return False

    return "button returned:Allow" in out


def confirm_summary(tool_name: str, args: dict) -> str:
    parts = [f"{k}={shlex.quote(str(v))[:80]}" for k, v in args.items()]
    return f"{tool_name}({', '.join(parts)})"
