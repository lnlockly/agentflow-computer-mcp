from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from agentflow_computer_mcp.driver.loop import run_task
from agentflow_computer_mcp.driver.state import DriverState


def _run_task_with_responses(
    responses: list[dict[str, Any]],
    tool_outputs: list[tuple[str, None]],
    *,
    llm_costs: list[float] | None = None,
) -> tuple[str, list[dict[str, Any]], MagicMock]:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-guard"

    published: list[dict[str, Any]] = []
    state.outbound_publisher = published.append

    executor = MagicMock()
    executor._af = None
    executor.execute.side_effect = tool_outputs

    pending = list(responses)
    pending_costs = list(llm_costs or [])
    budget_patch = (
        patch(
            "agentflow_computer_mcp.driver.loop._budget_record_llm",
            side_effect=pending_costs,
        )
        if pending_costs
        else patch(
            "agentflow_computer_mcp.driver.loop._budget_record_llm",
            return_value=0.0,
        )
    )

    def _llm(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if not pending:
            raise AssertionError("unexpected extra LLM call")
        return pending.pop(0)

    with (
        patch("agentflow_computer_mcp.driver.loop.post_llm_cancellable", side_effect=_llm),
        budget_patch,
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
        patch("agentflow_computer_mcp.driver.loop.update_live"),
    ):
        result = run_task("create a file and verify it", state, executor, api_key="k")
    return result, published, executor


def test_run_task_rejects_task_complete_after_unresolved_tool_error() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-write",
                    "name": "code_write_file",
                    "input": {"path": "/tmp/x.txt", "content": "hello"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {"answer": "готово, файл создан"},
                }
            ],
            "stop_reason": "tool_use",
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"ok": False, "error": "scope denied"}), None),
            ("__DONE__", None),
        ],
    )

    assert result == ""
    assert executor.execute.call_count == 2
    assert any(
        frame.get("type") == "task_error"
        and str(frame.get("error", "")).startswith("completion_blocked_after_tool_error")
        for frame in published
    )
    assert not any(frame.get("type") == "task_complete" for frame in published)


def test_read_only_tool_does_not_clear_completion_guard() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-write",
                    "name": "code_write_file",
                    "input": {"path": "/tmp/x.txt", "content": "hello"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-read",
                    "name": "code_read_file",
                    "input": {"path": "/tmp/x.txt"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {"answer": "готово"},
                }
            ],
            "stop_reason": "tool_use",
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"ok": False, "error": "scope denied"}), None),
            (json.dumps({"content": "missing", "line_count": 0, "truncated": False}), None),
            ("__DONE__", None),
        ],
    )

    assert result == ""
    assert executor.execute.call_count == 3
    assert any(frame.get("type") == "task_error" for frame in published)
    assert not any(frame.get("type") == "task_complete" for frame in published)


def test_successful_mutation_clears_guard_and_allows_completion() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-write-1",
                    "name": "code_write_file",
                    "input": {"path": "/tmp/x.txt", "content": "bad"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-write-2",
                    "name": "code_write_file",
                    "input": {"path": "/tmp/x.txt", "content": "good"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {"answer": "готово, файл создан"},
                }
            ],
            "stop_reason": "tool_use",
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"ok": False, "error": "scope denied"}), None),
            (json.dumps({"ok": True, "size_bytes": 4}), None),
            ("__DONE__", None),
        ],
    )

    assert result == "готово, файл создан"
    assert executor.execute.call_count == 3
    assert any(frame.get("type") == "task_complete" for frame in published)
    assert not any(frame.get("type") == "task_error" for frame in published)


def test_task_complete_emits_accumulated_tokens_used() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-write",
                    "name": "code_write_file",
                    "input": {"path": "/tmp/x.txt", "content": "good"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 111, "output_tokens": 22},
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {"answer": "готово, файл создан"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 33, "output_tokens": 4},
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"ok": True, "size_bytes": 4}), None),
            ("__DONE__", None),
        ],
        llm_costs=[0.12, 0.03],
    )

    assert result == "готово, файл создан"
    assert executor.execute.call_count == 2
    complete = next(frame for frame in published if frame.get("type") == "task_complete")
    assert complete["tokens_used"] == 170
    assert complete["cost_usd"] == 0.15


def test_run_task_emits_error_instead_of_task_complete_on_max_iters() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-max-iters"

    published: list[dict[str, Any]] = []
    state.outbound_publisher = published.append

    executor = MagicMock()
    executor._af = None
    executor.execute.return_value = ("still working", None)

    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-loop",
                    "name": "list_windows",
                    "input": {},
                }
            ],
            "stop_reason": "tool_use",
        }
    ]

    def _llm(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return responses[0]

    with (
        patch("agentflow_computer_mcp.driver.loop.post_llm_cancellable", side_effect=_llm),
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
        patch("agentflow_computer_mcp.driver.loop.update_live"),
    ):
        result = run_task("do something long", state, executor, api_key="k", max_iters=1)

    assert result == ""
    assert any(
        frame.get("type") == "task_error" and "max_iters reached" in str(frame.get("error", ""))
        for frame in published
    )
    assert not any(frame.get("type") == "task_complete" for frame in published)


def test_run_task_compacts_older_image_history_between_iterations() -> None:
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-history"

    published: list[dict[str, Any]] = []
    state.outbound_publisher = published.append

    executor = MagicMock()
    executor._af = None
    executor.execute.side_effect = [
        ("screenshot", {"b64": "A" * 4096}),
        ("__DONE__", None),
    ]

    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-capture",
                    "name": "screen_capture",
                    "input": {},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {"answer": "готово"},
                }
            ],
            "stop_reason": "tool_use",
        },
    ]
    seen_payloads: list[dict[str, Any]] = []

    def _llm(_url: str, _api_key: str, payload: dict[str, Any], _abort_flag: Any) -> dict[str, Any]:
        seen_payloads.append(json.loads(json.dumps(payload)))
        return responses[len(seen_payloads) - 1]

    with (
        patch("agentflow_computer_mcp.driver.loop.post_llm_cancellable", side_effect=_llm),
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value="seed-image"),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
        patch("agentflow_computer_mcp.driver.loop.update_live"),
    ):
        result = run_task("capture and finish", state, executor, api_key="k")

    assert result == "готово"
    assert len(seen_payloads) == 2
    initial_message = seen_payloads[1]["messages"][0]["content"]
    latest_message = seen_payloads[1]["messages"][-1]["content"]
    assert not any(block.get("type") == "image" for block in initial_message)
    assert any("omitted from history" in block.get("text", "") for block in initial_message)
    assert any(
        isinstance(block.get("content"), list)
        and any(nested.get("type") == "image" for nested in block["content"])
        for block in latest_message
    )


def test_run_task_rejects_task_complete_when_answer_explicitly_reports_failure() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-read",
                    "name": "code_read_file",
                    "input": {"path": "/tmp/x.txt"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {
                        "answer": "❌ Задача не выполнена: файл создать невозможно в текущей среде."
                    },
                }
            ],
            "stop_reason": "tool_use",
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"content": "", "line_count": 0, "truncated": False}), None),
            ("__DONE__", None),
        ],
    )

    assert result == ""
    assert executor.execute.call_count == 2
    assert any(
        frame.get("type") == "task_error"
        and "task_complete_reported_failure" in str(frame.get("error", ""))
        for frame in published
    )
    assert not any(frame.get("type") == "task_complete" for frame in published)


def test_cost_cap_allows_final_task_complete_after_successful_work() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-write",
                    "name": "code_write_file",
                    "input": {"path": "/tmp/x.txt", "content": "done"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-done",
                    "name": "task_complete",
                    "input": {"answer": "/tmp/x.txt"},
                }
            ],
            "stop_reason": "tool_use",
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"ok": True, "size_bytes": 4}), None),
            ("__DONE__", None),
        ],
        llm_costs=[0.6, 0.01],
    )

    assert result == "/tmp/x.txt"
    assert executor.execute.call_count == 2
    assert any(frame.get("type") == "task_complete" for frame in published)
    assert not any(frame.get("type") == "task_error" for frame in published)


def test_cost_cap_blocks_extra_work_before_non_terminal_tool() -> None:
    responses = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-read-1",
                    "name": "code_read_file",
                    "input": {"path": "/tmp/x.txt"},
                }
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu-read-2",
                    "name": "code_read_file",
                    "input": {"path": "/tmp/y.txt"},
                }
            ],
            "stop_reason": "tool_use",
        },
    ]

    result, published, executor = _run_task_with_responses(
        responses,
        [
            (json.dumps({"content": "x", "line_count": 1, "truncated": False}), None),
        ],
        llm_costs=[0.6, 0.01],
    )

    assert result == ""
    assert executor.execute.call_count == 1
    assert any(
        frame.get("type") == "task_error"
        and "cost_cap_exceeded" in str(frame.get("error", ""))
        for frame in published
    )
    assert not any(frame.get("type") == "task_complete" for frame in published)
