"""Tests for the user-editable Skills block path.

The daemon fetches `/me/devices/skills/prompt-block` from agentflow-agents
on every task and prepends the result to its system prompt so user-defined
phrase → action mappings win over the hardcoded intent_map. A missing or
broken endpoint must never block task execution; the helper returns "".
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from agentflow_computer_mcp.driver.af_client import AFClient, AFResponse
from agentflow_computer_mcp.driver.loop import _fetch_skills_prompt_block


def _mock_resp(body: dict[str, Any], status: int = 200):
    class _R:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return json.dumps(body).encode()

        def __enter__(self) -> "_R":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    return _R()


def test_get_skills_prompt_block_hits_correct_path() -> None:
    client = AFClient("af_live_test")
    with patch(
        "agentflow_computer_mcp.driver.af_client.urllib.request.urlopen",
        return_value=_mock_resp({"block": "• A → B"}),
    ) as mock_open:
        resp = client.get_skills_prompt_block(device_id="dev-1")
    req = mock_open.call_args[0][0]
    assert "/me/devices/skills/prompt-block" in req.full_url
    assert "device_id=dev-1" in req.full_url
    assert resp.ok
    assert resp.body == {"block": "• A → B"}


def test_get_skills_prompt_block_without_device_id_omits_param() -> None:
    client = AFClient("af_live_test")
    with patch(
        "agentflow_computer_mcp.driver.af_client.urllib.request.urlopen",
        return_value=_mock_resp({"block": ""}),
    ) as mock_open:
        client.get_skills_prompt_block()
    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/prompt-block")


def test_fetch_returns_trimmed_block_on_success() -> None:
    class StubClient:
        def get_skills_prompt_block(self) -> AFResponse:
            return AFResponse(ok=True, status=200, body={"block": "  • X → Y  \n"})

    assert _fetch_skills_prompt_block(StubClient()) == "• X → Y"


def test_fetch_returns_empty_on_http_error() -> None:
    class StubClient:
        def get_skills_prompt_block(self) -> AFResponse:
            return AFResponse(ok=False, status=503, body=None, error="upstream down")

    assert _fetch_skills_prompt_block(StubClient()) == ""


def test_fetch_returns_empty_on_malformed_body() -> None:
    class StubClient:
        def get_skills_prompt_block(self) -> AFResponse:
            return AFResponse(ok=True, status=200, body="not a dict")

    assert _fetch_skills_prompt_block(StubClient()) == ""


def test_fetch_returns_empty_when_block_missing() -> None:
    class StubClient:
        def get_skills_prompt_block(self) -> AFResponse:
            return AFResponse(ok=True, status=200, body={"other": "field"})

    assert _fetch_skills_prompt_block(StubClient()) == ""


def test_fetch_swallows_network_exception() -> None:
    class StubClient:
        def get_skills_prompt_block(self) -> AFResponse:
            raise RuntimeError("network unreachable")

    assert _fetch_skills_prompt_block(StubClient()) == ""
