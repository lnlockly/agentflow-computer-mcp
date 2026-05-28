"""Unit tests for ``connect_integration_direct`` — the no-probe, no-focus
WS-dispatch flow used by ``server._dispatch_tool``.

Covers:
    - happy path: first reader returns cookies → POST + summary
    - reader chain: empty Chrome falls back to browser_cookie3
    - no cookies in any reader → ``no_cookies_found`` with reader_errors
    - provider missing → ``provider_not_found``
    - reader values never leak into the returned summary
"""
from __future__ import annotations

from typing import Any

import pytest

from agentflow_computer_mcp.driver.tools import integrations as ci

KWORK_ENTRY = {
    "slug": "kwork",
    "display_name": "Kwork",
    "cookie_domain": "kwork.ru",
    "flow_kind": "cookie_export",
    "login_url": "https://kwork.ru/",
}


def _fake_registry_get(_url: str) -> Any:
    return [KWORK_ENTRY]


@pytest.fixture(autouse=True)
def _reset_cache():
    ci._registry_cache_clear()
    yield
    ci._registry_cache_clear()


def _make_reader(name: str, cookies: list[dict[str, Any]]):
    def _reader(_domain: str) -> list[dict[str, Any]]:
        return cookies

    _reader.__name__ = name
    return _reader


def test_direct_happy_path_first_reader_wins():
    posts: list[tuple[str, dict[str, Any], str]] = []

    def fake_post(url, body, api_key):
        posts.append((url, body, api_key))
        return 200, {"ok": True, "secret_created": True}

    cookies = [
        {"name": "csrf_user_token", "value": "secret-csrf", "domain": ".kwork.ru"},
        {"name": "slrememberme", "value": "secret-remember", "domain": ".kwork.ru"},
    ]
    reader = _make_reader("chrome_keychain", cookies)

    result = ci.connect_integration_direct(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        cookie_readers=[reader],
        http_get=_fake_registry_get,
        http_post=fake_post,
    )

    assert result["ok"] is True
    assert result["provider"] == "kwork"
    assert result["cookie_count"] == 2
    assert result["browser"] == "chrome_keychain"
    assert result["secret_created"] is True
    assert posts[0][0] == "https://example.test/_agents/me/integrations/kwork"
    assert len(posts[0][1]["cookies"]) == 2
    # Cookie values must never leak into the returned summary.
    flat = repr(result)
    assert "secret-csrf" not in flat
    assert "secret-remember" not in flat


def test_direct_falls_back_to_second_reader_when_first_empty():
    posts: list[tuple[str, dict[str, Any], str]] = []

    def fake_post(url, body, api_key):
        posts.append((url, body, api_key))
        return 200, {"ok": True}

    empty = _make_reader("chrome_keychain", [])
    full = _make_reader(
        "browser_cookie3_chrome",
        [{"name": "csrf", "value": "x", "domain": ".kwork.ru"}],
    )

    result = ci.connect_integration_direct(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        cookie_readers=[empty, full],
        http_get=_fake_registry_get,
        http_post=fake_post,
    )

    assert result["ok"] is True
    assert result["browser"] == "browser_cookie3_chrome"
    assert result["cookie_count"] == 1


def test_direct_no_cookies_returns_reader_errors():
    def raising_reader(_domain: str) -> list[dict[str, Any]]:
        raise RuntimeError("keychain locked")

    raising_reader.__name__ = "chrome_keychain"
    empty = _make_reader("browser_cookie3_safari", [])

    def fake_post(*_a, **_k):
        raise AssertionError("post should not be called when no cookies")

    result = ci.connect_integration_direct(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        cookie_readers=[raising_reader, empty],
        http_get=_fake_registry_get,
        http_post=fake_post,
    )

    assert result["ok"] is False
    assert result["error"] == "no_cookies_found"
    assert any("keychain locked" in e for e in result["reader_errors"])


def test_direct_provider_not_found_lists_available():
    result = ci.connect_integration_direct(
        "telegram_app",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        cookie_readers=[_make_reader("noop", [])],
        http_get=_fake_registry_get,
    )

    assert result["ok"] is False
    assert result["error"] == "provider_not_found"
    assert result["available"] == ["kwork"]


def test_direct_backend_4xx_surfaces_error_code():
    cookies = [{"name": "x", "value": "y", "domain": ".kwork.ru"}]
    reader = _make_reader("chrome_keychain", cookies)

    def fake_post(_url, _body, _key):
        return 401, {"error": "unauthorized"}

    result = ci.connect_integration_direct(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        cookie_readers=[reader],
        http_get=_fake_registry_get,
        http_post=fake_post,
    )

    assert result["ok"] is False
    assert result["error"] == "unauthorized"
    assert result["status"] == 401


def test_direct_empty_provider_returns_error():
    result = ci.connect_integration_direct(
        "",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
    )
    assert result == {"ok": False, "error": "provider_required"}
