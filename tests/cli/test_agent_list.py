"""`agentflow agent list` renders a table of slots from the daemon socket."""
from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from agentflow_computer_mcp.cli.main import app


@pytest.mark.skipif(sys.platform == "win32", reason="windows agent subcommands gated")
def test_agent_list_renders(monkeypatch) -> None:
    fake_slots = [
        {"id": "default", "name": "default", "persona": "research", "status": "idle"},
        {"id": "a1", "name": "writer", "persona": "long-form copy", "status": "running"},
        {"id": "a2", "name": "scout", "persona": "lead gen", "status": "paused"},
    ]

    from agentflow_computer_mcp.cli import agent as agent_mod

    def fake_call(method, **kwargs):
        assert method == "list"
        return fake_slots

    monkeypatch.setattr(agent_mod.socket_client, "call", fake_call)

    runner = CliRunner()
    res = runner.invoke(app, ["agent", "list"])
    assert res.exit_code == 0, res.output
    assert "default" in res.output
    assert "writer" in res.output
    assert "scout" in res.output
    assert "paused" in res.output


@pytest.mark.skipif(sys.platform == "win32", reason="windows agent subcommands gated")
def test_agent_list_daemon_down(monkeypatch) -> None:
    from agentflow_computer_mcp.cli import agent as agent_mod
    from agentflow_computer_mcp.cli.socket_client import DaemonUnavailable

    def fake_call(method, **kwargs):
        raise DaemonUnavailable("socket missing")

    monkeypatch.setattr(agent_mod.socket_client, "call", fake_call)

    runner = CliRunner()
    res = runner.invoke(app, ["agent", "list"])
    assert res.exit_code == 3
    assert "демон не запущен" in res.output
