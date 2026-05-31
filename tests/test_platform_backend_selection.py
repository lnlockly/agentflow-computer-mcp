from __future__ import annotations

import sys

import pytest

from agentflow_computer_mcp.platform import PLATFORM, PlatformBackend, backend


def test_platform_detects_current_os() -> None:
    if sys.platform == "darwin":
        assert PLATFORM == "mac"
    elif sys.platform.startswith("linux"):
        assert PLATFORM == "linux"
    elif sys.platform == "win32":
        assert PLATFORM == "windows"
    else:
        pytest.skip(f"unexpected sys.platform {sys.platform}")


def test_backend_exists_and_matches_protocol() -> None:
    assert backend is not None, f"no backend selected for {PLATFORM}"
    assert isinstance(backend, PlatformBackend)


def test_backend_advertises_name() -> None:
    assert backend is not None
    assert backend.name in {"mac", "linux", "windows"}


@pytest.mark.parametrize(
    "method",
    [
        "capture_screen_fast",
        "capture_screen",
        "capture_region",
        "screen_size",
        "mouse_click",
        "mouse_move",
        "mouse_scroll",
        "keyboard_type",
        "keyboard_key",
        "keyboard_shortcut",
        "window_list",
        "window_focus",
        "app_activate",
        "clipboard_read",
        "clipboard_write",
        "read_terminal",
    ],
)
def test_backend_implements_method(method: str) -> None:
    assert backend is not None
    fn = getattr(backend, method, None)
    assert callable(fn), f"backend {backend.name!r} missing {method!r}"
