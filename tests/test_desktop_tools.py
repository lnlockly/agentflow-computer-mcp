from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentflow_computer_mcp.driver.af_client import AFClient
from agentflow_computer_mcp.driver.desktop_tools import (
    DESKTOP_TOOLS,
    MAC_ONLY_TOOLS,
    WINDOWS_ONLY_TOOLS,
    ToolExecutor,
    all_tool_descriptors,
    get_window_list,
)


def test_all_tool_descriptors_includes_desktop_and_af() -> None:
    tools = all_tool_descriptors()
    names = {t["name"] for t in tools}
    assert "screen_capture" in names
    assert "task_complete" in names
    assert "af_list_devices" in names
    assert "af_create_project" in names
    assert "firefox_open" in names
    assert "af_remember" in names
    from agentflow_computer_mcp.driver.af_client import AF_TOOL_DESCRIPTORS
    from agentflow_computer_mcp.driver.firefox import FIREFOX_TOOL_DESCRIPTORS

    # The OS filter strips mac-only tools on Windows/Linux and windows-only tools
    # on macOS, so the total catalog drops by that count vs the raw sum.
    raw_total = len(DESKTOP_TOOLS) + len(FIREFOX_TOOL_DESCRIPTORS) + len(AF_TOOL_DESCRIPTORS)
    import platform as _platform

    if _platform.system() == "Darwin":
        expected = raw_total - len(WINDOWS_ONLY_TOOLS)
    else:
        expected = raw_total - len(MAC_ONLY_TOOLS)
    assert len(tools) == expected


def test_all_tool_descriptors_filters_mac_only_on_windows() -> None:
    """On a Windows host, AppleScript-only tools must not appear in the LLM catalog."""
    with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value="Windows"):
        names = {t["name"] for t in all_tool_descriptors()}
    assert "chrome_eval" not in names
    assert "chrome_tabs" not in names
    assert "powershell_exec" in names
    assert "winget_search" in names
    # Cross-platform tools stay visible.
    assert "chrome_open_url" in names
    assert "start_app" in names


def test_all_tool_descriptors_filters_windows_only_on_mac() -> None:
    """On a macOS host, Windows-only tools must not appear in the LLM catalog."""
    with patch("agentflow_computer_mcp.driver.desktop_tools.platform.system", return_value="Darwin"):
        names = {t["name"] for t in all_tool_descriptors()}
    assert "powershell_exec" not in names
    assert "winget_search" not in names
    assert "winget_install" not in names
    assert "chrome_eval" in names
    assert "chrome_tabs" in names


def test_executor_routes_af_tool_when_client_present() -> None:
    client = MagicMock(spec=AFClient)
    fake = MagicMock(ok=True, status=200, body={"items": []}, error=None)
    client.list_devices.return_value = fake

    cursor = [0, 0]
    ex = ToolExecutor(cursor, af_client=client, pw=MagicMock())
    out, image = ex.execute("af_list_devices", {})
    assert image is None
    assert "ok" in out
    client.list_devices.assert_called_once()


def test_executor_returns_error_when_no_af_client() -> None:
    ex = ToolExecutor([0, 0], af_client=None, pw=MagicMock())
    out, _ = ex.execute("af_list_projects", {})
    assert "no AF api key" in out


def test_executor_task_complete_sentinel() -> None:
    ex = ToolExecutor([0, 0], af_client=None, pw=MagicMock())
    out, _ = ex.execute("task_complete", {"answer": "done"})
    assert out == "__DONE__"


def test_executor_mouse_click_updates_cursor() -> None:
    cursor = [0, 0]
    ex = ToolExecutor(cursor, af_client=None, pw=MagicMock())
    with patch("agentflow_computer_mcp.driver.desktop_tools.mouse.click") as m:
        out, _ = ex.execute("mouse_click", {"x": 100, "y": 200})
    assert cursor == [100, 200]
    assert "100" in out and "200" in out
    m.assert_called_once()


def test_executor_unknown_tool_message() -> None:
    ex = ToolExecutor([0, 0], af_client=None, pw=MagicMock())
    out, _ = ex.execute("totally_bogus", {})
    assert "unknown tool" in out


def test_get_window_list_filters_noisy_owners() -> None:
    fake_windows = [
        {"owner": "Window Server", "title": "", "window_id": 1, "bounds": {"width": 200, "height": 200}},
        {"owner": "Real App", "title": "doc", "window_id": 2, "bounds": {"width": 800, "height": 600}},
        {"owner": "Tiny", "title": "x", "window_id": 3, "bounds": {"width": 10, "height": 10}},
    ]
    with patch("agentflow_computer_mcp.driver.desktop_tools.window.list_windows", return_value=fake_windows):
        result = get_window_list()
    assert len(result) == 1
    assert result[0]["owner"] == "Real App"
