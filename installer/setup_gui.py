"""AgentFlow Desktop · Windows self-contained installer + daemon.

Two roles in one binary:

1. Default (no args / `--setup`): show the Tkinter wizard. The user pastes
   an invite code, we write `%USERPROFILE%\\.agentflow\\auth.json`, copy
   THIS .exe to `%LOCALAPPDATA%\\AgentFlow\\agentflow-desktop.exe`,
   register a logon scheduled task that points at the copied .exe with
   `--daemon`, then spawn the daemon immediately.

2. `--daemon`: boot the agent runtime directly (no GUI). The same .exe
   is used here because PyInstaller already bundles CPython + the whole
   `agentflow_computer_mcp` package, so the user never needs Python.

3. `--daemon --selftest`: run the platform backend selftest and exit. CI
   uses this to validate the bundled daemon actually starts before
   publishing the release artifact.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
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
DEFAULT_WS_URL = "wss://agentflow.website/_agents/_devices/connect"
DEFAULT_API_BASE = "https://agentflow.website/_agents"
TASK_NAME = "AgentFlowDesktop"
DAEMON_DIR_NAME = "AgentFlow"
DAEMON_EXE_NAME = "agentflow-desktop.exe"
TRAY_EXE_NAME = "agentflow-tray.exe"
TRAY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TRAY_RUN_VALUE = "AgentFlowTray"
DAEMON_RUN_VALUE = "AgentFlowDaemon"


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
    """Decode a base64url invite blob and validate the three fields.

    Legacy format kept for users with old install instructions. New flow
    pastes a single `af_live_*` / `af_install_*` token; see `parse_token`.
    """
    blob = blob.strip()
    if not blob:
        raise ValueError("Токен пустой")
    try:
        raw = b64url_decode(blob)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Не получилось распарсить токен: {exc}") from exc
    api_key = data.get("k") or ""
    device_id = data.get("d") or ""
    device_token = data.get("t") or ""
    if not api_key or not device_id or not device_token:
        raise ValueError("В токене не хватает полей (k / d / t)")
    if not api_key.startswith("af_"):
        raise ValueError("api_key должен начинаться с af_")
    if not device_token.startswith("aft_"):
        raise ValueError("device_token должен начинаться с aft_")
    return {
        "api_key": api_key,
        "device_id": device_id,
        "device_token": device_token,
    }


def _default_device_name() -> str:
    """Friendly device name for `POST /me/devices`. Falls back to a
    generic label if the host name lookup fails (sandbox / locked-down
    runners)."""
    try:
        host = platform.node() or ""
    except Exception:
        host = ""
    host = host.strip()
    if not host:
        host = "Windows PC"
    osname = "Windows" if os.name == "nt" else platform.system() or "Desktop"
    return f"{osname} · {host}"[:64]


def mint_device_via_api(
    api_key: str,
    *,
    api_base: str | None = None,
    name: str | None = None,
    opener=None,
    timeout: float = 15.0,
) -> dict:
    """Exchange a long-lived `af_live_*` key for a fresh device row.

    Calls `POST {api_base}/me/devices` with `x-api-key: <key>` and
    returns the `device_id` + one-time `enrollment_token` straight from
    the server. The daemon then upgrades that token to a permanent
    `device_secret` on first WS connect.

    `opener` is the HTTP transport (defaults to `urllib.request.urlopen`)
    so tests can inject a fake without touching the network.
    """
    if not api_key.startswith("af_"):
        raise ValueError("api_key должен начинаться с af_")
    base = (api_base or os.environ.get("AF_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    url = f"{base}/me/devices"
    body = json.dumps({"name": name or _default_device_name()}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "agentflow-desktop-setup",
        },
    )
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=timeout) as resp:  # noqa: S310
            payload = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        if exc.code == 401:
            raise ValueError(
                "Токен не принят кабинетом (401). Открой кабинет и получи новый."
            ) from exc
        raise ValueError(
            f"Не получилось создать устройство: HTTP {exc.code} {detail[:200]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"Кабинет недоступен: {exc}") from exc
    try:
        data = json.loads(payload)
    except Exception as exc:
        raise ValueError(f"Кабинет вернул не-JSON ответ: {payload[:200]}") from exc
    # POST /me/devices returns `{ok, device: {id, ...}, enrollment_token}`.
    # Older variants returned device fields at the top level — accept both.
    device_obj = data.get("device") if isinstance(data.get("device"), dict) else {}
    device_id = (
        device_obj.get("id")
        or data.get("id")
        or data.get("device_id")
        or ""
    ).strip()
    enrollment_token = (
        data.get("enrollment_token") or data.get("token") or ""
    ).strip()
    if not device_id or not enrollment_token:
        raise ValueError(
            f"В ответе кабинета нет device_id или enrollment_token: {payload[:200]}"
        )
    if not enrollment_token.startswith("aft_"):
        raise ValueError(
            f"enrollment_token должен начинаться с aft_, получили: {enrollment_token[:12]}…"
        )
    return {
        "api_key": api_key,
        "device_id": device_id,
        "device_token": enrollment_token,
    }


def parse_token(raw: str, *, mint=mint_device_via_api) -> dict:
    """Single-field entry point — figures out whether `raw` is a modern
    `af_live_*` / `af_install_*` key or a legacy base64url invite blob and
    returns the three credentials the daemon expects.

    `mint` is injectable so unit tests don't reach the network.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Токен пустой")
    if raw.startswith("af_live_") or raw.startswith("af_install_"):
        return mint(raw)
    # Legacy path: base64url JSON blob with api_key + device_id + token.
    return parse_invite(raw)


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


def _self_exe_path() -> Path:
    """Path to the currently running setup .exe (PyInstaller frozen) or
    fallback to sys.executable + this script when running from source.

    Honors `AF_SETUP_EXE_OVERRIDE` so the CI headless e2e job can point at
    the freshly built `dist/agentflow-desktop-setup.exe` without spawning
    the Tk wizard. Production users never see this env var.
    """
    override = os.environ.get("AF_SETUP_EXE_OVERRIDE", "").strip()
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    # Dev mode — point at the .py file so smoke tests can still verify
    # the copy step. The real install always runs frozen.
    return Path(__file__).resolve()


def install_daemon_binary() -> Path:
    """Copy this .exe to %LOCALAPPDATA%\\AgentFlow\\agentflow-desktop.exe
    so the scheduled task survives the user moving / deleting the
    download from the Downloads folder."""
    target_dir = _install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / DAEMON_EXE_NAME
    src = _self_exe_path()
    if src.resolve() == target.resolve():
        # Already running from the installed location — nothing to copy.
        return target
    # On Windows you can't overwrite a running .exe. We're running from
    # the downloaded copy, not the target, so a plain copy is fine. If
    # the target is locked (previous daemon still running) try to delete
    # it first; if that fails fall back to a `.new` sidecar that the
    # task will pick up on next logon.
    try:
        if target.exists():
            target.unlink()
        shutil.copy2(src, target)
    except PermissionError:
        sidecar = target.with_suffix(".new.exe")
        shutil.copy2(src, sidecar)
        return sidecar
    return target


def register_scheduled_task(executable: Path) -> tuple[bool, str]:
    """Register the daemon to autostart at user logon.

    Uses schtasks /XML instead of /TR because /TR's quoting is broken
    when the executable path contains spaces (e.g. `C:\\Users\\Mick
    Thomson\\…`). XML schema sidesteps all quoting and is the same
    format the Windows Task Scheduler UI exports.

    Returns `(ok, detail)`. `ok=False` means the install can keep going
    via the `HKCU\\…\\Run\\AgentFlowDaemon` fallback — Task Scheduler
    needs `Log on as a batch job` rights or a non-locked-down policy,
    which some corporate / Windows Home installs don't grant.
    """
    import tempfile
    from xml.sax.saxutils import escape as xml_escape

    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <Enabled>true</Enabled>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{xml_escape(str(executable))}</Command>\n"
        "      <Arguments>--daemon</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", delete=False, encoding="utf-16"
    ) as fp:
        fp.write(xml)
        xml_path = fp.name
    try:
        proc = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                TASK_NAME,
                "/XML",
                xml_path,
                "/F",
            ],
            check=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass
    if proc.returncode == 0:
        return True, ""
    detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
    return False, f"schtasks rc={proc.returncode} {detail[:300]}"


def register_daemon_run_key(executable: Path) -> None:
    """Fallback autostart — `HKCU\\…\\Run\\AgentFlowDaemon = "<exe> --daemon"`.

    Less robust than Task Scheduler (no rerun on crash, doesn't survive a
    locked screen if the user is signed out) but it works on every
    Windows edition without needing batch-job rights. Wizard falls back
    here when `schtasks /Create` fails.
    """
    if os.name != "nt":
        return
    import winreg  # type: ignore[import-not-found]

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, TRAY_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key,
            DAEMON_RUN_VALUE,
            0,
            winreg.REG_SZ,
            f'"{executable}" --daemon',
        )


def launch_daemon(executable: Path) -> None:
    """Start the daemon once so the cabinet sees the device immediately."""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    subprocess.Popen(
        [str(executable), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _install_dir() -> Path:
    """`%LOCALAPPDATA%\\AgentFlow\\` — owner of both daemon + tray .exes."""
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or (Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "Local")
    )
    return base / DAEMON_DIR_NAME


def _bundled_tray_path() -> Path | None:
    """Locate `agentflow-tray.exe` shipped alongside the setup .exe.

    The release ZIP drops both binaries in the same dir; PyInstaller's
    spec builds them side-by-side under `dist/`. When the user runs the
    setup wizard the tray exe sits next to it. Returns `None` if absent
    (dev source mode, or release packaging glitch) so callers can fall
    back to `python -m agentflow_computer_mcp.winapp`."""
    src_dir = _self_exe_path().parent
    candidate = src_dir / TRAY_EXE_NAME
    if candidate.exists():
        return candidate
    # Allow CI override (same pattern as AF_SETUP_EXE_OVERRIDE).
    override = os.environ.get("AF_TRAY_EXE_OVERRIDE", "").strip()
    if override:
        p = Path(override)
        if p.exists():
            return p.resolve()
    return None


def install_tray_binary() -> Path | None:
    """Copy `agentflow-tray.exe` next to the daemon binary. Returns the
    target path on success, `None` if the source isn't bundled (dev
    mode). Same overwrite policy as the daemon: PermissionError falls
    back to a `.new.exe` sidecar."""
    src = _bundled_tray_path()
    if src is None:
        return None
    target_dir = _install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / TRAY_EXE_NAME
    if src.resolve() == target.resolve():
        return target
    try:
        if target.exists():
            target.unlink()
        shutil.copy2(src, target)
    except PermissionError:
        sidecar = target.with_suffix(".new.exe")
        shutil.copy2(src, sidecar)
        return sidecar
    return target


def register_tray_autostart(executable: Path) -> None:
    """Write `HKCU\\…\\Run\\AgentFlowTray = "<path>"` so the tray launches
    on every user logon. Mirrors `winapp.autostart.install()` but points
    at the bundled tray .exe instead of `pythonw -m …` so end-users
    without a system Python are still covered.

    No-op on non-Windows (dev mode) — callers can pass an `opener=` for
    tests via `winapp.autostart.install`. Production always goes through
    this helper from the wizard.
    """
    if os.name != "nt":
        return
    import winreg  # type: ignore[import-not-found]

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, TRAY_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, TRAY_RUN_VALUE, 0, winreg.REG_SZ, f'"{executable}"')


def launch_tray(executable: Path) -> None:
    """Spawn the tray once so the icon shows up immediately after install
    instead of waiting for the next logon."""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    subprocess.Popen(
        [str(executable)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _run_install_steps(
    creds: dict, *, on_step=None, install_tray: bool = True
) -> Path:
    """Pure install logic — no Tk. Runs the steps the GUI's Install button
    does:

      1. copy this .exe to %LOCALAPPDATA%\\AgentFlow\\agentflow-desktop.exe
      2. write %USERPROFILE%\\.agentflow\\auth.json
      3. schtasks /Create /TN AgentFlowDesktop /XML
      4. spawn the daemon
      5. (if install_tray) copy agentflow-tray.exe + HKCU Run key + spawn

    Returns the path to the installed daemon .exe so callers (CI / future
    `--silent --invite=...` CLI) can verify the schtasks XML.

    `on_step` is an optional callable that receives short progress strings
    so the GUI can pipe them into its log view.

    `install_tray=False` leaves the wizard backwards-compatible with the
    v0.4.x flow (daemon only) — useful for users who unchecked the tray
    checkbox.
    """

    def _emit(msg: str) -> None:
        if on_step is not None:
            try:
                on_step(msg)
            except Exception:
                pass

    _emit("install_daemon_binary")
    target = install_daemon_binary()
    _emit(f"  → {target}")

    _emit("write_auth_file")
    auth_path = write_auth_file(creds)
    _emit(f"  → {auth_path}")

    _emit(f"register_scheduled_task ({TASK_NAME})")
    ok, detail = register_scheduled_task(target)
    if ok:
        _emit("  → task created")
    else:
        _emit(f"  → schtasks отказал: {detail}")
        _emit("  → fallback: HKCU\\…\\Run\\AgentFlowDaemon")
        try:
            register_daemon_run_key(target)
            _emit("  → Run-key установлен")
        except Exception as exc:  # noqa: BLE001
            _emit(f"  → fallback тоже не сработал: {exc}")

    _emit("launch_daemon")
    launch_daemon(target)
    _emit("  → spawned")

    if install_tray:
        _emit("install_tray_binary")
        tray_target = install_tray_binary()
        if tray_target is None:
            _emit("  → пропуск: agentflow-tray.exe не найден рядом с инсталлятором")
        else:
            _emit(f"  → {tray_target}")
            _emit("register_tray_autostart (HKCU\\…\\Run\\AgentFlowTray)")
            try:
                register_tray_autostart(tray_target)
                _emit("  → Run-key установлен")
            except Exception as exc:  # noqa: BLE001
                _emit(f"  → ошибка Run-key: {exc}")
            _emit("launch_tray")
            try:
                launch_tray(tray_target)
                _emit("  → иконка в трее запущена")
            except Exception as exc:  # noqa: BLE001
                _emit(f"  → ошибка запуска трея: {exc}")

    return target


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
            text="Вставь токен установки из кабинета AgentFlow",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x")

        tk.Label(
            wrap,
            text="Один токен — одно поле. Кабинет выдаёт af_install_… или af_live_…",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        invite_row = tk.Frame(wrap, bg=BG)
        invite_row.pack(fill="x", pady=(0, 6))

        self.invite = tk.Text(
            invite_row,
            height=2,
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
        ).pack(side="left", padx=(6, 0), ipady=12)

        # Advanced параметры скрыты по умолчанию: 9 из 10 пользователей
        # вставляют один токен и больше ничего трогать не должны. Power-
        # users разворачивают блок, чтобы ввести три старых поля вручную.
        self.advanced_toggle = tk.Label(
            wrap,
            text="▸ Расширенные параметры",
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

        # «Запускать иконку в трее при старте Windows» — default ON, mirrors
        # the Mac DMG behaviour where AgentFlow.app shows up in the menu bar
        # after install AND on every login. Off → wizard skips the Run-key
        # write + immediate tray spawn (daemon-only mode, v0.4.x parity).
        self.tray_autostart_var = tk.BooleanVar(value=True)
        tray_row = tk.Frame(wrap, bg=BG)
        tray_row.pack(fill="x", pady=(2, 0))
        tk.Checkbutton(
            tray_row,
            text="Запускать иконку в трее при старте Windows",
            variable=self.tray_autostart_var,
            bg=BG,
            fg=FG,
            selectcolor=FIELD_BG,
            activebackground=BG,
            activeforeground=FG,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(side="left")

        # The bundle ships Python + agentflow_computer_mcp inside the
        # .exe, so install is now just file IO + schtasks — should
        # complete in ~5 seconds. The progressbar still spins so the
        # user sees motion.
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

        # «Получить обновление» — calls auto_updater.check_now() synchronously
        # and writes the result into the log view. Placed under the install
        # card so it's visible before AND after install.
        self.update_link = tk.Label(
            wrap,
            text="Получить обновление",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9, "underline"),
            cursor="hand2",
            anchor="w",
        )
        self.update_link.pack(fill="x", pady=(2, 0))
        self.update_link.bind("<Button-1>", lambda _e: self._on_check_update())

    def _toggle_advanced(self) -> None:
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.advanced_toggle.configure(text="▾ Расширенные параметры")
            self.advanced_frame.pack(fill="x", pady=(4, 0), after=self.advanced_toggle)
            self.root.geometry("520x540")
        else:
            self.advanced_toggle.configure(text="▸ Расширенные параметры")
            self.advanced_frame.pack_forget()
            self.root.geometry("520x440")

    def _collect_creds(self) -> dict:
        blob = self.invite.get("1.0", "end").strip()
        if blob:
            return parse_token(blob)
        manual = {k: e.get().strip() for k, e in self.fields.items()}
        if not all(manual.values()):
            raise ValueError(
                "Вставь токен установки или открой «Расширенные параметры» и заполни три поля."
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
            self.log_queue.put("Шаг 1/4: проверяю invite-код")
            assert self.creds is not None
            self.log_queue.put(f"  → device_id={self.creds['device_id'][:18]}…")
            self.device_id = self.creds["device_id"]

            step_labels = {
                "install_daemon_binary": "Шаг 2/5: копирую бинарь в %LOCALAPPDATA%",
                "write_auth_file": "Шаг 3/5: сохраняю учётные данные",
                f"register_scheduled_task ({TASK_NAME})": (
                    f"Шаг 4/5: регистрирую автозапуск ({TASK_NAME})"
                ),
                "launch_daemon": "  → задача создана, запускаю демон",
                "install_tray_binary": "Шаг 5/5: устанавливаю иконку в трее",
                "register_tray_autostart (HKCU\\…\\Run\\AgentFlowTray)": (
                    "  → прописываю автозапуск трея"
                ),
                "launch_tray": "  → запускаю иконку в трее",
            }

            def _bridge(msg: str) -> None:
                self.log_queue.put(step_labels.get(msg, msg))

            _run_install_steps(
                self.creds,
                on_step=_bridge,
                install_tray=bool(self.tray_autostart_var.get()),
            )

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

    def _on_check_update(self) -> None:
        """Hit GitHub releases, verify sha256, swap binary, restart daemon.

        In a PyInstaller-frozen build this runs the full download → verify →
        in-place replace pipeline shipped by `auto_updater.check_now()`. The
        running .exe lock is handled by the platform-specific apply step
        (`update.bat` on Windows, `os.execv` on Unix).

        From source (dev mode) GitHub releases don't ship the wheel so we
        delegate to `_dev_update()` which does git pull + pip install + a
        best-effort daemon restart through `schtasks` (Windows) or `pkill`
        (Unix). Either path streams progress into the log so the user sees
        motion.
        """
        try:
            from agentflow_computer_mcp import __version__ as local_version
            from agentflow_computer_mcp.auto_updater import _is_frozen, check_now
        except Exception as exc:  # noqa: BLE001
            self._log(f"Не получилось загрузить модуль обновления: {exc}")
            return

        if not _is_frozen():
            self._dev_update(local_version)
            return

        self._log("Проверяю обновление…")
        try:
            result = check_now(allow_unfrozen=False)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Ошибка проверки: {exc}")
            return

        status = result.get("status", "")
        latest = result.get("latest") or ""
        if status == "current":
            self._log(f"Версия {local_version} актуальна")
        elif status == "available":
            self._log(f"Доступна версия {latest}, скачиваю…")
        elif status == "applied":
            self._log(f"Обновлено до {latest}. Перезапусти приложение.")
        elif status == "skipped":
            self._log(f"Пропущено: {result.get('reason', '')}")
        else:
            self._log(f"Не получилось: {result.get('reason', '')}")

    def _dev_update(self, local_version: str) -> None:
        """Source-mode counterpart of `check_now()`: `git pull` in the
        repo root, `pip install -e .` to refresh deps, then nudge the
        running daemon. Each subprocess result lands in the log so a
        broken `git pull` is visible instead of swallowed.
        """
        repo_root = Path(__file__).resolve().parent.parent
        self._log(f"Источник: {repo_root}")
        steps: list[tuple[str, list[str]]] = [
            ("git pull", ["git", "pull", "--ff-only"]),
            ("pip install -e .", [sys.executable, "-m", "pip", "install", "-e", "."]),
        ]
        for label, cmd in steps:
            self._log(f"→ {label}")
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"  ошибка: {exc}")
                return
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-2:]
            for line in tail:
                self._log(f"  {line[:160]}")
            if proc.returncode != 0:
                self._log(f"  fail ({proc.returncode})")
                return
        # Best-effort daemon restart. We deliberately don't fail the
        # whole update on a missing daemon — the user can launch it from
        # the cabinet button afterwards.
        self._log("→ перезапуск демона")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["schtasks", "/End", "/TN", TASK_NAME],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                subprocess.run(
                    ["schtasks", "/Run", "/TN", TASK_NAME],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                subprocess.run(
                    ["pkill", "-f", "agentflow-desktop"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
        except Exception as exc:  # noqa: BLE001
            self._log(f"  предупреждение: не получилось перезапустить ({exc})")
        self._log(f"Готово. Версия {local_version} обновлена из исходников.")

    def run(self) -> None:
        self.root.mainloop()


def _run_daemon_mode() -> int:
    """Boot the agent daemon directly. CI calls this with `--selftest`
    so the bundled PyInstaller binary is validated before release."""
    # Drop --daemon from argv so the daemon's own argparse doesn't choke.
    argv = [arg for arg in sys.argv[1:] if arg != "--daemon"]
    if "--selftest" in argv:
        # Replace --selftest with the daemon's `selftest` subcommand,
        # which prints an OS-agnostic backend grid and exits 0.
        argv = [arg for arg in argv if arg != "--selftest"]
        argv.insert(0, "selftest")
    elif not argv:
        # Plain `--daemon` → `run` (full daemon, port 8765).
        argv = ["run"]
    sys.argv = [sys.argv[0], *argv]
    from agentflow_computer_mcp.desktop_cli import main as daemon_main
    return int(daemon_main() or 0)


def main() -> int:
    if "--daemon" in sys.argv:
        return _run_daemon_mode()
    SetupWindow().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
