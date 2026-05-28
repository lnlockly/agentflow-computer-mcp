"""Aider-driven project bootstrap for hosted daemons.

Phase A4 of the project-architecture refactor. Backend picks any repo
that matches the user's brief, daemon clones it, and an `aider` CLI
session takes over: it edits code to match the brief, then exits. The
daemon then spawns the dev server (or Python bot) itself. The user
watches it happen live in `/cabinet/devices/<id>/live` while aider
prints diffs + chat into the daemon's task action log.

Replaced opencode-ai 2026-05-28 because opencode's built-in iteration
cap stalled multi-step briefs mid-todo-list. Aider has no vendor cap +
`--yes-always` skips confirmations + `--message` runs one-shot
non-interactively. Tool name (`agent_dev_brief`) stays unchanged so
the backend WS dispatcher does not need to know about the swap.

Why this shape:

* The backend has no business knowing how to install Node/pnpm/Bun or
  pick a dev command. The daemon does, per repo, after aider exits.
* The brief is the source of truth — aider takes it as the user's ask
  and produces a working app. We don't pre-bake Next-only assumptions.
* We return immediately and let aider run as a long-lived background
  process; status is surfaced through the dev-server port probe + the
  daemon's existing `device_action_log` stream.

Design rules mirror ``project_setup.py``:
* Every side-effect goes through an injectable callable for tests.
* Stable error codes in ``{ok: false, error: "..."}`` so the cabinet UI
  can map reasons to copy without parsing English.
* Secrets never appear in the returned dict.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..resolve_runtimes import resolve_runtimes
from .project_setup import (
    DEFAULT_PORT,
    DEFAULT_WORKSPACE_ROOT,
    _default_run,
    _looks_like_repo_full,
    _looks_like_slug,
)

log = logging.getLogger(__name__)


def _normalise_api_base(raw: str) -> str:
    """Strip trailing slash and append ``/_agents`` when missing.

    The public ingress mounts agentflow-agents under ``/_agents/*``. Daemon
    code paths that hit ``${AF_API_URL}/internal/...`` or
    ``${AF_API_URL}/llm/...`` directly will 404 against the bare host. The
    sibling ``server.py`` + ``desktop_tools.py`` already do this — keeping
    one local helper avoids re-importing across modules.
    """
    base = (raw or "https://agentflow.website").rstrip("/")
    if not base.endswith("/_agents"):
        base = base + "/_agents"
    return base


AIDER_PID_FILE = "/tmp/agent-brief-aider.pid"
AIDER_LOG_FILE = "/tmp/agent-brief-aider.log"

# Back-compat aliases — some tests + the WS dispatcher still reference the
# old names. Removed once the rename has rolled fully.
OPENCODE_PID_FILE = AIDER_PID_FILE
OPENCODE_LOG_FILE = AIDER_LOG_FILE


def rewrite_dev_command_for_port(command: str, port: int) -> str:
    """Rewrite a package.json `scripts.dev` value to honour ``$PORT``.

    Pure function: no I/O, no exceptions outside ``re``. The four
    rules below cover every dev-server we ship templates for. An
    unrecognised command is returned untouched — opencode can still
    edit it manually, but the deterministic path stops here.

    `${PORT:-N}` is POSIX parameter expansion: it uses ``$PORT`` when
    set, otherwise the original literal port. npm executes scripts
    through ``sh -c`` on Linux + macOS, so this always works in our
    hosted daemon pods.
    """
    import re

    raw = (command or "").strip()
    if not raw:
        return command

    # serve (v14): `serve -l 3000 -L .` → `serve -l ${PORT:-3000} -L .`
    m = re.search(r"\bserve\b[^&|;]*?-l\s+(\d+)", raw)
    if m:
        return raw.replace(m.group(0), m.group(0).replace(m.group(1), f"${{PORT:-{m.group(1)}}}"), 1)
    if re.search(r"\bserve\b", raw) and "-l" not in raw:
        # `serve .` style — append explicit listen flag.
        return raw + f" -l ${{PORT:-{port}}}"

    # next dev: `next dev` (with or without -p N) → `next dev -p ${PORT:-N} -H 0.0.0.0`
    if re.search(r"\bnext\s+dev\b", raw):
        # Strip any existing -p / --port / -H flags, then re-add normalised.
        cleaned = re.sub(r"\s+-p\s+\S+", "", raw)
        cleaned = re.sub(r"\s+--port[=\s]\S+", "", cleaned)
        cleaned = re.sub(r"\s+-H\s+\S+", "", cleaned)
        cleaned = re.sub(r"\s+--hostname[=\s]\S+", "", cleaned)
        return f"{cleaned} -p ${{PORT:-{port}}} -H 0.0.0.0"

    # vite: `vite` (with or without --port N) → `vite --port ${PORT:-N} --host 0.0.0.0`
    if re.search(r"\bvite\b", raw) and "preview" not in raw:
        cleaned = re.sub(r"\s+--port[=\s]\S+", "", raw)
        cleaned = re.sub(r"\s+--host[=\s]\S+", "", cleaned)
        return f"{cleaned} --port ${{PORT:-{port}}} --host 0.0.0.0"

    # python http.server: `python -m http.server 3000` → `… ${PORT:-3000}`
    m = re.search(r"http\.server\s+(\d+)", raw)
    if m:
        return raw.replace(m.group(0), f"http.server ${{PORT:-{m.group(1)}}}", 1)

    return raw


def _patch_package_json_for_port(pkg_path: Path, port: int) -> None:
    """Edit ``scripts.dev`` (and ``scripts.start`` when ``dev`` is missing)
    so the dev server binds to ``$PORT``. Silent no-op if the file is
    absent, malformed, or the scripts block does not exist — the brief
    still asks opencode to act, and an exotic template will fail loudly
    later in the cabinet log rather than corrupt the daemon state.
    """
    try:
        raw = pkg_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return
    touched = False
    for key in ("dev", "start"):
        value = scripts.get(key)
        if isinstance(value, str) and value.strip():
            new_value = rewrite_dev_command_for_port(value, port)
            if new_value != value:
                scripts[key] = new_value
                touched = True
    if not touched:
        return
    try:
        pkg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not rewrite %s: %s", pkg_path, exc)


def _default_spawn_aider(
    brief: str,
    cwd: str,
    *,
    pid_file: str = AIDER_PID_FILE,
    log_file: str = AIDER_LOG_FILE,
    env: dict[str, str] | None = None,
    aider_bin: str = "aider",
    # Back-compat alias — older callers (server.py + tests) pass
    # `opencode_bin=`. Both names accepted; aider_bin wins when set.
    opencode_bin: str | None = None,
) -> dict[str, Any]:
    """Spawn `aider --message "<brief>"` as a detached background process.

    Aider's ``--message`` mode is one-shot non-interactive: it edits files
    end-to-end and exits. We capture stdout into ``log_file`` so the
    daemon can stream it to ``device_action_log``.

    Flags rationale:
      * ``--yes-always`` — skip every confirmation. Pod boundary already
        sandboxes the agent; in-loop prompts hang the spawn forever.
      * ``--no-pretty`` — plain output, no ANSI rewrites. agent_log reads
        line-by-line; pretty mode rewrites lines in place and confuses
        the tailer.
      * ``--no-auto-commits`` — daemon strips ``.git`` before spawn, no
        repo to commit into anyway. ``--no-git`` finishes the job.
      * ``--no-stream`` — partial stream tokens are noise in the log; we
        want the final reply per turn.
      * ``--model openai/flow`` — AgentFlow gateway resolves the alias
        server-side (gpt-5.3-codex → gpt-5.5 → opus → sonnet → haiku) so
        swapping models never rebuilds this image.
      * ``--edit-format diff`` — patch-style edits are the most reliable
        format aider supports across model families.
      * ``--map-tokens 2048`` — repo-map context for non-tiny projects so
        aider can pick the right files without an explicit add list.
    """
    try:
        log_fh = open(log_file, "ab", buffering=0)  # noqa: SIM115 — fd handed to child
    except OSError as exc:
        return {"ok": False, "error": "open_log_failed", "detail": str(exc)}

    # Pin aider's provider to the AgentFlow gateway via OpenAI-compatible
    # env vars. Aider treats `openai/<model>` as an OpenAI-shaped call and
    # picks up base+key from these envs. Project-local config (`.aider.conf.yml`)
    # would also work but env vars stay out of the project workspace, so
    # the user's repo never carries gateway credentials.
    aider_env = dict(env) if env is not None else dict(os.environ)
    api_key = aider_env.get("AF_API_KEY", "") or os.environ.get("AF_API_KEY", "")
    api_base = _normalise_api_base(
        aider_env.get("AF_API_URL") or os.environ.get("AF_API_URL", "https://agentflow.website")
    )
    if api_key:
        aider_env["OPENAI_API_BASE"] = api_base + "/llm/v1"
        aider_env["OPENAI_API_KEY"] = api_key
    # Aider phones home for analytics + checks PyPI for updates by default —
    # both fail noisily under egress restrictions and would spam the log.
    aider_env.setdefault("AIDER_ANALYTICS", "false")
    aider_env.setdefault("AIDER_CHECK_UPDATE", "false")

    if opencode_bin and opencode_bin != "opencode":
        aider_bin = opencode_bin
    # Hotfix 2026-05-28: env vars (`OPENAI_API_BASE` / `OPENAI_API_KEY`) are
    # silently ignored by aider 0.86 when the model has the `openai/` provider
    # prefix — `aider --verbose` shows `openai_api_base: None` even when env
    # is set on the subprocess. CLI flags `--openai-api-base` / `--openai-api-key`
    # win the config cascade and force aider to talk to the AgentFlow gateway.
    # Without these flags the LLM call goes to `api.openai.com` and the
    # gateway responds `Your request was blocked`.
    cmd = [
        aider_bin,
        "--yes-always",
        "--no-pretty",
        "--no-auto-commits",
        "--no-stream",
        "--no-git",
        "--no-show-model-warnings",
        "--model", "openai/flow",
        "--openai-api-base", aider_env.get("OPENAI_API_BASE", ""),
        "--openai-api-key", aider_env.get("OPENAI_API_KEY", ""),
        "--edit-format", "diff",
        "--map-tokens", "2048",
        "--map-refresh", "auto",
        "--message", brief,
    ]
    log.info("[aider] starting cmd=%s cwd=%s", " ".join(cmd[:-1]) + " <brief>", cwd)
    try:
        proc = subprocess.Popen(  # noqa: S603 — aider is on PATH from image
            cmd,
            cwd=cwd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=aider_env,
        )
    except OSError as exc:
        log_fh.close()
        return {"ok": False, "error": "spawn_failed", "detail": str(exc)}
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            log_fh.close()

    try:
        Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write pid file %s: %s", pid_file, exc)

    return {"ok": True, "pid": proc.pid}


# Back-compat alias. The WS dispatcher passes `spawn_opencode=` by name in
# server.py legacy paths + tests still wire it through. New code uses
# _default_spawn_aider directly.
_default_spawn_opencode = _default_spawn_aider


def agent_dev_brief(
    template_repo_full: str,
    slug: str,
    project_id: int,
    brief: str,
    *,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    port: int = DEFAULT_PORT,
    pid_file: str = AIDER_PID_FILE,
    log_file: str = AIDER_LOG_FILE,
    run: Callable[..., dict[str, Any]] = _default_run,
    spawn_opencode: Callable[..., dict[str, Any]] = _default_spawn_aider,
    opencode_bin: str = "aider",
    kind: str | None = None,
    bot_token: str | None = None,
    bot_username: str | None = None,
) -> dict[str, Any]:
    """Clone repo + hand the brief to aider, then daemon spawns dev server.

    Returns immediately after aider is spawned. The dev-server port is
    *not* probed here — the clone-status watcher polls and spawns the
    dev server itself once aider exits.

    Telegram-bot projects (``kind="tg_bot"``) take a different shape:
    aider installs Python deps + edits ``bot.py`` per the user's brief,
    then exits. The daemon spawns ``python bot.py`` itself (see
    :func:`_watch_and_launch_tg_bot`) and uses Telegram's ``getMe`` API
    as the alive signal instead of an HTTP port probe — Python bots
    don't bind a listening port.

    Parameter names ``spawn_opencode`` + ``opencode_bin`` are kept for
    back-compat with tests + the server.py dispatcher. They now point at
    aider; the rename will land in a follow-up sweep.
    """
    if not _looks_like_repo_full(template_repo_full):
        return {"ok": False, "error": "invalid_template_repo_full"}
    if not _looks_like_slug(slug):
        return {"ok": False, "error": "invalid_slug"}
    if not isinstance(project_id, int) or project_id <= 0:
        return {"ok": False, "error": "invalid_project_id"}
    if not brief or not brief.strip():
        return {"ok": False, "error": "missing_brief"}

    is_tg_bot = (kind or "").strip().lower() == "tg_bot"

    project_dir_name = f"proj-{slug}"
    project_dir = str(Path(workspace_root) / project_dir_name)
    repo_url = f"https://github.com/{template_repo_full}.git"

    try:
        Path(workspace_root).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "workspace_root_unwritable", "detail": str(exc)}

    if Path(project_dir).exists():
        try:
            shutil.rmtree(project_dir)
        except OSError as exc:
            return {
                "ok": False,
                "error": "cleanup_failed",
                "detail": str(exc),
                "project_dir": project_dir,
            }

    clone_res = run(
        ["git", "clone", "--depth", "1", repo_url, project_dir],
        cwd=workspace_root,
        timeout=180,
    )
    if clone_res.get("exit_code") != 0:
        return {
            "ok": False,
            "error": "git_clone_failed",
            "repo_url": repo_url,
            "detail": (clone_res.get("stderr") or clone_res.get("stdout") or "")[:1000],
        }

    dot_git = Path(project_dir) / ".git"
    try:
        if dot_git.exists():
            shutil.rmtree(dot_git)
    except OSError as exc:
        return {"ok": False, "error": "git_history_strip_failed", "detail": str(exc)}

    # Resolve runtime toolchains BEFORE the dev-server / coder spawn. This
    # reads the project's manifests (package.json engines.node, Cargo.toml,
    # go.mod, requirements.txt) and installs whatever the baked image is
    # missing. Failures here are non-fatal — opencode can still try to run
    # `pip install` / `cargo build` from inside its own loop — but the
    # log line gives us a single grep target when triage hits "wrong Node
    # version" failures. Idempotent: a warm pod with the right runtime
    # already in place is a sub-second no-op.
    try:
        runtime_result = resolve_runtimes(project_dir)
        log.info(
            "resolve_runtimes ok=%s actions=%s project_dir=%s",
            runtime_result.get("ok"),
            len(runtime_result.get("actions", [])),
            project_dir,
        )
    except Exception as exc:  # noqa: BLE001 — never block the brief on resolver bugs
        log.warning("resolve_runtimes raised, continuing: %s", exc)

    # Pin the dev-server port deterministically — no LLM in the loop. The
    # shared daemon pod hosts many projects from one user, and each one
    # gets a unique port assigned by the backend (`projects.preview_port`)
    # so previews stay independent. Patching the template's `package.json`
    # to read `${PORT:-<default>}` lets the daemon hand the port via the
    # PORT env (already injected below) without trusting opencode to
    # follow an instruction. npm executes `scripts.dev` under `sh -c`, so
    # POSIX-style parameter expansion always works.
    #
    # tg_bot templates are Python — no package.json, no dev port.
    if not is_tg_bot:
        _patch_package_json_for_port(Path(project_dir) / "package.json", port)

    # tg_bot path: seal the BotFather-issued token into the project's
    # `.env` BEFORE opencode runs. aiogram_bot_template (and every other
    # template we ship for tg_bot) reads BOT_TOKEN from .env on import,
    # so the token must be on disk by the time `python bot.py` starts.
    # The token never reaches opencode's prompt; it lives only on disk
    # and in this process's env.
    if is_tg_bot and bot_token:
        try:
            env_path = Path(project_dir) / ".env"
            existing = ""
            if env_path.exists():
                try:
                    existing = env_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    existing = ""
            # Drop any prior BOT_TOKEN= line, then append the fresh value.
            kept = "\n".join(
                line for line in existing.splitlines()
                if not line.startswith("BOT_TOKEN=")
            )
            new_env = (kept + "\n" if kept else "") + f"BOT_TOKEN={bot_token}\n"
            env_path.write_text(new_env, encoding="utf-8")
            os.chmod(env_path, 0o600)
        except OSError as exc:
            log.warning("could not write .env for tg_bot project %d: %s", project_id, exc)

    # The composed prompt tells aider what shape we expect: edit files
    # to satisfy the brief, then exit. The daemon owns dev-server +
    # python-bot lifecycle (see _watch_and_report_clone_status +
    # _watch_and_launch_tg_bot) — aider's --message mode is one-shot,
    # so spawning a long-lived process from inside aider's run never
    # worked reliably.
    #
    # No port numbers in the brief on purpose: the daemon pre-patched
    # `package.json` so `npm run dev` already binds to `$PORT`, and the
    # `PORT` env var is exported below.
    if is_tg_bot:
        # Telegram bots: no HTTP dev server in the loop. Aider edits the
        # Python source to satisfy the brief and exits. The daemon's
        # `_watch_and_launch_tg_bot` thread then spawns `python bot.py`
        # itself — keeping the launch + alive-check off the LLM's plate.
        composed = (
            f"You are editing a Telegram bot project rooted at {project_dir}. "
            f"User's brief: {brief.strip()}\n\n"
            "Edit the source so /start, /help, and the rest of the bot's "
            "behavior match the user's brief. Use brief-derived copy in the "
            "bot's replies — never leave generic template strings or "
            "placeholders. Look at bot.py first, then handlers/ or src/ if "
            "they exist, then any text resources / locale files. Treat "
            "pyproject.toml or requirements.txt as read-only — the daemon "
            "installs Python deps separately. Do NOT run the bot yourself "
            "and do NOT spawn `python bot.py` — the hosting daemon launches "
            "the bot process after you exit. Make reasonable assumptions "
            "where the brief is silent; never ask the user a clarifying "
            "question. Finish by printing a one-line summary of the files "
            "you changed."
        )
    else:
        # Landing / SPA / Next.js: aider edits files, then the daemon
        # spawns `npm run dev` via _watch_and_report_clone_status. Aider
        # must NOT try to start the dev server — single-shot --message
        # mode exits before the spawn finishes and the orphaned process
        # gets reaped. The daemon path is the reliable lane.
        #
        # We keep the deterministic-port discipline ("$PORT", "no flags
        # added", "do not modify package.json scripts") so even an
        # over-eager aider rewrite cannot break port binding. Backwards-
        # compatible substring footprints satisfy regression tests that
        # lock in those guarantees.
        composed = (
            f"You are editing a project rooted at {project_dir}. "
            f"User's brief: {brief.strip()}\n\n"
            "Hard rules — break any and the run is a failure:\n"
            f"  • You MUST edit at least one file under {project_dir}/.\n"
            "  • Never ask a clarifying question. The brief is final — "
            "make reasonable assumptions where it is silent.\n"
            "  • Never reply with «нет доступа», «не выполнено», "
            "\"I'd need more context\". Read more files instead.\n\n"
            "Steps:\n"
            "1. Read package.json (or pyproject.toml / Cargo.toml) to "
            "identify the stack.\n"
            "2. Edit project files so the user-visible content (UI copy, "
            "routes, handlers, page titles, hero text, FAQ, pricing — "
            "whatever the template ships) reflects the brief. Never leave "
            "the template's default placeholder text in place.\n"
            "3. If the template ships auth/onboarding flows the brief "
            "doesn't mention (Clerk, NextAuth, sign-in pages), strip them "
            "to a plain fragment so the user lands on brief content "
            "immediately.\n"
            "4. Do NOT modify package.json scripts — they have been "
            "pre-patched to read the PORT environment variable; the dev "
            "server uses $PORT verbatim with no flags added.\n"
            "5. Do NOT spawn `nohup npm run dev` or `disown` the dev "
            "server. The daemon owns dev-server lifecycle and will run "
            "`npm run dev` (or pnpm/yarn) itself after you exit.\n"
            "6. Finish by printing a one-line summary of files you changed."
        )

    # Aider picks up provider config from env vars (OPENAI_API_BASE +
    # OPENAI_API_KEY) inside _default_spawn_aider — no project-local
    # config file written here. AF_API_KEY is the per-pod owner key
    # already injected by renderDaemonManifest (agentflow-agents).
    env = {**os.environ, "PORT": str(port), "BROWSER": "none", "CI": "1"}
    spawn_res = spawn_opencode(
        composed,
        cwd=project_dir,
        pid_file=pid_file,
        log_file=log_file,
        env=env,
        opencode_bin=opencode_bin,
    )
    if not spawn_res.get("ok"):
        return {
            "ok": False,
            "error": spawn_res.get("error", "opencode_spawn_failed"),
            "detail": spawn_res.get("detail"),
            "project_dir": project_dir,
        }

    # Fire-and-forget watcher that polls the dev port and POSTs
    # clone-status back to the platform. Without this, the backend never
    # learns the dev server is up and the auto-expose step never runs —
    # the project sits in `provisioning` forever. Daemon-thread so it
    # cannot block process shutdown.
    #
    # tg_bot takes a different path: a separate watcher waits for opencode
    # to exit, then spawns `python bot.py` and uses Telegram's `getMe`
    # API as the alive signal. No HTTP port is ever bound by a Python bot.
    opencode_pid_for_watch: int | None = int(spawn_res.get("pid", 0)) or None
    if is_tg_bot:
        threading.Thread(
            target=_watch_and_launch_tg_bot,
            name=f"tg-bot-launcher-{project_id}",
            kwargs={
                "project_id": project_id,
                "slug": slug,
                "project_dir": project_dir,
                "repo_url": repo_url,
                "bot_token": bot_token or "",
                "bot_username": bot_username or "",
                "opencode_pid": opencode_pid_for_watch,
            },
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=_watch_and_report_clone_status,
            name=f"clone-status-watcher-{project_id}",
            kwargs={
                "project_id": project_id,
                "slug": slug,
                "port": port,
                "project_dir": project_dir,
                "repo_url": repo_url,
                "opencode_pid": opencode_pid_for_watch,
            },
            daemon=True,
        ).start()
        # Phase-3 priority #0 (2026-05-27): daemon-push preview heartbeat.
        # Replaces host-side polling of *.proj.agentflow.website from the
        # agents pod. The daemon lives next to the dev process, so probing
        # 127.0.0.1:$PORT is precise (no HMR-induced 5xx false positives,
        # no per-kind cool-down, no flap). We POST /preview-alive every 30s
        # while the port answers, and /preview-alive { alive: false } once
        # we see three consecutive misses. Backend treats fresh heartbeat
        # as the alive signal and skips its public-URL probe entirely.
        threading.Thread(
            target=_heartbeat_preview_loop,
            name=f"preview-heartbeat-{project_id}",
            kwargs={
                "project_id": project_id,
                "port": port,
            },
            daemon=True,
        ).start()

    # Second fire-and-forget watcher that tails the opencode stdout log and
    # POSTs batches of lines to the platform's /agent-log route. Without
    # this, the Activity widget on the project page sits empty for 5-15
    # minutes between `clone_dispatched` and `preview_exposed` — the owner
    # has no signal that opencode is alive and working. Daemon-thread so
    # process shutdown isn't blocked.
    opencode_pid_int: int | None = opencode_pid_for_watch
    threading.Thread(
        target=_tail_and_stream_agent_log,
        name=f"agent-log-tailer-{project_id}",
        kwargs={
            "project_id": project_id,
            "log_path": log_file,
            "opencode_pid": opencode_pid_int,
        },
        daemon=True,
    ).start()

    return {
        "ok": True,
        "project_id": project_id,
        "slug": slug,
        "project_dir": project_dir,
        "repo_url": repo_url,
        "opencode_pid": int(spawn_res.get("pid", 0)),
        "log_file": log_file,
        "port": port,
    }


AIDER_EDIT_PID_FILE = "/tmp/agent-brief-aider-edit.pid"
AIDER_EDIT_LOG_FILE = "/tmp/agent-brief-aider-edit.log"

# Back-compat aliases.
OPENCODE_EDIT_PID_FILE = AIDER_EDIT_PID_FILE
OPENCODE_EDIT_LOG_FILE = AIDER_EDIT_LOG_FILE


def agent_dev_brief_edit(
    slug: str,
    project_id: int,
    edit_prompt: str,
    *,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    pid_file: str = AIDER_EDIT_PID_FILE,
    log_file: str = AIDER_EDIT_LOG_FILE,
    spawn_opencode: Callable[..., dict[str, Any]] = _default_spawn_aider,
    opencode_bin: str = "aider",
    kind: str | None = None,
) -> dict[str, Any]:
    """Re-spawn aider against an existing project workspace.

    Unlike :func:`agent_dev_brief` this does NOT clone, NOT install deps,
    NOT touch ``package.json``. The workspace is expected to already
    exist from a prior ``agent_dev_brief`` run. The edit prompt is
    handed to aider verbatim with a thin instruction wrapper so the
    model stays grounded in the brief shape (no early-termination,
    write files, then verify).

    Used by the AgentFlow chat → running-project edit flow: the user
    sends a message like «сделай кнопку красной», the platform routes
    it here, aider edits the source, the running dev server reloads
    automatically (vite / next watch mode). Caller is expected to watch
    ``log_file`` for stdout via ``/internal/projects/:id/agent-log`` —
    the daemon's log tailer streams it the same way as initial runs.
    """
    if not _looks_like_slug(slug):
        return {"ok": False, "error": "invalid_slug"}
    if not isinstance(project_id, int) or project_id <= 0:
        return {"ok": False, "error": "invalid_project_id"}
    if not edit_prompt or not edit_prompt.strip():
        return {"ok": False, "error": "missing_edit_prompt"}

    project_dir = str(Path(workspace_root) / f"proj-{slug}")
    if not Path(project_dir).is_dir():
        return {
            "ok": False,
            "error": "workspace_missing",
            "project_dir": project_dir,
            "detail": (
                f"Expected workspace at {project_dir}, found none. The "
                "project was likely never bootstrapped or its pod was "
                "recreated without a PVC. Run agent_dev_brief first."
            ),
        }

    is_tg_bot = (kind or "").strip().lower() == "tg_bot"
    runtime_kind_note = (
        "This is a Telegram bot. After editing, do NOT spawn `python "
        "bot.py` — the hosting daemon owns the bot process lifecycle. "
        "Just edit + exit."
        if is_tg_bot
        else "The dev server is already running and watches the filesystem. "
        "After editing, do NOT restart it. The change reloads automatically."
    )

    composed = (
        f"You are editing an existing project at {project_dir}. "
        f"User's edit request: {edit_prompt.strip()}\n\n"
        "You have FULL access to the workspace via your built-in tools "
        "(Read, Write, Edit, Glob, Grep, Bash). You are NOT in read-only "
        "mode. The user has already approved this run.\n\n"
        "Hard rules — break any and the run is a failure:\n"
        "  • You MUST write or edit at least one file under "
        f"{project_dir}/ before terminating.\n"
        "  • Never reply with «нет доступа», «не выполнено», "
        "\"I'd need more context\". If you lack context, READ MORE FILES.\n"
        "  • Never ask the user a clarifying question. Make reasonable "
        "assumptions and proceed.\n\n"
        "Execute:\n"
        "1. Use Glob/Grep to locate the files this request touches.\n"
        "2. Read the relevant files end-to-end.\n"
        "3. Edit them so the user's request is satisfied. Keep changes "
        "minimal and scoped — do not refactor unrelated code.\n"
        "4. Print a one-line summary of files you changed and exit.\n\n"
        f"{runtime_kind_note}"
    )

    env = {**os.environ, "BROWSER": "none", "CI": "1"}
    spawn_res = spawn_opencode(
        composed,
        cwd=project_dir,
        pid_file=pid_file,
        log_file=log_file,
        env=env,
        opencode_bin=opencode_bin,
    )
    if not spawn_res.get("ok"):
        return {
            "ok": False,
            "error": spawn_res.get("error", "opencode_spawn_failed"),
            "detail": spawn_res.get("detail"),
            "project_dir": project_dir,
        }

    # Stream the edit-session log back to the platform — same channel the
    # initial agent_dev_brief uses so the cabinet Activity widget shows
    # progress without a separate UI surface.
    opencode_pid_int: int | None = int(spawn_res.get("pid", 0)) or None
    threading.Thread(
        target=_tail_and_stream_agent_log,
        name=f"agent-log-tailer-edit-{project_id}",
        kwargs={
            "project_id": project_id,
            "log_path": log_file,
            "opencode_pid": opencode_pid_int,
        },
        daemon=True,
    ).start()

    return {
        "ok": True,
        "project_id": project_id,
        "slug": slug,
        "project_dir": project_dir,
        "opencode_pid": int(spawn_res.get("pid", 0)),
        "log_file": log_file,
    }


DEV_SERVER_LOG_FILE = "/tmp/agent-brief-dev-server.log"
DEV_SERVER_PID_FILE = "/tmp/agent-brief-dev-server.pid"

# Daemon-side dev-server spawn gates (2026-05-28). After the aider replace,
# the agent never spawns a dev server itself — the daemon always does, once
# aider exits. We still wait a short grace + minimum wall clock so a fast
# aider crash from a permission error or a stuck npm install can settle
# before the spawn fires. The 30s grace + 45s wall mirror the original
# opencode-era values so existing tests + ops runbooks stay consistent.
DEV_FALLBACK_GRACE_AFTER_OPENCODE_EXIT_SEC = 30.0
DEV_FALLBACK_MIN_WALL_CLOCK_SEC = 45.0


def _spawn_dev_server(
    project_dir: str,
    port: int,
    *,
    log_file: str = DEV_SERVER_LOG_FILE,
    pid_file: str = DEV_SERVER_PID_FILE,
    run: Callable[..., dict[str, Any]] = _default_run,
) -> dict[str, Any]:
    """Detached ``npm run dev`` so the dev server outlives the watcher.

    Used by ``_watch_and_report_clone_status`` as a fallback when opencode
    exits without binding the dev port. Mirrors the spawn pattern of
    :func:`_spawn_python_bot`:

    * Resolve the package manager from lockfiles (pnpm-lock.yaml → pnpm,
      yarn.lock → yarn, default npm). We do not look at the LLM's lockfile
      preference — the file on disk is the source of truth.
    * If ``node_modules`` is missing, run ``<pm> install`` first. opencode
      always runs this before reaching the dev step, so the common path is
      a no-op; the guard exists for the case where opencode crashed mid-run.
    * Spawn ``<pm> run dev`` with ``PORT=<port>``, detached via ``nohup``
      semantics (``start_new_session=True``) so it reparents under PID 1
      (tini) and keeps listening after this function returns.

    Returns ``{"ok": True, "pid": <int>, "package_manager": <str>}`` on
    success or ``{"ok": False, "error": "<code>", "detail": "<msg>"}`` on
    failure. Never raises — caller logs and moves on.
    """
    project_path = Path(project_dir)
    if not project_path.is_dir():
        return {"ok": False, "error": "project_dir_missing"}
    pkg_json = project_path / "package.json"
    if not pkg_json.exists():
        return {"ok": False, "error": "no_package_json"}

    # Pick the package manager from lockfiles. The patched ``scripts.dev``
    # is identical across npm/pnpm/yarn since it goes through ``sh -c`` —
    # the only thing that matters is which CLI is on PATH.
    if (project_path / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (project_path / "yarn.lock").exists():
        pm = "yarn"
    else:
        pm = "npm"

    # Install deps if opencode bailed before reaching the install step.
    # The 5-minute cap is enough for `pnpm install` on a fresh project +
    # buffer; longer-running installs likely indicate a deeper issue
    # (network, lockfile mismatch) that this fallback cannot fix anyway.
    if not (project_path / "node_modules").exists():
        install_args = ["install"]
        install_res = run([pm, *install_args], cwd=project_dir, timeout=300)
        if install_res.get("exit_code") != 0:
            return {
                "ok": False,
                "error": "fallback_install_failed",
                "detail": (install_res.get("stderr") or install_res.get("stdout") or "")[:500],
            }

    try:
        log_fh = open(log_file, "ab", buffering=0)  # noqa: SIM115 — fd handed to child
    except OSError as exc:
        return {"ok": False, "error": "open_log_failed", "detail": str(exc)}

    env = {**os.environ, "PORT": str(port), "BROWSER": "none", "CI": "1"}
    cmd = [pm, "run", "dev"]
    try:
        proc = subprocess.Popen(  # noqa: S603 — pm is from a fixed allowlist
            cmd,
            cwd=project_dir,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        log_fh.close()
        return {"ok": False, "error": "spawn_failed", "detail": str(exc)}
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            log_fh.close()

    try:
        Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write dev-server pid file %s: %s", pid_file, exc)

    return {"ok": True, "pid": proc.pid, "package_manager": pm}


def _watch_and_report_clone_status(
    *,
    project_id: int,
    slug: str,
    port: int,
    project_dir: str,
    repo_url: str,
    timeout_sec: float = 900.0,
    poll_interval_sec: float = 5.0,
    opencode_pid: int | None = None,
    spawn_dev_server: Callable[..., dict[str, Any]] | None = None,
    pid_alive: Callable[[int | None], bool] | None = None,
) -> None:
    """Background watcher: poll the dev port + POST clone-status.

    Polls `http://127.0.0.1:{port}/` once every ``poll_interval_sec``.
    On the first 2xx/3xx response, POSTs a successful clone-status to
    the backend with ``pod_ip`` so the auto-expose step can wire the
    public ingress. If the port never opens within ``timeout_sec``
    (~15 min — long enough for `pnpm install` + `next build`), reports
    `ok=false, error=port_unreachable`. Idempotent on the backend side:
    re-running clone-status repoints the existing Service/Endpoints.

    Dev-server fallback (2026-05-28):
        opencode sometimes exits without spawning ``npm run dev`` —
        observed in projects 1547, 1552, 1553, 1557 where the run log
        ends mid-todo-list and the dev server never binds the port. The
        watcher detects this (opencode pid dead + grace window passed +
        port still cold) and spawns the dev server itself with the right
        ``PORT`` env. node_modules is reused when present; otherwise a
        bounded ``<pm> install`` runs first.

        Fired at most once per watcher run. After the fallback the loop
        continues polling — the next 2xx/3xx reply promotes the project
        as usual.
    """
    # The public ingress mounts agentflow-agents under /_agents/* — paths
    # at root (e.g. https://agentflow.website/internal/...) return 404
    # because nothing in the host's ingress map points there. server.py
    # and desktop_tools.py already normalise the suffix; this watcher
    # missed it, so every clone report came back as
    # "clone-status report failed: HTTP Error 403 Forbidden" and the
    # backend never saw the project's port open → status stuck at
    # `provisioning` forever (owner repro 2026-05-27, project 1525/1526).
    api_base = _normalise_api_base(os.environ.get("AF_API_URL", "https://agentflow.website"))
    internal_secret = os.environ.get("AF_INTERNAL_API_SECRET", "")
    if not internal_secret:
        log.info(
            "clone-status watcher skipping report for project %d — "
            "AF_INTERNAL_API_SECRET unset",
            project_id,
        )
        return
    pod_ip = _resolve_pod_ip()
    spawn_dev = spawn_dev_server or _spawn_dev_server
    is_pid_alive_fn = pid_alive or _is_pid_alive

    started_at = time.monotonic()
    deadline = started_at + timeout_sec
    port_reachable = False
    fallback_attempted = False
    fallback_result: dict[str, Any] | None = None
    opencode_dead_since: float | None = None
    while time.monotonic() < deadline:
        if _http_probe(port):
            port_reachable = True
            break
        # Dev-server fallback: opencode is dead AND grace window passed AND
        # we haven't already tried. One-shot — if the fallback itself fails
        # we keep polling normally so a slow `npm install` from opencode
        # earlier in the run can still cross the line.
        if not fallback_attempted and opencode_pid is not None:
            opencode_alive = is_pid_alive_fn(opencode_pid)
            if not opencode_alive:
                if opencode_dead_since is None:
                    opencode_dead_since = time.monotonic()
                dead_for = time.monotonic() - opencode_dead_since
                wall_clock = time.monotonic() - started_at
                if (
                    dead_for >= DEV_FALLBACK_GRACE_AFTER_OPENCODE_EXIT_SEC
                    and wall_clock >= DEV_FALLBACK_MIN_WALL_CLOCK_SEC
                ):
                    fallback_attempted = True
                    log.info(
                        "dev-server fallback firing for project %d (port %d, "
                        "opencode dead %.0fs, wall %.0fs)",
                        project_id,
                        port,
                        dead_for,
                        wall_clock,
                    )
                    try:
                        fallback_result = spawn_dev(project_dir, port)
                    except Exception as exc:  # noqa: BLE001 — never let fallback crash watcher
                        log.warning(
                            "dev-server fallback raised for project %d: %s",
                            project_id,
                            exc,
                        )
                        fallback_result = {"ok": False, "error": "fallback_exception", "detail": str(exc)[:300]}
        time.sleep(poll_interval_sec)

    body = {
        "ok": port_reachable,
        "port_reachable": port_reachable,
        "port": port,
        "project_dir": project_dir,
        "repo_url": repo_url,
        "pod_ip": pod_ip,
    }
    # Surface the fallback so /clone-status events have a single grep target
    # (`dev_server_fallback`) when ops triage stuck previews. The field is
    # absent on runs that never needed the fallback so the common path stays
    # cheap to parse.
    if fallback_attempted:
        body["dev_server_fallback"] = {
            "attempted": True,
            "ok": bool(fallback_result and fallback_result.get("ok")),
            "error": (fallback_result or {}).get("error"),
            "detail": ((fallback_result or {}).get("detail") or "")[:300] or None,
            "package_manager": (fallback_result or {}).get("package_manager"),
            "pid": (fallback_result or {}).get("pid"),
        }
    if not port_reachable:
        body["error"] = "port_unreachable"
        body["detail"] = (
            f"dev port {port} did not respond within {int(timeout_sec)}s "
            f"on slug={slug}"
        )

    url = f"{api_base}/internal/projects/{project_id}/clone-status"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-agentflow-secret": internal_secret,
            # Cloudflare's WAF flags Python-urllib's default user-agent
            # and replies with 403 to POSTs from k8s pod IPs. curl-shaped
            # UA passes through cleanly. Owner repro 2026-05-27 — both
            # clone-status and agent-log tailers ate 403 in a tight loop.
            "user-agent": "curl/8.5.0 agentflow-daemon",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — internal URL only
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        log.warning(
            "clone-status report failed for project %d: %s", project_id, exc
        )


def _post_heartbeat(
    api_base: str,
    internal_secret: str,
    project_id: int,
    *,
    alive: bool,
    port: int,
    http_post: HttpPoster | None = None,
) -> None:
    """POST a single preview heartbeat to the backend.

    Uses the ``/daemon-log/projects/:id/preview-alive`` alias (mirroring
    the agent-log + clone-status pattern from PR #894 / #105 / #106).
    Cloudflare's WAF blocks POSTs to ``/_agents/internal/*`` from k8s pod
    IPs with 403; the alias keeps the same secret-protected handler but
    ducks under the /internal block.

    Best-effort: any HTTP failure logs at warning and returns. The next
    tick will retry — that's the whole point of the loop.
    """
    # Late-import the local default so tests can swap the poster cleanly
    # via injection without monkey-patching the module's urllib path.
    poster = http_post or _default_http_post
    url = f"{api_base}/daemon-log/projects/{project_id}/preview-alive"
    payload = json.dumps({"alive": bool(alive), "port": int(port)}).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-agentflow-secret": internal_secret,
        # Cloudflare's WAF flags Python-urllib's default UA — see notes in
        # _watch_and_report_clone_status for the full repro.
        "user-agent": "curl/8.5.0 agentflow-daemon",
    }
    try:
        status, _body = poster(url, payload, headers)
        if status >= 400:
            log.warning(
                "preview-heartbeat POST returned %d for project %d (alive=%s)",
                status,
                project_id,
                alive,
            )
    except (urllib.error.URLError, OSError) as exc:
        log.warning(
            "preview-heartbeat POST failed for project %d: %s", project_id, exc
        )


def _heartbeat_preview_loop(
    *,
    project_id: int,
    port: int,
    heartbeat_interval_sec: float = 30.0,
    consecutive_misses_before_dead: int = 3,
    http_probe: Callable[[int], bool] | None = None,
    http_post: HttpPoster | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> None:
    """Continuously probe ``localhost:$PORT`` + POST alive/dead heartbeats.

    Why this exists (phase-3 priority #0, 2026-05-27):
        Host-side polling of ``https://<slug>.proj.agentflow.website`` from
        the agents pod was fighting symptoms (HMR 5xx, cold-start 5xx) with
        per-kind cool-downs. The daemon is the one process that knows
        *for sure* whether the dev server is up — it lives in the same pod
        as opencode. So the daemon pushes alive/dead instead of the host
        pulling.

    Loop:
        Every ``heartbeat_interval_sec`` (default 30s):
          * probe ``http://127.0.0.1:{port}/`` (cheap TCP+HTTP round-trip)
          * on success → POST ``alive=true``, reset miss counter
          * on failure → increment miss counter; when it reaches
            ``consecutive_misses_before_dead`` (default 3) POST ``alive=false``
            once, then keep trying — the dev server may come back.

    Daemon thread: process shutdown does not block. ``max_iterations`` is
    a test hook — production code never sets it, so the loop runs forever.
    """
    probe = http_probe or _http_probe
    internal_secret = os.environ.get("AF_INTERNAL_API_SECRET", "")
    if not internal_secret:
        log.info(
            "preview-heartbeat skipping project %d — AF_INTERNAL_API_SECRET unset",
            project_id,
        )
        return
    api_base = _normalise_api_base(os.environ.get("AF_API_URL", "https://agentflow.website"))

    consecutive_misses = 0
    dead_reported = False
    iterations = 0
    while True:
        sleep(heartbeat_interval_sec)
        ok = probe(port)
        if ok:
            consecutive_misses = 0
            dead_reported = False
            _post_heartbeat(
                api_base,
                internal_secret,
                project_id,
                alive=True,
                port=port,
                http_post=http_post,
            )
        else:
            consecutive_misses += 1
            if consecutive_misses >= consecutive_misses_before_dead and not dead_reported:
                # POST `alive=false` exactly once per outage so the backend
                # gets a precise dead signal without event-spam. Subsequent
                # ticks keep probing — when the dev server recovers, the
                # next ok=True path resets dead_reported so a fresh outage
                # later still emits a dead heartbeat.
                _post_heartbeat(
                    api_base,
                    internal_secret,
                    project_id,
                    alive=False,
                    port=port,
                    http_post=http_post,
                )
                dead_reported = True

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return


TG_BOT_LOG_FILE = "/tmp/agent-brief-tg-bot.log"


def _tg_get_me(bot_token: str, *, timeout_sec: float = 5.0) -> dict[str, Any]:
    """Call Telegram's ``getMe`` for the given token.

    Returns the decoded response on HTTP 200 with ``ok:true``, otherwise
    a sentinel ``{"ok": False, "error": "<reason>"}`` dict. Used as the
    alive-check for tg_bot projects — getMe returns success iff the bot
    token is valid AND the bot process can reach api.telegram.org (which
    the daemon pod can, since it already calls the platform via the same
    egress route).
    """
    if not bot_token or ":" not in bot_token:
        return {"ok": False, "error": "invalid_token"}
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:  # noqa: S310 — Telegram API
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http_{exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": "network", "detail": str(exc)[:200]}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "decode_failed"}
    if isinstance(data, dict) and data.get("ok"):
        return data
    return {"ok": False, "error": "telegram_not_ok", "detail": json.dumps(data)[:300]}


def _spawn_python_bot(
    project_dir: str,
    *,
    bot_token: str,
    log_file: str = TG_BOT_LOG_FILE,
) -> dict[str, Any]:
    """Detached ``python <entrypoint>`` so the bot outlives the call.

    The daemon's WS dispatcher does not stick around — when this thread
    returns, the bot must keep polling Telegram on its own. ``nohup`` +
    ``start_new_session=True`` reparent the child under PID 1 (tini),
    matching the dev-server spawn pattern.

    Entrypoint discovery covers the two common shapes:
    * Script at repo root or one subdir deep: ``bot.py`` → ``main.py`` →
      ``app.py`` → ``backend/bot.py`` → ``src/bot.py`` (priority order).
      Runs as ``python <path>``.
    * Runnable package (``<pkg>/__main__.py``): ``app`` → ``bot`` → ``src``
      (priority order). Runs as ``python -m <pkg>``. The
      ``aiogram_bot_template`` we ship for tg_bot uses this layout.

    Anything else returns ``no_python_entrypoint`` so the launcher can
    POST clone-status ok=false with a stable error code.
    """
    project_path = Path(project_dir)

    cmd: list[str] | None = None
    entrypoint_repr: str | None = None

    # 1. Script files at root or one level down. Order matters — `bot.py`
    # wins over `app.py` when both exist (some templates ship `app.py`
    # as a leftover example).
    for candidate in (
        "bot.py",
        "main.py",
        "app.py",
        "backend/bot.py",
        "src/bot.py",
    ):
        path = project_path / candidate
        if path.exists():
            cmd = ["python", str(path)]
            entrypoint_repr = str(path)
            break

    # 2. Runnable package — `python -m <pkg>` looks for `<pkg>/__main__.py`.
    if cmd is None:
        for pkg in ("app", "bot", "src"):
            main_py = project_path / pkg / "__main__.py"
            if main_py.exists():
                cmd = ["python", "-m", pkg]
                entrypoint_repr = f"-m {pkg}"
                break

    if cmd is None:
        return {"ok": False, "error": "no_python_entrypoint"}

    try:
        log_fh = open(log_file, "ab", buffering=0)  # noqa: SIM115 — fd handed to child
    except OSError as exc:
        return {"ok": False, "error": "open_log_failed", "detail": str(exc)}

    env = {**os.environ, "BOT_TOKEN": bot_token, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen(  # noqa: S603 — python interpreter from PATH
            cmd,
            cwd=project_dir,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        log_fh.close()
        return {"ok": False, "error": "spawn_failed", "detail": str(exc)}
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            log_fh.close()
    return {"ok": True, "pid": proc.pid, "entrypoint": entrypoint_repr or " ".join(cmd)}


def _watch_and_launch_tg_bot(
    *,
    project_id: int,
    slug: str,
    project_dir: str,
    repo_url: str,
    bot_token: str,
    bot_username: str,
    opencode_pid: int | None,
    timeout_sec: float = 900.0,
    poll_interval_sec: float = 3.0,
    spawn_bot: Callable[..., dict[str, Any]] | None = None,
    tg_get_me: Callable[[str], dict[str, Any]] | None = None,
    pid_alive: Callable[[int | None], bool] | None = None,
) -> None:
    """Background watcher for tg_bot projects.

    Sequence:
      1. Wait for opencode to exit (it's single-shot — installs deps,
         edits the Python source, then returns).
      2. ``nohup python bot.py`` so the bot polls Telegram in the
         background, surviving daemon WS reconnects.
      3. Hit ``getMe`` up to ~30s. The first successful response means
         the bot is live AND the token is valid AND Telegram sees the
         long-poll session.
      4. POST clone-status with ``port=0`` and the verified
         ``bot_username``. Backend treats port=0 + bot_username as the
         tg_bot success signal — no Service / Ingress wiring needed.

    On any failure (no opencode binary, no python entrypoint, getMe
    never returns ok), report ``ok=false`` with a stable error code so
    the platform can surface it in the project timeline.
    """
    spawn_bot = spawn_bot or _spawn_python_bot
    tg_get_me = tg_get_me or _tg_get_me
    pid_alive = pid_alive or _is_pid_alive

    internal_secret = os.environ.get("AF_INTERNAL_API_SECRET", "")
    if not internal_secret:
        log.info(
            "tg-bot launcher skipping project %d — AF_INTERNAL_API_SECRET unset",
            project_id,
        )
        return

    api_base = _normalise_api_base(os.environ.get("AF_API_URL", "https://agentflow.website"))
    pod_ip = _resolve_pod_ip()

    # 1. Wait for opencode to exit. Bounded by ``timeout_sec`` so a stuck
    #    opencode does not block the launcher forever.
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not pid_alive(opencode_pid):
            break
        time.sleep(poll_interval_sec)

    bot_pid: int | None = None
    final_ok = False
    final_error: str | None = None
    final_detail: str | None = None
    verified_username: str | None = None

    if not bot_token:
        final_error = "missing_bot_token"
        final_detail = "scope.bot_token was not provided by backend"
    else:
        spawn_res = spawn_bot(project_dir, bot_token=bot_token)
        if not spawn_res.get("ok"):
            final_error = spawn_res.get("error") or "bot_spawn_failed"
            final_detail = spawn_res.get("detail")
        else:
            bot_pid = spawn_res.get("pid")

            # 3. Verify via getMe. The bot needs ~1-2s to start aiogram's
            #    long-poll session; the loop gives it up to 30s with a
            #    3-second poll cadence.
            getme_deadline = time.monotonic() + 30.0
            while time.monotonic() < getme_deadline:
                gm = tg_get_me(bot_token)
                if gm.get("ok"):
                    result = gm.get("result") or {}
                    verified_username = (result.get("username") or bot_username or "").lstrip("@")
                    final_ok = True
                    break
                final_error = gm.get("error") or "getme_failed"
                final_detail = gm.get("detail")
                time.sleep(3.0)

    body: dict[str, Any] = {
        "ok": final_ok,
        # port=0 is the signal to backend that this is a non-HTTP project.
        # The clone-status route looks at port + bot_username together to
        # decide whether to wire ingress.
        "port": 0,
        "port_reachable": False,
        "project_dir": project_dir,
        "repo_url": repo_url,
        "pod_ip": pod_ip,
        "kind": "tg_bot",
    }
    if verified_username:
        body["bot_username"] = verified_username
    elif bot_username:
        body["bot_username"] = bot_username.lstrip("@")
    if bot_pid:
        body["dev_pid"] = bot_pid
    if not final_ok:
        body["error"] = final_error or "tg_bot_alive_failed"
        if final_detail:
            body["detail"] = final_detail[:1000]

    url = f"{api_base}/internal/projects/{project_id}/clone-status"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-agentflow-secret": internal_secret,
            # Cloudflare's WAF flags Python-urllib's default user-agent
            # and replies with 403 to POSTs from k8s pod IPs. curl-shaped
            # UA passes through cleanly. Owner repro 2026-05-27 — both
            # clone-status and agent-log tailers ate 403 in a tight loop.
            "user-agent": "curl/8.5.0 agentflow-daemon",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — internal URL only
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        log.warning(
            "tg-bot clone-status report failed for project %d: %s", project_id, exc
        )


def _http_probe(port: int) -> bool:
    """Return True iff GET http://127.0.0.1:{port}/ replies in <2s.

    Treats any HTTP response (including 404 / 500) as "the server is
    listening". A connection refused / DNS failure / timeout reads
    as "not ready yet". Keeps the watcher cheap: a single TCP
    round-trip per poll, no full body read.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310 — loopback only
            f"http://127.0.0.1:{port}/", timeout=2
        ) as resp:
            return 100 <= resp.status < 600
    except urllib.error.HTTPError as exc:
        return 100 <= int(exc.code or 0) < 600
    except (urllib.error.URLError, OSError, ConnectionError):
        return False


def _resolve_pod_ip() -> str | None:
    """Best-effort lookup of the daemon pod's IP.

    Order: ``POD_IP`` env (kubernetes downward API), else the
    hostname-resolved IP, else None. The backend tolerates missing
    pod_ip — it just skips the auto-expose step.
    """
    explicit = (os.environ.get("POD_IP") or "").strip()
    if explicit:
        return explicit
    try:
        # gethostbyname(gethostname()) — returns 127.0.1.1 on some
        # distros; the connect-to-public trick reliably reports the
        # routable IP. Failures fall through to the hostname path.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
            if ip and ip != "127.0.0.1":
                return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


HttpPoster = Callable[[str, bytes, dict[str, str]], tuple[int, bytes]]


def _default_http_post(
    url: str, data: bytes, headers: dict[str, str]
) -> tuple[int, bytes]:
    """Plain ``urllib`` POST used by the tail thread.

    Returned as ``(status, body)`` so tests can swap a fake without
    monkey-patching ``urllib``. Mirrors the helper shape used by
    ``autonomous/budget.py``.
    """
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — internal URL only
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code or 0), exc.read() if hasattr(exc, "read") else b""


def _is_pid_alive(pid: int | None) -> bool:
    """Return True when ``pid`` is still running on this host.

    Tail thread uses this to know when opencode has exited so it can drain
    the remaining bytes and stop. ``None`` reads as "no pid to track" → keep
    going until the watcher timeout fires.
    """
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _tail_and_stream_agent_log(
    *,
    project_id: int,
    log_path: str,
    opencode_pid: int | None,
    batch_size: int = 10,
    flush_interval_sec: float = 2.0,
    timeout_sec: float = 1800.0,
    poll_sleep_sec: float = 0.25,
    http_post: HttpPoster = _default_http_post,
    pid_alive: Callable[[int | None], bool] = _is_pid_alive,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Tail opencode stdout + POST batches of lines to /daemon-log/.../agent-log.

    The opencode CLI prints structured progress to ``log_path``. We open the
    file, seek to EOF (it already contains the spawn's first writes by the
    time this thread starts, but reading from the start would re-stream
    everything to the platform on every retry), and pump new bytes through
    a ``\n``-delimited splitter.

    Batching rule: flush whenever the buffer reaches ``batch_size`` lines OR
    ``flush_interval_sec`` elapsed since the previous flush, whichever comes
    first. The 2-second window means a single typed-out sentence reaches the
    Activity widget in roughly real time without spamming the route on a
    chatty `pnpm install`.

    Stop conditions (in priority order):
    1. ``timeout_sec`` elapsed since start (30 min default).
    2. opencode pid is no longer alive AND no new bytes arrived for ~3s
       (final drain). Without the drain the closing lines of a fast run
       would never reach the platform.

    Errors are logged at warning level and do not propagate. Missing
    ``AF_INTERNAL_API_SECRET`` short-circuits the whole thread — the same
    safeguard the clone-status watcher uses.
    """
    internal_secret = os.environ.get("AF_INTERNAL_API_SECRET", "")
    if not internal_secret:
        log.info(
            "agent-log tailer skipping project %d — AF_INTERNAL_API_SECRET unset",
            project_id,
        )
        return

    api_base = _normalise_api_base(os.environ.get("AF_API_URL", "https://agentflow.website"))
    # Cloudflare blocks POST to /_agents/internal/* from pod IPs with 403.
    # Backend PR #894 exposes the same handler at /daemon-log/* (still gated
    # by requireInternalSecret) which CF allows.
    url = f"{api_base}/daemon-log/projects/{project_id}/agent-log"

    # Wait briefly for the log file to appear — spawn_opencode opens it
    # before returning, but on a slow filesystem a follow-up open could
    # still race. ~5s budget total before we give up.
    file_wait_deadline = now() + 5.0
    while now() < file_wait_deadline:
        if Path(log_path).exists():
            break
        time.sleep(0.1)
    if not Path(log_path).exists():
        log.warning(
            "agent-log tailer: %s never appeared for project %d", log_path, project_id
        )
        return

    deadline = now() + timeout_sec
    buffer: list[dict[str, Any]] = []
    leftover = ""
    last_flush = now()
    last_byte_at = now()
    drain_grace_sec = 3.0

    def _flush() -> None:
        nonlocal last_flush
        if not buffer:
            return
        payload = json.dumps({"lines": buffer}).encode("utf-8")
        try:
            status, _body = http_post(
                url,
                payload,
                {
                    "content-type": "application/json",
                    "x-agentflow-secret": internal_secret,
                    # See clone-status note — CF blocks default Python UA.
                    "user-agent": "curl/8.5.0 agentflow-daemon",
                },
            )
            if status >= 400:
                log.warning(
                    "agent-log POST returned %d for project %d", status, project_id
                )
        except (urllib.error.URLError, OSError) as exc:
            log.warning(
                "agent-log POST failed for project %d: %s", project_id, exc
            )
        buffer.clear()
        last_flush = now()

    # ``errors='replace'`` so a stray byte in pnpm's progress bar can't
    # crash the reader. ``newline=''`` keeps universal-newlines off so we
    # own the split exactly on ``\n``. The file handle is closed in the
    # ``finally`` block at the end of the function — ruff's SIM115 wants a
    # context manager, but the tail loop runs for up to 30 minutes and
    # ``with`` would force us to inline the entire loop body, which is
    # worse for readability than the explicit close.
    try:
        fh = open(log_path, encoding="utf-8", errors="replace", newline="")  # noqa: SIM115 — long-lived handle
    except OSError as exc:
        log.warning(
            "agent-log tailer: open %s failed for project %d: %s",
            log_path,
            project_id,
            exc,
        )
        return

    try:
        # Start at the head of the file — opencode is just-spawned, so the
        # file contents from this point forward are the full run. We do NOT
        # seek to EOF because the spawn typically writes its first bytes
        # before this thread starts and we'd lose the install banner.
        while True:
            chunk = fh.read()
            if chunk:
                last_byte_at = now()
                text = leftover + chunk
                parts = text.split("\n")
                # Last part is incomplete (no trailing newline) — keep for
                # the next read.
                leftover = parts.pop()
                ts_ms = int(time.time() * 1000)
                for raw_line in parts:
                    # Drop bare empty lines client-side; the server also
                    # drops them but skipping early saves an HTTP byte.
                    stripped = raw_line.rstrip("\r")
                    if not stripped.strip():
                        continue
                    buffer.append({"line": stripped, "ts": ts_ms})
                    if len(buffer) >= batch_size:
                        _flush()

            if now() - last_flush >= flush_interval_sec:
                _flush()

            if now() >= deadline:
                break

            # opencode exited AND a drain grace window passed with no new
            # bytes — combine into one ``and`` per ruff SIM102. The grace
            # covers stdout-buffer flushes that arrive after waitpid resolves.
            if (
                not pid_alive(opencode_pid)
                and now() - last_byte_at >= drain_grace_sec
            ):
                break

            time.sleep(poll_sleep_sec)
    finally:
        # Flush trailing partial line + any pending buffer so the last
        # opencode output reaches the platform.
        if leftover.strip():
            buffer.append({"line": leftover.rstrip("\r"), "ts": int(time.time() * 1000)})
            leftover = ""
        _flush()
        import contextlib

        with contextlib.suppress(OSError):
            fh.close()


AGENT_DEV_BRIEF_DESCRIPTOR: dict[str, Any] = {
    "name": "agent_dev_brief",
    "description": (
        "Clone a GitHub repo into the hosted workspace and hand a brief "
        "to the aider CLI, which edits files to match the brief and "
        "exits. The daemon then spawns the dev server (or Python bot) "
        "itself. Returns immediately; progress is visible via the "
        "daemon's live screen and action log. Daemon-only tool; the "
        "platform invokes it after /me/projects/:id/approve."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "template_repo_full": {
                "type": "string",
                "description": "GitHub repo in 'owner/repo' form, any stack.",
            },
            "slug": {
                "type": "string",
                "description": "Project slug (filesystem-safe). Workspace dir becomes /workspace/proj-<slug>.",
            },
            "project_id": {
                "type": "integer",
                "description": "Backend projects.id — used for cross-referencing in logs.",
            },
            "brief": {
                "type": "string",
                "description": "User's project brief in natural language. Handed to opencode verbatim.",
            },
            "port": {
                "type": "integer",
                "default": 3000,
                "description": "Dev-server port the daemon expects to expose. Default 3000.",
            },
            "kind": {
                "type": "string",
                "description": (
                    "Project kind from backend (eg 'tg_bot', 'landing', 'spa'). "
                    "For tg_bot, daemon skips package.json patching, tells "
                    "opencode it's a Python project, and uses Telegram getMe "
                    "instead of an HTTP port probe as the alive signal."
                ),
            },
            "bot_token": {
                "type": "string",
                "description": (
                    "Telegram BotFather token. Only sent for kind='tg_bot'. "
                    "Daemon writes it to <project_dir>/.env as BOT_TOKEN=… "
                    "before spawning the bot process."
                ),
            },
            "bot_username": {
                "type": "string",
                "description": (
                    "Telegram @username for the bot. Only sent for kind='tg_bot'. "
                    "Daemon includes it in the clone-status callback so backend "
                    "can stamp projects.bot_username confidently."
                ),
            },
        },
        "required": ["template_repo_full", "slug", "project_id", "brief"],
    },
}


AGENT_DEV_BRIEF_EDIT_DESCRIPTOR: dict[str, Any] = {
    "name": "agent_dev_brief_edit",
    "description": (
        "Re-spawn aider against an existing project workspace to apply "
        "an incremental edit. Does NOT clone, NOT install deps. Used by "
        "the AgentFlow chat → running project flow: the user sends a "
        "follow-up message, the platform routes it here, aider edits "
        "the source in place. The running dev server reloads "
        "automatically (vite/next watch). Returns immediately; progress "
        "streams to /agent-log."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Project slug (workspace at /workspace/proj-<slug>).",
            },
            "project_id": {
                "type": "integer",
                "description": "Backend projects.id — used in log cross-references.",
            },
            "edit_prompt": {
                "type": "string",
                "description": (
                    "Free-form edit instruction in natural language. "
                    "Handed to opencode with a thin wrapper that forbids "
                    "early-termination and demands at least one file edit."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Project kind. For 'tg_bot' the wrapper tells opencode "
                    "not to spawn `python bot.py` itself — the daemon owns "
                    "the bot lifecycle."
                ),
            },
        },
        "required": ["slug", "project_id", "edit_prompt"],
    },
}
