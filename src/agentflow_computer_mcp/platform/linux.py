"""Linux backend: mss capture + pyautogui input + xdotool/wmctrl windows + xclip clipboard.

Supports both X11 and Wayland. Wayland uses ``grim`` for capture and falls back to
xdotool where possible. Clipboard prefers ``wl-copy``/``wl-paste`` on Wayland, ``xclip``
on X11. Window listing on pure Wayland is limited and returns whatever
``wmctrl``/``xdotool`` can see through XWayland (usually XWayland clients only).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
from typing import Any

from PIL import Image


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _is_wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def _encode_png(img: Image.Image, max_width: int = 1280) -> bytes:
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _grim_capture(region: dict[str, int] | None = None) -> Image.Image:
    cmd = ["grim"]
    if region:
        geom = f"{region['x']},{region['y']} {region['width']}x{region['height']}"
        cmd += ["-g", geom]
    cmd += ["-"]
    r = subprocess.run(cmd, capture_output=True, timeout=8, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"grim failed: {r.stderr!r}")
    return Image.open(io.BytesIO(r.stdout)).convert("RGBA")


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
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            box = mon
        shot = sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _capture(region: dict[str, int] | None = None) -> Image.Image:
    if _is_wayland() and _has("grim"):
        try:
            return _grim_capture(region)
        except Exception:  # noqa: BLE001
            pass
    return _mss_capture(region)


class LinuxBackend:
    name = "linux"

    # ---- Screen capture -----------------------------------------------------
    def capture_screen_fast(self, width_cap: int = 1400, quality: int = 68) -> bytes:
        img = _capture(None)
        if img.width > width_cap:
            ratio = width_cap / img.width
            img = img.resize((width_cap, int(img.height * ratio)), Image.BILINEAR)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=False)
        return out.getvalue()

    def capture_screen(self, region: dict[str, int] | None = None) -> bytes:
        return _encode_png(_capture(region))

    def capture_region(self, x: int, y: int, w: int, h: int) -> bytes:
        return _encode_png(_capture({"x": x, "y": y, "width": w, "height": h}))

    # ---- Screen geometry ----------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        import pyautogui

        # On X11/Xvfb there is no Retina scaling, so pyautogui.size() is both
        # the capture resolution and the click space (e.g. Xvfb 1440x900).
        size = pyautogui.size()
        return int(size[0]), int(size[1])

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
        # See mac.py: pyautogui.typewrite on Linux also goes through the active
        # XKB layout, so Cyrillic / CJK lose data when the user is in EN.
        # Route non-ASCII through clipboard-paste; ASCII keeps the fast path.
        if any(ord(c) > 127 for c in text):
            self._type_via_clipboard(text)
            return {"length": len(text)}
        import pyautogui

        pyautogui.typewrite(text, interval=interval)
        return {"length": len(text)}

    def _type_via_clipboard(self, text: str) -> None:
        """Paste ``text`` at the current focus, preserving the user's clipboard."""
        import contextlib

        saved = ""
        try:
            saved = self.clipboard_read()
        except Exception:  # noqa: BLE001
            saved = ""
        try:
            self.clipboard_write(text)
            import time

            time.sleep(0.05)
            if _has("xdotool"):
                subprocess.run(
                    ["xdotool", "key", "ctrl+v"],
                    capture_output=True,
                    timeout=4,
                    check=False,
                )
            else:
                import pyautogui

                pyautogui.hotkey("ctrl", "v")
            time.sleep(0.05)
        finally:
            with contextlib.suppress(Exception):
                self.clipboard_write(saved)

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
        # wmctrl -lG output: <window_id> <desktop> <x> <y> <w> <h> <host> <title>
        if not _has("wmctrl"):
            return []
        r = subprocess.run(["wmctrl", "-lG", "-p"], capture_output=True, text=True, timeout=4, check=False)
        if r.returncode != 0:
            return []
        out: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            wid, _desk, pid, x, y, w, h, _host, title = parts
            try:
                window_id = int(wid, 16)
                bounds = {
                    "x": int(x),
                    "y": int(y),
                    "width": int(w),
                    "height": int(h),
                }
                pid_int = int(pid)
            except ValueError:
                continue
            out.append({
                "owner": _owner_for_pid(pid_int),
                "title": title,
                "pid": pid_int,
                "window_id": window_id,
                "bounds": bounds,
            })
        return out

    def window_focus(self, query: str) -> dict[str, Any]:
        if _has("wmctrl"):
            r = subprocess.run(["wmctrl", "-a", query], capture_output=True, text=True, timeout=4, check=False)
            if r.returncode == 0:
                return {"ok": True, "focused": query}
        if _has("xdotool"):
            r = subprocess.run(
                ["xdotool", "search", "--name", query, "windowactivate"],
                capture_output=True, text=True, timeout=4, check=False,
            )
            if r.returncode == 0:
                return {"ok": True, "focused": query}
        return {"ok": False, "error": "no window manager tool (install wmctrl or xdotool)"}

    def app_activate(self, owner: str) -> str:
        result = self.window_focus(owner)
        return f"activated {owner!r}" if result.get("ok") else f"failed: {result.get('error')}"

    # ---- Clipboard ----------------------------------------------------------
    def clipboard_read(self) -> str:
        if _is_wayland() and _has("wl-paste"):
            r = subprocess.run(["wl-paste", "-n"], capture_output=True, text=True, timeout=4, check=False)
            return r.stdout
        if _has("xclip"):
            r = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, text=True, timeout=4, check=False,
            )
            return r.stdout
        if _has("xsel"):
            r = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True, text=True, timeout=4, check=False,
            )
            return r.stdout
        return ""

    def clipboard_write(self, text: str) -> None:
        if _is_wayland() and _has("wl-copy"):
            subprocess.run(["wl-copy"], input=text, text=True, timeout=4, check=False)
            return
        if _has("xclip"):
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text, text=True, timeout=4, check=False,
            )
            return
        if _has("xsel"):
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text, text=True, timeout=4, check=False,
            )
            return

    # ---- Terminal -----------------------------------------------------------
    def read_terminal(self) -> str:
        # Best-effort: tmux capture-pane of attached client's active pane.
        if _has("tmux"):
            r = subprocess.run(
                ["tmux", "capture-pane", "-p", "-S", "-200"],
                capture_output=True, text=True, timeout=4, check=False,
            )
            if r.returncode == 0:
                return r.stdout[-2500:]
        return ""


def _owner_for_pid(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


backend = LinuxBackend()
