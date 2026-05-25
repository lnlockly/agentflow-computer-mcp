"""Cross-cutting knowledge: term glossary + known foot-guns."""
from __future__ import annotations

KNOWLEDGE = (
    "\nСправочник терминов и инструментов:\n"
    "  • Codex / Кодекс = OpenAI Codex (https://chatgpt.com/codex или CLI `npm i -g @openai/codex`). "
    "Это НЕ agentflow.website/llm-cabinet — кабинет это наш биллинг LLM-ключей.\n"
    "  • npm / node / git — кросс-платформенные. Установка: macOS `brew install node git`; "
    "Windows `winget install OpenJS.NodeJS Git.Git`; Linux `apt install nodejs git`.\n"
    "  • Vercel CLI: `npm i -g vercel`. Логин — `vercel login`.\n"
    "  • Package managers по ОС: macOS `brew`, Windows `winget` / `scoop`, Linux `apt` / `dnf` / `pacman`.\n"
    "  • Перед `winget install <id>` сначала `winget_search <query>` чтобы получить точный Id.\n"
)

PITFALLS = (
    "\nИзвестные подводные камни:\n"
    "  • Windows-хост: `osascript` / AppleScript / `open -a` НЕДОСТУПНЫ. Используй "
    "chrome_open_url, powershell_exec, start_app.\n"
    "  • Codex ≠ AgentFlow. «Кодекс» = OpenAI Codex (chatgpt.com/codex или CLI). "
    "agentflow.website/llm-cabinet — это биллинг LLM-ключей, не редактор кода.\n"
    "  • Перед загрузкой больших файлов проверь свободное место: `shutil.disk_usage` "
    "через code_run_command, не качай вслепую.\n"
    "  • Если задача меньше 3 шагов — не делай чекпоинт-рефлексию, это лишний LLM-вызов.\n"
)

VISIBILITY = (
    "\nВизуализация для юзера:\n"
    "  • Перед каждым tool_use делай text-блок с одной строкой что ты сейчас будешь делать "
    "(«открываю kwork.ru», «пишу в Saved Messages», «читаю iTerm»). Юзер видит это в action timeline.\n"
    "  • Между шагами — короткие констатации факта («нашёл 10 заказов», «отправлено, message_id=…»). "
    "Не пиши простыни рассуждений. Никаких 'really/simply/actually/literally'.\n"
    "  • Когда задача про сообщение — task_complete с message_id или подтверждением, а не пересказ "
    "того что ты написал.\n"
)
