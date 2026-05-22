"""Perf regression tests for the MJPEG stream capture path.

Checks:
  1. Avg encode time per frame < 100ms.
  2. Identical-frame dedup drops >= 50% of WS emits when content is unchanged.
"""
from __future__ import annotations

import io
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from agentflow_computer_mcp.driver.streamer import (
    CaptureLoop,
    _frame_hash,
    fast_capture_jpeg,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_frame(width: int = 1280, height: int = 800, color: tuple = (80, 120, 200)) -> bytes:
    """Return a JPEG bytes object matching the optimised defaults."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=58)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: encode time benchmark
# ---------------------------------------------------------------------------

def test_encode_time_under_100ms() -> None:
    """30 back-to-back fast_capture_jpeg() calls must average < 100ms each.

    On macOS with Quartz the real path is ~5-15ms. On CI (mocked) it will be
    <1ms. The 100ms ceiling catches regressions like accidental LANCZOS on
    every frame.
    """
    fake_frame = _make_fake_frame()

    # Patch backend.capture_screen_fast so test runs without a real display.
    with patch(
        "agentflow_computer_mcp.driver.streamer.backend"
    ) as mock_backend:
        mock_backend.capture_screen_fast.return_value = fake_frame

        n = 30
        t0 = time.perf_counter()
        for _ in range(n):
            fast_capture_jpeg()
        elapsed = time.perf_counter() - t0

    avg_ms = (elapsed / n) * 1000
    assert avg_ms < 100, f"avg encode {avg_ms:.1f}ms >= 100ms ceiling"


# ---------------------------------------------------------------------------
# Test 2: blake2b dedup drops identical frames
# ---------------------------------------------------------------------------

def test_dedup_drops_identical_frames() -> None:
    """When the same frame bytes are fed twice, WS emit count <= 50% of attempts."""
    fake_frame = _make_fake_frame()
    different_frame = _make_fake_frame(color=(200, 80, 80))

    stream_frame: dict = {}
    stream_cond = threading.Condition()
    subscribed = threading.Event()
    subscribed.set()

    emitted: list[dict] = []

    def publisher(msg: dict) -> None:
        emitted.append(msg)

    loop = CaptureLoop(
        stream_frame=stream_frame,
        stream_cond=stream_cond,
        fps=20,
        stream_subscribed=subscribed,
        outbound_publisher=publisher,
    )

    # Simulate 20 publish attempts: 10 identical + 2 different + 8 identical.
    # We call _maybe_publish_ws directly to control timing.
    # Force last_ws_emit_at to 0 so interval gate is always open.
    total_calls = 0

    for i in range(20):
        # Advance simulated time by 0.2s each call (> WS_STREAM_MIN_INTERVAL_S=0.1).
        now = float(i) * 0.2
        loop._last_ws_emit_at = now - 0.15  # ensure interval gate passes
        frame = different_frame if i in (5, 12) else fake_frame
        loop._maybe_publish_ws(frame, now)
        total_calls += 1

    # Total unique content transitions: fake→different (i=5), different→fake (i=6),
    # fake→different (i=12), different→fake (i=13) = 4 emits max (+ first emit).
    # Identical runs between transitions are suppressed.
    emit_count = len(emitted)
    drop_rate = 1.0 - (emit_count / total_calls)
    assert drop_rate >= 0.5, (
        f"dedup only dropped {drop_rate*100:.0f}% of calls "
        f"({emit_count} emitted / {total_calls} attempts); expected >= 50%"
    )


# ---------------------------------------------------------------------------
# Test 3: adaptive rate backs off on publisher failures
# ---------------------------------------------------------------------------

def test_adaptive_rate_backoff_on_failure() -> None:
    """When publisher raises, emit interval should double up to max."""
    stream_frame: dict = {}
    stream_cond = threading.Condition()
    subscribed = threading.Event()
    subscribed.set()

    def failing_publisher(msg: dict) -> None:
        raise RuntimeError("connection lost")

    loop = CaptureLoop(
        stream_frame=stream_frame,
        stream_cond=stream_cond,
        fps=20,
        stream_subscribed=subscribed,
        outbound_publisher=failing_publisher,
    )

    from agentflow_computer_mcp.driver.streamer import WS_STREAM_MIN_INTERVAL_S, _WS_EMIT_MAX_INTERVAL_S

    fake_frame = _make_fake_frame()
    different_frames = [_make_fake_frame(color=(i * 10 % 255, 80, 80)) for i in range(8)]

    initial_interval = loop._ws_emit_interval

    for i, frame in enumerate(different_frames):
        now = float(i) * 2.0  # wide spacing so interval gate is always open
        loop._last_ws_emit_at = now - loop._ws_emit_interval - 0.01
        loop._last_frame_hash = b""  # force dedup gate open each call
        loop._maybe_publish_ws(frame, now)

    assert loop._ws_emit_interval > initial_interval, "interval should have grown on failures"
    assert loop._ws_emit_interval <= _WS_EMIT_MAX_INTERVAL_S, "interval must not exceed max"
