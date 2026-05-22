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
import fnmatch
import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

# Cookie-bearing domains the daemon must refuse to touch. Anything that
# moves money or holds custody. Wildcard match via fnmatch.
COOKIE_DENY_DOMAINS: tuple[str, ...] = (
    "*.bank*",
    "*bank*",
    "*.kassa*",
    "*kassa*",
    "*.qiwi*",
    "*qiwi*",
    "*.binance.com",
    "binance.com",
    "*.coinbase.com",
    "coinbase.com",
    "*.cryptobot.live",
    "cryptobot.live",
    "paypal.*",
    "*.paypal.com",
    "paypal.com",
)


def _domain_is_denied(domain: str) -> bool:
    d = domain.lower().lstrip(".")
    return any(fnmatch.fnmatch(d, pat.lower()) for pat in COOKIE_DENY_DOMAINS)


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

    # --- Cookie tools ------------------------------------------------------
    # The LLM never sees raw cookie values. `get_cookies` redacts long
    # values and stashes the full record under an opaque token; the LLM
    # passes the token back to `paste_cookie_into`, never the value.

    def _cookie_store(self) -> dict[str, dict[str, Any]]:
        if not hasattr(self, "_cookie_tokens"):
            self._cookie_tokens: dict[str, dict[str, Any]] = {}
        return self._cookie_tokens

    @staticmethod
    def _redact(value: str) -> str:
        if value is None:
            return ""
        if len(value) <= 100:
            return value
        return f"<redacted {len(value)} chars>"

    def get_cookies(self, domain: str) -> dict[str, Any]:
        """Return cookies for `domain` from the user's profile. Raw values
        stay in `_cookie_tokens`; only redacted summaries + opaque tokens
        go to the LLM."""
        if _domain_is_denied(domain):
            return {"ok": False, "error": f"domain refused by deny-list: {domain}"}

        self.ensure()
        d = domain.lower().lstrip(".")
        urls = [f"https://{d}/", f"https://www.{d}/"]

        async def _g() -> list[dict[str, Any]]:
            return await self._context.cookies(urls=urls)

        try:
            raw = self._submit(_g())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"playwright cookies() failed: {exc}"}

        store = self._cookie_store()
        summary: list[dict[str, Any]] = []
        tokens: list[str] = []
        for c in raw:
            tok = "t_" + secrets.token_hex(4)
            store[tok] = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", False)),
                "http_only": bool(c.get("httpOnly", False)),
                "expires": c.get("expires"),
            }
            tokens.append(tok)
            summary.append(
                {
                    "token": tok,
                    "name": c.get("name", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "value_preview": self._redact(c.get("value", "") or ""),
                    "secure": bool(c.get("secure", False)),
                    "http_only": bool(c.get("httpOnly", False)),
                    "expires": c.get("expires"),
                }
            )
        return {"ok": True, "count": len(raw), "tokens": tokens, "cookies": summary}

    def export_cookies_to(
        self,
        domain: str,
        dest_field_selector: str,
        fmt: str = "header",
    ) -> dict[str, Any]:
        """Grab `domain` cookies and paste them into `dest_field_selector`.
        fmt: 'header' → `name=value; name=value`. 'netscape' → tab-separated
        cookiejar lines (one cookie per line)."""
        if _domain_is_denied(domain):
            return {"ok": False, "error": f"domain refused by deny-list: {domain}"}

        got = self.get_cookies(domain)
        if not got.get("ok"):
            return got
        store = self._cookie_store()
        items = [store[t] for t in got.get("tokens", []) if t in store]

        if fmt == "netscape":
            lines = []
            for it in items:
                expires = int(it.get("expires") or 0)
                lines.append(
                    "\t".join(
                        [
                            it["domain"],
                            "TRUE",
                            it["path"] or "/",
                            "TRUE" if it.get("secure") else "FALSE",
                            str(expires if expires > 0 else 0),
                            it["name"],
                            it["value"],
                        ]
                    )
                )
            payload = "\n".join(lines)
        else:
            payload = "; ".join(f"{it['name']}={it['value']}" for it in items)

        self.ensure()

        async def _fill() -> None:
            await self._page.fill(dest_field_selector, payload, timeout=8000)

        try:
            self._submit(_fill())
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"fill failed: {exc}",
                "pasted": 0,
                "skipped": len(items),
            }
        return {
            "ok": True,
            "pasted": len(items),
            "skipped": 0,
            "format": fmt,
            "selector": dest_field_selector,
        }

    def drop_cookie_tokens(self) -> dict[str, Any]:
        store = self._cookie_store()
        n = len(store)
        store.clear()
        return {"ok": True, "dropped": n}


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
    {
        "name": "firefox_get_cookies",
        "description": (
            "Read cookies for a domain from the user's real Firefox profile. "
            "Raw values stay in process memory; the LLM sees redacted previews "
            "and opaque tokens that can be passed to firefox_export_cookies_to. "
            "Refuses bank / payment / crypto-custody domains."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": ["domain"],
        },
    },
    {
        "name": "firefox_export_cookies_to",
        "description": (
            "Format the user's cookies for `domain` and paste them into a form "
            "field selector inside the AgentFlow cabinet (or any other site). "
            "A native macOS confirm dialog runs first. fmt='header' produces "
            "`name=value; name=value`; fmt='netscape' produces a cookiejar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "dest_field_selector": {"type": "string"},
                "fmt": {
                    "type": "string",
                    "enum": ["header", "netscape"],
                    "default": "header",
                },
            },
            "required": ["domain", "dest_field_selector"],
        },
    },
    {
        "name": "firefox_drop_cookie_tokens",
        "description": (
            "Flush the in-process cookie-token store. Call between tasks so "
            "stale tokens never leak across user requests."
        ),
        "input_schema": {"type": "object", "properties": {}},
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
    if name == "firefox_get_cookies":
        return json.dumps(host.get_cookies(args["domain"]), ensure_ascii=False), None
    if name == "firefox_drop_cookie_tokens":
        return json.dumps(host.drop_cookie_tokens(), ensure_ascii=False), None
    # firefox_export_cookies_to is handled in the executor so it can fire
    # the macOS confirm dialog before any cookie paste.
    return f"unknown firefox tool: {name}", None
