"""Sanity-check the menu side-effects."""
from __future__ import annotations

import subprocess

from agentflow_computer_mcp.winapp import actions


def test_open_cabinet_calls_opener_with_url() -> None:
    calls: list[str] = []

    def opener(url: str) -> bool:
        calls.append(url)
        return True

    assert actions.open_cabinet(opener) is True
    assert calls == [actions.CABINET_URL]


def test_open_cabinet_returns_false_on_exception() -> None:
    def opener(url: str) -> bool:
        raise RuntimeError("nope")

    assert actions.open_cabinet(opener) is False


def test_restart_daemon_runs_stop_then_start() -> None:
    log: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        log.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0)

    assert actions.restart_daemon(runner) is True
    assert log[0][-1] == "stop"
    assert log[1][-1] == "start"


def test_restart_daemon_returns_false_when_subprocess_fails() -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=1)

    assert actions.restart_daemon(runner) is False


def test_kill_agent_returns_true_on_success() -> None:
    log: list[dict] = []

    def caller(method: str, **kwargs):
        log.append({"method": method, **kwargs})
        return {"id": kwargs["id"], "status": "paused"}

    assert actions.kill_agent("a1", caller) is True
    assert log == [{"method": "pause", "id": "a1"}]


def test_kill_agent_returns_false_on_daemon_unavailable() -> None:
    from agentflow_computer_mcp.cli import socket_client

    def caller(method: str, **kwargs):
        raise socket_client.DaemonUnavailable("no socket")

    assert actions.kill_agent("a1", caller) is False
