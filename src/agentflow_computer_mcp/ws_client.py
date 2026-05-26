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
from .health import get_health
from .ws_log_uploader import get_handler as get_ws_log_handler

# Imported lazily inside the handler to keep `from .ws_client import …` cheap
# for the test suite (auto_updater pulls in urllib + hashlib at import).
_CHECK_AND_APPLY_ONCE: Callable[..., dict[str, Any]] | None = None

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 15
HEARTBEAT_TIMEOUT_S = 45
WS_OPEN_TIMEOUT_S = 30
RECONNECT_BACKOFF_CAP_S = 30
DEGRADED_FAILURE_THRESHOLD = 5

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
TaskDispatchHandler = Callable[[str, str, dict[str, Any] | None], None]
StreamSubscribeHandler = Callable[[bool], None]


class WSClient:
    """Reverse-tunnel client for the AgentFlow devices WS.

    Routes server-initiated frames:
      • `tool_call_request` → tool handler (existing behavior)
      • `task_dispatch`     → `on_task_dispatch(task_id, task, scope)`
      • `task_cancel`       → `on_task_cancel(task_id?)`
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
        on_task_cancel: Callable[[str | None], None] | None = None,
    ) -> None:
        self._config = config
        self._handler = tool_handler
        self._tool_names = tool_names
        self._on_task_dispatch = on_task_dispatch
        self._on_stream_subscribe = on_stream_subscribe
        self._on_task_cancel = on_task_cancel
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._last_recv_ts: float = 0.0
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handshake_completed: bool = False

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        health = get_health()
        backoff = 1.0
        degraded_logged = False
        while not self._stop.is_set():
            health.mark_connecting()
            self._handshake_completed = False
            try:
                await self._connect_once()
                backoff = 1.0
                degraded_logged = False
            except Exception as exc:
                log.warning("ws session ended: %s", exc)
                # If the previous session completed the handshake, treat this as
                # a fresh failure cycle: start backoff at 1s rather than doubling
                # whatever value was left over from an earlier outage.
                if self._handshake_completed:
                    backoff = 1.0
                health.mark_reconnecting(str(exc))
                if (
                    health.consecutive_failures >= DEGRADED_FAILURE_THRESHOLD
                    and not degraded_logged
                ):
                    log.warning(
                        "ws degraded — restart recommended (%d consecutive failures)",
                        health.consecutive_failures,
                    )
                    degraded_logged = True
                sleep_s = min(backoff, RECONNECT_BACKOFF_CAP_S) + random.uniform(0, 0.5)
                log.info("reconnecting in %.1fs", sleep_s)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)

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
            open_timeout=WS_OPEN_TIMEOUT_S,
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

            # Wire the WS log uploader so WARN+ records start flowing to
            # the backend. Backlog accumulated while offline drains on
            # this call.
            log_handler = get_ws_log_handler()
            log_handler.set_publisher(self.publish)

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            try:
                await self._recv_loop()
            finally:
                heartbeat_task.cancel()
                with suppress_cancelled():
                    await heartbeat_task
                # Detach the log uploader BEFORE clearing self._ws so
                # any final records queue locally instead of being
                # silently dropped by `publish`.
                log_handler.set_publisher(None)
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
            if mtype == "task_cancel":
                self._handle_task_cancel(msg)
                continue
            if mtype == "subscribe_stream":
                self._handle_stream_subscription(True)
                continue
            if mtype == "unsubscribe_stream":
                self._handle_stream_subscription(False)
                continue
            if mtype == "check_update":
                asyncio.create_task(self._handle_check_update(msg))
                continue
            log.debug("unknown message type: %s", mtype)

    async def _handle_check_update(self, msg: dict[str, Any]) -> None:
        """Run an on-demand update probe and reply with the result.

        Runs the synchronous probe on a worker thread so the WS event
        loop keeps reading frames (download can take seconds). On Unix
        an applied update re-execs the process via ``os.execv``, so the
        ack frame may never reach the wire — that's expected; the
        backend's wait loop will hit its timeout and the cabinet shows
        a "перезагружаем агент" toast either way.
        """
        call_id = str(msg.get("id") or "")
        check = _resolve_check_and_apply_once()
        try:
            result = await asyncio.to_thread(check)
        except Exception as exc:  # noqa: BLE001
            log.warning("on-demand update probe crashed: %s", exc)
            result = {
                "ok": False,
                "current_version": __version__,
                "latest_version": None,
                "applied": False,
                "restarting": False,
                "reason": f"probe_crashed: {exc}",
            }
        with contextlib.suppress(Exception):
            await self._send({
                "type": "check_update_result",
                "id": call_id,
                "result": result,
            })

    def _handle_task_dispatch(self, msg: dict[str, Any]) -> None:
        task_id = str(msg.get("id") or "").strip()
        task = str(msg.get("task") or "").strip()
        scope = msg.get("scope") if isinstance(msg.get("scope"), dict) else None
        agent_id = str(msg.get("agent_id") or "").strip()
        # Direct-tool short-circuit: when the dispatch frame carries a `tool`
        # field, run the named MCP tool with `scope` as args and emit
        # task_complete/task_error WS frames straight away. Skips the LLM
        # agent loop entirely so deterministic backend-driven jobs
        # (project_clone_and_setup, integration cookie probes, …) run with
        # zero LLM-call latency.
        direct_tool = str(msg.get("tool") or "").strip()
        if direct_tool and task_id:
            asyncio.create_task(self._handle_direct_tool(task_id, direct_tool, scope or {}))
            return
        if not task_id or not task:
            log.warning("task_dispatch missing id/task: %s", msg)
            return
        if self._on_task_dispatch is None:
            log.warning("task_dispatch received but no handler registered")
            return
        try:
            # Back-compat: handlers may be 3-arg (legacy) or 4-arg (multi-agent).
            try:
                self._on_task_dispatch(task_id, task, scope, agent_id)  # type: ignore[call-arg]
            except TypeError:
                self._on_task_dispatch(task_id, task, scope)  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001
            log.exception("task_dispatch handler failed: %s", exc)

    async def _handle_direct_tool(
        self, task_id: str, tool_name: str, args: dict[str, Any]
    ) -> None:
        """Run an MCP tool synchronously for a task_dispatch with `tool=…`.

        Emits task_complete on success, task_error on failure. No screenshots,
        no thinking frames — this path is for backend-driven deterministic
        work like project_clone_and_setup, not LLM-driven autonomy.
        """
        try:
            result = await self._handler(tool_name, args)
            await self._send({
                "type": "task_complete",
                "task_id": task_id,
                "answer": (
                    result if isinstance(result, str) else json.dumps(result)
                ),
                "iterations": 0,
                "tokens_used": 0,
                "cost_usd": 0,
            })
        except Exception as exc:  # noqa: BLE001
            await self._send({
                "type": "task_error",
                "task_id": task_id,
                "error": f"direct_tool_failed: {type(exc).__name__}: {exc}",
            })

    def _handle_task_cancel(self, msg: dict[str, Any]) -> None:
        task_id: str | None = msg.get("task_id") or None
        if self._on_task_cancel is None:
            log.warning("task_cancel received but no handler registered (task_id=%s)", task_id)
            return
        try:
            self._on_task_cancel(task_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("task_cancel handler failed: %s", exc)

    def _handle_stream_subscription(self, subscribe: bool) -> None:
        if self._on_stream_subscribe is None:
            log.debug("stream subscription message ignored (no handler)")
            return
        try:
            self._on_stream_subscribe(subscribe)
        except Exception as exc:  # noqa: BLE001
            log.exception("stream subscribe handler failed: %s", exc)

    async def _handle_hello_ack(self, msg: dict[str, Any]) -> None:
        self._handshake_completed = True
        get_health().mark_connected()
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


def _resolve_check_and_apply_once() -> Callable[..., dict[str, Any]]:
    """Lazy-import :func:`auto_updater.check_and_apply_once`.

    Avoids importing the urllib stack at module-load time and lets the
    unit test monkeypatch the function via ``ws_client._CHECK_AND_APPLY_ONCE``.
    """
    global _CHECK_AND_APPLY_ONCE
    if _CHECK_AND_APPLY_ONCE is not None:
        return _CHECK_AND_APPLY_ONCE
    from . import auto_updater

    _CHECK_AND_APPLY_ONCE = auto_updater.check_and_apply_once
    return _CHECK_AND_APPLY_ONCE


class suppress_cancelled:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is asyncio.CancelledError

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return exc_type is asyncio.CancelledError
