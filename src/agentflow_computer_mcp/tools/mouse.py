"""Mouse — delegates to the platform backend."""
from __future__ import annotations

from typing import Literal

from ..platform import backend

Button = Literal["left", "right", "middle"]


def click(x: int, y: int, button: Button = "left", clicks: int = 1) -> dict[str, int]:
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.mouse_click(x, y, button=button, clicks=clicks)


def move(x: int, y: int, duration: float = 0.0) -> dict[str, int]:
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.mouse_move(x, y, duration=duration)


def scroll(dx: int, dy: int) -> dict[str, int]:
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.mouse_scroll(dx, dy)
