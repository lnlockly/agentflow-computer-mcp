"""Pytest fixtures for the multi-agent self-test harness.

`daemon` spawns the runtime harness subprocess with an isolated HOME +
socket path, waits for the socket to bind, and tears the child down on
teardown. Each test gets a clean daemon.
"""
from __future__ import annotations

import contextlib
import os
import signal
import socket as _socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentflow_computer_mcp.cli import socket_client

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="multi-agent socket harness is POSIX-only"
)


@dataclass
class DaemonHandle:
    """Thin wrapper around a running runtime-harness subprocess."""

    process: subprocess.Popen
    socket_path: Path
    agentflow_home: Path

    def _call(self, method: str, **kwargs: Any) -> Any:
        return socket_client.call(method, path=str(self.socket_path), **kwargs)

    def list_agents(self) -> list[dict]:
        return self._call("list") or []

    def spawn_agent(
        self, name: str, *, persona: str = "", scope_path: str | None = None
    ) -> dict:
        return self._call(
            "create", name=name, persona=persona, scope_path=scope_path
        )

    def pause_agent(self, agent_id: str) -> dict:
        return self._call("pause", id=agent_id)

    def resume_agent(self, agent_id: str) -> dict:
        return self._call("resume", id=agent_id)

    def kill(self, sig: int = signal.SIGTERM) -> None:
        if self.process.poll() is None:
            self.process.send_signal(sig)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)

    def is_alive(self) -> bool:
        return self.process.poll() is None


def _wait_for_socket(path: Path, timeout: float = 10.0) -> bool:
    """Poll for the socket file with exponential backoff."""
    deadline = time.monotonic() + timeout
    delay = 0.02
    while time.monotonic() < deadline:
        if path.exists():
            # Confirm the socket accepts a connection (file may exist mid-bind).
            try:
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(str(path))
                s.close()
                return True
            except OSError:
                pass
        time.sleep(delay)
        delay = min(delay * 1.5, 0.5)
    return False


@pytest.fixture
def daemon(tmp_path: Path) -> Any:
    """Spawn the runtime harness in a child process; tear it down on exit.

    Each test gets:
      - its own HOME ⇒ its own ~/.agentflow/
      - its own socket file under /tmp/af-<uuid>.sock (AF_UNIX 104-byte limit)
      - clean process, clean teardown
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    socket_path = Path(f"/tmp/af-{uuid.uuid4().hex[:10]}.sock")
    ready_file = tmp_path / "ready"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["AGENTFLOW_MULTI_AGENT"] = "1"
    env["AGENTFLOW_AGENT_SOCKET"] = str(socket_path)
    env["AGENTFLOW_HARNESS_READY"] = str(ready_file)
    # Make sure the child does not try to talk to the user's real network.
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.integration.runtime_harness"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=_repo_root(),
    )

    handle = DaemonHandle(
        process=proc, socket_path=socket_path, agentflow_home=home / ".agentflow"
    )

    if not _wait_for_socket(socket_path, timeout=10.0):
        stderr = ""
        with contextlib.suppress(Exception):
            proc.kill()
            _, err = proc.communicate(timeout=2)
            stderr = err.decode("utf-8", "replace")
        pytest.fail(f"daemon socket did not bind at {socket_path}; stderr:\n{stderr}")

    try:
        yield handle
    finally:
        handle.kill()
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()


def _repo_root() -> str:
    """Return the absolute path to the repo root so `python -m` resolves."""
    return str(Path(__file__).resolve().parents[2])
