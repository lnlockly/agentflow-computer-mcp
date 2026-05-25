"""Lolzteam.com forum block — авторизованная сессия юзера в Chrome.

Юзер сидит на lolzteam.com (русскоязычный underground-форум: market, услуги,
аккаунты, накрутка). У него залогинена постоянная сессия в Google Chrome.
Daemon видит её через chrome_eval / chrome_open_url (НЕ через browser_*,
который чистый Playwright без cookie).

Типовые задачи: найти исполнителя для услуги (накрутка GitHub-звёзд,
накрутка соцсетей, парсинг, аккаунты), оценить цены, разместить тему в
разделе «Хочу купить», ответить на сообщение в личке. Это серая зона —
работаем строго по инструкциям юзера, ничего не покупаем сами, никаких
сделок без явного approve в text-блоке.
"""
from __future__ import annotations

LOLZTEAM_BLOCK = (
    "\nLolzteam (lolzteam.com / lolz.live) — авторизованный форум юзера:\n"
    "  Это русскоязычный underground-форум (market, услуги, аккаунты, накрутка).\n"
    "  Юзер уже залогинен в обычном Chrome — работаем через chrome_open_url /\n"
    "  chrome_eval, НЕ через browser_* (Playwright не имеет cookie).\n"
    "\n"
    "  Стабильные селекторы Lolzteam (XenForo 2.x):\n"
    "    .structItemContainer .structItem-title a   — заголовок темы в списке\n"
    "    .p-title-value                              — название открытой темы\n"
    "    .message-userDetails .username              — автор сообщения\n"
    "    .message-body .bbWrapper                    — тело сообщения\n"
    "    .button--cta                                — кнопка «Создать тему» (CTA в разделе)\n"
    "    .formRow input[name=\"title\"]                — заголовок новой темы\n"
    "    .fr-element.fr-view[contenteditable=\"true\"] — редактор сообщения (Froala)\n"
    "    .input--search                              — поисковая строка вверху\n"
    "    .conversation-list .structItem-title a      — список личных диалогов\n"
    "\n"
    "  Карта релевантных разделов (URL-ы стабильные, форум их не двигает):\n"
    "    /forums/services/                — Услуги (накрутка / парсинг / SMM)\n"
    "    /forums/services/?prefix_id=N    — фильтр по типу услуги\n"
    "    /forums/promotion/               — Накрутка (звёзды, подписчики, отзывы)\n"
    "    /forums/accounts/                — Аккаунты (Telegram/GitHub/итд)\n"
    "    /forums/marketplace-wtb/         — Хочу купить (WTB / поиск исполнителей)\n"
    "    /conversations/                  — личка\n"
    "    /search/?q=<запрос>              — поиск по форуму\n"
    "\n"
    "  Типовые задачи:\n"
    "    • Найти исполнителя:\n"
    "        chrome_open_url('https://lolzteam.com/search/?q=<запрос>')\n"
    "        chrome_eval — собрать заголовки + цены через:\n"
    "          `[...document.querySelectorAll('.contentRow')].slice(0,15)\n"
    "             .map(r=>({title:r.querySelector('.contentRow-title')?.innerText,\n"
    "                       snippet:r.querySelector('.contentRow-snippet')?.innerText,\n"
    "                       url:r.querySelector('a')?.href}))`\n"
    "    • Разместить тему «Хочу купить»:\n"
    "        chrome_open_url('https://lolzteam.com/forums/marketplace-wtb/post-thread')\n"
    "        type заголовок в `input[name=\"title\"]`, тело в .fr-view, submit `.button--cta`.\n"
    "        ВСЕГДА сначала text-блок с черновиком юзеру, ждём подтверждение.\n"
    "    • Прочитать ответы в своей теме:\n"
    "        chrome_open_url(<thread_url>) → chrome_eval\n"
    "        `[...document.querySelectorAll('.message')].map(m=>({\n"
    "           author:m.querySelector('.username')?.innerText,\n"
    "           body:m.querySelector('.bbWrapper')?.innerText,\n"
    "           ts:m.querySelector('time')?.getAttribute('datetime')}))`.\n"
    "    • Ответить в личке:\n"
    "        chrome_open_url('https://lolzteam.com/conversations/<id>/') →\n"
    "        type текст в .fr-view → click 'Отправить'. Черновик в text-блоке СНАЧАЛА.\n"
    "\n"
    "  Антипаттерны (серьёзно — нарушение = бан аккаунта или потеря денег):\n"
    "    • НЕ покупать ничего самостоятельно (никаких «Купить» / «Оплатить»).\n"
    "      Всегда: найти оффер → text-блок с цифрами → ждать approve юзера.\n"
    "    • НЕ соглашаться на сделки в личке от своего имени. Только сбор\n"
    "      информации (цена, отзывы, sample-кейсы).\n"
    "    • НЕ переходить по подозрительным ссылкам в сообщениях (фишинг\n"
    "      под платёжки/garant — частый сценарий на форуме).\n"
    "    • НЕ публиковать ничего что нарушает закон/ToS платформы которой\n"
    "      ищем услугу (например, накрутка фейковыми аккаунтами — массовый\n"
    "      бан на GitHub за такое).\n"
    "    • НЕ кликать «Выйти» — повторно залогиниться без юзера не получится\n"
    "      (на форуме часто 2FA + капча).\n"
    "    • Файлы / архивы НЕ скачивать — частый malware-вектор. Только\n"
    "      читаем DOM.\n"
)
