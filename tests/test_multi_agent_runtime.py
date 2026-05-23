"""_runtime.maybe_start_runtime gating."""
from __future__ import annotations

import pytest

from agentflow_computer_mcp.agents import _runtime


def test_runtime_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFLOW_MULTI_AGENT", raising=False)
    assert _runtime.maybe_start_runtime() is None


def test_runtime_starts_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import tempfile
    import uuid

    monkeypatch.setenv("AGENTFLOW_MULTI_AGENT", "1")
    sock_path = (
        f"{tempfile.gettempdir()}/af-rt-{uuid.uuid4().hex[:8]}.sock"
    )
    monkeypatch.setenv("AGENTFLOW_AGENT_SOCKET", sock_path)
    # Redirect AGENTFLOW_DIR so we don't touch the user's real ~/.agentflow.
    from agentflow_computer_mcp.agents import bootstrap

    monkeypatch.setattr(bootstrap, "AGENTFLOW_DIR", tmp_path)
    handle = _runtime.maybe_start_runtime()
    try:
        assert handle is not None
        assert "default" in handle.router.slots
    finally:
        if handle is not None:
            handle.stop()
