"""Firefox driver — attach to the user's real Firefox so already-logged-in
sites (kwork.ru, Telegram Web, mail) work without re-auth.

Strategy:
  - Playwright `firefox.launch_persistent_context(user_data_dir=<profile>)`
    so the launched process reuses the user's cookies / extensions /
    bookmarks. Pure attach-over-CDP is unavailable for Firefox; persistent
    context is the supported equivalent.
  - Profile discovery: `~/Library/Application Support/Firefox/Profiles/
    *.default-release` on Mac, `~/.mozilla/firefox/*.default-release`
    on Linux, `%APPDATA%\\Mozilla\\Firefox\\Profiles\\*.default-release`
    on Windows. Env override: `AGENTFLOW_FIREFOX_PROFILE`.
  - Tools mirror the Chromium `browser_*` surface 1:1 with a `firefox_`
    prefix so the LLM can pick the right one per intent.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


def _candidate_profile_roots() -> list[Path]:
    """Per-OS list of likely Firefox `Profiles/` directories. Order matters —
    first hit wins so users with multiple OS installs land on the canonical
    profile, not the snap/flatpak copy."""
    home = Path.home()
    if sys.platform.startswith("darwin"):
        return [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return [Path(appdata) / "Mozilla" / "Firefox" / "Profiles"]
        return [home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"]
    # Linux — native install first, snap/flatpak as fallback.
    return [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]


def discover_profile_path() -> str | None:
    """Return the absolute path of the user's primary Firefox profile, or
    None if no profile is found and no env override is set.

    Resolution order:
      1. `AGENTFLOW_FIREFOX_PROFILE` env (absolute path).
      2. First `*.default-release` directory under a candidate root.
      3. First `*.default` directory (older Firefox / dev edition).
      4. None.
    """
    override = os.environ.get("AGENTFLOW_FIREFOX_PROFILE")
    if override:
        p = Path(override).expanduser()
        return str(p) if p.exists() else None

    for root in _candidate_profile_roots():
        if not root.exists():
            continue
        # Prefer release channel first.
        for suffix in ("*.default-release", "*.default", "*.default-esr"):
            matches = sorted(root.glob(suffix))
            if matches:
                return str(matches[0])
    return None


class FirefoxHost:
    """Lazy-init Firefox driving the user's real profile, on its own asyncio
    loop in a background thread. Mirrors the Chromium PlaywrightHost API so
    the executor can swap between the two without callers caring."""

    def __init__(
        self,
        profile_path: str | None = None,
        executable_path: str | None = None,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._context: Any = None
        self._page: Any = None
        self._lock = threading.Lock()
        # `profile_path` overrides discovery; useful for tests / power users.
        self._profile_override = profile_path
        # `executable_path` lets the brief point at a non-default Firefox
        # binary (Nightly, Developer Edition, custom build).
        self._executable_path = executable_path or os.environ.get(
            "AGENTFLOW_FIREFOX_BINARY"
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            with self._lock:
                if self._loop is None:
                    self._loop = asyncio.new_event_loop()
                    threading.Thread(
                        target=self._loop.run_forever, daemon=True
                    ).start()
        return self._loop

    def _submit(self, coro: Any, timeout: int = 60) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    async def _start(self) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        profile = self._profile_override or discover_profile_path()
        if not profile:
            raise RuntimeError(
                "firefox profile not found — set AGENTFLOW_FIREFOX_PROFILE to "
                "an absolute path or install Firefox in the default location"
            )

        pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": profile,
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
        }
        if self._executable_path:
            launch_kwargs["executable_path"] = self._executable_path
        # `launch_persistent_context` returns a BrowserContext, not a Browser.
        # The user's existing windows stay untouched; Playwright opens its
        # own window against the same profile dir.
        ctx = await pw.firefox.launch_persistent_context(**launch_kwargs)
        # New tab in the persistent profile.
        self._page = await ctx.new_page()
        self._context = ctx

    def ensure(self) -> str:
        self._submit(self._start())
        return "firefox ready"

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
            text = await page.evaluate(
                "() => document.body.innerText.slice(0, 3000)"
            )
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


# Tool descriptors mirror the Chromium browser_* surface 1:1 so the LLM
# picks the prefix per intent: anon scraping → `browser_*`, logged-in
# action on the user's identity → `firefox_*`.
FIREFOX_TOOL_DESCRIPTORS: list[dict[str, Any]] = [
    {
        "name": "firefox_open",
        "description": (
            "Open the user's real Firefox via persistent context. Reuses "
            "their cookies / extensions — use this for sites where the "
            "user is already logged in (kwork.ru, mail, TG Web)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "firefox_navigate",
        "description": "Navigate the user's Firefox to URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "firefox_snapshot",
        "description": "Title + URL + body text + screenshot of the current Firefox tab.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "firefox_click",
        "description": "Click an element in Firefox by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "firefox_fill",
        "description": "Fill an input in Firefox by selector.",
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
        "name": "firefox_press",
        "description": "Press a key in Firefox (e.g. 'Enter').",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "firefox_eval",
        "description": "Run a JS expression in the Firefox page context.",
        "input_schema": {
            "type": "object",
            "properties": {"js": {"type": "string"}},
            "required": ["js"],
        },
    },
]


def dispatch_firefox_tool(
    host: FirefoxHost, name: str, args: dict[str, Any]
) -> tuple[str, dict[str, str] | None]:
    """Execute one firefox_* tool. Mirrors the Chromium dispatcher: returns
    (text, optional_image_b64_dict). The executor passes the image dict
    through to the LLM as a tool_result image."""
    if name == "firefox_open":
        return host.ensure(), None
    if name == "firefox_navigate":
        return host.navigate(args["url"]), None
    if name == "firefox_snapshot":
        text, b64 = host.snapshot()
        return text, {"b64": b64}
    if name == "firefox_click":
        return host.click(args["selector"]), None
    if name == "firefox_fill":
        return host.fill(args["selector"], args["text"]), None
    if name == "firefox_press":
        return host.press(args["key"]), None
    if name == "firefox_eval":
        return host.eval_js(args["js"]), None
    return f"unknown firefox tool: {name}", None
