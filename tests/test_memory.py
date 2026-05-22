"""Memory tool tests (af_remember / af_recall).

We mock AFClient.remember + AFClient.recall directly so the suite never
talks to the real REST API. The goal is to pin the dispatcher's argument
mapping and the device_id default-resolution rule.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from agentflow_computer_mcp.driver.af_client import (
    AF_TOOL_DESCRIPTORS,
    AFClient,
    dispatch_af_tool,
)


def _resp(body: dict, status: int = 201, ok: bool = True):
    return type("R", (), {"ok": ok, "status": status, "body": body, "error": None})()


def test_descriptors_include_memory_tools() -> None:
    names = {d["name"] for d in AF_TOOL_DESCRIPTORS}
    assert "af_remember" in names
    assert "af_recall" in names
    remember = next(d for d in AF_TOOL_DESCRIPTORS if d["name"] == "af_remember")
    assert "kind" in remember["input_schema"]["properties"]
    assert "tags" in remember["input_schema"]["properties"]
    assert remember["input_schema"]["required"] == ["kind", "text"]


def test_af_remember_uses_client_device_id_when_omitted() -> None:
    client = AFClient(api_key="af_live_test", device_id="dev-uuid-1")
    client.remember = MagicMock(return_value=_resp({"ok": True}))

    out = dispatch_af_tool(
        client,
        "af_remember",
        {"kind": "lesson", "text": "X worked", "tags": ["kwork"]},
    )

    client.remember.assert_called_once_with(
        device_id="dev-uuid-1",
        kind="lesson",
        text="X worked",
        tags=["kwork"],
    )
    parsed = json.loads(out)
    assert parsed["ok"] is True


def test_af_remember_explicit_device_id_overrides_default() -> None:
    client = AFClient(api_key="af_live_test", device_id="default-id")
    client.remember = MagicMock(return_value=_resp({"ok": True}))

    dispatch_af_tool(
        client,
        "af_remember",
        {
            "device_id": "other-id",
            "kind": "fact",
            "text": "kwork uses captcha",
            "tags": [],
        },
    )

    client.remember.assert_called_once_with(
        device_id="other-id",
        kind="fact",
        text="kwork uses captcha",
        tags=[],
    )


def test_af_remember_errors_when_no_device_id() -> None:
    client = AFClient(api_key="af_live_test")  # no device_id
    client.remember = MagicMock()

    out = dispatch_af_tool(
        client,
        "af_remember",
        {"kind": "lesson", "text": "X"},
    )

    client.remember.assert_not_called()
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "device_id" in parsed["error"]


def test_af_recall_passes_tags_and_limit() -> None:
    client = AFClient(api_key="af_live_test", device_id="dev-1")
    client.recall = MagicMock(
        return_value=_resp({"ok": True, "items": [{"id": 1, "text": "y"}]}, status=200)
    )

    dispatch_af_tool(
        client,
        "af_recall",
        {"tags": ["kwork", "offer"], "limit": 10},
    )

    client.recall.assert_called_once_with(
        device_id="dev-1",
        tags=["kwork", "offer"],
        limit=10,
        kind=None,
    )


def test_af_recall_kind_filter_forwarded() -> None:
    client = AFClient(api_key="af_live_test", device_id="dev-1")
    client.recall = MagicMock(return_value=_resp({"ok": True, "items": []}, status=200))

    dispatch_af_tool(
        client,
        "af_recall",
        {"tags": [], "limit": 50, "kind": "lesson"},
    )

    client.recall.assert_called_once_with(
        device_id="dev-1",
        tags=[],
        limit=50,
        kind="lesson",
    )


def test_remember_builds_correct_url(monkeypatch) -> None:
    client = AFClient(api_key="af_live_test", device_id="dev-1")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode())

        class _R:
            status = 201

            def read(self) -> bytes:
                return json.dumps({"ok": True}).encode()

            def __enter__(self) -> _R:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = client.remember("dev-xyz", kind="lesson", text="t", tags=["a"])

    assert r.ok is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/me/devices/dev-xyz/memories")
    assert captured["body"] == {"kind": "lesson", "text": "t", "tags": ["a"]}


def test_recall_builds_query_params(monkeypatch) -> None:
    client = AFClient(api_key="af_live_test", device_id="dev-1")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()

        class _R:
            status = 200

            def read(self) -> bytes:
                return json.dumps({"ok": True, "items": []}).encode()

            def __enter__(self) -> _R:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client.recall("dev-xyz", tags=["kwork", "earn"], limit=25, kind="lesson")

    assert captured["method"] == "GET"
    assert "/me/devices/dev-xyz/memories?" in captured["url"]
    assert "tags=kwork%2Cearn" in captured["url"]
    assert "limit=25" in captured["url"]
    assert "kind=lesson" in captured["url"]
