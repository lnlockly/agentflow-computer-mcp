"""Tests for the gpt-5 / o-series `reasoning_effort` pass-through in the
daemon AI loop.

Verifies:
1. `_is_gpt5_family` returns True for gpt-5.* / gpt-5.3-codex / o-series,
   False for claude-* and empty strings.
2. `_augment_body_with_reasoning` injects `metadata.reasoning_effort` for
   gpt-5 family models and leaves claude bodies untouched.
3. `run_task` actually attaches `metadata.reasoning_effort=medium` to the
   outgoing body when the model is `gpt-5.3-codex`, and omits it for
   `claude-sonnet-4-6`.
4. `task_scope.get("reasoning_effort")` overrides the daemon default.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from agentflow_computer_mcp.driver.loop import (
    DEFAULT_REASONING_EFFORT,
    _augment_body_with_reasoning,
    _is_gpt5_family,
    run_task,
    task_worker,
)
from agentflow_computer_mcp.driver.state import DriverState


# ─────────────────────── _is_gpt5_family ────────────────────────────────────

def test_is_gpt5_family_positive() -> None:
    assert _is_gpt5_family("gpt-5") is True
    assert _is_gpt5_family("gpt-5.3-codex") is True
    assert _is_gpt5_family("gpt-5.4") is True
    assert _is_gpt5_family("GPT-5.3-CODEX") is True  # case-insensitive
    assert _is_gpt5_family("o1") is True
    assert _is_gpt5_family("o3-mini") is True
    assert _is_gpt5_family("o4-preview") is True


def test_is_gpt5_family_negative() -> None:
    assert _is_gpt5_family("") is False
    assert _is_gpt5_family("claude-sonnet-4-6") is False
    assert _is_gpt5_family("claude-haiku-4-5") is False
    assert _is_gpt5_family("gpt-4o") is False
    assert _is_gpt5_family("openai") is False  # `o` followed by non-digit


# ─────────────────────── _augment_body_with_reasoning ───────────────────────

def test_augment_body_attaches_metadata_for_gpt5() -> None:
    body = {"model": "gpt-5.3-codex", "max_tokens": 1024, "messages": []}
    out = _augment_body_with_reasoning(body, "gpt-5.3-codex", "medium")
    assert out["metadata"] == {"reasoning_effort": "medium"}
    # Untouched fields preserved.
    assert out["model"] == "gpt-5.3-codex"
    assert out["max_tokens"] == 1024


def test_augment_body_preserves_existing_metadata() -> None:
    body = {"model": "gpt-5.3-codex", "metadata": {"user_id": "u1"}}
    out = _augment_body_with_reasoning(body, "gpt-5.3-codex", "high")
    assert out["metadata"] == {"user_id": "u1", "reasoning_effort": "high"}


def test_augment_body_skips_claude() -> None:
    body = {"model": "claude-sonnet-4-6", "messages": []}
    out = _augment_body_with_reasoning(body, "claude-sonnet-4-6", "medium")
    assert "metadata" not in out


def test_augment_body_skips_when_effort_empty() -> None:
    body = {"model": "gpt-5.3-codex"}
    out = _augment_body_with_reasoning(body, "gpt-5.3-codex", "")
    assert "metadata" not in out
    out = _augment_body_with_reasoning(body, "gpt-5.3-codex", None)
    assert "metadata" not in out


# ─────────────────────── run_task wires the field into upstream body ────────

def _collect_bodies_run_task(model: str, reasoning_effort: str) -> list[dict[str, Any]]:
    """Spin run_task through one LLM round-trip with a mocked transport,
    capturing the exact body dict that would be sent upstream."""
    state = DriverState()
    state.busy = True
    state.current_task_id = "t-eff"
    state.outbound_publisher = lambda _f: None

    captured: list[dict[str, Any]] = []

    def _fake_llm(_url: str, _key: str, payload: dict[str, Any], _abort: Any) -> dict[str, Any]:
        # Snapshot before the loop mutates anything.
        captured.append({k: v for k, v in payload.items()})
        return {
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    executor = MagicMock()
    executor._af = None

    with (
        patch("agentflow_computer_mcp.driver.loop.post_llm_cancellable", side_effect=_fake_llm),
        patch("agentflow_computer_mcp.driver.loop._fetch_skills_prompt_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.jpeg_b64_full", return_value=""),
        patch("agentflow_computer_mcp.driver.loop.get_window_list", return_value=[]),
        patch("agentflow_computer_mcp.driver.loop.update_live"),
        patch("agentflow_computer_mcp.driver.loop._build_memory_block", return_value=""),
        patch("agentflow_computer_mcp.driver.loop._memory_save_outcome"),
    ):
        run_task(
            "test task",
            state,
            executor,
            api_key="k",
            model=model,
            reasoning_effort=reasoning_effort,
        )
    return captured


def test_run_task_sends_reasoning_effort_for_gpt5_codex() -> None:
    bodies = _collect_bodies_run_task("gpt-5.3-codex", "medium")
    assert bodies, "expected at least one LLM body capture"
    body = bodies[0]
    assert body["model"] == "gpt-5.3-codex"
    assert body.get("metadata", {}).get("reasoning_effort") == "medium"


def test_run_task_omits_reasoning_effort_for_claude() -> None:
    bodies = _collect_bodies_run_task("claude-sonnet-4-6", "medium")
    assert bodies, "expected at least one LLM body capture"
    body = bodies[0]
    assert body["model"] == "claude-sonnet-4-6"
    assert "metadata" not in body, body


# ─────────────────────── task_worker scope-level override ───────────────────

def test_task_worker_task_scope_overrides_defaults() -> None:
    """`task_scope.model` and `task_scope.reasoning_effort` win over the
    daemon-wide defaults at dispatch time."""
    state = DriverState()
    state.shutdown_flag.clear()

    # Pre-load one task with scope overrides, then trip shutdown once it's
    # been picked up so the worker exits.
    state.task_queue.put(
        (
            "t-override",
            "scoped task",
            {"model": "gpt-5.3-codex", "reasoning_effort": "high"},
        )
    )

    captured: dict[str, Any] = {}

    def _fake_run_task(
        _task: str,
        _state: Any,
        _executor: Any,
        _api_key: str,
        **kwargs: Any,
    ) -> str:
        captured.update(kwargs)
        state.shutdown_flag.set()
        return ""

    executor = MagicMock()
    executor.base_scope = MagicMock(budget_usd=0)

    with patch("agentflow_computer_mcp.driver.loop.run_task", side_effect=_fake_run_task):
        # Daemon defaults: claude-* + "low" — scope must override both.
        task_worker(
            state,
            executor,
            api_key="k",
            llm_url="http://x",
            model="claude-sonnet-4-6",
            reasoning_effort="low",
        )

    assert captured["model"] == "gpt-5.3-codex"
    assert captured["reasoning_effort"] == "high"


# ─────────────────────── DEFAULT constant — owner contract ──────────────────

def test_default_reasoning_effort_is_medium() -> None:
    """Owner contract: daemon ships with fast-mode (`medium`) out of the
    box. Changing this default = product decision that must be approved
    by the owner. Locking it down with a test."""
    assert DEFAULT_REASONING_EFFORT in ("low", "medium", "high")
    # Production default is `medium` per owner directive 2026-05-25.
    # If you flip this, update the RAG and chat with the owner first.
    import os as _os
    if not _os.environ.get("AF_DESKTOP_REASONING_EFFORT"):
        assert DEFAULT_REASONING_EFFORT == "medium"
