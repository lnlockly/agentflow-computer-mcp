"""User-phrase → tool intent map.

This is the largest block in the system prompt because it teaches the
model not to drive UI for things that have a clean API path.
"""
from __future__ import annotations

from .os_context import OS_INTENT_BLOCK, current_os


def intent_map() -> str:
    return (
        "\nКонкретные сопоставления (запрос юзера → инструмент):\n"
        "  • «напиши в TG / отправь сообщение в Telegram / в Saved» → "
        "af_send_telegram_message(chat_id='me', text=...). НЕ открывай приложение Telegram.\n"
        "  • «напиши в TG юзеру X / chat_id N» → af_send_telegram_message(chat_id=N, text=...).\n"
        "  • «покажи последние диалоги в TG / что у меня в Telegram» → af_telegram_dialogs(limit=20).\n"
        "  • «прочитай переписку с X / что писал Y» → af_telegram_search(q=\"X\") → "
        "af_telegram_messages(chat_id=<found>).\n"
        "  • «ответь Y / напиши Y» — если Y не chat_id: af_telegram_search(q=\"Y\") → возьми первый "
        "chat_id → af_send_telegram_message.\n"
        "  • «поставь лайк на сообщение в чате X» → af_telegram_react(chat_id, message_id, emoji='👍').\n"
        "  • «напиши в Matrix / в комнату X» → af_post_matrix_room(room_id=..., text=...).\n"
        f"{OS_INTENT_BLOCK[current_os()]}"
        "  • «открой kwork / kwork.ru / посмотри заказы на kwork / посмотри письма в браузере / "
        "открой Firefox / Telegram Web» → firefox_open → firefox_navigate <url> → "
        "firefox_snapshot → firefox_eval. firefox_* запускает РЕАЛЬНЫЙ Firefox юзера с его "
        "профилем — он уже залогинен в kwork / TG Web / mail. browser_* (headed Chromium) — "
        "только для анонимного скрейпа.\n"
        "  • «открой документ X / напиши в файл» → fs.write (с подтверждением), либо открой через "
        "activate_app соответствующего редактора, потом keypress/type.\n"
        "  • «прочитай экран / что сейчас открыто» → screen_capture + краткое описание.\n"
        "  • «напиши код для X / сделай скрипт Y» → активируй редактор (Cursor / VSCode / iTerm), "
        "читай существующий код через code_read_file, правь через code_edit_file/code_write_file "
        "(с подтверждением). Перед изменением — короткий план в text-блоке.\n"
        "  • «сделай проект Y / реализуй X как отдельный сервис» → af_spawn_subagent(brief=...) "
        "и стриминг прогресса через af_get_project_events. Не пытайся сделать всё внутри одного "
        "task если scope большой.\n"
        "  • «запусти под-агента / делегируй X» → af_spawn_subagent(brief=...). После старта верни "
        "project_id и slug, не жди до конца если время > 60с.\n"
        "  • «запиши видео экрана / сохрани видео что я делаю / clip последних N секунд» → "
        "screen_record_start(path=~/Movies/agentflow-<ts>.mp4, max_duration_s=120) → выполняй задачу → "
        "screen_record_stop. Если пользователь скажет «достаточно» / «хватит» — screen_record_stop сразу.\n"
        "  • Не запускай запись без явной просьбы. После stop напиши путь к файлу и его размер "
        "в task_complete.\n"
    )
