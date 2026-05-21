from __future__ import annotations

import pyautogui


def type_text(text: str, interval: float = 0.0) -> dict[str, int]:
    pyautogui.typewrite(text, interval=interval)
    return {"length": len(text)}


def key(name: str) -> dict[str, str]:
    pyautogui.press(name)
    return {"key": name}


def shortcut(combo: str) -> dict[str, str]:
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty shortcut combo")
    pyautogui.hotkey(*parts)
    return {"combo": "+".join(parts)}
