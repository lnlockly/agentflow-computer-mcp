"""Unit tests for the hosted-daemon ``project_clone_and_setup`` tool.

The real flow runs ``git clone`` + ``npm install`` + spawns a Next.js
dev server on port 3000 inside a hosted pod. Tests fake every side
effect (subprocess, sleep, port probe, HTTP) so they pass without a
network, a node toolchain, or a real GitHub repo.

Covered:
    * happy path — clone + install + spawn + port ready + report
    * port never opens — ``port_reachable=False`` + report reflects it
    * git clone failure → ``git_clone_failed`` (no install, no spawn)
    * install failure → ``install_failed`` (no spawn, no port wait)
    * malformed inputs (bad repo, bad slug) caught before any side effect
    * missing internal secret / api_base caught early
    * package-manager + dev-command detection from synthetic project dirs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentflow_computer_mcp.driver.tools import project_setup as ps

# --- shared fakes ---------------------------------------------------------


class FakeRunner:
    """Records every subprocess invocation and returns scripted results."""

    def __init__(self, scripted: dict[str, dict[str, Any]] | None = None):
        # Key is the first arg ("git", "npm", "pnpm", "yarn"); value is the
        # result dict. Unknown commands default to exit_code=0.
        self.scripted = scripted or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd, cwd=None, *, timeout=120, env=None):
        self.calls.append(
            {"cmd": list(cmd), "cwd": cwd, "timeout": timeout, "env_set": env is not None}
        )
        head = cmd[0]
        if head in self.scripted:
            return self.scripted[head]
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class FakeSpawner:
    def __init__(self, *, ok: bool = True, pid: int = 4242, error: str | None = None):
        self.ok = ok
        self.pid = pid
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd, cwd, *, pid_file, log_file, env=None):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "pid_file": pid_file})
        if not self.ok:
            return {"ok": False, "error": self.error or "spawn_failed"}
        return {"ok": True, "pid": self.pid}


class FakePortCheck:
    def __init__(self, ready_after: int = 0):
        self.ready_after = ready_after
        self.calls = 0

    def __call__(self, host, port):
        self.calls += 1
        return self.calls > self.ready_after


class FakeHttpPost:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        self.body = body or {"ok": True}
        self.calls: list[tuple[str, dict, str]] = []

    def __call__(self, url, body, internal_secret):
        self.calls.append((url, body, internal_secret))
        return self.status, self.body


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _seed_project_dir(workspace: Path, slug: str, package_manager: str = "npm") -> Path:
    """Make `_default_run` happy: a real project dir at /workspace/proj-<slug>.

    The fake runner doesn't actually clone, so we materialise the dir
    + lockfile + package.json the same way a real clone would, then
    inject scripted results that match.
    """
    proj = workspace / f"proj-{slug}"
    proj.mkdir(parents=True, exist_ok=True)
    # Realistic package.json so detect_dev_command picks "dev" + Next flag.
    pkg = {
        "name": slug,
        "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
        "dependencies": {"next": "14.2.0", "react": "18.3.1"},
    }
    (proj / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    lockfile = {
        "npm": "package-lock.json",
        "pnpm": "pnpm-lock.yaml",
        "yarn": "yarn.lock",
    }[package_manager]
    (proj / lockfile).write_text("{}", encoding="utf-8")
    return proj


# --- input validation -----------------------------------------------------


def test_invalid_repo_full_rejected_before_any_side_effect():
    runner = FakeRunner()
    spawner = FakeSpawner()
    http = FakeHttpPost()
    res = ps.project_clone_and_setup(
        template_repo_full="not-a-valid-repo;rm -rf /",
        slug="abc",
        project_id=1,
        api_base="https://x/y",
        internal_secret="s",
        run=runner,
        spawn_background=spawner,
        port_check=FakePortCheck(0),
        sleep=lambda _: None,
        http_post=http,
    )
    assert res["ok"] is False
    assert res["error"] == "invalid_template_repo_full"
    assert runner.calls == []
    assert spawner.calls == []
    assert http.calls == []


def test_invalid_slug_rejected():
    res = ps.project_clone_and_setup(
        template_repo_full="vercel/next.js",
        slug="bad slug with spaces",
        project_id=1,
        api_base="https://x/y",
        internal_secret="s",
        run=FakeRunner(),
        spawn_background=FakeSpawner(),
        port_check=FakePortCheck(0),
        sleep=lambda _: None,
        http_post=FakeHttpPost(),
    )
    assert res["ok"] is False
    assert res["error"] == "invalid_slug"


def test_missing_internal_secret_rejected():
    res = ps.project_clone_and_setup(
        template_repo_full="vercel/next.js",
        slug="abc",
        project_id=1,
        api_base="https://x/y",
        internal_secret="",
        run=FakeRunner(),
        spawn_background=FakeSpawner(),
        port_check=FakePortCheck(0),
        sleep=lambda _: None,
        http_post=FakeHttpPost(),
    )
    assert res["ok"] is False
    assert res["error"] == "missing_backend_config"


# --- happy path -----------------------------------------------------------


def test_happy_path_clones_installs_spawns_and_reports(workspace, monkeypatch):
    # Seed dir on `git clone` so detect_pm + detect_dev see a real project.
    runner = FakeRunner()

    real_default_run = runner.__call__

    def runner_with_seed(cmd, cwd=None, *, timeout=120, env=None):
        result = real_default_run(cmd, cwd=cwd, timeout=timeout, env=env)
        if cmd[0] == "git" and len(cmd) >= 2 and cmd[1] == "clone":
            _seed_project_dir(workspace, "abc")
        return result

    spawner = FakeSpawner(pid=9999)
    port_check = FakePortCheck(ready_after=2)  # opens on the 3rd attempt
    http = FakeHttpPost(status=200, body={"ok": True})

    res = ps.project_clone_and_setup(
        template_repo_full="vercel/next.js",
        slug="abc",
        project_id=42,
        api_base="https://api.example/_agents",
        internal_secret="internal-shared-secret",
        workspace_root=str(workspace),
        port=3000,
        dev_wait_timeout_s=10,
        run=runner_with_seed,
        spawn_background=spawner,
        port_check=port_check,
        sleep=lambda _s: None,
        http_post=http,
    )

    assert res["ok"] is True, res
    assert res["port_reachable"] is True
    assert res["dev_pid"] == 9999
    assert res["package_manager"] == "npm"
    assert res["project_dir"] == str(workspace / "proj-abc")
    assert res["repo_url"] == "https://github.com/vercel/next.js.git"
    assert res["reported"] is True
    assert res["report_status"] == 200
    # Dev cmd routed through `npm run dev -- --port 3000` for Next.
    assert "npm" in res["dev_command"]
    assert "--port 3000" in res["dev_command"]

    # Subprocess sequence: git clone → git init → git add → git commit → npm install.
    cmds = [c["cmd"] for c in runner.calls]
    assert cmds[0][:2] == ["git", "clone"]
    assert ["git", "init", "-q"] in cmds
    assert ["git", "add", "."] in cmds
    assert any(c[:2] == ["git", "commit"] for c in cmds)
    assert cmds[-1][:2] == ["npm", "install"]

    # Spawn was called with the dev argv inside the project dir.
    assert spawner.calls and spawner.calls[0]["cmd"][:2] == ["npm", "run"]
    assert spawner.calls[0]["cwd"] == str(workspace / "proj-abc")

    # Report POST hit the expected URL with the expected secret.
    assert http.calls
    url, body, secret = http.calls[0]
    assert url == "https://api.example/_agents/internal/projects/42/clone-status"
    assert secret == "internal-shared-secret"
    assert body["port_reachable"] is True
    assert body["dev_pid"] == 9999
    assert body["repo_url"] == "https://github.com/vercel/next.js.git"


# --- failure paths --------------------------------------------------------


def test_git_clone_failure_short_circuits(workspace):
    runner = FakeRunner(
        scripted={"git": {"exit_code": 128, "stdout": "", "stderr": "Repository not found"}}
    )
    spawner = FakeSpawner()
    http = FakeHttpPost()

    res = ps.project_clone_and_setup(
        template_repo_full="ghost/missing-repo",
        slug="abc",
        project_id=42,
        api_base="https://api.example/_agents",
        internal_secret="s",
        workspace_root=str(workspace),
        dev_wait_timeout_s=5,
        run=runner,
        spawn_background=spawner,
        port_check=FakePortCheck(0),
        sleep=lambda _s: None,
        http_post=http,
    )
    assert res["ok"] is False
    assert res["error"] == "git_clone_failed"
    assert "Repository not found" in res["detail"]
    # No install, no spawn, no report.
    assert all(c["cmd"][0] != "npm" for c in runner.calls)
    assert spawner.calls == []
    assert http.calls == []


def test_install_failure_skips_spawn_and_report(workspace):
    runner = FakeRunner(
        scripted={"npm": {"exit_code": 1, "stdout": "", "stderr": "ENOENT"}}
    )
    real_default_run = runner.__call__

    def runner_with_seed(cmd, cwd=None, *, timeout=120, env=None):
        result = real_default_run(cmd, cwd=cwd, timeout=timeout, env=env)
        if cmd[0] == "git" and len(cmd) >= 2 and cmd[1] == "clone":
            _seed_project_dir(workspace, "abc")
        return result

    spawner = FakeSpawner()
    http = FakeHttpPost()

    res = ps.project_clone_and_setup(
        template_repo_full="vercel/next.js",
        slug="abc",
        project_id=42,
        api_base="https://api.example/_agents",
        internal_secret="s",
        workspace_root=str(workspace),
        dev_wait_timeout_s=5,
        run=runner_with_seed,
        spawn_background=spawner,
        port_check=FakePortCheck(0),
        sleep=lambda _s: None,
        http_post=http,
    )
    assert res["ok"] is False
    assert res["error"] == "install_failed"
    assert res["package_manager"] == "npm"
    assert spawner.calls == []
    assert http.calls == []


def test_port_never_opens_returns_ok_false_but_still_reports(workspace):
    runner = FakeRunner()
    real_default_run = runner.__call__

    def runner_with_seed(cmd, cwd=None, *, timeout=120, env=None):
        result = real_default_run(cmd, cwd=cwd, timeout=timeout, env=env)
        if cmd[0] == "git" and len(cmd) >= 2 and cmd[1] == "clone":
            _seed_project_dir(workspace, "abc")
        return result

    spawner = FakeSpawner(pid=1234)
    # Port never opens — port_check always False.
    port_check = FakePortCheck(ready_after=10**6)
    http = FakeHttpPost(status=200, body={"ok": True})

    res = ps.project_clone_and_setup(
        template_repo_full="vercel/next.js",
        slug="abc",
        project_id=42,
        api_base="https://api.example/_agents",
        internal_secret="s",
        workspace_root=str(workspace),
        dev_wait_timeout_s=5,
        run=runner_with_seed,
        spawn_background=spawner,
        port_check=port_check,
        sleep=lambda _s: None,
        http_post=http,
        now=_make_clock(step=2.5),  # tick 2.5s per call → exits loop in 3 attempts
    )
    assert res["ok"] is False
    assert res["port_reachable"] is False
    assert res["dev_pid"] == 1234
    assert res["reported"] is True
    assert http.calls and http.calls[0][1]["port_reachable"] is False


# --- detection helpers ----------------------------------------------------


def test_detect_package_manager_prefers_pnpm_over_yarn_over_npm(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "package-lock.json").write_text("{}")
    (proj / "yarn.lock").write_text("")
    (proj / "pnpm-lock.yaml").write_text("")
    assert ps.detect_package_manager(str(proj)) == "pnpm"
    (proj / "pnpm-lock.yaml").unlink()
    assert ps.detect_package_manager(str(proj)) == "yarn"
    (proj / "yarn.lock").unlink()
    assert ps.detect_package_manager(str(proj)) == "npm"
    (proj / "package-lock.json").unlink()
    assert ps.detect_package_manager(str(proj)) == "npm"  # default fallback


def test_detect_dev_command_appends_port_flag_only_for_nextjs(tmp_path):
    next_proj = tmp_path / "next"
    next_proj.mkdir()
    (next_proj / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"dev": "next dev"},
                "dependencies": {"next": "14"},
            }
        )
    )
    argv = ps.detect_dev_command(str(next_proj), "npm", 3000)
    assert argv == ["npm", "run", "dev", "--", "--port", "3000"]

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}})
    )
    plain_argv = ps.detect_dev_command(str(plain), "pnpm", 3000)
    assert plain_argv == ["pnpm", "run", "dev"]  # no `--port` push


def test_detect_dev_command_falls_back_to_start(tmp_path):
    proj = tmp_path / "only-start"
    proj.mkdir()
    (proj / "package.json").write_text(json.dumps({"scripts": {"start": "node ./server.js"}}))
    argv = ps.detect_dev_command(str(proj), "yarn", 3000)
    assert argv == ["yarn", "run", "start"]


# --- helpers --------------------------------------------------------------


def _make_clock(step: float):
    """A monotonic-ish clock that ticks ``step`` seconds per call."""
    state = {"t": 0.0}

    def now() -> float:
        state["t"] += step
        return state["t"]

    return now
