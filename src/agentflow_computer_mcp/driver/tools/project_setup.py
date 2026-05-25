"""Project clone + dev-server bootstrap for hosted daemons.

Phase A3 of the project-architecture refactor. The platform picks a
Next.js template from GitHub (e.g.
``ixartz/Next-JS-Landing-Page-Starter-Template``) and asks the hosted
daemon to turn that template into a working project: clone the repo,
detach git history, install dependencies, and start a dev server on the
canonical port (3000).

When the dev server replies on the expected port the daemon POSTs the
result back to the backend so the platform can flip
``projects.status`` from ``provisioning`` to ``running`` and surface a
preview URL in the cabinet.

Design rules (mirror ``integrations.py``):

* Every side-effect (subprocess, sleep, HTTP, port probe, package-manager
  detection) goes through an injectable callable so the unit tests don't
  touch the host shell.
* Return shape is always ``{ok: bool, ...}`` with a stable ``error``
  string code on failure, so the cabinet UI and prompts can map the
  reason to user copy without parsing English.
* Secrets (``internal_secret``) never appear in the returned dict.
* If the backend POST fails the local clone+install state is preserved
  and the function still returns ``ok=True`` for the clone half but
  flags ``reported=False`` — the caller can re-report from a higher
  layer instead of rolling back a 5 GB ``node_modules``.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ROOT = "/workspace"
DEFAULT_PORT = 3000
DEFAULT_WAIT_TIMEOUT_S = 90
DEV_PID_FILE = "/tmp/project-dev.pid"
DEV_LOG_FILE = "/tmp/project-dev.log"

# Repos are validated against this rough shape before we hand them to git.
# Real validation lives on the backend; this is defence-in-depth so a
# malformed payload doesn't shell-inject through a `git clone` argument.
_REPO_FULL_RE = "abcdefghijklmnopqrstuvwxyz0123456789._-"


def _looks_like_repo_full(value: str) -> bool:
    """Cheap whitelist check for ``owner/repo`` shape."""
    if not value or "/" not in value:
        return False
    owner, _, repo = value.partition("/")
    if not owner or not repo or "/" in repo:
        return False
    for part in (owner, repo):
        lowered = part.lower()
        if any(ch not in _REPO_FULL_RE for ch in lowered):
            return False
    return True


def _looks_like_slug(value: str) -> bool:
    """Slug used for the workspace directory. Must be filesystem-safe."""
    if not value or len(value) > 64:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in value)


# --- subprocess + port-probe defaults -------------------------------------


def _default_run(
    cmd: Sequence[str],
    cwd: str | None = None,
    *,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a foreground subprocess. Returns ``{exit_code, stdout, stderr}``."""
    try:
        proc = subprocess.run(  # noqa: S603 — args are pre-validated
            list(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": -1,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": f"timeout after {timeout}s",
        }
    return {
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[:8000],
        "stderr": (proc.stderr or "")[:4000],
    }


def _default_spawn_background(
    cmd: Sequence[str],
    cwd: str,
    *,
    pid_file: str = DEV_PID_FILE,
    log_file: str = DEV_LOG_FILE,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn a detached background process and persist its PID.

    Writes both stdout and stderr to ``log_file`` so a tail can pick it
    up later. The parent returns immediately; the child keeps running
    after the MCP tool dispatch completes.
    """
    try:
        log_fh = open(log_file, "ab", buffering=0)  # noqa: SIM115 — file ownership transfers to child
    except OSError as exc:
        return {"ok": False, "error": "open_log_failed", "detail": str(exc)}
    try:
        proc = subprocess.Popen(  # noqa: S603 — args are pre-validated
            list(cmd),
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
        # Popen dup'd the fd; closing ours is safe and avoids leaking
        # into the daemon's own file table.
        import contextlib

        with contextlib.suppress(OSError):
            log_fh.close()

    try:
        Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write pid file %s: %s", pid_file, exc)
    return {"ok": True, "pid": proc.pid}


def _default_port_check(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """Return True iff a TCP connect to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (TimeoutError, OSError):
        return False


def _default_http_post(
    url: str, body: dict[str, Any], internal_secret: str, timeout_s: int = 15
) -> tuple[int, Any]:
    """POST JSON with the internal-secret header. Returns ``(status, body)``."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "x-agentflow-secret": internal_secret,
            "user-agent": "agentflow-desktop/0.5 (project-setup)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed


# --- package manager + dev command detection ------------------------------


_PACKAGE_MANAGER_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)


def detect_package_manager(project_dir: str) -> str:
    """Pick a package manager by inspecting lock files. Default: npm."""
    base = Path(project_dir)
    for lock, name in _PACKAGE_MANAGER_LOCKFILES:
        if (base / lock).is_file():
            return name
    return "npm"


def detect_dev_command(project_dir: str, package_manager: str, port: int) -> list[str]:
    """Return argv for the project's dev command.

    Prefers ``scripts.dev`` and falls back to ``scripts.start``. Next.js
    projects (``dependencies.next`` present) accept ``-- --port <port>``
    so we always pass that through; for non-Next projects the port flag
    is harmless when the user's script ignores it.
    """
    base = Path(project_dir)
    pkg_path = base / "package.json"
    script = "dev"
    is_next = False
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pkg = {}
        scripts = pkg.get("scripts") or {}
        if "dev" in scripts:
            script = "dev"
        elif "start" in scripts:
            script = "start"
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        is_next = "next" in deps

    # Each PM forwards extra args to the underlying script with `--`.
    # Next.js dev (`next dev`) accepts `--port <n>` directly so we only
    # add the flag when we know we're dealing with a Next project.
    argv = [package_manager, "run", script]
    if is_next:
        argv += ["--", "--port", str(port)]
    return argv


# --- main entry point -----------------------------------------------------


def project_clone_and_setup(
    template_repo_full: str,
    slug: str,
    project_id: int,
    *,
    api_base: str,
    internal_secret: str,
    workspace_root: str = DEFAULT_WORKSPACE_ROOT,
    port: int = DEFAULT_PORT,
    dev_wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
    pid_file: str = DEV_PID_FILE,
    log_file: str = DEV_LOG_FILE,
    run: Callable[..., dict[str, Any]] = _default_run,
    spawn_background: Callable[..., dict[str, Any]] = _default_spawn_background,
    port_check: Callable[[str, int], bool] = _default_port_check,
    sleep: Callable[[float], None] = time.sleep,
    http_post: Callable[[str, dict[str, Any], str], tuple[int, Any]] = _default_http_post,
    detect_pm: Callable[[str], str] = detect_package_manager,
    detect_dev: Callable[[str, str, int], list[str]] = detect_dev_command,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Clone, install, run, probe, report.

    Returns a small JSON-serialisable dict. ``ok`` is True only when
    every stage (clone → reinit-git → install → dev-spawn) succeeded;
    ``port_reachable`` is True only when the dev server replied within
    ``dev_wait_timeout_s``. ``reported`` is True only when the backend
    POST got a 2xx.
    """
    if not _looks_like_repo_full(template_repo_full):
        return {"ok": False, "error": "invalid_template_repo_full"}
    if not _looks_like_slug(slug):
        return {"ok": False, "error": "invalid_slug"}
    if not isinstance(project_id, int) or project_id <= 0:
        return {"ok": False, "error": "invalid_project_id"}
    if not api_base or not internal_secret:
        return {"ok": False, "error": "missing_backend_config"}

    project_dir_name = f"proj-{slug}"
    project_dir = str(Path(workspace_root) / project_dir_name)
    repo_url = f"https://github.com/{template_repo_full}.git"

    # 1. Workspace root must exist and be writable.
    try:
        Path(workspace_root).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "workspace_root_unwritable", "detail": str(exc)}

    # 2. Clean any half-cloned directory left over from a previous attempt.
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

    # 3. git clone (depth=1 keeps it fast; we throw the history away in step 4).
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

    # 4. Re-init git so the user owns the history. Remove the upstream
    #    .git first; `rm -rf` is safe here because the path is fully
    #    qualified and we just cloned into it ourselves.
    dot_git = Path(project_dir) / ".git"
    try:
        if dot_git.exists():
            shutil.rmtree(dot_git)
    except OSError as exc:
        return {
            "ok": False,
            "error": "git_history_strip_failed",
            "detail": str(exc),
        }

    git_user_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "agentflow-daemon",
        "GIT_AUTHOR_EMAIL": "daemon@agentflow.website",
        "GIT_COMMITTER_NAME": "agentflow-daemon",
        "GIT_COMMITTER_EMAIL": "daemon@agentflow.website",
    }
    for step in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", f"init from template {template_repo_full}"],
    ):
        step_res = run(step, cwd=project_dir, timeout=60, env=git_user_env)
        if step_res.get("exit_code") != 0:
            return {
                "ok": False,
                "error": "git_init_failed",
                "step": " ".join(step),
                "detail": (step_res.get("stderr") or step_res.get("stdout") or "")[:1000],
            }

    # 5. Pick a package manager by lockfile.
    pm = detect_pm(project_dir)

    # 6. Install dependencies.
    install_cmd = {
        "npm": ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        "pnpm": ["pnpm", "install", "--prefer-frozen-lockfile=false"],
        "yarn": ["yarn", "install", "--non-interactive"],
    }[pm]
    install_res = run(install_cmd, cwd=project_dir, timeout=600)
    if install_res.get("exit_code") != 0:
        return {
            "ok": False,
            "error": "install_failed",
            "package_manager": pm,
            "detail": (install_res.get("stderr") or install_res.get("stdout") or "")[
                :2000
            ],
        }

    # 7. Detect + spawn the dev server.
    dev_argv = detect_dev(project_dir, pm, port)
    # `nohup`-style detachment is handled by start_new_session=True in
    # the default spawn helper. The dev process keeps running after this
    # MCP tool dispatch returns; the PID lives in pid_file.
    spawn_res = spawn_background(
        dev_argv,
        cwd=project_dir,
        pid_file=pid_file,
        log_file=log_file,
        env={**os.environ, "PORT": str(port), "BROWSER": "none", "CI": "1"},
    )
    if not spawn_res.get("ok"):
        return {
            "ok": False,
            "error": spawn_res.get("error", "spawn_failed"),
            "detail": spawn_res.get("detail"),
            "dev_command": " ".join(shlex.quote(p) for p in dev_argv),
        }
    dev_pid = int(spawn_res.get("pid", 0))

    # 8. Wait for port 3000.
    deadline = now() + max(5, dev_wait_timeout_s)
    port_reachable = False
    attempts = 0
    while now() < deadline:
        attempts += 1
        if port_check("127.0.0.1", port):
            port_reachable = True
            break
        sleep(1.0)

    # 9. Report to backend (best-effort).
    report_body = {
        "ok": port_reachable,
        "port_reachable": port_reachable,
        "dev_pid": dev_pid,
        "repo_url": repo_url,
        "template_repo_full": template_repo_full,
        "slug": slug,
        "package_manager": pm,
        "dev_command": " ".join(shlex.quote(p) for p in dev_argv),
        "port": port,
        "attempts": attempts,
    }
    reported = False
    report_status = None
    report_error = None
    try:
        status, _body = http_post(
            f"{api_base.rstrip('/')}/internal/projects/{project_id}/clone-status",
            report_body,
            internal_secret,
        )
        report_status = status
        reported = 200 <= status < 300
        if not reported:
            report_error = (
                str(_body.get("error")) if isinstance(_body, dict) else f"http_{status}"
            )
    except Exception as exc:  # noqa: BLE001 — backend can be down; clone still succeeded
        report_error = f"{exc.__class__.__name__}: {exc}"

    return {
        "ok": port_reachable,
        "project_id": project_id,
        "slug": slug,
        "project_dir": project_dir,
        "repo_url": repo_url,
        "package_manager": pm,
        "dev_command": " ".join(shlex.quote(p) for p in dev_argv),
        "dev_pid": dev_pid,
        "port": port,
        "port_reachable": port_reachable,
        "wait_attempts": attempts,
        "reported": reported,
        "report_status": report_status,
        "report_error": report_error,
    }


# Tool descriptor consumed by ``desktop_tools.all_tool_descriptors``.
PROJECT_CLONE_AND_SETUP_DESCRIPTOR: dict[str, Any] = {
    "name": "project_clone_and_setup",
    "description": (
        "Clone a GitHub template into the hosted workspace, reset git, "
        "install dependencies, start the dev server on port 3000, and "
        "report status back to the AgentFlow backend. Returns "
        "{ok, port_reachable, dev_pid, project_dir} on success or "
        "{ok:false, error:<code>} on failure. Daemon-only tool; the "
        "platform invokes it after /me/projects/:id/approve."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "template_repo_full": {
                "type": "string",
                "description": "GitHub repo in 'owner/repo' form, e.g. 'ixartz/Next-JS-Landing-Page-Starter-Template'.",
            },
            "slug": {
                "type": "string",
                "description": "Project slug (filesystem-safe). Workspace dir becomes /workspace/proj-<slug>.",
            },
            "project_id": {
                "type": "integer",
                "description": "Backend projects.id — used to POST /internal/projects/:id/clone-status.",
            },
            "port": {
                "type": "integer",
                "default": 3000,
                "description": "Dev-server port to wait for. Default 3000.",
            },
        },
        "required": ["template_repo_full", "slug", "project_id"],
    },
}
