"""HealthRegistry transitions used by ws_client + viewer."""
from __future__ import annotations

from agentflow_computer_mcp.health import HealthRegistry, reset_health_for_tests


def test_initial_state_is_connecting() -> None:
    reg = HealthRegistry()
    snap = reg.snapshot()
    assert snap.ws_status == "connecting"
    assert snap.last_hello_at is None
    assert snap.last_failure is None
    assert snap.consecutive_failures == 0


def test_mark_connected_after_hello() -> None:
    reg = HealthRegistry()
    reg.mark_connected()
    snap = reg.snapshot()
    assert snap.ws_status == "connected"
    assert snap.last_hello_at is not None
    assert snap.consecutive_failures == 0


def test_failure_transitions_to_reconnecting_and_increments_counter() -> None:
    reg = HealthRegistry()
    reg.mark_reconnecting("timed out during opening handshake")
    snap1 = reg.snapshot()
    assert snap1.ws_status == "reconnecting"
    assert snap1.consecutive_failures == 1
    assert snap1.last_failure == "timed out during opening handshake"

    reg.mark_reconnecting("connection reset")
    snap2 = reg.snapshot()
    assert snap2.consecutive_failures == 2
    assert snap2.last_failure == "connection reset"


def test_success_after_failures_resets_counter() -> None:
    reg = HealthRegistry()
    reg.mark_reconnecting("boom")
    reg.mark_reconnecting("boom2")
    assert reg.snapshot().consecutive_failures == 2

    reg.mark_connected()
    snap = reg.snapshot()
    assert snap.ws_status == "connected"
    assert snap.consecutive_failures == 0
    assert snap.last_failure is None


def test_to_dict_serializes_state() -> None:
    reg = HealthRegistry()
    reg.mark_reconnecting("eof")
    d = reg.to_dict()
    assert d["ws_status"] == "reconnecting"
    assert d["consecutive_failures"] == 1
    assert d["last_failure"] == "eof"
    assert "last_hello_at" in d


def test_singleton_reset_helper() -> None:
    reset_health_for_tests()
    from agentflow_computer_mcp.health import get_health

    assert get_health().snapshot().ws_status == "connecting"
    get_health().mark_connected()
    assert get_health().snapshot().ws_status == "connected"

    reset_health_for_tests()
    assert get_health().snapshot().ws_status == "connecting"
