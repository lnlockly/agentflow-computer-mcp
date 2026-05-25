"""Integrations block — teaches the model how to harvest a logged-in
Chrome session (Kwork, Telegram Web, etc.) and POST it to the AgentFlow
owner's integrations endpoint.

The flow is intentionally probe-first: never run ``chrome_export_cookies``
blind. If the user is not signed in, the export still succeeds (returns
expired or anonymous cookies) and the downstream MCP wastes a round-trip.
A 2-line ``chrome_eval`` probe catches that before any keychain access.

Russian commentary in the prompt body matches the daemon's existing voice
(see lolzteam.py); the technical anchors (tool names, error codes, URLs)
stay in English for grep-ability.
"""
from __future__ import annotations

INTEGRATIONS_BLOCK = (
    "\nИнтеграции — экспорт сессии (kwork / telegram-web / прочие) для серверных MCP:\n"
    "  Когда юзер просит «подключи мой kwork», «забери мою сессию kwork»,\n"
    "  «вытащи cookies из kwork в платформу» (или аналогично для другого\n"
    "  сайта) — стандартный 3-step flow, БЕЗ отклонений:\n"
    "\n"
    "  Step 1. Probe залогинен ли юзер (без probe'а cookies могут оказаться\n"
    "  пустыми/expired, серверная сторона потратит запрос впустую):\n"
    "    chrome_open_url('https://kwork.ru/manage_offers')\n"
    "    chrome_eval `{logged: !!document.querySelector('[data-test=\"header-user-name\"]')\n"
    "                  || !!document.cookie.match(/track=/),\n"
    "                  url: location.href}`\n"
    "    Если logged=false → text-блок юзеру: «Не вижу залогиненную сессию\n"
    "    Kwork в Chrome. Открой kwork.ru, войди, потом повтори запрос.».\n"
    "    НЕ продолжать на step 2.\n"
    "\n"
    "  Step 2. Экспорт куков (HttpOnly включены, document.cookie их не\n"
    "  отдаёт — поэтому экспорт идёт через SQLite + Keychain, не через JS):\n"
    "    chrome_export_cookies(domain='kwork.ru')\n"
    "    Ответ — Playwright storage_state.cookies формат, готов к ingest.\n"
    "    Возможные ошибки (отдавать юзеру дословно, не интерпретировать):\n"
    "      profile_not_found            — нет ~/Library/.../Chrome/Default\n"
    "      keychain_failed              — Keychain заблокирован / отказал в доступе\n"
    "      db_locked                    — Chrome держит DB эксклюзивно\n"
    "      domain_denied                — финансовый домен в deny-list\n"
    "      encryption_v20_unsupported   — Chrome 127+ App-Bound Encryption,\n"
    "                                     платформенный fallback ещё не\n"
    "                                     написан (см. RAG runbook)\n"
    "      unsupported_platform         — daemon не на macOS\n"
    "\n"
    "  Step 3. Доставка на платформу (owner-key уже в env daemon'а, не\n"
    "  надо просить юзера). Endpoint:\n"
    "    POST https://agentflow.website/_agents/me/integrations/kwork\n"
    "    body = { cookies: <массив из chrome_export_cookies.cookies>,\n"
    "             profile: 'Default' }\n"
    "    headers = { 'x-api-key': $AF_OWNER_API_KEY, 'content-type': 'application/json' }\n"
    "    Для других сайтов хост тот же, путь меняется: /telegram, /linkedin, ...\n"
    "\n"
    "  Безопасность (нарушение = leak сессии в публичный лог):\n"
    "    • Cookie values НЕ выводить в text-блок целиком. Никогда. Если\n"
    "      надо подтвердить экспорт — пиши «получено N кук, имена: a,b,c»\n"
    "      (только имена + count, без значений).\n"
    "    • При ok=false ошибку отдавать ВЕРБАЛЬНО как есть («profile_not_found»),\n"
    "      не догадываться, не «починить руками».\n"
    "    • Финансовые домены (sber/tinkoff/paypal/binance/coinbase/...)\n"
    "      платформа отклонит сама (domain_denied). Не пытаться обойти.\n"
    "    • Если юзер просит «вытащи cookies в файл / в clipboard / в чат» —\n"
    "      отказ + text-блок: «Cookies уходят только в платформенный\n"
    "      integrations endpoint. Локально светить их нельзя.».\n"
)
