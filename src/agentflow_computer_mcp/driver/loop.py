"""Driver loop: pulls a task off the queue, runs an Anthropic-style tool-use loop until done."""
from __future__ import annotations

import contextlib
import json
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


def build_system_prompt(window_summary: str, af_tools_present: bool) -> str:
    af_block = ""
    if af_tools_present:
        af_block = (
            "\nAgentFlow API tools (`af_*`):\n"
            "  • af_list_projects / af_get_project / af_create_project / af_approve_project — manage user's projects.\n"
            "  • af_list_devices / af_get_device — see this user's enrolled desktop machines.\n"
            "  • af_list_agents / af_send_agent_message — talk to project agents.\n"
            "  • af_send_telegram_message / af_post_matrix_room — broadcast to bound channels.\n"
            "Prefer af_* when the task is platform-side (create a project, ping agent) — no need to open a browser.\n"
        )
    return (
        "Ты управляешь Mac пользователя. Перед действием — короткая мысль в text-блоке. "
        "Не извиняйся, не повторяй очевидное. Стратегия:\n"
        "  • для содержимого окон Mac: activate_app → wait 0.5 → screen_region(bounds) — быстро и детально.\n"
        "  • для iTerm/Terminal: read_terminal даёт точный текст через AppleScript.\n"
        "  • для веб-задач (открыть сайт, прочитать DOM, нажать кнопку): browser_open → browser_navigate → "
        "browser_snapshot → browser_click/browser_fill/browser_press/browser_eval. Это headed Chromium, "
        "ОТДЕЛЬНЫЙ от пользовательского Chrome. Видим в live viewer.\n"
        "  • для авторизованного веба (где у юзера уже залогинено): chrome_eval / chrome_open_url — реальный "
        "Google Chrome с его сессией.\n"
        f"{af_block}"
        "Scope hard rules: paths `~/.ssh`, `~/.config`, `~/Library/Keychains`, `~/.aws`, `~/.gnupg` всегда запрещены "
        "к чтению/записи. fs.write и shell.exec требуют подтверждения. Не пытайся это обходить.\n"
        f"Окна Mac сейчас:\n{window_summary}\n"
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
