"""Windows-specific behavior tests that run on a non-Windows host.

We can't boot a real Windows VM in CI, but every OS-branching code path in
the daemon is gated by either ``platform.system()`` or ``sys.platform``,
so a careful set of monkeypatches lets us exercise the Windows branches
from macOS/Linux. The point of this file is to lock the contract: the
LLM-facing system prompt must include PowerShell guidance on Windows,
hard-deny paths must cover Windows secrets, intent routing must not push
``Terminal.app`` at a Windows host, and clipboard / capture / chrome
helpers must avoid AppleScript when the host is Windows.

Where we cannot verify behavior end-to-end without booting Windows (UI
clicks via win32api, registry writes, schtasks registration), the test
is marked ``xfail(strict=False)`` so a real Windows run can flip it to
pass without churning the test list.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow_computer_mcp.config import HARD_DENY_PATHS, Scope
from agentflow_computer_mcp.driver import loop as loop_mod
from agentflow_computer_mcp.scope import ScopeDenied, check_path, check_shell


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_prompt_under_os(host_os: str, current_os_str: str) -> str:
    """Build the system prompt as if the daemon booted on ``host_os``.

    ``build_system_prompt`` reads two module-level captures:

    * ``HOST_OS`` — set from ``platform.system()`` at import time.
    * ``_current_os()`` — runs ``sys.platform.startswith(...)`` at call time.

    Patching both gives a deterministic prompt without re-importing the
    module (re-import would also reset MAX_ITERS / env caps).
    """
    with patch.object(loop_mod, "HOST_OS", host_os), \
         patch.object(loop_mod, "HOST_OS_RELEASE", "test-release"), \
         patch.object(loop_mod, "_current_os", lambda: current_os_str):
        return loop_mod.build_system_prompt(
            window_summary="  • (none)\n",
            af_tools_present=False,
        )


# ---------------------------------------------------------------------------
# System prompt OS context
# ---------------------------------------------------------------------------


def test_system_prompt_includes_powershell_on_windows() -> None:
    prompt = _build_prompt_under_os("Windows", "windows")
    # Tool surface advertised on Windows
    assert "powershell_exec" in prompt
    assert "winget_search" in prompt
    assert "winget_install" in prompt
    # Hard guidance that AppleScript is unavailable
    assert "AppleScript" in prompt
    lower = prompt.lower()
    assert "osascript" in lower
    assert "недоступн" in lower  # Russian "unavailable" — the prompt is in ru-RU
    # OS label header
    assert "Windows" in prompt
    # Intent map should pin Windows Terminal / PowerShell, not macOS apps
    assert "WindowsTerminal" in prompt or "Windows Terminal" in prompt


def test_system_prompt_macos_no_powershell_pinning() -> None:
    prompt = _build_prompt_under_os("Darwin", "macos")
    # On macOS the prompt explicitly tells the model NOT to call PowerShell tools
    assert "AppleScript" in prompt
    assert "macOS" in prompt or "Mac" in prompt
    # The "do not call" sentence is the only place PowerShell appears on Mac
    # — confirm via the explicit guard phrase.
    assert "НЕ зови powershell_exec" in prompt
    # Mac intent block should mention native apps
    assert "iTerm2" in prompt or "Terminal" in prompt


def test_system_prompt_linux_no_powershell_no_applescript_intent() -> None:
    prompt = _build_prompt_under_os("Linux", "linux")
    assert "Linux" in prompt
    # Linux block must mention bash + xdg-open + at least one X11/Wayland tool
    assert "bash" in prompt
    assert "xdg-open" in prompt
    # Linux intent block must not push the user toward Mail.app or Outlook desktop
    # as the primary option — Thunderbird or Gmail web is the documented path.
    assert "Thunderbird" in prompt or "mail.google.com" in prompt
    # The Linux guard sentence pins PowerShell / AppleScript / winget as missing.
    assert "AppleScript / PowerShell / winget недоступны" in prompt


def test_system_prompt_uses_current_os_for_intent_block() -> None:
    """OS routing inside intent_map must pick the matching block per host."""
    mac_prompt = _build_prompt_under_os("Darwin", "macos")
    win_prompt = _build_prompt_under_os("Windows", "windows")
    lin_prompt = _build_prompt_under_os("Linux", "linux")

    # macOS intent block uniquely mentions Cmd+Space (Spotlight)
    assert "Cmd+Space" in mac_prompt
    assert "Cmd+Space" not in win_prompt
    assert "Cmd+Space" not in lin_prompt

    # Windows intent block uniquely mentions Win+R
    assert "Win+R" in win_prompt
    assert "Win+R" not in mac_prompt
    assert "Win+R" not in lin_prompt

    # Linux intent block uniquely mentions gnome-terminal
    assert "gnome-terminal" in lin_prompt
    assert "gnome-terminal" not in mac_prompt
    assert "gnome-terminal" not in win_prompt


# ---------------------------------------------------------------------------
# Intent map: «открой Terminal» per OS
# ---------------------------------------------------------------------------


def test_intent_map_windows_terminal_app() -> None:
    prompt = _build_prompt_under_os("Windows", "windows")
    # Windows users open "Windows Terminal" (wt.exe alias) or fall back to
    # powershell/cmd. The model must NOT see macOS or Linux terminal apps
    # as the suggested target on Windows.
    assert "WindowsTerminal" in prompt
    assert "powershell" in prompt.lower()
    assert "Terminal.app" not in prompt
    assert "iTerm2" not in prompt
    assert "gnome-terminal" not in prompt


def test_intent_map_macos_terminal_app() -> None:
    prompt = _build_prompt_under_os("Darwin", "macos")
    assert "iTerm2" in prompt or "Terminal" in prompt
    assert "WindowsTerminal" not in prompt
    assert "gnome-terminal" not in prompt


def test_intent_map_linux_terminal_app() -> None:
    prompt = _build_prompt_under_os("Linux", "linux")
    assert "gnome-terminal" in prompt
    assert "WindowsTerminal" not in prompt
    # macOS-specific terminal app must not be the recommended Linux target
    assert "iTerm2" not in prompt


# ---------------------------------------------------------------------------
# Path validation / scope on Windows-style paths
# ---------------------------------------------------------------------------


def test_path_validation_windows_backslash_via_posix_allow(tmp_path: Path) -> None:
    """A Windows-style path under an allowed directory should resolve.

    On a POSIX host, the resolve() of ``C:\\Users\\test\\foo.txt`` produces
    a path treated as a single segment (no drive letter on POSIX). The
    contract we lock here is: ``check_path`` must not raise on a string
    that LOOKS like a Windows path as long as the resolved target sits
    under an ``allow_paths`` entry.
    """
    scope = Scope(allow_paths=(str(tmp_path),))
    # Synthesize a file the resolver will accept.
    target_file = tmp_path / "foo.txt"
    target_file.write_text("ok")
    # Direct POSIX form — the canonical case.
    result = check_path(str(target_file), scope, write=False)
    assert result == target_file.resolve()


@pytest.mark.xfail(
    reason="Windows backslash + drive-letter path expansion needs a real Windows host: "
    "PurePath on POSIX treats C:\\Users\\test as a single segment.",
    strict=False,
)
def test_path_validation_windows_backslash_drive_letter() -> None:
    """On real Windows, `C:\\Users\\test\\foo.txt` should resolve to the
    NT path and pass scope checks when `C:/Users/test` is in allow_paths.
    We xfail on POSIX because Path("C:\\Users\\test\\foo.txt").resolve()
    here yields ``$CWD/C:\\Users\\test\\foo.txt`` which is meaningless."""
    scope = Scope(allow_paths=("C:/Users/test",))
    result = check_path("C:\\Users\\test\\foo.txt", scope, write=False)
    assert "test" in str(result).replace("\\", "/")


@pytest.mark.xfail(
    reason="POSIX Path() treats backslash as a literal filename char, so on macOS "
    "a backslash-style path like ~/.ssh\\id_rsa resolves to a single-segment file "
    "that is NOT inside ~/.ssh. On real Windows, both separators normalise to the "
    "NT separator and the deny check fires. Verify on Windows.",
    strict=False,
)
def test_path_deny_hard_paths_still_block_on_windows_style_input() -> None:
    """``~/.ssh/id_rsa`` must always deny, regardless of the slash style."""
    scope = Scope(allow_paths=(str(Path.home()),))
    child = str(Path.home() / ".ssh" / "id_rsa").replace("/", "\\")
    with pytest.raises(ScopeDenied):
        check_path(child, scope)


def test_path_deny_hard_paths_blocks_forward_slash_ssh() -> None:
    """Cross-platform sanity: a forward-slash ``~/.ssh/id_rsa`` must deny on
    every host. Locks the existing invariant against accidental regression
    when we add Windows-only deny entries."""
    scope = Scope(allow_paths=(str(Path.home()),))
    target = str(Path.home() / ".ssh" / "id_rsa")
    with pytest.raises(ScopeDenied) as excinfo:
        check_path(target, scope)
    assert "hard-coded fallback" in str(excinfo.value)


@pytest.mark.xfail(
    reason="HARD_DENY_PATHS today is POSIX-only (~/.ssh, ~/.config). On Windows we "
    "should also block C:\\Windows\\System32\\config\\SAM and %APPDATA%\\Microsoft\\"
    "Credentials. Today the deny fires only because nothing matches allow_paths — "
    "we want a deny with reason 'hard-coded fallback'. Flip xfail→pass once "
    "config.py adds Windows entries.",
    strict=False,
)
def test_path_deny_hardcoded_windows_secrets() -> None:
    """Lock the expectation that Windows secret stores are hard-denied for the
    right reason (hard fallback), not as a side effect of allow_paths missing them."""
    sam = "C:\\Windows\\System32\\config\\SAM"
    # Allow everything UNDER the same notional root so the only path to a
    # deny is the hard-deny table — proves the hard table catches it.
    scope = Scope(allow_paths=(str(Path("/")),))
    with pytest.raises(ScopeDenied) as excinfo:
        check_path(sam, scope)
    assert "hard-coded fallback" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Shell whitelist semantics on Windows
# ---------------------------------------------------------------------------


def test_shell_whitelist_empty_blocks_powershell_on_windows() -> None:
    """A Scope with no shell_whitelist must deny ``powershell …`` too — the
    deny path must not silently allow PowerShell because we're "on Windows"."""
    scope = Scope(shell_whitelist=())
    with pytest.raises(ScopeDenied):
        check_shell("powershell -Command Get-Process", scope)


def test_shell_whitelist_allows_powershell_when_listed() -> None:
    scope = Scope(shell_whitelist=("powershell", "cmd"))
    assert check_shell("powershell -Command Get-Process", scope).startswith("powershell")
    assert check_shell("cmd /c dir", scope).startswith("cmd")
    with pytest.raises(ScopeDenied):
        check_shell("rm -rf /", scope)


@pytest.mark.xfail(
    reason="Default Scope() ships with an empty shell_whitelist, so on Windows the "
    "model can't call powershell_exec until the user edits computer-scope.toml. "
    "Decision deferred: do we ship a Windows default of ('powershell',) or keep "
    "shell.exec opt-in across all OSes?",
    strict=False,
)
def test_default_scope_windows_includes_powershell() -> None:
    scope = Scope()
    assert "powershell" in scope.shell_whitelist


# ---------------------------------------------------------------------------
# code_run_command shell selection
# ---------------------------------------------------------------------------


def test_code_run_command_uses_asyncio_subprocess_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """``code_run_command`` delegates to ``asyncio.create_subprocess_shell``,
    which on Windows hands the command to %COMSPEC% (cmd.exe). We can't
    swap interpreters from inside the helper, so the contract is: it
    invokes ``create_subprocess_shell`` and lets the OS pick the shell.

    Test: mock the shell call, run an allow-listed command, assert the
    helper called ``create_subprocess_shell`` exactly once with our
    command string (no manual bash/zsh prefix added).
    """
    import asyncio

    from agentflow_computer_mcp.tools import code as code_tools

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok\n", b""

        def kill(self) -> None:  # pragma: no cover - timeout path unused here
            pass

        async def wait(self) -> int:  # pragma: no cover
            return 0

    async def _fake_create(command: str, **kwargs: object) -> _FakeProc:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create)

    scope = Scope(
        allow_paths=(str(Path.home()),),
        shell_whitelist=("echo",),
    )
    result = asyncio.run(code_tools.run_command("echo hi", scope, cwd=str(Path.home())))
    assert result["exit_code"] == 0
    assert captured["command"] == "echo hi"
    # No manual `bash -c` / `powershell -Command` wrapping — the shell
    # selection is delegated to the OS so it stays platform-correct.
    assert "powershell" not in str(captured["command"])
    assert "bash -c" not in str(captured["command"])


def test_powershell_exec_refuses_on_non_windows() -> None:
    """``powershell_exec`` must hard-fail with a structured error on Mac/Linux
    rather than spawn `powershell` and 127-error out."""
    from agentflow_computer_mcp.driver import desktop_tools

    with patch.object(desktop_tools, "PLATFORM", "mac"):
        out = desktop_tools.powershell_exec("Get-Process", timeout=5)
    assert out["ok"] is False
    assert "mac_only_or_linux_only" in out["error"]
    assert "Windows" in out["detail"]


def test_winget_search_refuses_on_non_windows() -> None:
    from agentflow_computer_mcp.driver import desktop_tools

    with patch.object(desktop_tools, "PLATFORM", "linux"):
        out = desktop_tools.winget_search("vscode")
    assert out["ok"] is False
    assert out["error"] == "windows_only"


def test_winget_install_refuses_on_non_windows() -> None:
    from agentflow_computer_mcp.driver import desktop_tools

    with patch.object(desktop_tools, "PLATFORM", "mac"):
        out = desktop_tools.winget_install("Git.Git")
    assert out["ok"] is False
    assert out["error"] == "windows_only"


# ---------------------------------------------------------------------------
# Chrome eval / open behavior across OSes
# ---------------------------------------------------------------------------


def test_chrome_eval_refuses_applescript_on_windows() -> None:
    """``chrome_run_js`` (chrome_eval tool) is AppleScript-only. On Windows
    it must return an error that points the model at headed Chromium
    instead — never spawn osascript on a host where it doesn't exist."""
    from agentflow_computer_mcp.driver import desktop_tools

    with patch.object(desktop_tools, "PLATFORM", "windows"):
        out = desktop_tools.chrome_run_js("1+1")
    assert out.lower().startswith("error:")
    assert "applescript" in out.lower()
    assert "browser_eval" in out  # the documented fallback


def test_chrome_open_url_windows_uses_cmd_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, chrome_open_url must shell out to ``cmd /c start "" <url>`` —
    never osascript, never xdg-open."""
    from agentflow_computer_mcp.driver import desktop_tools

    captured: dict[str, object] = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _R:
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(desktop_tools, "PLATFORM", "windows")
    monkeypatch.setattr(desktop_tools.subprocess, "run", _fake_run)
    out = desktop_tools.chrome_open_url("https://example.com")
    assert "opened" in out
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "cmd"
    assert cmd[1] == "/c"
    assert cmd[2] == "start"
    assert "osascript" not in cmd
    assert "xdg-open" not in cmd


def test_chrome_list_tabs_refuses_on_windows() -> None:
    from agentflow_computer_mcp.driver import desktop_tools

    with patch.object(desktop_tools, "PLATFORM", "windows"):
        out = desktop_tools.chrome_list_tabs()
    assert "error" in out.lower()
    assert "applescript" in out.lower()


# ---------------------------------------------------------------------------
# OS-tool filtering: which tool descriptors reach the LLM
# ---------------------------------------------------------------------------


def test_filter_tools_drops_mac_tools_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentflow_computer_mcp.driver import desktop_tools

    monkeypatch.setattr(desktop_tools.platform, "system", lambda: "Windows")
    filtered = desktop_tools._filter_tools_by_os(desktop_tools.DESKTOP_TOOLS)
    names = {t["name"] for t in filtered}
    # Mac-only tools must be stripped on a Windows host
    assert "chrome_eval" not in names
    assert "chrome_tabs" not in names
    # Windows-only tools must survive
    assert "powershell_exec" in names
    assert "winget_search" in names
    assert "winget_install" in names


def test_filter_tools_drops_windows_tools_on_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentflow_computer_mcp.driver import desktop_tools

    monkeypatch.setattr(desktop_tools.platform, "system", lambda: "Darwin")
    filtered = desktop_tools._filter_tools_by_os(desktop_tools.DESKTOP_TOOLS)
    names = {t["name"] for t in filtered}
    assert "powershell_exec" not in names
    assert "winget_search" not in names
    assert "winget_install" not in names
    assert "chrome_eval" in names
    assert "chrome_tabs" in names


def test_filter_tools_drops_mac_tools_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentflow_computer_mcp.driver import desktop_tools

    monkeypatch.setattr(desktop_tools.platform, "system", lambda: "Linux")
    filtered = desktop_tools._filter_tools_by_os(desktop_tools.DESKTOP_TOOLS)
    names = {t["name"] for t in filtered}
    # Linux is treated like "not Darwin" for the Mac-only filter — Linux
    # shouldn't see chrome_eval either (AppleScript path).
    assert "chrome_eval" not in names
    # Windows-only tools today are kept visible on Linux because the
    # current filter is binary (Darwin vs non-Darwin). The tools refuse
    # at runtime — verified in test_winget_search_refuses_on_non_windows.
    # Lock the current behavior so any future tightening shows up as a
    # test failure (so we can decide whether to update the test or revert).
    assert "powershell_exec" in names


# ---------------------------------------------------------------------------
# Backend / clipboard / screen capture on a Windows host
# ---------------------------------------------------------------------------


def test_platform_module_selects_windows_backend_when_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-import the platform package with ``sys.platform`` patched to ``win32``
    and confirm the windows backend module is the one wired into ``backend``."""
    import agentflow_computer_mcp.platform as plat

    monkeypatch.setattr(sys, "platform", "win32")
    # Make sure the windows backend module is importable on this host so
    # the re-detection doesn't blow up on a missing dep. The backend
    # itself imports Pillow lazily; we only need the module object.
    if "agentflow_computer_mcp.platform.windows" not in sys.modules:
        try:
            importlib.import_module("agentflow_computer_mcp.platform.windows")
        except Exception:  # noqa: BLE001 — fine on Mac, we still proceed
            pytest.skip("windows backend module fails to import on this host")

    detected_name, detected_backend = plat._detect()
    assert detected_name == "windows"
    assert detected_backend is not None
    assert detected_backend.name == "windows"


def test_windows_backend_clipboard_uses_pyperclip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows backend's clipboard_write / read must go through
    ``pyperclip``, not ``pbcopy``/``pbpaste`` (mac) or ``xclip`` (linux)."""
    pytest.importorskip("PIL")
    from agentflow_computer_mcp.platform import windows as win_mod

    captured: dict[str, str] = {}

    class _FakePyperclip:
        @staticmethod
        def copy(text: str) -> None:
            captured["copied"] = text

        @staticmethod
        def paste() -> str:
            return captured.get("copied", "")

    monkeypatch.setitem(sys.modules, "pyperclip", _FakePyperclip)

    backend = win_mod.WindowsBackend()
    backend.clipboard_write("hello-windows")
    assert captured["copied"] == "hello-windows"
    assert backend.clipboard_read() == "hello-windows"


def test_windows_backend_read_terminal_invokes_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    """``read_terminal`` on Windows shells out to PowerShell's Get-History,
    not AppleScript / iTerm. We mock subprocess.run and assert the command
    string starts with ``powershell``."""
    pytest.importorskip("PIL")
    from agentflow_computer_mcp.platform import windows as win_mod

    class _R:
        returncode = 0
        stdout = "history-line-1\nhistory-line-2\n"
        stderr = ""

    captured: dict[str, object] = {}

    def _fake_run(cmd: object, **kwargs: object) -> _R:
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        return _R()

    monkeypatch.setattr(win_mod.subprocess, "run", _fake_run)
    backend = win_mod.WindowsBackend()
    out = backend.read_terminal()
    assert "history-line" in out
    # Powershell + Get-History command must be present
    cmd = captured["cmd"]
    assert isinstance(cmd, str)
    assert cmd.lower().startswith("powershell")
    assert "Get-History" in cmd
    assert "osascript" not in cmd
    assert "iTerm" not in cmd


def test_windows_backend_capture_uses_mss_not_quartz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Screen capture on Windows goes through mss → PIL.Image, not Quartz."""
    pytest.importorskip("PIL")
    from agentflow_computer_mcp.platform import windows as win_mod

    from PIL import Image

    fake_img = Image.new("RGB", (640, 400), color=(10, 20, 30))

    def _fake_mss(region: object = None) -> Image.Image:  # noqa: ARG001
        return fake_img

    monkeypatch.setattr(win_mod, "_mss_capture", _fake_mss)
    backend = win_mod.WindowsBackend()
    png = backend.capture_screen()
    assert isinstance(png, bytes)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# _current_os branch coverage
# ---------------------------------------------------------------------------


def test_current_os_returns_windows_when_sys_platform_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert loop_mod._current_os() == "windows"


def test_current_os_returns_macos_when_sys_platform_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert loop_mod._current_os() == "macos"


def test_current_os_returns_linux_when_sys_platform_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert loop_mod._current_os() == "linux"


def test_current_os_unknown_falls_back_to_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback path matters: a chromebook-ish 'cygwin'/'aix' host must
    not crash the prompt builder."""
    monkeypatch.setattr(sys, "platform", "freebsd13")
    assert loop_mod._current_os() == "linux"
