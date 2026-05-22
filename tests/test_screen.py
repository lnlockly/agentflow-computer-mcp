from __future__ import annotations

import base64
import io
from unittest.mock import patch

import pytest
from PIL import Image

from agentflow_computer_mcp.tools import screen


@pytest.fixture
def fake_screenshot() -> Image.Image:
    img = Image.new("RGB", (1920, 1080), color=(80, 120, 200))
    return img


def test_encode_png_resizes_above_1280(fake_screenshot: Image.Image) -> None:
    png = screen._encode_png(fake_screenshot, max_width=1280)
    decoded = Image.open(io.BytesIO(png))
    assert decoded.width == 1280
    assert decoded.height == 720


def test_capture_base64_via_pyautogui_path(fake_screenshot: Image.Image) -> None:
    # `backend` is non-None on Linux/Windows (platform-specific module) and
    # owns its own capture path, so the dispatcher in `capture()` short-
    # circuits before reaching pyautogui. Force the legacy pyautogui path
    # explicitly so this test stays meaningful across all OSes.
    with patch.object(screen, "_HAS_QUARTZ", False), \
         patch.object(screen, "PLATFORM", "mac"), \
         patch.object(screen, "backend", None), \
         patch("pyautogui.screenshot", return_value=fake_screenshot):
        result = screen.capture_base64()

    assert result["mime"] == "image/png"
    assert result["size_bytes"] > 100
    decoded = base64.b64decode(result["base64"])
    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")


def test_capture_region_passes_through(fake_screenshot: Image.Image) -> None:
    with patch.object(screen, "_HAS_QUARTZ", False), \
         patch.object(screen, "PLATFORM", "mac"), \
         patch.object(screen, "backend", None), \
         patch("pyautogui.screenshot", return_value=fake_screenshot) as mock_shot:
        screen.capture(region={"x": 10, "y": 20, "width": 100, "height": 200})

    mock_shot.assert_called_once_with(region=(10, 20, 100, 200))
