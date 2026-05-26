"""Driver loop: pulls a task off the queue, runs an Anthropic-style tool-use loop until done."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
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
from .prompts import (
    HOST_OS,  # noqa: F401 — re-exported for installer-smoke + downstream
    HOST_OS_RELEASE,  # noqa: F401 — re-exported, used by health probes
    build_system_prompt,
)
from .state import DriverState
from .streamer import compress_png_for_viewer

DEFAULT_LLM_URL = "https://agentflow.website/_agents/llm/v1/messages"
# `gpt-5.3-codex` через codex.sale upstream — owner-выбор: быстрая
# tool-use модель оптимизирована под coding/agent циклы. Маршрутизация
# идёт через тот же public AgentFlow ai-router (`/llm/v1/messages`):
# Anthropic-формат body → `anthropic-openai-bridge` на стороне backend
# переводит на OpenAI shape для codex.sale, биллинг через `recordUsage`
# в `agentflow-agents/src/routes/public-llm.ts`.
# Owner может override через AF_DESKTOP_MODEL env; scope-level override
# (per task) всё равно выигрывает.
DEFAULT_MODEL = os.environ.get("AF_DESKTOP_MODEL", "gpt-5.3-codex")
# Reasoning effort для GPT-5 серии: `medium` = fast-mode (быстрее `high`,
# точнее `low`). Передаётся в body как `reasoning_effort` если модель
# из gpt-5.* семейства. Anthropic-модели поле игнорируют.
DEFAULT_REASONING_EFFORT = os.environ.get("AF_DESKTOP_REASONING_EFFORT", "medium")
MAX_ITERS = 40
HISTORY_IMAGE_KEEP_RECENT_MESSAGES = 1


def _is_gpt5_family(model: str) -> bool:
    """True if the model id belongs to the OpenAI gpt-5.* / o-series family
    that accepts a `reasoning_effort` hint.

    codex.sale fronts these under names like `gpt-5.3-codex`, `gpt-5.4`, and
    `o3-mini` — they all honour the field. Anthropic / claude-* models
    silently ignore it, but we still skip the addition to keep the upstream
    body clean for east-api-3.
    """
    if not model:
        return False
    m = model.lower()
    if m.startswith("gpt-5") or m.startswith("gpt5"):
        return True
    # OpenAI o-series reasoning models (o1, o3, o4, …). Match `o<digit>` to
    # avoid catching unrelated names that happen to start with `o`.
    return bool(len(m) >= 2 and m[0] == "o" and m[1].isdigit())


def _augment_body_with_reasoning(
    body: dict[str, Any], model: str, reasoning_effort: str | None
) -> dict[str, Any]:
    """Attach `metadata.reasoning_effort` to the outgoing body when model
    is part of the gpt-5 / o-series family.

    Why `metadata.*` instead of a top-level field — the daemon talks
    Anthropic wire format to `/llm/v1/messages`. The Anthropic schema has
    no `reasoning_effort` slot, but it does accept arbitrary `metadata`
    on the request envelope. The backend bridge
    (`agentflow-agents/src/services/anthropic-openai-bridge.ts`) reads
    `metadata.reasoning_effort` and forwards it as a top-level field on
    the upstream OpenAI request, which is what codex.sale expects.
    """
    if not reasoning_effort or not _is_gpt5_family(model):
        return body
    meta = body.get("metadata")
    meta = {} if not isinstance(meta, dict) else dict(meta)
    meta["reasoning_effort"] = reasoning_effort
    body["metadata"] = meta
    return body


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to default on missing/garbage.

    Used by the loop caps so an operator can dial `LOOP_MAX_STEPS=120` without
    a redeploy. Non-numeric values fall back silently — the loop should never
    refuse to run because someone typed `abc` in the env.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Per-task hard caps. Configurable via env so ops can tune without a redeploy.
# Defaults: 50 tool calls and $2 USD spend — generous for normal tasks,
# cheap-fail for runaway agent loops. Per-task scope.budget_usd overrides
# this when the dispatch frame supplies one, and `~/.agentflow/computer-scope.toml`
# `budget_usd` lifts the floor at daemon-config load time (see the scope-aware
# fallback in run_task's caller).
LOOP_MAX_STEPS = _env_int("LOOP_MAX_STEPS", 50)
LOOP_MAX_USD = _env_float("LOOP_MAX_USD", 2.0)
# Reflection cadence. Every Nth tool call we inject a "are you on track?"
# turn; 0 disables. Short tasks (< 3 steps) skip the check regardless.
LOOP_CHECKPOINT_EVERY = _env_int("LOOP_CHECKPOINT_EVERY", 8)
# Below this step count we never bother with checkpoint reflection — short
# read tasks shouldn't pay a 1-LLM-call overhead just to confirm "yes, still
# on track".
CHECKPOINT_MIN_STEPS = 3

# Prompt blocks (cabinet, terminal, element, intent map, etc.) live under
# driver/prompts/. build_system_prompt is re-imported at the top of this
# module from .prompts so callers don't change.


class TaskCancelled(Exception):
    """Raised inside run_task when state.abort_flag fires mid-flight.

    The handler in run_task catches this, publishes task_error, and returns.
    Keeping it as an exception (instead of a sentinel return) makes every
    code path that goes through the LLM call or tool dispatch unwind
    immediately, including those nested inside helper functions.
    """


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


def _assemble_anthropic_sse(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reassemble an Anthropic /v1/messages SSE stream into the non-stream
    response shape that the rest of run_task expects.

    Anthropic streaming protocol:
      - message_start            → carries the skeleton (id, role, model)
      - content_block_start      → declares a content block (text or tool_use)
      - content_block_delta      → text_delta { text } | input_json_delta { partial_json }
      - content_block_stop
      - message_delta            → carries stop_reason, usage
      - message_stop
    """
    skeleton: dict[str, Any] = {
        "id": "",
        "type": "message",
        "role": "assistant",
        "model": "",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {},
    }
    # block index → assembled block dict (text accumulator or tool_use w/ json buffer)
    blocks: dict[int, dict[str, Any]] = {}
    tool_input_buffers: dict[int, str] = {}

    for ev in events:
        et = ev.get("type")
        if et == "message_start":
            msg = ev.get("message", {}) or {}
            skeleton["id"] = msg.get("id", "")
            skeleton["model"] = msg.get("model", "")
            if msg.get("role"):
                skeleton["role"] = msg["role"]
            if msg.get("usage"):
                skeleton["usage"] = msg["usage"]
        elif et == "content_block_start":
            idx = ev.get("index", 0)
            block = dict(ev.get("content_block") or {})
            if block.get("type") == "text":
                block.setdefault("text", "")
            elif block.get("type") == "tool_use":
                block.setdefault("input", {})
                tool_input_buffers[idx] = ""
            blocks[idx] = block
        elif et == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta", {}) or {}
            block = blocks.get(idx)
            if block is None:
                continue
            dt = delta.get("type")
            if dt == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dt == "input_json_delta":
                tool_input_buffers[idx] = tool_input_buffers.get(idx, "") + delta.get(
                    "partial_json", ""
                )
        elif et == "content_block_stop":
            idx = ev.get("index", 0)
            block = blocks.get(idx)
            if block is None:
                continue
            if block.get("type") == "tool_use":
                raw = tool_input_buffers.get(idx, "")
                if raw:
                    try:
                        block["input"] = json.loads(raw)
                    except json.JSONDecodeError:
                        block["input"] = {}
        elif et == "message_delta":
            delta = ev.get("delta", {}) or {}
            if "stop_reason" in delta:
                skeleton["stop_reason"] = delta["stop_reason"]
            if "stop_sequence" in delta:
                skeleton["stop_sequence"] = delta["stop_sequence"]
            usage = ev.get("usage")
            if usage:
                skeleton["usage"] = {**skeleton.get("usage", {}), **usage}
        # message_stop: nothing to assemble

    # Preserve block order by index
    skeleton["content"] = [blocks[i] for i in sorted(blocks.keys())]
    return skeleton


# Default per-call ceiling for the streaming LLM POST. Lower than the legacy
# 180 s so a half-open TCP socket (eg WinError 10060) frees the AI loop in
# under a minute; ops can dial it via env. The watchdog also enforces a
# per-chunk inactivity cap so a long stream that goes silent mid-flight
# does not park the daemon for the full ceiling.
DEFAULT_LLM_CONNECT_TIMEOUT_S = _env_int("AF_LLM_CONNECT_TIMEOUT_S", 60)
DEFAULT_LLM_STREAM_IDLE_TIMEOUT_S = _env_int("AF_LLM_STREAM_IDLE_TIMEOUT_S", 45)


class LlmNetworkError(Exception):
    """Raised when the LLM endpoint is unreachable or the stream stalls.

    Wraps urllib/OSError/socket failures so callers see one boundary type
    they can convert into a clean task_error frame instead of letting the
    raw exception bubble through the task_worker. The original cause is
    preserved via ``__cause__`` for log greppability.
    """


def _curl_post_messages(
    url: str,
    api_key: str,
    body: bytes,
    timeout: int,
    *,
    runner=None,
) -> dict[str, Any]:
    """Last-resort POST through the system `curl` binary.

    Windows Defender's TLS MITM occasionally mangles the handshake for
    `urlopen` (Python's bundled OpenSSL) but leaves Schannel-backed curl
    alone. Windows 10+ (1803+) ships `curl.exe` in `System32`. Linux/mac
    have it as a system package.

    Returns the parsed JSON response — same shape `post_llm` would have
    produced. Raises `LlmNetworkError` when curl is missing or non-zero,
    so the caller surfaces a clean task_error instead of crashing the
    worker. NOTE: this is the non-streaming path (we drop `stream=true`
    from the payload before calling so curl can return a single JSON
    blob; the SSE streaming path stays on urllib).

    `runner` is injectable for tests so we don't shell out for real.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            [
                "curl",
                "-sS",
                "--max-time",
                str(max(int(timeout), 5)),
                "-X",
                "POST",
                "-H",
                f"x-api-key: {api_key}",
                "-H",
                "content-type: application/json",
                "-H",
                "anthropic-version: 2023-06-01",
                "-H",
                "user-agent: agentflow-desktop/0.2-curl",
                "--data-binary",
                "@-",
                url,
            ],
            input=body,
            capture_output=True,
            check=False,
            timeout=max(int(timeout) + 5, 10),
        )
    except FileNotFoundError as exc:
        raise LlmNetworkError(f"curl missing: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LlmNetworkError(f"curl timeout after {timeout}s") from exc
    except Exception as exc:  # noqa: BLE001
        raise LlmNetworkError(f"curl subprocess failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise LlmNetworkError(f"curl rc={proc.returncode}: {stderr}")
    stdout = proc.stdout or b""
    if not stdout:
        raise LlmNetworkError("curl returned empty body")
    try:
        return json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise LlmNetworkError(
            f"curl returned non-JSON: {stdout[:200]!r}"
        ) from exc


def post_llm_cancellable(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    abort_flag: Any,
    timeout: int = DEFAULT_LLM_CONNECT_TIMEOUT_S,
    poll_interval: float = 0.2,
    stream_idle_timeout: int = DEFAULT_LLM_STREAM_IDLE_TIMEOUT_S,
) -> dict[str, Any]:
    """POST /v1/messages with stream=true and tear the connection down
    within ~poll_interval seconds when ``abort_flag`` fires.

    The Anthropic SDK has no first-class cancel; the urllib socket does.
    We register a watchdog thread that closes the response object the
    instant the flag is set, which surfaces as a read error on the main
    thread; we then translate it to ``TaskCancelled``.

    The watchdog also enforces a per-chunk inactivity cap: if the SSE
    stream stays silent for ``stream_idle_timeout`` seconds, close the
    socket so the next ``resp.read`` raises and we surface a clean
    ``LlmNetworkError`` instead of waiting out the full timeout.

    On normal completion, the SSE event list is folded back into the
    standard /v1/messages response shape so the caller sees the same
    ``{content: [...], stop_reason, ...}`` dict it would have seen from
    the blocking ``post_llm``.
    """
    streamed = dict(payload)
    streamed["stream"] = True
    body = json.dumps(streamed).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "user-agent": "agentflow-desktop/0.2",
            "accept": "text/event-stream",
        },
    )

    # Default urlopen на Windows иногда срывает TLS handshake с
    # `EOF occurred in violation of protocol (_ssl.c:2427)` — особенно
    # когда CA bundle Python'а отстаёт от Cloudflare-pin'а или антивирус
    # MITM-итает трафик. Принудительный TLS 1.2 minimum + system trust
    # store даёт более устойчивый handshake.
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    except Exception:  # noqa: BLE001
        ctx = None  # fallback на default behaviour

    def _open_with_retry():
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                if ctx is not None:
                    return urllib.request.urlopen(req, timeout=timeout, context=ctx)
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc_inner:
                last_exc = exc_inner
                msg = str(exc_inner)
                # Retry один раз на «EOF occurred in violation of protocol»
                # (transient TLS reset Cloudflare/MITM) или 10060 timeout.
                if attempt == 0 and (
                    "EOF occurred in violation" in msg
                    or "WinError 10060" in msg
                    or "_ssl.c" in msg
                ):
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    try:
        resp = _open_with_retry()
    except urllib.error.HTTPError:
        # 4xx/5xx — caller wants to read the body to surface a useful
        # message, so propagate the structured exception unchanged.
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Connect-phase failure: no DNS, no SYN-ACK, TLS handshake stall,
        # or the dreaded `WinError 10060` on a NAT'd network. Before we
        # give up, try the system `curl` binary — Schannel-backed curl
        # often survives a Defender MITM that mangles Python's OpenSSL
        # handshake. Drop `stream=true` so curl returns one JSON blob
        # we can fold straight back into the caller's expected shape.
        nonstream_payload = dict(payload)
        nonstream_payload.pop("stream", None)
        nonstream_body = json.dumps(nonstream_payload).encode()
        try:
            result = _curl_post_messages(
                url, api_key, nonstream_body, timeout=timeout
            )
        except LlmNetworkError as curl_exc:
            raise LlmNetworkError(
                f"llm connect failed (urlopen + curl fallback): "
                f"urlopen={exc} curl={curl_exc}"
            ) from exc
        # Curl succeeded — we have a complete non-stream response. Skip
        # the SSE assembly path and return immediately.
        if abort_flag.is_set():
            raise TaskCancelled() from None
        return result

    # Watchdog: closes the response socket within poll_interval of an
    # abort, or after stream_idle_timeout of silence on the stream.
    import threading as _th

    stop_watch = _th.Event()
    aborted = _th.Event()
    idle_timeout_fired = _th.Event()
    last_byte_ts = [time.monotonic()]
    idle_deadline_s = max(int(stream_idle_timeout or 0), 0)

    def _watch() -> None:
        while not stop_watch.is_set():
            if abort_flag.is_set():
                aborted.set()
                with contextlib.suppress(Exception):
                    resp.close()
                return
            if idle_deadline_s > 0 and (
                time.monotonic() - last_byte_ts[0] > idle_deadline_s
            ):
                idle_timeout_fired.set()
                with contextlib.suppress(Exception):
                    resp.close()
                return
            stop_watch.wait(poll_interval)

    watcher = _th.Thread(target=_watch, name="llm-cancel-watch", daemon=True)
    watcher.start()

    events: list[dict[str, Any]] = []
    current_event: str | None = None
    buffer = b""
    try:
        # Read SSE line by line. We do not trust the upstream to flush
        # promptly, but Anthropic streaming flushes per event which gives
        # us ~10-50 ms granularity in practice.
        while True:
            if abort_flag.is_set():
                raise TaskCancelled()
            try:
                chunk = resp.read(4096)
            except Exception as exc:  # noqa: BLE001
                if aborted.is_set() or abort_flag.is_set():
                    raise TaskCancelled() from exc
                if idle_timeout_fired.is_set():
                    raise LlmNetworkError(
                        f"llm stream idle for {idle_deadline_s}s"
                    ) from exc
                # Any other read failure (RST, TLS error mid-stream) is
                # a network problem from the daemon's point of view.
                raise LlmNetworkError(f"llm stream read failed: {exc}") from exc
            if not chunk:
                break
            last_byte_ts[0] = time.monotonic()
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                s = line.decode("utf-8", errors="replace").rstrip("\r")
                if not s:
                    current_event = None
                    continue
                if s.startswith(":"):
                    continue  # SSE comment / keepalive
                if s.startswith("event:"):
                    current_event = s[len("event:"):].strip()
                    continue
                if s.startswith("data:"):
                    data = s[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if current_event and "type" not in ev:
                        ev["type"] = current_event
                    events.append(ev)
                    if ev.get("type") == "error":
                        return {"type": "error", "error": ev.get("error") or ev}
    finally:
        stop_watch.set()
        with contextlib.suppress(Exception):
            resp.close()

    if abort_flag.is_set():
        raise TaskCancelled()
    return _assemble_anthropic_sse(events)


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


def _build_memory_block(task: str, lesson_limit: int = 6, skill_limit: int = 4) -> str:
    """Pre-task recall: top lessons + skills relevant to this task text.

    Always returns a string (possibly empty). Any error in the memory layer
    is swallowed — a missing/locked SQLite file must never block task
    execution. Each lesson summary is capped at 200 chars and each skill
    recipe at 400 to keep the system prompt tight; the cap matches the brief
    so prompt size stays bounded regardless of memory size.
    """
    if not task or not task.strip():
        return ""
    try:
        from ..autonomous import memory as _memory
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] memory import failed: {exc}", flush=True)
        return ""

    try:
        lessons = _memory.recall(topic=task, limit=lesson_limit)
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] memory.recall failed: {exc}", flush=True)
        lessons = []
    try:
        skills = _memory.top_skills(when_to_use_query=task, limit=skill_limit)
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] memory.top_skills failed: {exc}", flush=True)
        skills = []

    if not lessons and not skills:
        return ""

    chunks: list[str] = []
    if lessons:
        chunks.append("\nПрошлый опыт (newest-first, релевантные уроки):")
        for row in lessons:
            summary = (row.get("summary") or "").strip().replace("\n", " ")
            if len(summary) > 200:
                summary = summary[:197] + "…"
            topic = (row.get("topic") or "").strip()[:60]
            chunks.append(f"  • [{topic}] {summary}")
    if skills:
        chunks.append("\nИзвестные навыки (применяй когда совпадает):")
        for row in skills:
            name = (row.get("name") or "").strip()
            when = (row.get("when_to_use") or "").strip()[:80]
            recipe_raw = row.get("recipe_json") or "{}"
            try:
                recipe = json.loads(recipe_raw) if isinstance(recipe_raw, str) else recipe_raw
            except (json.JSONDecodeError, TypeError):
                recipe = {}
            recipe_str = json.dumps(recipe, ensure_ascii=False)
            if len(recipe_str) > 400:
                recipe_str = recipe_str[:397] + "…"
            chunks.append(f"  • {name} ({when}) → {recipe_str}")
    return "\n".join(chunks) + "\n"


def _budget_record_llm(model: str, usage: dict[str, Any]) -> float:
    """Record an LLM call against the budget ledger.

    Returns the estimated USD spend for this call, or 0.0 on any error.
    Soft-fails: a locked DB or missing schema must not crash the loop.
    """
    try:
        from ..autonomous import budget as _budget
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] budget import failed: {exc}", flush=True)
        return 0.0
    try:
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        return float(_budget.record_llm_cost(model, in_tok, out_tok))
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] budget.record_llm_cost failed: {exc}", flush=True)
        return 0.0


def _usage_total_tokens(usage: dict[str, Any]) -> int:
    """Best-effort total tokens across one LLM response payload."""
    try:
        return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    except Exception:
        return 0


def _memory_save_outcome(
    task: str,
    *,
    success: bool,
    steps: int,
    tools_used: list[str],
    answer: str,
    abandon_reason: str | None = None,
) -> None:
    """Persist a task_outcome lesson + auto-skill if a streak emerged.

    Soft-fails: storage problems must not surface to the user. Score is a
    crude 1-10 heuristic — successful short task = high, abandoned task =
    low, long-but-successful task = medium-high.
    """
    if not task or not task.strip():
        return
    try:
        from ..autonomous import memory as _memory
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] memory import failed in save: {exc}", flush=True)
        return

    if success:
        if steps <= 5:
            score = 9
        elif steps <= 15:
            score = 7
        else:
            score = 5
    else:
        score = 2

    summary_parts: list[str] = []
    if success:
        summary_parts.append(f"completed in {steps} steps")
        if answer:
            ans = answer.strip().replace("\n", " ")
            if len(ans) > 140:
                ans = ans[:137] + "…"
            summary_parts.append(f"answer: {ans}")
    else:
        summary_parts.append(f"abandoned after {steps} steps")
        if abandon_reason:
            reason = abandon_reason.strip().replace("\n", " ")
            if len(reason) > 140:
                reason = reason[:137] + "…"
            summary_parts.append(f"reason: {reason}")
    summary = "; ".join(summary_parts)

    try:
        _memory.learn(
            kind="task_outcome",
            topic=task[:200],
            summary=summary,
            payload={
                "steps": int(steps),
                "success": bool(success),
                "tools_used": list(tools_used)[:50],
                "abandon_reason": abandon_reason,
            },
            score=int(score),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] memory.learn failed: {exc}", flush=True)

    # Auto-skill: only when the task succeeded AND we saw a clean streak of
    # ≥4 consecutive tool calls. Streak is detected from `tools_used` order —
    # any 4 identical-or-not tools in a row without `__error__` markers count.
    if not success or len(tools_used) < 4:
        return
    streak: list[str] = []
    best_streak: list[str] = []
    for tool in tools_used:
        if tool.startswith("__"):  # synthetic markers we may add later
            streak = []
            continue
        streak.append(tool)
        if len(streak) > len(best_streak):
            best_streak = list(streak)
    if len(best_streak) < 4:
        return
    # Name: first 40 chars of task + step count, deduped via UPSERT on name.
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in task.lower()[:40])
    auto_name = f"auto:{base}:{len(best_streak)}"
    recipe = [{"tool": t} for t in best_streak]
    try:
        _memory.record_skill(
            name=auto_name,
            when_to_use=task[:80],
            recipe={"steps": recipe},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] memory.record_skill failed: {exc}", flush=True)


def _request_checkpoint(
    llm_url: str,
    api_key: str,
    model: str,
    system_msg: str,
    messages: list[dict[str, Any]],
    abort_flag: Any,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Inject a synthetic reflection turn and ask the LLM if it's on track.

    Returns a dict ``{on_track: bool, next_step: str, abandon_reason: str|None,
    usage: {...}}``. On any error returns ``{on_track: True, ...}`` — a broken
    checkpoint must not abort an otherwise-healthy task.
    """
    fallback = {"on_track": True, "next_step": "", "abandon_reason": None, "usage": {}}
    reflection_prompt = (
        "Сейчас сделано несколько шагов из задачи. Кратко: ты ещё на пути к цели "
        "или потерялся? Ответь СТРОГО валидным JSON-объектом без markdown: "
        '{"on_track": true/false, "next_step": "одна строка", '
        '"abandon_reason": null или "почему бросаешь"}'
    )
    probe_messages = list(messages) + [
        {"role": "user", "content": [{"type": "text", "text": reflection_prompt}]}
    ]
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 256,
        "system": system_msg,
        "messages": probe_messages,
    }
    body = _augment_body_with_reasoning(body, model, reasoning_effort)
    try:
        resp = post_llm_cancellable(llm_url, api_key, body, abort_flag)
    except Exception as exc:  # noqa: BLE001
        print(f"[loop] checkpoint LLM error: {exc}", flush=True)
        return fallback
    if resp.get("type") == "error":
        return fallback

    text_blocks = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
    raw = " ".join(t for t in text_blocks if t).strip()
    # Strip code fences if the model wrapped JSON in ```json … ```
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    # Find first { ... } to tolerate leading text.
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {**fallback, "usage": resp.get("usage", {})}
    return {
        "on_track": bool(parsed.get("on_track", True)),
        "next_step": str(parsed.get("next_step") or "")[:240],
        "abandon_reason": parsed.get("abandon_reason") or None,
        "usage": resp.get("usage", {}),
    }


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


def _tool_failure_reason(tool_name: str, out: Any) -> str | None:
    """Return a concise failure reason when a tool output represents failure."""
    if tool_name == "task_complete":
        return None
    if not isinstance(out, str):
        return None
    stripped = out.strip()
    lower = stripped.lower()
    if lower.startswith("error:") or lower.startswith("firefox error:") or lower.startswith("unknown tool:"):
        return stripped[:200]
    try:
        parsed = json.loads(stripped)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("ok") is False:
        detail = str(parsed.get("error") or stripped)
        return detail[:200]
    exit_code = parsed.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        detail = str(parsed.get("stderr") or "").strip() or f"exit_code={exit_code}"
        return detail[:200]
    return None


def _tool_is_observation_only(tool_name: str) -> bool:
    if tool_name.startswith("af_list_") or tool_name.startswith("af_get_"):
        return True
    return tool_name in {
        "af_telegram_dialogs",
        "af_telegram_messages",
        "af_telegram_search",
        "af_telegram_whoami",
        "browser_eval",
        "browser_snapshot",
        "chrome_eval",
        "chrome_tabs",
        "code_list_dir",
        "code_read_file",
        "goal_list",
        "goal_show",
        "list_windows",
        "read_terminal",
        "screen_capture",
        "screen_record_status",
        "screen_region",
        "winget_search",
    }


def _content_has_image(blocks: list[dict[str, Any]]) -> bool:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            return True
        nested = block.get("content")
        if isinstance(nested, list) and _content_has_image(nested):
            return True
    return False


def _compact_content_images(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    compacted: list[dict[str, Any]] = []
    removed_any = False
    for block in blocks:
        if not isinstance(block, dict):
            compacted.append(block)
            continue
        if block.get("type") == "image":
            removed_any = True
            continue
        nested = block.get("content")
        if isinstance(nested, list):
            nested_compacted, nested_removed = _compact_content_images(nested)
            if nested_removed:
                removed_any = True
                block = {**block, "content": nested_compacted}
        compacted.append(block)
    if removed_any:
        compacted.append(
            {
                "type": "text",
                "text": "[earlier screenshot omitted from history to bound memory]",
            }
        )
    return compacted, removed_any


def _compact_message_history(messages: list[dict[str, Any]]) -> None:
    image_message_indexes: list[int] = []
    for idx, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if _content_has_image(content):
            image_message_indexes.append(idx)

    keep = set(image_message_indexes[-HISTORY_IMAGE_KEEP_RECENT_MESSAGES:])
    for idx in image_message_indexes:
        if idx in keep:
            continue
        content = messages[idx].get("content")
        if not isinstance(content, list):
            continue
        compacted, removed_any = _compact_content_images(content)
        if not removed_any:
            continue
        messages[idx]["content"] = compacted


def _task_complete_answer_indicates_failure(answer: str) -> bool:
    text = answer.strip().lower()
    if not text:
        return False
    failure_markers = (
        "❌",
        "задача не выполн",
        "не выполнена",
        "не удалось",
        "невозможно",
        "заблокирован",
        "cannot ",
        "can't ",
        "failed",
        "blocked",
        "unable to",
    )
    return any(marker in text for marker in failure_markers)


def run_task(
    task: str,
    state: DriverState,
    executor: ToolExecutor,
    api_key: str,
    *,
    llm_url: str = DEFAULT_LLM_URL,
    model: str = DEFAULT_MODEL,
    max_iters: int = MAX_ITERS,
    max_usd: float = LOOP_MAX_USD,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
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

    # Pre-task recall: append the «Прошлый опыт» / «Известные навыки» block
    # for THIS task only. Soft-fails to empty string when memory is missing.
    memory_block = _build_memory_block(task)
    if memory_block:
        system_msg = f"{system_msg}\n{memory_block}"

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

    def _emit_cancel() -> str:
        state.abort_flag.clear()
        # Publish task_error FIRST so the cabinet flips to "stopped" within
        # milliseconds. update_live (which does a fresh screenshot for the
        # viewer JPEG) can take 2-5 s on macOS and must not block the WS frame.
        if state.current_task_id:
            state.publish_outbound(
                {
                    "type": "task_error",
                    "task_id": state.current_task_id,
                    "error": "cancelled_by_user",
                }
            )
        print("\n=== CANCELLED ===", flush=True)
        # Best-effort viewer update — never let a slow screencapture stall the
        # cancel path. Run in a daemon thread so this function returns now.
        import threading as _th_local

        _th_local.Thread(
            target=update_live,
            args=(state, "cancelled", "task cancelled by user"),
            daemon=True,
            name="cancel-update-live",
        ).start()
        return ""

    final_answer = ""
    iterations = 0
    tool_calls_count = 0
    tools_used: list[str] = []
    total_cost_usd = 0.0
    total_tokens_used = 0
    last_checkpoint_at = 0
    abandon_reason: str | None = None
    unresolved_tool_error: str | None = None
    budget_finalization_turn_allowed = False

    def _emit_abort(reason: str, kind: str) -> str:
        """Emit task_error + persist outcome lesson, then return.

        Used by the new caps (step / cost / abandon). Keeps WS frame parity
        with _emit_cancel: same `task_error` type so the cabinet shows the
        run as ended, just with a different `error` string for diagnosis.
        """
        update_live(state, kind, reason)
        print(f"\n=== {kind.upper()} === {reason}", flush=True)
        _memory_save_outcome(
            task,
            success=False,
            steps=tool_calls_count,
            tools_used=tools_used,
            answer="",
            abandon_reason=reason,
        )
        if state.current_task_id:
            state.publish_outbound(
                {
                    "type": "task_error",
                    "task_id": state.current_task_id,
                    "error": reason,
                }
            )
        return ""

    for i in range(max_iters):
        # Iteration-boundary check: fast path for tasks cancelled while idle
        # between iterations. The mid-stream and pre-tool-dispatch checks
        # below cover the long-pole cases.
        if state.abort_flag.is_set():
            return _emit_cancel()

        in_budget_finalization_turn = total_cost_usd >= max_usd and budget_finalization_turn_allowed
        if total_cost_usd >= max_usd and not in_budget_finalization_turn:
            return _emit_abort(
                f"cost_cap_exceeded: spent ${total_cost_usd:.4f} >= ${max_usd:.2f}",
                "cost_cap",
            )
        budget_finalization_turn_allowed = False

        iterations = i + 1
        print(f"\n--- iter {iterations} ---", flush=True)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 1024,
            "system": system_msg,
            "tools": tools,
            "messages": messages,
        }
        body = _augment_body_with_reasoning(body, model, reasoning_effort)
        try:
            resp = post_llm_cancellable(llm_url, api_key, body, state.abort_flag)
        except TaskCancelled:
            return _emit_cancel()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:300]
            print(f"http {exc.code}: {body}", flush=True)
            update_live(state, "error", f"llm http {exc.code}: {body}")
            if state.current_task_id:
                state.publish_outbound(
                    {
                        "type": "task_error",
                        "task_id": state.current_task_id,
                        "error": f"llm_http_{exc.code}: {body[:160]}",
                    }
                )
            return ""
        except LlmNetworkError as exc:
            # Surfaces fast (≤ AF_LLM_CONNECT_TIMEOUT_S or stream-idle cap)
            # instead of blocking the task_worker for the full urllib
            # default. Print + log + WS frame so the cabinet flips off the
            # "running" spinner instead of waiting for the backend reaper.
            reason = f"llm_unreachable: {exc}"
            print(reason, flush=True)
            update_live(state, "error", reason)
            if state.current_task_id:
                state.publish_outbound(
                    {
                        "type": "task_error",
                        "task_id": state.current_task_id,
                        "error": reason[:240],
                    }
                )
            return ""
        if resp.get("type") == "error":
            update_live(state, "error", f"api error: {resp}")
            return ""

        usage = resp.get("usage") or {}
        total_cost_usd += _budget_record_llm(model, usage)
        total_tokens_used += _usage_total_tokens(usage)

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
            if unresolved_tool_error:
                return _emit_abort(
                    f"completion_blocked_after_tool_error: {unresolved_tool_error}",
                    "completion_blocked",
                )
            # Persist a success outcome — the model finished without further tools.
            _memory_save_outcome(
                task,
                success=True,
                steps=tool_calls_count,
                tools_used=tools_used,
                answer=final_answer or thinking,
            )
            return final_answer

        # Once we enter the single grace turn after hitting the budget cap,
        # only a terminal task_complete is allowed. Any further non-terminal
        # tool work gets blocked before the executor mutates more state.
        if in_budget_finalization_turn and any(tu.get("name") != "task_complete" for tu in tool_uses):
            return _emit_abort(
                f"cost_cap_exceeded: spent ${total_cost_usd:.4f} >= ${max_usd:.2f}",
                "cost_cap",
            )

        messages.append({"role": "assistant", "content": content})
        results: list[dict[str, Any]] = []
        done = False
        hit_step_cap = False
        for tu in tool_uses:
            # Pre-dispatch abort gate: covers the case where cancel arrives
            # while the LLM was responding and we already started iterating
            # over its tool_use blocks.
            if state.abort_flag.is_set():
                return _emit_cancel()
            if tool_calls_count >= LOOP_MAX_STEPS:
                hit_step_cap = True
                break
            args_preview = json.dumps(tu["input"], ensure_ascii=False)[:160]
            print(f"  → {tu['name']}({args_preview})", flush=True)
            try:
                out, image = executor.execute(tu["name"], tu.get("input", {}))
            except Exception as exc:  # noqa: BLE001
                out, image = f"error: {exc}", None
            tool_calls_count += 1
            tools_used.append(tu["name"])
            preview = (out[:240] if isinstance(out, str) else str(out)[:240]).replace("\n", " | ")
            print(f"    = {preview}", flush=True)
            update_live(
                state,
                tu["name"],
                f"args: {args_preview}\nresult: {out[:600] if isinstance(out, str) else out}",
            )
            failure = _tool_failure_reason(tu["name"], out)
            if failure:
                unresolved_tool_error = f"{tu['name']}: {failure}"[:240]
            elif tu["name"] != "task_complete" and not _tool_is_observation_only(tu["name"]):
                unresolved_tool_error = None
            if out == "__DONE__":
                final_answer = tu["input"].get("answer", "")
                if unresolved_tool_error:
                    return _emit_abort(
                        f"completion_blocked_after_tool_error: {unresolved_tool_error}",
                        "completion_blocked",
                    )
                if _task_complete_answer_indicates_failure(final_answer):
                    return _emit_abort(
                        f"task_complete_reported_failure: {final_answer[:200]}",
                        "completion_blocked",
                    )
                done = True
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
        _compact_message_history(messages)

        if hit_step_cap:
            return _emit_abort(
                f"step_cap_exceeded: ran {tool_calls_count} tool calls, cap={LOOP_MAX_STEPS}",
                "step_cap",
            )

        if done:
            update_live(state, "DONE", final_answer)
            print(f"\n=== DONE ===\n{final_answer}", flush=True)
            _memory_save_outcome(
                task,
                success=True,
                steps=tool_calls_count,
                tools_used=tools_used,
                answer=final_answer,
            )
            if state.current_task_id:
                state.publish_outbound(
                    {
                        "type": "task_complete",
                        "task_id": state.current_task_id,
                        "answer": final_answer,
                        "iterations": iterations,
                        "tokens_used": total_tokens_used,
                        "cost_usd": round(total_cost_usd, 6),
                    }
                )
            return final_answer

        if total_cost_usd >= max_usd:
            budget_finalization_turn_allowed = True

        # Checkpoint reflection: fires every LOOP_CHECKPOINT_EVERY tool calls,
        # but skips when the task is too short to warrant the overhead. The
        # check runs AFTER the tools dispatch + their results are folded into
        # messages so the synthetic reflection turn sees the actual progress.
        if (
            LOOP_CHECKPOINT_EVERY > 0
            and tool_calls_count >= CHECKPOINT_MIN_STEPS
            and tool_calls_count - last_checkpoint_at >= LOOP_CHECKPOINT_EVERY
        ):
            last_checkpoint_at = tool_calls_count
            print(f"  ⟳ checkpoint @ step {tool_calls_count}", flush=True)
            check = _request_checkpoint(
                llm_url,
                api_key,
                model,
                system_msg,
                messages,
                state.abort_flag,
                reasoning_effort=reasoning_effort,
            )
            check_usage = check.get("usage") or {}
            total_cost_usd += _budget_record_llm(model, check_usage)
            total_tokens_used += _usage_total_tokens(check_usage)
            on_track = bool(check.get("on_track", True))
            next_step = (check.get("next_step") or "").strip()
            reason = check.get("abandon_reason")
            if not on_track and reason:
                abandon_reason = str(reason)[:200]
                update_live(state, "task.abandon", abandon_reason)
                # Mirror the standard cancel/error path: emit task_error so
                # the cabinet flips to "stopped" with a diagnostic reason.
                return _emit_abort(
                    f"task.abandon: {abandon_reason}",
                    "task.abandon",
                )
            if on_track and next_step:
                update_live(state, "checkpoint", f"on track → {next_step}")
                # Inject the hint as a user-side note so the next iteration
                # sees it without polluting the assistant turn.
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"(checkpoint: продолжай — следующий шаг: {next_step})",
                            }
                        ],
                    }
                )

    update_live(state, "max_iters", f"reached {max_iters}")
    print("\n=== max iters ===", flush=True)
    _memory_save_outcome(
        task,
        success=False,
        steps=tool_calls_count,
        tools_used=tools_used,
        answer=final_answer,
        abandon_reason=f"max_iters reached ({max_iters})",
    )
    if state.current_task_id:
        state.publish_outbound(
            {
                "type": "task_error",
                "task_id": state.current_task_id,
                "error": f"max_iters reached ({max_iters})",
            }
        )
    return final_answer


def _normalize_task_entry(entry: Any) -> tuple[str, str, dict[str, Any] | None]:
    """Accept legacy str entries and newer queue tuples uniformly."""
    if isinstance(entry, tuple):
        if len(entry) == 3:
            return str(entry[0]), str(entry[1]), entry[2] if isinstance(entry[2], dict) else None
        if len(entry) == 2:
            return str(entry[0]), str(entry[1]), None
    return f"local-{int(time.time() * 1000)}", str(entry), None


def task_worker(
    state: DriverState,
    executor: ToolExecutor,
    api_key: str,
    *,
    llm_url: str = DEFAULT_LLM_URL,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> None:
    """Blocking loop: pull tasks off the queue, run them sequentially."""
    update_live(state, "idle", "ожидаю задачу из чат-инпута")
    while not state.shutdown_flag.is_set():
        try:
            raw = state.task_queue.get(timeout=1)
        except Exception:  # noqa: BLE001 — queue.Empty
            if not state.busy and (int(time.time()) % 60 == 0):
                update_live(state, "idle", "ожидаю задачу")
            continue
        task_id, task, task_scope = _normalize_task_entry(raw)
        state.busy = True
        state.current_task = task
        state.current_task_id = task_id
        # Forward-visibility heartbeat: publish a task_action the moment the
        # worker dequeues a task, before any LLM / tool work runs. Without
        # this, the platform sees `task_dispatch` go out but has nothing on
        # the wire back until the LLM produces its first turn. If the LLM
        # call crashes silently or the daemon image lacks an api key, the
        # platform's `device-task-reaper` flips the row to `failed` 15 min
        # later with no diagnostic. Best-effort — never let a publish error
        # block the worker.
        if state.outbound_publisher is not None:
            try:
                state.publish_outbound(
                    {
                        "type": "task_action",
                        "task_id": task_id,
                        "ts": int(time.time() * 1000),
                        "action": "task_dequeued",
                        "detail": (task or "")[:160],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[task_worker] task_dequeued publish failed: {exc}", flush=True)
        try:
            executor.apply_task_scope(task_scope)
            # Precedence: per-task scope.budget_usd > base scope (computer-scope.toml)
            # > LOOP_MAX_USD env default. The base scope value is the owner's
            # global ceiling — if it's higher than the env default, trust it.
            base_budget = float(getattr(executor.base_scope, "budget_usd", 0) or 0)
            max_usd = max(LOOP_MAX_USD, base_budget) if base_budget > 0 else LOOP_MAX_USD
            # Per-task scope.model / scope.reasoning_effort win over the
            # daemon-wide defaults. Useful for cabinet-side overrides — eg
            # a coding task can ask for `gpt-5.3-codex` + `high` while
            # casual chats stay on `medium`.
            run_model = model
            run_reasoning = reasoning_effort
            if isinstance(task_scope, dict):
                raw_budget = task_scope.get("budget_usd")
                if isinstance(raw_budget, (int, float)) and raw_budget > 0:
                    max_usd = float(raw_budget)
                scope_model = task_scope.get("model")
                if isinstance(scope_model, str) and scope_model.strip():
                    run_model = scope_model.strip()
                scope_reasoning = task_scope.get("reasoning_effort")
                if isinstance(scope_reasoning, str) and scope_reasoning.strip():
                    run_reasoning = scope_reasoning.strip()
            run_task(
                task,
                state,
                executor,
                api_key,
                llm_url=llm_url,
                model=run_model,
                max_usd=max_usd,
                reasoning_effort=run_reasoning,
            )
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
            executor.reset_task_scope()
            state.busy = False
            state.current_task = ""
            state.current_task_id = ""
            state.task_count += 1
    update_live(state, "shutdown", "daemon worker stopped")
