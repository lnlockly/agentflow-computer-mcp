"""Firefox driver tests.

Playwright is mocked end-to-end so the suite runs without a real Firefox
install. We assert:
  - profile discovery resolves to the env override when set
  - profile discovery returns None when nothing is found
  - dispatch_firefox_tool routes each verb to the right host method
  - the host's ensure() path calls launch_persistent_context with the
    expected kwargs (user_data_dir + headless=False)
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from agentflow_computer_mcp.driver.firefox import (
    FIREFOX_TOOL_DESCRIPTORS,
    FirefoxHost,
    discover_profile_path,
    dispatch_firefox_tool,
)


def test_descriptors_have_firefox_prefix() -> None:
    names = {d["name"] for d in FIREFOX_TOOL_DESCRIPTORS}
    assert "firefox_open" in names
    assert "firefox_navigate" in names
    assert "firefox_snapshot" in names
    assert all(d["name"].startswith("firefox_") for d in FIREFOX_TOOL_DESCRIPTORS)
    assert all(d["input_schema"]["type"] == "object" for d in FIREFOX_TOOL_DESCRIPTORS)


def test_discover_profile_uses_env_override(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "my-profile.default-release"
    profile.mkdir()
    monkeypatch.setenv("AGENTFLOW_FIREFOX_PROFILE", str(profile))
    assert discover_profile_path() == str(profile)


def test_discover_profile_env_override_missing_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("AGENTFLOW_FIREFOX_PROFILE", "/nonexistent/path/xyz")
    assert discover_profile_path() is None


def test_discover_profile_returns_none_when_no_install(monkeypatch, tmp_path) -> None:
    # Point HOME at an empty tmp dir so no candidate roots exist.
    monkeypatch.delenv("AGENTFLOW_FIREFOX_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # On macOS Path.home() reads HOME first; the candidate roots will be
    # under tmp_path which is empty.
    assert discover_profile_path() is None


class _FakeNavigatorPage:
    def __init__(self) -> None:
        self.url = "https://example.com/"
        self.goto = AsyncMock()
        self.title = AsyncMock(return_value="Example")
        self.evaluate = AsyncMock(return_value="page text")
        self.screenshot = AsyncMock(return_value=b"\xff\xd8jpeg-bytes")
        self.click = AsyncMock()
        self.fill = AsyncMock()
        self.keyboard = MagicMock()
        self.keyboard.press = AsyncMock()


class _FakeContext:
    def __init__(self, page: _FakeNavigatorPage) -> None:
        self._page = page
        self.new_page = AsyncMock(return_value=page)


def _fake_playwright(page: _FakeNavigatorPage, ctx: _FakeContext) -> Any:
    """Mimic playwright.async_api.async_playwright().start() returning a
    context where `pw.firefox.launch_persistent_context(...)` resolves to
    our fake BrowserContext."""
    fake_pw = MagicMock()
    fake_pw.firefox = MagicMock()
    fake_pw.firefox.launch_persistent_context = AsyncMock(return_value=ctx)

    async def _start() -> Any:
        return fake_pw

    fake_factory = MagicMock()
    fake_factory.start = _start
    return fake_factory


def test_ensure_calls_launch_persistent_context_with_profile(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "fp.default-release"
    profile.mkdir()
    monkeypatch.setenv("AGENTFLOW_FIREFOX_PROFILE", str(profile))
    page = _FakeNavigatorPage()
    ctx = _FakeContext(page)
    fake_factory = _fake_playwright(page, ctx)

    host = FirefoxHost()

    with patch(
        "playwright.async_api.async_playwright",
        return_value=fake_factory,
        create=True,
    ):
        # Force module-level import even if playwright isn't installed in
        # CI: the patch above creates the attribute on demand.
        import sys
        import types

        fake_module = types.ModuleType("playwright.async_api")
        fake_module.async_playwright = lambda: fake_factory  # type: ignore[attr-defined]
        sys.modules["playwright.async_api"] = fake_module
        sys.modules.setdefault("playwright", types.ModuleType("playwright"))

        result = host.ensure()

    assert result == "firefox ready"
    # Re-run the fake_factory.start() coroutine to recover the same mock
    # object the host saw (idempotent — the AsyncMock returns the same
    # MagicMock every call) and assert the call kwargs.
    fake_pw = asyncio.run(fake_factory.start())
    fake_pw.firefox.launch_persistent_context.assert_called_once()
    call_kwargs = fake_pw.firefox.launch_persistent_context.call_args.kwargs
    assert call_kwargs["user_data_dir"] == str(profile)
    assert call_kwargs["headless"] is False
    assert call_kwargs["viewport"]["width"] == 1280


def test_dispatch_firefox_tool_navigate() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.navigate.return_value = "navigated to https://x/"
    text, image = dispatch_firefox_tool(host, "firefox_navigate", {"url": "https://x/"})
    host.navigate.assert_called_once_with("https://x/")
    assert text == "navigated to https://x/"
    assert image is None


def test_dispatch_firefox_tool_snapshot_returns_image() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.snapshot.return_value = ("title: X", "BASE64DATA")
    text, image = dispatch_firefox_tool(host, "firefox_snapshot", {})
    assert text == "title: X"
    assert image == {"b64": "BASE64DATA"}


def test_dispatch_firefox_tool_click() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.click.return_value = "clicked 'button'"
    text, image = dispatch_firefox_tool(host, "firefox_click", {"selector": "button"})
    host.click.assert_called_once_with("button")
    assert "clicked" in text
    assert image is None


def test_dispatch_firefox_tool_fill() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.fill.return_value = "filled 'input' with 5 chars"
    text, _ = dispatch_firefox_tool(
        host, "firefox_fill", {"selector": "input", "text": "hello"}
    )
    host.fill.assert_called_once_with("input", "hello")
    assert "filled" in text


def test_dispatch_firefox_tool_press_and_eval() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.press.return_value = "pressed Enter"
    host.eval_js.return_value = '"result"'
    text, _ = dispatch_firefox_tool(host, "firefox_press", {"key": "Enter"})
    assert text == "pressed Enter"
    text, _ = dispatch_firefox_tool(host, "firefox_eval", {"js": "1+1"})
    host.eval_js.assert_called_once_with("1+1")
    assert text == '"result"'


def test_dispatch_firefox_tool_unknown() -> None:
    host = MagicMock(spec=FirefoxHost)
    text, _ = dispatch_firefox_tool(host, "firefox_nope", {})
    assert "unknown firefox tool" in text
