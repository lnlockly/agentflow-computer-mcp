"""Shared mutable state for the driver: action log, task queue, MJPEG buffer, WS hooks."""
from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OutboundPublisher = Callable[[dict[str, Any]], None]


@dataclass
class DriverState:
    busy: bool = False
    current_task: str = ""
    current_task_id: str = ""
    task_count: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)
    last_cursor: list[int] = field(default_factory=lambda: [0, 0])
    actions_lock: threading.Lock = field(default_factory=threading.Lock)
    task_queue: queue.Queue[tuple[str, str]] = field(default_factory=queue.Queue)
    stream_frame: dict[str, Any] = field(
        default_factory=lambda: {"jpeg": b"", "ts": 0.0}
    )
    stream_cond: threading.Condition = field(default_factory=threading.Condition)
    live_dir: Path = field(default_factory=lambda: Path("/tmp/agentflow-live"))
    # WS bridge: set when the reverse-tunnel client is connected. Thread-safe
    # callable that schedules a JSON frame on the WS event loop. None when
    # the daemon runs without remote dispatch.
    outbound_publisher: OutboundPublisher | None = None
    # Set by `subscribe_stream` frames; cleared by `unsubscribe_stream` and
    # on WS disconnect. Capture loop polls this to decide whether to push
    # `stream_frame` frames over the wire.
    stream_subscribed: threading.Event = field(default_factory=threading.Event)
    # Set by `request_abort` when a task_cancel WS frame arrives. The run_task
    # loop checks this between iterations and exits early. Cleared after abort.
    abort_flag: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.live_dir.mkdir(parents=True, exist_ok=True)

    # ─────────── outbound WS helpers ───────────
    def publish_outbound(self, payload: dict[str, Any]) -> None:
        """Best-effort send to the WS bridge. No-op when offline."""
        pub = self.outbound_publisher
        if pub is None:
            return
        # never crash the AI loop on a flaky socket
        with contextlib.suppress(Exception):
            pub(payload)

    def request_abort(self, task_id: str | None = None) -> None:
        """Signal the running task to abort within ~2s.

        If *task_id* is given, the abort fires only when it matches the
        currently running task.  Pass ``None`` (or omit) to cancel whatever
        task is running unconditionally.

        Side effect: when the abort fires and a remote task_id is in flight,
        an immediate ``task_action: cancel_received`` frame is published so
        the cabinet UI shows "Останавливаю задачу..." within milliseconds.
        Real interruption follows once ``run_task`` reaches its next poll
        boundary (between SSE chunks or before the next tool dispatch).

        Idempotent: safe to call when no task is running — sets the flag which
        the idle worker will clear immediately on the next queue-drain cycle.
        """
        if task_id is not None and self.current_task_id and task_id != self.current_task_id:
            import logging
            logging.getLogger(__name__).warning(
                "request_abort: task_id=%s does not match current_task_id=%s — ignored",
                task_id,
                self.current_task_id,
            )
            return
        if not self.busy:
            import logging
            logging.getLogger(__name__).warning(
                "request_abort called but no task is running (task_id=%s) — no-op", task_id
            )
            return
        self.abort_flag.set()
        # Immediate ACK so the UI flips to "stopping" within <500 ms even
        # though the actual interrupt (LLM stream close / next tool gate)
        # may still take up to ~2 s to land.
        if self.current_task_id and self.outbound_publisher is not None:
            self.publish_outbound(
                {
                    "type": "task_action",
                    "task_id": self.current_task_id,
                    "ts": int(time.time() * 1000),
                    "action": "cancel_received",
                    "detail": "Останавливаю задачу...",
                }
            )

    def enqueue_task(self, task: str, task_id: str = "") -> str:
        """Queue a task with an optional pre-assigned id. Returns the id."""
        tid = task_id or f"local-{int(time.time() * 1000)}"
        self.task_queue.put((tid, task))
        return tid

    # ─────────── action log ───────────
    def push_action(
        self,
        action: str,
        detail: str = "",
        thinking: str = "",
        jpeg_path_writer: Any = None,
        screenshot_b64: str | None = None,
    ) -> None:
        ts = time.strftime("%H:%M:%S")
        with self.actions_lock:
            self.actions.append(
                {
                    "ts": ts,
                    "action": action,
                    "detail": detail,
                    "thinking": thinking,
                    "cursor": list(self.last_cursor),
                }
            )
            if len(self.actions) > 100:
                self.actions[:] = self.actions[-100:]
            if jpeg_path_writer is not None:
                jpeg_path_writer(self.live_dir)
        # Mirror to WS as a task_action when a remote (or local-tagged) task
        # is in flight. Cheap: the publisher is a thread-safe schedule call.
        if self.current_task_id and self.outbound_publisher is not None:
            frame: dict[str, Any] = {
                "type": "task_action",
                "task_id": self.current_task_id,
                "ts": int(time.time() * 1000),
                "action": action,
            }
            if detail:
                frame["detail"] = detail if not thinking else f"{detail}\n{thinking}"
            elif thinking:
                frame["detail"] = thinking
            if screenshot_b64:
                frame["screenshot_b64"] = screenshot_b64
            self.publish_outbound(frame)
