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

import json
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


# ---------------------------------------------------------------------------
# tg_bot branch — kind='tg_bot' takes a Python-shaped prompt + writes BOT_TOKEN
# to .env before opencode runs. The HTTP port watcher is not started; a
# separate launcher thread handles `python bot.py` + getMe verification (those
# threads are not invoked in unit tests — they reach the network).
# ---------------------------------------------------------------------------


def test_tg_bot_brief_targets_python_stack(workspace):
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "wakaree/aiogram_bot_template",
        "demo",
        99,
        "simple echo bot",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        kind="tg_bot",
        bot_token="123456:fake-token",
        bot_username="demo_bot",
    )
    composed = spawner.calls[0]["brief"]
    lc = composed.lower()
    # The tg_bot prompt must talk about Python + aiogram, not npm/Next.
    assert "python" in lc
    assert "aiogram" in lc or "requirements.txt" in lc or "pyproject.toml" in lc
    assert "npm run dev" not in lc
    # Daemon owns the launch — opencode must NOT spawn the bot itself.
    assert "do not run the bot" in lc or "do not spawn" in lc


def test_tg_bot_writes_bot_token_to_env(workspace):
    # Fake `git clone` by materialising the target directory. The real
    # subprocess `git clone` would do this; the FakeRunner here only
    # records the call.
    target = workspace / "proj-demo"

    def _fake_clone(cmd, cwd=None, *, timeout=120, env=None):
        if cmd[0] == "git" and cmd[1] == "clone":
            target.mkdir(exist_ok=True)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    runner = _fake_clone
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "wakaree/aiogram_bot_template",
        "demo",
        77,
        "simple echo bot",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        kind="tg_bot",
        bot_token="123456:fake-token",
        bot_username="demo_bot",
    )
    env_path = workspace / "proj-demo" / ".env"
    assert env_path.exists()
    content = env_path.read_text(encoding="utf-8")
    assert "BOT_TOKEN=123456:fake-token" in content


def test_tg_bot_does_not_patch_package_json(workspace, monkeypatch):
    # _patch_package_json_for_port is for landing/spa templates only.
    # Calling it on a Python project's stray package.json (eg dev tooling)
    # would silently rewrite scripts. The tg_bot branch must skip it.
    calls: list[Any] = []
    monkeypatch.setattr(ab, "_patch_package_json_for_port", lambda *a, **kw: calls.append(a))
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "wakaree/aiogram_bot_template",
        "demo",
        88,
        "echo bot",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        kind="tg_bot",
        bot_token="t",
        bot_username="b",
    )
    assert calls == []


def test_landing_path_still_patches_package_json(workspace, monkeypatch):
    # Defence-in-depth: the kind=landing path must keep calling the
    # package.json patcher — no accidental regression from the tg_bot branch.
    seen: list[Any] = []
    real = ab._patch_package_json_for_port
    monkeypatch.setattr(
        ab, "_patch_package_json_for_port", lambda *a, **kw: seen.append(a) or real(*a, **kw)
    )
    runner = FakeRunner()
    spawner = FakeOpencodeSpawner(pid=1)
    ab.agent_dev_brief(
        "owner/repo",
        "demo",
        88,
        "static landing",
        workspace_root=str(workspace),
        run=runner,
        spawn_opencode=spawner,
        kind="landing",
    )
    assert len(seen) == 1


def test_tg_get_me_rejects_empty_token():
    assert ab._tg_get_me("") == {"ok": False, "error": "invalid_token"}
    assert ab._tg_get_me("no-colon") == {"ok": False, "error": "invalid_token"}


def test_spawn_python_bot_rejects_when_no_entrypoint(tmp_path):
    proj = tmp_path / "proj-x"
    proj.mkdir()
    res = ab._spawn_python_bot(str(proj), bot_token="t:t")
    assert res == {"ok": False, "error": "no_python_entrypoint"}


def test_watch_and_launch_tg_bot_reports_bot_username_on_getme_ok(
    tmp_path, monkeypatch
):
    # End-to-end of the launcher thread with all I/O stubbed. The contract
    # we lock in: when getMe returns ok, the clone-status POST carries
    # port=0, ok=true, bot_username derived from Telegram's reply.
    monkeypatch.setenv("AF_INTERNAL_API_SECRET", "shh")
    monkeypatch.setenv("AF_API_URL", "https://example.test")
    posts: list[dict[str, Any]] = []

    class FakeReq:
        def __init__(self, url, data, method, headers):
            posts.append({"url": url, "body": json.loads(data.decode()), "headers": headers})

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b""

    monkeypatch.setattr(ab.urllib.request, "Request", FakeReq)
    monkeypatch.setattr(ab.urllib.request, "urlopen", lambda *a, **kw: FakeResp())
    monkeypatch.setattr(ab, "_resolve_pod_ip", lambda: "10.0.0.5")

    ab._watch_and_launch_tg_bot(
        project_id=42,
        slug="demo",
        project_dir=str(tmp_path),
        repo_url="https://github.com/x/y.git",
        bot_token="t:t",
        bot_username="orig_bot",
        opencode_pid=None,
        timeout_sec=1.0,
        poll_interval_sec=0.01,
        spawn_bot=lambda *a, **kw: {"ok": True, "pid": 555, "entrypoint": "/x/bot.py"},
        tg_get_me=lambda token: {"ok": True, "result": {"username": "verified_bot"}},
        pid_alive=lambda pid: False,
    )

    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["ok"] is True
    assert body["port"] == 0
    assert body["port_reachable"] is False
    assert body["bot_username"] == "verified_bot"
    assert body["kind"] == "tg_bot"
    assert body["dev_pid"] == 555


def test_watch_and_launch_tg_bot_reports_failure_when_spawn_dies(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AF_INTERNAL_API_SECRET", "shh")
    monkeypatch.setenv("AF_API_URL", "https://example.test")
    posts: list[dict[str, Any]] = []

    class FakeReq:
        def __init__(self, url, data, method, headers):
            posts.append({"url": url, "body": json.loads(data.decode())})
    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b""
    monkeypatch.setattr(ab.urllib.request, "Request", FakeReq)
    monkeypatch.setattr(ab.urllib.request, "urlopen", lambda *a, **kw: FakeResp())
    monkeypatch.setattr(ab, "_resolve_pod_ip", lambda: None)

    ab._watch_and_launch_tg_bot(
        project_id=99,
        slug="demo",
        project_dir=str(tmp_path),
        repo_url="https://github.com/x/y.git",
        bot_token="t:t",
        bot_username="b",
        opencode_pid=None,
        timeout_sec=1.0,
        poll_interval_sec=0.01,
        spawn_bot=lambda *a, **kw: {"ok": False, "error": "no_python_entrypoint"},
        tg_get_me=lambda token: {"ok": True},  # never reached
        pid_alive=lambda pid: False,
    )

    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["ok"] is False
    assert body["error"] == "no_python_entrypoint"
    assert body["port"] == 0


def test_watch_and_launch_tg_bot_skips_when_secret_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("AF_INTERNAL_API_SECRET", raising=False)
    posts: list[Any] = []
    monkeypatch.setattr(
        ab.urllib.request, "Request", lambda *a, **kw: posts.append(1)
    )
    ab._watch_and_launch_tg_bot(
        project_id=1,
        slug="s",
        project_dir=str(tmp_path),
        repo_url="r",
        bot_token="t:t",
        bot_username="b",
        opencode_pid=None,
        timeout_sec=0.1,
        spawn_bot=lambda *a, **kw: {"ok": True, "pid": 1},
        tg_get_me=lambda t: {"ok": True, "result": {"username": "b"}},
        pid_alive=lambda pid: False,
    )
    assert posts == []  # never POSTed
