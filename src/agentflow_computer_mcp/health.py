"""Thread-safe daemon health state shared between ws_client and the local viewer."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Literal

WSStatus = Literal["connecting", "connected", "reconnecting", "dead"]


@dataclass
class HealthState:
    ws_status: WSStatus = "connecting"
    last_hello_at: float | None = None
    last_failure: str | None = None
    consecutive_failures: int = 0


class HealthRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = HealthState()

    def snapshot(self) -> HealthState:
        with self._lock:
            return HealthState(
                ws_status=self._state.ws_status,
                last_hello_at=self._state.last_hello_at,
                last_failure=self._state.last_failure,
                consecutive_failures=self._state.consecutive_failures,
            )

    def to_dict(self) -> dict[str, object]:
        with self._lock:
            return asdict(self._state)

    def mark_connecting(self) -> None:
        with self._lock:
            self._state.ws_status = "connecting"

    def mark_connected(self) -> None:
        with self._lock:
            self._state.ws_status = "connected"
            self._state.last_hello_at = time.time()
            self._state.consecutive_failures = 0
            self._state.last_failure = None

    def mark_reconnecting(self, reason: str) -> None:
        with self._lock:
            self._state.ws_status = "reconnecting"
            self._state.last_failure = reason
            self._state.consecutive_failures += 1

    def mark_dead(self, reason: str) -> None:
        with self._lock:
            self._state.ws_status = "dead"
            self._state.last_failure = reason

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._state.consecutive_failures


_registry: HealthRegistry = HealthRegistry()


def get_health() -> HealthRegistry:
    return _registry


def reset_health_for_tests() -> None:
    """Reset the module-level singleton. Test-only helper."""
    global _registry
    _registry = HealthRegistry()
