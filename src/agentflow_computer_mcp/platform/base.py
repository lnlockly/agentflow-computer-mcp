"""Platform backend protocol.

Each platform module (``mac``, ``linux``, ``windows``) exposes a ``backend`` singleton
that implements :class:`PlatformBackend`. Callers route every OS-specific operation
through this protocol so the rest of the codebase contains no platform branching.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PlatformBackend(Protocol):
    """Cross-platform contract for screen/input/window/clipboard primitives."""

    #: Short identifier (``"mac"``, ``"linux"``, ``"windows"``).
    name: str

    # ---- Screen capture -----------------------------------------------------
    def capture_screen_fast(self, width_cap: int = 1400, quality: int = 68) -> bytes:
        """Return native-resolution JPEG bytes of the primary display."""

    def capture_screen(self, region: dict[str, int] | None = None) -> bytes:
        """Return PNG bytes for the full screen or a region."""

    def capture_region(self, x: int, y: int, w: int, h: int) -> bytes:
        """Return PNG bytes for the given pixel rectangle."""

    # ---- Screen geometry ----------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        """Logical primary-display size ``(width, height)`` in the SAME
        coordinate space :meth:`mouse_click` consumes (``pyautogui`` points —
        not the physical/Retina capture pixels, not the down-scaled MJPEG
        frame). Reported on the WS ``hello`` so the server can scale
        normalized 0..1 click coords to device pixels."""
        ...

    # ---- Mouse --------------------------------------------------------------
    def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, int]:
        ...

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> dict[str, int]:
        ...

    def mouse_scroll(self, dx: int, dy: int) -> dict[str, int]:
        ...

    # ---- Keyboard -----------------------------------------------------------
    def keyboard_type(self, text: str, interval: float = 0.0) -> dict[str, int]:
        ...

    def keyboard_key(self, name: str) -> dict[str, str]:
        ...

    def keyboard_shortcut(self, combo: str) -> dict[str, str]:
        ...

    # ---- Windows ------------------------------------------------------------
    def window_list(self) -> list[dict[str, Any]]:
        """Return ``[{"owner", "title", "pid", "window_id", "bounds": {...}}, ...]``."""

    def window_focus(self, query: str) -> dict[str, Any]:
        """Bring a window matching ``query`` (owner or title) to the front."""

    def app_activate(self, owner: str) -> str:
        """Activate an application by name. Returns a status string."""

    # ---- Clipboard ----------------------------------------------------------
    def clipboard_read(self) -> str:
        ...

    def clipboard_write(self, text: str) -> None:
        ...

    # ---- Terminal -----------------------------------------------------------
    def read_terminal(self) -> str:
        """Best-effort recent terminal output. Empty string if unsupported."""
