"""Platform backend selection.

Imports the correct backend for the host OS and exposes it as :data:`backend`.
On unsupported platforms the import still succeeds but :data:`backend` is ``None``
and :data:`PLATFORM` is ``"unknown"``. Callers should check for ``backend is None``
when they need a hard fail-fast.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .base import PlatformBackend

if TYPE_CHECKING:
    backend: PlatformBackend | None

PLATFORM: str = "unknown"
backend: PlatformBackend | None = None


def _detect() -> tuple[str, PlatformBackend | None]:
    if sys.platform == "darwin":
        from . import mac

        return "mac", mac.backend
    if sys.platform.startswith("linux"):
        from . import linux

        return "linux", linux.backend
    if sys.platform == "win32":
        from . import windows

        return "windows", windows.backend
    return "unknown", None


PLATFORM, backend = _detect()


__all__ = ["PLATFORM", "PlatformBackend", "backend"]
