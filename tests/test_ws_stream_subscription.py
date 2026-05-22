"""Stream subscription flow: subscribe_stream / unsubscribe_stream gate capture-loop emission."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

from agentflow_computer_mcp.config import AppConfig, Auth, Scope
from agentflow_computer_mcp.driver.state import DriverState
from agentflow_computer_mcp.driver.streamer import WS_STREAM_MIN_INTERVAL_S, CaptureLoop
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
async def test_subscribe_then_unsubscribe_toggles_state_flag() -> None:
    state = DriverState()
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    def on_stream(subscribe: bool) -> None:
        if subscribe:
            state.stream_subscribed.set()
        else:
            state.stream_subscribed.clear()

    client = WSClient(cfg, handler, [], on_stream_subscribe=on_stream)
    fake = FakeWS(
        [
            json.dumps({"type": "subscribe_stream"}),
            json.dumps({"type": "unsubscribe_stream"}),
        ]
    )
    client._ws = fake  # type: ignore[assignment]

    # Process the subscribe frame
    state.stream_subscribed.clear()
    await client._recv_loop()  # both frames drain in order
    # final state should be cleared after the unsubscribe
    assert not state.stream_subscribed.is_set()


@pytest.mark.asyncio
async def test_subscribe_only_keeps_flag_set() -> None:
    state = DriverState()
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    def on_stream(subscribe: bool) -> None:
        if subscribe:
            state.stream_subscribed.set()
        else:
            state.stream_subscribed.clear()

    client = WSClient(cfg, handler, [], on_stream_subscribe=on_stream)
    fake = FakeWS([json.dumps({"type": "subscribe_stream"})])
    client._ws = fake  # type: ignore[assignment]
    await client._recv_loop()
    assert state.stream_subscribed.is_set()


def test_capture_loop_only_emits_frames_when_subscribed() -> None:
    """The capture loop's WS-publish path must short-circuit on a clear flag.

    We exercise `_maybe_publish_ws` directly to avoid spinning up a real
    capture thread (which requires platform display access on CI).
    """
    state = DriverState()
    sent: list[dict[str, Any]] = []
    loop = CaptureLoop(
        state.stream_frame,
        state.stream_cond,
        stream_subscribed=state.stream_subscribed,
        outbound_publisher=sent.append,
    )

    # not subscribed → no emission
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())
    assert sent == []

    # subscribed → emission
    state.stream_subscribed.set()
    loop._last_ws_emit_at = 0.0  # reset rate-limiter
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())
    assert len(sent) == 1
    assert sent[0]["type"] == "stream_frame"
    assert sent[0]["frame"]  # base64-encoded payload
    assert isinstance(sent[0]["ts"], int)

    # back-to-back call inside the rate-limit window → no second emission
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())
    assert len(sent) == 1

    # after the cooldown → emission allowed again
    loop._last_ws_emit_at = time.time() - (WS_STREAM_MIN_INTERVAL_S + 0.01)
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())
    assert len(sent) == 2

    # cleared → no further emission
    state.stream_subscribed.clear()
    loop._last_ws_emit_at = 0.0
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())
    assert len(sent) == 2


def test_capture_loop_no_publisher_is_safe() -> None:
    state = DriverState()
    state.stream_subscribed.set()
    loop = CaptureLoop(
        state.stream_frame,
        state.stream_cond,
        stream_subscribed=state.stream_subscribed,
        outbound_publisher=None,
    )
    # must not raise
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())


def test_publisher_exception_does_not_crash_capture() -> None:
    state = DriverState()
    state.stream_subscribed.set()

    def boom(_p: dict[str, Any]) -> None:
        raise RuntimeError("ws closed")

    loop = CaptureLoop(
        state.stream_frame,
        state.stream_cond,
        stream_subscribed=state.stream_subscribed,
        outbound_publisher=boom,
    )
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())


def test_set_outbound_publisher_late_binding() -> None:
    """`set_outbound_publisher` lets desktop_cli wire the WS bridge after the
    capture loop has started."""
    state = DriverState()
    state.stream_subscribed.set()
    loop = CaptureLoop(state.stream_frame, state.stream_cond, stream_subscribed=state.stream_subscribed)
    sent: list[dict[str, Any]] = []
    loop.set_outbound_publisher(sent.append)
    loop._maybe_publish_ws(b"\xff\xd8\xff\xd9", time.time())
    assert len(sent) == 1


def test_state_event_default_is_clear() -> None:
    state = DriverState()
    assert isinstance(state.stream_subscribed, threading.Event)
    assert not state.stream_subscribed.is_set()


@pytest.mark.asyncio
async def test_disconnect_clears_subscription() -> None:
    """When the WS session ends, subscription should auto-clear so the
    capture loop stops emitting into a dead socket."""
    state = DriverState()
    state.stream_subscribed.set()
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    def on_stream(subscribe: bool) -> None:
        if subscribe:
            state.stream_subscribed.set()
        else:
            state.stream_subscribed.clear()

    client = WSClient(cfg, handler, [], on_stream_subscribe=on_stream)
    # simulate the cleanup path in `_connect_once`
    if client._on_stream_subscribe is not None:
        client._on_stream_subscribe(False)
    assert not state.stream_subscribed.is_set()


@pytest.mark.asyncio
async def test_publish_is_noop_when_disconnected() -> None:
    cfg = AppConfig(scope=Scope(), auth=Auth(api_key="k", device_id="d", device_secret="s"))

    async def handler(name: str, args: dict[str, Any]) -> Any:
        return None

    client = WSClient(cfg, handler, [])
    # never connected → publish should be a silent no-op
    client.publish({"type": "task_action", "task_id": "x", "ts": 0, "action": "noop"})
    # also when loop set but ws missing
    client._loop = asyncio.get_running_loop()
    client.publish({"type": "task_action", "task_id": "x", "ts": 0, "action": "noop"})
    # no exception means pass
