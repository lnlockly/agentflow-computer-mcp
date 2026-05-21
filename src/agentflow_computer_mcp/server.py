from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import AppConfig, load_config
from .confirm import confirm, confirm_summary
from .scope import requires_confirm
from .tools import clipboard as clipboard_tool
from .tools import fs as fs_tool
from .tools import keyboard as keyboard_tool
from .tools import mouse as mouse_tool
from .tools import screen as screen_tool
from .tools import shell as shell_tool
from .tools import window as window_tool

log = logging.getLogger(__name__)


TOOL_NAMES: list[str] = [
    "computer.screen.capture",
    "computer.mouse.click",
    "computer.mouse.move",
    "computer.mouse.scroll",
    "computer.keyboard.type",
    "computer.keyboard.key",
    "computer.keyboard.shortcut",
    "computer.window.list",
    "computer.window.focus",
    "computer.fs.read",
    "computer.fs.list",
    "computer.fs.write",
    "computer.shell.exec",
    "computer.clipboard.read",
    "computer.clipboard.write",
]


def build_mcp(config: AppConfig) -> FastMCP:
    mcp = FastMCP("agentflow-computer-mcp")

    @mcp.tool(name="computer.screen.capture")
    def screen_capture(region: dict[str, int] | None = None) -> dict[str, Any]:
        return screen_tool.capture_base64(region)

    @mcp.tool(name="computer.mouse.click")
    def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, int]:
        return mouse_tool.click(x, y, button=button, clicks=clicks)  # type: ignore[arg-type]

    @mcp.tool(name="computer.mouse.move")
    def mouse_move(x: int, y: int, duration: float = 0.0) -> dict[str, int]:
        return mouse_tool.move(x, y, duration=duration)

    @mcp.tool(name="computer.mouse.scroll")
    def mouse_scroll(dx: int = 0, dy: int = 0) -> dict[str, int]:
        return mouse_tool.scroll(dx, dy)

    @mcp.tool(name="computer.keyboard.type")
    def keyboard_type(text: str, interval: float = 0.0) -> dict[str, int]:
        return keyboard_tool.type_text(text, interval=interval)

    @mcp.tool(name="computer.keyboard.key")
    def keyboard_key(name: str) -> dict[str, str]:
        return keyboard_tool.key(name)

    @mcp.tool(name="computer.keyboard.shortcut")
    def keyboard_shortcut(combo: str) -> dict[str, str]:
        return keyboard_tool.shortcut(combo)

    @mcp.tool(name="computer.window.list")
    def window_list() -> list[dict[str, Any]]:
        return window_tool.list_windows()

    @mcp.tool(name="computer.window.focus")
    async def window_focus(title: str) -> dict[str, Any]:
        return await window_tool.focus(title)

    @mcp.tool(name="computer.fs.read")
    def fs_read(path: str) -> dict[str, Any]:
        return fs_tool.read(path, config.scope)

    @mcp.tool(name="computer.fs.list")
    def fs_list(path: str) -> dict[str, Any]:
        return fs_tool.list_dir(path, config.scope)

    @mcp.tool(name="computer.fs.write")
    async def fs_write(path: str, content: str, encoding: str = "utf-8") -> dict[str, Any]:
        if requires_confirm("computer.fs.write", config.scope):
            ok = await confirm("computer.fs.write", confirm_summary("fs.write", {"path": path}))
            if not ok:
                raise PermissionError("user denied fs.write")
        return fs_tool.write(path, content, config.scope, encoding=encoding)

    @mcp.tool(name="computer.shell.exec")
    async def shell_exec(cmd: str, timeout_s: int = 30) -> dict[str, Any]:
        if requires_confirm("computer.shell.exec", config.scope):
            ok = await confirm("computer.shell.exec", confirm_summary("shell.exec", {"cmd": cmd}))
            if not ok:
                raise PermissionError("user denied shell.exec")
        return await shell_tool.exec_cmd(cmd, config.scope, timeout_s=timeout_s)

    @mcp.tool(name="computer.clipboard.read")
    async def clipboard_read() -> dict[str, str]:
        return await clipboard_tool.read()

    @mcp.tool(name="computer.clipboard.write")
    async def clipboard_write(text: str) -> dict[str, int]:
        return await clipboard_tool.write(text)

    return mcp


async def _dispatch_tool(name: str, args: dict[str, Any], config: AppConfig) -> Any:
    """Direct tool dispatcher used by the WS reverse-tunnel client."""
    if name == "computer.screen.capture":
        return screen_tool.capture_base64(args.get("region"))
    if name == "computer.mouse.click":
        return mouse_tool.click(
            args["x"], args["y"], args.get("button", "left"), args.get("clicks", 1)
        )
    if name == "computer.mouse.move":
        return mouse_tool.move(args["x"], args["y"], args.get("duration", 0.0))
    if name == "computer.mouse.scroll":
        return mouse_tool.scroll(args.get("dx", 0), args.get("dy", 0))
    if name == "computer.keyboard.type":
        return keyboard_tool.type_text(args["text"], args.get("interval", 0.0))
    if name == "computer.keyboard.key":
        return keyboard_tool.key(args["name"])
    if name == "computer.keyboard.shortcut":
        return keyboard_tool.shortcut(args["combo"])
    if name == "computer.window.list":
        return window_tool.list_windows()
    if name == "computer.window.focus":
        return await window_tool.focus(args["title"])
    if name == "computer.fs.read":
        return fs_tool.read(args["path"], config.scope)
    if name == "computer.fs.list":
        return fs_tool.list_dir(args["path"], config.scope)
    if name == "computer.fs.write":
        if requires_confirm("computer.fs.write", config.scope):
            summary = confirm_summary("fs.write", {"path": args["path"]})
            if not await confirm("computer.fs.write", summary):
                raise PermissionError("user denied fs.write")
        return fs_tool.write(
            args["path"], args["content"], config.scope, args.get("encoding", "utf-8")
        )
    if name == "computer.shell.exec":
        if requires_confirm("computer.shell.exec", config.scope):
            summary = confirm_summary("shell.exec", {"cmd": args["cmd"]})
            if not await confirm("computer.shell.exec", summary):
                raise PermissionError("user denied shell.exec")
        return await shell_tool.exec_cmd(args["cmd"], config.scope, args.get("timeout_s", 30))
    if name == "computer.clipboard.read":
        return await clipboard_tool.read()
    if name == "computer.clipboard.write":
        return await clipboard_tool.write(args["text"])
    raise LookupError(f"unknown tool: {name}")


async def run(mode: str = "stdio") -> None:
    config = load_config()

    if mode == "ws":
        from .ws_client import WSClient

        async def handler(name: str, args: dict[str, Any]) -> Any:
            return await _dispatch_tool(name, args, config)

        client = WSClient(config, handler, TOOL_NAMES)
        await client.run()
        return

    mcp = build_mcp(config)
    await mcp.run_stdio_async()
