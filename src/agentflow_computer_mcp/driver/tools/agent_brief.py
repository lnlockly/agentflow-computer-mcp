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

import logging
import os
import shutil
import subprocess
import time
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

OPENCODE_PID_FILE = "/tmp/agent-brief-opencode.pid"
OPENCODE_LOG_FILE = "/tmp/agent-brief-opencode.log"


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

    cmd = [opencode_bin, "run", brief]
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


def _default_pid_alive(pid: int) -> bool:
    """Return True iff the process is still running. signal 0 just probes."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


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
    readback_seconds: float = 5.0,
    pid_alive: Callable[[int], bool] = _default_pid_alive,
    sleep: Callable[[float], None] = time.sleep,
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

    # The composed prompt tells opencode what shape we expect: install
    # deps, satisfy the brief, start the dev server on the project's
    # canonical port. Opencode picks the package manager + dev command
    # from the repo itself.
    composed = (
        f"You are bootstrapping a project from a GitHub template clone in {project_dir}. "
        f"Brief from the user: {brief.strip()}\n\n"
        "Steps:\n"
        "1. Inspect package.json / pyproject.toml / Cargo.toml to identify the stack.\n"
        "2. Install dependencies using the project's package manager (pnpm if pnpm-lock.yaml, "
        "yarn if yarn.lock, npm otherwise; uv/pip for Python).\n"
        "3. Edit files to satisfy the brief — landing copy, page content, brand, sections, etc.\n"
        f"4. Start the dev server bound to 0.0.0.0:{port} (Next.js: `next dev -H 0.0.0.0 -p {port}`).\n"
        "5. Confirm the server is reachable and the brief is reflected on the page.\n"
        "Do not ask the user for anything; act autonomously."
    )

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

    pid = int(spawn_res.get("pid", 0))

    # Diagnostic readback. The previous version returned ok immediately
    # after Popen, which means a crash in the first millisecond (missing
    # opencode binary, bad ANTHROPIC_BASE_URL, segfault, missing model
    # config) showed up as a silent `task.complete` with no signal. Wait
    # a few seconds, then surface either:
    #   - the early-exit reason (process gone, log content),
    #   - or the first chunk of the log so the cabinet can show "opencode
    #     printed: <first 2 KB>" while the long-running install proceeds.
    died_early = False
    if pid > 0 and readback_seconds > 0:
        # Probe every 100 ms up to the deadline. If the process is gone
        # before the deadline, mark it as died.
        steps = max(1, int(readback_seconds * 10))
        for _ in range(steps):
            sleep(0.1)
            if not pid_alive(pid):
                died_early = True
                break

    log_excerpt = ""
    try:
        with open(log_file, encoding="utf-8", errors="replace") as fh:
            log_excerpt = fh.read(4096)
    except OSError:
        pass

    if died_early:
        # Process died inside the readback window — almost always a config
        # problem (missing API key, bad URL, opencode crashed). Surface it
        # as a hard failure so the platform stops at provisioning + the
        # cabinet shows the reason instead of an infinite spinner.
        return {
            "ok": False,
            "error": "opencode_died_at_startup",
            "detail": (log_excerpt[-1500:] if log_excerpt else "no log captured")
            or "no log",
            "project_dir": project_dir,
            "opencode_pid": pid,
            "log_file": log_file,
        }

    # Still alive after 5 s — return the early log so the cabinet sees
    # something useful while the install + edits proceed in the background.
    return {
        "ok": True,
        "project_id": project_id,
        "slug": slug,
        "project_dir": project_dir,
        "repo_url": repo_url,
        "opencode_pid": pid,
        "log_file": log_file,
        "port": port,
        "opencode_boot_log": log_excerpt[:2000],
    }


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
