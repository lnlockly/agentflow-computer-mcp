"""macOS backend: Quartz / CoreGraphics + AppleScript + pyautogui + pbpaste/pbcopy."""
from __future__ import annotations

import contextlib
import io
import subprocess
import time
from typing import Any

import pyautogui
from PIL import Image

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0

try:
    import Quartz  # type: ignore[import-not-found]
    from Quartz import CoreGraphics as _CG  # type: ignore[import-not-found]

    _HAS_QUARTZ = True
    _MAIN_DISPLAY = _CG.CGMainDisplayID()
except ImportError:
    Quartz = None  # type: ignore[assignment]
    _CG = None  # type: ignore[assignment]
    _HAS_QUARTZ = False
    _MAIN_DISPLAY = 0


def _osa(script: str, timeout: int = 8) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout or r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "osascript timeout"


def _encode_png(img: Image.Image, max_width: int = 1280) -> bytes:
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _capture_quartz_full() -> Image.Image:
    if not _HAS_QUARTZ:
        raise RuntimeError("Quartz unavailable")
    img_ref = _CG.CGDisplayCreateImage(_MAIN_DISPLAY)
    if img_ref is None:
        raise RuntimeError("CGDisplayCreateImage returned None")
    w = Quartz.CGImageGetWidth(img_ref)
    h = Quartz.CGImageGetHeight(img_ref)
    bpr = Quartz.CGImageGetBytesPerRow(img_ref)
    raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img_ref)))
    return Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", bpr, 1)


def _capture_quartz_region(region: dict[str, int]) -> Image.Image:
    if not _HAS_QUARTZ:
        raise RuntimeError("Quartz unavailable")
    rect = _CG.CGRectMake(
        float(region["x"]),
        float(region["y"]),
        float(region["width"]),
        float(region["height"]),
    )
    image_ref = Quartz.CGWindowListCreateImage(
        rect,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if image_ref is None:
        raise RuntimeError("CGWindowListCreateImage returned None")
    width = Quartz.CGImageGetWidth(image_ref)
    height = Quartz.CGImageGetHeight(image_ref)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
    raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image_ref)))
    return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", bytes_per_row, 1)


class MacBackend:
    name = "mac"

    # ---- Screen capture -----------------------------------------------------
    def capture_screen_fast(self, width_cap: int = 1280, quality: int = 58) -> bytes:
        img = _capture_quartz_full() if _HAS_QUARTZ else pyautogui.screenshot()
        if img.width > width_cap:
            ratio = width_cap / img.width
            img = img.resize((width_cap, int(img.height * ratio)), Image.BILINEAR)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=False)
        return out.getvalue()

    def capture_screen(self, region: dict[str, int] | None = None) -> bytes:
        if _HAS_QUARTZ:
            img = _capture_quartz_region(region) if region else _capture_quartz_full()
        else:
            if region:
                box = (region["x"], region["y"], region["width"], region["height"])
                img = pyautogui.screenshot(region=box)
            else:
                img = pyautogui.screenshot()
        return _encode_png(img)

    def capture_region(self, x: int, y: int, w: int, h: int) -> bytes:
        return self.capture_screen({"x": x, "y": y, "width": w, "height": h})

    # ---- Screen geometry ----------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        # pyautogui.size() returns logical points on macOS (Retina-aware),
        # which is exactly the space pyautogui.click() below consumes.
        size = pyautogui.size()
        return int(size[0]), int(size[1])

    # ---- Mouse --------------------------------------------------------------
    def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, int]:
        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        return {"x": x, "y": y, "clicks": clicks}

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> dict[str, int]:
        pyautogui.moveTo(x=x, y=y, duration=duration)
        return {"x": x, "y": y}

    def mouse_scroll(self, dx: int, dy: int) -> dict[str, int]:
        if dy:
            pyautogui.scroll(dy)
        if dx:
            pyautogui.hscroll(dx)
        return {"dx": dx, "dy": dy}

    # ---- Keyboard -----------------------------------------------------------
    def keyboard_type(self, text: str, interval: float = 0.0) -> dict[str, int]:
        # pyautogui.typewrite on macOS routes through the active keyboard layout's
        # character map. Russian / Greek / Chinese chars come out as garbage when
        # the user is in EN layout (the typewrite path can only send keycodes that
        # exist in the active layout). Clipboard-paste sidesteps the layout map
        # entirely — pasted text lands as-is no matter what layout is active.
        if any(ord(c) > 127 for c in text):
            self._type_via_clipboard(text)
            return {"length": len(text)}
        pyautogui.typewrite(text, interval=interval)
        return {"length": len(text)}

    def _type_via_clipboard(self, text: str) -> None:
        """Paste ``text`` at the current focus, preserving the user's clipboard.

        Used for non-ASCII content where the keystroke path would be filtered
        through the active keyboard layout and produce garbage.
        """
        saved = ""
        try:
            saved = self.clipboard_read()
        except Exception:  # noqa: BLE001
            saved = ""
        try:
            self.clipboard_write(text)
            # Small delay so pbcopy commits before Cmd+V reads the pasteboard.
            time.sleep(0.05)
            _osa('tell application "System Events" to keystroke "v" using command down')
            time.sleep(0.05)
        finally:
            with contextlib.suppress(Exception):
                self.clipboard_write(saved)

    def keyboard_key(self, name: str) -> dict[str, str]:
        pyautogui.press(name)
        return {"key": name}

    def keyboard_shortcut(self, combo: str) -> dict[str, str]:
        parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
        if not parts:
            raise ValueError("empty shortcut combo")
        pyautogui.hotkey(*parts)
        return {"combo": "+".join(parts)}

    # ---- Windows ------------------------------------------------------------
    def window_list(self) -> list[dict[str, Any]]:
        if not _HAS_QUARTZ:
            return []
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        out: list[dict[str, Any]] = []
        for w in window_list:
            bounds = w.get("kCGWindowBounds", {})
            out.append({
                "owner": w.get("kCGWindowOwnerName", ""),
                "title": w.get("kCGWindowName", ""),
                "pid": int(w.get("kCGWindowOwnerPID", 0)),
                "window_id": int(w.get("kCGWindowNumber", 0)),
                "bounds": {
                    "x": int(bounds.get("X", 0)),
                    "y": int(bounds.get("Y", 0)),
                    "width": int(bounds.get("Width", 0)),
                    "height": int(bounds.get("Height", 0)),
                },
            })
        return out

    def window_focus(self, query: str) -> dict[str, Any]:
        safe = query.replace('"', "'")
        rc, out = _osa(f'tell application "{safe}" to activate')
        if rc != 0:
            return {"ok": False, "error": out}
        return {"ok": True, "focused": query}

    def app_activate(self, owner: str) -> str:
        rc, out = _osa(f'tell application "{owner}" to activate')
        time.sleep(0.5)
        return f"activated {owner!r}" if rc == 0 else f"failed: {out}"

    # ---- Clipboard ----------------------------------------------------------
    def clipboard_read(self) -> str:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=4)
        return r.stdout

    def clipboard_write(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=4, check=False)

    # ---- Terminal -----------------------------------------------------------
    def read_terminal(self) -> str:
        rc, out = _osa(
            'tell application "iTerm" to tell current window to tell current session to get contents'
        )
        if rc == 0 and out:
            return out[-2500:]
        rc2, out2 = _osa(
            'tell application "Terminal" to tell front window to get contents of selected tab'
        )
        if rc2 == 0 and out2:
            return out2[-2500:]
        return ""


backend = MacBackend()
