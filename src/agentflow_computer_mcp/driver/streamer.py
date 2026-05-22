"""Fast screen capture + JPEG compression for the MJPEG live stream.

Routes through the platform backend so the same loop runs on macOS, Linux, and Windows.
"""
from __future__ import annotations

import base64
import contextlib
import io
import threading
import time
from collections.abc import Callable
from typing import Any

from PIL import Image

from ..platform import backend

# Hard cap for WS stream_frame emission. The MJPEG (local) loop still runs at
# full `fps` for the viewer.
WS_STREAM_MIN_INTERVAL_S = 0.1


def fast_capture_jpeg(width_cap: int = 1400, quality: int = 68) -> bytes:
    """Native-resolution JPEG of the primary display.

    On macOS this is ~5ms (CGDisplayCreateImage); on Linux/Windows ~20-40ms (mss).
    """
    if backend is None:
        raise RuntimeError("no platform backend available")
    return backend.capture_screen_fast(width_cap=width_cap, quality=quality)


def compress_png_for_viewer(png: bytes, width_cap: int = 1600, quality: int = 78) -> bytes:
    img = Image.open(io.BytesIO(png))
    if img.width > width_cap:
        ratio = width_cap / img.width
        img = img.resize((width_cap, int(img.height * ratio)), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


class CaptureLoop:
    """Background thread that captures the display every ~50ms and pushes to a shared buffer.

    When `stream_subscribed` is set and an `outbound_publisher` is available,
    the loop also emits `stream_frame` WS frames at ≤10 fps regardless of the
    local viewer rate.
    """

    def __init__(
        self,
        stream_frame: dict[str, Any],
        stream_cond: threading.Condition,
        fps: int = 20,
        stream_subscribed: threading.Event | None = None,
        outbound_publisher: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._frame = stream_frame
        self._cond = stream_cond
        self._period = 1.0 / max(1, fps)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream_subscribed = stream_subscribed
        self._publisher = outbound_publisher
        self._last_ws_emit_at = 0.0

    def set_outbound_publisher(
        self, publisher: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Late-binding hook so the WS client can register after construction."""
        self._publisher = publisher

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _maybe_publish_ws(self, frame: bytes, now: float) -> None:
        if self._publisher is None or self._stream_subscribed is None:
            return
        if not self._stream_subscribed.is_set():
            return
        if now - self._last_ws_emit_at < WS_STREAM_MIN_INTERVAL_S:
            return
        self._last_ws_emit_at = now
        with contextlib.suppress(Exception):
            self._publisher(
                {
                    "type": "stream_frame",
                    "frame": base64.b64encode(frame).decode("ascii"),
                    "ts": int(now * 1000),
                }
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                frame = fast_capture_jpeg()
                with self._cond:
                    self._frame["jpeg"] = frame
                    self._frame["ts"] = time.time()
                    self._cond.notify_all()
                self._maybe_publish_ws(frame, time.time())
            except Exception as exc:  # noqa: BLE001
                print(f"[stream] capture err: {exc}", flush=True)
                time.sleep(0.5)
                continue
            dt = time.time() - t0
            if dt < self._period:
                time.sleep(self._period - dt)
