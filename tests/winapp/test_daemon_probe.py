"""Verify the daemon probe collapses every failure mode to a clean tuple."""
from __future__ import annotations

import pytest

from agentflow_computer_mcp.cli import socket_client
from agentflow_computer_mcp.winapp import daemon_probe


def test_probe_returns_up_with_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daemon_probe.socket_client,
        "call",
        lambda *args, **kwargs: [
            {"id": "a1", "name": "Pikku", "status": "running"},
            {"id": "a2", "name": "Mika", "status": "paused"},
        ],
    )
    status, agents = daemon_probe.probe()
    assert status == "up"
    assert [a.id for a in agents] == ["a1", "a2"]


def test_probe_returns_down_when_socket_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise socket_client.DaemonUnavailable("socket not found: /tmp/agentflow.sock")

    monkeypatch.setattr(daemon_probe.socket_client, "call", boom)
    monkeypatch.setattr(daemon_probe.sys, "platform", "linux")
    status, agents = daemon_probe.probe()
    assert status == "down"
    assert agents == ()


def test_probe_returns_unsupported_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise socket_client.DaemonUnavailable("Windows: локальный socket пока не поддерживается")

    monkeypatch.setattr(daemon_probe.socket_client, "call", boom)
    status, agents = daemon_probe.probe()
    assert status == "unsupported"
    assert agents == ()


def test_probe_swallows_daemon_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise socket_client.DaemonError("malformed")

    monkeypatch.setattr(daemon_probe.socket_client, "call", boom)
    status, agents = daemon_probe.probe()
    assert status == "down"
    assert agents == ()
