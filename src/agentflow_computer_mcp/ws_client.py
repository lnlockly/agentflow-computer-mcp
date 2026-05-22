from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from . import __version__
from .auth import build_connect_headers, save_auth
from .config import AUTH_FILE, AppConfig

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 15
HEARTBEAT_TIMEOUT_S = 45

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
TaskDispatchHandler = Callable[[str, str, dict[str, Any] | None], None]
StreamSubscribeHandler = Callable[[bool], None]


class WSClient:
    """Reverse-tunnel client for the AgentFlow devices WS.

    Routes server-initiated frames:
      • `tool_call_request` → tool handler (existing behavior)
      • `task_dispatch`     → `on_task_dispatch(task_id, task, scope)`
      • `subscribe_stream`  → `on_stream_subscribe(True)`
      • `unsubscribe_stream` → `on_stream_subscribe(False)`

    Other modules (AI loop, capture loop) can push outbound frames via
    `publish(payload)` — a thread-safe schedule onto the WS event loop.
    """

    def __init__(
        self,
        config: AppConfig,
        tool_handler: ToolHandler,
        tool_names: list[str],
        *,
        on_task_dispatch: TaskDispatchHandler | None = None,
        on_stream_subscribe: StreamSubscribeHandler | None = None,
    ) -> None:
        self._config = config
        self._handler = tool_handler
        self._tool_names = tool_names
        self._on_task_dispatch = on_task_dispatch
        self._on_stream_subscribe = on_stream_subscribe
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._last_recv_ts: float = 0.0
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0
            except Exception as exc:
                log.warning("ws session ended: %s", exc)
                sleep_s = min(backoff, 60) + random.uniform(0, 0.5)
                log.info("reconnecting in %.1fs", sleep_s)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
                backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stop.set()

    # ─────────── thread-safe outbound bridge ───────────
    def publish(self, payload: dict[str, Any]) -> None:
        """Schedule `payload` to be sent over the WS from any thread.

        Safe to call from non-asyncio threads. Drops the frame if the WS is
        not currently open. Never raises.
        """
        loop = self._loop
        ws = self._ws
        if loop is None or ws is None:
            return
        try:
            loop.call_soon_threadsafe(asyncio.create_task, self._send_safely(payload))
        except RuntimeError:
            # event loop closed mid-shutdown
            return

    async def _send_safely(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            log.debug("ws publish dropped: %s", exc)

    async def _connect_once(self) -> None:
        auth = self._config.auth
        headers = build_connect_headers(auth)
        log.info("connecting to %s as device=%s", auth.ws_url, auth.device_id)

        async with websockets.connect(
            auth.ws_url,
            additional_headers=headers,
            ping_interval=None,
            max_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            self._last_recv_ts = time.time()

            hello = {
                "type": "hello",
                "device_id": auth.device_id,
                "version": __version__,
                "tools": self._tool_names,
            }
            await ws.send(json.dumps(hello))

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            try:
                await self._recv_loop()
            finally:
                heartbeat_task.cancel()
                with suppress_cancelled():
                    await heartbeat_task
                # On disconnect, drop the stream subscription so the capture
                # loop stops emitting WS frames into the void.
                if self._on_stream_subscribe is not None:
                    with contextlib.suppress(Exception):
                        self._on_stream_subscribe(False)
                self._ws = None

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            self._last_recv_ts = time.time()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                log.warning("malformed json: %s", exc)
                continue

            mtype = msg.get("type")
            if mtype == "heartbeat":
                continue
            if mtype == "hello_ack":
                await self._handle_hello_ack(msg)
                continue
            if mtype == "tool_call_request":
                asyncio.create_task(self._handle_tool_call(msg))
                continue
            if mtype == "task_dispatch":
                self._handle_task_dispatch(msg)
                continue
            if mtype == "subscribe_stream":
                self._handle_stream_subscription(True)
                continue
            if mtype == "unsubscribe_stream":
                self._handle_stream_subscription(False)
                continue
            log.debug("unknown message type: %s", mtype)

    def _handle_task_dispatch(self, msg: dict[str, Any]) -> None:
        task_id = str(msg.get("id") or "").strip()
        task = str(msg.get("task") or "").strip()
        scope = msg.get("scope") if isinstance(msg.get("scope"), dict) else None
        if not task_id or not task:
            log.warning("task_dispatch missing id/task: %s", msg)
            return
        if self._on_task_dispatch is None:
            log.warning("task_dispatch received but no handler registered")
            return
        try:
            self._on_task_dispatch(task_id, task, scope)
        except Exception as exc:  # noqa: BLE001
            log.exception("task_dispatch handler failed: %s", exc)

    def _handle_stream_subscription(self, subscribe: bool) -> None:
        if self._on_stream_subscribe is None:
            log.debug("stream subscription message ignored (no handler)")
            return
        try:
            self._on_stream_subscribe(subscribe)
        except Exception as exc:  # noqa: BLE001
            log.exception("stream subscribe handler failed: %s", exc)

    async def _handle_hello_ack(self, msg: dict[str, Any]) -> None:
        new_secret = msg.get("device_secret")
        if new_secret and new_secret != self._config.auth.device_secret:
            self._config.auth.device_secret = new_secret
            self._config.auth.enrollment_token = ""
            save_auth(self._config.auth, AUTH_FILE)
            log.info("device_secret rotated and saved")

    async def _handle_tool_call(self, msg: dict[str, Any]) -> None:
        call_id = msg.get("id", "")
        name = msg.get("name", "")
        args = msg.get("args", {}) or {}

        try:
            result = await self._handler(name, args)
            await self._send({"type": "tool_call_result", "id": call_id, "result": result})
        except Exception as exc:
            err_code = getattr(exc, "code", None) or type(exc).__name__
            await self._send({
                "type": "tool_call_result",
                "id": call_id,
                "error": {"code": err_code, "message": str(exc)},
            })

    async def _heartbeat_loop(self) -> None:
        assert self._ws is not None
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                if time.time() - self._last_recv_ts > HEARTBEAT_TIMEOUT_S:
                    log.warning("heartbeat timeout — closing")
                    await self._ws.close(code=1011, reason="heartbeat_timeout")
                    return
                await self._send({"type": "heartbeat", "ts": int(time.time() * 1000)})
        except ConnectionClosed:
            return

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))


class suppress_cancelled:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is asyncio.CancelledError

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return exc_type is asyncio.CancelledError
