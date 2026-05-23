"""WS task_dispatch passes agent_id to multi-agent-aware handlers."""
from __future__ import annotations

from typing import Any

from agentflow_computer_mcp.config import AppConfig, Auth, Scope
from agentflow_computer_mcp.ws_client import WSClient


def _build_client(handler: Any) -> WSClient:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def tool_handler(name: str, args: dict[str, Any]) -> Any:
        return {}

    return WSClient(cfg, tool_handler, ["computer.screen.capture"], on_task_dispatch=handler)


def test_legacy_three_arg_handler_still_called() -> None:
    seen: list[tuple] = []

    def handler(task_id: str, task: str, scope: dict | None) -> None:
        seen.append((task_id, task, scope))

    client = _build_client(handler)
    client._handle_task_dispatch(
        {"type": "task_dispatch", "id": "t1", "task": "x", "scope": {"k": 1}, "agent_id": "trader"}
    )
    assert seen == [("t1", "x", {"k": 1})]


def test_four_arg_handler_receives_agent_id() -> None:
    seen: list[tuple] = []

    def handler(task_id: str, task: str, scope: dict | None, agent_id: str) -> None:
        seen.append((task_id, task, scope, agent_id))

    client = _build_client(handler)
    client._handle_task_dispatch(
        {"type": "task_dispatch", "id": "t1", "task": "x", "scope": None, "agent_id": "trader"}
    )
    assert seen == [("t1", "x", None, "trader")]


def test_missing_agent_id_becomes_empty_string() -> None:
    seen: list[tuple] = []

    def handler(task_id: str, task: str, scope: dict | None, agent_id: str) -> None:
        seen.append((task_id, task, scope, agent_id))

    client = _build_client(handler)
    client._handle_task_dispatch({"type": "task_dispatch", "id": "t1", "task": "x"})
    assert seen == [("t1", "x", None, "")]
