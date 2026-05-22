"""Driver loop: pulls a task off the queue, runs an Anthropic-style tool-use loop until done."""
from __future__ import annotations

import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .desktop_tools import (
    ToolExecutor,
    all_tool_descriptors,
    get_window_list,
    jpeg_b64_full,
)
from .state import DriverState
from .streamer import compress_png_for_viewer

DEFAULT_LLM_URL = "https://agentflow.website/_agents/llm/v1/messages"
DEFAULT_MODEL = "claude-opus-4-7"
MAX_ITERS = 40


def _current_os() -> str:
    """One of 'macos' | 'linux' | 'windows' — used to swap the OS-specific
    section of the intent map so the model doesn't try to drive Mail.app
    on Ubuntu or hit Cmd+Space on a Windows box."""
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    # Best-effort fallback — treat anything exotic as Linux since the
    # Linux block is the most generic (browser-first, no native keybinds).
    return "linux"


_OS_INTENT_BLOCK = {
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


def build_system_prompt(window_summary: str, af_tools_present: bool) -> str:
    af_block = ""
    if af_tools_present:
        af_block = (
            "\nAgentFlow API tools (`af_*`) — используй их вместо UI-кликов когда возможно:\n"
            "  • af_list_projects / af_get_project / af_create_project / af_approve_project — проекты.\n"
            "  • af_list_devices / af_get_device — мои desktop-машины.\n"
            "  • af_list_agents / af_send_agent_message — общение с агентами маркетплейса.\n"
            "  • af_send_telegram_message(chat_id, text) — отправить TG-сообщение через MCP.\n"
            "      chat_id='me' → Saved Messages. chat_id='1361064246' → owner.\n"
            "  • af_post_matrix_room(room_id, text) — Matrix-сообщение через MCP.\n"
            "  • af_recall(tags=[...], limit=20) — на старте задачи вспомни уроки прошлых "
            "прогонов в этом домене (kwork, mail, captcha). Newest-first.\n"
            "  • af_remember(kind='lesson'|'observation'|'fact', text=..., tags=[...]) — "
            "в конце задачи запиши что сработало / что нет.\n"
        )

    # Concrete mapping from common user phrases to the right tool. The model
    # needs this because "напиши в TG" used to trigger UI-driven Telegram-app
    # automation, which was slow, brittle, and visually noisy. The af_* path
    # is silent, idempotent, and works even when the Telegram window is
    # closed.
    intent_map = (
        "\nКонкретные сопоставления (запрос юзера → инструмент):\n"
        "  • «напиши в TG / отправь сообщение в Telegram / в Saved» → "
        "af_send_telegram_message(chat_id='me', text=...). НЕ открывай приложение Telegram.\n"
        "  • «напиши в TG юзеру X / chat_id N» → af_send_telegram_message(chat_id=N, text=...).\n"
        "  • «напиши в Matrix / в комнату X» → af_post_matrix_room(room_id=..., text=...).\n"
        f"{_OS_INTENT_BLOCK[_current_os()]}"
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
    )

    browser_efficiency = (
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
    )

    task_efficiency = (
        "\nЭффективность простых задач:\n"
        "  • Прочитать что в Telegram — af_recall(tags=['tg']) или browser_eval на Telegram Web,\n"
        "    НЕ activate_app + screenshot.\n"
        "  • Открыть kwork — chrome_open_url https://kwork.ru/projects если юзер залогинен\n"
        "    в Chrome, иначе browser_open + DOM extraction.\n"
        "  • Не больше 3 итераций на простое чтение. Если 3 шага не дали результат — task_complete\n"
        "    с честным «не получилось, нужно X» вместо бесконечного цикла.\n"
    )

    coding_workflow = (
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

    memory_block = ""
    if af_tools_present:
        memory_block = (
            "\nПамять задач (af_remember / af_recall):\n"
            "  • На старте долгой/повторяющейся задачи (kwork, mail, captcha-обходы) — "
            "af_recall(tags=['<domain>']) и прочти 5-10 свежих lessons.\n"
            "  • В конце задачи — af_remember(kind='lesson', tags=['<domain>', '<action>'], "
            "text='короткое утверждение: что сделал и что узнал'). Тэги — короткие, в нижнем регистре.\n"
        )

    visibility_block = (
        "\nВизуализация для юзера:\n"
        "  • Перед каждым tool_use делай text-блок с одной строкой что ты сейчас будешь делать "
        "(«открываю kwork.ru», «пишу в Saved Messages», «читаю iTerm»). Юзер видит это в action timeline.\n"
        "  • Между шагами — короткие констатации факта («нашёл 10 заказов», «отправлено, message_id=…»). "
        "Не пиши простыни рассуждений. Никаких 'really/simply/actually/literally'.\n"
        "  • Когда задача про сообщение — task_complete с message_id или подтверждением, а не пересказ "
        "того что ты написал.\n"
    )

    os_label = {"macos": "Mac", "linux": "Linux", "windows": "Windows"}[_current_os()]
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
        f"{af_block}"
        f"{intent_map}"
        f"{browser_efficiency}"
        f"{memory_block}"
        f"{coding_workflow}"
        f"{task_efficiency}"
        f"{visibility_block}"
        "Scope hard rules: paths `~/.ssh`, `~/.config`, `~/Library/Keychains`, `~/.aws`, `~/.gnupg` всегда запрещены "
        "к чтению/записи. fs.write и shell.exec требуют подтверждения. Не пытайся это обходить.\n"
        f"Окна сейчас:\n{window_summary}\n"
        "Когда выполнил — task_complete с кратким ответом. Отвечай по-русски."
    )


def post_llm(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int = 180,
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "user-agent": "agentflow-desktop/0.2",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def update_live(state: DriverState, action: str, detail: str = "", thinking: str = "") -> None:
    def _write_jpeg(live_dir: Any) -> None:
        try:
            from .desktop_tools import grab_full_png

            (live_dir / "latest.jpg").write_bytes(compress_png_for_viewer(grab_full_png()))
        except Exception as exc:  # noqa: BLE001
            print(f"[update_live] capture err: {exc}", flush=True)

    state.push_action(action, detail, thinking, jpeg_path_writer=_write_jpeg)
    with state.actions_lock:
        actions_copy = list(state.actions)
    with contextlib.suppress(Exception):
        (state.live_dir / "actions.json").write_text(
            json.dumps(actions_copy, ensure_ascii=False)
        )


def _fetch_skills_prompt_block(af_client: Any) -> str:
    """Fetch the user's pre-rendered intent-skills block from the server.

    Returns the block text on success, `""` on any failure (network,
    auth, malformed body). The daemon must never crash a task because
    the skills endpoint is down.
    """
    try:
        resp = af_client.get_skills_prompt_block()
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] skills fetch error: {exc}", flush=True)
        return ""
    if not getattr(resp, "ok", False):
        return ""
    body = getattr(resp, "body", None)
    if not isinstance(body, dict):
        return ""
    block = body.get("block")
    return block.strip() if isinstance(block, str) else ""


def run_task(
    task: str,
    state: DriverState,
    executor: ToolExecutor,
    api_key: str,
    *,
    llm_url: str = DEFAULT_LLM_URL,
    model: str = DEFAULT_MODEL,
    max_iters: int = MAX_ITERS,
) -> str:
    update_live(state, "start", task)
    wins = get_window_list()
    win_summary = "\n".join(
        f"  • {w['owner']!r}  bounds=({w['bounds'].get('x')},{w['bounds'].get('y')},"
        f"{w['bounds'].get('width')}x{w['bounds'].get('height')})  id={w['window_id']}"
        for w in wins
    )
    print(
        f"\n{'=' * 70}\nTask: {task}\nWindows ({len(wins)}):\n{win_summary}\n{'=' * 70}",
        flush=True,
    )

    af_present = executor._af is not None  # noqa: SLF001
    system_msg = build_system_prompt(win_summary, af_tools_present=af_present)

    # Prepend the user's editable Skills block from /me/devices/skills.
    # The cabinet UI at /cabinet/devices/skills lets the owner add custom
    # phrase → action mappings; they should win over the hardcoded
    # intent_map in build_system_prompt. Soft-fail: a missing/erroring
    # endpoint must not block task execution.
    if af_present:
        skills_block = _fetch_skills_prompt_block(executor._af)  # noqa: SLF001
        if skills_block:
            system_msg = (
                "Пользовательские skills (приоритетнее дефолтных правил):\n"
                f"{skills_block}\n\n"
                f"{system_msg}"
            )
    tools = all_tool_descriptors() if af_present else [
        t for t in all_tool_descriptors() if not t["name"].startswith("af_")
    ]

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": jpeg_b64_full(),
                    },
                },
                {
                    "type": "text",
                    "text": f"Снимок экрана. Задача: {task}\n\nКогда выполнишь — task_complete с ответом.",
                },
            ],
        }
    ]

    final_answer = ""
    iterations = 0
    for i in range(max_iters):
        # Check for cancel signal between iterations (never mid-tool-call).
        if state.abort_flag.is_set():
            state.abort_flag.clear()
            update_live(state, "cancelled", "task cancelled by user")
            print("\n=== CANCELLED ===", flush=True)
            if state.current_task_id:
                state.publish_outbound(
                    {
                        "type": "task_error",
                        "task_id": state.current_task_id,
                        "error": "cancelled_by_user",
                    }
                )
            return ""

        iterations = i + 1
        print(f"\n--- iter {iterations} ---", flush=True)
        try:
            resp = post_llm(
                llm_url,
                api_key,
                {
                    "model": model,
                    "max_tokens": 1024,
                    "system": system_msg,
                    "tools": tools,
                    "messages": messages,
                },
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:300]
            print(f"http {exc.code}: {body}", flush=True)
            update_live(state, "error", f"llm http {exc.code}: {body}")
            return ""
        if resp.get("type") == "error":
            update_live(state, "error", f"api error: {resp}")
            return ""

        content = resp.get("content", [])
        texts = [b["text"] for b in content if b.get("type") == "text"]
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        thinking = " ".join(texts).strip()
        for t in texts:
            print(f"claude: {t}", flush=True)
        if thinking:
            update_live(state, "thinking", "", thinking)

        if not tool_uses:
            print(f"\n=== END (no tools, stop_reason={resp.get('stop_reason')}) ===", flush=True)
            return final_answer

        messages.append({"role": "assistant", "content": content})
        results: list[dict[str, Any]] = []
        done = False
        for tu in tool_uses:
            args_preview = json.dumps(tu["input"], ensure_ascii=False)[:160]
            print(f"  → {tu['name']}({args_preview})", flush=True)
            try:
                out, image = executor.execute(tu["name"], tu.get("input", {}))
            except Exception as exc:  # noqa: BLE001
                out, image = f"error: {exc}", None
            preview = (out[:240] if isinstance(out, str) else str(out)[:240]).replace("\n", " | ")
            print(f"    = {preview}", flush=True)
            update_live(
                state,
                tu["name"],
                f"args: {args_preview}\nresult: {out[:600] if isinstance(out, str) else out}",
            )
            if out == "__DONE__":
                done = True
                final_answer = tu["input"].get("answer", "")
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": "ok"})
                continue
            if image is not None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": image["b64"],
                                },
                            },
                            {"type": "text", "text": out},
                        ],
                    }
                )
            else:
                results.append(
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": str(out)}
                )
        messages.append({"role": "user", "content": results})
        if done:
            update_live(state, "DONE", final_answer)
            print(f"\n=== DONE ===\n{final_answer}", flush=True)
            if state.current_task_id:
                state.publish_outbound(
                    {
                        "type": "task_complete",
                        "task_id": state.current_task_id,
                        "answer": final_answer,
                        "iterations": iterations,
                        "tokens_used": 0,
                        "cost_usd": 0.0,
                    }
                )
            return final_answer

    update_live(state, "max_iters", f"reached {max_iters}")
    print("\n=== max iters ===", flush=True)
    if state.current_task_id:
        state.publish_outbound(
            {
                "type": "task_complete",
                "task_id": state.current_task_id,
                "answer": final_answer,
                "iterations": iterations,
                "tokens_used": 0,
                "cost_usd": 0.0,
            }
        )
    return final_answer


def _normalize_task_entry(entry: Any) -> tuple[str, str]:
    """Accept legacy str entries or new (id, task) tuples uniformly."""
    if isinstance(entry, tuple) and len(entry) == 2:
        return str(entry[0]), str(entry[1])
    return f"local-{int(time.time() * 1000)}", str(entry)


def task_worker(
    state: DriverState,
    executor: ToolExecutor,
    api_key: str,
    *,
    llm_url: str = DEFAULT_LLM_URL,
    model: str = DEFAULT_MODEL,
) -> None:
    """Blocking loop: pull tasks off the queue, run them sequentially."""
    update_live(state, "idle", "ожидаю задачу из чат-инпута")
    while True:
        try:
            raw = state.task_queue.get(timeout=1)
        except Exception:  # noqa: BLE001 — queue.Empty
            if not state.busy and (int(time.time()) % 60 == 0):
                update_live(state, "idle", "ожидаю задачу")
            continue
        task_id, task = _normalize_task_entry(raw)
        state.busy = True
        state.current_task = task
        state.current_task_id = task_id
        try:
            run_task(task, state, executor, api_key, llm_url=llm_url, model=model)
        except Exception as exc:  # noqa: BLE001
            update_live(state, "error", f"{type(exc).__name__}: {exc}")
            print(f"task error: {exc}", flush=True)
            if state.current_task_id:
                state.publish_outbound(
                    {
                        "type": "task_error",
                        "task_id": state.current_task_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        finally:
            state.busy = False
            state.current_task = ""
            state.current_task_id = ""
            state.task_count += 1
