"""Cross-platform unit coverage for the OS-aware prompt path.

We can't easily run the real macOS / Windows backends inside the Linux
self-hosted CI, but we *can* validate that the platform-detection +
prompt-selection logic picks the right block for each `sys.platform`
value. That covers the failure mode the user actually hit (Mac
instructions sent to a non-Mac box) without needing a real Windows VM.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agentflow_computer_mcp.driver.loop import (
    _OS_INTENT_BLOCK,
    _current_os,
    build_system_prompt,
)


@pytest.mark.parametrize(
    ("platform_value", "expected"),
    [
        ("darwin", "macos"),
        ("darwin19", "macos"),
        ("linux", "linux"),
        ("linux2", "linux"),
        ("win32", "windows"),
        ("cygwin", "linux"),
        ("freebsd11", "linux"),
        ("unknown-platform", "linux"),
    ],
)
def test_current_os_maps_sys_platform(platform_value: str, expected: str) -> None:
    with patch("agentflow_computer_mcp.driver.loop.sys.platform", platform_value):
        assert _current_os() == expected


def test_os_intent_block_has_all_three_keys() -> None:
    assert set(_OS_INTENT_BLOCK.keys()) == {"macos", "linux", "windows"}


def test_macos_block_mentions_mac_specifics() -> None:
    block = _OS_INTENT_BLOCK["macos"]
    assert "Mail" in block
    assert "iTerm2" in block or "Terminal" in block
    assert "Cmd+" in block


def test_linux_block_avoids_mac_only_apps() -> None:
    block = _OS_INTENT_BLOCK["linux"]
    assert "Mail.app" not in block
    assert "iTerm2" not in block
    assert "Cmd+" not in block
    assert "gnome-terminal" in block or "konsole" in block or "xterm" in block
    # Either Thunderbird native or Gmail browser fallback.
    assert "Thunderbird" in block or "mail.google.com" in block


def test_windows_block_avoids_mac_only_apps() -> None:
    block = _OS_INTENT_BLOCK["windows"]
    assert "Mail.app" not in block
    assert "iTerm2" not in block
    assert "Cmd+" not in block
    # WindowsTerminal / powershell / cmd are all acceptable.
    assert (
        "WindowsTerminal" in block or "powershell" in block or "cmd" in block
    )


def test_system_prompt_contains_macos_block_when_on_darwin() -> None:
    with patch("agentflow_computer_mcp.driver.loop.sys.platform", "darwin"):
        prompt = build_system_prompt(window_summary="(none)", af_tools_present=True)
    assert "Mac пользователя" in prompt
    assert _OS_INTENT_BLOCK["macos"] in prompt
    assert _OS_INTENT_BLOCK["linux"] not in prompt
    assert _OS_INTENT_BLOCK["windows"] not in prompt


def test_system_prompt_contains_linux_block_when_on_linux() -> None:
    with patch("agentflow_computer_mcp.driver.loop.sys.platform", "linux"):
        prompt = build_system_prompt(window_summary="(none)", af_tools_present=True)
    assert "Linux пользователя" in prompt
    assert _OS_INTENT_BLOCK["linux"] in prompt
    assert _OS_INTENT_BLOCK["macos"] not in prompt
    assert _OS_INTENT_BLOCK["windows"] not in prompt


def test_system_prompt_contains_windows_block_when_on_win32() -> None:
    with patch("agentflow_computer_mcp.driver.loop.sys.platform", "win32"):
        prompt = build_system_prompt(window_summary="(none)", af_tools_present=True)
    assert "Windows пользователя" in prompt
    assert _OS_INTENT_BLOCK["windows"] in prompt
    assert _OS_INTENT_BLOCK["macos"] not in prompt
    assert _OS_INTENT_BLOCK["linux"] not in prompt


def test_system_prompt_swaps_window_listing_header() -> None:
    with patch("agentflow_computer_mcp.driver.loop.sys.platform", "linux"):
        prompt = build_system_prompt(window_summary="(none)", af_tools_present=True)
    # No leftover "Окна Mac" header on a non-Mac box.
    assert "Окна Mac" not in prompt
    assert "Окна сейчас" in prompt
