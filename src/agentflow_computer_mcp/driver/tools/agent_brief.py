"""OpenCode-driven project bootstrap for hosted daemons.

Phase A4 of the project-architecture refactor. Backend picks any repo
that matches the user's brief, daemon clones it, and an `opencode` CLI
session takes over: it installs deps, modifies code to match the brief,
and starts the dev server. The user watches it happen live in
`/cabinet/devices/<id>/live` (Xvfb screen) while opencode prints into
the daemon's task action log.

Why this shape:

* The backend has no business knowing how to install Node/pnpm/Bun or
  pick a dev command. Opencode does, per repo.
* The brief is the source of truth — opencode takes it as the user's
  ask and produces a working app. We don't pre-bake Next-only assumptions.
* We return immediately and let opencode run as a long-lived background
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


OPENCODE_PID_FILE = "/tmp/agent-brief-opencode.pid"
OPENCODE_LOG_FILE = "/tmp/agent-brief-opencode.log"


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


def _default_spawn_opencode(
    brief: str,
    cwd: str,
    *,
    pid_file: str = OPENCODE_PID_FILE,
    log_file: str = OPENCODE_LOG_FILE,
    env: dict[str, str] | None = None,
    opencode_bin: str = "opencode",
) -> dict[str, Any]:
    """Spawn `opencode run "<brief>"` as a detached background process.

    Opencode's `run` subcommand is non-interactive: it executes the brief
    end-to-end (install deps, edit files, start dev server when asked)
    and prints structured progress to stdout. We capture that into
    ``log_file`` so the daemon can stream it to ``device_action_log``.
    """
    try:
        log_fh = open(log_file, "ab", buffering=0)  # noqa: SIM115 — fd handed to child
    except OSError as exc:
        return {"ok": False, "error": "open_log_failed", "detail": str(exc)}

    # --dangerously-skip-permissions: opencode otherwise asks for
    # permission on every file outside its strict cwd guess, including
    # the project's own subdirectories — verified empirically in pod
    # hd-f86ecd7d-0 on 2026-05-26: every spawn died with
    # `permission requested: external_directory (/workspace/proj-*); auto-rejecting`
    # and exited as a zombie. The flag unblocks the agent for the
    # hosted sandbox we already enforce at the pod boundary.
    cmd = [opencode_bin, "run", "--dangerously-skip-permissions", brief]
    try:
        proc = subprocess.Popen(  # noqa: S603 — opencode is on PATH from image
            cmd,
            cwd=cwd,
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
        log.warning("could not write pid file %s: %s", pid_file, exc)

    return {"ok": True, "pid": proc.pid}


def agent_dev_brief(
    template_repo_full: str,
    slug: str,
    project_id: int,
    brief: str,
    *,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    port: int = DEFAULT_PORT,
    pid_file: str = OPENCODE_PID_FILE,
    log_file: str = OPENCODE_LOG_FILE,
    run: Callable[..., dict[str, Any]] = _default_run,
    spawn_opencode: Callable[..., dict[str, Any]] = _default_spawn_opencode,
    opencode_bin: str = "opencode",
    kind: str | None = None,
    bot_token: str | None = None,
    bot_username: str | None = None,
) -> dict[str, Any]:
    """Clone repo + hand the brief to opencode.

    Returns immediately after opencode is spawned. The dev-server port
    is *not* probed here — opencode owns the lifecycle and the cabinet's
    live screen tile + action log are the user's source of truth.

    Telegram-bot projects (``kind="tg_bot"``) take a different shape:
    the brief tells opencode to install Python deps + edit ``bot.py`` per
    the user's brief, but NOT to run anything. The daemon spawns
    ``python bot.py`` itself after opencode exits (see
    :func:`_watch_and_launch_tg_bot`) and uses Telegram's ``getMe`` API
    as the alive signal instead of an HTTP port probe — Python bots
    don't bind a listening port.
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

    # The composed prompt tells opencode what shape we expect: install
    # deps, satisfy the brief, start the dev server on the project's
    # canonical port. Opencode picks the package manager + dev command
    # from the repo itself.
    #
    # Step 4 used to read `Start the dev server bound to 0.0.0.0:{port}
    # (Next.js: next dev -H 0.0.0.0 -p {port})` and that wording caused a
    # real bug for our default static-starter template: opencode
    # interpreted the hint literally and ran `npm run dev -- --listen
    # 0.0.0.0:3000`, but the template's `serve` script does not accept a
    # `--listen` flag (serve v14 already binds to all interfaces with
    # `-l 3000`). The dev server crashed on first turn and the pod
    # served HTTP 502 forever. The new wording defers to the template's
    # own dev script and never tries to override host/port flags.
    #
    # `nohup ... & disown`: opencode `run` is single-shot — when it
    # exits, any child process inherits SIGHUP from the controlling tty
    # and dies. `nohup` plus `disown` reparents the dev server under
    # PID 1 (tini), so it keeps listening on $port after opencode is
    # gone. Without this, the dev server lived for ~2 seconds and the
    # public URL stayed 502.
    # No port numbers in the brief on purpose: the daemon pre-patched
    # `package.json` so `npm run dev` already binds to `$PORT`, and the
    # `PORT` env var is exported below. Telling opencode "bind to port
    # N" risks an LLM hallucination — wrong flag, wrong stack, wrong
    # number. The deterministic path is "trust the template + env".
    # Brief is split into IMPERATIVE blocks — opencode treats these as
    # commands, not questions. Earlier versions used quoted code blocks
    # ("`curl …`") and opencode mistook the prompt for a citation,
    # refused to run the server, and left the project stuck. Verified
    # 2026-05-26 on pod hd-f86ecd7d-0 (project 1506): edit ran, dev
    # server never started, opencode logged 'это оформлено как цитата'.
    if is_tg_bot:
        # Telegram bots: no HTTP dev server in the loop. opencode edits the
        # Python source to satisfy the brief, installs deps, and exits.
        # The daemon's `_watch_and_launch_tg_bot` thread then spawns
        # `python bot.py` itself — keeping the launch + alive-check off the
        # LLM's plate. Earlier landing-only wording ("npm run dev", "no
        # flags added", "do not modify package.json") was actively
        # misleading for Python projects: opencode would either invent
        # node commands or refuse to install Python deps.
        composed = (
            f"You are editing a Telegram bot at {project_dir}. "
            f"User's brief: {brief.strip()}\n\n"
            "You MUST complete every numbered step. Do not summarise. "
            "Do not ask for confirmation. Treat each shell command below as "
            "an instruction to execute, not text to quote back.\n\n"
            "1. Read pyproject.toml (or requirements.txt) to identify Python deps.\n"
            "2. Install dependencies. Use uv if `uv` is on PATH, else pip:\n"
            "       uv pip install --system -r requirements.txt  # preferred\n"
            "       pip install -r requirements.txt              # fallback\n"
            "   If neither file lists aiogram, run: pip install aiogram aiosqlite python-dotenv.\n"
            "3. Edit the bot's source code (typically `bot.py` and `handlers/` if present) "
            "so /start, /help, and the rest of the bot's behavior match the user's brief above. "
            "Use brief-derived copy in the bot's replies — never leave generic template strings.\n"
            "4. Do NOT run the bot yourself. Do NOT spawn `python bot.py`. "
            "The hosting daemon launches the bot process after you exit.\n"
            "5. End your run after step 3. Print a one-line summary of files you changed."
        )
    else:
        composed = (
            f"You are bootstrapping a project at {project_dir}. "
            f"User's brief: {brief.strip()}\n\n"
            "You have FULL access to the workspace via your built-in tools "
            "(Read, Write, Edit, Glob, Grep, Bash). You are NOT in a "
            "read-only mode. You are NOT asking for context. The user has "
            "already approved this run.\n\n"
            "Hard rules — break any of these and the run is a failure:\n"
            "  • You MUST write or edit at least one file under "
            f"{project_dir}/ before terminating.\n"
            "  • Reading files alone is NOT progress. After every Read/Glob, "
            "ask yourself: \"Have I edited anything yet?\" If no, keep going.\n"
            "  • Never reply with phrases like «нет доступа к структуре», "
            "«не выполнено в этом сообщении», \"I'd need more context\". If "
            "you feel you lack context, READ MORE FILES — never stop.\n"
            "  • Never ask the user a clarifying question. The brief above "
            "is final. Make reasonable assumptions where the brief is silent "
            "and proceed.\n\n"
            "Execute every numbered step below. Treat each shell command as "
            "an instruction to RUN, not text to quote.\n\n"
            "1. Identify the stack by reading package.json (or pyproject.toml / Cargo.toml).\n"
            "2. Install dependencies with the project's package manager: "
            "pnpm if pnpm-lock.yaml exists, yarn if yarn.lock exists, npm otherwise. "
            "For Python use uv if available, else pip.\n"
            "3. Edit project files to satisfy the user's brief. "
            "At minimum, change visible UI copy / routes / handlers to match "
            "the brief — never leave the template's default text in place. "
            "Do NOT modify package.json scripts — they have been pre-patched "
            "to read the PORT environment variable.\n"
            "4. Start the dev server. Run exactly this command in a shell, "
            "no flags added:\n"
            "       nohup npm run dev > /tmp/dev.log 2>&1 & disown\n"
            "   (Substitute pnpm or yarn for npm to match the lockfile.) "
            "The PORT environment variable is already set; npm passes it to the script.\n"
            "5. Verify the server replies. Run:\n"
            "       sleep 3 && curl -sf http://127.0.0.1:$PORT/ | head -c 200\n"
            "   The first attempt may be early — retry once after another `sleep 3` if it failed.\n"
            "6. End your run after step 5 succeeds. Do not stop the dev server.\n\n"
            "For Telegram bots or other non-HTTP runtimes: "
            "in step 4 spawn the entrypoint via `nohup … & disown`; "
            "in step 5 confirm the process is alive with `pgrep -f <entrypoint>` instead of curl."
        )

    # Pin opencode's provider to the AgentFlow gateway + a real model.
    # Without this opencode falls back to its built-in default
    # (`gpt-5.3-chat-latest`) which our gateway does not list, and the
    # subprocess dies on first turn with «Model not available».
    # Project-local `opencode.json` wins over `~/.config/opencode/`.
    # AF_API_KEY is the per-pod owner key already injected by
    # renderDaemonManifest (agentflow-agents).
    api_key = os.environ.get("AF_API_KEY", "")
    if api_key:
        # Use the logical "flow" model. The AgentFlow gateway picks the
        # actual upstream (gpt-5.3-codex → gpt-5.5 → opus → sonnet → haiku)
        # at request time, so swapping models never requires rebuilding
        # this image. Companion change: PR agentflow-agents#901 adds the
        # "flow" alias resolution in /llm/v1/{chat/completions,messages,
        # responses}.
        #
        # opencode 1.15.x's `build` sub-agent has its own default model
        # that overrides the root `model` key — pin every agent to
        # openai/flow so the gateway stays in charge of selection.
        opencode_cfg = {
            "$schema": "https://opencode.ai/config.json",
            "model": "openai/flow",
            "permission": {
                "edit": "allow",
                "bash": "allow",
                "webfetch": "allow",
            },
            "agent": {
                "build": {"model": "openai/flow"},
                "plan": {"model": "openai/flow"},
                "general": {"model": "openai/flow"},
            },
            "provider": {
                "openai": {
                    "options": {
                        "baseURL": (
                            _normalise_api_base(
                                os.environ.get("AF_API_URL", "https://agentflow.website")
                            )
                            + "/llm/v1"
                        ),
                        "apiKey": api_key,
                    },
                    "models": {"flow": {}},
                }
            },
        }
        try:
            Path(project_dir, "opencode.json").write_text(
                json.dumps(opencode_cfg, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("could not write opencode.json: %s", exc)

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


OPENCODE_EDIT_PID_FILE = "/tmp/agent-brief-opencode-edit.pid"
OPENCODE_EDIT_LOG_FILE = "/tmp/agent-brief-opencode-edit.log"


def agent_dev_brief_edit(
    slug: str,
    project_id: int,
    edit_prompt: str,
    *,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    pid_file: str = OPENCODE_EDIT_PID_FILE,
    log_file: str = OPENCODE_EDIT_LOG_FILE,
    spawn_opencode: Callable[..., dict[str, Any]] = _default_spawn_opencode,
    opencode_bin: str = "opencode",
    kind: str | None = None,
) -> dict[str, Any]:
    """Re-spawn opencode against an existing project workspace.

    Unlike :func:`agent_dev_brief` this does NOT clone, NOT install deps,
    NOT touch ``opencode.json`` or ``package.json``. The workspace is
    expected to already exist from a prior ``agent_dev_brief`` run. The
    edit prompt is handed to opencode verbatim with a thin instruction
    wrapper so the model stays grounded in the brief shape (no
    early-termination, write files, then verify).

    Used by the AgentFlow chat → running-project edit flow: the user
    sends a message like «сделай кнопку красной», the platform routes
    it here, opencode edits the source, the running dev server reloads
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


def _watch_and_report_clone_status(
    *,
    project_id: int,
    slug: str,
    port: int,
    project_dir: str,
    repo_url: str,
    timeout_sec: float = 900.0,
    poll_interval_sec: float = 5.0,
) -> None:
    """Background watcher: poll the dev port + POST clone-status.

    Polls `http://127.0.0.1:{port}/` once every ``poll_interval_sec``.
    On the first 2xx/3xx response, POSTs a successful clone-status to
    the backend with ``pod_ip`` so the auto-expose step can wire the
    public ingress. If the port never opens within ``timeout_sec``
    (~15 min — long enough for `pnpm install` + `next build`), reports
    `ok=false, error=port_unreachable`. Idempotent on the backend side:
    re-running clone-status repoints the existing Service/Endpoints.
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
    deadline = time.monotonic() + timeout_sec
    port_reachable = False
    while time.monotonic() < deadline:
        if _http_probe(port):
            port_reachable = True
            break
        time.sleep(poll_interval_sec)

    body = {
        "ok": port_reachable,
        "port_reachable": port_reachable,
        "port": port,
        "project_dir": project_dir,
        "repo_url": repo_url,
        "pod_ip": pod_ip,
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
        "to the opencode CLI, which then installs deps, edits files to "
        "match the brief, and starts the dev server. Returns immediately; "
        "progress is visible via the daemon's live screen and action log. "
        "Daemon-only tool; the platform invokes it after "
        "/me/projects/:id/approve."
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
        "Re-spawn opencode against an existing project workspace to apply "
        "an incremental edit. Does NOT clone, NOT install deps, NOT "
        "rewrite opencode.json. Used by the AgentFlow chat → running "
        "project flow: the user sends a follow-up message, the platform "
        "routes it here, opencode edits the source in place. The running "
        "dev server reloads automatically (vite/next watch). Returns "
        "immediately; progress streams to /agent-log."
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
