"""LLM-facing tool catalog + executor. Wraps:
- screen / mouse / keyboard / window / clipboard from ``agentflow_computer_mcp.tools``
- macOS AppleScript bridges to iTerm + Google Chrome
- Playwright headed Chromium (lazy)
- AgentFlow API client (``af_*`` tools)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
import threading
import time
from typing import Any

from PIL import Image

from ..config import Scope, load_scope
from ..confirm import confirm, confirm_summary
from ..platform import PLATFORM, backend
from ..scope import requires_confirm
from ..tools import clipboard, keyboard, mouse, screen, window
from ..tools import code as code_tool
from .af_client import AF_TOOL_DESCRIPTORS, AFClient, dispatch_af_tool

NOISY_OWNERS: frozenset[str] = frozenset(
    {
        "Window Server",
        "Пункт управления",
        "Focus",
        "TextInputMenuAgent",
        "SystemUIServer",
        "Spotlight",
        "Dock",
    }
)

CAPTURE_LOCK = threading.Lock()


def grab_full_png() -> bytes:
    with CAPTURE_LOCK:
        return screen.capture()


def jpeg_b64_full(quality: int = 70, width_cap: int = 1280) -> str:
    png = grab_full_png()
    img = Image.open(io.BytesIO(png))
    if img.width > width_cap:
        ratio = width_cap / img.width
        img = img.resize((width_cap, int(img.height * ratio)), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality)
    return base64.b64encode(out.getvalue()).decode()


def jpeg_b64_region(x: int, y: int, w: int, h: int, quality: int = 85) -> str:
    png = grab_full_png()
    img = Image.open(io.BytesIO(png))
    crop = img.crop(
        (max(0, x), max(0, y), min(img.width, x + w), min(img.height, y + h))
    )
    out = io.BytesIO()
    crop.convert("RGB").save(out, format="JPEG", quality=quality)
    return base64.b64encode(out.getvalue()).decode()


def osa(script: str, timeout: int = 8) -> tuple[int, str]:
    if PLATFORM != "mac":
        return -1, f"osascript unavailable on {PLATFORM}"
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout or r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "osascript timeout"


def app_activate(owner: str) -> str:
    if backend is None:
        return "failed: no platform backend"
    out = backend.app_activate(owner)
    time.sleep(0.3)
    return out


def chrome_run_js(js: str, tab_index: int | None = None) -> str:
    js_esc = js.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    if tab_index is None:
        script = f'tell application "Google Chrome" to tell active tab of front window to execute javascript "{js_esc}"'
    else:
        script = f'tell application "Google Chrome" to tell tab {tab_index} of front window to execute javascript "{js_esc}"'
    rc, out = osa(script, timeout=20)
    return out if rc == 0 else f"error: {out}"


def chrome_open_url(url: str, new_tab: bool = True) -> str:
    if new_tab:
        rc, out = osa(
            f'tell application "Google Chrome" to tell front window to make new tab with properties {{URL:"{url}"}}',
            timeout=10,
        )
    else:
        rc, out = osa(
            f'tell application "Google Chrome" to set URL of active tab of front window to "{url}"',
            timeout=10,
        )
    return f"opened {url}" if rc == 0 else f"error: {out}"


def chrome_list_tabs() -> str:
    rc, out = osa(
        'tell application "Google Chrome" to get {URL, title} of tabs of front window',
        timeout=10,
    )
    return out


def read_iterm_session() -> str:
    if backend is None:
        return ""
    text = backend.read_terminal()
    if text:
        return text
    return f"error: no terminal contents available on {PLATFORM}"


def get_window_list() -> list[dict[str, Any]]:
    wins = window.list_windows()
    compact: list[dict[str, Any]] = []
    for w in wins:
        owner = w.get("owner") or ""
        if owner in NOISY_OWNERS:
            continue
        b = w.get("bounds") or {}
        if (b.get("width") or 0) < 60 or (b.get("height") or 0) < 60:
            continue
        compact.append(
            {
                "owner": owner,
                "title": w.get("title") or "",
                "window_id": w.get("window_id"),
                "bounds": b,
            }
        )
    return compact


# ---- Playwright (headed AI-controlled Chromium) -----------------------------

class PlaywrightHost:
    """Lazy-init headed Chromium running in its own asyncio loop on a background thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._browser: Any = None
        self._page: Any = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            with self._lock:
                if self._loop is None:
                    self._loop = asyncio.new_event_loop()
                    threading.Thread(target=self._loop.run_forever, daemon=True).start()
        return self._loop

    def _submit(self, coro: Any, timeout: int = 60) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    async def _start(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = await ctx.new_page()
        self._browser = browser

    def ensure(self) -> str:
        self._submit(self._start())
        return "browser ready"

    def navigate(self, url: str) -> str:
        self.ensure()

        async def _go() -> str:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return f"navigated to {self._page.url}"

        return self._submit(_go())

    def snapshot(self) -> tuple[str, str]:
        self.ensure()

        async def _snap() -> tuple[str, str]:
            page = self._page
            title = await page.title()
            url = page.url
            text = await page.evaluate("() => document.body.innerText.slice(0, 3000)")
            png = await page.screenshot(full_page=False, type="jpeg", quality=70)
            return (
                f"title: {title}\nurl: {url}\ntext (first 3000 chars):\n{text}",
                base64.b64encode(png).decode(),
            )

        return self._submit(_snap())

    def click(self, selector: str) -> str:
        self.ensure()

        async def _c() -> str:
            await self._page.click(selector, timeout=8000)
            return f"clicked {selector!r}"

        return self._submit(_c())

    def fill(self, selector: str, text: str) -> str:
        self.ensure()

        async def _f() -> str:
            await self._page.fill(selector, text, timeout=8000)
            return f"filled {selector!r} with {len(text)} chars"

        return self._submit(_f())

    def press(self, key: str) -> str:
        self.ensure()

        async def _p() -> str:
            await self._page.keyboard.press(key)
            return f"pressed {key}"

        return self._submit(_p())

    def eval_js(self, js: str) -> str:
        self.ensure()

        async def _e() -> str:
            r = await self._page.evaluate(js)
            return json.dumps(r, ensure_ascii=False)[:2000]

        return self._submit(_e())


# ---- Tool catalog -----------------------------------------------------------

DESKTOP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "screen_capture",
        "description": "Full-screen screenshot (compressed JPEG, attached to next message).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "screen_region",
        "description": "Crop screenshot to (x,y,w,h) at native res.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["x", "y", "width", "height"],
        },
    },
    {
        "name": "mouse_click",
        "description": "Click absolute pixel (x,y).",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "default": "left"},
                "clicks": {"type": "integer", "default": 1},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "keyboard_type",
        "description": "Type text at current focus.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "keyboard_shortcut",
        "description": "Press key combo (e.g. 'cmd+space').",
        "input_schema": {
            "type": "object",
            "properties": {"combo": {"type": "string"}},
            "required": ["combo"],
        },
    },
    {
        "name": "activate_app",
        "description": "Bring an app to front by owner name.",
        "input_schema": {
            "type": "object",
            "properties": {"owner": {"type": "string"}},
            "required": ["owner"],
        },
    },
    {
        "name": "window_list",
        "description": "List visible windows with bounds.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_terminal",
        "description": "Read text of front iTerm/Terminal session via AppleScript (~2500 chars).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "chrome_open_url",
        "description": "Open URL in user's real Google Chrome.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "new_tab": {"type": "boolean", "default": True},
            },
            "required": ["url"],
        },
    },
    {
        "name": "chrome_tabs",
        "description": "List URL + title of all tabs in user's front Chrome window.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "chrome_eval",
        "description": "Run JS in user's real Chrome active tab. Uses their session cookies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "js": {"type": "string"},
                "tab_index": {"type": "integer"},
            },
            "required": ["js"],
        },
    },
    {
        "name": "clipboard_write",
        "description": "Write text to clipboard.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "clipboard_read",
        "description": "Read clipboard.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait",
        "description": "Sleep N seconds (max 5).",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "browser_open",
        "description": "Open headed Chromium (separate from user's Chrome). Once.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_navigate",
        "description": "Navigate the AI-controlled Chromium to URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "Get current Chromium title+URL+body text+screenshot.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_click",
        "description": "Click element in Chromium by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": "Fill input in Chromium by selector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_press",
        "description": "Press key in Chromium (e.g. 'Enter').",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "browser_eval",
        "description": "Run JS expression in Chromium page context.",
        "input_schema": {
            "type": "object",
            "properties": {"js": {"type": "string"}},
            "required": ["js"],
        },
    },
    {
        "name": "selfmod_request_change",
        "description": (
            "Request a code change to the agentflow-computer-mcp daemon itself. "
            "Queues the request; a background worker spawns a code agent that opens a PR. "
            "Rate-limited to 1 accepted request per 15 minutes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why this change matters (1-2 sentences).",
                },
                "suggested_change": {
                    "type": "string",
                    "description": "Concrete description of what to change. Files, behaviour, acceptance.",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "default": "normal",
                },
            },
            "required": ["reason", "suggested_change"],
        },
    },
    {
        "name": "selfmod_list_recent",
        "description": "List recent self-modification requests with status. Use to verify a previous request landed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "code_read_file",
        "description": (
            "Read a project file as the LLM's source-of-truth before editing. "
            "Returns up to max_lines lines and the total line count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_lines": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
        },
    },
    {
        "name": "code_write_file",
        "description": (
            "Write or append a file. mode='replace' overwrites; mode='append' tails. "
            "Triggers the macOS confirm dialog through the fs.write gate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["replace", "append"], "default": "replace"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "code_edit_file",
        "description": (
            "Find/replace edit on a file. count=1 by default; pass count='all' to "
            "replace every occurrence. Errors if find is missing or more frequent than count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "count": {"oneOf": [{"type": "integer"}, {"type": "string"}], "default": 1},
            },
            "required": ["path", "find", "replace"],
        },
    },
    {
        "name": "code_run_command",
        "description": (
            "Run a shell command from a chosen cwd (defaults to $HOME). Returns stdout, "
            "stderr, exit_code, duration_ms. Goes through the shell.exec confirm gate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "code_list_dir",
        "description": (
            "List files under a directory with depth limit. Ignores .git, node_modules, "
            "dist, .venv, __pycache__ by default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer", "default": 1},
                "ignore_globs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path"],
        },
    },
    {
        "name": "task_complete",
        "description": "Finish with answer.",
        "input_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
]


def all_tool_descriptors() -> list[dict[str, Any]]:
    """Desktop tools + AgentFlow API tools, in one flat list for the Anthropic API."""
    return DESKTOP_TOOLS + AF_TOOL_DESCRIPTORS


class ToolExecutor:
    def __init__(
        self,
        last_cursor_ref: list[int],
        af_client: AFClient | None = None,
        pw: PlaywrightHost | None = None,
        scope: Scope | None = None,
        state: Any = None,
    ) -> None:
        self._cursor = last_cursor_ref
        self._af = af_client
        self._pw = pw or PlaywrightHost()
        self._scope = scope if scope is not None else load_scope()
        # Optional DriverState reference for emitting "task_action" frames
        # BEFORE long-running code tool dispatch so the cabinet timeline
        # shows the action while it's in flight, not only after completion.
        self._state = state

    def _announce(self, action: str, detail: str = "") -> None:
        if self._state is None:
            return
        try:
            from .loop import update_live

            update_live(self._state, action, detail)
        except Exception:  # noqa: BLE001 — visualization is best-effort
            pass

    def _confirm_blocking(self, tool_name: str, summary: str) -> bool:
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(confirm(tool_name, summary))
            finally:
                loop.close()
        except Exception:  # noqa: BLE001
            return False

    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
        # AgentFlow API tools
        if name.startswith("af_"):
            if self._af is None:
                return json.dumps({"ok": False, "error": "no AF api key configured"}), None
            return dispatch_af_tool(self._af, name, args), None

        if name == "screen_capture":
            return "screenshot", {"b64": jpeg_b64_full()}
        if name == "screen_region":
            return (
                "region",
                {"b64": jpeg_b64_region(args["x"], args["y"], args["width"], args["height"])},
            )
        if name == "mouse_click":
            x, y = args["x"], args["y"]
            mouse.click(x, y, args.get("button", "left"), clicks=args.get("clicks", 1))
            self._cursor[:] = [x, y]
            return f"clicked ({x},{y})", None
        if name == "keyboard_type":
            keyboard.type_text(args["text"])
            return f"typed {len(args['text'])} chars", None
        if name == "keyboard_shortcut":
            keyboard.shortcut(args["combo"])
            return f"pressed {args['combo']}", None
        if name == "activate_app":
            return app_activate(args["owner"]), None
        if name == "window_list":
            w = get_window_list()
            return json.dumps({"count": len(w), "windows": w}, ensure_ascii=False), None
        if name == "read_terminal":
            return read_iterm_session(), None
        if name == "chrome_open_url":
            return chrome_open_url(args["url"], args.get("new_tab", True)), None
        if name == "chrome_tabs":
            return chrome_list_tabs(), None
        if name == "chrome_eval":
            return chrome_run_js(args["js"], args.get("tab_index")), None
        if name == "clipboard_write":
            asyncio.run(clipboard.write(args["text"]))
            return "ok", None
        if name == "clipboard_read":
            return json.dumps(asyncio.run(clipboard.read()), ensure_ascii=False), None
        if name == "wait":
            time.sleep(min(float(args["seconds"]), 5))
            return f"slept {args['seconds']}s", None
        if name == "browser_open":
            return self._pw.ensure(), None
        if name == "browser_navigate":
            return self._pw.navigate(args["url"]), None
        if name == "browser_snapshot":
            text, b64 = self._pw.snapshot()
            return text, {"b64": b64}
        if name == "browser_click":
            return self._pw.click(args["selector"]), None
        if name == "browser_fill":
            return self._pw.fill(args["selector"], args["text"]), None
        if name == "browser_press":
            return self._pw.press(args["key"]), None
        if name == "browser_eval":
            return self._pw.eval_js(args["js"]), None
        if name == "selfmod_request_change":
            from . import selfmod

            try:
                result = selfmod.request_change(
                    reason=args["reason"],
                    suggested_change=args["suggested_change"],
                    urgency=args.get("urgency", "normal"),
                )
                return json.dumps(result, ensure_ascii=False), None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "selfmod_list_recent":
            from . import selfmod

            try:
                rows = selfmod.list_recent(int(args.get("limit", 10)))
                return json.dumps({"items": rows}, ensure_ascii=False), None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "code_read_file":
            self._announce("code_read_file", f"path={args.get('path', '')}")
            try:
                result = code_tool.read_file(
                    args["path"],
                    scope=self._scope,
                    max_lines=int(args.get("max_lines", 2000)),
                )
                return json.dumps(result, ensure_ascii=False)[:4000], None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "code_write_file":
            mode = args.get("mode", "replace")
            self._announce("code_write_file", f"path={args.get('path', '')} mode={mode}")
            if requires_confirm("computer.fs.write", self._scope):
                summary = confirm_summary("code.write_file", {"path": args["path"], "mode": mode})
                if not self._confirm_blocking("computer.code.write_file", summary):
                    return json.dumps({"ok": False, "error": "user denied code_write_file"}), None
            try:
                result = code_tool.write_file(
                    args["path"], args["content"], scope=self._scope, mode=mode
                )
                return json.dumps(result, ensure_ascii=False), None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "code_edit_file":
            count = args.get("count", 1)
            self._announce("code_edit_file", f"path={args.get('path', '')} count={count}")
            if requires_confirm("computer.fs.write", self._scope):
                summary = confirm_summary("code.edit_file", {"path": args["path"], "count": count})
                if not self._confirm_blocking("computer.code.edit_file", summary):
                    return json.dumps({"ok": False, "error": "user denied code_edit_file"}), None
            try:
                result = code_tool.edit_file(
                    args["path"],
                    args["find"],
                    args["replace"],
                    scope=self._scope,
                    count=count,
                )
                return json.dumps(result, ensure_ascii=False), None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "code_run_command":
            cwd = args.get("cwd") or str(__import__("pathlib").Path.home())
            cmd = args["command"]
            self._announce("code_run_command", f"cwd={cwd} cmd={cmd[:120]}")
            if requires_confirm("computer.shell.exec", self._scope):
                summary = confirm_summary("code.run_command", {"cwd": cwd, "command": cmd})
                if not self._confirm_blocking("computer.code.run_command", summary):
                    return json.dumps({"ok": False, "error": "user denied code_run_command"}), None
            try:
                result = asyncio.run(
                    code_tool.run_command(
                        cmd,
                        scope=self._scope,
                        cwd=args.get("cwd"),
                        timeout=int(args.get("timeout", 120)),
                    )
                )
                # Truncate noisy output so the LLM tool_result stays under ~4 KB.
                return json.dumps(result, ensure_ascii=False)[:4000], None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "code_list_dir":
            self._announce("code_list_dir", f"path={args.get('path', '')}")
            try:
                result = code_tool.list_dir(
                    args["path"],
                    scope=self._scope,
                    depth=int(args.get("depth", 1)),
                    ignore_globs=args.get("ignore_globs"),
                )
                return json.dumps(result, ensure_ascii=False)[:4000], None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "task_complete":
            return "__DONE__", None
        return f"unknown tool: {name}", None
