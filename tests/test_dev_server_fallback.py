"""Unit tests for the daemon-side dev-server fallback (2026-05-28).

When opencode exits without binding the dev port (observed in projects
1547/1552/1553/1557 on 2026-05-27), ``_watch_and_report_clone_status``
detects the dead pid + cold port and spawns ``<pm> run dev`` itself.
The behaviour is gated on a grace window so a slow opencode that just
hadn't reached step 4 yet is not preempted.

Tests fake every side effect — no real HTTP, no real subprocess — so
they pass without a network or node binary.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from agentflow_computer_mcp.driver.tools import agent_brief as ab

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeProbe:
    """Scripted ``_http_probe`` returning booleans in order."""

    def __init__(self, responses: list[bool]):
        self.responses = list(responses)
        self.calls: list[int] = []

    def __call__(self, port: int) -> bool:
        self.calls.append(port)
        if not self.responses:
            return False
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class FakePidAlive:
    """Scripted ``_is_pid_alive`` returning booleans in order."""

    def __init__(self, responses: list[bool]):
        self.responses = list(responses)
        self.calls: list[int | None] = []

    def __call__(self, pid: int | None) -> bool:
        self.calls.append(pid)
        if not self.responses:
            return False
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class FakeDevSpawn:
    """Records every fallback spawn + returns a scripted result."""

    def __init__(self, result: dict[str, Any] | None = None):
        self.result = result or {"ok": True, "pid": 4242, "package_manager": "npm"}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, project_dir: str, port: int) -> dict[str, Any]:
        self.calls.append({"project_dir": project_dir, "port": port})
        return self.result


class FakeClock:
    """Deterministic ``time.monotonic`` substitute.

    ``advance`` lets a test step the clock forward without sleeping.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AF_INTERNAL_API_SECRET", "test-secret")
    monkeypatch.setenv("AF_API_URL", "https://agentflow.website")


def _run_watcher(
    *,
    probe: FakeProbe,
    pid_alive: FakePidAlive,
    spawn_dev: FakeDevSpawn,
    project_dir: str = "/workspace/proj-test",
    opencode_pid: int | None = 1111,
    timeout_sec: float = 600.0,
    captured_posts: list[dict[str, Any]] | None = None,
) -> None:
    """Drive ``_watch_and_report_clone_status`` with all I/O stubbed.

    ``time.sleep`` is patched to advance a fake clock by the requested
    interval so the polling loop runs in real time but the wall-clock
    inside the function jumps deterministically.
    """
    clock = FakeClock()
    posts = captured_posts if captured_posts is not None else []

    def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    class FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15):  # noqa: ARG001 — signature compat
        body = json.loads(req.data.decode("utf-8"))
        posts.append({"url": req.full_url, "body": body, "headers": dict(req.headers)})
        return FakeResponse()

    with (
        patch.object(ab.time, "monotonic", clock),
        patch.object(ab.time, "sleep", fake_sleep),
        patch.object(ab, "_http_probe", probe),
        patch.object(ab.urllib.request, "urlopen", fake_urlopen),
        patch.object(ab, "_resolve_pod_ip", lambda: "10.0.0.42"),
    ):
        ab._watch_and_report_clone_status(
            project_id=1557,
            slug="testslug",
            port=3557,
            project_dir=project_dir,
            repo_url="https://github.com/owner/repo.git",
            timeout_sec=timeout_sec,
            poll_interval_sec=5.0,
            opencode_pid=opencode_pid,
            spawn_dev_server=spawn_dev,
            pid_alive=pid_alive,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fallback_fires_after_opencode_dies_and_grace_elapses(env):
    """Port stays cold and opencode died early → fallback runs once.

    Sequence the fake clock walks:
      * t=0  start
      * t=5  probe #1 → False; opencode dead; first observation, grace starts
      * t=10..40 probe → False; opencode dead; once grace+wall pass, fallback fires
      * t=45+ probe → True → break + POST ok=true (fallback "worked")
    """
    probe = FakeProbe([False] * 12 + [True])
    pid_alive = FakePidAlive([False])  # dead from the start
    spawn_dev = FakeDevSpawn({"ok": True, "pid": 7777, "package_manager": "npm"})
    posts: list[dict[str, Any]] = []
    _run_watcher(
        probe=probe,
        pid_alive=pid_alive,
        spawn_dev=spawn_dev,
        captured_posts=posts,
    )
    assert len(spawn_dev.calls) == 1, "fallback must fire exactly once"
    assert spawn_dev.calls[0]["port"] == 3557
    assert spawn_dev.calls[0]["project_dir"] == "/workspace/proj-test"
    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["ok"] is True
    assert body["dev_server_fallback"]["attempted"] is True
    assert body["dev_server_fallback"]["ok"] is True
    assert body["dev_server_fallback"]["package_manager"] == "npm"


def test_fallback_does_not_fire_when_opencode_still_alive(env):
    """opencode running → never fall back, even if port is cold a while.

    Drains the timeout so the watcher reports ``port_unreachable`` —
    the legacy pre-2026-05-28 behaviour for this case.
    """
    probe = FakeProbe([False])
    pid_alive = FakePidAlive([True])  # alive forever
    spawn_dev = FakeDevSpawn()
    posts: list[dict[str, Any]] = []
    _run_watcher(
        probe=probe,
        pid_alive=pid_alive,
        spawn_dev=spawn_dev,
        timeout_sec=120.0,
        captured_posts=posts,
    )
    assert spawn_dev.calls == [], "fallback must not fire while opencode is alive"
    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["ok"] is False
    assert body["error"] == "port_unreachable"
    assert "dev_server_fallback" not in body


def test_fallback_fires_at_most_once_even_when_port_stays_cold(env):
    """Fallback spawn fails → still POSTed exactly once + watcher gives up.

    Mirrors the failure mode where `pnpm install` mid-fallback errors out.
    Port never reaches a 2xx so the watcher reaches timeout, and the
    final POST carries the failed fallback result for ops triage.
    """
    probe = FakeProbe([False])
    pid_alive = FakePidAlive([False])
    spawn_dev = FakeDevSpawn(
        {"ok": False, "error": "fallback_install_failed", "detail": "ENOSPC"}
    )
    posts: list[dict[str, Any]] = []
    _run_watcher(
        probe=probe,
        pid_alive=pid_alive,
        spawn_dev=spawn_dev,
        timeout_sec=240.0,
        captured_posts=posts,
    )
    assert len(spawn_dev.calls) == 1
    assert len(posts) == 1
    body = posts[0]["body"]
    assert body["ok"] is False
    assert body["error"] == "port_unreachable"
    assert body["dev_server_fallback"]["attempted"] is True
    assert body["dev_server_fallback"]["ok"] is False
    assert body["dev_server_fallback"]["error"] == "fallback_install_failed"
    assert body["dev_server_fallback"]["detail"] == "ENOSPC"


def test_fallback_skipped_when_opencode_pid_is_none(env):
    """No opencode pid handed in → cannot evaluate dead-ness → skip fallback.

    Maintains backward compatibility with any caller that hasn't been
    rewired yet (e.g. an older smoke test).
    """
    probe = FakeProbe([False])
    pid_alive = FakePidAlive([False])
    spawn_dev = FakeDevSpawn()
    posts: list[dict[str, Any]] = []
    _run_watcher(
        probe=probe,
        pid_alive=pid_alive,
        spawn_dev=spawn_dev,
        opencode_pid=None,
        timeout_sec=120.0,
        captured_posts=posts,
    )
    assert spawn_dev.calls == []
    assert len(posts) == 1
    assert "dev_server_fallback" not in posts[0]["body"]


def test_spawn_dev_server_skips_when_no_package_json(tmp_path):
    """Defence in depth — ``_spawn_dev_server`` returns a clean error code
    when the project dir has no ``package.json`` (e.g. a Python repo that
    leaked into the web pipeline by mistake)."""
    project_dir = tmp_path / "proj-x"
    project_dir.mkdir()
    res = ab._spawn_dev_server(str(project_dir), 3500)
    assert res == {"ok": False, "error": "no_package_json"}


def test_spawn_dev_server_skips_when_project_dir_missing(tmp_path):
    res = ab._spawn_dev_server(str(tmp_path / "does-not-exist"), 3500)
    assert res == {"ok": False, "error": "project_dir_missing"}


def test_spawn_dev_server_picks_pnpm_when_lockfile_exists(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj-y"
    project_dir.mkdir()
    (project_dir / "package.json").write_text('{"scripts":{"dev":"next dev"}}')
    (project_dir / "pnpm-lock.yaml").write_text("")
    (project_dir / "node_modules").mkdir()

    captured: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs.get("env", {})
            self.pid = 5555

    monkeypatch.setattr(ab.subprocess, "Popen", FakePopen)
    res = ab._spawn_dev_server(str(project_dir), 3600)
    assert res["ok"] is True
    assert res["package_manager"] == "pnpm"
    assert captured["cmd"][:3] == ["pnpm", "run", "dev"]
    assert captured["env"]["PORT"] == "3600"


def test_spawn_dev_server_runs_install_when_node_modules_missing(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj-z"
    project_dir.mkdir()
    (project_dir / "package.json").write_text('{"scripts":{"dev":"vite"}}')

    install_calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, *, timeout=120, env=None):  # noqa: ARG001
        install_calls.append(list(cmd))
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    class FakePopen:
        def __init__(self, cmd, **kwargs):  # noqa: ARG002
            self.pid = 9000

    monkeypatch.setattr(ab.subprocess, "Popen", FakePopen)
    res = ab._spawn_dev_server(str(project_dir), 3700, run=fake_run)
    assert res["ok"] is True
    assert res["package_manager"] == "npm"
    assert install_calls == [["npm", "install"]]
