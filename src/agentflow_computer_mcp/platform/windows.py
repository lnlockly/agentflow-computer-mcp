"""Windows backend: mss capture + pyautogui input + pywin32 windows + pyperclip clipboard."""
from __future__ import annotations

import io
import subprocess
from typing import Any

from PIL import Image


def _encode_png(img: Image.Image, max_width: int = 1280) -> bytes:
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _mss_capture(region: dict[str, int] | None = None) -> Image.Image:
    import mss  # type: ignore[import-not-found]

    with mss.mss() as sct:
        if region:
            box = {
                "left": region["x"],
                "top": region["y"],
                "width": region["width"],
                "height": region["height"],
            }
        else:
            box = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class WindowsBackend:
    name = "windows"

    # ---- Screen capture -----------------------------------------------------
    def capture_screen_fast(self, width_cap: int = 1400, quality: int = 68) -> bytes:
        img = _mss_capture(None)
        if img.width > width_cap:
            ratio = width_cap / img.width
            img = img.resize((width_cap, int(img.height * ratio)), Image.BILINEAR)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=False)
        return out.getvalue()

    def capture_screen(self, region: dict[str, int] | None = None) -> bytes:
        return _encode_png(_mss_capture(region))

    def capture_region(self, x: int, y: int, w: int, h: int) -> bytes:
        return _encode_png(_mss_capture({"x": x, "y": y, "width": w, "height": h}))

    # ---- Mouse --------------------------------------------------------------
    def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, int]:
        import pyautogui

        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        return {"x": x, "y": y, "clicks": clicks}

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> dict[str, int]:
        import pyautogui

        pyautogui.moveTo(x=x, y=y, duration=duration)
        return {"x": x, "y": y}

    def mouse_scroll(self, dx: int, dy: int) -> dict[str, int]:
        import pyautogui

        if dy:
            pyautogui.scroll(dy)
        if dx:
            pyautogui.hscroll(dx)
        return {"dx": dx, "dy": dy}

    # ---- Keyboard -----------------------------------------------------------
    def keyboard_type(self, text: str, interval: float = 0.0) -> dict[str, int]:
        import pyautogui

        pyautogui.typewrite(text, interval=interval)
        return {"length": len(text)}

    def keyboard_key(self, name: str) -> dict[str, str]:
        import pyautogui

        pyautogui.press(name)
        return {"key": name}

    def keyboard_shortcut(self, combo: str) -> dict[str, str]:
        import pyautogui

        parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
        if not parts:
            raise ValueError("empty shortcut combo")
        pyautogui.hotkey(*parts)
        return {"combo": "+".join(parts)}

    # ---- Windows ------------------------------------------------------------
    def window_list(self) -> list[dict[str, Any]]:
        try:
            import win32gui  # type: ignore[import-not-found]
            import win32process  # type: ignore[import-not-found]
        except ImportError:
            return []

        out: list[dict[str, Any]] = []

        def _enum(hwnd: int, _ignore: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:  # noqa: BLE001
                return True
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 0:
                return True
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:  # noqa: BLE001
                pid = 0
            out.append({
                "owner": _owner_for_pid(pid),
                "title": title,
                "pid": int(pid),
                "window_id": int(hwnd),
                "bounds": {"x": left, "y": top, "width": width, "height": height},
            })
            return True

        win32gui.EnumWindows(_enum, None)
        return out

    def window_focus(self, query: str) -> dict[str, Any]:
        try:
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]
        except ImportError:
            return {"ok": False, "error": "pywin32 not installed"}

        target: int | None = None

        def _find(hwnd: int, _ignore: Any) -> bool:
            nonlocal target
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if query.lower() in win32gui.GetWindowText(hwnd).lower():
                target = hwnd
                return False
            return True

        win32gui.EnumWindows(_find, None)
        if target is None:
            return {"ok": False, "error": f"no window matching {query!r}"}
        win32gui.ShowWindow(target, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(target)
        return {"ok": True, "focused": query}

    def app_activate(self, owner: str) -> str:
        result = self.window_focus(owner)
        return f"activated {owner!r}" if result.get("ok") else f"failed: {result.get('error')}"

    # ---- Clipboard ----------------------------------------------------------
    def clipboard_read(self) -> str:
        try:
            import pyperclip  # type: ignore[import-not-found]

            return pyperclip.paste() or ""
        except ImportError:
            return ""

    def clipboard_write(self, text: str) -> None:
        try:
            import pyperclip  # type: ignore[import-not-found]

            pyperclip.copy(text)
        except ImportError:
            pass

    # ---- Terminal -----------------------------------------------------------
    def read_terminal(self) -> str:
        # PowerShell history is the closest analog to "front terminal contents".
        ps = "powershell -NoProfile -Command \"Get-History | Select-Object -Last 50 | Format-List\""
        r = subprocess.run(ps, capture_output=True, text=True, timeout=4, shell=True, check=False)
        if r.returncode == 0 and r.stdout:
            return r.stdout[-2500:]
        return ""


def _owner_for_pid(pid: int) -> str:
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


backend = WindowsBackend()
