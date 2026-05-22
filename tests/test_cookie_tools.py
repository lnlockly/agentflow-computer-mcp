"""Cookie tool tests — verify deny-list, redaction, side-channel store,
and the confirm-gated paste path. Playwright is mocked so the suite runs
without a real Firefox install."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentflow_computer_mcp.driver.desktop_tools import ToolExecutor
from agentflow_computer_mcp.driver.firefox import (
    FIREFOX_TOOL_DESCRIPTORS,
    FirefoxHost,
    _domain_is_denied,
    dispatch_firefox_tool,
)


def test_deny_list_matches_known_money_domains() -> None:
    assert _domain_is_denied("binance.com")
    assert _domain_is_denied("www.binance.com")
    assert _domain_is_denied("paypal.com")
    assert _domain_is_denied("coinbase.com")
    assert _domain_is_denied("cryptobot.live")
    assert _domain_is_denied("my.bank.example")
    # Wildcards on kassa
    assert _domain_is_denied("yandex.kassa")
    # Safe domains pass through
    assert not _domain_is_denied("kwork.ru")
    assert not _domain_is_denied("chat.agentflow.website")
    assert not _domain_is_denied("github.com")


def test_get_cookies_refuses_deny_listed_domain() -> None:
    host = FirefoxHost()
    out = host.get_cookies("binance.com")
    assert out["ok"] is False
    assert "deny-list" in out["error"]


def test_export_cookies_to_refuses_deny_listed_domain() -> None:
    host = FirefoxHost()
    out = host.export_cookies_to("paypal.com", "input#x")
    assert out["ok"] is False
    assert "deny-list" in out["error"]


def _stub_host_with_cookies(raw: list[dict[str, Any]]) -> FirefoxHost:
    """Build a FirefoxHost whose `ensure()` is a no-op and whose `_context`
    returns canned cookies. The `_submit` shim runs the coroutine inline."""
    host = FirefoxHost()
    host.ensure = lambda: "firefox ready"  # type: ignore[assignment]
    host._page = MagicMock()
    host._page.fill = AsyncMock()
    host._context = MagicMock()
    host._context.cookies = AsyncMock(return_value=raw)

    def _sync_submit(coro: Any, timeout: int = 60) -> Any:
        import asyncio

        return asyncio.new_event_loop().run_until_complete(coro)

    host._submit = _sync_submit  # type: ignore[assignment]
    return host


def test_get_cookies_returns_redacted_summary_and_tokens() -> None:
    host = _stub_host_with_cookies(
        [
            {
                "name": "session",
                "value": "a" * 250,
                "domain": ".kwork.ru",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 1800000000,
            },
            {
                "name": "short",
                "value": "v",
                "domain": "kwork.ru",
                "path": "/",
            },
        ]
    )
    out = host.get_cookies("kwork.ru")
    assert out["ok"] is True
    assert out["count"] == 2
    assert len(out["tokens"]) == 2
    assert all(t.startswith("t_") for t in out["tokens"])

    long_entry = out["cookies"][0]
    assert long_entry["name"] == "session"
    assert "redacted" in long_entry["value_preview"]
    assert "a" * 250 not in json.dumps(out)

    short_entry = out["cookies"][1]
    assert short_entry["value_preview"] == "v"

    # Raw values stored side-channel, not returned to LLM.
    store = host._cookie_store()
    full = store[out["tokens"][0]]
    assert full["value"] == "a" * 250


def test_export_with_confirm_denied_does_not_fill() -> None:
    host = _stub_host_with_cookies(
        [
            {
                "name": "session",
                "value": "v",
                "domain": ".kwork.ru",
                "path": "/",
            }
        ]
    )
    exe = ToolExecutor(last_cursor_ref=[0, 0], firefox=host)
    with patch.object(exe, "_confirm_blocking", return_value=False):
        text, _ = exe.execute(
            "firefox_export_cookies_to",
            {"domain": "kwork.ru", "dest_field_selector": "textarea#cookies"},
        )
    result = json.loads(text)
    assert result["ok"] is False
    assert "denied" in result["error"]
    # fill must not have run
    host._page.fill.assert_not_called()


def test_export_with_confirm_allowed_fills_selector() -> None:
    host = _stub_host_with_cookies(
        [
            {"name": "a", "value": "1", "domain": ".kwork.ru", "path": "/"},
            {"name": "b", "value": "2", "domain": ".kwork.ru", "path": "/"},
        ]
    )
    exe = ToolExecutor(last_cursor_ref=[0, 0], firefox=host)
    with patch.object(exe, "_confirm_blocking", return_value=True):
        text, _ = exe.execute(
            "firefox_export_cookies_to",
            {"domain": "kwork.ru", "dest_field_selector": "textarea#cookies"},
        )
    result = json.loads(text)
    assert result["ok"] is True
    assert result["pasted"] == 2
    host._page.fill.assert_awaited_once()
    selector, payload = host._page.fill.await_args.args[:2]
    assert selector == "textarea#cookies"
    assert "a=1" in payload and "b=2" in payload


def test_export_netscape_format() -> None:
    host = _stub_host_with_cookies(
        [
            {
                "name": "a",
                "value": "1",
                "domain": ".kwork.ru",
                "path": "/",
                "secure": True,
                "expires": 1800000000,
            }
        ]
    )
    out = host.export_cookies_to("kwork.ru", "textarea#x", fmt="netscape")
    assert out["ok"] is True
    host._page.fill.assert_awaited_once()
    payload = host._page.fill.await_args.args[1]
    parts = payload.split("\t")
    assert parts[0] == ".kwork.ru"
    assert parts[3] == "TRUE"  # secure
    assert parts[5] == "a"
    assert parts[6] == "1"


def test_drop_cookie_tokens_clears_store() -> None:
    host = _stub_host_with_cookies(
        [{"name": "s", "value": "v", "domain": ".kwork.ru", "path": "/"}]
    )
    host.get_cookies("kwork.ru")
    assert host._cookie_store()
    dropped = host.drop_cookie_tokens()
    assert dropped["ok"] is True
    assert dropped["dropped"] >= 1
    assert host._cookie_store() == {}


def test_task_complete_auto_drops_tokens() -> None:
    host = _stub_host_with_cookies(
        [{"name": "s", "value": "v", "domain": ".kwork.ru", "path": "/"}]
    )
    host.get_cookies("kwork.ru")
    assert host._cookie_store()
    exe = ToolExecutor(last_cursor_ref=[0, 0], firefox=host)
    result, _ = exe.execute("task_complete", {"answer": "ok"})
    assert result == "__DONE__"
    assert host._cookie_store() == {}


def test_descriptors_expose_cookie_tools() -> None:
    names = {d["name"] for d in FIREFOX_TOOL_DESCRIPTORS}
    assert "firefox_get_cookies" in names
    assert "firefox_export_cookies_to" in names
    assert "firefox_drop_cookie_tokens" in names


def test_dispatch_get_cookies_routes_to_host() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.get_cookies.return_value = {"ok": True, "count": 0, "tokens": [], "cookies": []}
    text, image = dispatch_firefox_tool(host, "firefox_get_cookies", {"domain": "kwork.ru"})
    host.get_cookies.assert_called_once_with("kwork.ru")
    assert image is None
    assert json.loads(text)["ok"] is True


def test_dispatch_drop_tokens() -> None:
    host = MagicMock(spec=FirefoxHost)
    host.drop_cookie_tokens.return_value = {"ok": True, "dropped": 3}
    text, _ = dispatch_firefox_tool(host, "firefox_drop_cookie_tokens", {})
    assert json.loads(text)["dropped"] == 3


def test_dispatch_does_not_handle_export_directly() -> None:
    # Export goes through the executor (confirm dialog), not bare dispatch.
    host = MagicMock(spec=FirefoxHost)
    text, _ = dispatch_firefox_tool(
        host, "firefox_export_cookies_to", {"domain": "kwork.ru", "dest_field_selector": "x"}
    )
    assert "unknown firefox tool" in text
    host.export_cookies_to.assert_not_called()


def test_no_raw_cookie_value_in_llm_visible_payload() -> None:
    """Regression — long cookie values must not appear in the JSON the
    executor returns to the LLM."""
    host = _stub_host_with_cookies(
        [
            {
                "name": "secret",
                "value": "SUPER_SECRET_TOKEN_" + "x" * 200,
                "domain": ".kwork.ru",
                "path": "/",
            }
        ]
    )
    exe = ToolExecutor(last_cursor_ref=[0, 0], firefox=host)
    text, _ = exe.execute("firefox_get_cookies", {"domain": "kwork.ru"})
    assert "SUPER_SECRET_TOKEN" not in text
    assert "redacted" in text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
