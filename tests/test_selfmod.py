"""Tests for selfmod queue + worker."""
from __future__ import annotations

import io
import json
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow_computer_mcp.driver import selfmod
from agentflow_computer_mcp.driver.selfmod_worker import (
    SelfmodWorker,
    build_prompt,
    detect_forbidden_in_diff,
    parse_claude_output,
    process_request,
)


@pytest.fixture(autouse=True)
def _isolated_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(selfmod.QUEUE_DIR_ENV, str(tmp_path))
    return tmp_path


def test_request_change_writes_queue_entry(tmp_path: Path) -> None:
    out = selfmod.request_change("reason A", "do X", "high")
    assert out["queued"] is True
    assert out["status"] == "queued"
    assert out["request_id"].startswith("sm-")

    rows = selfmod.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["reason"] == "reason A"
    assert rows[0]["urgency"] == "high"


def test_request_change_validates_inputs() -> None:
    with pytest.raises(ValueError):
        selfmod.request_change("", "x")
    with pytest.raises(ValueError):
        selfmod.request_change("y", "")
    with pytest.raises(ValueError):
        selfmod.request_change("y", "x", "urgent")  # type: ignore[arg-type]


def test_rate_limit_marks_second_request_throttled() -> None:
    first = selfmod.request_change("first", "change A")
    second = selfmod.request_change("second", "change B")
    assert first["queued"] is True
    assert second["queued"] is False
    assert second["status"] == "throttled"

    rows = selfmod.list_recent(limit=10)
    statuses = [r["status"] for r in rows]
    assert "queued" in statuses
    assert "throttled" in statuses


def test_pop_next_queued_atomic() -> None:
    selfmod.request_change("r1", "c1")
    popped = selfmod.pop_next_queued()
    assert popped is not None
    assert popped["status"] == "in_progress"
    again = selfmod.pop_next_queued()
    assert again is None  # already claimed


def test_update_status_patches_row() -> None:
    out = selfmod.request_change("r", "c")
    rid = out["request_id"]
    ok = selfmod.update_status(rid, "merged", pr_url="https://example/pr/1")
    assert ok is True
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "merged"
    assert rows[0]["pr_url"] == "https://example/pr/1"


def test_cancel_queued_then_idempotent() -> None:
    out = selfmod.request_change("r", "c")
    rid = out["request_id"]
    assert selfmod.cancel(rid) is True
    assert selfmod.cancel(rid) is False  # already cancelled
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "cancelled"


def test_requeue_resets_failed_row() -> None:
    out = selfmod.request_change("r", "c")
    rid = out["request_id"]
    selfmod.update_status(rid, "failed", error="boom")
    assert selfmod.requeue(rid) is True
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "queued"
    assert rows[0]["error"] is None


def test_detect_forbidden_in_diff() -> None:
    diff = [
        "src/agentflow_computer_mcp/driver/loop.py",
        ".github/workflows/ci.yml",
        "tests/test_x.py",
    ]
    hits = detect_forbidden_in_diff(diff)
    assert hits == [".github/workflows/ci.yml"]


def test_detect_forbidden_blocks_auth_changes() -> None:
    diff = ["src/agentflow_computer_mcp/auth.py"]
    assert detect_forbidden_in_diff(diff)


def test_parse_claude_output_success() -> None:
    out = "did stuff\nPR: https://github.com/org/repo/pull/42\n"
    pr, rej = parse_claude_output(out)
    assert pr == "https://github.com/org/repo/pull/42"
    assert rej is None


def test_parse_claude_output_reject() -> None:
    out = "thought about it\nREJECT: would touch auth.py\n"
    pr, rej = parse_claude_output(out)
    assert pr is None
    assert rej == "would touch auth.py"


def test_parse_claude_output_malformed() -> None:
    pr, rej = parse_claude_output("nothing useful")
    assert pr is None and rej is None


def test_build_prompt_contains_inputs() -> None:
    p = build_prompt({"reason": "fix X", "suggested_change": "edit Y", "urgency": "high"})
    assert "fix X" in p
    assert "edit Y" in p
    assert "high" in p
    assert "REJECT" in p


def _fake_proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_process_request_pr_opened_no_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend cwd is a repo so we don't bail.
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))

    selfmod.request_change("r", "c")
    request = selfmod.pop_next_queued()
    assert request is not None

    with patch(
        "agentflow_computer_mcp.driver.selfmod_worker.changed_files_against_main",
        return_value=["src/agentflow_computer_mcp/driver/loop.py"],
    ):
        result = process_request(
            request,
            automerge=False,
            autoapply=False,
            spawn=lambda prompt, cwd, t: _fake_proc("PR: https://github.com/x/y/pull/1\n"),
        )

    assert result["status"] == "pr_opened"
    assert result["pr_url"] == "https://github.com/x/y/pull/1"
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "pr_opened"
    assert rows[0]["pr_url"] == "https://github.com/x/y/pull/1"


def test_process_request_reject_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))

    selfmod.request_change("r", "c")
    request = selfmod.pop_next_queued()
    assert request is not None

    result = process_request(
        request,
        automerge=False,
        autoapply=False,
        spawn=lambda prompt, cwd, t: _fake_proc("thinking...\nREJECT: refused\n", returncode=1),
    )
    assert result["status"] == "rejected"
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "rejected"
    assert rows[0]["error"] == "refused"


def test_process_request_forbidden_diff_blocks_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))

    selfmod.request_change("r", "c")
    request = selfmod.pop_next_queued()
    assert request is not None

    with patch(
        "agentflow_computer_mcp.driver.selfmod_worker.changed_files_against_main",
        return_value=[".github/workflows/ci.yml", "src/foo.py"],
    ):
        result = process_request(
            request,
            automerge=True,
            autoapply=False,
            spawn=lambda prompt, cwd, t: _fake_proc("PR: https://example/pr/9\n"),
        )

    assert result["status"] == "rejected"
    assert result["reason"] == "forbidden_paths"
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "rejected"
    assert ".github/workflows" in (rows[0]["error"] or "")


def test_process_request_automerge_and_autoapply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))

    selfmod.request_change("r", "c")
    request = selfmod.pop_next_queued()
    assert request is not None

    calls: dict[str, int] = {"merge": 0, "apply": 0}

    def fake_merge(url: str, cwd: Path) -> tuple[bool, str]:
        calls["merge"] += 1
        return True, "merged"

    def fake_apply(cwd: Path) -> tuple[bool, str]:
        calls["apply"] += 1
        return True, "upgraded"

    with patch(
        "agentflow_computer_mcp.driver.selfmod_worker.changed_files_against_main",
        return_value=["src/agentflow_computer_mcp/driver/loop.py"],
    ):
        result = process_request(
            request,
            automerge=True,
            autoapply=True,
            spawn=lambda prompt, cwd, t: _fake_proc("PR: https://x/pr/2\n"),
            merge=fake_merge,
            apply=fake_apply,
        )

    assert result["status"] == "merged"
    assert calls == {"merge": 1, "apply": 1}
    rows = selfmod.list_recent()
    assert rows[0]["status"] == "merged"


def test_process_request_no_git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))  # no .git
    selfmod.request_change("r", "c")
    request = selfmod.pop_next_queued()
    assert request is not None

    result = process_request(
        request,
        automerge=False,
        autoapply=False,
        spawn=lambda prompt, cwd, t: _fake_proc("PR: https://x/pr/3\n"),
    )
    assert result["status"] == "failed"


def test_process_request_handles_subprocess_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))

    selfmod.request_change("r", "c")
    request = selfmod.pop_next_queued()
    assert request is not None

    def raise_timeout(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    result = process_request(
        request,
        automerge=False,
        autoapply=False,
        spawn=raise_timeout,
    )
    assert result["status"] == "failed"
    assert result["error"] == "timeout"


def test_worker_drains_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SELFMOD_REPO_PATH", str(tmp_path))
    selfmod.request_change("r", "c")

    processed: list[dict[str, object]] = []

    def fake_process(request: dict[str, object], **kwargs: object) -> dict[str, object]:
        processed.append(request)
        return {"status": "pr_opened"}

    with patch(
        "agentflow_computer_mcp.driver.selfmod_worker.process_request",
        side_effect=fake_process,
    ):
        worker = SelfmodWorker(automerge=False, autoapply=False, poll_interval=0.05)
        worker.start()
        deadline = time.time() + 3
        while time.time() < deadline and not processed:
            time.sleep(0.05)
        worker.stop()
        worker._thread.join(timeout=2)  # type: ignore[union-attr]

    assert len(processed) == 1


# --- CLI surface ----------------------------------------------------------------


def test_cli_selfmod_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(selfmod.QUEUE_DIR_ENV, str(tmp_path))
    from agentflow_computer_mcp.desktop_cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["selfmod", "list"])
    assert rc == 0
    assert "no selfmod" in buf.getvalue()


def test_cli_selfmod_list_with_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(selfmod.QUEUE_DIR_ENV, str(tmp_path))
    from agentflow_computer_mcp.desktop_cli import main

    selfmod.request_change("speed up capture", "raise fps default to 30")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["selfmod", "list"])
    assert rc == 0
    output = buf.getvalue()
    assert "queued" in output
    assert "speed up capture" in output


def test_cli_selfmod_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(selfmod.QUEUE_DIR_ENV, str(tmp_path))
    from agentflow_computer_mcp.desktop_cli import main

    r = selfmod.request_change("r", "c")
    rid = r["request_id"]

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["selfmod", "cancel", rid])
    assert rc == 0
    assert "cancelled" in buf.getvalue()


def test_cli_run_parser_accepts_selfmod_flags() -> None:
    from agentflow_computer_mcp.desktop_cli import build_parser

    p = build_parser()
    args = p.parse_args(["run", "--no-selfmod"])
    assert args.no_selfmod is True
    args = p.parse_args(["run", "--selfmod-automerge", "--selfmod-autoapply"])
    assert args.selfmod_automerge is True
    assert args.selfmod_autoapply is True


def test_executor_dispatches_selfmod_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(selfmod.QUEUE_DIR_ENV, str(tmp_path))
    from agentflow_computer_mcp.driver.desktop_tools import ToolExecutor

    executor = ToolExecutor(last_cursor_ref=[0, 0], af_client=None)
    out, image = executor.execute(
        "selfmod_request_change",
        {"reason": "x", "suggested_change": "y"},
    )
    assert image is None
    payload = json.loads(out)
    assert payload["status"] == "queued"

    out2, _ = executor.execute("selfmod_list_recent", {"limit": 5})
    payload2 = json.loads(out2)
    assert len(payload2["items"]) == 1
