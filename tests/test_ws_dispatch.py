"""WS server→client routing: task_dispatch, subscribe_stream, unsubscribe_stream."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agentflow_computer_mcp.config import AppConfig, Auth, Scope
from agentflow_computer_mcp.driver.state import DriverState
from agentflow_computer_mcp.ws_client import WSClient


class FakeWS:
    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = list(incoming)

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


@pytest.mark.asyncio
async def test_task_dispatch_routes_into_driver_state_queue() -> None:
    state = DriverState()
    cfg = AppConfig(
        scope=Scope(),
        auth=Auth(api_key="k", device_id="d", device_secret="s"),
    )

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    def on_task_dispatch(task_id: str, task: str, scope: dict[str, Any] | None) -> None:
        state.enqueue_task(task, task_id)

    client = WSClient(
        cfg, handler, ["computer.clipboard.read"], on_task_dispatch=on_task_dispatch
    )
    frame = {"type": "task_dispatch", "id": "t-123", "task": "open Safari and search for AgentFlow"}
    fake = FakeWS([json.dumps(frame)])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    await asyncio.sleep(0.01)

    assert state.task_queue.qsize() == 1
    queued = state.task_queue.get_nowait()
    assert queued == ("t-123", "open Safari and search for AgentFlow")


@pytest.mark.asyncio
async def test_task_dispatch_missing_fields_is_ignored() -> None:
    state = DriverState()
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    calls: list[tuple[str, str]] = []

    def on_task_dispatch(task_id: str, task: str, scope: dict[str, Any] | None) -> None:
        calls.append((task_id, task))

    client = WSClient(cfg, handler, [], on_task_dispatch=on_task_dispatch)
    fake = FakeWS(
        [
            json.dumps({"type": "task_dispatch", "id": "", "task": "x"}),
            json.dumps({"type": "task_dispatch", "id": "y", "task": ""}),
        ]
    )
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()
    assert calls == []
    assert state.task_queue.empty()


@pytest.mark.asyncio
async def test_unknown_type_is_ignored() -> None:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    client = WSClient(cfg, handler, [])
    fake = FakeWS([json.dumps({"type": "made_up_thing", "x": 1})])
    client._ws = fake  # type: ignore[assignment]
    await client._recv_loop()
    assert fake.sent == []
