from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import AppConfig, load_config
from .confirm import confirm, confirm_summary
from .driver.tools.project_setup import project_clone_and_setup
from .scope import requires_confirm
from .tools import clipboard as clipboard_tool
from .tools import code as code_tool
from .tools import fs as fs_tool
from .tools import keyboard as keyboard_tool
from .tools import mouse as mouse_tool
from .tools import screen as screen_tool
from .tools import screen_record as screen_record_tool
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
    "computer.code.read_file",
    "computer.code.write_file",
    "computer.code.edit_file",
    "computer.code.run_command",
    "computer.code.list_dir",
    "computer.screen_record.start",
    "computer.screen_record.stop",
    "computer.screen_record.status",
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

    @mcp.tool(name="computer.code.read_file")
    def code_read_file(path: str, max_lines: int = 2000) -> dict[str, Any]:
        return code_tool.read_file(path, scope=config.scope, max_lines=max_lines)

    @mcp.tool(name="computer.code.write_file")
    async def code_write_file(
        path: str, content: str, mode: str = "replace"
    ) -> dict[str, Any]:
        if requires_confirm("computer.fs.write", config.scope):
            summary = confirm_summary("code.write_file", {"path": path, "mode": mode})
            if not await confirm("computer.code.write_file", summary):
                raise PermissionError("user denied code.write_file")
        return code_tool.write_file(path, content, scope=config.scope, mode=mode)

    @mcp.tool(name="computer.code.edit_file")
    async def code_edit_file(
        path: str, find: str, replace: str, count: int | str = 1
    ) -> dict[str, Any]:
        if requires_confirm("computer.fs.write", config.scope):
            summary = confirm_summary("code.edit_file", {"path": path, "count": count})
            if not await confirm("computer.code.edit_file", summary):
                raise PermissionError("user denied code.edit_file")
        return code_tool.edit_file(path, find, replace, scope=config.scope, count=count)

    @mcp.tool(name="computer.code.run_command")
    async def code_run_command(
        command: str, cwd: str | None = None, timeout: int = 120
    ) -> dict[str, Any]:
        if requires_confirm("computer.shell.exec", config.scope):
            summary = confirm_summary("code.run_command", {"cwd": cwd or "~", "command": command})
            if not await confirm("computer.code.run_command", summary):
                raise PermissionError("user denied code.run_command")
        return await code_tool.run_command(command, scope=config.scope, cwd=cwd, timeout=timeout)

    @mcp.tool(name="computer.code.list_dir")
    def code_list_dir(
        path: str,
        depth: int = 1,
        ignore_globs: list[str] | None = None,
    ) -> dict[str, Any]:
        return code_tool.list_dir(
            path, scope=config.scope, depth=depth, ignore_globs=ignore_globs
        )

    @mcp.tool(name="computer.screen_record.start")
    def screen_record_start(
        path: str,
        fps: int = 10,
        width_cap: int = 1280,
        max_duration_s: int = 120,
    ) -> dict[str, Any]:
        return screen_record_tool.get_recorder().start(
            path, fps=fps, width_cap=width_cap, max_duration_s=max_duration_s
        )

    @mcp.tool(name="computer.screen_record.stop")
    def screen_record_stop() -> dict[str, Any]:
        return screen_record_tool.get_recorder().stop()

    @mcp.tool(name="computer.screen_record.status")
    def screen_record_status() -> dict[str, Any]:
        return screen_record_tool.get_recorder().status()

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
    if name == "computer.code.read_file":
        return code_tool.read_file(
            args["path"], scope=config.scope, max_lines=int(args.get("max_lines", 2000))
        )
    if name == "computer.code.write_file":
        if requires_confirm("computer.fs.write", config.scope):
            summary = confirm_summary(
                "code.write_file",
                {"path": args["path"], "mode": args.get("mode", "replace")},
            )
            if not await confirm("computer.code.write_file", summary):
                raise PermissionError("user denied code.write_file")
        return code_tool.write_file(
            args["path"], args["content"], scope=config.scope, mode=args.get("mode", "replace")
        )
    if name == "computer.code.edit_file":
        if requires_confirm("computer.fs.write", config.scope):
            summary = confirm_summary(
                "code.edit_file",
                {"path": args["path"], "count": args.get("count", 1)},
            )
            if not await confirm("computer.code.edit_file", summary):
                raise PermissionError("user denied code.edit_file")
        return code_tool.edit_file(
            args["path"],
            args["find"],
            args["replace"],
            scope=config.scope,
            count=args.get("count", 1),
        )
    if name == "computer.code.run_command":
        if requires_confirm("computer.shell.exec", config.scope):
            summary = confirm_summary(
                "code.run_command",
                {"cwd": args.get("cwd") or "~", "command": args["command"]},
            )
            if not await confirm("computer.code.run_command", summary):
                raise PermissionError("user denied code.run_command")
        return await code_tool.run_command(
            args["command"],
            scope=config.scope,
            cwd=args.get("cwd"),
            timeout=int(args.get("timeout", 120)),
        )
    if name == "computer.code.list_dir":
        return code_tool.list_dir(
            args["path"],
            scope=config.scope,
            depth=int(args.get("depth", 1)),
            ignore_globs=args.get("ignore_globs"),
        )
    if name == "computer.screen_record.start":
        return screen_record_tool.get_recorder().start(
            args["path"],
            fps=int(args.get("fps", 10)),
            width_cap=int(args.get("width_cap", 1280)),
            max_duration_s=int(args.get("max_duration_s", 120)),
        )
    if name == "computer.screen_record.stop":
        return screen_record_tool.get_recorder().stop()
    if name == "computer.screen_record.status":
        return screen_record_tool.get_recorder().status()
    # Backend RPC-style tools — dispatched from the WS task_dispatch handler
    # with `tool=…`, never invoked by the LLM agent. Kept separate from the
    # `computer.*` LLM-visible surface to avoid prompting the model with
    # internal platform calls.
    if name == "project_clone_and_setup":
        api_base = os.environ.get("AF_API_URL", "https://agentflow.website").rstrip("/")
        if not api_base.endswith("/_agents"):
            api_base = api_base + "/_agents"
        internal_secret = os.environ.get("AF_INTERNAL_API_SECRET", "") or os.environ.get(
            "AF_INTERNAL_SECRET", ""
        )
        return project_clone_and_setup(
            template_repo_full=str(args.get("template_repo_full", "")),
            slug=str(args.get("slug", "")),
            project_id=int(args.get("project_id", 0) or 0),
            api_base=api_base,
            internal_secret=internal_secret,
            port=int(args.get("port", 3000) or 3000),
        )
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
