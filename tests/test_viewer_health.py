"""GET /api/health on the local viewer returns the daemon health snapshot."""
from __future__ import annotations

import json
import socket
import urllib.request

from agentflow_computer_mcp.driver.state import DriverState
from agentflow_computer_mcp.driver.viewer import start_viewer
from agentflow_computer_mcp.health import get_health, reset_health_for_tests


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_api_health_route_returns_state() -> None:
    reset_health_for_tests()
    get_health().mark_reconnecting("boom")

    state = DriverState()
    port = _free_port()
    srv = start_viewer(state, presets=[], port=port, host="127.0.0.1")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
            body = json.loads(resp.read().decode())
    finally:
        srv.shutdown()

    assert body["ws_status"] == "reconnecting"
    assert body["consecutive_failures"] == 1
    assert body["last_failure"] == "boom"
