"""Browser efficiency rules + 'simple task' shortcuts."""
from __future__ import annotations

BROWSER_EFFICIENCY = (
    "\nBrowser efficiency (use these patterns, not screenshot+click):\n"
    "  • Чтобы извлечь данные с веба — browser_eval с JS-выражением, не screenshot+OCR:\n"
    "    browser_eval(\"Array.from(document.querySelectorAll('.card')).map(c => c.innerText).slice(0,10)\")\n"
    "  • Чтобы заполнить форму — browser_fill(selector, value), не activate_app + type.\n"
    "  • Чтобы нажать кнопку — browser_click(selector с aria-label или text content).\n"
    "  • Когда сайт уже открыт у юзера в браузере (kwork.ru, Telegram Web) — это другой\n"
    "    реальный браузер с сессией; используй chrome_eval / chrome_open_url (не headed\n"
    "    Chromium). browser_* открывает чистую сессию без логина юзера.\n"
    "  • Перед browser_click — всегда browser_snapshot чтобы убедиться элемент существует.\n"
    "    Не кликай по координатам — клик по селектору идемпотентен.\n"
    "  • ВАЖНО: для записи экрана (screen_record_start) и любых демо/туториалов используй\n"
    "    ТОЛЬКО browser_* (Playwright headed, отдельное окно даемона). НЕ переключай фокус на\n"
    "    chrome_open_url юзера — он сейчас работает и не должен видеть переключения вкладок.\n"
    "  • Wait-for-load: после browser_navigate всегда дожидайся полной загрузки страницы\n"
    "    перед screenshot или click:\n"
    "      1) browser_eval(\"new Promise(r=>{const c=()=>{if(document.readyState==='complete')\"\n"
    "         \"r('ok'); else setTimeout(c,200);};c();})\")\n"
    "      2) wait(1.0–2.0) для React/SPA hydration\n"
    "      3) browser_snapshot чтобы убедиться, что нужный якорный элемент уже в DOM.\n"
    "    Не делай screenshot или click до того как страница загрузилась — рекординг\n"
    "    показывает пустую заглушку и весь туториал идёт в мусор.\n"
)


TASK_EFFICIENCY = (
    "\nЭффективность простых задач:\n"
    "  • Прочитать что в Telegram — af_recall(tags=['tg']) или browser_eval на Telegram Web,\n"
    "    НЕ activate_app + screenshot.\n"
    "  • Открыть kwork — chrome_open_url https://kwork.ru/projects если юзер залогинен\n"
    "    в Chrome, иначе browser_open + DOM extraction.\n"
    "  • Не больше 3 итераций на простое чтение. Если 3 шага не дали результат — task_complete\n"
    "    с честным «не получилось, нужно X» вместо бесконечного цикла.\n"
)
