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


def test_composed_prompt_keeps_dev_server_alive_after_opencode_exits(workspace):
    # Regression: the brief MUST tell opencode to background+disown the
    # dev server so it survives opencode's own exit. Previously the
    # server was spawned as a foreground child of the single-shot
    # `opencode run` and died ~2s later, leaving the project's public
    # URL returning 502 forever.
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "static landing",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
    )
    composed = spawner.calls[0]["brief"]
    assert "nohup" in composed
    assert "disown" in composed


def test_composed_prompt_does_not_inject_listen_flag(workspace):
    # Regression: the previous brief explicitly suggested `next dev -H
    # 0.0.0.0 -p 3000` and opencode generalised that to `npm run dev
    # -- --listen 0.0.0.0:3000` for our `serve`-based static-starter,
    # which `serve` v14 rejects. The new brief must steer opencode to
    # use the template's own dev script verbatim, no host/port flags.
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "static landing",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
    )
    composed = spawner.calls[0]["brief"]
    # The brief tells opencode to run the template's dev script
    # verbatim and forbids extra flags. Phrased as "no flags added"
    # rather than naming each banned flag — opencode followed the long
    # list literally in earlier versions, the shorter rule is robust.
    lc = composed.lower()
    assert "no flags added" in lc
    assert "do not modify package.json" in lc


def test_composed_prompt_does_not_embed_literal_port_number(workspace):
    # Deterministic-port policy: the daemon pre-patches package.json so
    # `npm run dev` reads `$PORT`. The brief MUST NOT mention the literal
    # port number — that would lead opencode to also try to bind it
    # explicitly (wrong flag, wrong stack) and double-set, or to choose
    # a port different from what the platform expects.
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "owner/repo",
        "demo",
        42,
        "spa brief",
        workspace_root=str(workspace),
        port=3742,
        run=runner,
        spawn_opencode=spawner,
    )
    composed = spawner.calls[0]["brief"]
    assert "3742" not in composed
    assert "$PORT" in composed


def test_package_json_dev_scripts_rewritten_to_use_port_env(tmp_path):
    # Universal port injection: the daemon rewrites the template's
    # `scripts.dev` so `${PORT:-N}` shell expansion picks up the env
    # var, with the literal default preserved for local dev outside the
    # pod. Each common dev-server shape must round-trip correctly.
    cases = {
        # serve v14 — explicit -l N
        'serve -l 3000 -L .':
            'serve -l ${PORT:-3000} -L .',
        # serve without -l → append explicit listen
        'serve .':
            'serve . -l ${PORT:-4242}',
        # next dev with no flags
        'next dev':
            'next dev -p ${PORT:-4242} -H 0.0.0.0',
        # next dev with explicit -p already → re-normalised
        'next dev -p 8080':
            'next dev -p ${PORT:-4242} -H 0.0.0.0',
        # next dev with -p and -H → both flags stripped + re-added
        'next dev -p 4000 -H 127.0.0.1':
            'next dev -p ${PORT:-4242} -H 0.0.0.0',
        # vite with no flags
        'vite':
            'vite --port ${PORT:-4242} --host 0.0.0.0',
        # python http.server with literal port
        'python -m http.server 8000':
            'python -m http.server ${PORT:-8000}',
        # unrecognised command — left alone
        'echo hello':
            'echo hello',
    }
    for source, expected in cases.items():
        got = ab.rewrite_dev_command_for_port(source, 4242)
        assert got == expected, f'rewrite of {source!r} -> {got!r}, expected {expected!r}'


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
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_project_id"
