"""Unit tests for the hosted-daemon ``agent_dev_brief`` tool.

The real flow clones a GitHub repo + spawns ``opencode run "<brief>"``
as a long-lived background process inside the hosted pod. Tests fake
every side effect so they pass without a network or the opencode binary.

Covered:
    * happy path — clone + opencode spawn → ok response
    * git clone failure → ``git_clone_failed``
    * opencode spawn failure → propagated error
    * malformed inputs (bad repo, bad slug, missing brief)
"""

from __future__ import annotations

from typing import Any

import pytest

from agentflow_computer_mcp.driver.tools import agent_brief as ab


class FakeRunner:
    def __init__(self, scripted: dict[str, dict[str, Any]] | None = None):
        self.scripted = scripted or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd, cwd=None, *, timeout=120, env=None):
        self.calls.append({"cmd": list(cmd), "cwd": cwd})
        head = cmd[0]
        if head in self.scripted:
            return self.scripted[head]
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class FakeOpencodeSpawner:
    def __init__(self, *, ok: bool = True, pid: int = 1234, error: str | None = None):
        self.ok = ok
        self.pid = pid
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, brief, cwd, *, pid_file, log_file, env=None, opencode_bin="opencode"):
        self.calls.append(
            {
                "brief": brief,
                "cwd": cwd,
                "pid_file": pid_file,
                "log_file": log_file,
                "opencode_bin": opencode_bin,
            }
        )
        if not self.ok:
            return {"ok": False, "error": self.error or "spawn_failed"}
        return {"ok": True, "pid": self.pid}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_happy_path_clones_and_spawns_opencode(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=9999)
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "build me a coffee landing",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is True
    assert result["opencode_pid"] == 9999
    assert result["project_dir"].endswith("/proj-demo")
    # git clone was the first subprocess call
    assert runner.calls[0]["cmd"][0] == "git"
    assert runner.calls[0]["cmd"][1] == "clone"
    # opencode received the brief in the composed prompt
    assert len(spawner.calls) == 1
    assert "build me a coffee landing" in spawner.calls[0]["brief"]


def test_invalid_repo_full_rejected_before_side_effects(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner()
    result = ab.agent_dev_brief(
        "not-a-valid-repo",
        "demo",
        42,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result == {"ok": False, "error": "invalid_template_repo_full"}
    assert runner.calls == []
    assert spawner.calls == []


def test_invalid_slug_rejected(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner()
    result = ab.agent_dev_brief(
        "owner/repo",
        "bad slug with spaces",
        42,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_slug"


def test_missing_brief_rejected(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner()
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "   ",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["error"] == "missing_brief"


def test_git_clone_failure_short_circuits(workspace):
    runner = FakeRunner(
        scripted={"git": {"exit_code": 128, "stdout": "", "stderr": "fatal: repo not found"}}
    )
    spawner = FakeOpencodeSpawner()
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["error"] == "git_clone_failed"
    assert spawner.calls == []


def test_opencode_spawn_failure_surfaces_error(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(ok=False, error="opencode_not_found")
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["error"] == "opencode_not_found"


def test_invalid_project_id_rejected(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner()
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        0,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_project_id"


def test_opencode_died_at_startup_surfaces_log_excerpt(workspace, tmp_path):
    """If opencode dies inside the readback window, surface the log."""
    log_file = tmp_path / "opencode.log"
    log_file.write_text("[opencode] panic: ANTHROPIC_API_KEY missing\nexit code 1\n")

    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=4242)
    # pid_alive returns False immediately → simulate the process having died
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        log_file=str(log_file),
        pid_alive=lambda _pid: False,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["error"] == "opencode_died_at_startup"
    assert "ANTHROPIC_API_KEY missing" in result["detail"]


def test_opencode_alive_returns_boot_log_excerpt(workspace, tmp_path):
    log_file = tmp_path / "opencode.log"
    log_file.write_text("[opencode] starting...\n[opencode] reading repo...\n")

    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=7777)
    result = ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "brief",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        log_file=str(log_file),
        pid_alive=lambda _pid: True,
        sleep=lambda _s: None,
    )
    assert result["ok"] is True
    assert "starting" in result["opencode_boot_log"]
