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
) -> dict[str, Any]:
    """Clone repo + hand the brief to opencode.

    Returns immediately after opencode is spawned. The dev-server port
    is *not* probed here — opencode owns the lifecycle and the cabinet's
    live screen tile + action log are the user's source of truth.
    """
    if not _looks_like_repo_full(template_repo_full):
        return {"ok": False, "error": "invalid_template_repo_full"}
    if not _looks_like_slug(slug):
        return {"ok": False, "error": "invalid_slug"}
    if not isinstance(project_id, int) or project_id <= 0:
        return {"ok": False, "error": "invalid_project_id"}
    if not brief or not brief.strip():
        return {"ok": False, "error": "missing_brief"}

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

    # Pin the dev-server port deterministically — no LLM in the loop. The
    # shared daemon pod hosts many projects from one user, and each one
    # gets a unique port assigned by the backend (`projects.preview_port`)
    # so previews stay independent. Patching the template's `package.json`
    # to read `${PORT:-<default>}` lets the daemon hand the port via the
    # PORT env (already injected below) without trusting opencode to
    # follow an instruction. npm executes `scripts.dev` under `sh -c`, so
    # POSIX-style parameter expansion always works.
    _patch_package_json_for_port(Path(project_dir) / "package.json", port)

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
    composed = (
        f"You are bootstrapping a project at {project_dir}. "
        f"User's brief: {brief.strip()}\n\n"
        "You MUST complete every numbered step. Do not summarise. "
        "Do not ask for confirmation. Treat each shell command below as "
        "an instruction to execute, not text to quote back.\n\n"
        "1. Identify the stack by reading package.json (or pyproject.toml / Cargo.toml).\n"
        "2. Install dependencies with the project's package manager: "
        "pnpm if pnpm-lock.yaml exists, yarn if yarn.lock exists, npm otherwise. "
        "For Python use uv if available, else pip.\n"
        "3. Edit project files to satisfy the user's brief. "
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
        opencode_cfg = {
            "$schema": "https://opencode.ai/config.json",
            "model": "openai/gpt-5.3-codex",
            "permission": {
                "edit": "allow",
                "bash": "allow",
                "webfetch": "allow",
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
                    "models": {"gpt-5.3-codex": {}},
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
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — internal URL only
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        log.warning(
            "clone-status report failed for project %d: %s", project_id, exc
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
        },
        "required": ["template_repo_full", "slug", "project_id", "brief"],
    },
}
