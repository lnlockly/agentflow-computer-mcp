"""Ship WARN/ERROR/CRITICAL log records to the backend over the WS.

The cabinet needs a recent error trail without asking the user to open
PowerShell, attach to the daemon process, or zip a log file. This
module wires a `logging.Handler` into the root logger that pushes
qualifying records onto a bounded queue; an attached publisher (set by
`ws_client` when a session is connected) drains the queue and serialises
each record as a ``device_log`` WS frame.

Design choices:

* Bounded queue (default 500 records). The handler MUST NOT block the
  thread that emitted the log — production daemons run a capture loop
  at 20 fps, the AI loop is bursty under tool retries, and a slow WS
  socket would cascade into both. Drop-oldest beats drop-newest because
  the newest record is the one a developer wants when diagnosing a
  crash loop.
* Level filter at attach time (WARNING and above). DEBUG/INFO would
  flood the backend with capture-loop chatter that is not actionable.
* Lazy publisher binding. The handler stays valid across reconnects;
  ``set_publisher(None)`` parks the drain until the WS comes back, at
  which point queued records flush in order.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

Publisher = Callable[[dict[str, Any]], None]

DEFAULT_MAX_QUEUE = 500
UPLOAD_MIN_LEVEL = logging.WARNING
MESSAGE_TRUNCATE_CHARS = 4_000
EXC_TRUNCATE_CHARS = 4_000


class WsLogHandler(logging.Handler):
    """Queue WARN+ records and drain them through a WS publisher."""

    def __init__(
        self,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        min_level: int = UPLOAD_MIN_LEVEL,
    ) -> None:
        super().__init__(level=min_level)
        self._queue: deque[dict[str, Any]] = deque(maxlen=max_queue)
        self._lock = threading.Lock()
        self._publisher: Publisher | None = None
        self._dropped_oldest = 0

    # ─────────── logging.Handler hook ───────────
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.level:
            return
        try:
            payload = self._record_to_payload(record)
        except Exception:  # noqa: BLE001 — never raise out of a log call
            return
        publisher: Publisher | None
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                # deque with maxlen drops the oldest on append; we count
                # the drop so observability stays honest.
                self._dropped_oldest += 1
            self._queue.append(payload)
            publisher = self._publisher
        if publisher is not None:
            self._drain(publisher)

    # ─────────── publisher binding ───────────
    def set_publisher(self, publisher: Publisher | None) -> None:
        """Attach (or detach) the WS publisher. Flushes the backlog on attach."""
        with self._lock:
            self._publisher = publisher
            should_flush = publisher is not None and bool(self._queue)
        if should_flush and publisher is not None:
            self._drain(publisher)

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_oldest

    # ─────────── internals ───────────
    def _drain(self, publisher: Publisher) -> None:
        # Snapshot the backlog under the lock, then publish without it.
        # The publisher is a `loop.call_soon_threadsafe`-backed function
        # in ws_client; calling it while holding our lock would not
        # deadlock today but keeps us defensive against future changes.
        with self._lock:
            pending = list(self._queue)
            self._queue.clear()
        for payload in pending:
            try:
                publisher(payload)
            except Exception:  # noqa: BLE001 — publisher must never bubble
                # Re-queue the survivors so we retry on the next flush.
                # Drop-oldest semantics still apply via maxlen.
                with self._lock:
                    for leftover in pending[pending.index(payload):]:
                        if len(self._queue) == self._queue.maxlen:
                            self._dropped_oldest += 1
                        self._queue.append(leftover)
                return

    @staticmethod
    def _record_to_payload(record: logging.LogRecord) -> dict[str, Any]:
        message = record.getMessage()
        if len(message) > MESSAGE_TRUNCATE_CHARS:
            message = message[:MESSAGE_TRUNCATE_CHARS] + "…[truncated]"
        exc_text = ""
        if record.exc_info:
            try:
                exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                exc_text = ""
        if exc_text and len(exc_text) > EXC_TRUNCATE_CHARS:
            exc_text = exc_text[:EXC_TRUNCATE_CHARS] + "…[truncated]"
        payload: dict[str, Any] = {
            "type": "device_log",
            "level": record.levelname,
            "ts": int(record.created * 1000),
            "logger_name": record.name,
            "message": message,
        }
        if exc_text:
            payload["exc_info_text"] = exc_text
        # JSON-roundtrip safety check. If anything in the message is
        # unserialisable, fall back to repr() so we never crash the
        # caller.
        try:
            json.dumps(payload)
        except (TypeError, ValueError):
            payload["message"] = repr(record.msg)[:MESSAGE_TRUNCATE_CHARS]
        return payload


# Module-level singleton so ws_client can grab the same handler the
# logging setup attached at boot.
_singleton: WsLogHandler | None = None
_singleton_lock = threading.Lock()


def get_handler() -> WsLogHandler:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = WsLogHandler()
        return _singleton


def reset_for_tests() -> None:
    """Drop the singleton. Tests call this between cases for isolation."""
    global _singleton
    with _singleton_lock:
        _singleton = None
    # Strip any references the handler may have on the root logger.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, WsLogHandler):
            root.removeHandler(handler)


# Re-export the small helper that ws_client uses to format an event time
# without importing time here.
def now_ms() -> int:
    return int(time.time() * 1000)
