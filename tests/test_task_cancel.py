"""Tests for daemon-side task_cancel handling.

Covers:
  - WSClient._handle_task_cancel routes to callback with correct task_id.
  - DriverState.request_abort sets abort_flag only when ids match (or task_id is None).
  - run_task loop checks abort_flag at iteration boundary and emits task_error.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentflow_computer_mcp.config import AppConfig, Auth, Scope
from agentflow_computer_mcp.driver.state import DriverState
from agentflow_computer_mcp.ws_client import WSClient

# ─── helpers shared with test_ws_dispatch ────────────────────────────────────

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


def _make_client(on_task_cancel=None) -> WSClient:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    return WSClient(cfg, handler, [], on_task_cancel=on_task_cancel)


# ─── WSClient._handle_task_cancel ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_task_cancel_with_task_id_calls_callback() -> None:
    """task_cancel frame with task_id → callback receives that id."""
    received: list[str | None] = []

    client = _make_client(on_task_cancel=received.append)
    frame = {"type": "task_cancel", "task_id": "t-42"}
    fake = FakeWS([json.dumps(frame)])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()

    assert received == ["t-42"]


@pytest.mark.asyncio
async def test_handle_task_cancel_without_task_id_calls_callback_with_none() -> None:
    """task_cancel frame with no task_id → callback receives None."""
    received: list[str | None] = []

    client = _make_client(on_task_cancel=received.append)
    frame = {"type": "task_cancel"}  # no task_id field
    fake = FakeWS([json.dumps(frame)])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()

    assert received == [None]


@pytest.mark.asyncio
async def test_handle_task_cancel_no_handler_is_noop() -> None:
    """task_cancel with no registered callback must not raise."""
    client = _make_client(on_task_cancel=None)
    frame = {"type": "task_cancel", "task_id": "t-99"}
    fake = FakeWS([json.dumps(frame)])
    client._ws = fake  # type: ignore[assignment]

    await client._recv_loop()  # should not raise


# ─── DriverState.request_abort ───────────────────────────────────────────────

def test_request_abort_sets_flag_when_ids_match() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-1"

    state.request_abort("t-1")

    assert state.abort_flag.is_set()


def test_request_abort_unconditional_when_task_id_none() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-1"

    state.request_abort(None)

    assert state.abort_flag.is_set()


def test_request_abort_ignores_mismatched_id() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-1"

    state.request_abort("t-999")

    assert not state.abort_flag.is_set()


def test_request_abort_noop_when_not_busy() -> None:
    state = DriverState()
    # state.busy defaults to False, current_task_id = ""

    state.request_abort(None)

    # Flag stays clear; no exception
    assert not state.abort_flag.is_set()


# ─── run_task abort path ──────────────────────────────────────────────────────

def test_run_task_aborts_between_iterations_and_emits_task_error() -> None:
    """With abort_flag pre-set, run_task exits on first iteration check."""
    from agentflow_computer_mcp.driver.loop import run_task

    state = DriverState()
    state.busy = True
    state.current_task_id = "t-cancel-1"
    state.abort_flag.set()

    published: list[dict[str, Any]] = []
    state.outbound_publisher = published.append

    executor = MagicMock()

    # post_llm should never be called because the flag is checked before the
    # first LLM call.
    with patch("agentflow_computer_mcp.driver.loop.post_llm") as mock_llm:
        result = run_task("do something", state, executor, api_key="k")

    mock_llm.assert_not_called()
    assert result == ""
    assert not state.abort_flag.is_set(), "abort_flag must be cleared after cancel"

    # Exactly one task_error frame with error=cancelled_by_user
    assert len(published) >= 1
    cancel_frames = [f for f in published if f.get("type") == "task_error"]
    assert cancel_frames, "expected a task_error frame"
    assert cancel_frames[0]["error"] == "cancelled_by_user"
    assert cancel_frames[0]["task_id"] == "t-cancel-1"


def test_run_task_abort_flag_cleared_even_without_current_task_id() -> None:
    """Abort with no current_task_id: no WS frame emitted, flag still cleared."""
    from agentflow_computer_mcp.driver.loop import run_task

    state = DriverState()
    state.busy = True
    state.current_task_id = ""  # local task — no WS task id
    state.abort_flag.set()

    published: list[dict[str, Any]] = []
    state.outbound_publisher = published.append

    executor = MagicMock()

    with patch("agentflow_computer_mcp.driver.loop.post_llm"):
        result = run_task("do something", state, executor, api_key="k")

    assert result == ""
    assert not state.abort_flag.is_set()
    # No task_error frame when there is no task_id to attach it to
    assert not any(f.get("type") == "task_error" for f in published)
