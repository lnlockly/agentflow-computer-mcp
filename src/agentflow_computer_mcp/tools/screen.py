from __future__ import annotations

import base64
import io
from typing import Any

try:
    import Quartz
    from Quartz import CoreGraphics as CG
    _HAS_QUARTZ = True
except ImportError:
    _HAS_QUARTZ = False

from PIL import Image


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
    if _HAS_QUARTZ:
        return _capture_via_quartz(region)
    return _capture_via_pyautogui(region)


def capture_base64(region: dict[str, int] | None = None) -> dict[str, Any]:
    png = capture(region)
    return {
        "mime": "image/png",
        "base64": base64.b64encode(png).decode("ascii"),
        "size_bytes": len(png),
    }
