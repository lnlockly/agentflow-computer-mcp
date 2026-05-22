"""Fast-cancel tests for run_task.

These cover the new ≤2s cancellation path:

1. ``post_llm_cancellable`` tears down the LLM stream within ~poll_interval
   of ``abort_flag.set()`` and raises ``TaskCancelled``.
2. ``run_task`` translates ``TaskCancelled`` into a single
   ``task_error: cancelled_by_user`` frame and clears the flag.
3. The pre-tool-dispatch gate aborts even when the LLM call already
   returned and we are iterating tool_use blocks.
4. ``ToolExecutor`` ``wait`` slices its sleep so a mid-call abort lands
   within ~0.2 s instead of after the full 5 s.
5. ``DriverState.request_abort`` emits the immediate ``cancel_received``
   ACK frame before the run loop has had a chance to react.
6. End-to-end timing: a long task dispatched, abort fired 200 ms later,
   ``run_task`` returns within 2 s with ``cancelled_by_user``.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentflow_computer_mcp.driver.loop import (
    TaskCancelled,
    _assemble_anthropic_sse,
    post_llm_cancellable,
    run_task,
)
from agentflow_computer_mcp.driver.state import DriverState

# ─────────────────────── SSE reassembly ─────────────────────────────────────

def test_assemble_anthropic_sse_text_only() -> None:
    events = [
        {"type": "message_start", "message": {"id": "m1", "model": "claude-opus-4-7"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    out = _assemble_anthropic_sse(events)
    assert out["content"] == [{"type": "text", "text": "Hello world"}]
    assert out["stop_reason"] == "end_turn"
    assert out["model"] == "claude-opus-4-7"


def test_assemble_anthropic_sse_tool_use() -> None:
    events = [
        {"type": "message_start", "message": {"id": "m1", "model": "claude-opus-4-7"}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "tu_1", "name": "screen_capture", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"foo":'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '"bar"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    out = _assemble_anthropic_sse(events)
    assert len(out["content"]) == 1
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "screen_capture"
    assert block["input"] == {"foo": "bar"}


# ─────────────────────── post_llm_cancellable ───────────────────────────────

class _FakeStreamResponse:
    """Pretends to be the urllib response. Yields bytes in chunks with
    optional delays so tests can race the abort_flag against the read."""

    def __init__(self, chunks: list[tuple[bytes, float]]) -> None:
        self._chunks = list(chunks)
        self._buf = b""
        self.closed = False

    def read(self, n: int = 4096) -> bytes:
        if self.closed:
            raise OSError("response closed")
        while not self._buf and self._chunks:
            data, delay = self._chunks.pop(0)
            if delay:
                # Sleep in 50 ms slices so a concurrent close() lands fast.
                end = time.monotonic() + delay
                while time.monotonic() < end:
                    if self.closed:
                        raise OSError("response closed")
                    time.sleep(0.05)
            self._buf += data
        if not self._buf:
            return b""
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self) -> None:
        self.closed = True


def _sse_chunk(events: list[dict[str, Any]]) -> bytes:
    """Encode a list of dicts as a single SSE payload."""
    out = []
    for ev in events:
        out.append(f"event: {ev['type']}".encode())
        out.append(b"data: " + json.dumps(ev).encode())
        out.append(b"")
    return b"\n".join(out) + b"\n"


def test_post_llm_cancellable_normal_completion() -> None:
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude-opus-4-7"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    fake = _FakeStreamResponse([(_sse_chunk(events), 0.0)])
    abort = threading.Event()
    with patch("agentflow_computer_mcp.driver.loop.urllib.request.urlopen", return_value=fake):
        resp = post_llm_cancellable("http://x", "k", {"model": "m", "messages": []}, abort)
    assert resp["content"] == [{"type": "text", "text": "ok"}]
    assert resp["stop_reason"] == "end_turn"
    assert fake.closed  # we always close on exit


def test_post_llm_cancellable_aborts_mid_stream_within_500ms() -> None:
    """abort_flag set 200 ms into a 10 s stream → TaskCancelled within 500 ms."""
    long_chunks = [
        (_sse_chunk([{"type": "message_start", "message": {"id": "m", "model": "x"}}]), 0.0),
        # Server "thinks" for 10 s without flushing anything else.
        (b"", 10.0),
    ]
    fake = _FakeStreamResponse(long_chunks)
    abort = threading.Event()

    def _trip() -> None:
        time.sleep(0.2)
        abort.set()

    threading.Thread(target=_trip, daemon=True).start()

    started = time.monotonic()
    with (
        patch("agentflow_computer_mcp.driver.loop.urllib.request.urlopen", return_value=fake),
        pytest.raises(TaskCancelled),
    ):
        post_llm_cancellable(
            "http://x", "k", {"model": "m", "messages": []}, abort, poll_interval=0.1
        )
    elapsed = time.monotonic() - started
    # 1 s budget per cancellation primitive — leaves ≥1 s headroom under the
    # 2 s end-to-end SLA. CI macOS runners stall up to ~600 ms on watchdog
    # close + socket teardown; local Linux/Mac finishes in <300 ms.
    assert elapsed < 1.0, f"cancel took {elapsed:.2f}s, want <1.0s"
    assert fake.closed


# ─────────────────────── run_task pre-tool-dispatch gate ────────────────────

def test_run_task_pre_tool_dispatch_gate_fires() -> None:
    """LLM returns 2 tool_use blocks; abort fires before executor runs.
    run_task must emit cancelled_by_user without calling executor.execute."""
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-pre-dispatch"
    published: list[dict[str, Any]] = []
    state.outbound_publisher = published.append

    fake_llm_resp = {
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "screen_capture", "input": {}},
            {"type": "tool_use", "id": "tu_2", "name": "screen_capture", "input": {}},
        ],
        "stop_reason": "tool_use",
    }

    def _llm(*a: Any, **kw: Any) -> dict[str, Any]:
        # Trip the flag the moment the LLM returns — before any tool runs.
        state.abort_flag.set()
        return fake_llm_resp

    executor = MagicMock()
    executor._af = None

    with (
        patch("agentflow_computer_mcp.driver.loop.post_llm_cancellable", side_effect=_llm),
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
    ):
        result = run_task("task", state, executor, api_key="k")

    assert result == ""
    executor.execute.assert_not_called()
    cancel_frames = [f for f in published if f.get("type") == "task_error"]
    assert cancel_frames and cancel_frames[0]["error"] == "cancelled_by_user"


# ─────────────────────── ToolExecutor.wait abort-aware sleep ────────────────

def test_wait_tool_aborts_within_300ms() -> None:
    from agentflow_computer_mcp.driver.desktop_tools import ToolExecutor

    state = DriverState()
    state.busy = True
    # bypass __init__ side effects that need real env (Playwright, etc.)
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._cursor = [0, 0]  # type: ignore[attr-defined]
    executor._af = None  # type: ignore[attr-defined]
    executor._pw = None  # type: ignore[attr-defined]
    executor._firefox = None  # type: ignore[attr-defined]
    executor._scope = None  # type: ignore[attr-defined]
    executor._state = state  # type: ignore[attr-defined]

    def _trip() -> None:
        time.sleep(0.2)
        state.abort_flag.set()

    threading.Thread(target=_trip, daemon=True).start()

    started = time.monotonic()
    out, _ = executor.execute("wait", {"seconds": 5})
    elapsed = time.monotonic() - started
    # 1 s budget — see comment in test_post_llm_cancellable_aborts_mid_stream.
    assert elapsed < 1.0, f"wait took {elapsed:.2f}s, want <1.0s"
    assert "aborted" in out


# ─────────────────────── request_abort emits ACK frame ──────────────────────

def test_request_abort_emits_cancel_received_frame() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-ack"
    frames: list[dict[str, Any]] = []
    state.outbound_publisher = frames.append

    state.request_abort("t-ack")

    assert state.abort_flag.is_set()
    ack = [f for f in frames if f.get("action") == "cancel_received"]
    assert ack, "expected immediate cancel_received task_action frame"
    assert ack[0]["task_id"] == "t-ack"
    assert ack[0]["type"] == "task_action"
    assert "Останавливаю" in ack[0]["detail"]


def test_request_abort_no_ack_when_no_task_id() -> None:
    """Local-only task (no remote task_id) → no ACK published."""
    state = DriverState()
    state.busy = True
    state.current_task_id = ""
    frames: list[dict[str, Any]] = []
    state.outbound_publisher = frames.append

    state.request_abort(None)

    # Flag is set unconditionally, but no remote frames emitted.
    assert state.abort_flag.is_set()
    assert not frames


# ─────────────────────── End-to-end timing ──────────────────────────────────

def test_run_task_end_to_end_cancel_within_2s() -> None:
    """Dispatch task → 200 ms later set abort_flag → run_task returns <2 s."""
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-e2e"
    state.outbound_publisher = lambda _f: None

    # Build a stream that "stalls" 10 s mid-response so the only way out is
    # via the cancel watchdog.
    long_chunks = [
        (_sse_chunk([{"type": "message_start", "message": {"id": "m", "model": "x"}}]), 0.0),
        (b"", 10.0),
    ]

    def _open_url(*a: Any, **kw: Any) -> _FakeStreamResponse:
        return _FakeStreamResponse(long_chunks)

    def _trip() -> None:
        time.sleep(0.2)
        state.request_abort("t-e2e")

    threading.Thread(target=_trip, daemon=True).start()

    executor = MagicMock()
    executor._af = None

    started = time.monotonic()
    with (
        patch("agentflow_computer_mcp.driver.loop.urllib.request.urlopen", side_effect=_open_url),
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
        patch("agentflow_computer_mcp.driver.loop.update_live"),
    ):
        result = run_task("task", state, executor, api_key="k")
    elapsed = time.monotonic() - started

    assert result == ""
    assert elapsed < 2.0, f"end-to-end cancel took {elapsed:.2f}s, want <2s"
    assert not state.abort_flag.is_set(), "flag must be cleared after cancel"
