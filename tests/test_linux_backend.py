from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("linux-only", allow_module_level=True)

from agentflow_computer_mcp.platform import linux as linux_mod  # noqa: E402


def test_backend_loads() -> None:
    assert linux_mod.backend.name == "linux"


def test_wayland_detection_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert linux_mod._is_wayland() is True

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert linux_mod._is_wayland() is False


def test_window_list_returns_list() -> None:
    # Even with no wmctrl installed this returns an empty list, not a crash.
    out = linux_mod.backend.window_list()
    assert isinstance(out, list)


def test_read_terminal_returns_str() -> None:
    out = linux_mod.backend.read_terminal()
    assert isinstance(out, str)
