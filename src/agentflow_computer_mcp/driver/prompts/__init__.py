"""System-prompt assembly for the driver loop.

Historically all of these blocks lived inside ``driver/loop.py`` as a
single 300+ line function. They are now split per concern (cabinet,
element, terminal, etc.) so each block can be edited and reviewed in
isolation.

Public surface:
  - ``build_system_prompt(window_summary, af_tools_present)``
  - ``HOST_OS`` / ``HOST_OS_RELEASE`` — for log lines elsewhere
"""
from __future__ import annotations

from .af_tools import AF_TOOLS, MEMORY
from .browser import BROWSER_EFFICIENCY, TASK_EFFICIENCY
from .cabinet import CABINET_MAP
from .coding import CODING_WORKFLOW
from .element import ELEMENT_BLOCK
from .intent import intent_map
from .knowledge import KNOWLEDGE, PITFALLS, VISIBILITY
from .os_context import (
    HOST_OS,
    HOST_OS_RELEASE,
    OS_LABEL,
    current_os,
    os_context_block,
)
from .terminal import TERMINAL_PLAYBOOK


def build_system_prompt(window_summary: str, af_tools_present: bool) -> str:
    af = AF_TOOLS if af_tools_present else ""
    mem = MEMORY if af_tools_present else ""
    os_label = OS_LABEL[current_os()]

    return (
        f"Ты управляешь {os_label} пользователя. Перед действием — короткая мысль в text-блоке. "
        "Не извиняйся, не повторяй очевидное. Стратегия:\n"
        "  • для содержимого окон: activate_app → wait 0.5 → screen_region(bounds) — быстро и детально.\n"
        "  • для содержимого терминала: read_terminal даёт точный текст активной вкладки.\n"
        "  • для веб-задач (открыть сайт, прочитать DOM, нажать кнопку): browser_open → browser_navigate → "
        "browser_snapshot → browser_click/browser_fill/browser_press/browser_eval. Это headed Chromium, "
        "ОТДЕЛЬНЫЙ от пользовательского браузера. Видим в live viewer.\n"
        "  • для авторизованного веба (где у юзера уже залогинено): chrome_eval / chrome_open_url — реальный "
        "Google Chrome с его сессией.\n"
        f"{os_context_block()}"
        f"{KNOWLEDGE}"
        f"{PITFALLS}"
        f"{af}"
        f"{intent_map()}"
        f"{BROWSER_EFFICIENCY}"
        f"{mem}"
        f"{CODING_WORKFLOW}"
        f"{TERMINAL_PLAYBOOK}"
        f"{CABINET_MAP}"
        f"{ELEMENT_BLOCK}"
        f"{TASK_EFFICIENCY}"
        f"{VISIBILITY}"
        "Scope hard rules: paths `~/.ssh`, `~/.config`, `~/Library/Keychains`, `~/.aws`, `~/.gnupg` всегда запрещены "
        "к чтению/записи. fs.write и shell.exec требуют подтверждения. Не пытайся это обходить.\n"
        f"Окна сейчас:\n{window_summary}\n"
        "Когда выполнил — task_complete с кратким ответом. Отвечай по-русски."
    )


__all__ = [
    "build_system_prompt",
    "HOST_OS",
    "HOST_OS_RELEASE",
    "current_os",
]
