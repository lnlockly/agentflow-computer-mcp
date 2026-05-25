"""LLM-facing tool catalog + executor. Wraps:
- screen / mouse / keyboard / window / clipboard from ``agentflow_computer_mcp.tools``
- macOS AppleScript bridges to iTerm + Google Chrome
- Playwright headed Chromium (lazy)
- AgentFlow API client (``af_*`` tools)
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any

from PIL import Image

from ..config import Scope, load_scope, scope_from_mapping
from ..confirm import confirm, confirm_summary
from ..platform import PLATFORM, backend
from ..scope import requires_confirm
from ..tools import clipboard, keyboard, mouse, screen, window
from ..tools import code as code_tool
from ..tools import screen_record as screen_record_tool
from .af_client import AF_TOOL_DESCRIPTORS, AFClient, dispatch_af_tool
from .firefox import (
    FIREFOX_TOOL_DESCRIPTORS,
    FirefoxHost,
    dispatch_firefox_tool,
)

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
    # AppleScript-only — no cross-platform analog. Headless DevTools Protocol
    # would work on Windows/Linux but needs Chrome started with --remote-debug.
    # On non-Mac the LLM should fall back to browser_eval (headed Chromium).
    if PLATFORM != "mac":
        return (
            "error: chrome_eval requires AppleScript and only works on macOS. "
            "Use browser_open + browser_navigate + browser_eval (headed Chromium) instead."
        )
    js_esc = js.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    if tab_index is None:
        script = f'tell application "Google Chrome" to tell active tab of front window to execute javascript "{js_esc}"'
    else:
        script = f'tell application "Google Chrome" to tell tab {tab_index} of front window to execute javascript "{js_esc}"'
    rc, out = osa(script, timeout=20)
    return out if rc == 0 else f"error: {out}"


def chrome_open_url(url: str, new_tab: bool = True) -> str:
    """Open a URL in the user's real Chrome / default browser.

    Cross-platform: AppleScript on macOS, `start chrome` on Windows,
    `xdg-open` on Linux. ``new_tab`` is honoured only on macOS — the
    other platforms always open in whichever window the OS shell picks.
    """
    if PLATFORM == "mac":
        if new_tab:
            # `make new tab` creates AND returns the tab but doesn't
            # activate it — subsequent `chrome_run_js` was hitting the
            # caller's previous active tab (localhost dev server,
            # whatever was front), not this freshly-opened URL. Set it
            # active in the same AppleScript so chrome_eval picks it up
            # without needing chrome_list_tabs + tab_index.
            rc, out = osa(
                'tell application "Google Chrome"\n'
                f'  tell front window\n'
                f'    set newTab to make new tab with properties {{URL:"{url}"}}\n'
                f'    set active tab index to (count of tabs)\n'
                f'  end tell\n'
                'end tell',
                timeout=10,
            )
        else:
            rc, out = osa(
                f'tell application "Google Chrome" to set URL of active tab of front window to "{url}"',
                timeout=10,
            )
        return f"opened {url}" if rc == 0 else f"error: {out}"
    if PLATFORM == "windows":
        try:
            # `start` is a cmd builtin — invoke via shell. Quote-escape the URL.
            subprocess.run(
                ["cmd", "/c", "start", "", url],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return f"opened {url}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
    # Linux
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if not opener:
        return "error: no xdg-open / gio available; install xdg-utils"
    try:
        cmd = [opener, "open", url] if opener.endswith("gio") else [opener, url]
        subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        return f"opened {url}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def chrome_list_tabs() -> str:
    if PLATFORM != "mac":
        return (
            "error: chrome_tabs requires AppleScript and only works on macOS. "
            "Use browser_snapshot (headed Chromium) or query DevTools Protocol directly."
        )
    rc, out = osa(
        'tell application "Google Chrome" to get {URL, title} of tabs of front window',
        timeout=10,
    )
    return out


# ---- Windows / cross-platform helpers ---------------------------------------

def powershell_exec(command: str, timeout: int = 30) -> dict[str, Any]:
    """Run a PowerShell command and return stdout/stderr/exit_code.

    Only works on Windows. The LLM-facing tool dispatcher also routes this
    through ``scope.shell_whitelist`` — the program ``powershell`` must be
    allow-listed for the call to land.
    """
    if PLATFORM != "windows":
        return {
            "ok": False,
            "error": "mac_only_or_linux_only",
            "detail": "powershell_exec is Windows-only; use code_run_command on macOS/Linux",
        }
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": (r.stdout or "")[:8000],
            "stderr": (r.stderr or "")[:4000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "timeout_s": timeout}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def winget_search(query: str) -> dict[str, Any]:
    """Discover an app via `winget search`. Windows-only."""
    if PLATFORM != "windows":
        return {"ok": False, "error": "windows_only", "detail": "winget exists only on Windows"}
    try:
        r = subprocess.run(
            ["winget", "search", "--source", "winget", "--accept-source-agreements", query],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": (r.stdout or "")[:8000],
            "stderr": (r.stderr or "")[:2000],
        }
    except FileNotFoundError:
        return {"ok": False, "error": "winget_not_found", "detail": "winget is not on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def winget_install(package_id: str) -> dict[str, Any]:
    """Install an app via `winget install <id>`. Windows-only."""
    if PLATFORM != "windows":
        return {"ok": False, "error": "windows_only", "detail": "winget exists only on Windows"}
    try:
        r = subprocess.run(
            [
                "winget",
                "install",
                "--id",
                package_id,
                "--exact",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": (r.stdout or "")[:8000],
            "stderr": (r.stderr or "")[:2000],
        }
    except FileNotFoundError:
        return {"ok": False, "error": "winget_not_found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def start_app(name: str) -> str:
    """Launch an app by name, cross-platform.

    macOS:  `open -a <name>`
    Windows: `Start-Process <name>` via PowerShell
    Linux:  best-effort — try the binary, then xdg-open as a fallback
    """
    if PLATFORM == "mac":
        try:
            r = subprocess.run(
                ["open", "-a", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return f"launched {name}" if r.returncode == 0 else f"error: {r.stderr.strip() or 'open -a failed'}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
    if PLATFORM == "windows":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Start-Process -FilePath '{name}'"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return f"launched {name}" if r.returncode == 0 else f"error: {(r.stderr or 'Start-Process failed').strip()}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
    # Linux
    binary = shutil.which(name)
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"launched {name}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
    opener = shutil.which("xdg-open")
    if opener:
        try:
            subprocess.Popen([opener, name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"launched {name} (via xdg-open)"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
    return f"error: cannot launch {name!r} on Linux — not in PATH and xdg-open missing"


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
        self._thread: threading.Thread | None = None
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            with self._lock:
                if self._loop is None:
                    self._loop = asyncio.new_event_loop()
                    loop = self._loop

                    def _run() -> None:
                        asyncio.set_event_loop(loop)
                        loop.run_forever()
                        loop.close()

                    self._thread = threading.Thread(target=_run, daemon=True)
                    self._thread.start()
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
        self._pw = pw
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = await ctx.new_page()
        self._browser = browser
        self._context = ctx

    async def _close(self) -> None:
        page = self._page
        context = self._context
        browser = self._browser
        pw = self._pw
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        if page is not None:
            with contextlib.suppress(Exception):
                await page.close()
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()

    def close(self, timeout: int = 15) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._close(), loop).result(timeout=timeout)
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=1.0)
        self._loop = None
        self._thread = None

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
        "name": "chrome_export_cookies",
        "description": (
            "Export cookies (HttpOnly + non-HttpOnly) for a domain from user's Chrome profile by "
            "reading the local SQLite store and decrypting via macOS Keychain. Result is "
            "Playwright storage_state.cookies compatible. Use ONLY after a successful "
            "chrome_open_url+chrome_eval login probe."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "profile": {"type": "string", "default": "Default"},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "connect_integration",
        "description": (
            "Connect a third-party integration (kwork, vk, lolzteam, ...) using the "
            "registry-driven flow. Opens the login URL, probes the logged-in state, "
            "exports cookies, POSTs them to /me/integrations/<provider>. Returns "
            "{ok, cookie_count, secret_created} or {ok:false, error:<code>}. Cookie "
            "values are never echoed back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Provider slug from /integrations/registry (e.g. 'kwork', 'vk').",
                }
            },
            "required": ["provider"],
        },
    },
    {
        "name": "project_clone_and_setup",
        "description": (
            "Clone a GitHub Next.js template into /workspace/proj-<slug>, reinit "
            "git, install dependencies (npm/pnpm/yarn auto-detected by lockfile), "
            "start `npm run dev` in the background, wait for port 3000, then POST "
            "the result to /internal/projects/<id>/clone-status. Returns "
            "{ok, port_reachable, dev_pid, project_dir} or {ok:false, error:<code>}. "
            "Invoked by the platform after /me/projects/:id/approve; daemon-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_repo_full": {
                    "type": "string",
                    "description": "GitHub repo in 'owner/repo' form, e.g. 'ixartz/Next-JS-Landing-Page-Starter-Template'.",
                },
                "slug": {
                    "type": "string",
                    "description": "Project slug — workspace dir becomes /workspace/proj-<slug>.",
                },
                "project_id": {
                    "type": "integer",
                    "description": "Backend projects.id for the clone-status callback.",
                },
                "port": {
                    "type": "integer",
                    "default": 3000,
                    "description": "Dev-server port to wait for. Default 3000.",
                },
            },
            "required": ["template_repo_full", "slug", "project_id"],
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
        "name": "screen_record_start",
        "description": (
            "Start recording a short screen-video to a local .mp4 file. "
            "Use ONLY when the user explicitly asks for a video / clip / запись экрана. "
            "Path must live under ~/Movies, ~/tmp, ~/Downloads, or a recordings/ subdir. "
            "Auto-stops at max_duration_s (default 120s) so a forgotten stop can't fill the disk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "fps": {"type": "integer", "default": 10},
                "width_cap": {"type": "integer", "default": 1280},
                "max_duration_s": {"type": "integer", "default": 120},
            },
            "required": ["path"],
        },
    },
    {
        "name": "screen_record_stop",
        "description": (
            "Stop the current screen recording. Returns final path, duration_ms, "
            "and file_bytes. Call right after the demoed action completes."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "screen_record_status",
        "description": (
            "Check whether a recording is currently active and how many frames "
            "have been written so far."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "powershell_exec",
        "description": (
            "Windows only. Run a PowerShell command. Returns stdout/stderr/exit_code. "
            "The program `powershell` must be in scope.shell_whitelist. Use this instead "
            "of code_run_command when you need PowerShell-specific cmdlets (Get-Process, "
            "Start-Process, Get-WmiObject, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "winget_search",
        "description": "Windows only. Search winget for a package (returns matching Ids).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "winget_install",
        "description": (
            "Windows only. Install a winget package by exact Id. Run winget_search first "
            "to find the right Id. Requires user confirmation (shell.exec gate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "start_app",
        "description": (
            "Launch an app by name, cross-platform. macOS uses `open -a`, Windows uses "
            "`Start-Process`, Linux tries the binary then xdg-open. For full window control "
            "use activate_app instead (focuses an already-running window)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
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


# Tools that only make sense on macOS — wrap AppleScript or *nix-only paths.
# Filtered out of the LLM tool list on Windows/Linux so the model never tries
# to call them on the wrong host.
MAC_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "chrome_eval",
        "chrome_tabs",
    }
)

# Tools that only work on Windows. Filtered out on macOS/Linux.
WINDOWS_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "powershell_exec",
        "winget_search",
        "winget_install",
    }
)


def _filter_tools_by_os(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip OS-incompatible tools out of the LLM-visible catalog.

    The driver loop never exposes ``osascript_*`` on Windows nor
    ``powershell_exec`` on macOS — anything the model can call back must
    be backed by a real executable on the current host. Tools that are
    cross-platform (chrome_open_url, start_app, screen_*, browser_*) stay
    visible everywhere.
    """
    host = platform.system()
    out: list[dict[str, Any]] = []
    for t in tools:
        name = t.get("name", "")
        if host == "Darwin" and name in WINDOWS_ONLY_TOOLS:
            continue
        if host != "Darwin" and name in MAC_ONLY_TOOLS:
            continue
        out.append(t)
    return out


def all_tool_descriptors() -> list[dict[str, Any]]:
    """Desktop tools + Firefox tools + AgentFlow API tools, filtered by host OS."""
    raw = DESKTOP_TOOLS + FIREFOX_TOOL_DESCRIPTORS + AF_TOOL_DESCRIPTORS
    return _filter_tools_by_os(raw)


class ToolExecutor:
    def __init__(
        self,
        last_cursor_ref: list[int],
        af_client: AFClient | None = None,
        pw: PlaywrightHost | None = None,
        firefox: FirefoxHost | None = None,
        scope: Scope | None = None,
        state: Any = None,
    ) -> None:
        self._cursor = last_cursor_ref
        self._af = af_client
        self._pw = pw or PlaywrightHost()
        # Firefox attaches to the user's real profile so logged-in sites
        # (kwork, TG Web, mail) just work without re-auth. Lazy-init via
        # firefox_open so a missing profile doesn't crash the daemon.
        self._firefox = firefox or FirefoxHost()
        self._base_scope = scope if scope is not None else load_scope()
        self._scope = self._base_scope
        # Optional DriverState reference for emitting "task_action" frames
        # BEFORE long-running code tool dispatch so the cabinet timeline
        # shows the action while it's in flight, not only after completion.
        self._state = state

    def apply_task_scope(self, raw_scope: dict[str, Any] | None) -> None:
        self._scope = scope_from_mapping(raw_scope, base=self._base_scope)

    def reset_task_scope(self) -> None:
        self._scope = self._base_scope

    @property
    def base_scope(self):
        return self._base_scope

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._pw.close()
        with contextlib.suppress(Exception):
            self._firefox.close()

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
        if name == "chrome_export_cookies":
            from .chrome_cookies import export_cookies

            try:
                result = export_cookies(args["domain"], args.get("profile", "Default"))
            except Exception as exc:  # noqa: BLE001 — surface as tool_result, never crash the loop
                result = {"ok": False, "error": f"unexpected: {exc.__class__.__name__}"}
            return json.dumps(result, ensure_ascii=False), None
        if name == "connect_integration":
            from .chrome_cookies import export_cookies
            from .tools.integrations import connect_integration

            if self._af is None:
                return (
                    json.dumps(
                        {
                            "ok": False,
                            "error": "no_api_key",
                            "hint": "AF owner key missing on daemon.",
                        },
                        ensure_ascii=False,
                    ),
                    None,
                )
            api_key = self._af._key  # noqa: SLF001 — daemon owns this private member
            api_base = self._af._base  # noqa: SLF001
            try:
                result = connect_integration(
                    provider=str(args.get("provider", "")),
                    api_key=api_key,
                    api_base=api_base,
                    chrome_open_url=lambda url, new_tab=True: chrome_open_url(url, new_tab),
                    chrome_eval=lambda js, tab=None: chrome_run_js(js, tab),
                    chrome_export_cookies=lambda domain, profile="Default": export_cookies(
                        domain, profile
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — surface as tool_result, never crash the loop
                result = {
                    "ok": False,
                    "error": f"unexpected: {exc.__class__.__name__}",
                    "detail": str(exc),
                }
            return json.dumps(result, ensure_ascii=False), None
        if name == "project_clone_and_setup":
            # Phase A3 — clone GitHub template + start dev server + report.
            # See driver/tools/project_setup.py for the full flow.
            from .tools.project_setup import project_clone_and_setup

            # Backend coords: the platform-set AF_INTERNAL_API_SECRET grants
            # the daemon write access to /internal/projects/:id/clone-status.
            # Falls back to selftest if missing so a misconfigured pod surfaces
            # a clean tool_result instead of a stack trace.
            internal_secret = os.environ.get("AF_INTERNAL_API_SECRET", "")
            if self._af is not None:
                api_base = self._af._base  # noqa: SLF001 — daemon owns this
            else:
                api_base = os.environ.get(
                    "AF_API_URL", "https://agentflow.website"
                ).rstrip("/")
                if not api_base.endswith("/_agents"):
                    api_base = api_base + "/_agents"
            if not internal_secret:
                return (
                    json.dumps(
                        {
                            "ok": False,
                            "error": "missing_internal_secret",
                            "hint": "AF_INTERNAL_API_SECRET env var not set on hosted pod.",
                        },
                        ensure_ascii=False,
                    ),
                    None,
                )
            try:
                result = project_clone_and_setup(
                    template_repo_full=str(args.get("template_repo_full", "")),
                    slug=str(args.get("slug", "")),
                    project_id=int(args.get("project_id", 0) or 0),
                    api_base=api_base,
                    internal_secret=internal_secret,
                    port=int(args.get("port", 3000) or 3000),
                )
            except Exception as exc:  # noqa: BLE001 — surface as tool_result
                result = {
                    "ok": False,
                    "error": f"unexpected: {exc.__class__.__name__}",
                    "detail": str(exc),
                }
            return json.dumps(result, ensure_ascii=False), None
        if name == "clipboard_write":
            asyncio.run(clipboard.write(args["text"]))
            return "ok", None
        if name == "clipboard_read":
            return json.dumps(asyncio.run(clipboard.read()), ensure_ascii=False), None
        if name == "wait":
            # Sleep is the canonical long-pole inside a tool dispatch. Slice
            # into 0.2 s polls so a mid-task cancel signal lands within that
            # interval instead of after the full 5 s.
            total = min(float(args["seconds"]), 5)
            remaining = total
            slept = 0.0
            abort = getattr(self._state, "abort_flag", None) if self._state else None
            while remaining > 0:
                if abort is not None and abort.is_set():
                    return f"aborted after {slept:.1f}s of {total}s", None
                step = 0.2 if remaining > 0.2 else remaining
                time.sleep(step)
                slept += step
                remaining -= step
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
        if name.startswith("firefox_"):
            try:
                return dispatch_firefox_tool(self._firefox, name, args)
            except Exception as exc:  # noqa: BLE001 — surface as tool_result, never crash the loop
                return f"firefox error: {exc}", None
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
        if name == "screen_record_start":
            path_arg = args.get("path", "")
            fps = int(args.get("fps", 10))
            self._announce(
                "screen_record_start", f"path={path_arg} fps={fps}"
            )
            try:
                result = screen_record_tool.get_recorder().start(
                    path_arg,
                    fps=fps,
                    width_cap=int(args.get("width_cap", 1280)),
                    max_duration_s=int(args.get("max_duration_s", 120)),
                )
                return json.dumps(result, ensure_ascii=False), None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "screen_record_stop":
            try:
                result = screen_record_tool.get_recorder().stop()
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
            detail = (
                f"path={result.get('path')} "
                f"duration_ms={result.get('duration_ms')} "
                f"bytes={result.get('file_bytes')}"
            )
            self._announce("screen_record_stop", detail)
            return json.dumps(result, ensure_ascii=False), None
        if name == "screen_record_status":
            try:
                result = screen_record_tool.get_recorder().status()
                return json.dumps(result, ensure_ascii=False), None
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), None
        if name == "powershell_exec":
            cmd = args.get("command", "")
            self._announce("powershell_exec", f"cmd={cmd[:120]}")
            if PLATFORM != "windows":
                return json.dumps(
                    {"ok": False, "error": "windows_only", "detail": "powershell_exec is Windows-only"},
                    ensure_ascii=False,
                ), None
            # Route through the same shell_whitelist gate as code_run_command.
            try:
                from ..scope import check_shell

                check_shell("powershell", self._scope)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": f"scope: {exc}"}), None
            if requires_confirm("computer.shell.exec", self._scope):
                summary = confirm_summary(
                    "powershell.exec", {"command": cmd[:300]}
                )
                if not self._confirm_blocking("computer.powershell.exec", summary):
                    return json.dumps({"ok": False, "error": "user denied powershell_exec"}), None
            result = powershell_exec(cmd, timeout=int(args.get("timeout", 30)))
            return json.dumps(result, ensure_ascii=False)[:4000], None
        if name == "winget_search":
            self._announce("winget_search", f"query={args.get('query', '')[:80]}")
            result = winget_search(args.get("query", ""))
            return json.dumps(result, ensure_ascii=False)[:4000], None
        if name == "winget_install":
            pkg = args.get("id", "")
            self._announce("winget_install", f"id={pkg}")
            if PLATFORM != "windows":
                return json.dumps(
                    {"ok": False, "error": "windows_only"}, ensure_ascii=False
                ), None
            if requires_confirm("computer.shell.exec", self._scope):
                summary = confirm_summary("winget.install", {"id": pkg})
                if not self._confirm_blocking("computer.winget.install", summary):
                    return json.dumps({"ok": False, "error": "user denied winget_install"}), None
            result = winget_install(pkg)
            return json.dumps(result, ensure_ascii=False)[:4000], None
        if name == "start_app":
            self._announce("start_app", f"name={args.get('name', '')}")
            return start_app(args.get("name", "")), None
        if name == "task_complete":
            return "__DONE__", None
        return f"unknown tool: {name}", None
