"""Tests for the clipboard-paste fallback used when keyboard_type sees non-ASCII.

The active OS keyboard layout filters keystrokes — Cyrillic / CJK typed under
an EN layout comes out as garbage. ``keyboard_type`` detects ``ord(c) > 127``
and routes through ``_type_via_clipboard``, which writes the text to the
clipboard, sends Cmd+V (Mac) / Ctrl+V (Linux/Win), and restores the previous
clipboard contents.
"""
from __future__ import annotations

import sys

import pytest

# Each test targets the current platform's backend module. The backends are
# selected at import time, so we always assert against the one for this OS.
if sys.platform == "darwin":
    from agentflow_computer_mcp.platform import mac as platform_mod
elif sys.platform.startswith("linux"):
    from agentflow_computer_mcp.platform import linux as platform_mod
elif sys.platform == "win32":
    from agentflow_computer_mcp.platform import windows as platform_mod
else:
    pytest.skip(f"unsupported sys.platform: {sys.platform}", allow_module_level=True)


@pytest.fixture
def backend_with_fake_clipboard(monkeypatch: pytest.MonkeyPatch):
    """Return the platform backend with clipboard read/write swapped for an in-memory cell."""
    backend = platform_mod.backend
    state: dict[str, str] = {"value": "PREVIOUS_CLIPBOARD_CONTENT"}

    def fake_read() -> str:
        return state["value"]

    def fake_write(text: str) -> None:
        state["value"] = text

    monkeypatch.setattr(backend, "clipboard_read", fake_read)
    monkeypatch.setattr(backend, "clipboard_write", fake_write)
    return backend, state


def test_non_ascii_routes_through_clipboard_paste(
    backend_with_fake_clipboard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cyrillic input must not hit the keystroke path — it goes via clipboard-paste."""
    backend, state = backend_with_fake_clipboard

    pasted: list[str] = []
    typewrite_calls: list[str] = []

    # The keystroke path must NOT be used for non-ASCII text.
    if sys.platform == "darwin":
        import pyautogui

        monkeypatch.setattr(pyautogui, "typewrite", lambda *a, **kw: typewrite_calls.append(a[0] if a else ""))

        def fake_osa(script: str, timeout: int = 8) -> tuple[int, str]:
            # We expect a Cmd+V AppleScript invocation.
            if 'keystroke "v" using command down' in script:
                pasted.append(state["value"])
                return 0, "ok"
            return 0, "ok"

        monkeypatch.setattr(platform_mod, "_osa", fake_osa)
    elif sys.platform.startswith("linux"):
        import pyautogui

        monkeypatch.setattr(pyautogui, "typewrite", lambda *a, **kw: typewrite_calls.append(a[0] if a else ""))
        monkeypatch.setattr(platform_mod, "_has", lambda cmd: False)
        # On Linux without xdotool we fall back to pyautogui.hotkey('ctrl','v').
        monkeypatch.setattr(
            pyautogui, "hotkey", lambda *a, **kw: pasted.append(state["value"])
        )
    elif sys.platform == "win32":
        import pyautogui

        monkeypatch.setattr(pyautogui, "typewrite", lambda *a, **kw: typewrite_calls.append(a[0] if a else ""))
        monkeypatch.setattr(
            pyautogui, "hotkey", lambda *a, **kw: pasted.append(state["value"])
        )

    result = backend.keyboard_type("привет мир")

    assert result == {"length": len("привет мир")}
    assert typewrite_calls == [], "keystroke path should be skipped for non-ASCII"
    assert pasted == ["привет мир"], "paste must read the just-written target text"


def test_ascii_keeps_keystroke_path(
    backend_with_fake_clipboard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASCII text must continue to use the fast keystroke path, never clipboard."""
    backend, _state = backend_with_fake_clipboard

    typed: list[str] = []
    pasted: list[bool] = []

    import pyautogui

    monkeypatch.setattr(pyautogui, "typewrite", lambda text, interval=0.0: typed.append(text))

    if sys.platform == "darwin":
        def fake_osa(script: str, timeout: int = 8) -> tuple[int, str]:
            if 'keystroke "v"' in script:
                pasted.append(True)
            return 0, "ok"

        monkeypatch.setattr(platform_mod, "_osa", fake_osa)
    else:
        monkeypatch.setattr(
            pyautogui, "hotkey", lambda *a, **kw: pasted.append(True)
        )

    result = backend.keyboard_type("hello world")

    assert result == {"length": len("hello world")}
    assert typed == ["hello world"], "ASCII should use typewrite, not clipboard"
    assert pasted == [], "ASCII must not trigger Cmd+V / Ctrl+V"


def test_clipboard_restored_after_paste(
    backend_with_fake_clipboard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After non-ASCII paste the user's prior clipboard contents must be restored."""
    backend, state = backend_with_fake_clipboard
    original = state["value"]

    if sys.platform == "darwin":
        monkeypatch.setattr(platform_mod, "_osa", lambda script, timeout=8: (0, "ok"))
    elif sys.platform.startswith("linux"):
        monkeypatch.setattr(platform_mod, "_has", lambda cmd: False)
        import pyautogui

        monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **kw: None)
    elif sys.platform == "win32":
        import pyautogui

        monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **kw: None)

    backend.keyboard_type("тест")

    assert state["value"] == original, (
        f"clipboard not restored: expected {original!r}, got {state['value']!r}"
    )
