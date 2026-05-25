"""Capture-loop toggle: idle when no consumer, resumes on subscribe."""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

from agentflow_computer_mcp.driver.state import DriverState
from agentflow_computer_mcp.driver.streamer import CaptureLoop


def _new_loop(state: DriverState, capture_calls: list[float]) -> CaptureLoop:
    """Build a CaptureLoop wired to the gating helpers, with a mocked
    `fast_capture_jpeg` that records each invocation timestamp.
    """
    sent: list[dict[str, Any]] = []
    loop = CaptureLoop(
        state.stream_frame,
        state.stream_cond,
        fps=50,  # tight period so the test runs fast when capture is active
        stream_subscribed=state.stream_subscribed,
        outbound_publisher=sent.append,
        has_consumer=state.has_capture_consumer,
        wake_event=state.capture_wake,
    )
    loop._sent_frames = sent  # type: ignore[attr-defined]
    return loop


def test_default_state_has_no_consumer() -> None:
    state = DriverState()
    assert state.has_capture_consumer() is False
    assert state.local_viewer_count == 0
    assert not state.stream_subscribed.is_set()
    assert not state.capture_wake.is_set()


def test_subscribe_makes_consumer_present() -> None:
    state = DriverState()
    state.stream_subscribed.set()
    assert state.has_capture_consumer() is True


def test_local_viewer_acquire_release() -> None:
    state = DriverState()
    state.acquire_local_viewer()
    assert state.has_capture_consumer() is True
    assert state.capture_wake.is_set()
    state.release_local_viewer()
    assert state.has_capture_consumer() is False


def test_local_viewer_release_is_idempotent_below_zero() -> None:
    state = DriverState()
    state.release_local_viewer()
    state.release_local_viewer()
    assert state.local_viewer_count == 0
    assert state.has_capture_consumer() is False


def test_capture_loop_skips_capture_when_no_consumer() -> None:
    """Hot path — no consumer means zero `fast_capture_jpeg` calls.

    Runs the loop for ~300 ms with no subscriber and no local viewer.
    The mocked capture function must not be called.
    """
    state = DriverState()
    capture_calls: list[float] = []

    def fake_capture(width_cap: int = 1280, quality: int = 58) -> bytes:
        capture_calls.append(time.time())
        return b"\xff\xd8\xff\xd9"

    loop = _new_loop(state, capture_calls)
    with patch(
        "agentflow_computer_mcp.driver.streamer.fast_capture_jpeg",
        side_effect=fake_capture,
    ):
        loop.start()
        time.sleep(0.3)
        loop.stop()
        if loop._thread is not None:
            loop._thread.join(timeout=1.0)

    assert capture_calls == [], (
        f"capture loop ran {len(capture_calls)} times while no consumer "
        "was registered — toggle is broken"
    )


def test_capture_loop_resumes_when_subscriber_arrives() -> None:
    """Idle loop must wake and start capturing once the WS subscribes."""
    state = DriverState()
    capture_calls: list[float] = []

    def fake_capture(width_cap: int = 1280, quality: int = 58) -> bytes:
        capture_calls.append(time.time())
        return b"\xff\xd8\xff\xd9"

    loop = _new_loop(state, capture_calls)
    with patch(
        "agentflow_computer_mcp.driver.streamer.fast_capture_jpeg",
        side_effect=fake_capture,
    ):
        loop.start()
        time.sleep(0.15)
        # Still idle.
        assert capture_calls == []

        # Flip the subscriber flag, mirror what `on_stream_subscribe(True)` does.
        state.stream_subscribed.set()
        state.capture_wake.set()

        # Give the loop time to capture a few frames.
        time.sleep(0.25)
        loop.stop()
        if loop._thread is not None:
            loop._thread.join(timeout=1.0)

    assert len(capture_calls) >= 2, (
        f"loop captured only {len(capture_calls)} frames after subscribe — "
        "wake event did not interrupt the idle wait"
    )


def test_capture_loop_pauses_again_after_unsubscribe() -> None:
    """After a sub→unsub cycle the loop must go back to idle.

    Catches a regression where the wake event stays set forever (or the
    loop forgets to re-check `has_consumer` after each frame), turning
    the gating into a one-way switch.
    """
    state = DriverState()
    capture_calls: list[float] = []

    def fake_capture(width_cap: int = 1280, quality: int = 58) -> bytes:
        capture_calls.append(time.time())
        return b"\xff\xd8\xff\xd9"

    loop = _new_loop(state, capture_calls)
    with patch(
        "agentflow_computer_mcp.driver.streamer.fast_capture_jpeg",
        side_effect=fake_capture,
    ):
        loop.start()
        state.stream_subscribed.set()
        state.capture_wake.set()
        time.sleep(0.2)
        count_after_subscribe = len(capture_calls)
        assert count_after_subscribe >= 2

        # Unsubscribe — loop should park again.
        state.stream_subscribed.clear()
        # Give it a tick to notice the next iteration. With the 1 s ceiling
        # wait in `_wait_for_consumer`, leave generous headroom.
        time.sleep(1.3)
        count_after_unsubscribe = len(capture_calls)
        # We sample again after an extra wait — no new captures should land.
        time.sleep(0.3)
        loop.stop()
        if loop._thread is not None:
            loop._thread.join(timeout=1.0)

    final_count = len(capture_calls)
    # Allow at most one trailing capture from the in-flight period after
    # unsubscribe (the loop may already be mid-iteration when the flag
    # flips). Anything more = the loop didn't go idle.
    assert final_count - count_after_unsubscribe <= 1, (
        f"loop captured {final_count - count_after_unsubscribe} extra frames "
        "after unsubscribe — gating regression"
    )


def test_local_viewer_keeps_capture_alive_without_ws() -> None:
    """A local browser at localhost:8765 must keep frames flowing even
    when the cabinet has the cloud feed switched off."""
    state = DriverState()
    capture_calls: list[float] = []

    def fake_capture(width_cap: int = 1280, quality: int = 58) -> bytes:
        capture_calls.append(time.time())
        return b"\xff\xd8\xff\xd9"

    loop = _new_loop(state, capture_calls)
    with patch(
        "agentflow_computer_mcp.driver.streamer.fast_capture_jpeg",
        side_effect=fake_capture,
    ):
        loop.start()
        # Simulate /stream.mjpg open.
        state.acquire_local_viewer()
        time.sleep(0.2)
        count_with_viewer = len(capture_calls)
        assert count_with_viewer >= 2

        # Close the local viewer; nothing else is consuming.
        state.release_local_viewer()
        time.sleep(1.3)
        count_after_close = len(capture_calls)
        time.sleep(0.3)
        loop.stop()
        if loop._thread is not None:
            loop._thread.join(timeout=1.0)

    final_count = len(capture_calls)
    assert final_count - count_after_close <= 1, (
        "capture continued after the last local viewer disconnected"
    )


def test_stop_unblocks_idle_loop() -> None:
    """`stop()` from another thread must wake the loop within ~100 ms.

    Without the wake-on-stop signal, the loop could sit blocked on its
    1 s idle ceiling and delay process shutdown.
    """
    state = DriverState()
    capture_calls: list[float] = []

    def fake_capture(width_cap: int = 1280, quality: int = 58) -> bytes:
        capture_calls.append(time.time())
        return b"\xff\xd8\xff\xd9"

    loop = _new_loop(state, capture_calls)
    with patch(
        "agentflow_computer_mcp.driver.streamer.fast_capture_jpeg",
        side_effect=fake_capture,
    ):
        loop.start()
        time.sleep(0.05)
        t0 = time.time()
        loop.stop()
        if loop._thread is not None:
            loop._thread.join(timeout=0.5)
        elapsed = time.time() - t0

    assert loop._thread is None or not loop._thread.is_alive()
    assert elapsed < 0.3, f"stop took {elapsed:.2f}s — wake-on-stop regression"


def test_legacy_caller_without_gating_still_works() -> None:
    """When `has_consumer` / `wake_event` are None, the loop captures
    every tick — preserves the one-shot `drive` command's behaviour."""
    state = DriverState()
    capture_calls: list[float] = []

    def fake_capture(width_cap: int = 1280, quality: int = 58) -> bytes:
        capture_calls.append(time.time())
        return b"\xff\xd8\xff\xd9"

    loop = CaptureLoop(
        state.stream_frame,
        state.stream_cond,
        fps=50,
        # has_consumer / wake_event intentionally omitted.
    )
    with patch(
        "agentflow_computer_mcp.driver.streamer.fast_capture_jpeg",
        side_effect=fake_capture,
    ):
        loop.start()
        time.sleep(0.2)
        loop.stop()
        if loop._thread is not None:
            loop._thread.join(timeout=1.0)

    assert len(capture_calls) >= 2, "legacy mode dropped frames"


def test_concurrent_acquire_release_thread_safety() -> None:
    """Many parallel /stream.mjpg opens + closes must leave the counter
    consistent. Guards against a missing lock in the helpers."""
    state = DriverState()

    def worker(n: int) -> None:
        for _ in range(n):
            state.acquire_local_viewer()
            state.release_local_viewer()

    threads = [threading.Thread(target=worker, args=(200,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.local_viewer_count == 0
    assert state.has_capture_consumer() is False
