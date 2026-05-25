"""Unit tests for the registry-driven ``connect_integration`` tool.

The real flow touches macOS Chrome + the AgentFlow backend. Tests fake
the registry HTTP fetch, the Chrome helpers, and the backend POST, then
drive ``connect_integration`` to assert:
    - happy path: cookies exported and POSTed, summary shape correct
    - provider missing from registry → ``provider_not_found``
    - probe says not logged in → ``not_logged_in`` (no export, no POST)
    - registry 5-minute cache: a second call within TTL skips the HTTP
"""
from __future__ import annotations

from typing import Any

import pytest

from agentflow_computer_mcp.driver.tools import integrations as ci


KWORK_ENTRY = {
    "slug": "kwork",
    "display_name": "Kwork",
    "emoji": "💼",
    "cookie_domain": "kwork.ru",
    "login_url": "https://kwork.ru/manage_offers",
    "logged_probe_js": "({logged: true, url: location.href})",
    "secret_key": "KWORK_SESSION_JSON",
    "flow_kind": "cookie_export",
}


def _fake_registry_get(_url: str) -> Any:
    return [KWORK_ENTRY]


def _fake_chrome_open(url: str, _new_tab: bool = True) -> str:
    return f"opened {url}"


def _logged_in_eval(_js: str, _tab: Any = None) -> str:
    return '{"logged": true, "url": "https://kwork.ru/manage_offers"}'


def _logged_out_eval(_js: str, _tab: Any = None) -> str:
    return '{"logged": false, "url": "https://kwork.ru/login"}'


def _fake_export(_domain: str, _profile: str = "Default") -> dict[str, Any]:
    return {
        "ok": True,
        "domain": "kwork.ru",
        "cookies": [
            {"name": "ws-session", "value": "redacted", "domain": ".kwork.ru"},
            {"name": "csrf", "value": "redacted", "domain": ".kwork.ru"},
        ],
        "profile": "Default",
    }


@pytest.fixture(autouse=True)
def _reset_cache():
    ci._registry_cache_clear()
    yield
    ci._registry_cache_clear()


def test_happy_path_posts_cookies_and_returns_summary():
    posts: list[tuple[str, dict[str, Any], str]] = []

    def fake_post(url: str, body: dict[str, Any], api_key: str):
        posts.append((url, body, api_key))
        return 200, {"ok": True, "secret_created": True}

    result = ci.connect_integration(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        chrome_open_url=_fake_chrome_open,
        chrome_eval=_logged_in_eval,
        chrome_export_cookies=_fake_export,
        sleep=lambda _s: None,
        http_get=_fake_registry_get,
        http_post=fake_post,
    )

    assert result["ok"] is True
    assert result["provider"] == "kwork"
    assert result["cookie_count"] == 2
    assert result["secret_created"] is True
    assert posts and posts[0][0] == "https://example.test/_agents/me/integrations/kwork"
    posted_body = posts[0][1]
    assert "cookies" in posted_body and len(posted_body["cookies"]) == 2
    assert posts[0][2] == "af_live_test"
    # Cookie values must NOT appear in the returned summary.
    flat = repr(result)
    assert "redacted" not in flat


def test_provider_not_found_returns_available_list():
    result = ci.connect_integration(
        "telegram_app",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        chrome_open_url=_fake_chrome_open,
        chrome_eval=_logged_in_eval,
        chrome_export_cookies=_fake_export,
        sleep=lambda _s: None,
        http_get=_fake_registry_get,
    )

    assert result["ok"] is False
    assert result["error"] == "provider_not_found"
    assert result["available"] == ["kwork"]


def test_not_logged_in_skips_export_and_post():
    export_calls: list[Any] = []
    post_calls: list[Any] = []

    def fake_export(domain, profile="Default"):
        export_calls.append((domain, profile))
        return _fake_export(domain, profile)

    def fake_post(url, body, api_key):
        post_calls.append((url, body, api_key))
        return 200, {"ok": True}

    result = ci.connect_integration(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        chrome_open_url=_fake_chrome_open,
        chrome_eval=_logged_out_eval,
        chrome_export_cookies=fake_export,
        sleep=lambda _s: None,
        http_get=_fake_registry_get,
        http_post=fake_post,
    )

    assert result["ok"] is False
    assert result["error"] == "not_logged_in"
    assert "hint" in result and "Kwork" in result["hint"]
    assert export_calls == [], "export must not run when probe says logged-out"
    assert post_calls == [], "POST must not fire when probe says logged-out"


def test_registry_cache_skips_repeat_http_within_ttl():
    calls: list[str] = []

    def counting_get(url: str):
        calls.append(url)
        return [KWORK_ENTRY]

    # Fixed clock so we stay inside the TTL window.
    clock = [0.0]

    def now():
        return clock[0]

    ci.fetch_registry(
        api_base="https://example.test/_agents",
        http_get=counting_get,
        now=now,
    )
    clock[0] = 60.0  # 1 minute later, still inside 300s TTL
    ci.fetch_registry(
        api_base="https://example.test/_agents",
        http_get=counting_get,
        now=now,
    )
    assert len(calls) == 1, "second call within TTL must hit the cache"

    clock[0] = 1000.0  # past TTL
    ci.fetch_registry(
        api_base="https://example.test/_agents",
        http_get=counting_get,
        now=now,
    )
    assert len(calls) == 2, "stale cache must refetch"


def test_telegram_app_flow_returns_not_implemented():
    tg_entry = {
        "slug": "telegram_app",
        "display_name": "Telegram",
        "flow_kind": "telegram_app",
    }
    result = ci.connect_integration(
        "telegram_app",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        chrome_open_url=_fake_chrome_open,
        chrome_eval=_logged_in_eval,
        chrome_export_cookies=_fake_export,
        sleep=lambda _s: None,
        http_get=lambda _u: [tg_entry],
    )
    assert result["ok"] is False
    assert result["error"] == "not_implemented"


def test_backend_error_propagates_verbatim():
    def fake_post(_url, _body, _key):
        return 403, {"error": "domain_denied"}

    result = ci.connect_integration(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        chrome_open_url=_fake_chrome_open,
        chrome_eval=_logged_in_eval,
        chrome_export_cookies=_fake_export,
        sleep=lambda _s: None,
        http_get=_fake_registry_get,
        http_post=fake_post,
    )
    assert result["ok"] is False
    assert result["error"] == "domain_denied"
    assert result["status"] == 403


def test_registry_unavailable_when_http_fails():
    def broken_get(_url):
        raise RuntimeError("connection reset")

    result = ci.connect_integration(
        "kwork",
        api_key="af_live_test",
        api_base="https://example.test/_agents",
        chrome_open_url=_fake_chrome_open,
        chrome_eval=_logged_in_eval,
        chrome_export_cookies=_fake_export,
        sleep=lambda _s: None,
        http_get=broken_get,
    )
    assert result["ok"] is False
    assert result["error"] == "registry_unavailable"
