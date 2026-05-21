from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentflow_computer_mcp.driver.af_client import AFClient
from agentflow_computer_mcp.driver.desktop_tools import (
    DESKTOP_TOOLS,
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
    # 22 desktop + 11 af currently
    assert len(tools) == len(DESKTOP_TOOLS) + 11


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
