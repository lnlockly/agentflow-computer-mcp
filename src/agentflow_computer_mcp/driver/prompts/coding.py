"""Coding-tool workflow rules."""
from __future__ import annotations

CODING_WORKFLOW = (
    "\nCoding workflow:\n"
    "  1. Read first: code_list_dir для обзора, code_read_file для конкретных файлов. "
    "Не редактируй вслепую.\n"
    "  2. Batch edits: один code_edit_file per logical change. Большие новые файлы → "
    "code_write_file(mode='replace'). Дописать в конец → mode='append'.\n"
    "  3. Run + react: после code_run_command всегда читай stderr. Если exit_code != 0 — "
    "fix по stderr перед следующим шагом, не повторяй ту же команду.\n"
    "  4. Delegate when big: фича на 3+ файла или новый сервис — af_spawn_subagent, не "
    "пиши руками на десктопе.\n"
)
