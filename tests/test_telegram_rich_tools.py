"""Tests for the rich Telegram surfaces in AFClient + dispatcher.

Each AFClient method is exercised by monkey-patching ``_req`` so we can
inspect the (method, path, body, params) it builds and confirm the
dispatcher routes ``af_telegram_*`` tool names to the right method.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from agentflow_computer_mcp.driver.af_client import (
    AF_TOOL_DESCRIPTORS,
    AFClient,
    AFResponse,
    dispatch_af_tool,
)


def _stub_ok(body: Any = None) -> AFResponse:
    return AFResponse(ok=True, status=200, body=body)


def _capture(client: AFClient) -> dict[str, Any]:
    """Replace ``_req`` with a recorder. Returns the captured args dict
    (filled as the next ``_req`` call runs) plus its mutable status holder."""
    captured: dict[str, Any] = {}

    def fake_req(method: str, path: str, body=None, params=None) -> AFResponse:
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        captured["params"] = params
        return _stub_ok({"ok": True})

    client._req = fake_req  # type: ignore[method-assign]
    return captured


# --- telegram_dialogs ----------------------------------------------------


def test_telegram_dialogs_default_limit_25() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    r = client.telegram_dialogs()
    assert r.ok is True
    assert cap["method"] == "GET"
    assert cap["path"] == "/me/telegram/dialogs"
    assert cap["params"] == {"limit": "25"}
    assert cap["body"] is None


def test_telegram_dialogs_custom_limit() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_dialogs(limit=50)
    assert cap["params"] == {"limit": "50"}


# --- telegram_messages ---------------------------------------------------


def test_telegram_messages_passes_chat_id_and_limit() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_messages(chat_id="@durov", limit=10)
    assert cap["method"] == "GET"
    assert cap["path"] == "/me/telegram/messages"
    assert cap["params"] == {"chat_id": "@durov", "limit": "10"}


def test_telegram_messages_stringifies_numeric_chat_id() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_messages(chat_id=1361064246)
    assert cap["params"]["chat_id"] == "1361064246"
    assert cap["params"]["limit"] == "20"


# --- telegram_search -----------------------------------------------------


def test_telegram_search_global_when_chat_id_omitted() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_search(q="crypto")
    assert cap["path"] == "/me/telegram/search"
    assert cap["params"] == {"q": "crypto"}


def test_telegram_search_scoped_when_chat_id_given() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_search(q="invoice", chat_id="@boss", limit=15)
    assert cap["params"] == {"q": "invoice", "chat_id": "@boss", "limit": "15"}


def test_telegram_search_ignores_empty_chat_id() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_search(q="x", chat_id="   ")
    assert "chat_id" not in cap["params"]


# --- telegram_react ------------------------------------------------------


def test_telegram_react_posts_body() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_react(chat_id="@boss", message_id=9001, emoji="🔥")
    assert cap["method"] == "POST"
    assert cap["path"] == "/me/telegram/react"
    assert cap["body"] == {
        "chat_id": "@boss",
        "message_id": 9001,
        "emoji": "🔥",
    }


def test_telegram_react_big_flag_included_when_true() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_react(
        chat_id="me", message_id=1, emoji="👍", big=True
    )
    assert cap["body"]["big"] is True


def test_telegram_react_emoji_null_clears_reaction() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_react(chat_id="me", message_id=1, emoji=None)
    assert cap["body"]["emoji"] is None


# --- telegram_whoami -----------------------------------------------------


def test_telegram_whoami_get_no_body() -> None:
    client = AFClient(api_key="af_live_test")
    cap = _capture(client)
    client.telegram_whoami()
    assert cap["method"] == "GET"
    assert cap["path"] == "/me/telegram/whoami"
    assert cap["body"] is None
    assert cap["params"] is None


# --- dispatcher routing --------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,args,method_name",
    [
        ("af_telegram_dialogs", {"limit": 30}, "telegram_dialogs"),
        (
            "af_telegram_messages",
            {"chat_id": "@x", "limit": 5},
            "telegram_messages",
        ),
        (
            "af_telegram_search",
            {"q": "hello", "chat_id": "@x"},
            "telegram_search",
        ),
        (
            "af_telegram_react",
            {"chat_id": "@x", "message_id": 1, "emoji": "👍"},
            "telegram_react",
        ),
        ("af_telegram_whoami", {}, "telegram_whoami"),
    ],
)
def test_dispatcher_routes_each_telegram_tool(
    tool_name: str, args: dict[str, Any], method_name: str
) -> None:
    client = AFClient(api_key="af_live_test")
    called: dict[str, Any] = {}

    def fake_method(*a: Any, **kw: Any) -> AFResponse:
        called["a"] = a
        called["kw"] = kw
        return _stub_ok({"ok": True})

    setattr(client, method_name, fake_method)
    out = dispatch_af_tool(client, tool_name, args)
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert called  # method was invoked


def test_telegram_descriptors_registered() -> None:
    names = {d["name"] for d in AF_TOOL_DESCRIPTORS}
    assert {
        "af_telegram_dialogs",
        "af_telegram_messages",
        "af_telegram_search",
        "af_telegram_react",
        "af_telegram_whoami",
    }.issubset(names)
