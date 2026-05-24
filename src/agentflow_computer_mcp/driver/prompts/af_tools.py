"""AgentFlow `af_*` tool block and the recall/remember memory block.

Both blocks are only injected when `af_tools_present` is true — the daemon
strips them when running without a logged-in AgentFlow session.
"""
from __future__ import annotations


AF_TOOLS = (
    "\nAgentFlow API tools (`af_*`) — используй их вместо UI-кликов когда возможно:\n"
    "  • af_list_projects / af_get_project / af_create_project / af_approve_project — проекты.\n"
    "  • af_list_devices / af_get_device — мои desktop-машины.\n"
    "  • af_list_agents / af_send_agent_message — общение с агентами маркетплейса.\n"
    "  • af_send_telegram_message(chat_id, text) — отправить TG-сообщение через MCP.\n"
    "      chat_id='me' → Saved Messages. chat_id='1361064246' → owner.\n"
    "  • af_telegram_dialogs / af_telegram_messages / af_telegram_search / "
    "af_telegram_react / af_telegram_whoami — читать TG без UI-кликов.\n"
    "  • af_post_matrix_room(room_id, text) — Matrix-сообщение через MCP.\n"
    "  • af_recall(tags=[...], limit=20) — на старте задачи вспомни уроки прошлых "
    "прогонов в этом домене (kwork, mail, captcha). Newest-first.\n"
    "  • af_remember(kind='lesson'|'observation'|'fact', text=..., tags=[...]) — "
    "в конце задачи запиши что сработало / что нет.\n"
)


MEMORY = (
    "\nПамять задач (af_remember / af_recall):\n"
    "  • На старте долгой/повторяющейся задачи (kwork, mail, captcha-обходы) — "
    "af_recall(tags=['<domain>']) и прочти 5-10 свежих lessons.\n"
    "  • В конце задачи — af_remember(kind='lesson', tags=['<domain>', '<action>'], "
    "text='короткое утверждение: что сделал и что узнал'). Тэги — короткие, в нижнем регистре.\n"
)
