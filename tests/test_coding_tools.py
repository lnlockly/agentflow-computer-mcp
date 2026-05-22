"""Behavioral tests for the coding-side tools (code_read_file / write / edit / run / list).

Covers happy paths, scope enforcement, and the run_command timing+cwd contract. The async
``run_command`` path uses ``asyncio.run`` directly because pytest-asyncio is not a dep
on this project.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow_computer_mcp.config import Scope
from agentflow_computer_mcp.driver.af_client import (
    AF_TOOL_DESCRIPTORS,
    AFClient,
    dispatch_af_tool,
)
from agentflow_computer_mcp.driver.desktop_tools import (
    DESKTOP_TOOLS,
    ToolExecutor,
    all_tool_descriptors,
)
from agentflow_computer_mcp.scope import ScopeDenied
from agentflow_computer_mcp.tools import code as code_tool

# ─── code_read_file ──────────────────────────────────────────────────────────


def test_read_file_returns_content_and_line_count(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "hello.py"
    target.write_text("line one\nline two\nline three\n")

    result = code_tool.read_file(str(target), scope=scope)
    assert result["line_count"] == 3
    assert result["truncated"] is False
    assert "line one" in result["content"]


def test_read_file_truncates_when_over_max_lines(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "big.txt"
    target.write_text("\n".join(str(i) for i in range(500)))

    result = code_tool.read_file(str(target), scope=scope, max_lines=10)
    assert result["truncated"] is True
    assert result["line_count"] >= 500
    assert result["content"].count("\n") == 10


def test_read_file_denies_outside_allow(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    with pytest.raises(ScopeDenied):
        code_tool.read_file("/etc/hosts", scope=scope)


def test_read_file_denies_hard_deny_paths(tmp_path: Path) -> None:
    # ~/.ssh is in HARD_DENY_PATHS, must be refused even with broad allow.
    scope = Scope(allow_paths=(str(Path.home()),))
    with pytest.raises(ScopeDenied):
        code_tool.read_file("~/.ssh/id_rsa", scope=scope)


# ─── code_write_file ─────────────────────────────────────────────────────────


def test_write_file_replace_round_trip(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "out.txt"

    code_tool.write_file(str(target), "hello", scope=scope, mode="replace")
    assert target.read_text() == "hello"

    code_tool.write_file(str(target), "fresh", scope=scope, mode="replace")
    assert target.read_text() == "fresh"


def test_write_file_append_concatenates(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "log.txt"
    code_tool.write_file(str(target), "first\n", scope=scope, mode="append")
    code_tool.write_file(str(target), "second\n", scope=scope, mode="append")

    assert target.read_text() == "first\nsecond\n"


def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    nested = tmp_path / "a" / "b" / "c.txt"
    code_tool.write_file(str(nested), "deep", scope=scope)
    assert nested.read_text() == "deep"


def test_write_file_rejects_unknown_mode(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    with pytest.raises(ValueError):
        code_tool.write_file(str(tmp_path / "x.txt"), "y", scope=scope, mode="prepend")


def test_write_file_blocked_outside_allow(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    with pytest.raises(ScopeDenied):
        code_tool.write_file("/tmp/wrong_root.txt", "x", scope=scope)


# ─── code_edit_file ──────────────────────────────────────────────────────────


def test_edit_file_single_replace(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "src.py"
    target.write_text("def foo():\n    return 1\n")

    result = code_tool.edit_file(
        str(target),
        find="return 1",
        replace="return 42",
        scope=scope,
        count=1,
    )
    assert result["replaced"] == 1
    assert "return 42" in target.read_text()


def test_edit_file_all_occurrences(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "config.txt"
    target.write_text("foo\nfoo\nfoo\n")

    result = code_tool.edit_file(
        str(target), find="foo", replace="bar", scope=scope, count="all"
    )
    assert result["replaced"] == 3
    assert target.read_text() == "bar\nbar\nbar\n"


def test_edit_file_raises_when_too_many_occurrences(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "dup.txt"
    target.write_text("x\nx\nx\n")

    with pytest.raises(ValueError, match="exceeds count"):
        code_tool.edit_file(str(target), find="x", replace="y", scope=scope, count=1)


def test_edit_file_raises_when_pattern_missing(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    target = tmp_path / "src.py"
    target.write_text("def foo(): pass\n")

    with pytest.raises(ValueError, match="not present"):
        code_tool.edit_file(str(target), find="nope", replace="yes", scope=scope)


# ─── code_run_command ────────────────────────────────────────────────────────


def test_run_command_returns_stdout_and_duration(tmp_path: Path) -> None:
    scope = Scope(
        allow_paths=(str(tmp_path),),
        shell_whitelist=("echo",),
    )
    result = asyncio.run(code_tool.run_command("echo hi", scope=scope, cwd=str(tmp_path)))
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]
    assert result["duration_ms"] >= 0
    assert result["cwd"] == str(tmp_path)


def test_run_command_blocks_unwhitelisted(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),), shell_whitelist=("echo",))
    with pytest.raises(ScopeDenied):
        asyncio.run(code_tool.run_command("rm -rf /", scope=scope, cwd=str(tmp_path)))


def test_run_command_rejects_bad_cwd(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),), shell_whitelist=("echo",))
    with pytest.raises(NotADirectoryError):
        asyncio.run(
            code_tool.run_command(
                "echo hi", scope=scope, cwd=str(tmp_path / "nope")
            )
        )


# ─── code_list_dir ───────────────────────────────────────────────────────────


def test_list_dir_returns_files_and_subdirs(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "nested.txt").write_text("n")

    result = code_tool.list_dir(str(tmp_path), scope=scope, depth=2)
    names = {Path(e["path"]).name for e in result["entries"]}
    assert "a.txt" in names
    assert "b" in names
    assert "nested.txt" in names


def test_list_dir_ignores_globs(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),))
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("nope")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib").write_text("lib")

    result = code_tool.list_dir(str(tmp_path), scope=scope, depth=3)
    names = {Path(e["path"]).name for e in result["entries"]}
    assert "keep.txt" in names
    assert ".git" not in names
    assert "node_modules" not in names
    assert "secret" not in names


# ─── Tool descriptors / executor surface ─────────────────────────────────────


def test_code_tool_descriptors_registered() -> None:
    names = {d["name"] for d in DESKTOP_TOOLS}
    expected = {
        "code_read_file",
        "code_write_file",
        "code_edit_file",
        "code_run_command",
        "code_list_dir",
    }
    assert expected.issubset(names)


def test_af_spawn_and_events_descriptors_present() -> None:
    names = {d["name"] for d in AF_TOOL_DESCRIPTORS}
    assert "af_spawn_subagent" in names
    assert "af_get_project_events" in names


def test_all_tools_appears_in_combined_list() -> None:
    names = {d["name"] for d in all_tool_descriptors()}
    assert "code_read_file" in names
    assert "af_spawn_subagent" in names


def test_executor_code_read_file_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "hi.txt"
    target.write_text("agent")
    scope = Scope(allow_paths=(str(tmp_path),))
    exec_ = ToolExecutor(
        last_cursor_ref=[0, 0],
        af_client=None,
        pw=type("Stub", (), {})(),
        scope=scope,
        state=None,
    )
    out, image = exec_.execute("code_read_file", {"path": str(target)})
    assert image is None
    body = json.loads(out)
    assert "agent" in body["content"]


def test_executor_code_write_file_uses_scope(tmp_path: Path) -> None:
    # confirm_before=() disables the native dialog so the test can assert the write path.
    scope = Scope(allow_paths=(str(tmp_path),), confirm_before=())
    exec_ = ToolExecutor(
        last_cursor_ref=[0, 0],
        af_client=None,
        pw=type("Stub", (), {})(),
        scope=scope,
        state=None,
    )
    out, _ = exec_.execute(
        "code_write_file", {"path": str(tmp_path / "x.txt"), "content": "z"}
    )
    body = json.loads(out)
    assert body.get("size_bytes") == 1
    assert (tmp_path / "x.txt").read_text() == "z"


def test_executor_code_write_file_denies_outside_scope(tmp_path: Path) -> None:
    scope = Scope(allow_paths=(str(tmp_path),), confirm_before=())
    exec_ = ToolExecutor(
        last_cursor_ref=[0, 0],
        af_client=None,
        pw=type("Stub", (), {})(),
        scope=scope,
        state=None,
    )
    out, _ = exec_.execute(
        "code_write_file",
        {"path": "/tmp/agentflow-test-blocked", "content": "x"},
    )
    body = json.loads(out)
    assert body["ok"] is False
    assert "denied" in body["error"] or "not in allow_paths" in body["error"]


# ─── af_spawn_subagent dispatch ──────────────────────────────────────────────


def test_dispatch_af_spawn_subagent_calls_client() -> None:
    client = AFClient(api_key="af_live_test")
    with patch.object(client, "spawn_project_task") as m:
        m.return_value = {
            "ok": True,
            "project_id": 7,
            "slug": "test-slug",
            "preview_url": None,
            "kind": "code_only",
            "status": "pending",
        }
        out = dispatch_af_tool(
            client, "af_spawn_subagent", {"brief": "build a thing"}
        )
    body = json.loads(out)
    assert body["ok"] is True
    assert body["project_id"] == 7
    m.assert_called_once()
    kwargs = m.call_args.kwargs
    assert kwargs["brief"] == "build a thing"
    assert kwargs["auto_approve"] is True


def test_dispatch_af_get_project_events_calls_client() -> None:
    client = AFClient(api_key="af_live_test")
    with patch.object(client, "get_project_events") as m:
        m.return_value = type(
            "R",
            (),
            {
                "ok": True,
                "status": 200,
                "body": {"items": [{"id": 1, "kind": "queued"}]},
                "error": None,
            },
        )()
        out = dispatch_af_tool(
            client,
            "af_get_project_events",
            {"project_id": 42, "since_event_id": 5, "limit": 25},
        )
    body = json.loads(out)
    assert body["ok"] is True
    m.assert_called_once_with(42, since_event_id=5, limit=25)
