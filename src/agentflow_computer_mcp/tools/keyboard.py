"""Keyboard — delegates to the platform backend."""
from __future__ import annotations

from ..platform import backend


def type_text(text: str, interval: float = 0.0) -> dict[str, int]:
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.keyboard_type(text, interval=interval)


def key(name: str) -> dict[str, str]:
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.keyboard_key(name)


def shortcut(combo: str) -> dict[str, str]:
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.keyboard_shortcut(combo)
