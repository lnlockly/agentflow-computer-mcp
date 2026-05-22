"""AgentFlow Desktop · Windows setup wizard.

Single-window Tkinter GUI. User pastes an invite code (base64url JSON
blob containing the three credentials) or expands the advanced
disclosure to enter api_key / device_id / device_token by hand.

On Install we:
  1. Decode the invite (if used) and validate the fields.
  2. pip install --user the package from GitHub, streaming output to a
     progress label.
  3. Write %USERPROFILE%\\.agentflow\\auth.json.
  4. Locate the launcher script (try nt_user scripts, then global,
     then fall back to `python -m agentflow_computer_mcp.desktop_cli`).
  5. Register a scheduled task `AgentFlowDesktop` at logon via
     `schtasks /Create` (no PowerShell).
  6. Launch the daemon once so the user sees it work.
  7. Swap the Install button for «Открыть кабинет» that opens the
     device live page in the default browser.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import sysconfig
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

BG = "#0B0B0F"
FG = "#E8E8EC"
ACCENT = "#A6F25C"
ACCENT_DARK = "#0B0B0F"
FIELD_BG = "#16161D"
MUTED = "#7A7A85"

WINDOW_TITLE = "AgentFlow Desktop · Установка"
PACKAGE_GIT_URL = "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
DEFAULT_WS_URL = "wss://agentflow.website/_agents/_devices/connect"
TASK_NAME = "AgentFlowDesktop"


def b64url_decode(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    return base64.b64decode(s + ("=" * pad))


def _paste_from_clipboard(widget: tk.Widget) -> str:
    """Paste clipboard text into a Tk Entry or Text widget. Returns
    'break' so caller's keysym binding can stop the default handler.

    Works regardless of keyboard layout — we read the clipboard directly
    instead of relying on Tk's default Ctrl+V binding (which doesn't
    fire when the active layout maps V to a non-Latin keysym, e.g.
    Cyrillic м on Russian RU keyboard).
    """
    try:
        text = widget.clipboard_get()
    except tk.TclError:
        return "break"
    try:
        if isinstance(widget, tk.Text):
            # Replace selection if any, else insert at cursor.
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            widget.insert("insert", text)
        else:
            # tk.Entry path.
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            widget.insert("insert", text)
    except Exception:
        pass
    return "break"


def _bind_paste_anywhere(widget: tk.Widget) -> None:
    """Bind Ctrl+V / Ctrl+М (Cyrillic) + right-click → Вставить on a
    Tk Entry or Text. Idempotent — call once per widget on construction."""

    def on_ctrl(event):  # noqa: ANN001
        ks = (event.keysym or "").lower()
        # 'v' = Latin V, 'cyrillic_em' / 'м' = Russian М (same physical key)
        if ks in ("v", "м", "cyrillic_em"):
            return _paste_from_clipboard(widget)
        return None

    widget.bind("<Control-KeyPress>", on_ctrl)

    menu = tk.Menu(widget, tearoff=0, bg=FIELD_BG, fg=FG, activebackground=ACCENT, activeforeground=BG)
    menu.add_command(label="Вставить", command=lambda: _paste_from_clipboard(widget))
    menu.add_command(label="Копировать", command=lambda: _copy_to_clipboard(widget))
    menu.add_command(label="Выбрать всё", command=lambda: _select_all(widget))

    def on_right_click(event):  # noqa: ANN001
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind("<Button-3>", on_right_click)


def _copy_to_clipboard(widget: tk.Widget) -> None:
    try:
        if isinstance(widget, tk.Text):
            text = widget.get("sel.first", "sel.last")
        else:
            text = widget.selection_get()
    except tk.TclError:
        return
    widget.clipboard_clear()
    widget.clipboard_append(text)


def _select_all(widget: tk.Widget) -> str:
    try:
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
        else:
            widget.select_range(0, "end")
            widget.icursor("end")
    except Exception:
        pass
    return "break"


def parse_invite(blob: str) -> dict:
    """Decode a base64url invite blob and validate the three fields."""
    blob = blob.strip()
    if not blob:
        raise ValueError("Invite-код пустой")
    try:
        raw = b64url_decode(blob)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Не получилось распарсить invite-код: {exc}") from exc
    api_key = data.get("k") or ""
    device_id = data.get("d") or ""
    device_token = data.get("t") or ""
    if not api_key or not device_id or not device_token:
        raise ValueError("В invite-коде не хватает полей (k / d / t)")
    if not api_key.startswith("af_"):
        raise ValueError("api_key должен начинаться с af_")
    if not device_token.startswith("aft_"):
        raise ValueError("device_token должен начинаться с aft_")
    return {
        "api_key": api_key,
        "device_id": device_id,
        "device_token": device_token,
    }


def find_python() -> str:
    """Return path to a working python interpreter on the host.

    Prefer the interpreter under which the setup .exe runs only if it
    looks like a real on-disk python (PyInstaller's frozen runtime
    won't have `pip`). Otherwise look for `python` or `python3` on
    PATH via `where`.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3"):
        try:
            result = subprocess.run(
                ["where", name],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0].strip()
    raise RuntimeError(
        "Python 3.11+ не найден в PATH. Установи Python с python.org "
        "и поставь галку «Add to PATH»."
    )


def write_auth_file(creds: dict) -> Path:
    af_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".agentflow"
    af_dir.mkdir(parents=True, exist_ok=True)
    auth_path = af_dir / "auth.json"
    payload = {
        "api_key": creds["api_key"],
        "device_id": creds["device_id"],
        "enrollment_token": creds["device_token"],
        "device_secret": "",
        "ws_url": os.environ.get("AF_WS_URL") or DEFAULT_WS_URL,
    }
    auth_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return auth_path


def locate_launcher(python_exe: str) -> tuple[str, list[str]]:
    """Find the agentflow-desktop launcher .exe; fall back to python -m."""

    def probe(scheme: str | None) -> str | None:
        try:
            if scheme:
                out = subprocess.run(
                    [
                        python_exe,
                        "-c",
                        f"import sysconfig; print(sysconfig.get_path('scripts', '{scheme}') or '')",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return out.stdout.strip() or None
            return sysconfig.get_path("scripts") or None
        except Exception:
            return None

    candidates: list[str] = []
    for scheme in ("nt_user", None):
        path = probe(scheme)
        if path:
            candidates.append(path)
    for scripts_dir in candidates:
        for name in ("agentflow-desktop.exe", "agentflow-computer-mcp.exe"):
            exe = Path(scripts_dir) / name
            if exe.exists():
                if "computer-mcp" in name:
                    return str(exe), ["--mode", "ws"]
                return str(exe), ["run"]
    return python_exe, ["-m", "agentflow_computer_mcp.desktop_cli", "run"]


def register_scheduled_task(executable: str, args: list[str]) -> None:
    """Register the daemon to autostart at user logon via schtasks."""
    tr_value = f'"{executable}" ' + " ".join(args)
    subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            tr_value,
            "/SC",
            "ONLOGON",
            "/F",
            "/RL",
            "LIMITED",
        ],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def launch_daemon(executable: str, args: list[str]) -> None:
    """Start the daemon once so the cabinet sees the device immediately."""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    subprocess.Popen(
        [executable, *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


class SetupWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=BG)
        self.root.geometry("520x440")
        self.root.minsize(520, 440)
        self._build_styles()
        self._build_widgets()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.creds: dict | None = None
        self.device_id: str | None = None

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "AF.TButton",
            background=ACCENT,
            foreground=ACCENT_DARK,
            borderwidth=0,
            focusthickness=0,
            font=("Segoe UI", 10, "bold"),
            padding=(14, 6),
        )
        style.map("AF.TButton", background=[("active", "#B8FF6F"), ("disabled", "#3A4A2A")])
        style.configure(
            "AF.TEntry",
            fieldbackground=FIELD_BG,
            foreground=FG,
            insertcolor=FG,
            borderwidth=0,
        )

    def _build_widgets(self) -> None:
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=14)

        tk.Label(
            wrap,
            text="Вставь invite-код из кабинета AgentFlow",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x")

        invite_row = tk.Frame(wrap, bg=BG)
        invite_row.pack(fill="x", pady=(4, 6))

        self.invite = tk.Text(
            invite_row,
            height=3,
            bg=FIELD_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=("Consolas", 9),
            wrap="char",
        )
        self.invite.pack(side="left", fill="x", expand=True)

        # Tkinter's default Ctrl+V binding doesn't fire when the user has
        # a non-Latin keyboard layout active (Cyrillic Ctrl+М ≠ Ctrl+V).
        # Bind both common Russian-layout keysyms + provide a dedicated
        # «Вставить» button + right-click menu so paste always works.
        _bind_paste_anywhere(self.invite)
        tk.Button(
            invite_row,
            text="Вставить",
            bg=ACCENT,
            fg=BG,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=lambda: _paste_from_clipboard(self.invite),
            padx=10,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(6, 0), ipady=18)

        self.advanced_toggle = tk.Label(
            wrap,
            text="▸ Расширенные настройки",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            cursor="hand2",
            anchor="w",
        )
        self.advanced_toggle.pack(fill="x")
        self.advanced_toggle.bind("<Button-1>", lambda _e: self._toggle_advanced())

        self.advanced_frame = tk.Frame(wrap, bg=BG)
        self.adv_open = False
        self.fields: dict[str, tk.Entry] = {}
        for key, label in (
            ("api_key", "api_key (af_live_…)"),
            ("device_id", "device_id (uuid)"),
            ("device_token", "device_token (aft_…)"),
        ):
            row = tk.Frame(self.advanced_frame, bg=BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=BG, fg=MUTED, width=22, anchor="w").pack(side="left")
            entry = tk.Entry(row, bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat")
            entry.pack(side="left", fill="x", expand=True)
            _bind_paste_anywhere(entry)
            self.fields[key] = entry

        # Live log — scrolling text + animated progressbar. The pip
        # install warmup pulls the whole git history (~30-60s of silent
        # work in the original GUI), which looked frozen. Now the bar
        # spins, the latest 6 lines of pip output stream in real-time.
        self.progressbar = ttk.Progressbar(
            wrap, mode="indeterminate", length=100, style="AF.Horizontal.TProgressbar"
        )
        self.progressbar.pack(fill="x", pady=(8, 4))

        self.log_view = tk.Text(
            wrap,
            height=6,
            bg=FIELD_BG,
            fg=MUTED,
            insertbackground=FG,
            relief="flat",
            font=("Consolas", 8),
            wrap="none",
            state="disabled",
        )
        self.log_view.pack(fill="x", pady=(0, 6))
        self.log_view.tag_configure("step", foreground=ACCENT, font=("Consolas", 8, "bold"))
        self.log_view.tag_configure("dim", foreground=MUTED)

        bottom = tk.Frame(wrap, bg=BG)
        bottom.pack(fill="x", side="bottom", pady=(8, 0))

        self.progress = tk.Label(
            bottom,
            text="",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=320,
        )
        self.progress.pack(side="left", fill="x", expand=True)

        self.action_btn = ttk.Button(
            bottom, text="Install", style="AF.TButton", command=self._on_install
        )
        self.action_btn.pack(side="right")

    def _toggle_advanced(self) -> None:
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.advanced_toggle.configure(text="▾ Расширенные настройки")
            self.advanced_frame.pack(fill="x", pady=(4, 0), after=self.advanced_toggle)
            self.root.geometry("520x540")
        else:
            self.advanced_toggle.configure(text="▸ Расширенные настройки")
            self.advanced_frame.pack_forget()
            self.root.geometry("520x440")

    def _collect_creds(self) -> dict:
        blob = self.invite.get("1.0", "end").strip()
        if blob:
            return parse_invite(blob)
        manual = {k: e.get().strip() for k, e in self.fields.items()}
        if not all(manual.values()):
            raise ValueError(
                "Вставь invite-код или открой «Расширенные настройки» и заполни все три поля."
            )
        if not manual["api_key"].startswith("af_"):
            raise ValueError("api_key должен начинаться с af_")
        if not manual["device_token"].startswith("aft_"):
            raise ValueError("device_token должен начинаться с aft_")
        return manual

    def _log(self, msg: str) -> None:
        # short single-line summary at the bottom
        self.progress.configure(text=msg)
        # full multi-line log above
        self.log_view.configure(state="normal")
        # Highlight step headers («Шаг N/4: ...») in accent colour.
        is_step = msg.startswith("Шаг ")
        self.log_view.insert("end", msg + "\n", "step" if is_step else "dim")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")
        self.root.update_idletasks()

    def _drain_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        if getattr(self, "_install_thread", None) and self._install_thread.is_alive():
            self.root.after(120, self._drain_log_queue)

    def _on_install(self) -> None:
        try:
            self.creds = self._collect_creds()
        except ValueError as exc:
            messagebox.showerror(WINDOW_TITLE, str(exc))
            return
        self.action_btn.state(["disabled"])
        self.progressbar.start(12)  # animate the indeterminate bar
        self._log("Запуск установки…")
        self._install_thread = threading.Thread(target=self._install_worker, daemon=True)
        self._install_thread.start()
        self.root.after(120, self._drain_log_queue)

    def _install_worker(self) -> None:
        try:
            self.log_queue.put("Шаг 1/4: ищу Python")
            python_exe = find_python()
            self.log_queue.put(f"  → {python_exe}")

            self.log_queue.put("Шаг 2/4: качаю agentflow-computer-mcp с GitHub")
            self.log_queue.put("  · клонирую репозиторий через git (≈30-60 сек первый запуск)")
            # PyInstaller's frozen runtime + subprocess on Windows
            # buffers pip output by default — the log stayed empty even
            # though pip was working. Force unbuffered IO via `python -u`
            # + PYTHONUNBUFFERED + raw byte reads so every newline shows
            # up in real time.
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PIP_NO_INPUT"] = "1"
            env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            proc = subprocess.Popen(
                [
                    python_exe,
                    "-u",
                    "-m",
                    "pip",
                    "install",
                    "--user",
                    "--upgrade",
                    "--progress-bar",
                    "off",
                    "-v",
                    PACKAGE_GIT_URL,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert proc.stdout is not None
            last_line = ""
            buf = bytearray()
            last_heartbeat = [time.monotonic()]

            def heartbeat() -> None:
                if proc.poll() is None:
                    if time.monotonic() - last_heartbeat[0] > 10:
                        self.log_queue.put("  · всё ещё работаю, не закрывай окно")
                        last_heartbeat[0] = time.monotonic()
                    threading.Timer(5.0, heartbeat).start()

            heartbeat()

            while True:
                chunk = proc.stdout.read(1)
                if not chunk:
                    break
                if chunk in (b"\n", b"\r"):
                    if buf:
                        line = buf.decode("utf-8", "replace").strip()
                        buf.clear()
                        if line:
                            last_line = line
                            last_heartbeat[0] = time.monotonic()
                            self.log_queue.put(f"  · {line[:140]}")
                else:
                    buf.extend(chunk)
            if buf:
                line = buf.decode("utf-8", "replace").strip()
                if line:
                    last_line = line
                    self.log_queue.put(f"  · {line[:140]}")
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"pip install failed (rc={rc}): {last_line}")

            self.log_queue.put("Шаг 3/4: сохраняю учётные данные")
            assert self.creds is not None
            auth_path = write_auth_file(self.creds)
            self.device_id = self.creds["device_id"]
            self.log_queue.put(f"  → {auth_path}")

            executable, args = locate_launcher(python_exe)
            self.log_queue.put(f"  → launcher: {executable}")

            self.log_queue.put(f"Шаг 4/4: регистрирую автозапуск ({TASK_NAME})")
            register_scheduled_task(executable, args)
            self.log_queue.put("  → задача создана, запускаю демон")
            launch_daemon(executable, args)

            self.log_queue.put("Готово. Устройство онлайн.")
            self.root.after(0, self._on_success)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            self.root.after(0, lambda: self._on_failure(err))
        except Exception as exc:
            self.root.after(0, lambda: self._on_failure(str(exc)))

    def _on_failure(self, msg: str) -> None:
        try:
            self.progressbar.stop()
        except Exception:
            pass
        self.action_btn.state(["!disabled"])
        self._log(f"Ошибка: {msg[:200]}")
        messagebox.showerror(WINDOW_TITLE, msg)

    def _on_success(self) -> None:
        try:
            self.progressbar.stop()
            self.progressbar.configure(mode="determinate", value=100)
        except Exception:
            pass
        self.action_btn.configure(text="Открыть кабинет", command=self._open_cabinet)
        self.action_btn.state(["!disabled"])
        self._log("Установка завершена. Открой кабинет — устройство уже там.")

    def _open_cabinet(self) -> None:
        device_id = self.device_id or ""
        url = f"https://agentflow.website/cabinet/devices/{device_id}/live"
        webbrowser.open(url)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    SetupWindow().run()


if __name__ == "__main__":
    main()
