from __future__ import annotations

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("windows-only", allow_module_level=True)

from agentflow_computer_mcp.platform import windows as win_mod  # noqa: E402


def test_backend_loads() -> None:
    assert win_mod.backend.name == "windows"


def test_window_list_returns_list() -> None:
    out = win_mod.backend.window_list()
    assert isinstance(out, list)


def test_clipboard_read_returns_str() -> None:
    out = win_mod.backend.clipboard_read()
    assert isinstance(out, str)


def test_read_terminal_returns_str() -> None:
    out = win_mod.backend.read_terminal()
    assert isinstance(out, str)
