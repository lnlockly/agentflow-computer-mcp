"""run_task must always report a terminal frame to the platform.

Regression coverage for the b4 device-use-case stall: when the model ends a
turn with a plain-text answer (stop_reason=end_turn, no task_complete tool
call), run_task used to return the answer WITHOUT publishing a task_complete
frame. The platform then left device_tasks.status stuck `dispatched` with an
empty result until the 15-min reaper marked it failed.

These tests assert:
1. The no-tools / end_turn success path publishes a task_complete with the
   answer text.
2. An LLM api-error response publishes a task_error.
3. DriverState records terminal frames (publish_outbound) and exposes them via
   terminal_emitted / reset_terminal — the hook task_worker uses for its
   fallback net.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from agentflow_computer_mcp.driver.loop import run_task
from agentflow_computer_mcp.driver.state import DriverState


def _run(state: DriverState, llm_resp: dict[str, Any]) -> str:
    executor = MagicMock()
    executor._af = None
    with (
        patch(
            "agentflow_computer_mcp.driver.loop.post_llm_cancellable",
            return_value=llm_resp,
        ),
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop._memory_save_outcome"),
        patch("agentflow_computer_mcp.driver.loop._build_memory_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
        patch("agentflow_computer_mcp.driver.loop.update_live"),
    ):
        return run_task("task", state, executor, api_key="k")


def test_no_tools_end_turn_publishes_task_complete() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-no-tools"
    frames: list[dict[str, Any]] = []
    state.outbound_publisher = frames.append

    # Model gives a final text answer and stops, never calling task_complete.
    resp = {
        "content": [{"type": "text", "text": "The foreground app is Safari."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = _run(state, resp)

    assert result == "The foreground app is Safari."
    completes = [f for f in frames if f.get("type") == "task_complete"]
    assert completes, f"expected a task_complete frame, got {[f.get('type') for f in frames]}"
    assert completes[0]["task_id"] == "t-no-tools"
    assert completes[0]["answer"] == "The foreground app is Safari."
    assert state.terminal_emitted("t-no-tools") is True


def test_api_error_publishes_task_error() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-api-err"
    frames: list[dict[str, Any]] = []
    state.outbound_publisher = frames.append

    resp = {"type": "error", "error": {"message": "boom"}}
    result = _run(state, resp)

    assert result == ""
    errors = [f for f in frames if f.get("type") == "task_error"]
    assert errors, f"expected a task_error frame, got {[f.get('type') for f in frames]}"
    assert errors[0]["task_id"] == "t-api-err"
    assert "api_error" in errors[0]["error"]
    assert state.terminal_emitted("t-api-err") is True


def test_terminal_tracking_records_and_resets() -> None:
    state = DriverState()
    assert state.terminal_emitted("x") is False
    state.publish_outbound({"type": "task_action", "task_id": "x", "action": "step"})
    assert state.terminal_emitted("x") is False  # non-terminal frame
    state.publish_outbound({"type": "task_complete", "task_id": "x", "answer": "ok"})
    assert state.terminal_emitted("x") is True
    state.reset_terminal("x")
    assert state.terminal_emitted("x") is False
