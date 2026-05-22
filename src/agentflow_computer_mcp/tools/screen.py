"""Screen capture — thin wrapper over the platform backend.

Public API:
- :func:`capture` returns PNG bytes (optionally a region).
- :func:`capture_base64` returns ``{"mime", "base64", "size_bytes"}`` for MCP responses.

The historical Quartz/pyautogui dual-path is preserved here for back-compat with
existing tests that patch ``_HAS_QUARTZ`` to force a pyautogui fallback. On Linux
and Windows the backend takes over via the explicit ``elif`` branches.
"""
from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image

from ..platform import PLATFORM, backend

try:
    import Quartz  # type: ignore[import-not-found]
    from Quartz import CoreGraphics as CG  # type: ignore[import-not-found]

    _HAS_QUARTZ = True
except ImportError:
    Quartz = None  # type: ignore[assignment]
    CG = None  # type: ignore[assignment]
    _HAS_QUARTZ = False


def _capture_via_quartz(region: dict[str, int] | None) -> bytes:
    if region:
        rect = CG.CGRectMake(
            float(region["x"]),
            float(region["y"]),
            float(region["width"]),
            float(region["height"]),
        )
    else:
        rect = CG.CGRectInfinite

    image_ref = Quartz.CGWindowListCreateImage(
        rect,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if image_ref is None:
        raise RuntimeError("CGWindowListCreateImage returned None")

    width = Quartz.CGImageGetWidth(image_ref)
    height = Quartz.CGImageGetHeight(image_ref)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
    data_provider = Quartz.CGImageGetDataProvider(image_ref)
    data = Quartz.CGDataProviderCopyData(data_provider)
    raw = bytes(data)

    img = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", bytes_per_row, 1)
    return _encode_png(img)


def _capture_via_pyautogui(region: dict[str, int] | None) -> bytes:
    import pyautogui

    if region:
        box = (region["x"], region["y"], region["width"], region["height"])
        shot = pyautogui.screenshot(region=box)
    else:
        shot = pyautogui.screenshot()
    return _encode_png(shot)


def _encode_png(img: Image.Image, max_width: int = 1280) -> bytes:
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def capture(region: dict[str, int] | None = None) -> bytes:
    # On macOS keep the legacy dual-path so existing patch-based tests still drive both
    # branches. Off-Mac platforms route through the backend abstraction.
    if PLATFORM == "mac" or _HAS_QUARTZ:
        if _HAS_QUARTZ:
            return _capture_via_quartz(region)
        return _capture_via_pyautogui(region)
    if backend is None:
        return _capture_via_pyautogui(region)
    return backend.capture_screen(region)


def capture_base64(region: dict[str, int] | None = None) -> dict[str, Any]:
    png = capture(region)
    return {
        "mime": "image/png",
        "base64": base64.b64encode(png).decode("ascii"),
        "size_bytes": len(png),
    }
