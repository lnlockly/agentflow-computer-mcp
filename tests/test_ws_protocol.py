from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agentflow_computer_mcp.auth import build_connect_headers
from agentflow_computer_mcp.config import AppConfig, Auth, Scope
from agentflow_computer_mcp.ws_client import WSClient


def test_build_connect_headers_requires_secret_or_token() -> None:
    with pytest.raises(ValueError):
        build_connect_headers(Auth(api_key="k", device_id="d"))


def test_build_connect_headers_with_enrollment() -> None:
    h = dict(build_connect_headers(Auth(api_key="k", device_id="d", enrollment_token="t")))
    assert h["x-api-key"] == "k"
    assert h["x-device-id"] == "d"
    assert h["x-enrollment-token"] == "t"


def test_build_connect_headers_with_secret() -> None:
    h = dict(build_connect_headers(Auth(api_key="k", device_id="d", device_secret="s")))
    assert h["x-device-secret"] == "s"
    assert "x-enrollment-token" not in h


class FakeWS:
    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_tool_call_success_result_envelope() -> None:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        assert name == "computer.clipboard.read"
        return {"text": "hello"}

    client = WSClient(cfg, handler, ["computer.clipboard.read"])
    msg = {
        "type": "tool_call_request",
        "id": "abc",
        "name": "computer.clipboard.read",
        "args": {},
    }
    fake = FakeWS([json.dumps(msg)])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    await asyncio.sleep(0.05)

    payloads = [json.loads(s) for s in fake.sent]
    assert any(p.get("id") == "abc" and p.get("result") == {"text": "hello"} for p in payloads)


@pytest.mark.asyncio
async def test_tool_call_error_envelope() -> None:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        raise LookupError("nope")

    client = WSClient(cfg, handler, ["computer.clipboard.read"])
    msg = {"type": "tool_call_request", "id": "x", "name": "computer.unknown", "args": {}}
    fake = FakeWS([json.dumps(msg)])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    await asyncio.sleep(0.05)

    payloads = [json.loads(s) for s in fake.sent]
    err_msgs = [p for p in payloads if p.get("id") == "x" and "error" in p]
    assert err_msgs
    assert err_msgs[0]["error"]["code"] == "LookupError"


@pytest.mark.asyncio
async def test_malformed_message_skipped() -> None:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return {}

    client = WSClient(cfg, handler, [])
    fake = FakeWS(["not-json", json.dumps({"type": "heartbeat"})])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    assert fake.sent == []


@pytest.mark.asyncio
async def test_hello_ack_rotates_secret(tmp_path) -> None:
    cfg = AppConfig(
        scope=Scope(),
        auth=Auth(api_key="k", device_id="d", enrollment_token="enroll"),
    )

    saved: dict[str, Any] = {}

    def fake_save(a, path):
        saved["secret"] = a.device_secret
        saved["enrollment"] = a.enrollment_token

    from agentflow_computer_mcp import ws_client as ws_mod
    original_save = ws_mod.save_auth
    ws_mod.save_auth = fake_save  # type: ignore[assignment]
    try:
        async def noop_handler(n: str, a: dict[str, Any]) -> Any:
            return None

        client = WSClient(cfg, noop_handler, [])
        await client._handle_hello_ack({"type": "hello_ack", "device_secret": "newsecret"})
    finally:
        ws_mod.save_auth = original_save

    assert saved["secret"] == "newsecret"
    assert saved["enrollment"] == ""
    assert cfg.auth.device_secret == "newsecret"
