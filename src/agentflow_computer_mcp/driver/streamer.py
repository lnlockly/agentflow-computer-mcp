"""Fast Quartz screen capture + JPEG compression for the MJPEG live stream."""
from __future__ import annotations

import io
import threading
import time
from typing import Any

from PIL import Image

try:
    import Quartz as _Q
    from Quartz import CoreGraphics as _CG

    _HAS_QUARTZ = True
    _MAIN_DISPLAY = _CG.CGMainDisplayID()
except ImportError:
    _HAS_QUARTZ = False
    _Q = None  # type: ignore[assignment]
    _CG = None  # type: ignore[assignment]
    _MAIN_DISPLAY = 0


def fast_capture_jpeg(width_cap: int = 1400, quality: int = 68) -> bytes:
    """Capture full screen as JPEG. Quartz CGDisplayCreateImage is ~5ms; falls back to pyautogui."""
    if _HAS_QUARTZ:
        img_ref = _CG.CGDisplayCreateImage(_MAIN_DISPLAY)
        if img_ref is None:
            raise RuntimeError("CGDisplayCreateImage returned None")
        w = _Q.CGImageGetWidth(img_ref)
        h = _Q.CGImageGetHeight(img_ref)
        bpr = _Q.CGImageGetBytesPerRow(img_ref)
        raw = bytes(_Q.CGDataProviderCopyData(_Q.CGImageGetDataProvider(img_ref)))
        img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", bpr, 1)
    else:
        import pyautogui

        img = pyautogui.screenshot()
        w = img.width

    if img.width > width_cap:
        ratio = width_cap / img.width
        img = img.resize((width_cap, int(img.height * ratio)), Image.BILINEAR)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=False)
    return out.getvalue()


def compress_png_for_viewer(png: bytes, width_cap: int = 1600, quality: int = 78) -> bytes:
    img = Image.open(io.BytesIO(png))
    if img.width > width_cap:
        ratio = width_cap / img.width
        img = img.resize((width_cap, int(img.height * ratio)), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


class CaptureLoop:
    """Background thread that captures the display every ~50ms and pushes to a shared buffer."""

    def __init__(
        self,
        stream_frame: dict[str, Any],
        stream_cond: threading.Condition,
        fps: int = 20,
    ) -> None:
        self._frame = stream_frame
        self._cond = stream_cond
        self._period = 1.0 / max(1, fps)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                frame = fast_capture_jpeg()
                with self._cond:
                    self._frame["jpeg"] = frame
                    self._frame["ts"] = time.time()
                    self._cond.notify_all()
            except Exception as exc:  # noqa: BLE001
                print(f"[stream] capture err: {exc}", flush=True)
                time.sleep(0.5)
                continue
            dt = time.time() - t0
            if dt < self._period:
                time.sleep(self._period - dt)
