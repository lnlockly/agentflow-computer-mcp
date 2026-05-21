"""Shared mutable state for the driver: action log, task queue, MJPEG buffer."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DriverState:
    busy: bool = False
    current_task: str = ""
    task_count: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)
    last_cursor: list[int] = field(default_factory=lambda: [0, 0])
    actions_lock: threading.Lock = field(default_factory=threading.Lock)
    task_queue: queue.Queue[str] = field(default_factory=queue.Queue)
    stream_frame: dict[str, Any] = field(
        default_factory=lambda: {"jpeg": b"", "ts": 0.0}
    )
    stream_cond: threading.Condition = field(default_factory=threading.Condition)
    live_dir: Path = field(default_factory=lambda: Path("/tmp/agentflow-live"))

    def __post_init__(self) -> None:
        self.live_dir.mkdir(parents=True, exist_ok=True)

    def push_action(
        self,
        action: str,
        detail: str = "",
        thinking: str = "",
        jpeg_path_writer: Any = None,
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
