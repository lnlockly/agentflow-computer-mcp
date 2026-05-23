"""`agentflow daemon status` checks pid + socket."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentflow_computer_mcp.cli.main import app


@pytest.mark.skipif(sys.platform == "win32", reason="windows daemon gated")
def test_daemon_status_not_running(monkeypatch, tmp_path: Path) -> None:
    from agentflow_computer_mcp.cli import daemon as daemon_mod

    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(daemon_mod, "PID_FILE", pid_file)
    # ensure socket path doesn't exist either
    monkeypatch.setattr(
        daemon_mod.socket_client, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock")
    )

    runner = CliRunner()
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 1
    assert "not running" in res.output


@pytest.mark.skipif(sys.platform == "win32", reason="windows daemon gated")
def test_daemon_status_running(monkeypatch, tmp_path: Path) -> None:
    import os

    from agentflow_computer_mcp.cli import daemon as daemon_mod

    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(daemon_mod, "PID_FILE", pid_file)
    sock = tmp_path / "agentflow.sock"
    sock.touch()
    monkeypatch.setattr(daemon_mod.socket_client, "DEFAULT_SOCKET_PATH", str(sock))

    runner = CliRunner()
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 0
    assert "running" in res.output
