from __future__ import annotations

from typing import Literal

import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0

Button = Literal["left", "right", "middle"]


def click(x: int, y: int, button: Button = "left", clicks: int = 1) -> dict[str, int]:
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)
    return {"x": x, "y": y, "clicks": clicks}


def move(x: int, y: int, duration: float = 0.0) -> dict[str, int]:
    pyautogui.moveTo(x=x, y=y, duration=duration)
    return {"x": x, "y": y}


def scroll(dx: int, dy: int) -> dict[str, int]:
    if dy:
        pyautogui.scroll(dy)
    if dx:
        pyautogui.hscroll(dx)
    return {"dx": dx, "dy": dy}
