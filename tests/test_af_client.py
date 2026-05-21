from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agentflow_computer_mcp.driver.af_client import (
    AF_TOOL_DESCRIPTORS,
    AFClient,
    dispatch_af_tool,
)


def _mock_response(body: dict, status: int = 200):
    class _R:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return json.dumps(body).encode()

        def __enter__(self) -> _R:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    return _R()


def test_requires_api_key() -> None:
    with pytest.raises(ValueError):
        AFClient(api_key="")


def test_list_devices_uses_get_with_header() -> None:
    client = AFClient(api_key="af_live_test")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["header"] = req.get_header("X-api-key")
        return _mock_response({"ok": True, "items": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        r = client.list_devices()

    assert r.ok is True
    assert r.status == 200
    assert captured["url"].endswith("/me/devices")
    assert captured["method"] == "GET"
    assert captured["header"] == "af_live_test"


def test_create_project_posts_brief() -> None:
    client = AFClient(api_key="af_live_test")
    captured_body: dict[str, object] = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        if req.data:
            captured_body.update(json.loads(req.data.decode()))
        return _mock_response({"ok": True, "id": 999, "slug": "abc"}, status=201)

    with patch("urllib.request.urlopen", fake_urlopen):
        r = client.create_project("hello world")

    assert r.ok is True
    assert r.status == 201
    assert captured_body == {"brief": "hello world"}


def test_dispatch_af_tool_routes_correctly() -> None:
    client = AFClient(api_key="af_live_test")

    with patch.object(client, "list_devices") as m:
        m.return_value = type("R", (), {"ok": True, "status": 200, "body": {"items": []}, "error": None})()
        out = dispatch_af_tool(client, "af_list_devices", {})
    assert "ok" in out
    parsed = json.loads(out)
    assert parsed["ok"] is True


def test_dispatch_af_tool_unknown() -> None:
    client = AFClient(api_key="af_live_test")
    out = dispatch_af_tool(client, "af_unknown_tool", {})
    assert "unknown" in out.lower()


def test_tool_descriptors_have_required_shape() -> None:
    for desc in AF_TOOL_DESCRIPTORS:
        assert desc["name"].startswith("af_")
        assert "description" in desc
        assert desc["input_schema"]["type"] == "object"


def test_dispatch_truncates_long_body() -> None:
    client = AFClient(api_key="af_live_test")
    huge = {"items": ["x" * 200 for _ in range(50)]}
    with patch.object(client, "list_projects") as m:
        m.return_value = type("R", (), {"ok": True, "status": 200, "body": huge, "error": None})()
        out = dispatch_af_tool(client, "af_list_projects", {})
    assert len(out) <= 4020
