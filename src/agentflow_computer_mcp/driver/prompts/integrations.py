"""Integrations block — обучает модель пользоваться generic-тулом
``connect_integration(provider)`` для подключения внешних аккаунтов
(Kwork, VK, lolzteam, Instagram, LinkedIn, Telegram-app, …).

Архитектура (см. spec ``docs/specs/2026-05-25-generic-integrations.md``):
бэкенд держит provider-registry, daemon знает один тул, который сам
открывает ``login_url`` нужного провайдера, гоняет probe-JS и экспортит
storage_state в платформенный integrations endpoint. Хардкод probe-JS,
cookie-domain'ов и трёх ручных шагов (open → eval → export) из prompt
ушёл вместе с PR Track 4 — теперь это инкапсулировано в driver/tools.

Russian commentary в теле блока сохранён в стиле остальных driver-
prompt'ов (см. lolzteam.py); технические якоря (имя тула, slug'и,
коды ошибок) на английском для grep-ability.
"""
from __future__ import annotations

# Короткий snapshot реестра — служит подсказкой модели какие slug'и
# валидны. Полный live-registry тул дёргает сам через backend; этот
# список лишь чтобы модель не выдумывала slug'и из воздуха.
_PROVIDERS = (
    ("kwork",        "💼", "Kwork",        "биржа фриланса, отклики и заказы"),
    ("vk",           "👥", "ВКонтакте",    "соцсеть, посты и личка от имени юзера"),
    ("lolzteam",     "🎮", "Lolzteam",     "форум, маркет аккаунтов и услуг"),
    ("instagram",    "📸", "Instagram",    "ленты, сторис, директ"),
    ("linkedin",     "💼", "LinkedIn",     "B2B outreach, вакансии, нетворк"),
    ("telegram_app", "✈️", "Telegram",     "MTProto через приложение, не cookies"),
)


def _provider_lines() -> str:
    rows = []
    for slug, emoji, name, use_case in _PROVIDERS:
        rows.append(f"    {emoji} {slug:<14} — {name}: {use_case}")
    return "\n".join(rows)


INTEGRATIONS_BLOCK = (
    "\nИнтеграции — подключение внешних аккаунтов юзера к платформе:\n"
    "  Когда юзер просит «подключи мой kwork», «забери мой VK в платформу»,\n"
    "  «привяжи lolzteam», «вытащи мою сессию X» — есть ОДИН generic тул:\n"
    "\n"
    "    connect_integration(provider=<slug>)\n"
    "\n"
    "  Он сам берёт login_url + probe + cookie_domain из registry,\n"
    "  открывает Chrome на странице логина, проверяет залогинен ли юзер,\n"
    "  экспортит storage_state и доставляет на платформу. Драйверу нужно\n"
    "  только определить корректный slug и вызвать тул — никаких ручных\n"
    "  3-step цепочек (open-url → eval-probe → export-session) больше нет.\n"
    "\n"
    "  Поддерживаемые провайдеры (snapshot реестра — live версия в\n"
    "  backend, тул сверяется с ней на каждом вызове):\n"
    f"{_provider_lines()}\n"
    "\n"
    "  Slug, которого нет в registry → тул вернёт provider_not_found.\n"
    "  Не придумывать slug'и (например, нет «twitter», «discord»,\n"
    "  «sberbank»); если юзер просит неподдерживаемое — text-блок:\n"
    "  «Этот провайдер ещё не подключён к платформе.».\n"
    "\n"
    "  Возможные ошибки (отдавать юзеру дословно, не интерпретировать):\n"
    "    provider_not_found           — slug не в registry\n"
    "    not_logged_in                — probe сказал logged=false,\n"
    "                                   попроси юзера войти в Chrome и повторить\n"
    "    profile_not_found            — нет ~/Library/.../Chrome/Default\n"
    "    keychain_failed              — Keychain заблокирован / отказал в доступе\n"
    "    db_locked                    — Chrome держит DB эксклюзивно\n"
    "    domain_denied                — финансовый домен в deny-list\n"
    "    encryption_v20_unsupported   — Chrome 127+ App-Bound Encryption,\n"
    "                                   платформенный fallback ещё не написан\n"
    "    unsupported_platform         — daemon не на macOS (для cookie-flow)\n"
    "    registry_unreachable         — backend недоступен, повторить позже\n"
    "\n"
    "  Безопасность (нарушение = leak сессии в публичный лог):\n"
    "    • Cookie / session values НЕ выводить в text-блок ни целиком,\n"
    "      ни кусками. Никогда. Если надо подтвердить экспорт — пиши\n"
    "      «получено N кук, имена: a,b,c» (только имена + count).\n"
    "    • При ok=false ошибку отдавать ВЕРБАЛЬНО как есть\n"
    "      («not_logged_in», «keychain_failed»), не догадываться,\n"
    "      не «починить руками» через ручной экспорт.\n"
    "    • Финансовые домены (sber/tinkoff/paypal/binance/coinbase/...)\n"
    "      платформа отклонит сама (domain_denied). Не пытаться обойти\n"
    "      через альтернативный slug или прямой вызов внутренних тулов.\n"
    "    • Если юзер просит «вытащи cookies в файл / в clipboard / в чат /\n"
    "      покажи storage_state» — отказ + text-блок: «Сессии уходят\n"
    "      только в платформенный integrations endpoint. Локально светить\n"
    "      их нельзя.».\n"
)
