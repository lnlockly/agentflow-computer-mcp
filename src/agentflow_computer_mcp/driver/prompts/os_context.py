"""OS-specific intent fragments and host-context block.

Split out of ``loop.py`` so the desktop / terminal / cabinet blocks
don't all share one 1500-line file.
"""
from __future__ import annotations

import platform
import sys

HOST_OS = platform.system()
HOST_OS_RELEASE = platform.release()


def current_os() -> str:
    """One of 'macos' | 'linux' | 'windows'."""
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


OS_INTENT_BLOCK = {
    "macos": (
        "  • «открой Mail / проверь почту» → activate_app('Mail') → wait 0.5 → screen_region.\n"
        "  • «напиши email на X тему Y» → activate_app('Mail') → keypress Cmd+N → "
        "Tab-driven type To/Subject/body. НЕ нажимай Send без явного «отправляй».\n"
        "  • «открой Terminal / iTerm» → activate_app('iTerm2') или activate_app('Terminal'); "
        "wait 0.4 → read_terminal.\n"
        "  • shell-shortcuts: Cmd+C / Cmd+V / Cmd+Space (Spotlight).\n"
    ),
    "linux": (
        "  • «открой почту / проверь mail» → если Thunderbird установлен — activate_app('Thunderbird'), "
        "иначе browser_open + browser_navigate https://mail.google.com (Gmail web).\n"
        "  • «напиши email на X тему Y» — на Linux браузерный Gmail надёжнее десктопного клиента: "
        "browser_open + navigate https://mail.google.com → browser_click 'Compose' → "
        "fill To/Subject/body. НЕ нажимай Send без явного «отправляй».\n"
        "  • «открой Terminal» → activate_app('gnome-terminal') / 'konsole' / 'xterm' "
        "(пробуй в этом порядке).\n"
        "  • shell-shortcuts: Ctrl+C / Ctrl+V; нет Spotlight — используй активацию окна.\n"
    ),
    "windows": (
        "  • «открой почту / проверь mail» → если Outlook установлен — activate_app('Outlook'), "
        "иначе browser_open + browser_navigate https://outlook.live.com или https://mail.google.com.\n"
        "  • «напиши email на X тему Y» — браузерный путь обычно надёжнее десктопного: "
        "browser_open + соответствующий navigate, потом fill полей. НЕ нажимай Send без явного «отправляй».\n"
        "  • «открой Terminal» → activate_app('WindowsTerminal') (Windows Terminal), "
        "fallback 'powershell' или 'cmd'.\n"
        "  • shell-shortcuts: Ctrl+C / Ctrl+V; Win+R = Run dialog (аналог Spotlight).\n"
    ),
}


def os_context_block() -> str:
    base = (
        f"\nОС хоста: {HOST_OS} ({HOST_OS_RELEASE})\n"
        "Доступные инструменты — только те, что работают на этой ОС:\n"
        "  • macOS:   AppleScript (osascript), Quartz screen capture, pbcopy/pbpaste, "
        "chrome_open_url / chrome_eval / chrome_tabs (через AppleScript), read_terminal "
        "(iTerm/Terminal через AppleScript), `open -a <App>`.\n"
        "  • Windows: PowerShell (`powershell -Command \"...\"`) через powershell_exec, "
        "winget_search / winget_install для пакетов, pywin32 windows через activate_app, "
        "pyperclip для буфера. Chrome — только через chrome_open_url + chrome_eval "
        "(headed Chromium / Firefox), НЕ AppleScript.\n"
        "  • Linux:   bash, xdotool / wmctrl (X11) либо wl-tools (Wayland), xclip / wl-copy "
        "для буфера, `xdg-open <url>` для дефолтного браузера.\n"
    )
    if HOST_OS == "Darwin":
        return base + (
            "\nТы на macOS: AppleScript-инструменты разрешены. НЕ зови powershell_exec / winget_*.\n"
        )
    if HOST_OS == "Windows":
        return base + (
            "\nТы на Windows: osascript / AppleScript / pbcopy / `open -a` НЕДОСТУПНЫ. "
            "Для shell — powershell_exec. Для запуска приложений — start_app(name). "
            "Для установки софта — winget_search / winget_install. Chrome через chrome_open_url "
            "+ chrome_eval (headed). read_terminal вернёт PowerShell history, а не iTerm.\n"
        )
    return base + (
        "\nТы на Linux: AppleScript / PowerShell / winget недоступны. Используй bash через "
        "code_run_command, xdg-open для браузера, activate_app для X11/Wayland окон.\n"
    )


OS_LABEL = {"macos": "Mac", "linux": "Linux", "windows": "Windows"}
